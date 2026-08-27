"""Editing with an outside editor -- vi, vim, or whatever ``[ui] editor`` names.

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

*An upload can fail after the editing is done.*  A dropped connection or a
read-only archive would otherwise throw the work away, so when the write-back
fails the temporary copy is deliberately **not** deleted and the message says
where it is.
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
    name = fs.basename(path) or path
    local = getattr(fs, "local_path", None)
    if local is not None:
        return _edit_in_place(local(path), name, argv, run)
    return _edit_via_copy(fs, path, name, argv, run)


def _edit_in_place(real: str, name: str, argv: list[str], run) -> str:
    """The local case: the editor opens the file itself.

    Which is the point of using it -- ``:w`` writes the real file, the
    permissions and ownership stay as they were, and the editor's own backup
    and undo files land beside it as their owner expects.
    """
    status = run(argv + [real], os.path.dirname(real) or None)
    if status is None:
        return f"Could not start {argv[0]}"
    if status != 0:
        return f"{argv[0]} exited {status} -- {name} may be unsaved"
    return f"Edited {name}"


def _edit_via_copy(fs, path: str, name: str, argv: list[str], run) -> str:
    """The remote case: fetch, edit a private copy, write back if changed."""
    try:
        before = _fetch(fs, path)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Cannot read {name}: {exc}"

    # 0700, so a file fetched from a server is not left readable by everyone
    # else on this machine while it is being edited.
    workdir = tempfile.mkdtemp(prefix="meridian-edit-")
    keep = False
    copy = os.path.join(workdir, _copy_name(name))
    try:
        with open(copy, "wb") as handle:
            handle.write(before)
        status = run(argv + [copy], workdir)
        if status is None:
            return f"Could not start {argv[0]}"
        try:
            with open(copy, "rb") as handle:
                after = handle.read()
        except OSError:
            # The editor deleted or never wrote it: nothing to send back.
            return f"{name} unchanged"
        if after == before:
            return f"{name} unchanged"
        try:
            writer = fs.open_write(path)
            try:
                writer.write(after)
            finally:
                writer.close()
        except Exception as exc:
            keep = True
            return f"Could not save {name}: {exc} -- your edit is in {copy}"
        return f"Saved {name} ({len(after)} bytes)"
    finally:
        if not keep:
            _discard(workdir, copy)


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
