"""Read ``.pptx`` / ``.pptm`` presentations using the standard library alone.

A deck's text lives in DrawingML (``<a:p>`` paragraphs of ``<a:r>`` runs of
``<a:t>``) rather than the WordprocessingML the document reader deals with,
but the package around it is the same OOXML zip, so
:mod:`meridian_commander.ooxml` does the plumbing.

Slide order comes from ``<p:sldIdLst>`` in ``ppt/presentation.xml``, resolved
through relationships -- **not** from the file names.  A deck whose slides have
been reordered keeps ``slide1.xml`` where it always was, so reading by name
would show a reordered deck in the wrong order.

What is read
------------
Per slide: the title, the body text with its bullet levels, tables, and the
speaker notes.  Placeholder text inherited from a layout or master is not
pulled in, so a slide shows what was typed on *it*; slide numbers and dates,
which are fields rather than content, are left out of the notes.

What is not
-----------
Images, charts, animations, geometry and formatting.  Shapes are read in the
order the file lists them, which is usually but not always reading order --
there is no layout engine here to do better.
"""

from __future__ import annotations

import curses
import zipfile
from dataclasses import dataclass, field

from . import theme
from .ooxml import (
    OoxmlError,
    attr,
    load_bytes,
    local,
    main_part,
    open_package,
    parse_part,
    rel_id,
    related,
    relationships,
)
from .util import ljust, truncate
from .viewer import wrap_line

PRESENTATION_SUFFIXES = (".pptx", ".pptm")

MAX_SLIDES = 2_000
MAX_LINES_PER_SLIDE = 2_000
MAX_TABLE_COLS = 32
MAX_CELL_WIDTH = 30

# Placeholder types that mean "this shape is the slide's title".
TITLE_PLACEHOLDERS = ("title", "ctrtitle")


@dataclass
class Slide:
    number: int
    title: str = ""
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.title or f"(slide {self.number})"


@dataclass
class Presentation:
    name: str
    slides: list[Slide] = field(default_factory=list)
    truncated: bool = False


def is_presentation(name: str) -> bool:
    """True if ``name`` looks like a deck this module can read."""
    return name.lower().endswith(PRESENTATION_SUFFIXES)


# -- text -------------------------------------------------------------------
def paragraph_text(para) -> str:
    """The visible text of an ``<a:p>``.

    Fields -- a slide number, a date -- are skipped: they are furniture the
    application fills in, not something anybody typed.  That means walking the
    tree rather than using ``iter()``, which cannot skip a subtree.
    """
    parts: list[str] = []

    def walk(node):
        for child in node:
            tag = local(child.tag)
            if tag == "fld":
                continue
            if tag == "t":
                parts.append(child.text or "")
            elif tag == "br":
                parts.append(" ")
            else:
                walk(child)

    walk(para)
    return "".join(parts)


def paragraph_level(para) -> int:
    """A paragraph's outline depth, from ``<a:pPr lvl="...">``."""
    for node in para:
        if local(node.tag) != "pPr":
            continue
        try:
            return max(0, int(attr(node, "lvl") or "0"))
        except ValueError:
            return 0
    return 0


def _paragraphs(elem):
    """Every ``<a:p>`` under ``elem``, in document order."""
    return [node for node in elem.iter() if local(node.tag) == "p"]


# -- shapes -----------------------------------------------------------------
def _placeholder_type(shape) -> str:
    """The shape's placeholder type (``title``, ``body``, ...), lowercased."""
    for node in shape.iter():
        if local(node.tag) == "ph":
            return (attr(node, "type") or "").lower()
    return ""


def _table_lines(table) -> list[str]:
    """A ``<a:tbl>`` as aligned columns with a rule under the first row."""
    rows = []
    for tr in table:
        if local(tr.tag) != "tr":
            continue
        cells: list[str] = []
        for tc in tr:
            if local(tc.tag) != "tc":
                continue
            if len(cells) >= MAX_TABLE_COLS:
                break
            cells.append(" ".join(paragraph_text(p)
                                  for p in _paragraphs(tc)).strip())
        rows.append(cells)
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


def _shape_lines(shape) -> list[str]:
    """A shape's text as lines, bullets indented by their outline level."""
    out = []
    for para in _paragraphs(shape):
        text = paragraph_text(para).strip()
        if not text:
            continue
        out.append("  " * (paragraph_level(para) + 1) + "* " + text)
    return out


