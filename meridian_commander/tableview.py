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

And the ways of finding one's way across a wide one:

- ``F`` freezes the columns up to the cursor's at the left edge, so a host
  name stays in view while the other thirty columns pan past it.
- ``i`` summarises the cursor's column over the rows shown: its type, how
  many values are empty and how many different, the statistics and a
  histogram of a column of numbers, and its most frequent values, any of
  which ``Enter`` turns into a filter.
- ``-`` hides the cursor's column, and ``+`` brings every hidden one back.

The keys the spreadsheet grid has mean the same here.  Reading, filtering,
sorting and summarising live in :mod:`meridian_commander.table`; this module
draws.
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
    Summary,
    Table,
    View,
    cell,
    clean,
    decimals,
    display_width,
    fit,
    format_number,
    is_numeric,
    parse_filter,
    quick_filter,
    read_table,
    shown_value,
    summarise,
    wrap,
)
from .util import rjust, run_full_screen, truncate

#: The colours the columns cycle through, when colour is on.
COLUMN_ROLES = ("edit", "tablecol1", "tablecol2", "tablecol3")

QUIT_KEYS = (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10)
ENTER_KEYS = (10, 13, curses.KEY_ENTER)
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)

FOOTER_KEYS = ("Enter row  [f]ilter  =/! this value  Bksp/0 unfilter  "
               "[s]ort  [i] summary  [F]reeze  -/+ hide/show  [/]find  "
               "[H]eader  [c]olour  [w]idth  [#]comments  [q]uit")
RECORD_KEYS = "Up/Down row  PgUp/PgDn scroll  Enter/q back to the table"
SUMMARY_KEYS = ("Up/Down value  Enter/= keep its rows  ! hide them  "
                "Left/Right column  PgUp/PgDn scroll  q back")
#: How a summary names what a column holds.
KIND_NAMES = {"number": "numbers", "date": "dates (ISO 8601)",
              "text": "text", "empty": "nothing: every cell is blank"}
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


