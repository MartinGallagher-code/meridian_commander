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

from . import theme
from .filesystems import FileSystem

MAX_VIEW_BYTES = 16 * 1024 * 1024  # 16 MiB safety cap

#: What each style code means on screen, as a theme role.  Going through the
#: theme rather than naming attributes here is what keeps a rendered document
#: in the same palette as everything else -- and it still degrades to a plain
#: monochrome attribute on a terminal that has no colour to give.
STYLE_ROLES = {
    "B": "editbold",     # **strong**
    "I": "editem",       # *emphasis*
    "C": "editcode",     # `code`
    "D": "editdim",      # rules, quotes, URLs
    "U": "editlink",     # link text
}


def style_attr(code: str) -> int:
    """The curses attribute for a style code, or the plain text one."""
    role = STYLE_ROLES.get(code)
    return theme.attr(role) if role else theme.attr("edit")


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
        #: One style code per character, per source line, or "" for a line
        #: that is all plain.  Empty overall for an ordinary text file, which
        #: keeps that case exactly as cheap as it was before styling existed.
        self.styles: list[str] = []
        #: Source lines that must not be wrapped -- a table or a code block
        #: is a shape, and reflowing it destroys the thing being shown.
        self.nowrap: set[int] = set()
        #: The file's own lines.  Searching and line numbering work in this
        #: space, so a wrapped paragraph stays one line to jump to and keeps
        #: one number however many screen rows it happens to occupy.
        self.source = self._load()
        self._flow: tuple[bool, int] | None = None
        self._reflow(0)

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

    # -- line flow --------------------------------------------------------
    def _style_of(self, index: int) -> str:
        return self.styles[index] if index < len(self.styles) else ""

    def _row_style(self, row: int) -> str:
        return self.row_styles[row] if row < len(self.row_styles) else ""

    def _reflow(self, width: int) -> None:
        """Rebuild the display lines for a text area ``width`` columns wide.

        With wrapping off this is the file's lines unchanged, so everything
        downstream behaves exactly as it did before wrapping existed.
        """
        width = max(1, width)
        if self._flow == (self.wrap, width):
            return
        self._flow = (self.wrap, width)
        if not self.wrap:
            self.lines = self.source
            self.row_styles = self.styles
            # Each source line is its own single display row.
            self.first_row = list(range(len(self.source)))
            self.origin = list(range(len(self.source)))
            return
        styled = bool(self.styles)
        lines: list[str] = []
        styles: list[str] = []
        origin: list[int] = []
        first: list[int] = []
        for index, line in enumerate(self.source):
            first.append(len(lines))
            style = self._style_of(index)
            if index in self.nowrap or len(line) <= width:
                lines.append(line)
                origin.append(index)
                if styled:
                    styles.append(style)
                continue
            for start, end in wrap_spans(line, width):
                lines.append(line[start:end])
                origin.append(index)
                if styled:
                    # The style is cut at the same points as the text, which
                    # is the whole reason wrap_spans hands back indices.
                    styles.append(style[start:end])
        self.lines, self.origin, self.first_row = lines, origin, first
        self.row_styles = styles

    def toggle_wrap(self) -> None:
        """Turn wrapping on or off, keeping the cursor on the same line."""
        at = self.origin[self.top] if self.top < len(self.origin) else 0
        width = self._flow[1]
        self.wrap = not self.wrap
        self._flow = None
        self._reflow(width)
        self._jump_to(at)

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
        # Matched against the file's own line, not a display row: a phrase
        # broken across a wrap is still one match on one line.
        line = self.source[index]
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
        n = len(self.source)
        if n == 0:
            return None
        if self.cur_match is not None:
            base = self.cur_match
        else:
            # First jump after a new pattern: include the top visible line.
            base = self.origin[self.top] - direction
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
        """Scroll so source line ``index`` is visible, a couple of rows down."""
        row = self.first_row[index] if 0 <= index < len(self.first_row) else 0
        self.top = max(0, min(row - 2, max(0, len(self.lines) - 1)))

    # -- rendering --------------------------------------------------------
    def draw(self, win) -> None:
        theme.background(win, "edit")
        win.erase()
        height, width = win.getmaxyx()
        body_h = height - 2

        title = self._title()
        if self.truncated:
            title += "[truncated] "
        theme.paint(win, 0, 0, title, "keybar", width)

        if self.error:
            theme.paint(win, 2, 2, f"Cannot open file: {self.error}"[: width - 4],
                        "panelerror")
            self._draw_footer(win, height, width)
            win.noutrefresh()
            return

        gutter = len(str(len(self.source))) + 1 if self.show_line_numbers else 0
        self._reflow(width - gutter)
        for row in range(body_h):
            idx = self.top + row
            if idx >= len(self.lines):
                break
            y = row + 1
            if self.show_line_numbers:
                # A wrapped line is numbered once, on its first row; the
                # continuations are left blank so the numbers stay countable.
                src = self.origin[idx]
                label = str(src + 1) if self.first_row[src] == idx else ""
                theme.paint(win, y, 0, label.rjust(gutter - 1) + " ", "editnum")
            line = self.lines[idx]
            visible = line[self.left : self.left + (width - gutter)]
            style = self._row_style(idx)[self.left : self.left + len(visible)]
            try:
                if style.strip():
                    self._draw_styled(win, y, gutter, visible, style)
                else:
                    win.addstr(y, gutter, visible, theme.attr("edit"))
            except curses.error:
                pass
            if self.search:
                self._highlight_matches(win, y, line, gutter, width)

        self._draw_footer(win, height, width)
        win.noutrefresh()

    def _title(self) -> str:
        """The title bar's text.  Subclasses say what they are showing."""
        return f" View: {self.name} "

    def _hints(self) -> str:
        """The key reminders in the footer."""
        return "[/]find  n/N next/prev  [l]#  [w]rap  [q]uit"

    def _other_key(self, key: int) -> None:
        """A key this viewer does not handle.  Subclasses may claim it."""

    def _draw_styled(self, win, y: int, x: int, text: str, style: str) -> None:
        """Draw one row as runs of equal style, one write per run."""
        style = style.ljust(len(text))
        start = 0
        for index in range(1, len(text) + 1):
            if index < len(text) and style[index] == style[start]:
                continue
            win.addstr(y, x + start, text[start:index], style_attr(style[start]))
            start = index

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
            theme.paint(win, y, sx, seg, "editmatch")

    def _draw_footer(self, win, height: int, width: int) -> None:
        # An unreadable file has no lines at all; the counter keeps its
        # old shape there rather than showing a zeroth line.
        shown = self.origin[self.top] + 1 if self.origin else 1
        pos = f"line {shown}/{len(self.source)}"
        parts = [pos]
        if self.search:
            parts.append(f"/{self.search}")
        if self.notice:
            parts.append(self.notice)
        parts.append(self._hints())
        hint = "  ".join(parts)
        theme.paint(win, height - 1, 0, f" {hint} ", "keybar", width)

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
                self.toggle_wrap()
            else:
                self._other_key(key)

    def _scroll(self, delta: int, body_h: int) -> None:
        self.notice = ""
        self.top = max(0, min(self.top + delta, max(0, len(self.lines) - 1)))


