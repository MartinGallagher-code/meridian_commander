"""A full-screen, scrollable grid browser for spreadsheets.

Why full screen rather than a pane plug-in: on an 80-column terminal a pane is
about 39 columns wide, which is two or three spreadsheet columns.  The viewer,
the editor and the find browser all take the whole screen for the same reason,
and this follows them -- ``F3`` on a workbook opens the grid exactly the way
``F3`` on a text file opens the viewer.

Reading is delegated to :mod:`meridian_commander.xlsx`, so the grid works on
remote panes too: the file arrives through the backend's ``open_read`` like
any other.
"""

from __future__ import annotations

import curses

from . import theme
from .filesystems import FileSystem
from .util import ljust, rjust, truncate
from .xlsx import Sheet, Workbook, column_name, read_workbook

# Column widths are clamped into this range, then cycled by the "w" key: a
# spreadsheet full of prose is unreadable at 12 columns and a spreadsheet full
# of numbers wastes half the screen at 48.
WIDTH_CHOICES = (12, 24, 48)
MIN_COL_WIDTH = 3
# Widths are measured once, over the top of the sheet, so they do not shift
# under the reader while scrolling through a long file.
WIDTH_SAMPLE_ROWS = 500


def looks_numeric(text: str) -> bool:
    """True for text a spreadsheet would right-align.

    The leading-character test keeps ``float()`` from accepting the words
    ``nan`` and ``inf``, which are ordinary text in a cell.
    """
    text = text.strip()
    if not text or text[0] not in "+-.0123456789":
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