def frozen_room(avail: int) -> int:
    """How many of ``avail`` screen columns the frozen columns may take.

    Two-thirds, so that whatever is frozen, the rest of the table can still
    be panned past it.
    """
    return avail - max(MIN_COL_WIDTH + 1, avail // 3)


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
        #: The columns before this one are frozen at the left edge; 0 for
        #: none.  A column number, like the filters' and the sort's, so it
        #: means the same columns whichever are hidden.
        self.frozen = 0
        #: The room the columns had when last drawn; ``None`` until then.
        self.avail: int | None = None
        #: The columns hidden from the grid, the search and the record.
        self.hidden: set[int] = set()
        #: The cursor's column summarised, when that is showing rather than
        #: the grid; which of its values is picked, and how far it scrolled.
        self.summary: Summary | None = None
        self.summary_pick = 0
        self.summary_top = 0
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

    def shown(self) -> list[int]:
        """The columns not hidden, left to right."""
        return [c for c in range(self.ncols) if c not in self.hidden]

    def pinned(self) -> list[int]:
        """The frozen columns not hidden, left to right."""
        return [c for c in range(min(self.frozen, self.ncols))
                if c not in self.hidden]

    def _neighbour(self, column: int, step: int) -> int:
        """The nearest column shown from ``column``, ``step`` being -1 for
        leftwards and 1 for rightwards; ``column`` itself at the edge."""
        index = column + step
        while 0 <= index < self.ncols:
            if index not in self.hidden:
                return index
            index += step
        return column

    def layout(self, avail: int) -> list[tuple[int, int, int]]:
        """``(column, x, width)`` for the columns drawn in ``avail`` columns.

        The frozen columns come first, at the left edge, and then the others
        from ``self.left`` on.  Hidden columns are left out.
        """
        widths = self._measure()[0]
        out: list[tuple[int, int, int]] = []
        x = 0
        # The frozen columns never take more than two-thirds of the screen.
        # F will not freeze more than that, but the screen can shrink or the
        # width limit grow afterwards: then the frozen columns that no longer
        # fit are left out, and a first one too wide is cut, as a column too
        # wide for the screen is.
        room = frozen_room(avail)
        for index in self.pinned():
            width = widths[index]
            if x + width > room:
                if not out and room > 0:
                    out.append((index, 0, room))
                    x = room + 1
                break
            out.append((index, x, width))
            x += width + 1          # one blank column as a separator
        frozen = len(out)
        rest = [c for c in range(max(self.left, self.frozen), len(widths))
                if c not in self.hidden]
        for index in rest:
            width = widths[index]
            if x + width > avail:
                break
            out.append((index, x, width))
            x += width + 1
        if len(out) == frozen and rest and avail > x:
            # Not even one column fits; show as much of it as there is room
            # for rather than an empty screen.
            out.append((rest[0], x, avail - x))
        return out

    def boundary(self, columns: list[tuple[int, int, int]]) -> int | None:
        """Where the line after the frozen columns goes, if any are drawn."""
        ends = [x + width for column, x, width in columns
                if column < self.frozen]
        return ends[-1] if ends else None

    def _ensure_visible(self, avail: int, body_h: int) -> None:
        if self.row < self.top:
            self.top = self.row
        elif self.row >= self.top + body_h:
            self.top = self.row - body_h + 1
        if self.col < self.frozen:
            return              # a frozen column is drawn wherever the rest are
        if self.col < max(self.left, self.frozen):
            self.left = self.col
        # Bounded by the cursor's own column, which is drawn, at least in
        # part, once the panning starts at it.
        while self.left < self.col:
            if self.col in [c for c, _x, _w in self.layout(avail)]:
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
        order they are shown in, since that is the table being read -- and
        only the columns shown, since a match in a hidden one could not be
        shown either.
        """
        if not self.search or not self.order:
            return False
        columns = self.shown()
        ncols = len(columns)
        total = len(self.order) * ncols
        start = self.row * ncols + columns.index(self.col)
        for step in range(1, total + 1):
            at = (start + step * direction) % total
            row, position = divmod(at, ncols)
            col = columns[position]
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

    # -- frozen and hidden columns ---------------------------------------------
    def toggle_freeze(self) -> None:
        """Freeze the columns up to the cursor's, or unfreeze them."""
        if self.frozen:
            self.frozen = 0
            self.notice = " columns unfrozen "
        elif not self.ncols:
            self.notice = " no column to freeze "
        elif self.avail is not None and self._span(self.col) > frozen_room(
                self.avail):
            self.notice = (" too wide to freeze here: those columns would "
                           "take over two-thirds of the screen ")
        else:
            self.frozen = self.col + 1
            self.notice = (f" frozen up to {self.labels()[self.col]}; "
                           "F again unfreezes ")

    def _span(self, column: int) -> int:
        """Screen columns the shown columns up to ``column`` take, drawn."""
        widths = self._measure()[0]
        return sum(widths[c] + 1 for c in range(column + 1)
                   if c not in self.hidden) - 1

    def hide_column(self) -> None:
        """Hide the cursor's column, which moves on to the next one shown."""
        if len(self.shown()) < 2:
            self.notice = " the last column shown cannot be hidden "
            return
        self.hidden.add(self.col)
        # The next column along takes the cursor, or at the right-hand edge
        # the one before.
        after = self._neighbour(self.col, 1)
        hidden, self.col = self.col, (after if after != self.col
                                      else self._neighbour(self.col, -1))
        self.notice = f" {self.labels()[hidden]} hidden; + shows it again "

    def show_columns(self) -> None:
        """Bring back every hidden column."""
        if not self.hidden:
            self.notice = " no columns are hidden "
            return
        count = len(self.hidden)
        self.hidden.clear()
        self.notice = f" {_count(count, 'hidden column')} shown again "

    # -- the column summary ------------------------------------------------------
    def open_summary(self) -> None:
        """Summarise the cursor's column over the rows the filters keep."""
        if not self.order:
            self.notice = " no rows to summarise "
            return
        # In the file's order, whatever the sort, so values that are equally
        # frequent are listed the same way every time.
        self.summary = summarise([cell(self.records[index], self.col)
                                  for index in sorted(self.order)])
        self.summary_pick = 0
        self.summary_top = 0

    def summary_filter(self, keep: bool) -> None:
        """Keep (``Enter``, ``=``) or hide (``!``) the rows holding the
        picked value, and go back to the table to show them."""
        if self.summary is None or not self.summary.top:
            self.notice = " no value to filter on "
            return
        value = self.summary.top[self.summary_pick][0]
        self.view.filters.append(quick_filter(
            self.labels()[self.col], self.col, value, keep))
        self.summary = None
        self.refresh()

    def _summary_stats(self, summary: Summary) -> list[tuple[str, str]]:
        rows = f"{summary.count:,}"
        if self.view.filters:
            rows += f" of {self.data_rows():,}, the rows the filters keep"
        stats = [("Holds", KIND_NAMES[summary.kind]), ("Rows", rows),
                 ("Empty", f"{summary.empty:,}"),
                 ("Distinct", f"{summary.distinct:,} values")]
        if summary.numbers is not None:
            stats += [(name.capitalize(), format_number(value))
                      for name, value in summary.numbers._asdict().items()]
        return stats

    def summary_lines(self, summary: Summary, width: int
                      ) -> list[tuple[str, str]]:
        """The summary laid out for a screen ``width`` wide, as ``(text,
        role)`` per line, the picked value in the cursor's role."""
        lines = [(f" {name:<9} {value}", "edit")
                 for name, value in self._summary_stats(summary)]
        lines.append(("", "edit"))
        heading = " Most frequent"
        if summary.distinct > len(summary.top):
            heading += f" ({len(summary.top)} of {summary.distinct:,})"
        lines.append((heading + ": Enter keeps the rows holding one, "
                      "! hides them", "editheading"))
        counts = max((len(f"{count:,}") for _v, count in summary.top),
                     default=0)
        for index, (value, count) in enumerate(summary.top):
            role = "panelcursor" if index == self.summary_pick else "edit"
            lines.append((f"  {count:>{counts},}  {shown_value(clean(value))}",
                          role))
        if not summary.top:
            lines.append(("  (none: every cell in these rows is blank)",
                          "editdim"))
        if summary.buckets:
            lines += [("", "edit"), (" Histogram", "editheading")]
            edges = [format_number(edge) for edge, _count in summary.buckets]
            edge_w = max(len(edge) for edge in edges)
            count_w = max(len(f"{count:,}") for _e, count in summary.buckets)
            bar_w = max(0, width - edge_w - count_w - 6)
            peak = max(count for _e, count in summary.buckets)
            for edge, (_edge, count) in zip(edges, summary.buckets):
                # Any range with something in it gets a bar, however short.
                length = max(1, round(count / peak * bar_w)) if count else 0
                bar = theme.glyph("block") * min(length, bar_w)
                lines.append((f"  {edge:>{edge_w}}  {bar:<{bar_w}} "
                              f"{count:>{count_w},}", "edit"))
        return [(fit(text, width), role) for text, role in lines]

    def _summary_first(self, summary: Summary) -> int:
        """The line the first of the most frequent values is on."""
        return len(self._summary_stats(summary)) + 2

    def _summary_column(self, step: int) -> None:
        """Summarise the next column shown to the left or right instead."""
        column = self._neighbour(self.col, step)
        if column == self.col:
            self.notice = (" this is the first column shown " if step < 0
                           else " this is the last column shown ")
            return
        self.col = column
        self.open_summary()

    # -- drawing -------------------------------------------------------------
    def draw(self, win) -> None:
        theme.background(win, "edit")
        win.erase()
        height, width = win.getmaxyx()
        self._clamp()
        if self.summary is not None:
            self._draw_summary(win, height, width, self.summary)
        elif self.record and self.order:
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
            avail = self.avail = max(0, width - gutter)
            self._ensure_visible(avail, body_h)
            columns = self.layout(avail)
            self._draw_headers(win, gutter, columns)
            for screen_row in range(min(body_h, len(self.order) - self.top)):
                self._draw_row(win, screen_row + 2, self.top + screen_row,
                               gutter, columns)
            line = self.boundary(columns)
            if line is not None:
                # A rule down the gap after the frozen columns, so it is
                # plain which columns stay and which pan.
                for y in range(1, 2 + min(body_h, len(self.order) - self.top)):
                    theme.paint(win, y, gutter + line, theme.glyph("v1"),
                                "panelhead" if y == 1 else "editdim")
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
            shape = _count(self.ncols, "column")
            if self.hidden:
                shape += f" ({len(self.hidden)} hidden)"
            shape += f", {self.table.delimiter_name}"
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
        columns = [column for column
                   in range(max(len(record), len(self.records[0])))
                   if column not in self.hidden]
        label_w = min(max((display_width(labels[column])
                           for column in columns), default=0),
                      max(8, width // 3))
        value_w = max(1, width - label_w - 3)
        lines: list[tuple[str, str, str]] = []
        for column in columns:
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
        if self.hidden:
            # Left out of the record as they are out of the grid, and said
            # so, so that a field nobody can see is not taken for missing.
            names = ", ".join(labels[column] for column in sorted(self.hidden))
            for piece in wrap(f"(hidden: {names}; + shows them)", value_w):
                lines.append((" " * (label_w + 3), piece, "editdim"))
        return label_w + 3, lines

    def _draw_record(self, win, height: int, width: int) -> None:
        index = self.order[self.row]
        parts = [f" Row {self.number(index):,} "
                 f"({self.row + 1:,} of {len(self.order):,} shown)"]
        if self.ragged(index):
            parts.append(self.ragged_note(index))
        if self.hidden:
            parts.append(f"{_count(len(self.hidden), 'column')} hidden")
        parts.append(f"{self.name} ")
        theme.paint(win, 0, 0, clean(" -- ".join(parts)), "keybar", width)
        start, lines = self.record_lines(width)
        body_h = max(0, height - 2)
        self.record_top = max(0, min(self.record_top, len(lines) - body_h))
        for y, (name, value, role) in enumerate(
                lines[self.record_top:self.record_top + body_h], start=1):
            theme.paint(win, y, 0, name, "editbold")
            theme.paint(win, y, start, value, role)

    def _draw_summary(self, win, height: int, width: int,
                      summary: Summary) -> None:
        parts = [f" Column: {self.labels()[self.col]}",
                 KIND_NAMES[summary.kind].split(":")[0],
                 f"{self.name} "]
        if self.view.filters:
            parts.insert(2, f"{summary.count:,} of "
                            f"{_count(self.data_rows(), 'row')}, filtered")
        else:
            parts.insert(2, _count(summary.count, "row"))
        theme.paint(win, 0, 0, clean(" -- ".join(parts)), "keybar", width)
        lines = self.summary_lines(summary, width)
        body_h = max(0, height - 2)
        self.summary_top = max(0, min(self.summary_top, len(lines) - body_h))
        for y, (text, role) in enumerate(
                lines[self.summary_top:self.summary_top + body_h], start=1):
            theme.paint(win, y, 0, text, role)

    def _draw_footer(self, win, height: int, width: int) -> None:
        if self.notice:
            hint = self.notice
        elif self.error:
            hint = "[q]uit"
        elif self.summary is not None:
            hint = SUMMARY_KEYS
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

    def _summary_key(self, key, body_h: int) -> None:
        summary = self.summary
        assert summary is not None
        if key in QUIT_KEYS or key == ord("i"):
            self.summary = None
        elif key in ENTER_KEYS or key == ord("="):
            self.summary_filter(keep=True)
        elif key == ord("!"):
            self.summary_filter(keep=False)
        elif key in (curses.KEY_DOWN, ord("j"), curses.KEY_UP, ord("k")):
            step = 1 if key in (curses.KEY_DOWN, ord("j")) else -1
            self.summary_pick = max(0, min(self.summary_pick + step,
                                           len(summary.top) - 1))
            # Keep the picked value on the screen.
            line = self._summary_first(summary) + self.summary_pick
            self.summary_top = max(min(self.summary_top, line),
                                   line - body_h + 1)
        elif key in (curses.KEY_LEFT, ord("h")):
            self._summary_column(-1)
        elif key in (curses.KEY_RIGHT, ord("l")):
            self._summary_column(1)
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.summary_top += body_h
        elif key == curses.KEY_PPAGE:
            self.summary_top = max(0, self.summary_top - body_h)
        elif key == curses.KEY_HOME:
            self.summary_top = 0
        elif key == curses.KEY_END:
            self.summary_top = _BOTTOM

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
            self.col = self._neighbour(self.col, -1)
        elif key in (curses.KEY_RIGHT, ord("l")):
            self.col = self._neighbour(self.col, 1)
        elif key == curses.KEY_NPAGE:
            self.row += body_h
        elif key == curses.KEY_PPAGE:
            self.row -= body_h
        elif key == curses.KEY_HOME:
            self.col = self._neighbour(-1, 1)
        elif key == curses.KEY_END:
            self.col = self._neighbour(self.ncols, -1)
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
        elif key == ord("i"):
            self.open_summary()
        elif key == ord("F"):
            self.toggle_freeze()
        elif key == ord("-"):
            self.hide_column()
        elif key == ord("+"):
            self.show_columns()
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
        run_full_screen(self, stdscr)

    def handle_key(self, key, stdscr, height: int, width: int) -> bool:
        """Apply one key; False when it closes the table."""
        self.notice = ""
        if self.summary is not None:
            self._summary_key(key, max(1, height - 2))
        elif self.record and self.order:
            self._record_key(key, max(1, height - 2))
        elif key in QUIT_KEYS:
            return False
        else:
            self._grid_key(key, stdscr, max(1, height - 3))
        self._clamp()
        return True
