"""A scrollable read-only file viewer with optional line numbers.

Works on any filesystem: the file is streamed through the backend's
``open_read`` so remote files can be viewed exactly like local ones.  A size cap
keeps a stray attempt to view a multi-gigabyte file from exhausting memory.
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

        self._draw_footer(win, height, width)
        win.noutrefresh()

    def _draw_footer(self, win, height: int, width: int) -> None:
        ln = "on" if self.show_line_numbers else "off"
        pos = f"line {self.top + 1}/{len(self.lines)}"
        hint = f" {pos}   [N]umbers:{ln}  [W]rap  arrows/PgUp/PgDn  [Q]uit "
        win.attrset(curses.A_REVERSE)
        try:
            win.addstr(height - 1, 0, hint.ljust(width)[:width])
        except curses.error:
            pass
        win.attrset(curses.A_NORMAL)

    # -- interaction ------------------------------------------------------
    def run(self, stdscr) -> None:
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
            if key in (ord("q"), ord("Q"), 27, curses.KEY_F3):
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
            elif key in (ord("n"), ord("N")):
                self.show_line_numbers = not self.show_line_numbers
            elif key in (ord("w"), ord("W")):
                self.wrap = not self.wrap

    def _scroll(self, delta: int, body_h: int) -> None:
        self.top = max(0, min(self.top + delta, max(0, len(self.lines) - 1)))
