# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Permission bits and owners: working out a change, and making it.

What ``a`` (File ▸ Attributes…) does once its dialog has been answered, kept
apart from curses like the rest of the engine.  A change is a set of bits to
turn on and a set to turn off; a bit in neither is one the tagged entries did
not agree on and the dialog was not asked about, so each entry keeps its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .filesystems import DirEntry, FileSystem
from .operations import OperationCancelled

#: The nine ``rwx`` bits: all a change touches.
PERM_BITS = 0o777


def common_bits(modes: list[int]) -> tuple[int, int]:
    """The bits every mode has on, and the bits every mode has off.

    A bit in neither is mixed: some of the entries have it and some do not.
    """
    on, off = PERM_BITS, PERM_BITS
    for mode in modes:
        on &= mode
        off &= ~mode & PERM_BITS
    return on, off


def octal_text(on: int, off: int) -> str:
    """The mode in octal, ``7?5`` where a digit's three bits are not agreed."""
    digits = []
    for shift in (6, 3, 0):
        if ((on | off) >> shift) & 7 == 7:
            digits.append(str((on >> shift) & 7))
        else:
            digits.append("?")
    return "".join(digits)


@dataclass
class AttributeChange:
    """Bits to set and clear, and an owner or group to give, blank for none."""

    on: int
    off: int
    owner: str = ""
    group: str = ""
    recursive: bool = False

    def mode_for(self, mode: int) -> int:
        """The permission bits ``mode`` gets: its own wherever undecided."""
        return ((mode & PERM_BITS) & ~self.off) | self.on


ProgressCB = Callable[[int, str], None]


def apply_change(fs: FileSystem, targets: list[tuple[str, DirEntry]],
                 change: AttributeChange,
                 progress: ProgressCB = lambda done, label: None,
                 cancel: Callable[[], bool] = lambda: False,
                 ) -> tuple[int, list[str]]:
    """Make ``change`` on each target, and below directories if recursive.

    Returns how many entries were changed and, for those that would not be,
    ``"path: reason"``.  One refusal -- a file owned by someone else, which
    only root may give away -- does not stop the rest.  The walk does not
    follow symbolic links it finds under a directory, as ``chmod -R`` does
    not: a link points somewhere the tree may not own.  A link tagged by name
    is changed through, since that is what was asked for.
    """
    done = 0
    failures: list[str] = []
    stack = list(reversed(targets))
    while stack:
        if cancel():
            raise OperationCancelled()
        path, entry = stack.pop()
        try:
            if entry.is_symlink:
                entry = fs.stat(path)
            if change.on | change.off:
                fs.chmod(path, change.mode_for(entry.mode))
            if change.owner or change.group:
                fs.chown(path, change.owner, change.group)
            done += 1
        except Exception as exc:
            failures.append(f"{path}: {exc}")
        progress(done, fs.basename(path))
        if change.recursive and entry.is_dir and not entry.is_symlink:
            try:
                children = fs.listdir(path)
            except Exception as exc:
                failures.append(f"{path}: {exc}")
                continue
            for child in sorted(children, key=lambda e: e.name, reverse=True):
                if not child.is_symlink:
                    stack.append((fs.join(path, child.name), child))
    return done, failures
