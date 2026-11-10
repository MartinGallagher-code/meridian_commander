# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Delimited text tables -- TSV, CSV and their cousins -- read for browsing.

This is the model behind :mod:`meridian_commander.tableview`: reading a file
into records, bounded, and the view layer over them -- which rows a stack of
filters keeps, the order a stack of sort keys puts them in, and the display
widths that line the columns up.  Nothing here draws, so all of it is tested
without a screen.

There are two ways of reading, and the file's name chooses between them:

- ``.tsv``, ``.tab`` and ``.psv`` are split on their delimiter and nothing
  else.  IANA's ``text/tab-separated-values`` has no quoting, and the files
  the owner's tools write have none.  Read through :mod:`csv`, one cell that
  starts with ``"`` swallows tabs and newlines up to the next quote, so a
  file of a thousand rows can come out as three.
- ``.csv`` goes through :mod:`csv`, so a quoted comma or line break stays in
  its cell.  Its delimiter is taken from the first line, because a "CSV" a
  European spreadsheet exports is as often separated by semicolons.

Reads are bounded twice over -- :data:`MAX_BYTES` and :data:`MAX_ROWS` -- and
stop at a row boundary, so a huge results file opens quickly, marked as
truncated, rather than hanging the interface while it downloads.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .sheetview import looks_numeric
from .vt import char_width

#: The names the table browser claims.
TABLE_SUFFIXES = (".tsv", ".tab", ".psv", ".csv")

# The same row limit as the spreadsheet grid, and a byte limit low enough to
# stay quick over SFTP.  Read at call time, so the tests can lower them.
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 50_000
CHUNK = 64 * 1024
#: Comment lines are looked for among this many lines at the top of a file.
PREAMBLE_LINES = 1000

#: Suffixes whose delimiter is known and which are split without quoting.
_SPLIT = {".tsv": "\t", ".tab": "\t", ".psv": "|"}
#: What a ``.csv`` may turn out to be separated by, in order of preference
#: when the first line has as many of one as of another.
_CANDIDATES = (",", ";", "\t", "|")
DELIMITER_NAMES = {"\t": "tab", ",": "comma", ";": "semicolon", "|": "pipe"}

# A control character in a cell would move the cursor or end the line when
# drawn, so in the grid each one is shown as a space.  The C1 range is
# included: some terminals act on those too.
_CONTROLS = dict.fromkeys([*range(32), *range(127, 160)], " ")


def is_table(name: str) -> bool:
    """True if ``name`` is a delimited text file the table browser opens."""
    return name.lower().endswith(TABLE_SUFFIXES)


# -- reading -------------------------------------------------------------------
@dataclass
class Table:
    """Every record of a file, the first line included, and how it was read."""

    records: list[list[str]]
    delimiter: str
    quoted: bool = False        # read through the csv module
    truncated: bool = False
    #: The comment lines set aside from the top of the file.
    comments: list[str] = field(default_factory=list)

    @property
    def delimiter_name(self) -> str:
        return DELIMITER_NAMES.get(self.delimiter, repr(self.delimiter))

    @property
    def ncols(self) -> int:
        return max((len(record) for record in self.records), default=0)


def _close(stream) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            # A file that was read in full is not made unreadable by a
            # backend complaining as the handle goes away.
            pass


