# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Read a ``.dc`` datacenter layout: the floor it describes, not its cables.

``.dc`` is the plain-text layout format of datacenter_visualization (dcviz,
``datacenter-layout-viewer`` on PyPI).  Every line is ``<kind> [<id-spec>]
[key=value ...] [+tag ...]``, indentation nests, and ranges multiply: eight
lines can be five hundred servers.  This module turns the text into the tree
of elements those lines make, so :mod:`~meridian_commander.dcview` can draw
the floor.  It has no curses in it, so every rule here is tested on its own.

dcviz's own parser is JavaScript (``js/parse.js`` and ``js/expand.js``), and
dcviz deliberately keeps one: a second parser is a second answer to "the same
floor plan", and the two can drift.  This is that second parser, kept because
Meridian is pure Python and reads files on SFTP and FTP panes where neither
Node nor dcviz is installed.  So it reads exactly what decides what the floor
*holds*, follows the JavaScript rule for rule there, is tested against
dcviz's own example files, and names everything else rather than guessing.

What it reads, as dcviz does:

- indentation nesting, tabs counting four spaces; a ``node`` that ends up
  holding other lines is reported as the slipped indent it almost always is;
- every range form: ``R[01..20]`` (padding kept), ``u[1..40x2]``, ``A..H``
  (``A..z`` refused), ``[web|db]``, ``r[1..2]-[a|b]``, ``R[1..4,7..10]``,
  and a range that expands to nothing (``r[,]``) reported;
- placeholders -- ``{id}`` ``{i}`` ``{i0}`` ``{n}`` ``{kind}`` ``{parent}``
  ``{path}``, any enclosing kind (``{room}``, ``{rack}``), widths
  (``{i:3}``) -- with an unknown one reported;
- ``{seq}`` and its ``seq=`` scopes, which number racks through a floor;
- ``if=`` conditions, which decide whether a line's element exists at all,
  giving its ``{seq}`` number back when it does not;
- attribute and tag inheritance (the layout keys -- ``id name u at`` and the
  rest of :data:`NON_INHERITED` -- stay with their own element);
- ``model`` lines, whose ``u=`` and tags every ``model=`` device takes;
- ``title`` lines, and rack slotting: ``u=`` heights, ``at=`` pins, the rest
  auto-filled into the lowest free run, a slot above the rack's ``u=``
  reported, and an empty rack as tall as its slots.

What it passes over on purpose: ``net`` and ``link`` lines (and the cable
settings on them), which describe the networks and do not change what stands
on the floor; and the keys that only shape dcviz's canvas (``cols dir gap
align w cables``) or describe a device's datasheet (``gbps pps ports``...).
Those are not checked, and a mistake in them is not counted here.

What it reports instead of reading: whatever dcviz itself would refuse --
a range it cannot expand, a number out of range, a placeholder with nothing
to name -- plus the limits of this reader (:data:`MAX_ELEMENTS`,
:data:`MAX_DEPTH`), so a picture that is not the whole layout always says so.
Each report is one line in :attr:`Layout.problems`, once per source line
however many elements that line made.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterator

#: The most elements one layout may make.  dcviz's own biggest example is a
#: quarter of a million; a typo like ``R[1..10000000]`` must not be allowed to
#: take the machine's memory with it before anything is drawn.
MAX_ELEMENTS = 1_000_000

#: The deepest nesting read.  Real layouts are five or six levels deep; a
#: file that steps every line further in would otherwise recurse until Python
#: gives up, and take the browser with it.
MAX_DEPTH = 200

#: Attributes that describe *this* element only and do not cascade to its
#: children -- the same set as ``NON_INHERITED`` in dcviz's ``parse.js``.
NON_INHERITED = frozenset((
    "id", "name", "at", "u", "w", "cols", "dir", "gap", "label", "size",
    "seq", "align", "cables", "if", "gbps", "pps", "buffer", "ports",
    "breakout", "latency", "stream", "cpugbps", "rxcost", "busy", "cores",
    "addr",
))

#: The numbers that change the floor, and the range each means anything over.
#: dcviz checks more (``cols``, ``gap``, ``w``) but those only arrange its
#: canvas; a mistake in them changes nothing drawn here.
NUMBERS = {"u": (1, 1000), "at": (1, 1000), "size": (1, 1000)}

#: Lines that are not elements.  ``net`` and ``link`` describe cables;
#: ``title`` names the floor and ``model`` defines a device once.
DIRECTIVES = frozenset(("net", "link", "title", "model"))

#: The placeholders every line has, which a kind of the same name never hides.
CONTEXT_NAMES = frozenset(("id", "i", "i0", "n", "seq", "kind", "parent", "path"))

#: What a layout's first real line starts with -- dcviz's ``looksLikeLayout``.
LAYOUT_START = re.compile(r"(dc|title|model)\s+\S", re.IGNORECASE)

