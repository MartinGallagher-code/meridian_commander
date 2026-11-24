# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Two tables and the rows they make joined, on one screen (``K``).

File A, the left pane's file, is in the top left quarter; File B, the right
pane's, in the top right; and the joined rows run across the whole bottom
half.  Each of the three is a table browser
(:class:`~meridian_commander.tableview.TableView`) in a window of its own, so
filters, sorting, the record view, the column summary and ``F2`` work in each
exactly as they do anywhere else.  What this screen adds is the keyboard
moving between them, and the join:

- ``m`` in File A or File B marks the cursor's column as a join column, or
  unmarks it.  The marked columns pair up in the order they were marked, and
  each header shows its place (``1:id``) so the pairs read straight across.
- The output is recomputed whenever what it is made from changes: a column
  marked, or a filter, sort or header toggled in either file.  It is made
  from the rows each quarter shows, in the order shown, so a filter in File A
  narrows the join to the rows it keeps.  The joining itself, with nothing
  drawn, is :mod:`meridian_commander.tablejoin`.
- ``o`` chooses which rows are kept and ``e`` how strict a match is; ``z``
  zooms the region with the keyboard to the whole screen and back.
- Moving the output's cursor moves File A's and File B's to the rows that
  output row came from.

Like ``D``, it takes the left pane's file for the left of the screen whichever
pane is active, so the picture matches where the files came from.
"""

from __future__ import annotations

import curses
from typing import Sequence

from . import dialogs, saveview, tablejoin, theme
from .filesystems import FileSystem
from .table import Table
from .tableview import TableView
from .util import read_key, truncate

#: The smallest screen the three regions share: each needs its title, its
#: header, a row or two and its footer.  Smaller, ``z`` still shows one.
MIN_HEIGHT = 10
MIN_WIDTH = 40

TAB_KEYS = (9,)
BACK_TAB_KEYS = (curses.KEY_BTAB,)
HELP_KEYS = (curses.KEY_F1, ord("?"))

QUARTER_KEYS = ("Tab next  [m]ark join column  [o] rows  [e] match  [z]oom  "
                "F1 keys  [q]uit")
OUTPUT_KEYS = ("Tab next  [o] rows  [e] match  [z]oom  F2 save  F1 keys  "
               "[q]uit")

NAMES = ("File A", "File B", "the output")

HELP = """\
File A, top left, is the left pane's file; File B, top right,
the right pane's.  The bottom half is every combination of an A
row and a B row whose join columns match: a value twice in A and
three times in B gives six rows.

  Tab, Shift-Tab  the keyboard to the next region, or back
  m               in File A or B: mark the cursor's column as a
                  join column, or unmark it.  They pair up in
                  the order marked
  o               which rows are kept: matching rows only; and
                  every row of A; and every row of A and B
  e               how strict a match is: exact text; any case,
                  spaces trimmed; and numbers by value (7 = 007)
  z               this region on the whole screen, and back
  F2, Ctrl-S      save this region's rows; in the output, the join
  q, Esc          back to the panes

Each region is a table browser, with all of its keys: f = !
filter, Backspace 0 unfilter, s sort, Enter the row as a record,
i column summary, F freeze, - + hide and show columns, H header,
/ find.