class SheetView:
    def __init__(self, fs: FileSystem, path: str) -> None:
        self.fs = fs
        self.path = path
        self.name = fs.basename(path)
        self.error: str | None = None
        self.sheet_index = 0
        self.row = 0
        self.col = 0
        self.top = 0
        self.left = 0
        self.search = ""
        self.notice = ""
        self.max_width = WIDTH_CHOICES[1]
        self._widths_cache: dict[tuple[int, int], list[int]] = {}
        self.workbook = self._load()

    def _load(self) -> Workbook:
        try:
            return read_workbook(self.fs, self.path)
        except Exception as exc:    # unreadable file, or the backend failed
            self.error = str(exc)
            # One empty sheet rather than no sheets: every movement and drawing
            # path then works on the error screen without a second set of
            # guards, because an empty sheet is a case they must handle anyway.
            return Workbook(self.name, [Sheet("")])

    # -- current position ---------------------------------------------------
    @property
    def sheet(self) -> Sheet:
        return self.workbook.sheets[self.sheet_index]

    def cell(self, row: int, col: int) -> str:
        rows = self.sheet.rows
        if 0 <= row < len(rows) and 0 <= col < len(rows[row]):
            return rows[row][col]
        return ""

    def current_cell(self) -> str:
        return self.cell(self.row, self.col)

    def reference(self) -> str:
        """The cursor's cell reference, the way a spreadsheet writes it."""
        return f"{column_name(self.col)}{self.row + 1}"

    def _clamp(self) -> None:
        sheet = self.sheet
        self.row = max(0, min(self.row, len(sheet.rows) - 1))
        self.col = max(0, min(self.col, sheet.ncols - 1))

    # -- geometry -----------------------------------------------------------
    def _widths(self) -> list[int]:
        key = (self.sheet_index, self.max_width)
        cached = self._widths_cache.get(key)
        if cached is not None:
            return cached
        sheet = self.sheet
        widths = [max(MIN_COL_WIDTH, len(column_name(i)))
                  for i in range(sheet.ncols)]
        for row in sheet.rows[:WIDTH_SAMPLE_ROWS]:
            for index, text in enumerate(row):
                if len(text) > widths[index]:
                    widths[index] = min(len(text), self.max_width)
        self._widths_cache[key] = widths
        return widths

    def gutter(self) -> int:
        """Width of the row-number column, including its trailing space."""
        return max(4, len(str(max(1, len(self.sheet.rows)))) + 1)

    def layout(self, avail: int) -> list[tuple[int, int, int]]:
        """``(column, x, width)`` for the columns visible from ``self.left``."""
        widths = self._widths()
        out: list[tuple[int, int, int]] = []
        x = 0
        for index in range(self.left, len(widths)):
            width = widths[index]
            if x + width > avail:
                break
            out.append((index, x, width))
            x += width + 1          # one blank column as a separator
        if not out and self.left < len(widths) and avail > 0:
            # Not even one column fits; show as much of it as there is room for
            # rather than drawing an empty screen.
            out.append((self.left, 0, avail))
        return out

    def _ensure_visible(self, avail: int, body_h: int) -> None:
        if self.row < self.top:
            self.top = self.row
        elif self.row >= self.top + body_h:
            self.top = self.row - body_h + 1
        if self.col < self.left:
            self.left = self.col
        while True:
            columns = [c for c, _x, _w in self.layout(avail)]
            if not columns or self.col <= columns[-1]:
                break
            self.left += 1

    # -- search -------------------------------------------------------------
    def set_search(self, pattern: str) -> None:
        self.search = pattern or ""

    def _matches(self, text: str) -> bool:
        # Smart case, spelled the same way as in the viewer: an all-lowercase
        # pattern matches case-insensitively.
        if self.search == self.search.lower():
            return self.search in text.lower()
        return self.search in text

    def find_next(self, direction: int = 1) -> bool:
        """Move to the next matching cell in reading order.  Wraps around."""
        sheet = self.sheet
        if not self.search or not sheet.rows:
            return False
        ncols = max(1, sheet.ncols)
        total = len(sheet.rows) * ncols
        start = self.row * ncols + self.col
        for step in range(1, total + 1):
            at = (start + step * direction) % total
            row, col = divmod(at, ncols)
            if self._matches(self.cell(row, col)):
                self.row, self.col = row, col
                return True
        return False

    # -- drawing ------------------------------------------------------------
    def draw(self, win) -> None:
        theme.background(win, "edit")
        win.erase()
        height, width = win.getmaxyx()
        body_h = max(0, height - 3)
        self._clamp()

        self._draw_title(win, width)
        if self.error:
            try:
                win.addstr(2, 2, truncate(f"Cannot read: {self.error}",
                                          max(0, width - 4)))
            except curses.error:
                pass
            self._draw_footer(win, height, width)
            win.noutrefresh()
            return

        gutter = self.gutter()
        avail = max(0, width - gutter)
        self._ensure_visible(avail, body_h)
        columns = self.layout(avail)

        self._draw_headers(win, width, gutter, columns)
        for screen_row in range(body_h):
            index = self.top + screen_row
            if index >= len(self.sheet.rows):
                break
            self._draw_row(win, screen_row + 2, index, gutter, columns, width)

        if not self.sheet.rows:
            try:
                win.addstr(2, 2, truncate("(this sheet is empty)", width - 4))
            except curses.error:
                pass
        self._draw_footer(win, height, width)
        win.noutrefresh()

    def _draw_title(self, win, width: int) -> None:
        count = len(self.workbook.sheets)
        title = f" Sheet: {self.name}"
        if not self.error:
            title += f" -- {self.sheet.name} ({self.sheet_index + 1}/{count})"
            if self.sheet.truncated:
                title += " [truncated]"
        title += " "
        theme.paint(win, 0, 0, title, "keybar", width)

    def _draw_headers(self, win, width: int, gutter: int, columns) -> None:
        theme.fill(win, 1, 0, min(gutter, width), "panelhead")
        for index, x, cell_w in columns:
            role = "panelcursor" if index == self.col else "panelhead"
            theme.paint(win, 1, gutter + x, ljust(column_name(index), cell_w),
                        role)

    def _draw_row(self, win, y: int, index: int, gutter: int, columns,
                  width: int) -> None:
        role = "panelcursor" if index == self.row else "editnum"
        theme.paint(win, y, 0, rjust(str(index + 1), gutter - 1) + " ", role)
        for col, x, cell_w in columns:
            text = self.cell(index, col)
            shown = (rjust(text, cell_w) if looks_numeric(text)
                     else ljust(text, cell_w))
            cell_role = "edit"
            if index == self.row and col == self.col:
                cell_role = "panelcursor"
            elif index == self.row:
                cell_role = "editbold"
            # No guard here, unlike the writes around it: layout() only hands
            # back columns that end inside the window, and a body row is never
            # the last row, so this write is always one curses accepts.
            win.addstr(y, gutter + x, shown, theme.attr(cell_role))

    def _draw_footer(self, win, height: int, width: int) -> None:
        if self.notice:
            hint = self.notice
        elif self.error:
            hint = "[q]uit"
        else:
            value = self.current_cell()
            parts = [self.reference()]
            if value:
                parts.append(value)
            if self.search:
                parts.append(f"/{self.search}")
            parts.append("Tab sheet  [/]find  n/N  [w]idth  [q]uit")
            hint = "  ".join(parts)
        theme.paint(win, height - 1, 0, f" {hint} ", "keybar", width)

    # -- interaction --------------------------------------------------------
    def switch_sheet(self, delta: int) -> None:
        count = len(self.workbook.sheets)
        if count < 2:
            self.notice = " this workbook has only one sheet "
            return
        self.sheet_index = (self.sheet_index + delta) % count
        self.row = self.col = self.top = self.left = 0

    def cycle_width(self) -> None:
        position = WIDTH_CHOICES.index(self.max_width)
        self.max_width = WIDTH_CHOICES[(position + 1) % len(WIDTH_CHOICES)]

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
            body_h = max(1, height - 3)
            key = win.getch()
            self.notice = ""
            if key in (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10):
                break
            elif key in (curses.KEY_DOWN, ord("j")):
                self.row += 1
            elif key in (curses.KEY_UP, ord("k")):
                self.row -= 1
            elif key in (curses.KEY_LEFT, ord("h")):
                self.col -= 1
            elif key in (curses.KEY_RIGHT, ord("l")):
                self.col += 1
            elif key == curses.KEY_NPAGE:
                self.row += body_h
            elif key == curses.KEY_PPAGE:
                self.row -= body_h
            elif key == curses.KEY_HOME:
                self.col = 0
            elif key == curses.KEY_END:
                self.col = self.sheet.ncols - 1
            elif key == ord("g"):
                self.row = 0
            elif key == ord("G"):
                self.row = len(self.sheet.rows) - 1
            elif key in (9, ord("]")):                  # 9 = Tab
                self.switch_sheet(1)
            elif key in (curses.KEY_BTAB, ord("[")):
                self.switch_sheet(-1)
            elif key in (ord("/"), curses.KEY_F7):
                pattern = dialogs.prompt(stdscr, "Search",
                                         "Find in sheet (smart case):",
                                         default=self.search)
                curses.curs_set(0)
                if pattern is not None:
                    self.set_search(pattern)
                    if pattern and not self.find_next(1):
                        self.notice = f" '{pattern}' not found "
            elif key == ord("n"):
                if self.search and not self.find_next(1):
                    self.notice = " no match "
            elif key in (ord("N"), ord("p")):
                if self.search and not self.find_next(-1):
                    self.notice = " no match "
            elif key in (ord("w"), ord("W")):
                self.cycle_width()
            self._clamp()
