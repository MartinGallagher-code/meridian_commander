"""The head and the tail of a file at once, with the middle folded away.

What you usually want from a log or a data file is both ends: the header row
or the start-up banner, and whatever happened last.  ``head`` and ``tail`` give
you one each; this gives you both in one screen, with a rule across the middle
saying how many lines were skipped between them.

The file is read once, streaming, keeping only the first ``n`` lines and a
rolling window of the last ``n``.  A multi-gigabyte log therefore costs the
same memory as a short one -- and because it reads through the pane's
filesystem, a log on an SFTP or SSH pane peeks exactly like a local one.
"""

from __future__ import annotations

from collections import deque

from .filesystems import FileSystem
from .viewer import Viewer

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


class PeekViewer(Viewer):
    """A :class:`~meridian_commander.viewer.Viewer` over both ends of a file.

    Everything the viewer does -- search, wrapping, line numbers, scrolling --
    works here unchanged, because what it is given is simply a shorter list of
    lines.  The numbers down the side count the rows shown, not the file's own
    lines; the two ends' real line numbers are on the rule between them, which
    is where the question "where am I in the file" actually gets answered.
    """

    def __init__(self, fs: FileSystem, path: str,
                 count: int = DEFAULT_LINES) -> None:
        self.count = max(1, count)
        self.total = 0
        self.omitted = 0
        super().__init__(fs, path)

    def _load(self) -> list[str]:
        try:
            head, tail, total = read_ends(self.fs, self.path, self.count)
        except Exception as exc:
            self.error = str(exc)
            return []
        self.total = total
        self.omitted = max(0, total - len(head) - len(tail))
        if self.omitted == 0:
            # The ends meet (or overlap): show the file itself rather than a
            # rule claiming nothing was skipped.
            overlap = len(head) + len(tail) - total
            lines = head + tail[max(0, overlap):]
            self.styles = []
            return lines
        rule = (f"---- {self.omitted} line(s) omitted "
                f"(lines {len(head) + 1}-{total - len(tail)}) ----")
        lines = head + [rule] + tail
        # Style the rule dim; the file's own lines stay plain.
        self.styles = [""] * len(head) + ["D" * len(rule)] + [""] * len(tail)
        return lines

    def _reload(self, count: int) -> None:
        self.count = max(1, count)
        self.error = None
        self.source = self._load()
        self._flow = None
        self._reflow(0)
        self.top = 0
        self.notice = f"first/last {self.count}"

    def _title(self) -> str:
        return (f" Peek: {self.name}  "
                f"(first/last {self.count} of {self.total} lines) ")

    def _hints(self) -> str:
        return "+/- more/less  [/]find  [l]#  [w]rap  [q]uit"

    def _other_key(self, key: int) -> None:
        if key in (ord("+"), ord("=")):
            self._reload(self.count + STEP)
        elif key in (ord("-"), ord("_")):
            self._reload(self.count - STEP)
