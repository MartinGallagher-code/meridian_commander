# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Moving files to the desktop's trash instead of deleting them.

On Linux and the BSDs this is the FreeDesktop.org Trash specification, so the
desktop's own trash can list and restore what went in: each item is moved to
``files/`` in a trash directory, beside a ``.trashinfo`` file in ``info/``
holding where it came from and when.  The trash is the one in the user's home
for anything on the same disk; on another disk it is that disk's
``$topdir/.Trash/$uid`` when the disk has a proper ``.Trash`` (a real, sticky
directory), and ``$topdir/.Trash-$uid`` otherwise -- a rename across disks
would be a copy and a delete, which is exactly what a trash is not.

On macOS it is ``~/.Trash``.  Finder's "Put Back" reads metadata only Finder
writes, so an item trashed here can be dragged back but not put back.

:func:`trash_location` answers the other question -- where to look -- without
making anything but the home trash, and :func:`trash_root` and
:func:`info_file` let a pane already in the trash tell where it is (MC-024).
"""

from __future__ import annotations

import os
import stat
import sys
import time
from urllib.parse import quote


class TrashUnavailable(Exception):
    """There is no trash this item can be moved to without copying it."""


def _home_trash() -> str:
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    return os.path.join(data, "Trash")


def _mount_point(path: str) -> str:
    """The top of the filesystem ``path`` is on."""
    path = os.path.realpath(path)
    device = os.lstat(path).st_dev
    while True:
        parent = os.path.dirname(path)
        if parent == path or os.lstat(parent).st_dev != device:
            return path
        path = parent


def _usable(directory: str) -> bool:
    """Whether ``directory`` is a trash this user may write into."""
    try:
        os.makedirs(os.path.join(directory, "files"), mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(directory, "info"), mode=0o700, exist_ok=True)
    except OSError:
        return False
    return os.access(directory, os.W_OK)


def trash_for(path: str) -> tuple[str, str | None]:
    """The trash directory for ``path``, and the top its info is relative to.

    The top is ``None`` for the home trash, whose info holds absolute paths.
    """
    home = _home_trash()
    os.makedirs(home, mode=0o700, exist_ok=True)
    item_dev = os.lstat(os.path.dirname(os.path.abspath(path)) or "/").st_dev
    if os.stat(home).st_dev == item_dev:
        if not _usable(home):
            raise TrashUnavailable(f"cannot write to the trash in {home}")
        return home, None
    top = _mount_point(os.path.dirname(os.path.abspath(path)))
    mine = _shared_trash(top)
    if mine is not None and _usable(mine):
        return mine, top
    own = os.path.join(top, f".Trash-{os.getuid()}")
    if _usable(own):
        return own, top
    raise TrashUnavailable(f"no trash on the disk {top} can be used")


def _shared_trash(top: str) -> str | None:
    """This user's place in the disk's shared ``.Trash``, if it has one.

    Only a real, sticky ``$topdir/.Trash`` counts: the specification says to
    pass over one that is a link or lacks the sticky bit, since anyone could
    have made it to collect other people's files.  Nothing is created here.
    """
    try:
        st = os.lstat(os.path.join(top, ".Trash"))
    except OSError:
        return None
    if (stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
            and st.st_mode & stat.S_ISVTX):
        return os.path.join(top, ".Trash", str(os.getuid()))
    return None


def _make_home_trash() -> str:
    """The home trash, made as :func:`trash_for` makes it if it is not there.

    A failure is left for the listing that follows to report, which can say
    what was wrong with the directory rather than only that it is missing.
    """
    home = _home_trash()
    try:
        os.makedirs(home, mode=0o700, exist_ok=True)
    except OSError:
        return home
    _usable(home)
    return home


def trash_location(path: str | None = None) -> str:
    """The directory a pane lists to show the trash.

    On Linux and the BSDs it is a trash's ``files/``: the one ``F8`` would use
    for things in the local directory ``path`` -- that disk's own trash, when
    it is on another disk and has one -- and otherwise, or with no ``path``,
    the home trash.  On macOS it is ``~/.Trash``.

    Looking must not litter: the home trash is made if it is missing (``F8``
    would make it anyway, and an empty trash is a better answer than an
    error), but nothing is ever created on another disk -- a USB stick that
    has never had anything trashed on it is left without a ``.Trash-$uid``.
    """
    if sys.platform == "darwin":
        trash = os.path.expanduser("~/.Trash")
        try:
            os.makedirs(trash, exist_ok=True)
        except OSError:
            pass     # the listing says why
        return trash
    home = _make_home_trash()
    files = os.path.join(home, "files")
    if path is None:
        return files
    try:
        if os.stat(path).st_dev == os.stat(home).st_dev:
            return files
        top = _mount_point(path)
    except OSError:
        return files
    own = os.path.join(top, f".Trash-{os.getuid()}")
    for trash in (_shared_trash(top), own):
        if trash is not None and os.path.isdir(os.path.join(trash, "files")):
            return os.path.join(trash, "files")
    return files


def _is_trash(directory: str) -> bool:
    """Whether ``directory`` is a FreeDesktop trash this user's ``F8`` uses."""
    name = os.path.basename(directory)
    if name == f".Trash-{os.getuid()}":
        return True
    if (name == str(os.getuid())
            and os.path.basename(os.path.dirname(directory)) == ".Trash"):
        return True
    return os.path.realpath(directory) == os.path.realpath(_home_trash())


