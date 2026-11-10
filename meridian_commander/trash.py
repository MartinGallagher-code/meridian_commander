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
    uid = os.getuid()
    shared = os.path.join(top, ".Trash")
    try:
        st = os.lstat(shared)
        if (stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
                and st.st_mode & stat.S_ISVTX):
            mine = os.path.join(shared, str(uid))
            if _usable(mine):
                return mine, top
    except OSError:
        pass
    own = os.path.join(top, f".Trash-{uid}")
    if _usable(own):
        return own, top
    raise TrashUnavailable(f"no trash on the disk {top} can be used")


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
