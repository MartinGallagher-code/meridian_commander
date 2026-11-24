# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Two tables joined on columns picked in each: the model behind the join screen.

:mod:`meridian_commander.joinview` puts File A and File B side by side and the
rows they make together underneath.  This is the joining, with nothing drawn,
so it is tested without a screen, as :mod:`meridian_commander.table` is.

- **Every combination.**  A value found twice in A and three times in B gives
  six rows, in A's order and, within each A row, B's.  That is what "the row
  combinations" asks for; Build dataset's ``join <key>``, which keeps one B row
  per key, is a different tool and stays as it is.
- **The columns pair up in the order they were picked**: A's first with B's
  first, A's second with B's second, and a row matches only when every pair
  does.
- **Which rows are kept** is one of :data:`KEEPS`: the matches only, those and
  every A row with no match (its B side blank), or those and every unmatched
  row of either file.
- **How strict a match is** is one of :data:`RULES`: the text exactly, or
  ignoring upper and lower case and spaces at either end, or that and numbers
  by value, so ``7``, ``7.0`` and ``007`` are one value.
- **A blank key matches nothing.**  A row whose join cells are all empty is a
  row with no key, not a key shared with every other such row: in a results
  file the blanks are the commonest value, and matching them with each other
  multiplies them into rows nobody asked for.  Such a row is kept only where
  unmatched rows are, and counted, so the screen can say so.
- **Bounded.**  The output stops at :data:`~meridian_commander.table.MAX_ROWS`,
  the most the table browser holds, and says it was cut short.  The work done
  is bounded by the inputs and that limit, never by how many combinations
  there would have been: two columns of a thousand equal values would be a
  million rows, and the join stops at fifty thousand rather than making them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Hashable, Sequence

from . import table
from .sheetview import looks_numeric
from .table import cell

#: Which rows the output keeps, in the order the join screen's ``o`` steps
#: through them, and how the screen says each.
KEEPS = ("inner", "left", "full")
KEEP_NAMES = {
    "inner": "matching rows only",
    "left": "matching rows, and every row of A",
    "full": "matching rows, and every row of A and B",
}

#: How strict a match is, in the order ``e`` steps through them.
RULES = ("exact", "loose", "numbers")
RULE_NAMES = {
    "exact": "exact text",
    "loose": "any case, spaces trimmed",
    "numbers": "any case, spaces trimmed, numbers by value",
}


def match_value(text: str, rule: str) -> Hashable:
    """What a cell is compared by under ``rule``.

    ``numbers`` compares a number by its value, as a :class:`~decimal.Decimal`
    rather than a float: ``007`` and ``7.0`` are equal, but two twenty-digit
    identifiers that differ in their last digit are not, which a float would
    round into one.  A Decimal hashes as the number it is, so it can never be
    equal to the text of a cell that is not a number.  ``-nan`` is the one
    "number" that is not equal to itself, so it is compared as text, which
    is.
    """
    if rule == "exact":
        return text
    text = text.strip()
    if rule == "numbers" and looks_numeric(text):
        # Decimal reads everything float() does, which looks_numeric asked.
        value = Decimal(text)
        if not value.is_nan():
            return value
    return text.casefold()


def join_key(record: Sequence[str], columns: Sequence[int],
             rule: str) -> tuple | None:
    """A record's key: its join cells, compared under ``rule``.

    ``None`` when every one of them is blank (spaces count as blank), which
    matches nothing.  A cell a short row does not have is blank, as the grid
    shows it.
    """
    cells = [cell(record, column) for column in columns]
    if not any(text.strip() for text in cells):
        return None
    return tuple(match_value(text, rule) for text in cells)


@dataclass
class Side:
    """One file as the join takes it: its records, which of them and in what
    order (the rows its quarter shows), the join columns in the order they
    were picked, and how many columns it has."""

    records: Sequence[Sequence[str]]
    rows: Sequence[int]
    columns: Sequence[int]
    width: int

    def cells(self, index: int) -> list[str]:
        """Record ``index`` padded to the side's width: a short row is
        filled out with blanks, never shifted."""
        record = self.records[index]
        return [cell(record, column) for column in range(self.width)]


@dataclass
class Joined:
    """The output: its rows, and for each the A and B record it came from
    (``None`` for the side an unmatched row has no record on)."""

    rows: list[list[str]] = field(default_factory=list)
    sources: list[tuple[int | None, int | None]] = field(default_factory=list)
    #: Stopped at the row limit, with more to come.
    truncated: bool = False
    #: How many of the rows are matches, as opposed to unmatched rows kept.
    matched: int = 0
    #: Rows of each side whose join cells were all blank, so matched nothing.
    blank_a: int = 0
    blank_b: int = 0


def join(a: Side, b: Side, keep: str = "inner", rule: str = "exact",
         limit: int | None = None) -> Joined:
    """Join ``a`` to ``b`` on their columns, pair by pair.

    A hash join: B's rows are indexed by key once, and each A row then finds
    its matches in one lookup.  ``limit`` defaults to the table browser's
    :data:`~meridian_commander.table.MAX_ROWS`, read at call time so the tests
    can lower it.
    """
    if not a.columns or len(a.columns) != len(b.columns):
        raise ValueError("pick the same number of join columns in each file")
    if keep not in KEEPS:
        raise ValueError(f"unknown choice of rows: {keep!r}")
    if rule not in RULES:
        raise ValueError(f"unknown match rule: {rule!r}")
    if limit is None:
        limit = table.MAX_ROWS
    out = Joined()
    b_keys: dict[int, tuple | None] = {}
    index: dict[tuple, list[int]] = {}
    for row in b.rows:
        key = b_keys[row] = join_key(b.records[row], b.columns, rule)
        if key is None:
            out.blank_b += 1
        else:
            index.setdefault(key, []).append(row)
    matched_keys: set[tuple | None] = set()

    def add(cells: list[str], source: tuple[int | None, int | None]) -> bool:
        """Add a row; False, and the output marked cut short, when full."""
        if len(out.rows) >= limit:
            out.truncated = True
            return False
        out.rows.append(cells)
        out.sources.append(source)
        return True

    blank_b = [""] * b.width
    for row in a.rows:
        key = join_key(a.records[row], a.columns, rule)
        if key is None:
            out.blank_a += 1
        matches = index.get(key, []) if key is not None else []
        left = a.cells(row)
        for other in matches:
            if not add(left + b.cells(other), (row, other)):
                return out
            out.matched += 1
        if matches:
            matched_keys.add(key)
        elif keep != "inner" and not add(left + blank_b, (row, None)):
            return out
    if keep == "full":
        blank_a = [""] * a.width
        for row in b.rows:
            key = b_keys[row]
            if key is None or key not in matched_keys:
                if not add(blank_a + b.cells(row), (None, row)):
                    return out
    return out


def labels(a_labels: Sequence[str], b_labels: Sequence[str]) -> list[str]:
    """The output's column names: A's columns and then B's, each named for
    its file (``A.id``, ``B.id``), so two columns with the same name stay
    apart in the grid, in a filter and in a saved file."""
    return [f"A.{label}" for label in a_labels] + \
        [f"B.{label}" for label in b_labels]


def pairing(a_labels: Sequence[str], a_columns: Sequence[int],
            b_labels: Sequence[str], b_columns: Sequence[int]) -> str:
    """Which A column is matched with which B column, as the screen says it:
    ``A.id = B.user_id, A.day = B.date``."""
    return ", ".join(f"A.{a_labels[left]} = B.{b_labels[right]}"
                     for left, right in zip(a_columns, b_columns))
