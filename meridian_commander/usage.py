"""Measuring what is underneath a directory, a little at a time.

A pane that shows how much each of its subdirectories holds cannot afford to
find out the way ``du`` does.  Walking a home directory takes seconds at best
and minutes over SFTP, and an application that stops answering the keyboard
while it counts is worse than one that never counted: the reason to look is to
*navigate*, and navigation is exactly what the walk would take away.

So the walk is cut into steps.  :class:`TreeSizer` keeps its own stack of
directories still to be listed and does a bounded number of listings each time
it is asked, which the main loop does between keystrokes.  Totals grow while
you look at them, the pane stays responsive, and a directory whose size is
still being counted is drawn as the running figure rather than as a blank.

Nothing here knows about curses, and it goes through the pane's own
filesystem -- ``listdir`` and the sizes already in the entries -- so a remote
pane is measured exactly like a local one, with no shell on the far side.
"""

from __future__ import annotations

from .filesystems import FileSystem

#: Directory listings per step.  Small enough that a step over SFTP (one round
#: trip per listing) still returns between keystrokes, large enough that a
#: local tree of a few thousand directories finishes in a second or two.
DEFAULT_BUDGET = 8


class TreeSizer:
    """Totals the bytes under each of ``paths``, resumably.

    ``totals`` holds what has been counted so far for every path -- a running
    figure until that path lands in ``done``, and the final answer after.
    Symlinked directories are counted as their own (link) size rather than
    walked: following them would count a target twice, and a loop for ever.
    """

    def __init__(self, fs: FileSystem, paths: list[str]) -> None:
        self.fs = fs
        self.totals: dict[str, int] = dict.fromkeys(paths, 0)
        self.done: set[str] = set()
        self._queue: list[str] = list(paths)
        self._stack: list[str] = []
        self._root: str | None = None

    @property
    def finished(self) -> bool:
        return self._root is None and not self._queue

    def step(self, budget: int = DEFAULT_BUDGET) -> bool:
        """Do up to ``budget`` directory listings.  True while work remains."""
        for _ in range(max(1, budget)):
            if self._root is None:
                if not self._queue:
                    return False
                self._root = self._queue.pop(0)
                self._stack = [self._root]
                continue
            if not self._stack:
                self.done.add(self._root)
                self._root = None
                continue
            self._list(self._stack.pop(), self._root)
        return not self.finished

    def _list(self, path: str, root: str) -> None:
        """Add one directory's files to ``root``'s total; queue its children.

        A directory that cannot be read counts as nothing rather than stopping
        the walk: an unreadable corner of a tree is a normal thing to meet, and
        the other 99% of the answer is still worth having.
        """
        try:
            entries = self.fs.listdir(path)
        except Exception:
            return
        total = 0
        for entry in entries:
            if entry.is_dir and not entry.is_symlink:
                self._stack.append(self.fs.join(path, entry.name))
            else:
                total += entry.size or 0
        self.totals[root] += total

    def run(self, limit: int = 1_000_000) -> dict[str, int]:
        """Finish the whole walk (for a caller with nothing else to do)."""
        while self.step() and limit > 0:
            limit -= 1
        return self.totals


def bar(size: int, biggest: int, width: int, full: str = "#",
        empty: str = "-") -> str:
    """A ``width``-column bar showing ``size`` against the largest entry.

    Scaled to the biggest thing in the directory rather than to the total,
    because the question being asked is "which of these is the big one?" --
    and against a total, twenty similar directories all draw nothing.
    """
    if width <= 0:
        return ""
    if biggest <= 0 or size <= 0:
        return empty * width
    filled = int(round(width * size / biggest))
    filled = max(1, min(width, filled))     # anything non-zero shows something
    return full * filled + empty * (width - filled)