_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_SUBST = re.compile(r"\{(\w+)(?::(\d+))?\}", re.ASCII)
_SEQ_USE = re.compile(r"\{seq(?::\d+)?\}", re.ASCII)
_BRACKET = re.compile(r"\[([^\]]*)\]")
_RANGE = re.compile(r"(.+?)\.\.(.+?)(?:x(\d+))?", re.ASCII)
_BARE_RANGE = re.compile(r"\S+\.\.\S+")
_INTEGER = re.compile(r"-?[0-9]+")
_ALPHABETS = (re.compile(r"[A-Z]"), re.compile(r"[a-z]"), re.compile(r"[0-9]"))
# What JavaScript's Number() accepts as a decimal; Python's float() would also
# take "inf", "nan" and "1_000", none of which a layout means.
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_PREFIXED = {"0x": re.compile(r"[0-9a-fA-F]+"), "0o": re.compile(r"[0-7]+"),
             "0b": re.compile(r"[01]+")}
_BASES = {"0x": 16, "0o": 8, "0b": 2}
#: Lines end at a newline, with or without a carriage return before it -- not
#: at every character Python's splitlines() would break on.
_NEWLINE = re.compile(r"\r?\n")


class LayoutError(ValueError):
    """A line dcviz would refuse, worded as dcviz words it."""


class _TooBig(Exception):
    """Raised to stop reading once :data:`MAX_ELEMENTS` have been made."""


def looks_like_layout(text: str) -> bool:
    """Whether ``text`` opens like a floor plan, as dcviz's test has it.

    Its first line that is neither blank nor a ``#`` comment has to start
    with ``dc``, ``title`` or ``model`` and a word after it.  Deliberately
    narrow: a ``dc`` calculator script or a fragment starting at ``room`` is
    left to the text viewer.
    """
    for raw in _NEWLINE.split(_without_mark(text)):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return LAYOUT_START.match(line) is not None
    return False


def _without_mark(text: str) -> str:
    """``text`` without a leading byte-order mark, which a browser's decoder
    drops before dcviz ever sees the file."""
    return text[1:] if text.startswith("\ufeff") else text


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------

def tokenize(line: str) -> tuple[list[str], str | None]:
    """Split one line into words, and say whether a quote was left open.

    Quotes (single or double) group a value with spaces in it and are not
    kept.  A ``#`` where a word would start begins a comment, so
    ``color=#4fa3ff`` keeps its colour and ``node a # spare`` loses the note.
    """
    out: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    started = False
    for char in line:
        if quote:
            if char == quote:
                quote = None
            else:
                cur.append(char)
            continue
        if char in "\"'":
            quote = char
            started = True
            continue
        if char == "#" and not started:
            break
        if char in " \t":
            if started:
                out.append("".join(cur))
                cur = []
                started = False
            continue
        cur.append(char)
        started = True
    if started:
        out.append("".join(cur))
    return out, quote


def indent_of(line: str) -> int:
    """The indentation of ``line``: a space counts one, a tab four."""
    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4
        else:
            break
    return width


def _is_attr_token(token: str) -> bool:
    return token.startswith("+") or "=" in token