# -- slides -----------------------------------------------------------------
def _read_slide(zf: zipfile.ZipFile, member: str, number: int) -> Slide:
    slide = Slide(number)
    root = parse_part(zf, member)
    if root is None:
        return slide
    for shape in root.iter():
        tag = local(shape.tag)
        if tag == "graphicFrame":
            for node in shape.iter():
                if local(node.tag) == "tbl":
                    slide.lines.extend(_table_lines(node))
        elif tag == "sp":
            if not slide.title and _placeholder_type(shape) in TITLE_PLACEHOLDERS:
                slide.title = " ".join(
                    paragraph_text(p).strip() for p in _paragraphs(shape)
                ).strip()
                continue
            slide.lines.extend(_shape_lines(shape))
        if len(slide.lines) >= MAX_LINES_PER_SLIDE:
            del slide.lines[MAX_LINES_PER_SLIDE:]
            slide.lines.append("(slide truncated)")
            break
    slide.notes = _read_notes(zf, member)
    return slide


def _read_notes(zf: zipfile.ZipFile, slide_member: str) -> list[str]:
    member = related(relationships(zf, slide_member), "notesSlide")
    if member is None:
        return []
    root = parse_part(zf, member)
    if root is None:
        return []
    out = []
    for para in _paragraphs(root):
        text = paragraph_text(para).strip()
        if text:
            out.append(text)
    return out


def parse_presentation(data: bytes, name: str = "presentation") -> Presentation:
    """Parse deck bytes.  Raises :class:`OoxmlError` on anything unreadable."""
    with open_package(data) as zf:
        part = main_part(zf, "ppt/presentation.xml")
        root = parse_part(zf, part)
        if root is None:
            raise OoxmlError("no presentation part in the archive")
        rels = relationships(zf, part)

        deck = Presentation(name)
        for node in root:
            if local(node.tag) != "sldIdLst":
                continue
            for entry in node:
                if local(entry.tag) != "sldId":
                    continue
                if len(deck.slides) >= MAX_SLIDES:
                    deck.truncated = True
                    break
                rid = rel_id(entry)
                member = rels.get(rid, ("", None))[1] if rid else None
                number = len(deck.slides) + 1
                if member is None:
                    # A slide whose relationship is missing or external has no
                    # content here; it keeps its place so the numbering the
                    # user sees still matches the deck.
                    deck.slides.append(Slide(number))
                    continue
                deck.slides.append(_read_slide(zf, member, number))
        if not deck.slides:
            raise OoxmlError("the presentation declares no slides")
        return deck


def read_presentation(fs, path: str) -> Presentation:
    """Load and parse the presentation at ``path`` on filesystem ``fs``."""
    return parse_presentation(load_bytes(fs, path), fs.basename(path))