The output is made from the rows File A and File B show, in the
order shown, so a filter in either narrows the join.  A row whose
join cells are all blank matches nothing."""


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}" + ("" if number == 1 else "s")


def _next(names: Sequence[str], current: str) -> str:
    """The choice after ``current``, round to the first again."""
    return names[(list(names).index(current) + 1) % len(names)]


def _stem(name: str) -> tuple[str, str]:
    """A file name split into its stem and its extension, dot included."""
    stem, dot, ext = name.rpartition(".")
    return (stem, dot + ext) if dot and stem else (name, "")


class JoinedView(TableView):
    """The bottom half: the joined rows, browsed as any table is.

    Until there is something to join, it holds no rows and ``instruction``
    says what to do instead.
    """

    def __init__(self, fs: FileSystem, path: str, table: Table,
                 instruction: str | None = None) -> None:
        super().__init__(fs, path, table)
        self.caption = "Joined"
        self.footer_keys = OUTPUT_KEYS
        self.instruction = instruction
        #: Which A column goes with which B column, said as the title says it.
        self.pairing = ""
        self.keep = tablejoin.KEEPS[0]
        self.rule = tablejoin.RULES[0]
        #: The join stopped at the row limit, with more to come.
        self.cut = False
        #: The files only partly read, by name: "File A", "File B".
        self.partial: list[str] = []
        #: The two files' names, which a saved join is named after.
        self.files = ("", "")
        self.blanks = (0, 0)

    def _message(self) -> str | None:
        if self.instruction:
            return self.instruction
        if not self.records[1:]:
            return ("(no rows match: no row of A has the same values in its "
                    "join columns as a row of B; e loosens the match, o "
                    "keeps the rows that do not match)")
        return super()._message()

    def title_parts(self) -> list[str]:
        if self.instruction:
            return [f" {self.caption}: nothing yet"]
        parts = super().title_parts()
        parts[0] = f" Joined on {self.pairing}"
        rule = (f"{tablejoin.KEEP_NAMES[self.keep]}, "
                f"{tablejoin.RULE_NAMES[self.rule]}")
        blank_a, blank_b = self.blanks
        if blank_a or blank_b:
            rule += (f", blank keys match nothing ({blank_a:,} in A, "
                     f"{blank_b:,} in B)")
        parts.insert(2, rule)
        return parts

    def truncated_note(self) -> str:
        notes = []
        if self.cut:
            notes.append(f"cut short at {self.data_rows():,} rows")
        notes += [f"{name} only partly read" for name in self.partial]
        return f" [{'; '.join(notes)}]"

    def cut_short(self) -> tuple[str, str]:
        rows = _count(self.data_rows(), "row")
        if self.cut:
            return (f"Only the first {rows} of the join can be saved: it "
                    f"stopped there.", f", the first {rows} only: the join "
                    f"stopped there")
        which = " and ".join(self.partial)
        return (f"The join is of the rows read: {which} goes on past them.",
                f", from the rows read only: {which} goes on")

    def save_base(self) -> tuple[str, str]:
        """``people-orders.joined.tsv``: both files' names, in A's format."""
        a_stem, a_ext = _stem(self.files[0])
        b_stem = _stem(self.files[1])[0]
        return f"{a_stem}-{b_stem}{a_ext}", "joined"


class JoinScreen:
    """File A, File B and their join, three table browsers on one screen."""

    def __init__(self, a_fs: FileSystem, a_path: str, b_fs: FileSystem,
                 b_path: str, a_elsewhere: saveview.Place | None = None,
                 b_elsewhere: saveview.Place | None = None) -> None:
        self.a = TableView(a_fs, a_path)
        self.b = TableView(b_fs, b_path)
        for view, caption, elsewhere in ((self.a, "File A", a_elsewhere),
                                         (self.b, "File B", b_elsewhere)):
            view.caption = caption
            view.footer_keys = QUARTER_KEYS
            view.elsewhere = elsewhere
        self.keep = tablejoin.KEEPS[0]
        self.rule = tablejoin.RULES[0]
        #: Which region has the keyboard: 0 File A, 1 File B, 2 the output.
        self.focus = 0
        self.zoomed = False
        #: Said in the footer of the region with the keyboard, next draw.
        self.notice = ""
        self.joined: tablejoin.Joined | None = None
        self.out = JoinedView(a_fs, a_path, Table([], ","), "")
        # The output saves beside File A; where that cannot be written, in
        # the other pane's directory, as File A's own view does.
        self.out.elsewhere = a_elsewhere
        #: What the output was last made from, to tell when it is stale.
        self._inputs: tuple | None = None
        #: The output row the two files' cursors were last moved to.
        self._followed: int | None = None
        #: Each region's window and the geometry it was made for.
        self._windows: dict[int, tuple[tuple[int, int, int, int], object]] = {}
        self.sync()

    @property
    def views(self) -> list[TableView]:
        return [self.a, self.b, self.out]

    @property
    def saved(self) -> list[saveview.Written]:
        """Every file ``F2`` has written in any region, for the panes."""
        return self.a.saved + self.b.saved + self.out.saved

    # -- the join ---------------------------------------------------------------
    def instruction(self) -> str | None:
        """What the output says to do, when there is nothing to join yet."""
        for view, name in ((self.a, "File A"), (self.b, "File B")):
            if view.error:
                return f"{name} cannot be read, so there is nothing to join."
        marked = len(self.a.marks), len(self.b.marks)
        if marked == (0, 0):
            return ("Mark the column to join on in File A and in File B: m "
                    "on a column marks it, Tab moves between them.")
        for count, name in zip(marked, ("File A", "File B")):
            if not count:
                return (f"Now mark the column to join on in {name}: Tab to "
                        f"it, then m.")
        if marked[0] != marked[1]:
            return (f"File A has {_count(marked[0], 'join column')} marked "
                    f"and File B {marked[1]:,}: mark or unmark until they are "
                    f"the same.  They pair up in the order marked.")
        return None

    def _inputs_now(self) -> tuple:
        a, b = self.a, self.b
        return (a.order, a.has_header, tuple(a.marks), b.order, b.has_header,
                tuple(b.marks), self.keep, self.rule)

    def stale(self) -> bool:
        """Whether what the output is made from has changed since it was.

        The rows each quarter shows are compared by identity: a quarter
        builds a new list whenever a filter, sort or header changes them, so
        that is the test of a change, and it costs nothing per key.
        """
        if self._inputs is None:
            return True
        return any(new is not old if isinstance(new, list) else new != old
                   for new, old in zip(self._inputs_now(), self._inputs))

    def sync(self) -> None:
        """Make the output again if it is stale, and let the two files'
        cursors follow the output's."""
        if self.stale():
            self._rejoin()
        if self.focus == 2:
            self._follow()

    def _rejoin(self) -> None:
        a, b, old = self.a, self.b, self.out
        self._inputs = self._inputs_now()
        self._followed = None
        instruction = self.instruction()
        if instruction:
            self.joined = None
            new = JoinedView(a.fs, a.path, Table([], a.table.delimiter),
                             instruction)
        else:
            joined = self.joined = tablejoin.join(
                tablejoin.Side(a.records, a.order, a.marks, a.ncols),
                tablejoin.Side(b.records, b.order, b.marks, b.ncols),
                self.keep, self.rule)
            header = tablejoin.labels(a.labels(), b.labels())
            partial = [name for view, name in ((a, "File A"), (b, "File B"))
                       if view.table.truncated]
            new = JoinedView(a.fs, a.path, Table(
                [header] + joined.rows, a.table.delimiter, a.table.quoted,
                joined.truncated or bool(partial)))
            new.pairing = tablejoin.pairing(a.labels(), a.marks, b.labels(),
                                            b.marks)
            new.keep, new.rule = self.keep, self.rule
            new.cut, new.partial = joined.truncated, partial
            new.blanks = (joined.blank_a, joined.blank_b)
            new.files = (a.name, b.name)
            new.name = f"{a.name} joined with {b.name}"
            if old.instruction is None and old.labels() == new.labels():
                # The same columns: the output's own filters, sort, hidden
                # columns and place still mean what they did, so they stay.
                new.view, new.hidden, new.frozen = (old.view, old.hidden,
                                                    old.frozen)
                new.refresh()
                # The cursor stays where it was on the screen: the rows
                # under it may be others now, which is the change asked for.
                new.row, new.col, new.top, new.left = (old.row, old.col,
                                                       old.top, old.left)
                new._clamp()
            if joined.truncated:
                self.notice = (f" the join stopped at {len(joined.rows):,} "
                               f"rows: there are more ")
        for name in ("saved", "elsewhere", "colour", "max_width", "active"):
            setattr(new, name, getattr(old, name))
        self.out = new

    def _follow(self) -> None:
        """Move File A's and File B's cursors to the rows the output's
        cursor row was made from."""
        out, joined = self.out, self.joined
        if joined is None or not out.order:
            return
        index = out.order[out.row]
        if index == self._followed:
            return
        self._followed = index
        # Record 0 is the header row the join wrote; with H it is data, and
        # comes from neither file.
        if index == 0:
            return
        # The output was made from the rows each file shows (sync makes it
        # again before this whenever they change), so both are there.
        for view, record in zip((self.a, self.b), joined.sources[index - 1]):
            if record is not None:
                view.row = view.order.index(record)

    # -- keys ---------------------------------------------------------------------
    def _mark(self, view: TableView) -> None:
        """Mark the cursor's column as a join column, or unmark it."""
        if view.error or not view.ncols:
            self.notice = " no column to mark "
            return
        label = view.labels()[view.col]
        if view.col in view.marks:
            view.marks.remove(view.col)
            self.notice = f" {label} unmarked "
        else:
            view.marks.append(view.col)
            self.notice = f" {label} marked: join column {len(view.marks)} "

    def layout(self, height: int, width: int
               ) -> dict[int, tuple[int, int, int, int]] | None:
        """``(height, width, y, x)`` of each region drawn, or ``None`` when
        the screen is too small for all three."""
        if self.zoomed:
            return {self.focus: (height, width, 0, 0)}
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            return None
        top, left = height // 2, width // 2
        return {0: (top, left, 0, 0), 1: (top, width - left, 0, left),
                2: (height - top, width, top, 0)}

    def handle_key(self, key: int | str, stdscr, height: int,
                   width: int) -> bool:
        """Apply one key; False when it closes the screen."""
        view = self.views[self.focus]
        view.notice = ""
        if key in TAB_KEYS or key in BACK_TAB_KEYS:
            self.focus = (self.focus + (1 if key in TAB_KEYS else -1)) % 3
        elif key == ord("z"):
            self.zoomed = not self.zoomed
            self.notice = (f" {NAMES[self.focus]} on the whole screen; z "
                           f"again shows all three " if self.zoomed else "")
        elif key == ord("o"):
            self.keep = _next(tablejoin.KEEPS, self.keep)
            self.notice = f" rows kept: {tablejoin.KEEP_NAMES[self.keep]} "
        elif key == ord("e"):
            self.rule = _next(tablejoin.RULES, self.rule)
            self.notice = f" match: {tablejoin.RULE_NAMES[self.rule]} "
        elif key in HELP_KEYS:
            dialogs.message(stdscr, "Join files", HELP)
            curses.curs_set(0)
        elif key == ord("m"):
            if self.focus == 2:
                self.notice = (" the join columns are marked in File A and "
                               "File B: Tab to one, then m ")
            else:
                self._mark(view)
        elif key != curses.KEY_RESIZE:
            size = (self.layout(height, width) or {}).get(self.focus)
            rows, columns = size[:2] if size else (height, width)
            if not view.handle_key(key, stdscr, rows, columns):
                return False
        self.sync()
        if self.notice:
            self.views[self.focus].notice = self.notice
            self.notice = ""
        return True

    # -- drawing ----------------------------------------------------------------
    def _window(self, index: int, geometry: tuple[int, int, int, int]):
        cached = self._windows.get(index)
        if cached is None or cached[0] != geometry:
            cached = self._windows[index] = (geometry,
                                             curses.newwin(*geometry))
        return cached[1]

    def draw(self, frame) -> None:
        """Draw every region shown; ``frame`` is the whole screen, which
        says so when it is too small for all three."""
        height, width = frame.getmaxyx()
        theme.background(frame, "edit")
        frame.erase()
        regions = self.layout(height, width)
        if regions is None:
            lines = [f"Too small for File A, File B and the output: it "
                     f"needs {MIN_WIDTH}x{MIN_HEIGHT}.",
                     "z shows the region with the keyboard on its own, Tab "
                     "the next; q goes back."]
            for y, line in enumerate(lines[:height]):
                theme.paint(frame, y, 0, truncate(line, width), "edit")
        # Seen by curses as drawn, so reading a key from it does not paint
        # it over the regions.
        frame.noutrefresh()
        for index, geometry in (regions or {}).items():
            view = self.views[index]
            view.active = index == self.focus
            view.draw(self._window(index, geometry))

    def run(self, stdscr) -> None:
        curses.curs_set(0)
        height, width = stdscr.getmaxyx()
        frame = curses.newwin(height, width, 0, 0)
        frame.keypad(True)
        while True:
            if frame.getmaxyx() != (height, width):
                frame.resize(height, width)      # the terminal was resized
            self.draw(frame)
            curses.doupdate()
            height, width = stdscr.getmaxyx()
            if not self.handle_key(read_key(frame), stdscr, height, width):
                return