def read_attrs(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
    """The ``key=value`` attributes and ``+tags`` among a line's words.

    Keys are lower-cased and a later one wins.  ``+a,b`` is two tags, and a
    bare word where an attribute would be reads as a tag too.
    """
    attrs: dict[str, str] = {}
    tags: list[str] = []
    for token in tokens:
        if token.startswith("+"):
            tags.extend(tag for tag in token[1:].split(",") if tag)
            continue
        at = token.find("=")
        if at > 0:
            attrs[token[:at].lower()] = token[at + 1:]
        else:
            tags.append(token)
    return attrs, tags


# ---------------------------------------------------------------------------
# Ranges and placeholders
# ---------------------------------------------------------------------------

def _same_alphabet(low: str, high: str) -> bool:
    return any(alpha.fullmatch(low) and alpha.fullmatch(high)
               for alpha in _ALPHABETS)


def _span(low: int, high: int, step: int, limit: int, spec: str) -> range:
    """The values from ``low`` to ``high`` either way, refused when too many."""
    direction = 1 if high >= low else -1
    values = range(low, high + direction, direction * step)
    if len(values) > limit:
        raise LayoutError(f'range "{spec}" makes {len(values):,} ids, more '
                          f"than the {limit:,} this reader takes")
    return values


def _expand_spec(spec: str, limit: int) -> list[str]:
    """One bracketed (or bare) range: ``01..20``, ``1..40x2``, ``A..H``,
    ``a|b|c``, or comma-separated segments of those."""
    if "," in spec:
        out: list[str] = []
        for part in spec.split(","):
            if part:
                out.extend(_expand_spec(part, limit))
            if len(out) > limit:
                raise LayoutError(f'range "{spec}" makes more than the '
                                  f"{limit:,} ids this reader takes")
        return out
    if "|" in spec:
        return spec.split("|")
    match = _RANGE.fullmatch(spec)
    if match is None:
        return [spec]
    raw_low, raw_high, raw_step = match.groups()
    step = int(raw_step) if raw_step else 1
    if step < 1:
        raise LayoutError(f'bad step in range "{spec}"')
    if _INTEGER.fullmatch(raw_low) and _INTEGER.fullmatch(raw_high):
        # "01" pads every value to its width.  The digits are what is padded,
        # so a minus sign is neither counted nor dropped.
        digits = raw_low[1:] if raw_low.startswith("-") else raw_low
        width = len(digits) if re.match(r"0[0-9]", digits) else 0
        values = _span(int(raw_low), int(raw_high), step, limit, spec)
        if not width:
            return [str(value) for value in values]
        return [("-" if value < 0 else "") + str(abs(value)).zfill(width)
                for value in values]
    if len(raw_low) == 1 and len(raw_high) == 1:
        if not _same_alphabet(raw_low, raw_high):
            raise LayoutError(
                f'cannot expand range "{spec}": {raw_low} and {raw_high} are '
                "not both A-Z, both a-z or both digits")
        return [chr(code) for code in
                _span(ord(raw_low), ord(raw_high), step, limit, spec)]
    raise LayoutError(f'cannot expand range "{spec}"')


def expand(token: str, limit: int = MAX_ELEMENTS) -> list[str]:
    """Every id a spec stands for, as dcviz's ``expand`` makes them.

    ``R[01..03]`` is R01 R02 R03; several bracket groups multiply out, and a
    bracket-free ``A..H`` is a range when it is the whole token.  More than
    ``limit`` ids is refused rather than built.
    """
    if not token:
        return [""]
    if not _BRACKET.search(token):
        if _BARE_RANGE.fullmatch(token):
            return _expand_spec(token, limit)
        return [token]
    results = [token]
    while _BRACKET.search(results[0]):
        grown: list[str] = []
        for current in results:
            match = _BRACKET.search(current)
            if match is None:
                grown.append(current)
                continue
            head, tail = current[:match.start()], current[match.end():]
            for piece in _expand_spec(match.group(1), limit):
                grown.append(head + piece + tail)
            if len(grown) > limit:
                raise LayoutError(f'"{token}" makes more than the {limit:,} '
                                  "ids this reader takes")
        results = grown
        if not results:
            break
    return results


def subst(text: str, ctx: dict[str, object]) -> str:
    """Fill ``{name}`` and ``{name:width}`` placeholders from ``ctx``.

    A name ``ctx`` does not hold is left as written, so literal braces in a
    label survive.  A width pads a whole number with zeros (``{seq:2}`` is
    07) and leaves anything else alone.
    """
    if "{" not in text:
        return text

    def fill(match: re.Match) -> str:
        key, width = match.group(1), match.group(2)
        if key not in ctx:
            return match.group(0)
        value = str(ctx[key])
        if not width or not _INTEGER.fullmatch(value):
            return value
        sign = "-" if value.startswith("-") else ""
        return sign + value.replace("-", "", 1).zfill(int(width))

    return _SUBST.sub(fill, text)


@lru_cache(maxsize=4096)
def js_number(raw: str) -> float | None:
    """``raw`` read as JavaScript's ``Number()`` reads it, or None for NaN.

    The checks below have to agree with dcviz's to the letter: ``u=1e2`` is
    100 there and ``u=0x10`` is 16, while ``u=1_0`` is not a number at all.
    """
    text = raw.strip()
    if not text:
        return 0.0
    prefix = text[:2].lower()
    digits = _PREFIXED.get(prefix)
    if digits is not None:
        if not digits.fullmatch(text[2:]):
            return None
        return float(int(text[2:], _BASES[prefix]))
    if text.lstrip("+-") == "Infinity" and len(text) - len(text.lstrip("+-")) <= 1:
        return float("-inf") if text.startswith("-") else float("inf")
    if not _DECIMAL.fullmatch(text):
        return None
    return float(text)


def int_attr(raw: str | None, fallback: int, bounds: tuple[int, int]) -> int:
    """A whole-number attribute, or ``fallback`` where dcviz would ignore it."""
    if raw is None:
        return fallback
    value = js_number(raw)
    if value is None or not bounds[0] <= value <= bounds[1]:
        return fallback
    return int(value)


def _glob(pattern: str) -> re.Pattern:
    body = "".join(".*" if char == "*" else "." if char == "?"
                   else re.escape(char) for char in pattern)
    return re.compile(body, re.IGNORECASE | re.DOTALL)


def nearest(word: str, options) -> str | None:
    """The option a mistyped ``word`` was most likely meant to be, or None.

    Two edits at most and fewer than half the word, a swap of neighbours
    counting as one -- dcviz's ``nearest``.
    """
    lowered = word.lower()
    best: str | None = None
    best_distance = float("inf")
    for option in options:
        target = option.lower()
        prev = list(range(len(target) + 1))
        for i in range(1, len(lowered) + 1):
            diag = prev[0]
            prev[0] = i
            for j in range(1, len(target) + 1):
                up = prev[j]
                cost = 0 if lowered[i - 1] == target[j - 1] else 1
                prev[j] = min(prev[j] + 1, prev[j - 1] + 1, diag + cost)
                if (i > 1 and j > 1 and lowered[i - 1] == target[j - 2]
                        and lowered[i - 2] == target[j - 1]):
                    prev[j] = min(prev[j], diag)
                diag = up
        if prev[len(target)] < best_distance:
            best_distance = prev[len(target)]
            best = option
    if best_distance <= 2 and best_distance < max(2, len(lowered) / 2):
        return best
    return None


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------

class _Line:
    """One line of the file, before expansion: a node of the syntax tree."""

    __slots__ = ("kind", "id_spec", "attrs", "tags", "children", "indent",
                 "line", "up", "cache")

    def __init__(self, kind: str, id_spec: str | None, attrs: dict[str, str],
                 tags: list[str], indent: int, line: int,
                 up: "_Line | None") -> None:
        self.kind = kind
        self.id_spec = id_spec
        self.attrs = attrs
        self.tags = tags
        self.children: list[_Line] = []
        self.indent = indent
        self.line = line
        self.up = up
        #: What every element this line makes shares, worked out once:
        #: hundreds of thousands of servers from one line need one copy.
        self.cache: dict | None = None


class Element:
    """One element of the expanded floor: a hall, a row, a rack, a server.

    ``attrs`` and ``tags`` are the element's own (after its ``model=`` is
    merged in); what it inherits is looked up on demand, so a quarter of a
    million servers from one line share one dictionary.
    """

    __slots__ = ("kind", "id", "segment", "parent", "children", "attrs",
                 "tags", "line", "u_at", "u_size", "u_height", "count")

    def __init__(self, kind: str, element_id: str, segment: str,
                 parent: "Element | None", attrs: dict[str, str],
                 tags: tuple[str, ...], line: int) -> None:
        self.kind = kind
        self.id = element_id
        #: Its part of the path: the id, or ``id#2`` for a second of a name.
        self.segment = segment
        self.parent = parent
        self.children: list[Element] = []
        self.attrs = attrs
        self.tags = tags
        self.line = line
        #: The U-slot it starts at and how many it fills, inside a rack.
        self.u_at = 0
        self.u_size = 0
        #: A rack's height in U.
        self.u_height = 0
        #: How many elements it is, itself included.
        self.count = 1

    @property
    def name(self) -> str:
        return self.attrs.get("name") or self.id

    @property
    def path(self) -> str:
        parts = []
        element: Element | None = self
        while element is not None:
            parts.append(element.segment)
            element = element.parent
        return "/".join(reversed(parts))

    def ancestors(self) -> Iterator["Element"]:
        """Its parent, the parent's parent, and so on up to the root."""
        element = self.parent
        while element is not None:
            yield element
            element = element.parent

    def get(self, key: str) -> str | None:
        """An attribute as the element has it, inherited ones included."""
        if key in self.attrs:
            return self.attrs[key]
        if key in NON_INHERITED:
            return None
        for ancestor in self.ancestors():
            if key in ancestor.attrs:
                return ancestor.attrs[key]
        return None

    def attributes(self) -> list[tuple[str, str, bool]]:
        """Every attribute it has, as ``(key, value, inherited)``: its own
        first, in the order written, then what it inherits, nearest first."""
        out = [(key, value, False) for key, value in self.attrs.items()]
        seen = set(self.attrs)
        for ancestor in self.ancestors():
            for key, value in ancestor.attrs.items():
                if key not in seen and key not in NON_INHERITED:
                    seen.add(key)
                    out.append((key, value, True))
        return out

    def all_tags(self) -> list[tuple[str, bool]]:
        """Its tags as ``(tag, inherited)``: its own, then its ancestors'."""
        out = [(tag, False) for tag in self.tags]
        seen = set(self.tags)
        for ancestor in self.ancestors():
            for tag in ancestor.tags:
                if tag not in seen:
                    seen.add(tag)
                    out.append((tag, True))
        return out

    def first_tag(self) -> str | None:
        """Its own first tag, else the nearest ancestor's: what it most
        specifically is, which is what colouring by tag shows."""
        if self.tags:
            return self.tags[0]
        for ancestor in self.ancestors():
            if ancestor.tags:
                return ancestor.tags[0]
        return None

    def __repr__(self) -> str:
        return f"<{self.kind} {self.path}>"


class Layout:
    """A read layout: its root element, counts and what could not be read."""

    def __init__(self) -> None:
        self.title = "datacenter"
        self.root: Element | None = None
        #: Further top-level elements.  dcviz draws only the first.
        self.others: list[Element] = []
        #: Elements made, by kind, in the order kinds were first met.
        self.counts: dict[str, int] = {}
        self.total = 0
        #: Each thing that was not read as written, worded as dcviz words it.
        self.problems: list[str] = []
        #: Devices defined by ``model`` lines, by lower-cased name.
        self.models: dict[str, tuple[str, dict[str, str], list[str]]] = {}
        #: Network lines passed over, as information rather than a problem.
        self.network_lines = 0
        #: Whether reading stopped at :data:`MAX_ELEMENTS`.
        self.stopped = False
        self._said: set = set()

    def warn(self, key, message: str) -> None:
        """Report ``message`` once for ``key``: a line's mistake is one
        problem, not one for each of the forty racks it became."""
        if key in self._said:
            return
        self._said.add(key)
        self.problems.append(message)

    def walk(self) -> Iterator[Element]:
        """Every element of the drawn tree, parents before children."""
        if self.root is None:
            return
        stack = [self.root]
        while stack:
            element = stack.pop()
            yield element
            stack.extend(reversed(element.children))


def _syntax_tree(text: str, layout: Layout) -> _Line:
    root = _Line("@root", None, {}, [], -1, 0, None)
    stack = [root]
    for number, raw in enumerate(_NEWLINE.split(text), 1):
        tokens, quote = tokenize(raw)
        if not tokens:
            continue
        if quote:
            layout.problems.append(
                f"line {number}: a {quote} is never closed, so the rest of "
                f"the line became one value: {tokens[-1]}")
        indent = indent_of(raw)
        while len(stack) > 1 and indent <= stack[-1].indent:
            stack.pop()
        parent = stack[-1]
        rest = tokens[1:]
        id_spec = rest.pop(0) if rest and not _is_attr_token(rest[0]) else None
        attrs, tags = read_attrs(rest)
        node = _Line(tokens[0].lower(), id_spec, attrs, tags, indent, number,
                     parent)
        parent.children.append(node)
        stack.append(node)
    return root


def _number_notes(attrs: dict[str, str], what: str,
                  line: int) -> list[tuple[tuple, str]]:
    """What is wrong with the numbers that place things, as ``(key,
    message)`` for :meth:`Layout.warn`: dcviz's ``checkNumbers``."""
    notes = []
    for key, (least, most) in NUMBERS.items():
        raw = attrs.get(key)
        if raw is None:
            continue
        said = f"line {line}: {what}: {key}={raw}"
        value = js_number(raw)
        if raw == "" or value is None or value in (float("inf"), float("-inf")):
            message = f"{said} is not a number -- ignored"
        elif not least <= value <= most:
            message = f"{said} is outside {least}..{most} -- ignored"
        elif value != int(value):
            message = f"{said} is not a whole number -- using {int(value)}"
        else:
            continue
        notes.append((("num", line, key, raw), message))
    return notes


def _note_unresolved(text: str, what: str, ctx: dict, layout: Layout,
                     line: int) -> None:
    if not _PLACEHOLDER.search(text):
        return
    names = " ".join("{%s}" % key for key in sorted(ctx))
    layout.warn(("ph", line, text),
                f'line {line}: {what} "{text}": no such placeholder here -- '
                f"this line can use {names}")


def _condition_holds(raw: str, ctx: dict, layout: Layout, line: int) -> bool:
    """Whether a line's ``if=`` holds for the element about to be made."""
    for clause in raw.split(","):
        negated = clause.find("!=")
        at = negated if negated >= 0 else clause.find("=")
        if at <= 0:
            layout.warn(("if", line, raw),
                        f"line {line}: if={raw}: each condition is "
                        "{placeholder}=value or {placeholder}!=value, "
                        "joined by commas -- ignored")
            return True
        left = subst(clause[:at], ctx)
        wanted = [subst(value, ctx) for value in
                  clause[at + (2 if negated >= 0 else 1):].split("|")]
        hit = any(_glob(value).fullmatch(left) is not None
                  if ("*" in value or "?" in value)
                  else left.lower() == value.lower()
                  for value in wanted)
        if hit == (negated >= 0):
            return False
    return True


class _Slots:
    """The U-slots of one rack taken so far, while its lines are read."""

    __slots__ = ("used", "low", "rack_u")

    def __init__(self, rack_u: int) -> None:
        self.used: set[int] = set()
        #: No slot below this one is free: where an auto-fill starts looking.
        self.low = 1
        #: The rack's declared height, or 0 when it has none.
        self.rack_u = rack_u

    def place(self, at: int | None, size: int) -> int:
        """Take ``size`` slots at ``at``, or the lowest free run of them."""
        if at is None:
            at = self.low
            while size > 1:
                clash = next((slot for slot in range(at, at + size)
                              if slot in self.used), None)
                if clash is None:
                    break
                at = clash + 1
        self.used.update(range(at, at + size))
        while self.low in self.used:
            self.low += 1
        return at


class _Reader:
    """Expands the syntax tree into elements, as dcviz's ``materialize``."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        #: The first element made, which is the root even when reading stops
        #: before it is finished.
        self.first: Element | None = None
        #: {seq} counts: per block of lines side by side, and per seq= name
        #: (or per enclosing element, for a seq= naming a kind).
        self.block_counts: dict[_Line, dict[str, int]] = {}
        self.named_counts: dict[object, int] = {}
        #: Each id spec's expansion, made once: forty racks of the same forty
        #: servers share one list of names.
        self.ids: dict[str, list[str]] = {}

    def _next_seq(self, syntax: _Line, series: str | None,
                  parent: Element | None) -> tuple[dict, object, int]:
        if series:
            counts: dict = self.named_counts
            scope = next((element for element in _chain(parent)
                          if element.kind == series), None)
            key: object = (scope, syntax.kind) if scope is not None else series
        else:
            block = syntax.up if syntax.up is not None else syntax
            counts = self.block_counts.setdefault(block, {})
            key = syntax.kind
        number = counts.get(key, 0) + 1
        counts[key] = number
        return counts, key, number

    def materialize(self, syntax: _Line, parent: Element | None,
                    taken: set[str], slots: _Slots | None,
                    depth: int) -> list[Element]:
        """Make the elements ``syntax`` stands for under ``parent``."""
        layout = self.layout
        line = syntax.line
        if depth > MAX_DEPTH:
            layout.warn(("deep", line),
                        f"line {line}: nested more than {MAX_DEPTH} levels "
                        "deep -- not read, nor the lines under it")
            return []
        base = _context_for(parent)
        spec = subst(syntax.id_spec, base) if syntax.id_spec else None
        try:
            if spec:
                # The id spec is filled before the ids exist, so {id} and {i}
                # cannot be used in it; they belong on an attribute.
                _note_unresolved(spec, "id", base, layout, line)
                ids = self.ids.get(spec)
                if ids is None:
                    ids = self.ids[spec] = expand(spec)
            else:
                count = len(parent.children) if parent is not None else 0
                ids = [f"{syntax.kind}{count + 1}"]
        except LayoutError as exc:
            layout.warn(("expand", line), f"line {line}: {exc}")
            return []
        if not ids:
            layout.warn(("empty", line),
                        f'line {line}: {syntax.kind} "{syntax.id_spec}" expands '
                        "to no ids -- nothing was created for this line or "
                        "the lines under it")
            return []

        shared = self._shared(syntax)
        if syntax.kind == "node" and syntax.children:
            # Almost always a slipped indent: the rack's lines became the
            # node's, and the rack stands empty.
            layout.warn(("nested", line),
                        f'line {line}: node "{syntax.id_spec or syntax.kind}" '
                        "contains the lines below it (from line "
                        f"{syntax.children[0].line}) \u2014 nodes rarely contain "
                        "elements; if the indentation slipped, children sit "
                        "deeper than their parent and siblings align")

        made: list[Element] = []
        for index, raw_id in enumerate(ids):
            plain, attrs, tags = shared["plain"], shared["attrs"], shared["tags"]
            notes = shared["notes"]
            if shared["needs_ctx"]:
                ctx: dict[str, object] = dict(base)
                if syntax.kind not in CONTEXT_NAMES:
                    # {rack} on a rack's own line is the rack itself.
                    ctx[syntax.kind] = raw_id
                ctx.update(id=raw_id, i=index + 1, i0=index, n=len(ids),
                           seq=0, kind=syntax.kind)
                numbered = None
                if shared["uses_seq"]:
                    series = syntax.attrs.get("seq")
                    numbered = self._next_seq(
                        syntax, None if series is None else subst(series, ctx),
                        parent)
                    ctx["seq"] = numbered[2]
                if shared["dynamic"]:
                    plain = {}
                    for key, value in syntax.attrs.items():
                        plain[key] = subst(value, ctx)
                        _note_unresolved(plain[key], f"{key}=", ctx, layout, line)
                if "if" in syntax.attrs:
                    if not _condition_holds(syntax.attrs["if"], ctx, layout, line):
                        # Not made, so its number goes to the next one.
                        if numbered is not None:
                            counts, key, number = numbered
                            counts[key] = number - 1
                        continue
                    plain.pop("if", None)
                if shared["dynamic"] or shared["dynamic_tags"]:
                    written = syntax.tags
                    if shared["dynamic_tags"]:
                        written = []
                        for tag in syntax.tags:
                            filled = subst(tag, ctx)
                            _note_unresolved(filled, "tag +", ctx, layout, line)
                            written.append(filled)
                    notes = (_number_notes(plain, shared["what"], line)
                             if shared["dynamic"] else list(shared["numbers"]))
                    attrs, tags = self._with_model(syntax, plain, written, notes)
            for key, message in notes:
                layout.warn(key, message)
            made.append(self._make(syntax, plain.get("id") or raw_id, parent,
                                   attrs, tags, taken, slots, depth))
        return made

    def _shared(self, syntax: _Line) -> dict:
        """What every element of a line has in common, worked out once."""
        if syntax.cache is not None:
            return syntax.cache
        values = list(syntax.attrs.values())
        dynamic = any("{" in value for value in values)
        dynamic_tags = any("{" in tag for tag in syntax.tags)
        uses_seq = (any(_SEQ_USE.search(value) for value in values)
                    or any(_SEQ_USE.search(tag) for tag in syntax.tags))
        # if= decides whether an element is made; it is not one of its
        # attributes.
        plain = {key: value for key, value in syntax.attrs.items()
                 if key != "if"}
        what = f'"{syntax.id_spec or syntax.kind}"'
        cache: dict = {
            "dynamic": dynamic,
            "dynamic_tags": dynamic_tags,
            "uses_seq": uses_seq,
            "needs_ctx": (dynamic or dynamic_tags or uses_seq
                          or "if" in syntax.attrs),
            "plain": plain,
            "what": what,
        }
        # Checked once for the line, and said when an element is made: a line
        # whose if= never holds makes nothing, and dcviz says nothing of it.
        numbers = [] if dynamic else _number_notes(plain, what, syntax.line)
        notes = list(numbers)
        cache["attrs"], cache["tags"] = self._with_model(syntax, plain,
                                                         syntax.tags, notes)
        cache["numbers"] = numbers
        cache["notes"] = notes
        syntax.cache = cache
        return cache

    def _with_model(self, syntax: _Line, attrs: dict[str, str],
                    tags: list[str], notes: list) -> tuple[dict[str, str],
                                                           tuple[str, ...]]:
        """``attrs`` and ``tags`` with the named ``model``'s merged under
        them.  A name a letter off a defined one goes into ``notes``."""
        written = attrs.get("model")
        if written is not None and self.layout.models:
            found = self.layout.models.get(written.lower())
            if found is not None:
                _, model_attrs, model_tags = found
                attrs = {**model_attrs, **attrs}
                tags = [*model_tags, *tags]
            else:
                guess = nearest(written, [name for name, _, _ in
                                          self.layout.models.values()])
                if guess:
                    notes.append((
                        ("model", syntax.line),
                        f"line {syntax.line}: "
                        f'"{syntax.id_spec or syntax.kind}": model={written} '
                        f"is not a model defined here -- did you mean "
                        f"{guess}? Kept as a note"))
        return attrs, _own_tags(tags)

    def _make(self, syntax: _Line, element_id: str, parent: Element | None,
              attrs: dict[str, str], tags: tuple[str, ...], taken: set[str],
              slots: _Slots | None, depth: int) -> Element:
        layout = self.layout
        if layout.total >= MAX_ELEMENTS:
            raise _TooBig
        segment = element_id
        if segment in taken:
            number = 2
            while f"{segment}#{number}" in taken:
                number += 1
            where = f"{parent.path}/{segment}" if parent is not None else segment
            layout.warn(("duplicate", syntax.line),
                        f'line {syntax.line}: duplicate path "{where}" renamed '
                        f'to "{where}#{number}"')
            segment = f"{segment}#{number}"
        taken.add(segment)
        element = Element(syntax.kind, element_id, segment, parent, attrs,
                          tags, syntax.line)
        layout.total += 1
        layout.counts[syntax.kind] = layout.counts.get(syntax.kind, 0) + 1
        if self.first is None:
            self.first = element
        if parent is not None:
            parent.children.append(element)
        if slots is not None:
            self._slot(element, parent, slots, syntax.line)

        if syntax.children:
            names: set[str] = set()
            inner = None
            if element.kind == "rack":
                inner = _Slots(int_attr(attrs.get("u"), 0, NUMBERS["u"]))
            for child in syntax.children:
                self.materialize(child, element, names, inner, depth + 1)
            element.count += sum(child.count for child in element.children)
        if element.kind == "rack":
            # A u= that cannot be used means no declared height, and the rack
            # is as tall as what is in it, or 42U.
            declared = int_attr(element.attrs.get("u"), 0, NUMBERS["u"])
            top = max((child.u_at + child.u_size - 1
                       for child in element.children), default=0)
            element.u_height = declared or max(top, 42)
        return element

    def _slot(self, element: Element, rack: Element | None, slots: _Slots,
              line: int) -> None:
        """Place a rack's child: at its ``at=``, or in the lowest free run."""
        assert rack is not None
        raw_size = element.attrs.get("u")
        if raw_size is None:
            raw_size = element.attrs.get("size")
        size = int_attr(raw_size, 1, NUMBERS["size"])
        pinned = element.attrs.get("at")
        at = slots.place(None if pinned is None
                         else int_attr(pinned, 1, NUMBERS["at"]), size)
        element.u_at = at
        element.u_size = size
        rack_u = slots.rack_u
        if rack_u and at + size - 1 > rack_u:
            self.layout.warn(
                ("overflow", line),
                f'line {line}: node "{element.id}" reaches U{at + size - 1}, '
                f'above rack "{rack.id}" u={rack_u} \u2014 raise the rack\'s u= '
                "or lower the node's at=")


def _chain(element: Element | None) -> Iterator[Element]:
    while element is not None:
        yield element
        element = element.parent


def _context_for(parent: Element | None) -> dict[str, object]:
    """The placeholders a line under ``parent`` can use before its own."""
    ctx: dict[str, object] = {}
    if parent is not None:
        for element in reversed(list(_chain(parent))):
            ctx[element.kind] = element.id
        ctx["parent"] = parent.id
        ctx["path"] = parent.path
    return ctx


def _own_tags(tags: list[str]) -> tuple[str, ...]:
    """Tags lower-cased, each once, in the order written."""
    return tuple(dict.fromkeys(tag.lower() for tag in tags))


def parse(text: str) -> Layout:
    """Read a whole layout.

    Never raises for anything in the text: what it cannot read is in
    :attr:`Layout.problems`, and a layout with no elements has no root.
    """
    layout = Layout()
    syntax = _syntax_tree(_without_mark(text), layout)
    elements: list[_Line] = []

    def sift(node: _Line) -> None:
        # net, link, title and model lines are pulled out at the top level
        # and under one another, and whatever sits under them is still read.
        for child in node.children:
            if child.kind in ("net", "link"):
                layout.network_lines += 1
            elif child.kind == "title":
                layout.title = (child.id_spec or child.attrs.get("name")
                                or layout.title)
            elif child.kind == "model":
                _define_model(child, layout)
            else:
                elements.append(child)
                continue
            sift(child)

    sift(syntax)
    if not elements:
        return layout
    # One top-level line making one element is the root; anything else is
    # gathered under a dc named by the title, as dcviz gathers it.
    top = _Line("dc", layout.title, {}, [], -1, 0, None)
    top.children = elements
    if len(elements) == 1:
        alone = elements[0]
        try:
            made = len(expand(alone.id_spec)) if alone.id_spec else 1
        except LayoutError:
            made = 1
        if made == 1:
            top = alone
    reader = _Reader(layout)
    try:
        roots = reader.materialize(top, None, set(), None, 0)
    except _TooBig:
        layout.stopped = True
        layout.problems.append(
            f"stopped after {MAX_ELEMENTS:,} elements -- the rest of the "
            "layout was not read")
        roots = [reader.first] if reader.first is not None else []
        if roots:
            _recount(roots[0])
    if roots:
        layout.root, layout.others = roots[0], roots[1:]
        layout.title = layout.root.name
    if layout.others:
        layout.problems.append(
            f"{len(layout.others)} more top-level element"
            f"{'s' if len(layout.others) > 1 else ''} beside "
            f'"{layout.root.id}" -- only the first is drawn, as in dcviz')
    return layout


def _recount(root: Element) -> None:
    """Fill in ``count`` after a stop left the open elements unfinished."""
    order = []
    stack = [root]
    while stack:
        element = stack.pop()
        order.append(element)
        stack.extend(element.children)
    for element in reversed(order):
        element.count = 1 + sum(child.count for child in element.children)


def _define_model(line: _Line, layout: Layout) -> None:
    name = line.id_spec
    if not name:
        layout.problems.append(
            f"line {line.line}: model needs a name, as in model 7050dx4 u=1 "
            "+switch gbps=12800")
        return
    if name.lower() in layout.models:
        layout.problems.append(
            f'line {line.line}: model "{name}" is defined twice -- the later '
            "one is used")
    for key, message in _number_notes(line.attrs, f'model "{name}"', line.line):
        layout.warn(key, message)
    layout.models[name.lower()] = (name, dict(line.attrs), list(line.tags))
