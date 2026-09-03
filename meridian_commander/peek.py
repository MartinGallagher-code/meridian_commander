"""The head and the tail of a file at once, shown inside a pane.

What you usually want from a log or a data file is both ends: the header row
or the start-up banner, and whatever happened last.  ``head`` and ``tail`` give
you one each; this gives you both, with a rule across the middle saying how
many lines were skipped between them.

It lives **in the pane**, opposite the listing it is reading from, which is the
whole point: the other pane keeps its cursor, and moving that cursor changes
what this pane shows.  A full-screen window would have to be opened and closed
around every file, which is a worse tool than the listing it replaced -- the
question "what is in these files?" is asked of a directory, not of one file.

The file is read once per look, streaming, keeping only the first ``n`` lines
and a rolling window of the last ``n``.  A multi-gigabyte log therefore costs
the same memory as a short one -- and because it reads through the other pane's
filesystem, a log on an SFTP or SSH pane peeks exactly like a local one.
"""

from __future__ import annotations

import curses
from collections import deque

from . import theme
from .filesystems import FileSystem
from .plugin_api import PanePlugin

#: Lines shown at each end, and the step ``+``/``-`` moves that by.
DEFAULT_LINES = 20
STEP = 10

#: Read granularity, and the longest run of bytes accepted as a single line.
#: A file with no newline in it at all would otherwise be buffered whole,
#: which is the one shape that could defeat the streaming.
CHUNK = 64 * 1024
MAX_LINE_BYTES = 1024 * 1024


def read_ends(fs: FileSystem, path: str,
              count: int) -> tuple[list[str], list[str], int]:
    """Return ``(head, tail, total_lines)`` for ``path``.

    One pass, bounded memory: the head stops growing after ``count`` lines and
    the tail is a ``deque`` of the same length, so only the count keeps rising.
    """
    head: list[bytes] = []
    tail: "deque[bytes]" = deque(maxlen=max(1, count))
    total = 0
    buf = b""
    stream = fs.open_read(path)

    def keep(line: bytes) -> None:
        nonlocal total
        total += 1
        if len(head) < count:
            head.append(line)
        tail.append(line)

    try:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                pieces = buf.split(b"\n")
                buf = pieces.pop()
                for piece in pieces:
                    keep(piece)
            while len(buf) > MAX_LINE_BYTES:
                # A "line" longer than the cap is cut rather than buffered on;
                # the alternative is holding the whole file for one long line.
                keep(buf[:MAX_LINE_BYTES])
                buf = buf[MAX_LINE_BYTES:]
    finally:
        try:
            stream.close()
        except Exception:
            pass
    if buf:
        keep(buf)                    # a last line with no newline after it
    return ([_text(line) for line in head],
            [_text(line) for line in tail],
            total)


def _text(line: bytes) -> str:
    return line.decode("utf-8", errors="replace").rstrip("\r").expandtabs(4)


def fold(head: list[str], tail: list[str], total: int) -> tuple[list[str], int]:
    """The two ends as one list of rows, and how many lines were skipped.

    When the ends meet or overlap the file is shown whole: a rule claiming
    nothing was skipped would be a line of furniture standing in for nothing.
    """
    omitted = total - len(head) - len(tail)
    if omitted <= 0:
        overlap = len(head) + len(tail) - total
        return head + tail[max(0, overlap):], 0
    rule = (f"---- {omitted} line(s) omitted "
            f"(lines {len(head) + 1}-{total - len(tail)}) ----")
    return head + [rule] + tail, omitted