# -- the browser ------------------------------------------------------------
class SlideView:
    """A full-screen slide browser: one slide at a time, Tab between them.

    Slides are discrete, so this is paged rather than scrolled the way a
    document is -- moving between slides is the main gesture, and scrolling
    only matters for a slide with more on it than fits.
    """

    def __init__(self, fs, path: str) -> None:
        self.fs = fs
        self.path = path
        self.name = fs.basename(path)
        self.error: str | None = None
        self.index = 0
        self.top = 0
        self.show_notes = False
        self.search = ""
        self.notice = ""
        self._cache: dict[tuple[int, bool, int], list[str]] = {}
        self.deck = self._load()

    def _load(self) -> Presentation:
        try:
            return read_presentation(self.fs, self.path)
        except Exception as exc:    # unreadable file, or the backend failed
            self.error = str(exc)
            # One empty slide rather than none, so every movement and drawing
            # path works on the error screen without a second set of guards.
            return Presentation(self.name, [Slide(1)])

    # -- current position -------------------------------------------------
    @property
    def slide(self) -> Slide:
        return self.deck.slides[self.index]

    def slide_lines(self, slide: Slide) -> list[str]:
        """A slide as text: its title ruled, its body, and optionally notes."""
        out = []
        if slide.title:
            out.append(slide.title)
            out.append("=" * len(slide.title))
        out.extend(slide.lines)
        if not out:
            out.append("(this slide has no text on it)")
        if self.show_notes:
            out.append("")
            out.append("Notes")
            out.append("-----")
            if slide.notes:
                out.extend("  " + note for note in slide.notes)
            else:
                out.append("  (none)")
        return out

    def display(self, width: int) -> list[str]:
        """The current slide wrapped to ``width``, cached per slide and width."""
        key = (self.index, self.show_notes, max(1, width))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        rows: list[str] = []
        for line in self.slide_lines(self.slide):
            rows.extend(wrap_line(line, max(1, width)))
        self._cache[key] = rows
        return rows

    # -- movement ---------------------------------------------------------
    def go_to(self, index: int) -> None:
        self.index = max(0, min(index, len(self.deck.slides) - 1))
        self.top = 0

    def step(self, delta: int) -> None:
        """Move by whole slides, wrapping at either end."""
        count = len(self.deck.slides)
        if count < 2:
            self.notice = " this deck has only one slide "
            return
        self.index = (self.index + delta) % count
        self.top = 0

    def toggle_notes(self) -> None:
        self.show_notes = not self.show_notes
        self.top = 0

    # -- search -----------------------------------------------------------
    def set_search(self, pattern: str) -> None:
        self.search = pattern or ""

    def _matches(self, text: str) -> bool:
        # Smart case, spelled the same way as in the viewer.
        if self.search == self.search.lower():
            return self.search in text.lower()
        return self.search in text

    def find_next(self, direction: int = 1) -> bool:
        """Move to the next slide containing the pattern.  Wraps around."""
        if not self.search:
            return False
        count = len(self.deck.slides)
        for step in range(1, count + 1):
            at = (self.index + step * direction) % count
            slide = self.deck.slides[at]
            haystack = "\n".join([slide.title] + slide.lines + slide.notes)
            if self._matches(haystack):
                self.go_to(at)
                return True
        return False

    # -- drawing ----------------------------------------------------------
    def draw(self, win) -> None:
        theme.background(win, "edit")
        win.erase()
        height, width = win.getmaxyx()
        body_h = max(0, height - 2)

        count = len(self.deck.slides)
        title = f" Slides: {self.name}"
        if not self.error:
            title += f" -- {self.index + 1}/{count}"
            if self.deck.truncated:
                title += " [truncated]"
        title += " "
        theme.paint(win, 0, 0, title, "keybar", width)

        if self.error:
            theme.paint(win, 2, 2, truncate(f"Cannot read: {self.error}",
                                            max(0, width - 4)), "panelerror")
            self._draw_footer(win, height, width)
            win.noutrefresh()
            return

        rows = self.display(width)
        self.top = max(0, min(self.top, max(0, len(rows) - 1)))
        for row in range(body_h):
            index = self.top + row
            if index >= len(rows):
                break
            attr = theme.attr("editheading" if self._is_heading(index, rows)
                              else "edit")
            # No guard here, unlike the title and footer around it: a body row
            # is never the last row of the window, and ljust() has already cut
            # the text to the width, so this is a write curses accepts.
            win.addstr(row + 1, 0, ljust(rows[index], width), attr)
        self._draw_footer(win, height, width)
        win.noutrefresh()

    def _is_heading(self, index: int, rows: list[str]) -> bool:
        """A row is a heading when the row under it is its rule."""
        return (index + 1 < len(rows) and bool(rows[index])
                and set(rows[index + 1]) in ({"="}, {"-"}))

    def _draw_footer(self, win, height: int, width: int) -> None:
        if self.notice:
            hint = self.notice
        elif self.error:
            hint = "[q]uit"
        else:
            parts = [self.slide.label[:30]]
            if self.search:
                parts.append(f"/{self.search}")
            parts.append("Tab slide  [t]notes  [/]find  n/N  [q]uit")
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
            body_h = max(1, height - 2)
            key = win.getch()
            self.notice = ""
            if key in (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10):
                break
            elif key in (curses.KEY_RIGHT, 9, ord("]"), ord(" ")):
                self.step(1)
            elif key in (curses.KEY_LEFT, curses.KEY_BTAB, ord("[")):
                self.step(-1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.top += 1
            elif key in (curses.KEY_UP, ord("k")):
                self.top = max(0, self.top - 1)
            elif key == curses.KEY_NPAGE:
                self.top += body_h
            elif key == curses.KEY_PPAGE:
                self.top = max(0, self.top - body_h)
            elif key == curses.KEY_HOME:
                self.top = 0
            elif key == curses.KEY_END:
                self.go_to(len(self.deck.slides) - 1)
            elif key in (ord("t"), ord("T")):
                self.toggle_notes()
            elif key in (ord("/"), curses.KEY_F7):
                pattern = dialogs.prompt(stdscr, "Search",
                                         "Find in deck (smart case):",
                                         default=self.search)
                curses.curs_set(0)
                if pattern is not None:
                    self.set_search(pattern)
                    if pattern and not self.find_next(1):
                        self.notice = f" '{pattern}' not found "
            elif key == ord("n"):
                if self.search and not self.find_next(1):
                    self.notice = " no match "
            elif key in (ord("N"), ord("p")):
                if self.search and not self.find_next(-1):
                    self.notice = " no match "
