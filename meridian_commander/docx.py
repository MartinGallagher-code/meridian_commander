"""Read ``.docx`` / ``.docm`` documents using the standard library alone.

A Word document is `word/document.xml` inside the OOXML package: a flat
sequence of paragraphs (``<w:p>``), each a run of runs (``<w:r>``) carrying
text (``<w:t>``).  There is no shared-string table and no number formats, so
this is a good deal simpler than the spreadsheet reader -- the work is in
recovering the *structure* a document has and a plain text file does not:
headings, lists and tables.

What is read
------------
Body text in document order, with headings, bulleted and numbered lists, and
tables.  A list's marker comes from ``numbering.xml`` where the paragraph
references it, because in a real Word document a bullet and a numbered item
are both style "ListParagraph" and are told apart only there.

What is not
-----------
Headers, footers, footnotes, comments and tracked changes are skipped -- they
are not part of the body flow.  Character formatting (bold, colour, size) is
dropped: this is a reader for what the document *says*.

Numbered items are counted by this reader rather than by Word.  Word's own
numbering restarts and level rules live in ``numbering.xml`` and depend on
state a linear reader does not keep, so a document that restarts a list
mid-way will be numbered straight through here.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field

from .ooxml import (
    OoxmlError,
    attr,
    load_bytes,
    local,
    main_part,
    open_package,
    parse_part,
    related,
    relationships,
)
from .viewer import Viewer

DOCUMENT_SUFFIXES = (".docx", ".docm")

# A document is one long flow, so the cap is on paragraphs rather than on
# anything two-dimensional.
MAX_BLOCKS = 50_000
MAX_TABLE_COLS = 64
# Wide enough for prose, narrow enough that one runaway cell cannot push every
# other column off the screen.
MAX_CELL_WIDTH = 40

HEADING = "heading"
PARAGRAPH = "paragraph"
BULLET = "bullet"
NUMBER = "number"
TABLE = "table"


@dataclass
class Block:
    """One unit of the document flow."""

    kind: str = PARAGRAPH
    text: str = ""
    level: int = 0                                   # heading or list depth
    rows: list[list[str]] = field(default_factory=list)   # kind == TABLE


@dataclass
class Document:
    name: str
    blocks: list[Block] = field(default_factory=list)
    truncated: bool = False


def is_document(name: str) -> bool:
    """True if ``name`` looks like a document this module can read."""
    return name.lower().endswith(DOCUMENT_SUFFIXES)


# -- paragraph text ---------------------------------------------------------
def paragraph_text(elem) -> str:
    """The visible text of a ``<w:p>``, with its runs glued back together.

    Word splits a sentence across runs wherever formatting changes, and a
    hyperlink puts its runs in a child element, so the whole subtree is walked
    rather than just the direct children.
    """
    parts = []
    for node in elem.iter():
        tag = local(node.tag)
        if tag == "t":
            parts.append(node.text or "")
        elif tag == "tab":
            parts.append(" ")
        elif tag == "br":
            # A line break inside a paragraph, including a page break: both
            # are a gap in the flow, and the viewer wraps for itself.
            parts.append(" ")
    return "".join(parts)


def _pr(elem):
    """A paragraph's ``<w:pPr>`` properties element, or ``None``."""
    for node in elem:
        if local(node.tag) == "pPr":
            return node
    return None


def _style_name(pr) -> str:
    for node in pr:
        if local(node.tag) == "pStyle":
            return (attr(node, "val") or "").lower()
    return ""


def _num_ref(pr) -> tuple[str, int] | None:
    """``(numId, ilvl)`` when the paragraph is part of a numbered list."""
    for node in pr:
        if local(node.tag) != "numPr":
            continue
        num_id, level = None, 0
        for child in node:
            tag = local(child.tag)
            if tag == "numId":
                num_id = attr(child, "val")
            elif tag == "ilvl":
                try:
                    level = int(attr(child, "val") or "0")
                except ValueError:
                    level = 0
        if num_id is not None:
            return num_id, level
    return None


def _heading_level(style: str) -> int | None:
    """Heading depth from a style name, or ``None`` if it is not a heading."""
    if not style.startswith("heading"):
        return None
    try:
        return max(1, int(style[len("heading"):]))
    except ValueError:
        return None


# -- numbering --------------------------------------------------------------
def read_numbering(zf: zipfile.ZipFile, member: str) -> dict[tuple[str, int], str]:
    """``{(numId, ilvl): format}`` from ``numbering.xml``.

    Two hops: a paragraph names a ``numId``, which names an ``abstractNumId``,
    which holds one ``<w:lvl>`` per indent level with the format on it.  The
    format is what separates a bullet from a numbered item -- both paragraphs
    look identical without it.
    """
    root = parse_part(zf, member)
    if root is None:
        return {}
    formats: dict[tuple[str, int], str] = {}     # (abstractNumId, ilvl) -> fmt
    concrete: dict[str, str] = {}                # numId -> abstractNumId
    for node in root:
        tag = local(node.tag)
        if tag == "abstractNum":
            abstract = attr(node, "abstractNumId") or ""
            for lvl in node:
                if local(lvl.tag) != "lvl":
                    continue
                try:
                    ilvl = int(attr(lvl, "ilvl") or "0")
                except ValueError:
                    ilvl = 0
                for child in lvl:
                    if local(child.tag) == "numFmt":
                        formats[(abstract, ilvl)] = (attr(child, "val")
                                                     or "").lower()
        elif tag == "num":
            num_id = attr(node, "numId") or ""
            for child in node:
                if local(child.tag) == "abstractNumId":
                    concrete[num_id] = attr(child, "val") or ""
    out = {}
    for num_id, abstract in concrete.items():
        for (a, ilvl), fmt in formats.items():
            if a == abstract:
                out[(num_id, ilvl)] = fmt
    return out


