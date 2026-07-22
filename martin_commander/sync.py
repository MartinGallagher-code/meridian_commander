"""Bidirectional directory synchronization.

The goal, in the user's words, is that *all of the latest versions of the files
end up in both locations*.  This is a two-way merge, not a mirror: nothing is
ever deleted, and for files that exist on both sides the newer one (by
modification time) wins and is copied over the older one.

The plan is computed first (:func:`build_sync_plan`) so the UI can show the user
exactly what will happen and how many bytes will move before anything is
touched; :func:`execute_sync_plan` then carries it out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .filesystems import DirEntry, FileSystem
from .operations import CancelCB, ProgressCB, _noop_cancel, _noop_progress, copy_file

# Modification times from different systems (and filesystems with coarse
# granularity, e.g. FAT) rarely match to the second.  Treat files whose mtimes
# are within this tolerance as identical in age to avoid pointless back-copies.
MTIME_TOLERANCE = 2.0


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


def _index_tree(fs: FileSystem, root: str) -> dict[str, DirEntry]:
    """Map every file's relative path (POSIX separators) to its entry.

    Directories are walked but only files are recorded -- directories are
    created implicitly as their files are copied.
    """
    index: dict[str, DirEntry] = {}

    def walk(path: str, rel: str) -> None:
        try:
            entries = fs.listdir(path)
        except Exception:
            return
        for entry in entries:
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            child_path = fs.join(path, entry.name)
            if entry.is_dir and not entry.is_symlink:
                walk(child_path, child_rel)
            else:
                index[child_rel] = entry

    walk(root, "")
    return index


def build_sync_plan(
    left_fs: FileSystem,
    left_root: str,
    right_fs: FileSystem,
    right_root: str,
) -> SyncPlan:
    """Compare two trees and return the list of copies needed to reconcile them."""
    left = _index_tree(left_fs, left_root)
    right = _index_tree(right_fs, right_root)

    actions: list[SyncAction] = []
    for rel in sorted(set(left) | set(right)):
        l = left.get(rel)
        r = right.get(rel)

        if l and not r:
            actions.append(SyncAction(rel, "->", "new on left", l.size or 0))
        elif r and not l:
            actions.append(SyncAction(rel, "<-", "new on right", r.size or 0))
        else:
            assert l and r
            lt = l.mtime
            rt = r.mtime
            if lt is None or rt is None:
                # Cannot compare ages; only copy when sizes differ, preferring
                # the left side as the source of truth for that case.
                if (l.size or 0) != (r.size or 0):
                    actions.append(
                        SyncAction(rel, "->", "differs (no mtime)", l.size or 0)
                    )
                continue
            if lt - rt > MTIME_TOLERANCE:
                actions.append(SyncAction(rel, "->", "left is newer", l.size or 0))
            elif rt - lt > MTIME_TOLERANCE:
                actions.append(SyncAction(rel, "<-", "right is newer", r.size or 0))
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
