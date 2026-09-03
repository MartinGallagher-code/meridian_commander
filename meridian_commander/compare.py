"""Two files side by side, aligned line for line and scrolled as one.

The pane on the left of the screen shows the file picked in the left pane and
the one on the right shows the right pane's, so what you see matches where the
files came from.  Alignment is :mod:`difflib`'s, turned into one row per
*pair* of lines: a row holds a line from each side, or a line from one side
and a gap opposite it.  Scrolling then needs no synchronising, because there
is only one thing to scroll -- the row list.  Two independently scrolled
panes would drift the moment the files differed in length, which is exactly
when a comparison is worth looking at.

It is deliberately internal rather than a call out to ``vimdiff``: the panes
read through :class:`~meridian_commander.filesystems.FileSystem`, so a local
file compares against an SFTP, SSH, FTP or in-archive one without either
being fetched to disk first, and the result is drawn in the same palette as
the rest of the application.
"""

from __future__ import annotations

import curses
import difflib
from dataclasses import dataclass

from . import theme
from .filesystems import FileSystem

MAX_COMPARE_BYTES = 8 * 1024 * 1024  # 8 MiB per side

#: How each kind of row is painted, as (left role, right role).  A side with
#: no line of its own gets ``difffill``, which is a shaded gap rather than an
#: empty line: "there is nothing here" and "there is an empty line here" are
#: different answers and must not look the same.
ROW_ROLES: dict[str, tuple[str, str]] = {
    "same": ("edit", "edit"),
    "change": ("diffchange", "diffchange"),
    "del": ("diffdel", "difffill"),
    "add": ("difffill", "diffadd"),
}

#: The marker column between the line number and the text.
MARKERS = {"same": " ", "change": "!", "del": "-", "add": "+"}


@dataclass
class Row:
    """One screen row: a line index per side, or ``None`` for a gap."""

    left: int | None
    right: int | None
    kind: str

    @property
    def differs(self) -> bool:
        return self.kind != "same"


def load_lines(fs: FileSystem, path: str,
               cap: int = MAX_COMPARE_BYTES) -> tuple[list[str], bool]:
    """Read ``path`` as text lines; also says whether the cap cut it short."""
    reader = fs.open_read(path)
    try:
        data = reader.read(cap + 1)
    finally:
        reader.close()
    truncated = len(data) > cap
    if truncated:
        data = data[:cap]
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    lines = text.split("\n")
    # A trailing newline ends the last line; it does not start an empty one.
    if lines and lines[-1] == "":
        lines.pop()
    return lines, truncated


def align(left: list[str], right: list[str]) -> list[Row]:
    """Pair the two files up into rows, gaps included."""
    rows: list[Row] = []
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            rows += [Row(i, j, "same")
                     for i, j in zip(range(i1, i2), range(j1, j2))]
        elif tag == "delete":
            rows += [Row(i, None, "del") for i in range(i1, i2)]
        elif tag == "insert":
            rows += [Row(None, j, "add") for j in range(j1, j2)]
        else:  # replace: rewritten lines line up, the surplus hangs below
            for step in range(max(i2 - i1, j2 - j1)):
                i = i1 + step if i1 + step < i2 else None
                j = j1 + step if j1 + step < j2 else None
                if i is not None and j is not None:
                    kind = "change"
                else:
                    kind = "del" if j is None else "add"
                rows.append(Row(i, j, kind))
    return rows


def hunk_starts(rows: list[Row]) -> list[int]:
    """The first row of each run of differing rows.

    Navigation jumps between *blocks*, not rows: a rewritten paragraph is one
    difference to look at, and ``n`` pressed ten times to cross it would be a
    worse tool than the one the user already has.
    """
    starts = []
    for index, row in enumerate(rows):
        if row.differs and (index == 0 or not rows[index - 1].differs):
            starts.append(index)
    return starts


