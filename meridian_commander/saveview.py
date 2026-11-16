# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Saving what a browser shows as a file of its own: ``F2``, or ``Ctrl-S``.

The table browser narrows a results file down -- filters, a sort, columns
hidden -- and this is how what it narrowed to is kept.  The spreadsheet grid
uses it too, for the sheet on the screen, and so can any browser after them
with a view worth keeping: what is the same whatever is being saved lives
here, once.

- **The name offered.**  ``results.view.tsv`` beside ``results.tsv``,
  numbered (``results.view2.tsv``) when that is taken, so ``Enter`` alone
  never replaces anything.  Typing over it chooses another name, and the
  browser reads the format from the name chosen.
- **Where.**  Beside the file, unless that directory cannot take a new one:
  inside an archive, where a new file means writing the whole archive out
  again, or a local directory the user may not write to.  The other pane's
  directory is offered then, and the prompt says why; when that cannot be
  written either, nothing is offered and the footer says so.  A remote
  directory cannot be asked without writing something, so there the write
  is tried and a refusal reported as its failure.
- **The checks.**  The file being browsed is never written over, whatever
  name reaches it, and an existing file is replaced only after a yes.
- **The write** goes through the pane's own backend, so a view saves on an
  SFTP, SSH or FTP pane as on a local one, and a remote close that fails --
  where paramiko and FTP actually send -- is a failed save, not a quiet one.