# -- body -------------------------------------------------------------------
def _classify(elem, numbering) -> Block:
    """Turn a ``<w:p>`` into a block, reading its style and list properties."""
    text = paragraph_text(elem)
    pr = _pr(elem)
    if pr is None:
        return Block(PARAGRAPH, text)
    style = _style_name(pr)
    level = _heading_level(style)
    if level is not None:
        return Block(HEADING, text, level=level)
    ref = _num_ref(pr)
    if ref is not None:
        fmt = numbering.get(ref, "")
        kind = BULLET if fmt == "bullet" else NUMBER
        return Block(kind, text, level=ref[1])
    if "listbullet" in style:
        return Block(BULLET, text)
    if "listnumber" in style:
        return Block(NUMBER, text)
    return Block(PARAGRAPH, text)


def _table_rows(elem) -> list[list[str]]:
    rows = []
    for tr in elem:
        if local(tr.tag) != "tr":
            continue
        cells: list[str] = []
        for tc in tr:
            if local(tc.tag) != "tc":
                continue
            if len(cells) >= MAX_TABLE_COLS:
                break
            text = " ".join(paragraph_text(p) for p in tc
                            if local(p.tag) == "p")
            cells.append(text.strip())
        rows.append(cells)
    return rows


def parse_document(data: bytes, name: str = "document") -> Document:
    """Parse document bytes.  Raises :class:`OoxmlError` on anything unreadable."""
    with open_package(data) as zf:
        part = main_part(zf, "word/document.xml")
        root = parse_part(zf, part)
        if root is None:
            raise OoxmlError("no document part in the archive")
        rels = relationships(zf, part)
        numbering_member = related(rels, "numbering")
        numbering = (read_numbering(zf, numbering_member)
                     if numbering_member else {})

        body = None
        for node in root:
            if local(node.tag) == "body":
                body = node
                break
        if body is None:
            raise OoxmlError("the document has no body")

        doc = Document(name)
        for node in body:
            if len(doc.blocks) >= MAX_BLOCKS:
                doc.truncated = True
                break
            tag = local(node.tag)
            if tag == "p":
                doc.blocks.append(_classify(node, numbering))
            elif tag == "tbl":
                doc.blocks.append(Block(TABLE, rows=_table_rows(node)))
        return doc


def read_document(fs, path: str) -> Document:
    """Load and parse the document at ``path`` on filesystem ``fs``."""
    return parse_document(load_bytes(fs, path), fs.basename(path))


# -- rendering --------------------------------------------------------------
def _table_lines(rows: list[list[str]]) -> list[str]:
    """Lay a table out as aligned columns with a rule under the first row."""
    if not rows:
        return []
    width = max(len(r) for r in rows)
    widths = [0] * width
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], min(len(cell), MAX_CELL_WIDTH))
    out = []
    for index, row in enumerate(rows):
        cells = [(row[i] if i < len(row) else "")[:widths[i]].ljust(widths[i])
                 for i in range(width)]
        out.append("  " + "  ".join(cells).rstrip())
        if index == 0:
            out.append("  " + "  ".join("-" * w for w in widths).rstrip())
    return out


def to_lines(doc: Document) -> list[str]:
    """Render a document as plain text lines for the viewer.

    Structure is carried by layout rather than by attributes: a heading gets a
    rule under it, list items are indented by depth, and a table becomes
    aligned columns.  That way the ordinary viewer -- which knows only about
    lines -- shows a document with its shape intact.
    """
    lines: list[str] = []
    counters: dict[int, int] = {}

    def blank():
        # Never open with a blank line, and never stack two.
        if lines and lines[-1] != "":
            lines.append("")

    for block in doc.blocks:
        if block.kind != NUMBER:
            counters.clear()
        if block.kind == HEADING:
            blank()
            lines.append(block.text)
            rule = "=" if block.level <= 1 else "-"
            lines.append(rule * max(1, len(block.text)))
        elif block.kind == BULLET:
            lines.append("  " * (block.level + 1) + "* " + block.text)
        elif block.kind == NUMBER:
            counters[block.level] = counters.get(block.level, 0) + 1
            lines.append("  " * (block.level + 1)
                         + f"{counters[block.level]}. " + block.text)
        elif block.kind == TABLE:
            blank()
            lines.extend(_table_lines(block.rows))
            lines.append("")
        else:
            lines.append(block.text)
    if doc.truncated:
        blank()
        lines.append("(document truncated)")
    return lines


class DocxView(Viewer):
    """The document viewer: the ordinary viewer over a rendered document.

    A document is a linear flow of text, which is exactly what
    :class:`~meridian_commander.viewer.Viewer` already scrolls, searches and
    numbers.  Only the loading differs, so that is all this overrides --
    wrapping in particular matters here, because prose is written to a page
    width rather than to a terminal's.
    """

    def __init__(self, fs, path: str) -> None:
        super().__init__(fs, path)
        # Prose is the case wrapping exists for, so it starts on.
        self.wrap = True
        self._flow = None
        self._reflow(0)

    def _load(self) -> list[str]:
        try:
            document = read_document(self.fs, self.path)
        except Exception as exc:
            self.error = str(exc)
            return []
        self.truncated = document.truncated
        return to_lines(document)
