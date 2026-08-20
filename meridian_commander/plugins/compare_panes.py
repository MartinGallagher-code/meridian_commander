"""Built-in plugin: compare this pane's directory tree against the other's.

The *seeing* half of synchronising: where the ``s`` sync copies differences,
this lists them.  It walks both panes' trees and reports each relative path that
is only on one side or that differs between them -- by name, size and
modification time, or, with ``hash``, by actually comparing file contents.

It reuses the same tree index the sync planner uses, so "left" and "right" mean
exactly what they do in a sync, and it works whatever each pane is: local or
remote, and the two need not be the same kind.
"""

from __future__ import annotations

from . import _io
from .. import sync
from ..plugin_api import Command, InputOutputPlugin

MAX_RESULTS = 1000    # differing paths listed before the rest are summarised


def _path(fs, root: str, rel: str) -> str:
    """Join a POSIX-relative index key back onto a backend's root."""
    return fs.join(root, *rel.split("/"))


class ComparePanes(InputOutputPlugin):
    name = "Compare panes"
    commands = (
        Command("", "compare by name, size and time", label="compare"),
        Command("hash", "compare file contents byte-for-byte"),
    )
    description = "Diff this pane's tree against the other pane's"
    prompt = "Enter=compare, F2 for options> "

    @property
    def greeting(self) -> str:
        c = self.ctx
        return (f"Left  (this pane):  {c.own_fs.label()}:{c.own_path}\n"
                f"Right (other pane): {c.other_fs.label()}:{c.other_path}\n"
                "Enter compares by name+size+time; type 'hash' to compare "
                "file contents.")

    def process(self, line: str):
        choice = line.strip().lower()
        if choice and choice != "hash":
            return "Press Enter to compare, or type 'hash' to compare contents."
        by_hash = choice == "hash"

        c = self.ctx
        left = sync._index_tree(c.own_fs, c.own_path)
        right = sync._index_tree(c.other_fs, c.other_path)

        only_left = only_right = differ = same = 0
        shown = 0
        truncated = False
        for rel in sorted(set(left) | set(right)):
            l, r = left.get(rel), right.get(rel)
            if l and not r:
                only_left += 1
                mark, detail = "<--", "only left"
            elif r and not l:
                only_right += 1
                mark, detail = "-->", "only right"
            elif self._differs(rel, l, r, by_hash):
                differ += 1
                mark, detail = "!= ", "differs"
            else:
                same += 1
                continue
            if shown < MAX_RESULTS:
                self.print(f"  {mark} {rel}  ({detail})")
                shown += 1
            else:
                truncated = True

        summary = (f"{only_left} only-left, {only_right} only-right, "
                   f"{differ} differ, {same} identical")
        if truncated:
            summary += f" (listing stopped at {MAX_RESULTS})"
        return summary

    def _differs(self, rel: str, l, r, by_hash: bool) -> bool:
        if (l.size or 0) != (r.size or 0):
            return True   # different sizes settle it without reading anything
        c = self.ctx
        if by_hash:
            try:
                lh = _io.hash_file(c.own_fs, _path(c.own_fs, c.own_path, rel),
                                   "sha256")
                rh = _io.hash_file(c.other_fs, _path(c.other_fs, c.other_path, rel),
                                   "sha256")
            except Exception:
                return True   # unreadable on a side: report it rather than hide it
            return lh != rh
        lt, rt = l.mtime, r.mtime
        if lt is None or rt is None:
            return False   # same size, no clock to compare: treat as identical
        return abs(lt - rt) > sync.MTIME_TOLERANCE