The plug-ins write their output through the same two helpers,
:func:`derive_output_path` and :func:`write_bytes`: they live here, in the
core, and ``plugins/_tabular`` and ``plugins/_io`` hand them on.
"""

from __future__ import annotations

import curses
import os
from typing import Callable, Protocol, Sequence, Tuple

from . import dialogs, theme
from .archive import ArchiveFileSystem
from .filesystems import FileSystem
from .operations import overlapping

#: The keys that save the view: the editor's own save keys (19 is Ctrl-S).
SAVE_KEYS = (curses.KEY_F2, 19)

#: A file written: its backend and its path.
Written = Tuple[FileSystem, str]


class Place(Protocol):
    """Somewhere else a view may be saved: the other pane, which a
    :class:`~meridian_commander.panel.Panel` is."""

    fs: FileSystem
    path: str


def derive_output_path(fs, path: str, suffix: str) -> str:
    """A sibling path like ``sales.cleaned.csv``, numbered if it already exists."""
    directory = fs.dirname(path)
    base = fs.basename(path)
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""

    def build(tag: str) -> str:
        name = f"{stem}.{tag}.{ext}" if ext else f"{stem}.{tag}"
        return fs.join(directory, name)

    candidate = build(suffix)
    n = 2
    while fs.exists(candidate):
        candidate = build(f"{suffix}{n}")
        n += 1
    return candidate


def write_bytes(fs, path, data: bytes) -> None:
    """Write ``data`` to ``path`` through the filesystem backend.

    ``close()`` is *not* swallowed here, unlike on the read side.  A remote
    backend sends on close -- paramiko flushes there, and the FTP writer
    raises there when the transfer failed -- so swallowing it turned a write
    that never arrived into a plug-in reporting "changed a.txt" for a file it
    had emptied.  A failure on the way in is only a closed handle; a failure
    on the way out is the write.
    """
    stream = fs.open_write(path)
    try:
        stream.write(data)
    finally:
        closer = getattr(stream, "close", None)
        if callable(closer):
            closer()


def shape(rows: int, columns: int) -> str:
    """``1,204 rows × 7 columns``: how much a save holds, as the footer says it."""
    def count(number: int, noun: str) -> str:
        return f"{number:,} {noun}" + ("" if number == 1 else "s")

    return f"{count(rows, 'row')} {theme.glyph('times')} {count(columns, 'column')}"


def _may_write(directory: str) -> bool:
    """Whether this user may make a file in the local ``directory``."""
    return os.access(directory, os.W_OK)


def unwritable(fs: FileSystem, directory: str) -> str | None:
    """Why a new file cannot be put in ``directory``, or ``None``.

    Said as the end of a sentence about the directory.  ``None`` also means
    "cannot be known without trying", which is every remote directory: only
    writing there would tell, and a failure then is reported as the write's.
    """
    if isinstance(fs, ArchiveFileSystem):
        # It can be written -- a copy into an archive pane does it -- but only
        # by writing the whole archive out again, which a save meant to keep
        # a filtered table should not do behind the user's back.
        return f"is inside the archive {fs.name}"
    if fs.scheme == "local" and not _may_write(directory):
        return "cannot be written to"
    return None


def resolve(fs: FileSystem, directory: str, typed: str) -> str:
    """A typed name made a path: as it is when absolute, else in ``directory``."""
    if typed.startswith(fs.sep):
        return fs.normpath(typed)
    return fs.normpath(fs.join(directory, typed))


def _same_file(fs: FileSystem, path: str, target_fs: FileSystem,
               target: str) -> bool:
    """Whether ``target`` is the file at ``path``, by any name.

    The names are compared first.  On this machine the file itself is asked
    too, so a link to the file, or another spelling of its name on a disk
    that ignores case (as a Mac's does by default), is caught as well.
    """
    if overlapping(fs, path, target_fs, target):
        return True
    if fs.scheme == target_fs.scheme == "local":
        try:
            return os.path.samefile(path, target)
        except OSError:          # either one not there: not the same file
            return False
    return False


def save_as(stdscr, fs: FileSystem, path: str, *, title: str, what: str,
            base: str, tag: str, render: Callable[[str], bytes],
            elsewhere: Place | None = None, notes: Sequence[str] = (),
            caveat: str = "") -> tuple[str, Written | None]:
    """Ask for a name and write ``render(name)`` there.

    ``fs`` and ``path`` are the file being browsed.  The name offered is
    ``base`` tagged with ``tag`` (``results.tsv`` and ``view`` offer
    ``results.view.tsv``), beside that file, or in ``elsewhere`` when the
    file's own directory cannot be written.  ``what`` says how much is being
    saved (:func:`shape`), ``notes`` are lines the prompt shows under the
    name, and ``caveat`` goes on the end of the footer after a save.
    ``render`` turns the name chosen into the bytes to write, and raises
    :class:`ValueError`, with the reason, when the view cannot be written in
    the format that name asks for.

    Returns what the footer should say, and the file written -- its backend
    and path -- or ``None`` when nothing was.
    """
    name = fs.basename(path)
    directory = fs.dirname(path)
    target_fs = fs
    shown_notes = list(notes)
    why = unwritable(fs, directory)
    if why is not None:
        refused = f" nowhere to save: {name}'s directory {why}"
        if elsewhere is None:
            return refused + " ", None
        other_why = unwritable(elsewhere.fs, elsewhere.path)
        if other_why is not None:
            return f"{refused}, and the other pane's {other_why} ", None
        target_fs, directory = elsewhere.fs, elsewhere.path
        shown_notes.append(f"{name}'s directory {why}, so this saves in "
                           f"the other pane's.")
    suggestion = derive_output_path(target_fs, target_fs.join(directory, base),
                                    tag)
    # Beside the file, the name is enough; anywhere else the whole path is
    # offered, so where it will go is in front of the user before Enter.
    typed = dialogs.prompt(stdscr, title, f"Save {what} as:",
                           default=(target_fs.basename(suggestion)
                                    if why is None else suggestion),
                           notes=shown_notes)
    if typed is None or not typed.strip():
        return " not saved ", None
    target = resolve(target_fs, directory, typed.strip())
    chosen = target_fs.basename(target)
    if _same_file(fs, path, target_fs, target):
        return (f" not saved: {chosen} is the file being browsed; give the "
                f"view a name of its own "), None
    if target_fs.is_dir(target):
        return f" not saved: {chosen} is a directory ", None
    try:
        data = render(chosen)
    except ValueError as exc:
        return f" not saved: {exc} ", None
    if target_fs.exists(target) and not dialogs.confirm(
            stdscr, title, f"{chosen} already exists.\nReplace it?"):
        return f" not saved: {chosen} is left as it was ", None
    try:
        write_bytes(target_fs, target, data)
    except Exception as exc:         # the backend refused, or a close failed
        reason = str(exc) or type(exc).__name__
        return f" not saved: cannot write {chosen}: {reason} ", None
    return f" wrote {what} to {chosen}{caveat} ", (target_fs, target)
