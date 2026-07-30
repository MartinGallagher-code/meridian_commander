"""Show a Markdown file roughly as it is meant to look, in a terminal.

The other readers here throw formatting away, because a spreadsheet's meaning
is in its values and a document's is in its structure.  Markdown is the first
one where the formatting *is* the content: strip the emphasis and it stops
looking rendered at all.  So this produces styled lines -- text plus one style
code per character -- which the viewer draws with curses attributes.

Three things carry the rendering, in order of how much they do:

* **The markers go away.**  ``**bold**`` becoming bold text is most of the
  effect; the asterisks were never meant to be read.
* **Attributes** carry inline emphasis: bold, italic, reversed for code.
* **Layout** carries structure: headings ruled, lists indented by depth,
  quotes gutter-marked, tables aligned.

This is a *pragmatic subset*, not CommonMark.  The specification is enormous
because the edge cases are -- emphasis precedence, lazy continuation, HTML
blocks, entity handling -- and claiming compliance would be wrong in ways a
reader would have to discover for themselves.  What is here covers the
markdown that READMEs and documentation are actually written in.  Anything
unrecognised is shown as the plain text it is, never swallowed.

Press ``r`` in the viewer to see the source instead; a renderer that cannot be
turned off is a renderer you have to trust.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .viewer import Viewer

MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdown", ".mkd", ".mdwn")

# Style codes, as the viewer's STYLE_ATTRS reads them.
PLAIN = " "
BOLD = "B"
ITALIC = "I"
CODE = "C"
DIM = "D"
LINK = "U"

# A rule has no natural width, so it takes one wide enough for a comfortable
# reading column and is left unwrapped -- a narrow screen simply clips it.
RULE_WIDTH = 72
MAX_TABLE_COLS = 32
MAX_CELL_WIDTH = 40
MAX_LINES = 100_000


def is_markdown(name: str) -> bool:
    """True if ``name`` looks like a Markdown file."""
    return name.lower().endswith(MARKDOWN_SUFFIXES)


@dataclass
class Rendered:
    lines: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    nowrap: set[int] = field(default_factory=set)
    truncated: bool = False

    def add(self, text: str, style: str = "", wrap: bool = True) -> None:
        if len(self.lines) >= MAX_LINES:
            self.truncated = True
            return
        if style and len(style) != len(text):
            style = style.ljust(len(text))[:len(text)]
        if not self.lines and not text:
            return              # never open with a blank line
        if not wrap:
            self.nowrap.add(len(self.lines))
        self.lines.append(text)
        self.styles.append(style)

    def blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.add("")

    def trim(self) -> None:
        """Drop trailing blank lines, which no rendering should end on."""
        while self.lines and self.lines[-1] == "":
            self.nowrap.discard(len(self.lines) - 1)
            self.lines.pop()
            self.styles.pop()


# -- inline -----------------------------------------------------------------
_ESCAPABLE = set("\\`*_{}[]()#+-.!|~<>")

# Each opening and closing delimiter refuses to match after a backslash, so
# an escape stops the span from forming rather than merely being stripped out
# of it afterwards -- "\*not italic\*" has to stay plain text.
_PATTERNS = (
    ("code", re.compile(r"(?<!\\)(`+)(.+?)(?<!\\)\1", re.S)),
    # A badge -- an image wrapped in a link -- is the commonest thing in a
    # README, and neither the plain image nor the plain link pattern can read
    # one: the label contains brackets and parentheses of its own.
    ("linked_image",
     re.compile(r"(?<!\\)\[!\[([^\]]*)\]\([^)]*\)\]\(\s*([^)\s]*)[^)]*\)")),
    ("image", re.compile(r"(?<!\\)!\[([^\]]*)\]\(\s*([^)\s]*)[^)]*\)")),
    ("link", re.compile(r"(?<!\\)\[([^\]]+)\]\(\s*([^)\s]*)[^)]*\)")),
    ("reflink", re.compile(r"(?<!\\)\[([^\]]+)\]\[([^\]]*)\]")),
    ("autolink", re.compile(r"(?<!\\)<((?:https?|ftp|mailto):[^>\s]+)>")),
    ("strong", re.compile(r"(?<!\\)\*\*(?=\S)(.+?)(?<=\S)(?<!\\)\*\*", re.S)),
    # An underscore delimiter must stand outside a word: "some_variable_name"
    # is a name, not emphasis, and this is the rule that says so.
    ("strong_", re.compile(r"(?<![\w\\])__(?=\S)(.+?)(?<=\S)(?<!\\)__(?!\w)", re.S)),
    ("strike", re.compile(r"(?<!\\)~~(?=\S)(.+?)(?<=\S)(?<!\\)~~", re.S)),
    ("emph", re.compile(r"(?<!\\)\*(?=[^\s*])(.+?)(?<=[^\s*])(?<!\\)\*")),
    ("emph_", re.compile(r"(?<![\w\\])_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)")),
)


class Inline:
    """Turns inline Markdown into text plus a style code per character.

    Links are numbered rather than shown in place: a URL in the middle of a
    sentence is unreadable, and a terminal has no way to make text clickable
    that curses can drive.  The numbers refer to a list printed at the end,
    which is what ``lynx`` and ``w3m`` settled on for the same reason.
    """

    def __init__(self, refs: dict | None = None) -> None:
        self.refs = refs or {}
        self.links: list[str] = []

    def __call__(self, text: str, base: str = PLAIN) -> tuple[str, str]:
        out: list[str] = []
        style: list[str] = []

        def emit(chunk: str, code: str) -> None:
            # Escapes are undone as the text is emitted, so the style stays
            # one code per *output* character rather than per source character.
            index = 0
            while index < len(chunk):
                ch = chunk[index]
                if ch == "\\" and index + 1 < len(chunk) \
                        and chunk[index + 1] in _ESCAPABLE:
                    index += 1
                    ch = chunk[index]
                out.append(ch)
                style.append(code)
                index += 1

        rest = text
        while rest:
            best = None
            for kind, pattern in _PATTERNS:
                match = pattern.search(rest)
                if match is not None and (best is None
                                          or match.start() < best[1].start()):
                    best = (kind, match)
            if best is None:
                emit(rest, base)
                break
            kind, match = best
            emit(rest[:match.start()], base)
            self._emit_span(kind, match, base, emit, out, style)
            rest = rest[match.end():]
        return "".join(out), "".join(style)

    def _emit_span(self, kind, match, base, emit, out, style) -> None:
        if kind == "code":
            emit(match.group(2).strip(), CODE)
        elif kind == "linked_image":
            emit(f"[image: {match.group(1)}]" if match.group(1)
                 else "[image]", DIM)
            if match.group(2):
                self.links.append(match.group(2))
                emit(f"[{len(self.links)}]", DIM)
        elif kind == "image":
            emit(f"[image: {match.group(1)}]" if match.group(1)
                 else "[image]", DIM)
        elif kind in ("link", "reflink"):
            label = match.group(1)
            if kind == "link":
                url = match.group(2)
            else:
                key = (match.group(2) or label).lower()
                url = self.refs.get(key, "")
            inner, inner_style = self(label, LINK)
            out.extend(inner)
            style.extend(inner_style)
            if url:
                self.links.append(url)
                emit(f"[{len(self.links)}]", DIM)
        elif kind == "autolink":
            emit(match.group(1), LINK)
        elif kind == "strike":
            # No A_STRIKEOUT exists in Python's curses, so struck-out text is
            # dimmed: still visibly set apart, without pretending.
            emit(match.group(1), DIM)
        else:
            code = BOLD if kind.startswith("strong") else ITALIC
            inner, inner_style = self(match.group(1), code)
            out.extend(inner)
            # A span inside a span keeps its own code; the rest takes this one.
            style.extend(c if c != PLAIN else code for c in inner_style)


# -- blocks -----------------------------------------------------------------
_REF = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*<?([^>\s]+)>?\s*(?:\"[^\"]*\")?\s*$")
_ATX = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE = re.compile(r"^ {0,3}(```+|~~~+)\s*(\S*)")
_RULE = re.compile(r"^ {0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$")
_QUOTE = re.compile(r"^ {0,3}(>+)\s?(.*)$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
_DELIM = re.compile(r"^ {0,3}\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def collect_refs(lines: list[str]) -> dict:
    """``{label: url}`` for the ``[label]: url`` definitions in the file."""
    refs = {}
    for line in lines:
        match = _REF.match(line)
        if match:
            refs[match.group(1).lower()] = match.group(2)
    return refs


def _table_at(lines: list[str], index: int) -> int:
    """How many lines from ``index`` form a pipe table, or 0 for none."""
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return 0
    if not _DELIM.match(lines[index + 1]) or "-" not in lines[index + 1]:
        return 0
    if len(_cells(lines[index + 1])) != len(_cells(lines[index])):
        # GFM requires the delimiter row to have as many cells as the header.
        # Without this a paragraph mentioning "a | b" followed by a setext
        # "---" underline would be read as a table instead of a heading.
        return 0
    end = index + 2
    while end < len(lines) and "|" in lines[end] and lines[end].strip():
        end += 1
    return end - index


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")][:MAX_TABLE_COLS]


def _alignments(delim: str, count: int) -> list[str]:
    out = []
    for cell in _cells(delim):
        left, right = cell.startswith(":"), cell.endswith(":")
        out.append("^" if left and right else ">" if right else "<")
    while len(out) < count:
        out.append("<")
    return out[:count]


class _Renderer:
    def __init__(self, text: str) -> None:
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n") \
                         .expandtabs(4).split("\n")
        self.refs = collect_refs(self.lines)
        self.inline = Inline(self.refs)
        self.out = Rendered()

    # -- helpers ----------------------------------------------------------
    def _para(self, chunk: list[str], prefix: str = "",
              prefix_style: str = DIM) -> None:
        """Emit a paragraph, joining its soft-wrapped lines into one.

        This is why blocks are parsed before inline: an emphasis span can open
        on one source line and close on the next, and no line-at-a-time parser
        can see it.
        """
        parts, current = [], []
        for number, line in enumerate(chunk):
            current.append(line.strip())
            if number == len(chunk) - 1:
                continue
            if line.rstrip().endswith("\\"):
                current[-1] = current[-1].rstrip()[:-1].rstrip()
            elif not line.endswith("  "):
                continue
            parts.append(" ".join(current))         # a hard break
            current = []
        if current:
            parts.append(" ".join(current))
        for part in parts:
            body, style = self.inline(part.strip())
            self.out.add(prefix + body, prefix_style * len(prefix) + style)

    def _heading(self, level: int, text: str) -> None:
        self.out.blank()
        body, style = self.inline(text, BOLD)
        self.out.add(body, "".join(c if c != PLAIN else BOLD for c in style))
        if level <= 2 and body:
            rule = ("=" if level == 1 else "-") * len(body)
            self.out.add(rule, DIM * len(rule), wrap=False)

    def _table(self, chunk: list[str]) -> None:
        rows = [_cells(chunk[0])] + [_cells(line) for line in chunk[2:]]
        width = max(len(r) for r in rows)
        aligns = _alignments(chunk[1], width)
        cells = [[self.inline(row[i] if i < len(row) else "")
                  for i in range(width)] for row in rows]
        widths = [min(MAX_CELL_WIDTH,
                      max(len(row[i][0]) for row in cells)) for i in range(width)]
        self.out.blank()
        for index, row in enumerate(cells):
            text_parts, style_parts = [], []
            for i, (body, style) in enumerate(row):
                body, style = body[:widths[i]], style[:widths[i]]
                pad = widths[i] - len(body)
                if aligns[i] == ">":
                    left, right = pad, 0
                elif aligns[i] == "^":
                    left, right = pad // 2, pad - pad // 2
                else:
                    left, right = 0, pad
                text_parts.append(" " * left + body + " " * right)
                style_parts.append(PLAIN * left + style + PLAIN * right)
            joined = "  " + "  ".join(text_parts)
            self.out.add(joined.rstrip(),
                         (PLAIN * 2 + (PLAIN * 2).join(style_parts))[:len(joined.rstrip())],
                         wrap=False)
            if index == 0:
                rule = "  " + "  ".join("─" * w for w in widths)
                self.out.add(rule, DIM * len(rule), wrap=False)
        self.out.add("")

    # -- the scan ---------------------------------------------------------
    def run(self) -> Rendered:
        lines = self.lines
        index = self._front_matter(lines)
        para: list[str] = []
        while index < len(lines):
            line = lines[index]

            if _REF.match(line):
                index += 1                      # a definition, not content
                continue

            fence = _FENCE.match(line)
            if fence:
                self._flush(para)
                para = []
                index = self._fenced(lines, index, fence.group(1)[0])
                continue

            span = _table_at(lines, index)
            if span:
                self._flush(para)
                para = []
                self._table(lines[index:index + span])
                index += span
                continue

            setext = _SETEXT.match(line)
            if setext and para:
                self._heading(1 if setext.group(1)[0] == "=" else 2,
                              " ".join(p.strip() for p in para))
                para = []
                index += 1
                continue

            if _RULE.match(line):
                self._flush(para)
                para = []
                self.out.blank()
                self.out.add("─" * RULE_WIDTH, DIM * RULE_WIDTH, wrap=False)
                self.out.add("")
                index += 1
                continue

            atx = _ATX.match(line)
            if atx:
                self._flush(para)
                para = []
                self._heading(len(atx.group(1)), atx.group(2))
                index += 1
                continue

            quote = _QUOTE.match(line)
            if quote:
                self._flush(para)
                para = []
                depth = len(quote.group(1))
                self._para([quote.group(2)], "│ " * depth)
                index += 1
                continue

            item = _ITEM.match(line)
            if item:
                self._flush(para)
                para = []
                index = self._list_item(lines, index, item)
                continue

            if not line.strip():
                self._flush(para)
                para = []
                self.out.blank()
                index += 1
                continue

            para.append(line)
            index += 1

        self._flush(para)
        self.out.trim()
        self._link_list()
        return self.out

    def _front_matter(self, lines: list[str]) -> int:
        """Consume a YAML front-matter block, if the file opens with one.

        It has to be recognised before anything else: otherwise the opening
        "---" reads as a thematic break and the line after it as a setext
        heading, which is what a generic scanner does with it.  It is shown
        dimmed rather than dropped -- it is really in the file.
        """
        if not lines or lines[0].strip() != "---":
            return 0
        end = 1
        while end < len(lines) and lines[end].strip() not in ("---", "..."):
            end += 1
        if end >= len(lines):
            return 0                    # never closed: not front matter
        for line in lines[1:end]:
            self.out.add(line, DIM * len(line), wrap=False)
        self.out.add("")
        return end + 1

    def _flush(self, para: list[str]) -> None:
        if para:
            self._para(para)

    def _fenced(self, lines: list[str], index: int, marker: str) -> int:
        index += 1
        while index < len(lines) and not lines[index].lstrip().startswith(marker * 3):
            body = "    " + lines[index]
            self.out.add(body, DIM * len(body), wrap=False)
            index += 1
        return index + 1 if index < len(lines) else index

    def _list_item(self, lines: list[str], index: int, item) -> int:
        depth = len(item.group(1)) // 2
        marker = item.group(2)
        bullet = "•" if marker[0] in "-*+" else marker
        content = item.group(3)
        task = _TASK.match(content)
        box = ""
        if task:
            box = "☑ " if task.group(1) in "xX" else "☐ "
            content = task.group(2)
        chunk = [content]
        index += 1
        # A wrapped list item continues on the next line, indented; that is
        # lazy continuation, and READMEs are full of it.
        while index < len(lines) and lines[index].strip() \
                and not _ITEM.match(lines[index]) and not _ATX.match(lines[index]) \
                and lines[index].startswith(" "):
            chunk.append(lines[index].strip())
            index += 1
        prefix = "  " * (depth + 1) + bullet + " " + box
        self._para(chunk, prefix)
        return index

    def _link_list(self) -> None:
        if not self.inline.links:
            return
        self.out.blank()
        self.out.add("Links", BOLD * 5)
        for number, url in enumerate(self.inline.links, 1):
            text = f"  [{number}] {url}"
            self.out.add(text, DIM * len(text), wrap=False)


def render(text: str) -> Rendered:
    """Render Markdown source into styled lines for the viewer."""
    return _Renderer(text).run()


# -- the viewer -------------------------------------------------------------
class MarkdownView(Viewer):
    """The Markdown viewer: the ordinary viewer over rendered, styled lines.

    Everything the text viewer does -- scrolling, smart-case search, line
    numbers, wrapping -- works unchanged, because the rendering only adds a
    style code per character alongside the text it already understands.
    """

    def __init__(self, fs, path: str) -> None:
        self.show_source = False
        self._raw: list[str] = []
        super().__init__(fs, path)
        # Prose, like a document: wrapping is the point.
        self.wrap = True
        self._flow = None
        self._reflow(0)

    def _load(self) -> list[str]:
        lines = super()._load()
        self._raw = lines
        if self.error:
            return lines
        return self._rendered()

    def _rendered(self) -> list[str]:
        result = render("\n".join(self._raw))
        self.styles = result.styles
        self.nowrap = result.nowrap
        if result.truncated:
            self.truncated = True
        return result.lines

    def toggle_source(self) -> None:
        """Switch between the rendered view and the file as it was written."""
        self.show_source = not self.show_source
        if self.show_source:
            self.styles, self.nowrap = [], set()
            self.source = self._raw
        else:
            self.source = self._rendered()
        self.cur_match = None
        self.top = 0
        width = self._flow[1]
        self._flow = None
        self._reflow(width)

    # -- presentation -----------------------------------------------------
    def _title(self) -> str:
        title = f" Markdown: {self.name} "
        if self.show_source:
            title = f" Source: {self.name} "
        return title

    def _hints(self) -> str:
        return "[/]find  n/N  [r]aw  [w]rap  [q]uit"

    def _other_key(self, key: int) -> None:
        if key in (ord("r"), ord("R")):
            self.toggle_source()
