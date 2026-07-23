"""A scrollable read-only file viewer with optional line numbers and search.

Works on any filesystem: the file is streamed through the backend's
``open_read`` so remote files can be viewed exactly like local ones.  A size cap
keeps a stray attempt to view a multi-gigabyte file from exhausting memory.

Search follows ``less`` conventions: ``/`` (or F7) prompts for a pattern,
``n``/``N`` jump to the next/previous matching line with wrap-around, and all
matches on screen are highlighted.  A pattern typed in lowercase searches
case-insensitively; any uppercase letter makes it case-sensitive (smart case).
"""

from __future__ import annotations

import curses

from .filesystems import FileSystem

MAX_VIEW_BYTES = 16 * 1024 * 1024  # 16 MiB safety cap


class Viewer:
    def __init__(self, fs: FileSystem, path: str) -> None:
        self.fs = fs
        self.path = path
        self.name = fs.basename(path)
        self.top = 0
        self.left = 0
        self.show_line_numbers = True
        self.wrap = False
        self.error: str | None = None
        self.truncated = False
        self.search = ""
        self.cur_match: int | None = None
        self.notice = ""
        self.lines = self._load()

    def _load(self) -> list[str]:
        try:
            reader = self.fs.open_read(self.path)
            try:
                data = reader.read(MAX_VIEW_BYTES + 1)
            finally:
                reader.close()
        except Exception as exc:
            self.error = str(exc)
            return []
        if len(data) > MAX_VIEW_BYTES:
            data = data[:MAX_VIEW_BYTES]
            self.truncated = True
        text = data.decode("utf-8", errors="replace")
        # Normalise newlines; keep tabs expanded for stable columns.
        text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
        return text.split("\n")

    # -- search -----------------------------------------------------------
    def set_search(self, pattern: str) -> None:
        """Set (or clear) the search pattern and reset match state."""
        self.search = pattern or ""
        self.cur_match = None
        self.notice = ""

    def _case_insensitive(self) -> bool:
        # Smart case: an all-lowercase pattern matches case-insensitively.
        return self.search == self.search.lower()

    def _line_matches(self, index: int) -> bool:
        if not self.search:
            return False
        line = self.lines[index]
        if self._case_insensitive():
            return self.search.lower() in line.lower()
        return self.search in line

    def match_positions(self, line: str) -> list[int]:
        """Column positions of every match of the pattern in ``line``."""
        if not self.search:
            return []
        needle, hay = self.search, line
        if self._case_insensitive():
            needle, hay = needle.lower(), hay.lower()
        positions = []
        start = 0
        while True:
            j = hay.find(needle, start)
            if j < 0:
                break
            positions.append(j)
            start = j + max(1, len(needle))
        return positions

    def find_next(self, direction: int = 1) -> int | None:
        """Jump to the next (or previous) matching line, wrapping around.

        Returns the line index, or None when the pattern matches nowhere.
        """
        if not self.search:
            self.notice = "no search pattern -- press /"
            return None
        n = len(self.lines)
        if n == 0:
            return None
        if self.cur_match is not None:
            base = self.cur_match
        else:
            # First jump after a new pattern: include the top visible line.
            base = self.top - direction
        for step in range(1, n + 1):
            i = (base + direction * step) % n
            if self._line_matches(i):
                wrapped = (i - base) * direction <= 0
                self.cur_match = i
                self._jump_to(i)
                self.notice = f"match on line {i + 1}" + \
                    (" (wrapped)" if wrapped else "")
                return i
        self.notice = f"'{self.search}' not found"
        return None

    def _jump_to(self, index: int) -> None:
        """Scroll so line ``index`` is visible (a couple of rows from the top)."""
        self.top = max(0, min(index - 2, max(0, len(self.lines) - 1)))

    # -- rendering --------------------------------------------------------
    def draw(self, win) -> None:
        win.erase()
        height, width = win.getmaxyx()
        body_h = height - 2

        title = f" View: {self.name} "
        if self.truncated:
            title += "[truncated] "
        win.attrset(curses.A_REVERSE)
        win.addstr(0, 0, title.ljust(width)[:width])
        win.attrset(curses.A_NORMAL)

        if self.error:
            win.addstr(2, 2, f"Cannot open file: {self.error}"[: width - 4])
            self._draw_footer(win, height, width)
            win.noutrefresh()
            return

        gutter = len(str(len(self.lines))) + 1 if self.show_line_numbers else 0
        for row in range(body_h):
            idx = self.top + row
            if idx >= len(self.lines):
                break
            y = row + 1
            if self.show_line_numbers:
                num = str(idx + 1).rjust(gutter - 1)
                win.attrset(curses.color_pair(0) | curses.A_DIM)
                win.addstr(y, 0, num + " ")
                win.attrset(curses.A_NORMAL)
            line = self.lines[idx]
            visible = line[self.left : self.left + (width - gutter)]
            try:
                win.addstr(y, gutter, visible)
            except curses.error:
                pass
            if self.search:
                self._highlight_matches(win, y, line, gutter, width)

        self._draw_footer(win, height, width)
        win.noutrefresh()

    def _highlight_matches(self, win, y: int, line: str, gutter: int,
                           width: int) -> None:
        """Overdraw every on-screen occurrence of the pattern in reverse."""
        length = len(self.search)
        for pos in self.match_positions(line):
            sx = gutter + pos - self.left
            seg = line[pos : pos + length]
            if sx < gutter:  # partially scrolled off to the left
                seg = seg[gutter - sx :]
                sx = gutter
            if not seg or sx >= width:
                continue
            seg = seg[: width - sx]
            try:
                win.addstr(y, sx, seg, curses.A_REVERSE)
            except curses.error:
                pass

    def _draw_footer(self, win, height: int, width: int) -> None:
        pos = f"line {self.top + 1}/{len(self.lines)}"
        parts = [pos]
        if self.search:
            parts.append(f"/{self.search}")
        if self.notice:
            parts.append(self.notice)
        parts.append("[/]find  n/N next/prev  [l]#  [w]rap  [q]uit")
        hint = "  ".join(parts)
        win.attrset(curses.A_REVERSE)
        try:
            win.addstr(height - 1, 0, f" {hint} ".ljust(width)[:width])
        except curses.error:
            pass
        win.attrset(curses.A_NORMAL)

    # -- interaction ------------------------------------------------------
    def run(self, stdscr) -> None:
        from . import dialogs

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
            if key in (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10):
                break
            elif key in (curses.KEY_DOWN, ord("j")):
                self._scroll(1, body_h)
            elif key in (curses.KEY_UP, ord("k")):
                self._scroll(-1, body_h)
            elif key in (curses.KEY_NPAGE, ord(" ")):
                self._scroll(body_h, body_h)
            elif key == curses.KEY_PPAGE:
                self._scroll(-body_h, body_h)
            elif key == curses.KEY_HOME:
                self.top = 0
            elif key == curses.KEY_END:
                self.top = max(0, len(self.lines) - body_h)
            elif key == curses.KEY_LEFT:
                self.left = max(0, self.left - 8)
            elif key == curses.KEY_RIGHT:
                self.left += 8
            elif key in (ord("/"), curses.KEY_F7):
                pattern = dialogs.prompt(stdscr, "Search",
                                         "Find (smart case):",
                                         default=self.search)
                curses.curs_set(0)
                if pattern is not None:
                    self.set_search(pattern)
                    if pattern:
                        self.find_next(1)
            elif key == ord("n"):
                self.find_next(1)
            elif key in (ord("N"), ord("p")):
                self.find_next(-1)
            elif key in (ord("l"), ord("L")):
                self.show_line_numbers = not self.show_line_numbers
            elif key in (ord("w"), ord("W")):
                self.wrap = not self.wrap

    def _scroll(self, delta: int, body_h: int) -> None:
        self.notice = ""
        self.top = max(0, min(self.top + delta, max(0, len(self.lines) - 1)))