def trash_root(directory: str) -> str | None:
    """The trash listing ``directory`` is in, or ``None`` if it is in none.

    The listing is a trash's ``files/`` (or ``~/.Trash`` on macOS), and
    ``directory`` may be it or anywhere below it: inside a directory that was
    trashed whole is still inside the trash.
    """
    here = os.path.abspath(directory)
    if sys.platform == "darwin":
        trash = os.path.realpath(os.path.expanduser("~/.Trash"))
        real = os.path.realpath(here)
        if real == trash or real.startswith(trash + os.sep):
            return trash
        return None
    while True:
        if os.path.basename(here) == "files" and _is_trash(
                os.path.dirname(here)):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def info_file(path: str) -> str | None:
    """The ``.trashinfo`` that goes with ``path``, an item in a trash.

    ``None`` unless ``path`` is at the top of a FreeDesktop trash's ``files/``:
    something deeper is part of an item rather than one, and macOS keeps no
    such file (Finder's own record is not one this module writes).
    """
    if sys.platform == "darwin":
        return None
    files = os.path.dirname(os.path.abspath(path))
    if os.path.basename(files) != "files" or not _is_trash(
            os.path.dirname(files)):
        return None
    return os.path.join(os.path.dirname(files), "info",
                        os.path.basename(path) + ".trashinfo")


def _reserve(info_dir: str, name: str, text: str) -> str:
    """Claim a name in the trash by creating its info file; returns the name.

    ``O_EXCL`` makes the claim atomic, as the specification asks, so two
    things trashed at once can never take one name.
    """
    base, ext = os.path.splitext(name)
    candidate, n = name, 1
    while True:
        try:
            fd = os.open(os.path.join(info_dir, candidate + ".trashinfo"),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            n += 1
            candidate = f"{base}.{n}{ext}"
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as info:
            info.write(text)
        return candidate


def _move_freedesktop(path: str) -> str:
    trash, top = trash_for(path)
    absolute = os.path.abspath(path)
    shown = absolute if top is None else os.path.relpath(absolute, top)
    text = ("[Trash Info]\n"
            f"Path={quote(shown)}\n"
            f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    name = _reserve(os.path.join(trash, "info"), os.path.basename(absolute),
                    text)
    target = os.path.join(trash, "files", name)
    try:
        os.rename(absolute, target)
    except OSError:
        os.remove(os.path.join(trash, "info", name + ".trashinfo"))
        raise
    return target


def _move_macos(path: str) -> str:
    trash = os.path.expanduser("~/.Trash")
    os.makedirs(trash, exist_ok=True)
    name = os.path.basename(os.path.abspath(path))
    base, ext = os.path.splitext(name)
    candidate, n = name, 1
    while os.path.lexists(os.path.join(trash, candidate)):
        n += 1
        candidate = f"{base} {n}{ext}"
    target = os.path.join(trash, candidate)
    try:
        os.rename(path, target)
    except OSError as exc:
        raise TrashUnavailable(f"cannot move it to {trash}: {exc}") from exc
    return target


def move_to_trash(path: str) -> str:
    """Move ``path`` -- file, directory or link -- to the trash; returns where.

    A link goes in itself, never the thing it points at.
    """
    if sys.platform == "darwin":
        return _move_macos(path)
    return _move_freedesktop(path)


def forget(path: str) -> None:
    """Remove the ``.trashinfo`` of ``path``, an item deleted from the trash.

    Left behind, it would have the desktop's trash list an entry with nothing
    behind it.  An item with no record -- below the top of ``files/``, on
    macOS, or put there by hand -- is nothing to do; any other failure to
    remove one is raised.
    """
    info = info_file(path)
    if info is None:
        return
    try:
        os.remove(info)
    except FileNotFoundError:
        pass