class PeekPane(PanePlugin):
    """A pane showing both ends of whatever the *other* pane points at."""

    name = "Head + tail"
    description = "Both ends of the other pane's file, without leaving here"

    def on_start(self) -> None:
        self.count = DEFAULT_LINES
        #: Whether the count follows the pane's height.  It does until ``+``
        #: or ``-`` says otherwise: the reason to show both ends in a pane is
        #: to see them *at once*, and a fixed 20 lines each end in a pane 20
        #: rows tall shows the head and half a rule.
        self.auto = True
        self.top = 0
        self.rows: list[str] = []
        self.rule = -1            # which row is the omitted-lines rule
        self.total = 0
        self.error: str | None = None
        self.name_shown = ""
        self._source: tuple[str, str] | None = None
        #: Body rows the last draw had, so a page key knows how far a page is.
        self._body = 1
        self.reload()

    # -- what is being looked at ------------------------------------------
    def target(self) -> str | None:
        """The other pane's file, or ``None`` when it is not pointing at one."""
        entry = self.ctx.other_panel.current()
        if entry is None or entry.is_dir or entry.name == "..":
            return None
        return self.ctx.other_fs.join(self.ctx.other_path, entry.name)

    def follow(self) -> None:
        """Re-read when the other pane's cursor has moved on to another file.

        Called from :meth:`draw`, which is the only place that knows the pane
        is being looked at.  The check is a tuple comparison; the file is read
        only when the answer changes, so holding the cursor down in the other
        pane does not re-read anything.
        """
        path = self.target()
        signature = (path or "", getattr(self.ctx.other_fs, "scheme", ""))
        if signature != self._source:
            self._source = signature
            self.reload()

    def reload(self) -> None:
        """Read both ends of the current target."""
        self.top = 0
        self.rows = []
        self.rule = -1
        self.total = 0
        self.error = None
        path = self.target()
        if path is None:
            self.name_shown = ""
            self.error = "no file under the other pane's cursor"
            return
        self.name_shown = self.ctx.other_fs.basename(path)
        try:
            head, tail, total = read_ends(self.ctx.other_fs, path, self.count)
        except Exception as exc:
            self.error = str(exc)
            return
        self.total = total
        rows, omitted = fold(head, tail, total)
        self.rows = rows
        self.rule = len(head) if omitted else -1

    def fit(self, body: int) -> int:
        """Lines at each end that make both ends land in ``body`` rows.

        One row goes to the rule between them, so the two ends get what is
        left, halved.  A pane too short to hold two lines and a rule still
        gets one line each end and scrolls -- something is better than a
        blank pane.
        """
        return max(1, (body - 1) // 2)

    def resize(self, count: int) -> None:
        """Pin the count to ``count``; the pane stops sizing itself."""
        self.auto = False
        self.count = max(1, count)
        self.reload()

    def scroll(self, delta: int, page: int) -> None:
        limit = max(0, len(self.rows) - max(1, page))
        self.top = max(0, min(self.top + delta, limit))

    # -- the pane ----------------------------------------------------------
    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        # The same layout as a listing: a header row, a body, a footer -- so
        # the two panes' rows line up and this reads as one of the pair.
        body = h - 2
        if body < 1:
            return
        self._body = body
        if self.auto and self.count != self.fit(body):
            # The pane knows its height only once it is drawn, so this is
            # where a pane sized to fit finds out what "fit" means.
            self.count = self.fit(body)
            self.reload()
        self.follow()
        title = f" [head+tail] {self.name_shown or 'nothing selected'} "
        self.put(stdscr, y, x, w, title, theme.attr("keybar"))

        if self.error:
            self.put(stdscr, y + 1, x, w, f" {self.error}"[:w],
                     theme.attr("panelerror"))
            for row in range(1, body):
                self.put(stdscr, y + 1 + row, x, w, "")
        else:
            for row in range(body):
                index = self.top + row
                text = self.rows[index] if index < len(self.rows) else ""
                attr = theme.attr("editdim") if index == self.rule else None
                self.put(stdscr, y + 1 + row, x, w, text, attr)

        self.put(stdscr, y + h - 1, x, w, self._footer(body),
                 theme.attr("keybar"))

    def _footer(self, body: int) -> str:
        if self.error:
            return " +/- lines   Esc/h close "
        how = "fitted" if self.auto else "pinned"
        shown = f"first/last {self.count} ({how}) of {self.total}"
        more = "" if len(self.rows) <= body else f"  row {self.top + 1}"
        return f" {shown}{more}   +/- lines   r reload   Esc/h close "

    def handle_key(self, key: int):
        page = max(1, self._body)
        if key in (27, ord("q"), ord("h"), curses.KEY_F10):
            return False                       # give the pane back
        if key in (curses.KEY_DOWN, ord("j")):
            self.scroll(1, page)
        elif key in (curses.KEY_UP, ord("k")):
            self.scroll(-1, page)
        elif key == curses.KEY_NPAGE:
            self.scroll(page, page)
        elif key == curses.KEY_PPAGE:
            self.scroll(-page, page)
        elif key == curses.KEY_HOME:
            self.top = 0
        elif key == curses.KEY_END:
            self.scroll(len(self.rows), page)
        elif key in (ord("+"), ord("=")):
            self.resize(self.count + STEP)
        elif key in (ord("-"), ord("_")):
            self.resize(self.count - STEP)
        elif key in (ord("r"), curses.KEY_F5):
            self.reload()
        else:
            return None                        # Tab and the rest are the app's
        return True
