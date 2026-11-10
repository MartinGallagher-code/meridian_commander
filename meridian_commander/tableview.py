# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""A full-screen browser for delimited text tables: TSV, CSV and the like.

The text viewer shows a TSV with its tabs expanded to every fourth column,
which lines the columns up only while every cell is shorter than four
characters.  This shows it as a table instead: every column as wide as its
values, the file's own header pinned above them, numbers right-aligned on
their decimal points, and CJK and emoji measured in the columns they take.

On top of the grid, the ways of narrowing a long results file down:

- ``f`` keeps the rows that match: a word anywhere, or ``column op value``.
  Filters stack, and ``=``/``!`` on a cell keep or hide the rows holding
  that value -- a drill-down in two keys.
- ``s`` sorts by the cursor's column: ascending, descending, then back.
- ``Enter`` shows the cursor's row as a record, one field per line, so a
  row of thirty columns is readable on an eighty-column screen.

The keys the spreadsheet grid has mean the same here.  Reading, filtering
and sorting live in :mod:`meridian_commander.table`; this module draws.
"""

from __future__ import annotations

import curses

from . import theme
from .filesystems import FileSystem
from .sheetview import (
    MIN_COL_WIDTH,
    WIDTH_CHOICES,
    WIDTH_SAMPLE_ROWS,
    looks_numeric,
)
from .table import (
    Table,
    View,
    cell,
    clean,
    decimals,
    display_width,
    fit,
    is_numeric,
    parse_filter,
    quick_filter,
    read_table,
    wrap,
)
from .util import read_key, rjust, truncate

#: The colours the columns cycle through, when colour is on.
COLUMN_ROLES = ("edit", "tablecol1", "tablecol2", "tablecol3")

QUIT_KEYS = (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10)
ENTER_KEYS = (10, 13, curses.KEY_ENTER)
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)

FOOTER_KEYS = ("Enter row  [f]ilter  =/! this value  Bksp/0 unfilter  "
               "[s]ort  [/]find  [H]eader  [c]olour  [w]idth  [#]comments  "
               "[q]uit")
RECORD_KEYS = "Up/Down row  PgUp/PgDn scroll  Enter/q back to the table"
# Scrolled "to the end" of a record; drawing clamps it to the last screenful,
# which only drawing knows the height of.
_BOTTOM = 1 << 30


def displayed(text: str, tail: int) -> tuple[str, bool]:
    """A cell's text as the grid draws it, and whether it is right-aligned.

    A number is right-aligned, and padded on the right out to ``tail`` -- the
    widest decimal part in its column -- so the points line up: ``7``,
    ``12.5`` and ``0.25`` sit one above another with the units aligned.
    """
    text = clean(text)
    if not looks_numeric(text):
        return text, False
    text = text.strip()
    return text + " " * max(0, tail - decimals(text)), True


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}" + ("" if number == 1 else "s")


class TableView:
    def __init__(self, fs: FileSystem, path: str) -> None:
        self.fs = fs
        self.path = path
        self.name = fs.basename(path)
        self.error: str | None = None
        self.has_header = True
        self.view = View()
        #: Indices into the records, in the order shown: the filters and the
        #: sort applied.  ``row`` is a position in this list.
        self.order: list[int] = []
        self.row = 0
        self.col = 0
        self.top = 0
        self.left = 0
        self.search = ""
        self.notice = ""
        self.max_width = WIDTH_CHOICES[1]
        self.colour = True
        #: Showing the cursor's row as a record rather than the grid.
        self.record = False
        self.record_top = 0
        self._measured: dict[tuple[bool, int], tuple[list[int], list[bool],
                                                     list[int]]] = {}
        self._labels: dict[bool, list[str]] = {}
        self.table = self._load()
        self.ncols = self.table.ncols
        self.refresh()

    def _load(self) -> Table:
        try:
            return read_table(self.fs, self.path)
        except Exception as exc:    # unreadable file, or the backend failed
            self.error = str(exc) or type(exc).__name__
            # An empty table rather than none, so every path below works on
            # the error screen without a second set of guards.
            return Table([], ",")

    # -- the records -------------------------------------------------------
    @property
    def records(self) -> list[list[str]]:
        return self.table.records

    @property
    def first(self) -> int:
        """The index of the first row of data: 1 when line one is the header."""
        return 1 if self.has_header and self.records else 0

    def data_rows(self) -> int:
        return len(self.records) - self.first

    def labels(self) -> list[str]:
        """Each column's name: its header, or its number where it has none."""
        cached = self._labels.get(self.has_header)
        if cached is None:
            header = self.records[0] if self.has_header and self.records else []
            cached = [clean(cell(header, index)).strip() or str(index + 1)
                      for index in range(self.ncols)]
            self._labels[self.has_header] = cached
        return cached

    def ragged(self, index: int) -> bool:
        """Whether a record has more or fewer fields than the first line."""
        return len(self.records[index]) != len(self.records[0])

    def ragged_note(self, index: int) -> str:
        return (f"{_count(len(self.records[index]), 'field')}; "
                f"the first line has {len(self.records[0])}")

    def number(self, index: int) -> int:
        """A record's row number in the file, counting from the first row of
        data -- the same however the view is filtered or sorted."""
        return index - self.first + 1

    def current_cell(self) -> str:
        if not self.order:
            return ""
        return cell(self.records[self.order[self.row]], self.col)

    def refresh(self) -> None:
        """Apply the filters and sort again, keeping the cursor's row if shown."""
        current = self.order[self.row] if self.order else None
        self.order = self.view.rows(self.records, self.first)
        if current in self.order:
            self.row = self.order.index(current)
        self._clamp()

    def _clamp(self) -> None:
        self.row = max(0, min(self.row, len(self.order) - 1))
        self.col = max(0, min(self.col, self.ncols - 1))

    # -- geometry ------------------------------------------------------------
    def _measure(self) -> tuple[list[int], list[bool], list[int]]:
        """Each column's width, whether it holds numbers, and its decimal tail.

        Measured once, over the top of the file, so the columns do not shift
        under the reader while scrolling -- the spreadsheet grid's rule.
        """
        key = (self.has_header, self.max_width)
        cached = self._measured.get(key)
        if cached is not None:
            return cached
        sample = self.records[self.first:self.first + WIDTH_SAMPLE_ROWS]
        labels = self.labels()
        widths, numeric, tails = [], [], []
        for column in range(self.ncols):
            values = [cell(record, column) for record in sample]
            numeric.append(is_numeric(values))
            tail = max((decimals(value) for value in values
                        if looks_numeric(value)), default=0)
            tails.append(tail if numeric[-1] else 0)
            # One column more than the name, for the sort arrow.
            width = display_width(labels[column]) + 1
            for value in values:
                width = max(width, display_width(displayed(value, tails[-1])[0],
                                                 self.max_width))
            widths.append(max(MIN_COL_WIDTH, min(width, self.max_width)))
        measured = self._measured[key] = (widths, numeric, tails)
        return measured

    def gutter(self) -> int:
        """Width of the row-number column, including its marker column."""
        return max(4, len(str(max(1, self.data_rows()))) + 1)

    def layout(self, avail: int) -> list[tuple[int, int, int]]:
        """``(column, x, width)`` for the columns visible from ``self.left``."""
        widths = self._measure()[0]
        out: list[tuple[int, int, int]] = []
        x = 0
        for index in range(self.left, len(widths)):
            width = widths[index]
            if x + width > avail:
                break
            out.append((index, x, width))
            x += width + 1          # one blank column as a separator
        if not out and self.left < len(widths) and avail > 0:
            # Not even one column fits; show as much of it as there is room
            # for rather than an empty screen.
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

    # -- search ----------------------------------------------------------------
    def set_search(self, pattern: str) -> None:
        self.search = pattern or ""

    def _matches(self, text: str) -> bool:
        # Smart case, as everywhere else: an all-lowercase pattern matches
        # case-insensitively.
        if self.search == self.search.lower():
            return self.search in text.lower()
        return self.search in text

    def find_next(self, direction: int = 1) -> bool:
        """Move to the next matching cell of the rows shown, in reading order.

        Wraps around.  Only the rows the filters keep are searched, in the
        order they are shown in, since that is the table being read.
        """
        if not self.search or not self.order:
            return False
        ncols = self.ncols
        total = len(self.order) * ncols
        start = self.row * ncols + self.col
        for step in range(1, total + 1):
            at = (start + step * direction) % total
            row, col = divmod(at, ncols)
            if self._matches(cell(self.records[self.order[row]], col)):
                self.row, self.col = row, col
                return True
        return False

    # -- filters, sort and display -------------------------------------------
    def add_filter(self, expression: str) -> bool:
        """Add the filter typed at the prompt; False, with a notice, if it
        names a column the table does not have."""
        try:
            self.view.filters.append(parse_filter(expression, self.labels()))
        except ValueError as exc:
            self.notice = f" {exc} "
            return False
        self.refresh()
        return True

    def quick_filter(self, keep: bool) -> None:
        """Keep (``=``) or hide (``!``) the rows holding the cursor's value."""
        if not self.order:
            self.notice = " no row to take a value from "
            return
        self.view.filters.append(quick_filter(
            self.labels()[self.col], self.col, self.current_cell(), keep))
        self.refresh()

    def drop_filter(self) -> None:
        if not self.view.filters:
            self.notice = " no filters to drop "
            return
        self.view.filters.pop()
        self.refresh()

    def clear_filters(self) -> None:
        if not self.view.filters:
            self.notice = " no filters to clear "
            return
        self.view.filters.clear()
        self.refresh()

    def sort_by_cursor(self) -> None:
        if self.ncols:
            self.view.sort_by(self.col)
            self.refresh()

    def toggle_header(self) -> None:
        """Read the first line as the header, or as the first row of data.

        The filters and sort keys hold column numbers, not names, so they
        carry across unchanged.
        """
        self.has_header = not self.has_header
        self.refresh()
        self.notice = (" the first line is the header " if self.has_header
                       else " the first line is data ")

    def toggle_colour(self) -> None:
        self.colour = not self.colour
        self.notice = " column colours on " if self.colour \
            else " column colours off "

    def cycle_width(self) -> None:
        position = WIDTH_CHOICES.index(self.max_width)
        self.max_width = WIDTH_CHOICES[(position + 1) % len(WIDTH_CHOICES)]

    # -- drawing -------------------------------------------------------------
    def draw(self, win) -> None:
        theme.background(win, "edit")
        win.erase()
        height, width = win.getmaxyx()
        self._clamp()
        if self.record and self.order:
            self._draw_record(win, height, width)
        else:
            self._draw_grid(win, height, width)
        self._draw_footer(win, height, width)
        win.noutrefresh()

    def _message(self) -> str | None:
        """What to say where the rows would be, when there are none to show."""
        if self.error:
            return f"Cannot read: {self.error}"
        if not self.records:
            if self.table.comments:
                return "(nothing but comment lines; # shows them)"
            return "(this file is empty)"
        if not self.data_rows():
            return "(nothing below the header; H reads the first line as data)"
        if not self.order:
            return ("(no row matches the filters; Backspace drops the last, "
                    "0 clears them)")
        return None

    def _draw_grid(self, win, height: int, width: int) -> None:
        body_h = max(0, height - 3)
        self._draw_title(win, width)
        if not self.error and self.records:
            gutter = self.gutter()
            avail = max(0, width - gutter)
            self._ensure_visible(avail, body_h)
            columns = self.layout(avail)
            self._draw_headers(win, gutter, columns)
            for screen_row in range(min(body_h, len(self.order) - self.top)):
                self._draw_row(win, screen_row + 2, self.top + screen_row,
                               gutter, columns)
        message = self._message()
        if message:
            try:
                win.addstr(2, 2, truncate(message, max(0, width - 4)))
            except curses.error:
                pass

    def _draw_title(self, win, width: int) -> None:
        # Most telling first, since a narrow screen cuts the end off: how many
        # rows and which filters chose them, the sort, then the shape of the
        # file.
        parts = [f" Table: {self.name}"]
        if not self.error:
            rows = self.data_rows()
            if self.view.filters:
                info = (f"{len(self.order):,} of {_count(rows, 'row')}: "
                        + ", ".join(f.text for f in self.view.filters))
            else:
                info = _count(rows, "row")
            if self.table.truncated:
                info += " [truncated]"
            parts.append(info)
            if self.view.keys:
                labels = self.labels()
                parts.append("sorted by " + ", ".join(
                    labels[key.column] + theme.glyph("down" if key.descending
                                                     else "up")
                    for key in self.view.keys))
            shape = (f"{_count(self.ncols, 'column')}, "
                     f"{self.table.delimiter_name}")
            comments = len(self.table.comments)
            if comments:
                shape += f", {_count(comments, 'comment line')} above (#)"
            parts.append(shape)
        theme.paint(win, 0, 0, clean(" -- ".join(parts) + " "), "keybar", width)

    def _draw_headers(self, win, gutter: int, columns) -> None:
        theme.fill(win, 1, 0, gutter, "panelhead")
        labels = self.labels()
        numeric = self._measure()[1]
        for index, x, cell_w in columns:
            direction = self.view.direction(index)
            if direction is None:
                text = fit(labels[index], cell_w, numeric[index])
            else:
                text = (fit(labels[index], cell_w - 1, numeric[index])
                        + theme.glyph("down" if direction else "up"))
            role = "panelcursor" if index == self.col else "panelhead"
            theme.paint(win, 1, gutter + x, text, role)

    def _draw_row(self, win, y: int, position: int, gutter: int,
                  columns) -> None:
        index = self.order[position]
        record = self.records[index]
        cursor = position == self.row
        ragged = self.ragged(index)
        # A row with the wrong number of fields is marked, not quietly padded:
        # a "!" beside its number, which reads without colour too.
        role = "panelcursor" if cursor else "panelerror" if ragged \
            else "editnum"
        theme.paint(win, y, 0, rjust(str(self.number(index)), gutter - 1)
                    + ("!" if ragged else " "), role)
        tails = self._measure()[2]
        for col, x, cell_w in columns:
            text, right = displayed(cell(record, col), tails[col])
            if cursor and col == self.col:
                role = "panelcursor"
            elif cursor:
                role = "editbold"
            elif self.colour:
                role = COLUMN_ROLES[col % len(COLUMN_ROLES)]
            else:
                role = "edit"
            # Guarded, unlike the spreadsheet grid's cells: a terminal can
            # count an emoji's width differently from the Unicode tables, and
            # a cell that then runs past the edge must not end the program.
            theme.paint(win, y, gutter + x, fit(text, cell_w, right), role)

    def record_lines(self, width: int
                     ) -> tuple[int, list[tuple[str, str, str]]]:
        """The cursor's row laid out as a record, for a screen ``width`` wide.

        Returns where the values start, and ``(name, value, role)`` per line:
        one field to a line, ``name : value``, the value wrapped rather than
        cut.  A field the row is short of is said to be missing, and one past
        the first line's fields is shown under its number.
        """
        index = self.order[self.row]
        record = self.records[index]
        labels = self.labels()
        label_w = min(max(display_width(label) for label in labels),
                      max(8, width // 3))
        value_w = max(1, width - label_w - 3)
        lines: list[tuple[str, str, str]] = []
        for column in range(max(len(record), len(self.records[0]))):
            name = fit(labels[column], label_w, right=True) + " : "
            if column < len(record):
                pieces = wrap(record[column], value_w)
                role = COLUMN_ROLES[column % len(COLUMN_ROLES)] \
                    if self.colour else "edit"
            else:
                pieces = ["(missing: this row is short of fields)"]
                role = "editdim"
            for number, piece in enumerate(pieces):
                lines.append((name if number == 0 else " " * (label_w + 3),
                              piece, role))
        return label_w + 3, lines

    def _draw_record(self, win, height: int, width: int) -> None:
        index = self.order[self.row]
        parts = [f" Row {self.number(index):,} "
                 f"({self.row + 1:,} of {len(self.order):,} shown)"]
        if self.ragged(index):
            parts.append(self.ragged_note(index))
        parts.append(f"{self.name} ")
        theme.paint(win, 0, 0, clean(" -- ".join(parts)), "keybar", width)
        start, lines = self.record_lines(width)
        body_h = max(0, height - 2)
        self.record_top = max(0, min(self.record_top, len(lines) - body_h))
        for y, (name, value, role) in enumerate(
                lines[self.record_top:self.record_top + body_h], start=1):
            theme.paint(win, y, 0, name, "editbold")
            theme.paint(win, y, start, value, role)

    def _draw_footer(self, win, height: int, width: int) -> None:
        if self.notice:
            hint = self.notice
        elif self.error:
            hint = "[q]uit"
        elif self.record and self.order:
            hint = RECORD_KEYS
        else:
            parts = []
            if self.order:
                parts.append(f"{self.labels()[self.col]}: "
                             f"{clean(self.current_cell())}")
                index = self.order[self.row]
                if self.ragged(index):
                    parts.append(f"[{self.ragged_note(index)}]")
            if self.search:
                parts.append(f"/{self.search}")
            parts.append(FOOTER_KEYS)
            hint = "  ".join(parts)
        theme.paint(win, height - 1, 0, f" {hint} ", "keybar", width)

    # -- interaction ---------------------------------------------------------
    def _record_key(self, key, body_h: int) -> None:
        if key in QUIT_KEYS or key in ENTER_KEYS:
            self.record = False
        elif key in (curses.KEY_DOWN, ord("j")):
            if self.row + 1 < len(self.order):
                self.row += 1
                self.record_top = 0
            else:
                self.notice = " this is the last row shown "
        elif key in (curses.KEY_UP, ord("k")):
            if self.row > 0:
                self.row -= 1
                self.record_top = 0
            else:
                self.notice = " this is the first row shown "
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.record_top += body_h
        elif key == curses.KEY_PPAGE:
            self.record_top = max(0, self.record_top - body_h)
        elif key == curses.KEY_HOME:
            self.record_top = 0
        elif key == curses.KEY_END:
            self.record_top = _BOTTOM

    def _prompt_search(self, stdscr) -> None:
        from . import dialogs

        pattern = dialogs.prompt(stdscr, "Search", "Find in table (smart case):",
                                 default=self.search)
        curses.curs_set(0)
        if pattern is not None:
            self.set_search(pattern)
            if pattern and not self.find_next(1):
                self.notice = f" '{pattern}' not found "

    def _show_comments(self, stdscr) -> None:
        """The comment lines set aside from the top of the file, in a box."""
        from . import dialogs

        if not self.table.comments:
            self.notice = " no comment lines at the top of this file "
            return
        dialogs.message(stdscr, "Comments above the table",
                        "\n".join(clean(line) for line in self.table.comments))
        curses.curs_set(0)

    def _prompt_filter(self, stdscr) -> None:
        from . import dialogs

        expression = dialogs.prompt(
            stdscr, "Filter",
            "Keep rows with: a word, or column op value (= != < > <= >= ~ !~)")
        curses.curs_set(0)
        if expression and expression.strip():
            self.add_filter(expression)

    def _grid_key(self, key, stdscr, body_h: int) -> None:
        if key in (curses.KEY_DOWN, ord("j")):
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
            self.col = self.ncols - 1
        elif key == ord("g"):
            self.row = 0
        elif key == ord("G"):
            self.row = len(self.order) - 1
        elif key in ENTER_KEYS:
            if self.order:
                self.record = True
                self.record_top = 0
        elif key == ord("f"):
            self._prompt_filter(stdscr)
        elif key == ord("="):
            self.quick_filter(keep=True)
        elif key == ord("!"):
            self.quick_filter(keep=False)
        elif key in BACKSPACE_KEYS:
            self.drop_filter()
        elif key == ord("0"):
            self.clear_filters()
        elif key in (ord("s"), ord("S")):
            self.sort_by_cursor()
        elif key == ord("H"):
            self.toggle_header()
        elif key in (ord("c"), ord("C")):
            self.toggle_colour()
        elif key in (ord("w"), ord("W")):
            self.cycle_width()
        elif key in (ord("/"), curses.KEY_F7):
            self._prompt_search(stdscr)
        elif key == ord("#"):
            self._show_comments(stdscr)
        elif key == ord("n"):
            if self.search and not self.find_next(1):
                self.notice = " no match "
        elif key in (ord("N"), ord("p")):
            if self.search and not self.find_next(-1):
                self.notice = " no match "

    def run(self, stdscr) -> None:
        curses.curs_set(0)
        height, width = stdscr.getmaxyx()
        win = curses.newwin(height, width, 0, 0)
        win.keypad(True)
        while True:
            self.draw(win)
            curses.doupdate()
            height, width = stdscr.getmaxyx()
            key = read_key(win)
            self.notice = ""
            if self.record and self.order:
                self._record_key(key, max(1, height - 2))
            elif key in QUIT_KEYS:
                break
            else:
                self._grid_key(key, stdscr, max(1, height - 3))
            self._clamp()
