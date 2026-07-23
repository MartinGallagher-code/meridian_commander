"""Find files under a directory and browse the results.

Works on any pane backend (local, SFTP, SSH, FTP): the scan walks the
filesystem through the same abstraction the panes use.  The results open in a
full-screen, scrollable browser from which a file can be viewed or edited in
place, or the pane can jump straight to the directory that contains it.
"""

from __future__ import annotations

import curses
import fnmatch

from .filesystems import DirEntry, FileSystem
from .util import human_size, human_time, ljust

MAX_RESULTS = 2000
MAX_DEPTH = 25


def collect_matches(
    fs: FileSystem,
    root: str,
    pattern: str,
    cancel=None,
    progress=None,
    max_results: int = MAX_RESULTS,
    max_depth: int = MAX_DEPTH,
):
    """Recursively find entries whose *name* matches ``pattern``.

    A pattern without glob characters is treated as a substring match
    (``conf`` behaves like ``*conf*``).  Returns ``(matches, truncated)``
    where matches are ``(relative_path, DirEntry)`` tuples in walk order.
    ``cancel()`` is polled between directories; ``progress(count, rel)`` is
    called as directories are entered.
    """
    if any(c in pattern for c in "*?["):
        pat = pattern
    else:
        pat = f"*{pattern}*"

    matches: list[tuple[str, DirEntry]] = []
    truncated = False

    def walk(path: str, rel: str, depth: int) -> None:
        nonlocal truncated
        if truncated or depth > max_depth:
            return
        if cancel is not None and cancel():
            return
        try:
            entries = fs.listdir(path)
        except Exception:
            return  # unreadable directory: skip, keep searching elsewhere
        for entry in sorted(entries, key=lambda e: e.name.lower()):
            if truncated or (cancel is not None and cancel()):
                return
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            if fnmatch.fnmatch(entry.name, pat):
                matches.append((child_rel, entry))
                if len(matches) >= max_results:
                    truncated = True
                    return
            if entry.is_dir and not entry.is_symlink:
                if progress is not None:
                    progress(len(matches), child_rel)
                walk(fs.join(path, entry.name), child_rel, depth + 1)

    walk(root, "", 0)
    return matches, truncated


class FindBrowser:
    """Full-screen, scrollable browser over find results.

    Returns from :meth:`run` with the selected result's relative path when the
    user asks to jump to its containing directory, or ``None`` when closed.
    Viewing and editing happen in place without leaving the list.
    """

    def __init__(self, fs: FileSystem, root: str, pattern: str,
                 matches, truncated: bool = False) -> None:
        self.fs = fs
        self.root = root
        self.pattern = pattern
        self.matches = matches
        self.truncated = truncated
        self.cursor = 0
        self.top = 0
        self.notice = ""

    # -- selection helpers -------------------------------------------------
    def current(self):
        if 0 <= self.cursor < len(self.matches):
            return self.matches[self.cursor]
        return None

    def current_path(self) -> str | None:
        cur = self.current()
        if cur is None:
            return None
        return self.fs.join(self.root, *[p for p in cur[0].split("/") if p])

    # -- drawing -------------------------------------------------------------
    def draw(self, win) -> None:
        win.erase()
        height, width = win.getmaxyx()
        body_h = height - 2

        # Count first so it survives truncation of a long root path.
        title = (f" Find '{self.pattern}': {len(self.matches)} match(es)"
                 f"{' (truncated)' if self.truncated else ''}"
                 f" under {self.fs.label()}:{self.root} ")
        win.attrset(curses.A_REVERSE)
        try:
            win.addstr(0, 0, title.ljust(width)[:width])
        except curses.error:
            pass
        win.attrset(curses.A_NORMAL)

        # Keep the cursor visible.
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + body_h:
            self.top = self.cursor - body_h + 1

        for row in range(body_h):
            idx = self.top + row
            if idx >= len(self.matches):
                break
            rel, entry = self.matches[idx]
            marker = "/" if entry.is_dir else " "
            size_s = "  <DIR>" if entry.is_dir else human_size(entry.size)
            time_s = human_time(entry.mtime)
            name_w = max(10, width - 24)
            line = f" {ljust(rel + marker, name_w)}{size_s:>7} {time_s[:12]:>12}"
            attr = curses.A_REVERSE if idx == self.cursor else curses.A_NORMAL
            if entry.is_dir:
                attr |= curses.A_BOLD
            try:
                win.addstr(row + 1, 0, ljust(line, width), attr)
            except curses.error:
                pass

        hint = self.notice or \
            " Enter/g goto dir   v/F3 view   e/F4 edit   q/Esc close "
        win.attrset(curses.A_REVERSE)
        try:
            win.addstr(height - 1, 0, f"{hint}".ljust(width)[:width])
        except curses.error:
            pass
        win.attrset(curses.A_NORMAL)
        win.noutrefresh()

    # -- main loop -------------------------------------------------------------
    def run(self, stdscr) -> str | None:
        from .editor import Editor
        from .viewer import Viewer

        curses.curs_set(0)
        height, width = stdscr.getmaxyx()
        win = curses.newwin(height, width, 0, 0)
        win.keypad(True)
        while True:
            self.draw(win)
            curses.doupdate()
            height, width = stdscr.getmaxyx()
            body_h = height - 2
            key = win.getch()
            self.notice = ""
            if key in (ord("q"), ord("Q"), 27, curses.KEY_F10):
                return None
            elif key in (curses.KEY_DOWN, ord("j")):
                self.cursor = min(self.cursor + 1, len(self.matches) - 1)
            elif key in (curses.KEY_UP, ord("k")):
                self.cursor = max(0, self.cursor - 1)
            elif key == curses.KEY_NPAGE:
                self.cursor = min(self.cursor + body_h, len(self.matches) - 1)
            elif key == curses.KEY_PPAGE:
                self.cursor = max(0, self.cursor - body_h)
            elif key == curses.KEY_HOME:
                self.cursor = 0
            elif key == curses.KEY_END:
                self.cursor = len(self.matches) - 1
            elif key in (10, 13, curses.KEY_ENTER, ord("g"), ord("G")):
                cur = self.current()
                if cur is not None:
                    return cur[0]
            elif key in (ord("v"), curses.KEY_F3):
                self._open(Viewer, stdscr)
            elif key in (ord("e"), curses.KEY_F4):
                self._open(Editor, stdscr)

    def _open(self, cls, stdscr) -> None:
        cur = self.current()
        if cur is None:
            return
        rel, entry = cur
        if entry.is_dir:
            self.notice = " directories can be entered with Enter/g "
            return
        target = self.current_path()
        try:
            cls(self.fs, target).run(stdscr)
        except Exception as exc:
            self.notice = f" {exc} "
        curses.curs_set(0)
