"""Bidirectional directory synchronization.

The goal, in the user's words, is that *all of the latest versions of the files
end up in both locations*.  This is a two-way merge, not a mirror: nothing is
ever deleted, and for files that exist on both sides the newer one (by
modification time) wins and is copied over the older one.

The plan is computed first (:func:`build_sync_plan`) so the UI can show the user
exactly what will happen and how many bytes will move before anything is
touched; :func:`execute_sync_plan` then carries it out.

Both halves take ``progress`` and ``cancel`` callbacks, and the scan needs them
more than the copying does.  Copying a large tree at least looks busy, but the
scan produces nothing until it has walked both sides to the bottom -- and on a
remote pane every directory is a network round trip, so a tree with a lot of
files can take minutes during which the application has nothing to say.  Left
silent and uninterruptible that is indistinguishable from a hang, which is
exactly what it was mistaken for.

Better still is not to start.  :func:`survey_directory` looks at what a pane has
already listed and says whether it is big enough to be worth asking about first,
so a sync of somebody's whole home directory can be waved off in a keystroke
rather than scanned for a minute and then declined.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .filesystems import DirEntry, FileSystem
from .operations import (
    CancelCB,
    OperationCancelled,
    ProgressCB,
    _noop_cancel,
    _noop_progress,
    copy_file,
)

# Modification times from different systems (and filesystems with coarse
# granularity, e.g. FAT) rarely match to the second.  Treat files whose mtimes
# are within this tolerance as identical in age to avoid pointless back-copies.
MTIME_TOLERANCE = 2.0

# How often the scan reports in and looks for a cancel key.  Every file would
# be correct and far too slow -- each report polls the keyboard and redraws --
# while once per directory leaves a single huge directory silent for as long as
# it takes to read.  Doing both bounds the wait either way.
SCAN_REPORT_EVERY = 250

# What counts as "a lot" when deciding whether to warn before scanning at all.
# Both numbers describe a directory's *immediate* listing, which is the only
# thing that can be counted for free: the pane has already listed it, so the
# check costs nothing, where measuring the tree would mean doing the very walk
# the warning exists to talk the user out of.
#
# 200 files is roughly two screens of listing -- past that the user is looking
# at a summary rather than at files they can name, which is the point where a
# two-way sync stops being something they can predict the result of.  25
# subdirectories matters more than the file count, because each one is a whole
# tree the scan will descend into and, on a remote pane, its own round trip.
LARGE_FILE_COUNT = 200
LARGE_DIR_COUNT = 25


@dataclass
class DirectorySurvey:
    """How big a directory looks from its own listing, before any walking.

    A cheap proxy, and deliberately a shallow one: three subdirectories holding
    a hundred thousand files each will not trip either threshold.  It catches
    the case the warning is actually for -- a home directory or a filesystem
    root left in a pane -- without making the user wait to be told they should
    not have waited.
    """

    files: int = 0
    dirs: int = 0

    @property
    def is_large(self) -> bool:
        return self.files >= LARGE_FILE_COUNT or self.dirs >= LARGE_DIR_COUNT

    def describe(self) -> str:
        files = f"{self.files:,} file{'' if self.files == 1 else 's'}"
        dirs = f"{self.dirs:,} subdirector{'y' if self.dirs == 1 else 'ies'}"
        return f"{files}, {dirs}"


def survey_directory(entries: Iterable[DirEntry]) -> DirectorySurvey:
    """Count what a sync would find at the top of ``entries``.

    Symlinked directories count as files, because that is what
    :func:`_index_tree` makes of them: it records them and does not descend, so
    they cost one entry rather than a subtree.  A ``".."`` pseudo-entry -- which
    a panel puts at the head of its listing, and which is not part of the
    directory -- is ignored.
    """
    survey = DirectorySurvey()
    for entry in entries:
        if entry.name == "..":
            continue
        if entry.is_dir and not entry.is_symlink:
            survey.dirs += 1
        else:
            survey.files += 1
    return survey


@dataclass
class SyncAction:
    """A single planned copy, from one side to the other."""

    rel: str                 # path relative to the two roots
    direction: str           # "->" copy left-to-right, "<-" right-to-left
    reason: str              # human readable explanation
    size: int = 0

    def render(self) -> str:
        return f" {self.direction}  {self.rel}    ({self.reason})"


@dataclass
class SyncPlan:
    left_root: str
    right_root: str
    actions: list[SyncAction]

    @property
    def total_bytes(self) -> int:
        return sum(a.size for a in self.actions)

    def __bool__(self) -> bool:
        return bool(self.actions)


def _index_tree(
    fs: FileSystem,
    root: str,
    side: str = "",
    progress: ProgressCB = _noop_progress,
    cancel: CancelCB = _noop_cancel,
) -> dict[str, DirEntry]:
    """Map every file's relative path (POSIX separators) to its entry.

    Directories are walked but only files are recorded -- directories are
    created implicitly as their files are copied.

    The walk is a stack rather than recursion.  Partly so a deep tree cannot
    exhaust the interpreter's stack, but mostly because a loop has somewhere
    obvious to check for a cancel key: the scan is the slowest part of a sync
    and the part that used to be impossible to get out of.
    """
    index: dict[str, DirEntry] = {}
    stack = [(root, "")]
    since_report = 0

    def report() -> None:
        # total 0 -- a scan has no denominator until it has finished, which is
        # the whole problem; the dialog draws this as an indeterminate bar.
        progress(len(index), 0, f"{side}{len(index):,} files")
        if cancel():
            raise OperationCancelled()

    while stack:
        path, rel = stack.pop()
        report()
        try:
            entries = fs.listdir(path)
        except Exception:
            # An unreadable directory is skipped rather than fatal: one denied
            # subdirectory should not cost the user the whole sync.
            continue
        for entry in entries:
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            child_path = fs.join(path, entry.name)
            if entry.is_dir and not entry.is_symlink:
                stack.append((child_path, child_rel))
            else:
                index[child_rel] = entry
                since_report += 1
                if since_report >= SCAN_REPORT_EVERY:
                    since_report = 0
                    report()
    return index


def build_sync_plan(
    left_fs: FileSystem,
    left_root: str,
    right_fs: FileSystem,
    right_root: str,
    progress: ProgressCB = _noop_progress,
    cancel: CancelCB = _noop_cancel,
) -> SyncPlan:
    """Compare two trees and return the list of copies needed to reconcile them.

    Raises :class:`~meridian_commander.operations.OperationCancelled` if
    ``cancel`` returns true while either side is being scanned.
    """
    left = _index_tree(left_fs, left_root, "left: ", progress, cancel)
    right = _index_tree(right_fs, right_root, "right: ", progress, cancel)

    progress(0, 0, f"comparing {len(set(left) | set(right)):,} paths")
    actions: list[SyncAction] = []
    for rel in sorted(set(left) | set(right)):
        here = left.get(rel)
        there = right.get(rel)

        if here and not there:
            actions.append(SyncAction(rel, "->", "new on left", here.size or 0))
        elif there and not here:
            actions.append(SyncAction(rel, "<-", "new on right", there.size or 0))
        else:
            assert here and there
            lt = here.mtime
            rt = there.mtime
            if lt is None or rt is None:
                # Cannot compare ages; only copy when sizes differ, preferring
                # the left side as the source of truth for that case.
                if (here.size or 0) != (there.size or 0):
                    actions.append(
                        SyncAction(rel, "->", "differs (no mtime)", here.size or 0)
                    )
                continue
            if lt - rt > MTIME_TOLERANCE:
                actions.append(
                    SyncAction(rel, "->", "left is newer", here.size or 0))
            elif rt - lt > MTIME_TOLERANCE:
                actions.append(
                    SyncAction(rel, "<-", "right is newer", there.size or 0))
            # else: same age within tolerance -> already in sync, skip.

    return SyncPlan(left_root, right_root, actions)


def execute_sync_plan(
    plan: SyncPlan,
    left_fs: FileSystem,
    right_fs: FileSystem,
    progress: ProgressCB = _noop_progress,
    cancel: CancelCB = _noop_cancel,
    on_action: Callable[[SyncAction, int, int], None] | None = None,
) -> int:
    """Carry out a plan.  Returns the number of files copied.

    ``on_action`` (if given) is called before each file as
    ``on_action(action, index, total)`` for coarse-grained progress display.
    """
    total = len(plan.actions)
    copied = 0
    for i, action in enumerate(plan.actions):
        if cancel():
            break
        if on_action:
            on_action(action, i, total)

        parts = [p for p in action.rel.split("/") if p]
        if action.direction == "->":
            src_fs, src_root, dst_fs, dst_root = (
                left_fs, plan.left_root, right_fs, plan.right_root,
            )
        else:
            src_fs, src_root, dst_fs, dst_root = (
                right_fs, plan.right_root, left_fs, plan.left_root,
            )
        src = src_fs.join(src_root, *parts)
        dst = dst_fs.join(dst_root, *parts)
        # Preserve the source timestamp so both copies stay identical in age;
        # otherwise the just-written file would look "newer" and the next sync
        # would copy it straight back the other way.
        copy_file(src_fs, src, dst_fs, dst, progress, cancel,
                  preserve_mtime=True)
        copied += 1
    return copied
