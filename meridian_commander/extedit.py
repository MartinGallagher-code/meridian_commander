# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Handing a file to an outside program -- an editor for F4, a pager for F3.

Editing with an outside editor -- vi, vim, or whatever ``[ui] editor`` names.

The built-in editor is deliberately small, and for a config file or a quick
correction that is the right size.  People who live in vi do not want it, so
F4 can hand the file to a real editor instead: set ``[ui] editor`` (from
Options > Editor, or in the file by hand) and the built-in one steps aside.

Two things make this more than "run the command":

*A remote file has no path the editor could open.*  Only the local backend has
a real file on this machine; an SFTP, FTP or archive pane has bytes behind a
connection.  So a remote file is fetched to a private temporary directory,
edited there, and written back -- and only when it actually changed, so
quitting vim with ``:q`` costs no upload.

*An upload can fail after the editing is done.*  A dropped connection or an
archive that cannot be rewritten would otherwise throw the work away, so when
the write-back fails the temporary copy is deliberately **not** deleted and the
message says where it is.

Viewing with an outside pager -- ``less``, or whatever ``[ui] viewer`` names --
is the same errand with the second half removed: fetch the same way, run the
same way, and send nothing back, because a pager has nothing to send.
"""

from __future__ import annotations

import os
import shlex
import tempfile

#: How much of a remote file is fetched for editing. The same ceiling the
#: built-in editor uses: a file manager that quietly pulls a gigabyte down an
#: SFTP connection because a key was pressed is not being helpful.
MAX_FETCH_BYTES = 8 * 1024 * 1024


def editor_argv(setting: str) -> list[str] | None:
    """The configured editor as an argv, or ``None`` for the built-in one.

    ``setting`` is the raw ``[ui] editor`` value.  Blank means the built-in
    editor -- the default, and not an error.  Anything else is a command line
    in shell syntax, so ``emacs -nw`` and ``$EDITOR`` both work; the whole
    string is expanded before it is split, which is what makes an ``EDITOR``
    of ``"code --wait"`` arrive as two words rather than one impossible
    filename.

    Raises :class:`ValueError` when the setting is present but unusable, which
    is worth saying out loud: silently falling back to the built-in editor
    would look like the setting being ignored.
    """
    text = (setting or "").strip()
    if not text:
        return None
    expanded = os.path.expandvars(text).strip()
    if not expanded:
        raise ValueError(f"{text} expands to nothing")
    try:
        parts = [part for part in shlex.split(expanded) if part]
    except ValueError as exc:
        raise ValueError(f"{text} is not a usable command ({exc})") from exc
    if not parts:
        raise ValueError(f"{text} expands to nothing")
    # expandvars leaves an unset name alone, so a leading "$" here is the
    # variable the user hoped for rather than a program that happens to start
    # with one.
    for part in parts:
        if part.startswith("$"):
            raise ValueError(f"the environment does not set {part}")
    return [os.path.expanduser(part) for part in parts]


def edit(fs, path: str, argv: list[str], run) -> str:
    """Edit ``path`` with ``argv``; returns a line for the status bar.

    ``run(cmd, cwd)`` hands the real terminal to ``cmd`` and returns its exit
    status, or ``None`` if it could not start -- :meth:`App._suspend_and_run`
    in practice, a stub in the tests.
    """
    return _errand(Session(fs, path, argv, editing=True), run)


def view(fs, path: str, argv: list[str], run) -> str:
    """Show ``path`` with ``argv``; returns a line for the status bar.

    The same two cases :func:`edit` has, without the write-back.  A local file
    is opened where it lives; a remote one is fetched to a private copy which
    is removed afterwards however the pager exits -- there is no work in it to
    lose, so the copy that :func:`edit` deliberately keeps has no reason to
    survive here.
    """
    return _errand(Session(fs, path, argv, editing=False), run)


def _errand(session: Session, run) -> str:
    """Start, run and finish ``session``, waiting for the program."""
    try:
        started = session.start()
        if isinstance(started, str):
            return started
        return session.finish(run(*started))
    finally:
        session.close()


class Session:
    """One file handed to an outside program, from fetching to writing back.

    :func:`edit` and :func:`view` wait for the program, so they run the three
    steps back to back.  A program running in a pane (MC-025) does not block
    anything: the pane calls :meth:`start` to get the command, lets it run
    beside the listing, and calls :meth:`finish` once it has exited.  Either
    way :meth:`close` removes the working copy, unless :meth:`finish` kept it
    because the write-back failed.

    *A local file* is opened where it lives.  That is the point of an outside
    editor -- ``:w`` writes the real file, the permissions and ownership stay
    as they were, and the editor's own backup and undo files land beside it
    as their owner expects.

    *A remote file* is fetched to a private copy first, in a directory of its
    own (0700, so a file from a server is not left readable by everyone else
    on this machine), and written back only when it changed.
    """

    def __init__(self, fs, path: str, argv: list[str], editing: bool) -> None:
        self.fs = fs
        self.path = path
        self.argv = argv
        self.editing = editing
        self.name = fs.basename(path) or path
        #: The working copy of a remote file, and the directory made for it.
        self.copy: str | None = None
        self.workdir: str | None = None
        self.before = b""
        #: Whether the copy holds an edit that could not be written back.
        self.kept = False

    def start(self) -> tuple[list[str], str | None] | str:
        """The command and the directory to run it in, or why there is none."""
        local = getattr(self.fs, "local_path", None)
        if local is not None:
            real = local(self.path)
            return self.argv + [real], os.path.dirname(real) or None
        try:
            data = _fetch(self.fs, self.path)
        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            return f"Cannot read {self.name}: {exc}"
        prefix = "meridian-edit-" if self.editing else "meridian-view-"
        self.workdir = tempfile.mkdtemp(prefix=prefix)
        self.copy = os.path.join(self.workdir, _copy_name(self.name))
        self.before = data
        with open(self.copy, "wb") as handle:
            handle.write(data)
        return self.argv + [self.copy], self.workdir

    def finish(self, status: int | None) -> str:
        """Write back what changed; a line for the status bar.

        ``status`` is the program's exit status, or ``None`` if it never
        started.
        """
        if not self.editing:
            return _viewed(self.name, self.argv, status)
        if status is None:
            return f"Could not start {self.argv[0]}"
        if self.copy is None:
            if status != 0:
                return (f"{self.argv[0]} exited {status} -- "
                        f"{self.name} may be unsaved")
            return f"Edited {self.name}"
        try:
            with open(self.copy, "rb") as handle:
                after = handle.read()
        except OSError:
            # The editor deleted or never wrote it: nothing to send back.
            return f"{self.name} unchanged"
        if after == self.before:
            return f"{self.name} unchanged"
        try:
            writer = self.fs.open_write(self.path)
            try:
                writer.write(after)
            finally:
                writer.close()
        except Exception as exc:
            self.kept = True
            return (f"Could not save {self.name}: {exc} -- "
                    f"your edit is in {self.copy}")
        return f"Saved {self.name} ({len(after)} bytes)"

    def close(self) -> None:
        """Remove the working copy, unless it holds an edit not written back."""
        if self.copy is not None and not self.kept:
            _discard(self.workdir or "", self.copy)


def _viewed(name: str, argv: list[str], status: int | None) -> str:
    """What to say about a pager that has finished."""
    if status is None:
        return f"Could not start {argv[0]}"
    if status != 0:
        return f"{argv[0]} exited {status}"
    return f"Viewed {name}"


def _copy_name(name: str) -> str:
    """A filename for the working copy that stays inside the directory made
    for it.

    The remote name is kept -- the extension is what tells the editor which
    syntax to highlight -- but it is a name from another machine, so anything
    that would make it a path here is flattened first.
    """
    safe = name.replace("/", "_").replace(os.sep, "_").strip()
    if safe in ("", ".", ".."):
        return "meridian-edit.txt"
    return safe


def _fetch(fs, path: str) -> bytes:
    """The file's bytes, or empty for one that does not exist yet."""
    if not fs.exists(path):
        return b""
    reader = fs.open_read(path)
    try:
        data = reader.read(MAX_FETCH_BYTES + 1) or b""
    finally:
        try:
            reader.close()
        except Exception:
            pass
    if len(data) > MAX_FETCH_BYTES:
        raise ValueError(
            f"{fs.basename(path)} is larger than "
            f"{MAX_FETCH_BYTES // (1024 * 1024)} MB -- too big to edit here")
    return data


def _discard(workdir: str, copy: str) -> None:
    """Remove the working copy, best effort: it is a temporary file."""
    for step in (lambda: os.unlink(copy), lambda: os.rmdir(workdir)):
        try:
            step()
        except OSError:
            pass