def wrap_spans(text: str, width: int) -> list[tuple[int, int]]:
    """Where to cut ``text`` into pieces no wider than ``width`` columns.

    Returns half-open ``(start, end)`` index pairs rather than the pieces
    themselves, so a caller holding something parallel to the text -- an
    attribute per character, say -- can cut that the same way and stay in
    step.  Breaks at a space where there is one, and mid-word only when a
    single word is wider than the screen: the alternative there is losing
    characters off the edge, which is the thing wrapping exists to stop.
    """
    spans = []
    length = len(text)
    start = 0
    while length - start > width:
        # Search from start + 1 so a leading space can never give a
        # zero-width piece, which would never finish.
        cut = text.rfind(" ", start + 1, start + width + 1)
        if cut <= start:
            cut = start + width
        end = cut
        while end > start and text[end - 1] == " ":
            end -= 1                       # no trailing space on a piece
        spans.append((start, end))
        start = cut
        while start < length and text[start] == " ":
            start += 1                     # nor a leading one on the next
    spans.append((start, length))
    return spans


def wrap_line(text: str, width: int) -> list[str]:
    """Break one line into pieces no wider than ``width`` columns."""
    if len(text) <= width:
        return [text]
    return [text[a:b] for a, b in wrap_spans(text, width)]