def read_bytes(fs, path: str) -> tuple[bytes, bool]:
    """The front of ``path``, and whether the file goes on past it.

    Reading stops at :data:`MAX_BYTES`, or as soon as there are more lines in
    hand than the browser will keep -- which for a file of short rows is long
    before the byte limit.  Only the backend's ``read(n)`` is used, so this
    works on every pane: local, SFTP, SSH, FTP and inside an archive.
    """
    max_bytes = MAX_BYTES
    # The header, every row kept, and room for comment lines above them.
    max_lines = MAX_ROWS + 1 + PREAMBLE_LINES
    stream = fs.open_read(path)
    chunks: list[bytes] = []
    total = newlines = 0
    more = False
    try:
        while total < max_bytes and newlines <= max_lines:
            chunk = stream.read(min(CHUNK, max_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            newlines += chunk.count(b"\n")
        else:
            # Stopped before the end of the file was seen: one byte more says
            # whether there is anything being left behind.
            more = bool(stream.read(1))
    finally:
        _close(stream)
    data = b"".join(chunks)
    if more:
        # Whatever follows the last line break is the front of a line, not a
        # line, so it goes rather than being shown as a short row.
        data = data[:data.rfind(b"\n") + 1]
    return data, more


def sniff_delimiter(text: str) -> str:
    """The delimiter of a ``.csv``: whichever candidate the first line has most of.

    The first line is usually the header, which is the line least likely to
    hold a stray delimiter inside a value.  Comment lines above it are passed
    over, being prose.  Ties go to the comma.
    """
    lines = [line for line in text[:CHUNK].split("\n") if line.strip()]
    first = next((line for line in lines if not line.startswith("#")),
                 lines[0] if lines else "")
    return max(_CANDIDATES, key=first.count)


def parse(text: str, delimiter: str, quoted: bool) -> list[list[str]]:
    """``text`` as a list of records.

    A blank line is one empty field -- in a one-column file that is exactly
    what it means -- and the line break that ends the last line does not make
    a record of its own.
    """
    if quoted:
        # A NUL stops the csv module on older Pythons; it is no more readable
        # as text than an invalid byte, so it is replaced the same way.
        reader = csv.reader(io.StringIO(text.replace("\x00", "�")),
                            delimiter=delimiter)
        return [record or [""] for record in reader]
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [(line[:-1] if line.endswith("\r") else line).split(delimiter)
            for line in lines]


def split_preamble(records: list[list[str]]
                   ) -> tuple[list[list[str]], list[list[str]]]:
    """The comment lines at the top of a file, and the records after them.

    The suite's own tools open a results file with ``#`` lines saying which
    run it came from.  Read as the table, the first of them became the
    header and every row was marked as having the wrong number of fields.
    So blank lines and lines starting with ``#`` at the top are set aside --
    except a last ``#`` line that splits into as many fields as the line
    after it, which is a header written as a comment (VCF's ``#CHROM``,
    BED's ``#chrom``) and stays the header.
    """
    skip = 0
    limit = min(len(records), PREAMBLE_LINES)
    while skip < limit and (records[skip] == [""]
                            or records[skip][0].startswith("#")):
        skip += 1
    if 0 < skip < len(records) and records[skip - 1] != [""] \
            and len(records[skip - 1]) == len(records[skip]) > 1:
        skip -= 1
    return records[:skip], records[skip:]


def read_table(fs, path: str) -> Table:
    """Read ``path`` for the browser.  Raises if the backend or the file fails."""
    data, more = read_bytes(fs, path)
    # A spreadsheet exporting UTF-8 writes a byte-order mark; left in, the
    # first column would be called "﻿id" and no filter on "id" would find
    # it.
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    text = data.decode("utf-8", errors="replace")
    name = fs.basename(path).lower()
    delimiter = next((d for s, d in _SPLIT.items() if name.endswith(s)), None)
    quoted = delimiter is None
    if delimiter is None:
        delimiter = sniff_delimiter(text)
    try:
        records = parse(text, delimiter, quoted)
    except csv.Error as exc:     # a cell past csv's field size limit, say
        raise ValueError(f"not readable as CSV: {exc}") from exc
    if more and quoted and records:
        # The cut was made at a line break, and in CSV a line break can sit
        # inside a quoted cell: the last record may be the front of one.
        records.pop()
    preamble, records = split_preamble(records)
    limit = MAX_ROWS + 1
    truncated = more or len(records) > limit
    return Table(records[:limit], delimiter, quoted, truncated,
                 [delimiter.join(record) for record in preamble
                  if record != [""]])


# -- display widths ------------------------------------------------------------
def clean(text: str) -> str:
    """``text`` with every control character shown as a space."""
    return text.translate(_CONTROLS)


def display_width(text: str, limit: int | None = None) -> int:
    """How many terminal columns ``text`` takes; CJK and emoji count two.

    With ``limit``, counting stops as soon as the total passes it -- all a
    caller asking "does it fit?" needs, and a 10 MB cell then costs nothing.
    """
    total = 0
    for ch in text:
        total += char_width(ch)
        if limit is not None and total > limit:
            break
    return total


def fit(text: str, width: int, right: bool = False) -> str:
    """``text`` padded or cut to exactly ``width`` display columns.

    A cut is marked with ``~``, as :func:`~meridian_commander.util.truncate`
    marks one; a wide character that would straddle the edge is left out and
    its column padded, so the next column still starts where it should.
    """
    if width <= 0:
        return ""
    full = display_width(text, width)
    if full <= width:
        pad = " " * (width - full)
        return pad + text if right else text + pad
    kept: list[str] = []
    used = 0
    for ch in text:
        step = char_width(ch)
        if used + step > width - 1:
            break
        kept.append(ch)
        used += step
    return "".join(kept) + "~" + " " * (width - 1 - used)


def wrap(text: str, width: int) -> list[str]:
    """``text`` broken into lines of at most ``width`` display columns.

    For the record view, which shows a value whole: a line break in the value
    (a quoted CSV cell can hold one) starts a new line, and a long line is cut
    wherever it reaches the edge.  Words are not kept together -- the point is
    to show every character, and a 2 KB stack trace has few spaces to keep.
    """
    lines: list[str] = []
    for piece in text.replace("\r\n", "\n").split("\n"):
        current: list[str] = []
        used = 0
        for ch in clean(piece):
            step = char_width(ch)
            if used + step > width and current:
                lines.append("".join(current))
                current, used = [], 0
            current.append(ch)
            used += step
        lines.append("".join(current))
    return lines


def decimals(text: str) -> int:
    """Columns from the decimal point to the end of a number, point included.

    ``"10.25"`` has 3, ``"7"`` has none.  Padding each number in a column out
    to the column's most is what lines the points up.  A number written with
    an exponent has no point to line up, so it counts none either.
    """
    text = text.strip()
    if "e" in text.lower() or "." not in text:
        return 0
    return len(text) - text.index(".")


def number(text: str) -> float | None:
    """``text`` as a number, or ``None`` when it is not one."""
    return float(text) if looks_numeric(text) else None


def is_numeric(values: Iterable[str]) -> bool:
    """True when every non-empty value is a number, and there is at least one."""
    seen = False
    for value in values:
        if not value.strip():
            continue
        if not looks_numeric(value):
            return False
        seen = True
    return seen


# -- filters -------------------------------------------------------------------
# Longest first at each position, so "<=" is not read as "<" followed by "=".
_OPERATOR = re.compile(r"!=|<=|>=|!~|=|<|>|~")
_ORDERS = {
    "<": lambda a, b: a < b, ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
}


def cell(record: Sequence[str], column: int) -> str:
    """One field of a record; a field a short row does not have is empty."""
    return record[column] if column < len(record) else ""


class Filter:
    """One condition a row must meet to stay in view.

    ``column`` is ``None`` for free text, which keeps a row if any cell holds
    it.  Matching follows the find rule everywhere in the program: a value in
    lower case matches regardless of case, and one with a capital in it
    matches exactly.  ``exact`` is the quick filter's comparison, which takes
    the cell's value as it is, case and spaces included.
    """

    def __init__(self, text: str, column: int | None, op: str, value: str,
                 exact: bool = False) -> None:
        self.text = text
        self.column = column
        self.op = op
        self.value = value
        self.exact = exact
        self._fold = value == value.lower()
        self._number = None if exact else number(value)

    def __repr__(self) -> str:
        return f"Filter({self.text!r})"

    def keeps(self, record: Sequence[str]) -> bool:
        if self.column is None:
            return any(self._contains(text) for text in record)
        text = cell(record, self.column)
        if self.op == "~":
            return self._contains(text)
        if self.op == "!~":
            return not self._contains(text)
        if self.op == "=":
            return self._equals(text)
        if self.op == "!=":
            return not self._equals(text)
        return self._orders(text)

    def _contains(self, text: str) -> bool:
        if self._fold:
            return self.value in text.lower()
        return self.value in text

    def _equals(self, text: str) -> bool:
        if self.exact:
            return text == self.value
        if self._number is not None and looks_numeric(text):
            return float(text) == self._number       # "1.0" = "1"
        text = text.strip()
        return (text.lower() if self._fold else text) == self.value

    def _orders(self, text: str) -> bool:
        compare = _ORDERS[self.op]
        if self._number is not None:
            # A cell that is not a number never meets a numeric comparison:
            # "gbps < 10" should not keep the rows where gbps is blank.
            value = number(text)
            return value is not None and compare(value, self._number)
        text = text.strip()
        return compare(text.lower() if self._fold else text, self.value)


def find_column(name: str, labels: Sequence[str]) -> int | None:
    """The column called ``name``: exactly if one is, else ignoring case."""
    if name in labels:
        return list(labels).index(name)
    low = name.lower()
    for index, label in enumerate(labels):
        if label.lower() == low:
            return index
    return None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _shown(value: str) -> str:
    """A value as the title shows it: quoted if its spaces would be lost."""
    return value if value and value == value.strip() else f'"{value}"'


def parse_filter(expression: str, labels: Sequence[str]) -> Filter:
    """A :class:`Filter` from what was typed at the prompt.

    ``column op value`` compares one column, with ``= != < > <= >=``, ``~``
    (contains) and ``!~`` (does not).  A value may be quoted to keep its
    spaces.  Anything else is free text, matched against every cell.  Raises
    :class:`ValueError` naming the problem when a column is named that the
    table does not have -- a typo would otherwise filter every row away
    without saying why.
    """
    expression = expression.strip()
    found = _OPERATOR.search(expression)
    name = expression[:found.start()].strip() if found else ""
    if not found or not name:
        return Filter(expression, None, "~", expression)
    column = find_column(name, labels)
    if column is None:
        raise ValueError(f"no column called '{name}'")
    op = found.group()
    value = _unquote(expression[found.end():].strip())
    return Filter(f"{labels[column]} {op} {_shown(value)}", column, op, value)


def quick_filter(label: str, column: int, value: str, keep: bool) -> Filter:
    """Keep (or hide) the rows whose ``column`` holds exactly ``value``."""
    op = "=" if keep else "!="
    return Filter(f"{label} {op} {_shown(value)}", column, op, value,
                  exact=True)


# -- sorting -------------------------------------------------------------------
@dataclass
class SortKey:
    column: int
    descending: bool = False


def sort_rows(rows: Sequence[int], records: Sequence[Sequence[str]],
              keys: Sequence[SortKey]) -> list[int]:
    """``rows`` (indices into ``records``) in the order ``keys`` put them.

    The first key decides and each later one breaks its ties, which is done
    the usual way: a stable sort by the last key first.  A column whose every
    value is a number sorts as numbers -- 9 before 10 -- and anything else as
    text, ignoring case.  Empty cells go last whichever way the column runs,
    since a blank is never the answer to "which is biggest".
    """
    out = list(rows)
    for key in reversed(keys):
        values = {index: cell(records[index], key.column) for index in out}
        filled = [index for index in out if values[index].strip()]
        empty = [index for index in out if not values[index].strip()]
        if is_numeric(values[index] for index in filled):
            filled.sort(key=lambda index: float(values[index]),
                        reverse=key.descending)
        else:
            filled.sort(key=lambda index: values[index].casefold(),
                        reverse=key.descending)
        out = filled + empty
    return out


@dataclass
class View:
    """Which records are shown, and in what order: the filters and sort keys.

    The records never change.  Filtering and sorting rebuild a list of
    indices into them, once per change rather than once per keystroke, so a
    file of fifty thousand rows stays quick to browse.
    """

    filters: list[Filter] = field(default_factory=list)
    keys: list[SortKey] = field(default_factory=list)

    def rows(self, records: Sequence[Sequence[str]], first: int) -> list[int]:
        """The indices of the records shown, from ``first`` on, in order."""
        kept = [index for index in range(first, len(records))
                if all(f.keeps(records[index]) for f in self.filters)]
        return sort_rows(kept, records, self.keys) if self.keys else kept

    def sort_by(self, column: int) -> None:
        """Ascending, then descending, then not sorted by ``column``.

        Sorting by one column and then another sorts by both, the newer
        first and the older breaking its ties, as a spreadsheet does.
        """
        if self.keys and self.keys[0].column == column:
            if self.keys[0].descending:
                self.keys.pop(0)
            else:
                self.keys[0].descending = True
            return
        self.keys = [SortKey(column)] + [k for k in self.keys
                                         if k.column != column]

    def direction(self, column: int) -> bool | None:
        """``True`` descending, ``False`` ascending, ``None`` not sorted."""
        for key in self.keys:
            if key.column == column:
                return key.descending
        return None