class Comparison:
    """The side-by-side view of two files."""

    def __init__(self, left_fs: FileSystem, left_path: str,
                 right_fs: FileSystem, right_path: str) -> None:
        self.left_fs, self.left_path = left_fs, left_path
        self.right_fs, self.right_path = right_fs, right_path
        self.left_name = left_fs.basename(left_path)
        self.right_name = right_fs.basename(right_path)
        self.top = 0
        self.col = 0
        self.show_line_numbers = True
        self.error: str | None = None
        self.truncated = False
        self.notice = ""
        self.left_lines: list[str] = []
        self.right_lines: list[str] = []
        self.rows: list[Row] = []
        self.hunks: list[int] = []
        self.cur_hunk: int | None = None
        self._load()

    def _load(self) -> None:
        try:
            self.left_lines, cut_left = load_lines(self.left_fs, self.left_path)
            self.right_lines, cut_right = load_lines(self.right_fs,
                                                     self.right_path)
        except Exception as exc:
            self.error = str(exc)
            return
        self.truncated = cut_left or cut_right
        self.rows = align(self.left_lines, self.right_lines)
        self.hunks = hunk_starts(self.rows)

    # -- state ------------------------------------------------------------
    @property
    def identical(self) -> bool:
        return not self.hunks and self.error is None

    def text_of(self, row: Row, side: str) -> str:
        """The line one side of ``row`` shows, or "" where it has a gap."""
        index = row.left if side == "left" else row.right
        if index is None:
            return ""
        lines = self.left_lines if side == "left" else self.right_lines
        return lines[index] if index < len(lines) else ""

    def next_hunk(self, direction: int) -> int | None:
        """Move to the next (or previous) block of differences, with wrap."""
        if not self.hunks:
            self.notice = "no differences"
            return None
        if direction > 0:
            later = [h for h in self.hunks if h > self.top]
            target = later[0] if later else self.hunks[0]
        else:
            earlier = [h for h in self.hunks if h < self.top]
            target = earlier[-1] if earlier else self.hunks[-1]
        wrapped = (target - self.top) * direction < 0
        self.top = target
        self.cur_hunk = self.hunks.index(target)
        self.notice = (f"difference {self.cur_hunk + 1}/{len(self.hunks)}"
                       + (" (wrapped)" if wrapped else ""))
        return target

    def _scroll(self, delta: int) -> None:
        self.notice = ""
        self.top = max(0, min(self.top + delta, max(0, len(self.rows) - 1)))

    # -- rendering --------------------------------------------------------
    def draw(self, win) -> None:
        theme.background(win, "edit")
        win.erase()
        height, width = win.getmaxyx()
        body_h = height - 2
        half = (width - 1) // 2          # one column between the two sides

        self._draw_header(win, width, half)
        if self.error:
            theme.paint(win, 2, 2, f"Cannot compare: {self.error}"[:width - 4],
                        "panelerror")
            self._draw_footer(win, height, width)
            win.noutrefresh()
            return

        gutter = self._gutter()
        for offset in range(body_h):
            index = self.top + offset
            if index >= len(self.rows):
                break
            row = self.rows[index]
            y = offset + 1
            left_role, right_role = ROW_ROLES[row.kind]
            self._draw_side(win, y, 0, half, row, "left", left_role, gutter)
            theme.paint(win, y, half, theme.glyph("v1"), "framenc")
            self._draw_side(win, y, half + 1, width - half - 1, row, "right",
                            right_role, gutter)

        self._draw_footer(win, height, width)
        win.noutrefresh()

    def _gutter(self) -> int:
        if not self.show_line_numbers:
            return 0
        longest = max(len(self.left_lines), len(self.right_lines), 1)
        return len(str(longest)) + 1

    def _draw_header(self, win, width: int, half: int) -> None:
        left = f" {self.left_name} "
        right = f" {self.right_name} "
        if self.truncated:
            right += "[truncated] "
        theme.paint(win, 0, 0, "", "keybar", width)
        theme.paint(win, 0, 0, left[:half], "keybar")
        theme.paint(win, 0, half + 1, right[:width - half - 1], "keybar")

    def _draw_side(self, win, y: int, x: int, width: int, row: Row, side: str,
                   role: str, gutter: int) -> None:
        if width <= 0:
            return
        index = row.left if side == "left" else row.right
        if index is None:
            # The gap opposite an added or deleted line: shaded, not blank.
            theme.paint(win, y, x, "", "difffill", width)
            return
        if gutter:
            theme.paint(win, y, x, str(index + 1).rjust(gutter - 1) + " ",
                        "editnum")
        marker = MARKERS[row.kind]
        text = self.text_of(row, side)[self.col:]
        theme.paint(win, y, x + gutter, marker + text, role, width - gutter)

    def _draw_footer(self, win, height: int, width: int) -> None:
        parts = [f"row {min(self.top + 1, max(len(self.rows), 1))}"
                 f"/{max(len(self.rows), 1)}"]
        if self.error is None:
            parts.append("identical" if self.identical
                         else f"{len(self.hunks)} difference(s)")
        if self.notice:
            parts.append(self.notice)
        parts.append("n/N next/prev diff  [l]#  arrows scroll  [q]uit")
        theme.paint(win, height - 1, 0, " " + "  ".join(parts) + " ", "keybar",
                    width)

    # -- interaction ------------------------------------------------------
    def run(self, stdscr) -> None:
        curses.curs_set(0)
        height, width = stdscr.getmaxyx()
        win = curses.newwin(height, width, 0, 0)
        win.keypad(True)
        while True:
            self.draw(win)
            curses.doupdate()
            body_h = stdscr.getmaxyx()[0] - 2
            key = win.getch()
            if key in (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10):
                break
            elif key in (curses.KEY_DOWN, ord("j")):
                self._scroll(1)
            elif key in (curses.KEY_UP, ord("k")):
                self._scroll(-1)
            elif key in (curses.KEY_NPAGE, ord(" ")):
                self._scroll(body_h)
            elif key == curses.KEY_PPAGE:
                self._scroll(-body_h)
            elif key == curses.KEY_HOME:
                self.top = 0
            elif key == curses.KEY_END:
                self.top = max(0, len(self.rows) - body_h)
            elif key == curses.KEY_LEFT:
                self.col = max(0, self.col - 8)
            elif key == curses.KEY_RIGHT:
                self.col += 8
            elif key == ord("n"):
                self.next_hunk(1)
            elif key in (ord("N"), ord("p")):
                self.next_hunk(-1)
            elif key in (ord("l"), ord("L")):
                self.show_line_numbers = not self.show_line_numbers
