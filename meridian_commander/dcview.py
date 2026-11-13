# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The floor view: a ``.dc`` datacenter layout drawn as the floor it makes.

A ``.dc`` file is compact on purpose -- eight lines can be five hundred
servers -- so its text does not show what stands on the floor.  This browser
reads it with :mod:`~meridian_commander.dclayout` and draws the result, a
level at a time, walking from the whole site down to one rack:

- **Plan** (a site, a hall, a room): one line per row, each rack one cell,
  the cell filled as far as the rack is full (``▁`` to ``█``) and coloured by
  what it mostly holds.  A level that holds halls shows each with its rows
  under it.
- **Row**: the racks side by side as columns of U-slots, two U to a cell in
  half-blocks, as tall as each rack is, standing on one floor.
- **Rack**: the rack's elevation, U1 at the bottom, one line per U, every
  device in its ``at=`` or auto-filled slot with its id beside it.

Arrows move the cursor (at rack zoom left and right walk on to the next
rack, at row zoom up and down to the next row), ``Enter`` zooms in and
``Backspace`` or ``Esc`` back out.  The inspector under the floor shows the
element under the cursor: its path, kind, slot, how many elements it holds,
its attributes and tags, inherited ones marked ``^``.  ``c`` colours devices
by ``role=``, by tag, by ``model=``, or not at all, with a legend.

Without the 256-colour palette -- or under a monochrome scheme -- the same
floor is drawn in letters, one per kind of device, with ``.`` for an empty
slot.  Colour pairs come from :func:`theme.shared_pairs`; should they run out,
the cells that have none are drawn in letters too and the footer says so.

The footer also says when the reader met something it could not read -- the
first of them, and how many -- since the floor drawn is then not quite the
one written.  Only what is on the screen is drawn, so a quarter of a million
elements costs a screenful.
"""

from __future__ import annotations

import curses
from dataclasses import dataclass

from . import theme
from .dclayout import NUMBERS, Element, Layout, int_attr, looks_like_layout, parse
from .filesystems import FileSystem
from .imageview import HALF_BLOCK, full_colour
from .theme import Pairs
from .util import read_key, truncate

#: The largest layout read.  dcviz's quarter-million-element example is a
#: few kilobytes; four megabytes of layout is a typo or not a layout at all.
MAX_BYTES = 4 * 1024 * 1024

#: How much of a ``.dc`` file is read to decide whether it is a layout.  The
#: quick view asks on every move of the cursor, over SFTP too.
PROBE_BYTES = 8 * 1024

#: What ``c`` cycles the colouring through.
COLOUR_MODES = ("role", "tag", "model", "off")

#: xterm-256 colours for the commonest kinds of device, in order: told apart
#: on a dark background and from one another, and none of them grey, which is
#: kept for "other", for nothing set, and for empty slots.
PALETTE = (39, 214, 41, 170, 203, 220, 51, 141, 113, 209)
OTHER = 246           # a kind of device past the palette's ten
UNSET = 252           # a device with no role (tag, model) at all
USED = 250            # any device, with colouring off
EMPTY = 238           # an empty U-slot
CAP = 234             # above a rack's top, where a half cell overhangs it
CURSOR = 231          # behind the cell under the cursor, on a plan

#: A rack's fill, from empty to full, for its one cell on a plan.
FILL = " ▁▂▃▄▅▆▇█"
#: A device on a plan, and a container of devices.
DEVICE = "█"
CONTAINER = "▓"
#: The cell under the cursor on a plan: its colour shows through, on white,
#: however full the rack is.
MARK = "▒"

#: The smallest screen the floor is drawn on.
MIN_WIDTH, MIN_HEIGHT = 30, 10

BACKSPACE = (curses.KEY_BACKSPACE, 127, 8)
ENTER = (10, 13, curses.KEY_ENTER)


# ---------------------------------------------------------------------------
# Claiming a file
# ---------------------------------------------------------------------------

def is_layout_name(name: str) -> bool:
    """Whether ``name`` could be a layout: it ends in ``.dc``, any case."""
    return name.lower().endswith(".dc")


def is_layout_file(fs: FileSystem, path: str) -> bool:
    """Whether the file at ``path`` opens like a layout.

    ``.dc`` is also a ``dc`` calculator script's extension, so the name is
    not enough: the first :data:`PROBE_BYTES` are read and the first line that
    is neither blank nor a comment has to start with ``dc``, ``title`` or
    ``model``.  A file that cannot be read is not claimed -- the text viewer
    will say why it cannot be read either.
    """
    try:
        reader = fs.open_read(path)
        try:
            data = reader.read(PROBE_BYTES)
        finally:
            reader.close()
    except Exception:
        return False
    # A line cut off at the end of the probe can only fail to match, never
    # match wrongly: what decides is its first word and the one after it.
    return looks_like_layout(data.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# What a device is, for colouring
# ---------------------------------------------------------------------------

def category(element: Element, mode: str) -> str | None:
    """What ``element`` counts as under colouring ``mode``."""
    if mode == "role":
        return element.get("role")
    if mode == "model":
        return element.get("model")
    if mode == "tag":
        return element.first_tag()
    return ""


def devices(element: Element):
    """The devices at and under ``element``: everything with nothing in it,
    except a rack, which is an empty place for devices rather than one."""
    stack = [element]
    while stack:
        current = stack.pop()
        if current.children:
            stack.extend(current.children)
        elif current.kind != "rack":
            yield current


@dataclass
class Swatch:
    """One entry of the legend: a kind of device and how it is drawn."""

    name: str
    colour: int
    letter: str
    count: int


class Legend:
    """The colours and letters of one colouring mode, for a whole layout."""

    def __init__(self, layout: Layout, mode: str) -> None:
        self.mode = mode
        counts: dict[str | None, int] = {}
        if layout.root is not None and mode != "off":
            for device in devices(layout.root):
                key = category(device, mode)
                counts[key] = counts.get(key, 0) + 1
        ranked = sorted((key for key in counts if key is not None),
                        key=lambda key: -counts[key])
        self.swatches: list[Swatch] = []
        self._by_name: dict[str | None, Swatch] = {}
        taken = {".", "-", "?", "#"}
        for index, name in enumerate(ranked[:len(PALETTE)]):
            letter = _letter(name, taken)
            swatch = Swatch(name, PALETTE[index], letter, counts[name])
            self.swatches.append(swatch)
            self._by_name[name] = swatch
        rest = ranked[len(PALETTE):]
        self.other = Swatch(f"{len(rest)} more", OTHER, "?",
                            sum(counts[name] for name in rest))
        self.unset = Swatch(f"no {mode}", UNSET, "-", counts.get(None, 0))
        self.used = Swatch("device", USED, "#", 0)

    def swatch(self, name: str | None) -> Swatch:
        """How a device of kind ``name`` is drawn."""
        if self.mode == "off":
            return self.used
        if name is None:
            return self.unset
        return self._by_name.get(name, self.other)

    def entries(self) -> list[Swatch]:
        """What the legend lists: the named kinds, then the leftovers."""
        out = list(self.swatches)
        if self.other.count:
            out.append(self.other)
        if self.unset.count:
            out.append(self.unset)
        return out


def _letter(name: str, taken: set[str]) -> str:
    """A letter for ``name`` on a screen without colour: its first one free."""
    for char in name.upper() + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        if char.isalnum() and char not in taken:
            taken.add(char)
            return char
    return "*"


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------

@dataclass
class PlanLine:
    """One line of a plan: an element and the cells drawn for it."""

    element: Element
    cells: list[Element]
    indent: int = 0
    header: bool = False


def _holds_racks(element: Element) -> bool:
    return any(child.kind == "rack" for child in element.children)


def _top(rack: Element) -> int:
    """The highest U a rack shows: its height, or a device above it."""
    return max([rack.u_height] + [child.u_at + child.u_size - 1
                                  for child in rack.children])


class FloorView:
    """A full-screen browser for a ``.dc`` layout, drawn as its floor."""

    def __init__(self, fs: FileSystem, path: str) -> None:
        self.fs = fs
        self.path = path
        self.name = fs.basename(path)
        self.error: str | None = None
        self.layout = Layout()
        self.notice = ""
        self.mode = 0
        self.pairs: Pairs | None = None
        #: Whether a cell had to be drawn in letters for want of a pair.
        self.ran_out = False
        self._legends: dict[str, Legend] = {}
        self._dominant: dict[tuple[str, int], tuple[str | None, int]] = {}
        self._kinds: dict[int, dict[str, int]] = {}
        self._colour = False
        #: The first row and column on screen, kept between draws.
        self.top = 0
        self.left = 0
        self._load()
        root = self.layout.root
        #: The element whose contents fill the screen.
        self.focus: Element | None = root
        #: The element under the cursor: one of those drawn.
        self.cursor: Element | None = root
        if root is not None:
            self.cursor = self._first_visible()

    def _load(self) -> None:
        try:
            reader = self.fs.open_read(self.path)
            try:
                data = reader.read(MAX_BYTES + 1)
            finally:
                reader.close()
        except Exception as exc:
            self.error = str(exc)
            return
        if len(data) > MAX_BYTES:
            self.error = (f"layout is larger than {MAX_BYTES // (1024 * 1024)} "
                          "MiB -- not read")
            return
        self.layout = parse(data.decode("utf-8", errors="replace"))

    # -- what is on the screen -----------------------------------------------
    def zoom(self) -> str:
        """``rack``, ``row`` or ``plan``: how the focus is drawn."""
        focus = self.focus
        if focus is None:
            return "plan"
        if focus.kind == "rack":
            return "rack"
        if _holds_racks(focus):
            return "row"
        return "plan"

    def plan_lines(self) -> list[PlanLine]:
        """The lines of the plan of the focus: one per child, or a header
        and a line per grandchild for a child holding containers."""
        assert self.focus is not None
        lines = []
        for child in self.focus.children:
            if (child.children and not _holds_racks(child)
                    and any(grand.children for grand in child.children)):
                lines.append(PlanLine(child, [], 0, header=True))
                for grand in child.children:
                    lines.append(PlanLine(grand, list(grand.children) or [grand], 2))
            else:
                lines.append(PlanLine(child, list(child.children) or [child]))
        return lines

    def visible(self) -> list[Element]:
        """The elements the cursor can be on at this zoom."""
        if self.focus is None:
            return []
        if self.zoom() == "plan":
            return [cell for line in self.plan_lines() for cell in line.cells]
        return list(self.focus.children)

    def _first_visible(self) -> Element | None:
        shown = self.visible()
        if not shown:
            return self.focus
        if self.zoom() == "rack":
            return max(shown, key=lambda element: element.u_at)
        return shown[0]

    def _settle(self) -> None:
        """Put the cursor on what is drawn: itself, or the ancestor of it
        that is, or else the first thing on the screen."""
        shown = {id(element) for element in self.visible()}
        element = self.cursor
        while element is not None and id(element) not in shown:
            element = element.parent
        self.cursor = element if element is not None else self._first_visible()

    # -- moving --------------------------------------------------------------
    def zoom_in(self) -> None:
        """Zoom in one level towards the cursor: to the element on the screen
        that holds it, or into it when it is one of the focus's own."""
        focus, cursor = self.focus, self.cursor
        if focus is None or cursor is None:
            return
        target = cursor
        while target is not focus and target.parent is not focus:
            target = target.parent
        # An empty rack still opens, on its empty slots.
        if target is focus or not (target.children or target.kind == "rack"):
            self.notice = f"nothing inside {cursor.id}"
            return
        self.focus = target
        self.top = self.left = 0
        if cursor is target:
            self.cursor = self._first_visible()
        else:
            self._settle()

    def zoom_out(self) -> bool:
        """Zoom out one level; False at the top already."""
        focus = self.focus
        if focus is None or focus.parent is None:
            return False
        self.focus = focus.parent
        self.top = self.left = 0
        self._settle()
        return True

    def move(self, dx: int, dy: int) -> None:
        """Move the cursor ``dx`` cells across and ``dy`` lines down."""
        if self.focus is None or self.cursor is None:
            return
        zoom = self.zoom()
        if zoom == "rack":
            if dx:
                self._next_sibling(self.focus, dx, "rack")
            else:
                self._step_slot(dy)
        elif zoom == "row":
            if dy:
                self._next_sibling(self.focus, dy, "row")
            else:
                self._step_list(self.focus.children, dx)
        else:
            self._step_plan(dx, dy)

    def _step_list(self, items: list[Element], delta: int) -> None:
        index = next((i for i, item in enumerate(items)
                      if item is self.cursor), 0)
        wanted = index + delta
        if not 0 <= wanted < len(items):
            self.notice = "no more this way"
            wanted = max(0, min(len(items) - 1, wanted))
        self.cursor = items[wanted]

    def _step_slot(self, delta: int) -> None:
        """Up and down a rack, from device to device by their slots."""
        assert self.focus is not None
        order = sorted(self.focus.children,
                       key=lambda element: -element.u_at)
        if not order:
            self.notice = "this rack is empty"
            return
        self._step_list(order, delta)

    def _next_sibling(self, focus: Element, delta: int, what: str) -> None:
        """Walk on to the next rack (or row) beside the one in focus."""
        parent = focus.parent
        if parent is None:
            self.notice = f"no other {what} here"
            return
        if what == "rack":
            kin = [child for child in parent.children if child.kind == "rack"]
        else:
            kin = [child for child in parent.children if _holds_racks(child)]
        index = next(i for i, item in enumerate(kin) if item is focus)
        wanted = index + delta
        if not 0 <= wanted < len(kin):
            self.notice = f"no more {what}s this way"
            return
        here = self._position(focus)
        self.focus = kin[wanted]
        self.top = 0
        self.cursor = self._at_position(here)

    def _position(self, focus: Element) -> int:
        """Where the cursor is inside the focus, to carry to its neighbour:
        the U it is at in a rack, its column in a row."""
        cursor = self.cursor
        if focus.kind == "rack":
            return cursor.u_at if cursor is not None and cursor is not focus else 0
        return next((i for i, child in enumerate(focus.children)
                     if child is cursor), 0)

    def _at_position(self, position: int) -> Element | None:
        focus = self.focus
        assert focus is not None
        if not focus.children:
            return focus
        if focus.kind == "rack":
            if not position:
                return self._first_visible()
            return min(focus.children,
                       key=lambda element: (abs(element.u_at - position),
                                            -element.u_at))
        return focus.children[min(position, len(focus.children) - 1)]

    def _step_plan(self, dx: int, dy: int) -> None:
        lines = [line for line in self.plan_lines() if not line.header]
        if not lines:
            self.notice = f"{self.cursor.id} holds nothing"
            return
        row = next((i for i, line in enumerate(lines)
                    if any(cell is self.cursor for cell in line.cells)), 0)
        column = next((i for i, cell in enumerate(lines[row].cells)
                       if cell is self.cursor), 0)
        if dy:
            wanted = row + dy
            if not 0 <= wanted < len(lines):
                self.notice = "no more this way"
                wanted = max(0, min(len(lines) - 1, wanted))
            cells = lines[wanted].cells
            self.cursor = cells[min(column, len(cells) - 1)]
        else:
            self._step_list(lines[row].cells, dx)

    def cycle_colour(self) -> None:
        self.mode = (self.mode + 1) % len(COLOUR_MODES)

    # -- what things are -----------------------------------------------------
    def legend(self) -> Legend:
        mode = COLOUR_MODES[self.mode]
        found = self._legends.get(mode)
        if found is None:
            found = self._legends[mode] = Legend(self.layout, mode)
        return found

    def dominant(self, element: Element) -> tuple[str | None, int]:
        """What ``element`` mostly holds, and how full it is in eighths.

        A rack is weighed by U, so one 4U box outweighs three 1U ones; a
        device is itself, and anything else is counted by its devices.
        """
        mode = COLOUR_MODES[self.mode]
        key = (mode, id(element))
        found = self._dominant.get(key)
        if found is not None:
            return found
        weights: dict[str | None, int] = {}
        if element.kind == "rack":
            for child in element.children:
                name = category(child, mode)
                weights[name] = weights.get(name, 0) + child.u_size
            used = sum(weights.values())
            fill = min(8, -(-used * 8 // max(1, element.u_height)))
        elif not element.children:
            weights[category(element, mode)] = 1
            fill = 8
        else:
            for device in devices(element):
                name = category(device, mode)
                weights[name] = weights.get(name, 0) + 1
            fill = 8 if weights else 0
        best = max(weights, key=lambda name: weights[name]) if weights else None
        found = self._dominant[key] = (best, fill)
        return found

    def kinds_under(self, element: Element) -> dict[str, int]:
        """How many elements of each kind ``element`` holds, itself apart."""
        found = self._kinds.get(id(element))
        if found is None:
            found = {}
            level = list(element.children)
            while level:                    # a level at a time: outermost first
                below = []
                for current in level:
                    found[current.kind] = found.get(current.kind, 0) + 1
                    below.extend(current.children)
                level = below
            self._kinds[id(element)] = found
        return found

    # -- drawing -------------------------------------------------------------
    def coloured(self) -> bool:
        """Whether the floor is drawn in colour or in letters."""
        return bool(full_colour() and theme.in_colour())

    def _attr(self, fg: int, bg: int) -> int | None:
        """The attribute for a cell in these colours, or None when there is
        no pair left for them."""
        if self.pairs is None:
            self.pairs = theme.shared_pairs()
        pair = self.pairs.find(fg, bg)
        if pair is None:
            self.ran_out = True
            return None
        return curses.color_pair(pair)

    def draw(self, win) -> None:
        theme.background(win, "edit")
        win.erase()
        self._colour = self.coloured()
        height, width = win.getmaxyx()
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            theme.paint(win, 0, 0, truncate(
                f" Floor view needs {MIN_WIDTH}x{MIN_HEIGHT}", width),
                "keybar", width)
            win.noutrefresh()
            return
        self._draw_title(win, width)
        body = (1, height - 5)                  # first row, row count
        if self.error is not None:
            theme.paint(win, 2, 2, truncate(
                f"Cannot show this layout: {self.error}", width - 4),
                "panelerror")
        elif self.focus is None:
            theme.paint(win, 2, 2, truncate(
                "This layout makes no elements.", width - 4), "edit")
        else:
            zoom = self.zoom()
            if zoom == "rack":
                self._draw_rack(win, body, width)
            elif zoom == "row":
                self._draw_row(win, body, width)
            else:
                self._draw_plan(win, body, width)
            self._draw_legend(win, height - 4, width)
            self._draw_inspector(win, height - 3, width)
        self._draw_footer(win, height - 1, width)
        win.noutrefresh()

    def _draw_title(self, win, width: int) -> None:
        title = f" Floor: {self.name}"
        if self.focus is not None:
            crumbs = [self.focus] + list(self.focus.ancestors())
            title += " -- " + " > ".join(element.id for element in
                                         reversed(crumbs))
            title += f"  [{self.zoom()}]"
        theme.paint(win, 0, 0, truncate(title + " ", width), "keybar", width)

    def _put(self, win, y: int, x: int, glyph: str, fg: int, bg: int,
             letter: str, mark: bool = False) -> None:
        """One cell, or a run of them: the glyph in its colours, or the
        letter when there is no colour (or no pair) for it.  ``mark``
        reverses the letter, for the cursor, where colour gives it a white
        background instead."""
        if self._colour:
            attr = self._attr(fg, bg)
            if attr is not None:
                self._write(win, y, x, glyph, attr)
                return
        self._write(win, y, x, letter,
                    theme.attr("edit") | (curses.A_REVERSE if mark else 0))

    @staticmethod
    def _write(win, y: int, x: int, text: str, attr: int) -> None:
        try:
            win.addstr(y, x, text, attr)
        except curses.error:
            pass        # the bottom-right cell of a window refuses a write

    # -- a plan --------------------------------------------------------------
    def _draw_plan(self, win, body: tuple[int, int], width: int) -> None:
        first, rows = body
        lines = self.plan_lines()
        if not lines:
            theme.paint(win, first + 1, 2, truncate(
                f"{self.focus.kind} {self.focus.id} holds nothing.", width - 4),
                "edit")
            return
        legend = self.legend()
        row = next((i for i, line in enumerate(lines)
                    if any(cell is self.cursor for cell in line.cells)), 0)
        self.top = max(0, min(self.top, row), row - rows + 1)
        label_width = min(14, max(len(line.element.id) + line.indent
                                  for line in lines))
        room = max(1, width - label_width - 2)
        # Two columns and a gap to a rack while the longest line fits, which
        # reads as a row of racks; one column each when it does not.
        longest = max(len(line.cells) for line in lines)
        size, step = (2, 3) if longest * 3 <= room else (1, 1)
        across = max(1, room // step)
        column = next((i for i, cell in enumerate(lines[row].cells)
                       if cell is self.cursor), 0)
        self.left = max(0, min(self.left, column), column - across + 1)
        for offset, line in enumerate(lines[self.top:self.top + rows]):
            y = first + offset
            mine = line is lines[row]
            if line.header:
                said = _tally(self.kinds_under(line.element))
                text = f"{line.element.id}  {line.element.name}" \
                    if line.element.name != line.element.id else line.element.id
                theme.paint(win, y, 0, truncate(f"{text}  {said}", width),
                            "editbold")
                continue
            label = " " * line.indent + line.element.id
            theme.paint(win, y, 0, truncate(label, label_width).ljust(label_width),
                        "editbold" if mine else "edit")
            x = label_width + 1
            cells = line.cells[self.left:self.left + across]
            if self.left and line.cells:
                theme.paint(win, y, x - 1, "<", "editdim")
            for index, cell in enumerate(cells):
                name, fill = self.dominant(cell)
                swatch = legend.swatch(name)
                if cell.kind == "rack":
                    # In letters: capital for a full rack, small for one
                    # partly filled, a dot for an empty one.
                    glyph = FILL[fill]
                    letter = ("." if not fill else swatch.letter if fill == 8
                              else swatch.letter.lower())
                elif cell.children:
                    # Nothing to show in one with no devices at all.
                    glyph, letter = ((CONTAINER, swatch.letter) if fill
                                     else (FILL[0], "."))
                else:
                    glyph, letter = DEVICE, swatch.letter
                if cell is self.cursor:
                    self._put(win, y, x + index * step, MARK * size,
                              swatch.colour, CURSOR, letter * size, mark=True)
                else:
                    self._put(win, y, x + index * step, glyph * size,
                              swatch.colour, EMPTY, letter * size)
            if len(line.cells) > self.left + across:
                theme.paint(win, y, width - 1, ">", "editdim")

    # -- a row of racks ------------------------------------------------------
    def _draw_row(self, win, body: tuple[int, int], width: int) -> None:
        first, rows = body
        columns = self.focus.children
        cell_width = max(2, min(4, max(len(child.id) for child in columns)))
        step = cell_width + 1
        fit = max(1, width // step)
        index = next((i for i, child in enumerate(columns)
                      if child is self.cursor), 0)
        self.left = max(0, min(self.left, index), index - fit + 1)
        shown = columns[self.left:self.left + fit]
        tall = max(_top(child) if child.kind == "rack" else
                   _height(child) for child in shown)
        room = max(1, rows - 1)                 # the last row is the labels
        per_half = max(1, -(-tall // (room * 2)))
        floor = first + rows - 2
        for offset, column in enumerate(shown):
            x = offset * step
            if column.kind == "rack":
                self._draw_rack_column(win, column, x, cell_width, floor,
                                       per_half, room)
            else:
                self._draw_box_column(win, column, x, cell_width, floor,
                                      per_half, room)
            label = truncate(column.id[-cell_width:], cell_width)
            attr = (theme.attr("editbold") | curses.A_REVERSE
                    if column is self.cursor else theme.attr("edit"))
            self._write(win, floor + 1, x, label.ljust(cell_width), attr)
        if self.left:
            theme.paint(win, floor + 1, width - 1, "<", "editdim")
        elif self.left + fit < len(columns):
            theme.paint(win, floor + 1, width - 1, ">", "editdim")

    def _slot_owners(self, rack: Element) -> dict[int, Element]:
        owners: dict[int, Element] = {}
        for child in rack.children:
            for slot in range(child.u_at, child.u_at + child.u_size):
                owners.setdefault(slot, child)
        return owners

    def _half(self, rack: Element, owners: dict[int, Element], low: int,
              high: int) -> tuple[int, str]:
        """The colour and letter of the U-slots ``low``..``high`` of a rack:
        what fills most of them, empty, or nothing (above the rack)."""
        legend = self.legend()
        weights: dict[str | None, int] = {}
        empty = 0
        for slot in range(low, high + 1):
            owner = owners.get(slot)
            if owner is not None:
                name = category(owner, legend.mode)
                weights[name] = weights.get(name, 0) + 1
            elif slot <= rack.u_height:
                empty += 1
        if weights and max(weights.values()) >= empty:
            swatch = legend.swatch(max(weights, key=lambda key: weights[key]))
            return swatch.colour, swatch.letter
        if empty:
            return EMPTY, "."
        return CAP, " "

    def _draw_rack_column(self, win, rack: Element, x: int, cell_width: int,
                          floor: int, per_half: int, room: int) -> None:
        owners = self._slot_owners(rack)
        top = _top(rack)
        cells = -(-top // (per_half * 2))
        for level in range(min(cells, room)):
            low = level * per_half * 2 + 1
            bottom = self._half(rack, owners, low, low + per_half - 1)
            upper = self._half(rack, owners, low + per_half,
                               low + 2 * per_half - 1)
            letter = bottom[1] if bottom[1] != "." else upper[1]
            if bottom[1] == " " and upper[1] == " ":
                continue
            self._put(win, floor - level, x, HALF_BLOCK * cell_width,
                      upper[0], bottom[0], letter * cell_width)

    def _draw_box_column(self, win, element: Element, x: int, cell_width: int,
                         floor: int, per_half: int, room: int) -> None:
        """Something in a row that is not a rack: a device as tall as its
        ``u=``, or a container, drawn as one block of what it holds."""
        name, _ = self.dominant(element)
        swatch = self.legend().swatch(name)
        glyph = CONTAINER if element.children else DEVICE
        cells = -(-_height(element) // (per_half * 2))
        for level in range(min(max(1, cells), room)):
            self._put(win, floor - level, x, glyph * cell_width,
                      swatch.colour, EMPTY, swatch.letter * cell_width)

    # -- one rack ------------------------------------------------------------
    def _draw_rack(self, win, body: tuple[int, int], width: int) -> None:
        first, rows = body
        rack = self.focus
        legend = self.legend()
        owners = self._slot_owners(rack)
        top = _top(rack)
        cursor = self.cursor if self.cursor is not rack else None
        bar = max(4, min(16, width // 4))
        if cursor is not None:
            # Keep the whole of the cursor's device on the screen.
            high = cursor.u_at + cursor.u_size - 1
            wanted_top = top - high
            self.top = max(0, min(self.top, wanted_top),
                           top - cursor.u_at - rows + 1)
        self.top = max(0, min(self.top, max(0, top - rows)))
        for offset in range(rows):
            slot = top - self.top - offset
            if slot < 1:
                break
            y = first + offset
            outside = slot > rack.u_height
            theme.paint(win, y, 0, f"{slot:>3}{'!' if outside else ' '}",
                        "panelerror" if outside else "editnum")
            owner = owners.get(slot)
            mine = owner is not None and owner is cursor
            edge = ">" if mine else ("|" if not outside else " ")
            theme.paint(win, y, 4, edge, "editbold" if mine else "editdim")
            if owner is None:
                self._put(win, y, 5, " " * bar, EMPTY, EMPTY, "." * bar)
            else:
                swatch = legend.swatch(category(owner, legend.mode))
                self._put(win, y, 5, DEVICE * bar, swatch.colour, EMPTY,
                          swatch.letter * bar)
            theme.paint(win, y, 5 + bar, "<" if mine else edge,
                        "editbold" if mine else "editdim")
            if owner is not None and slot == owner.u_at + owner.u_size - 1:
                label = owner.id
                sharing = [child for child in rack.children
                           if child is not owner and child.u_at <= slot
                           < child.u_at + child.u_size]
                if sharing:
                    label += f" (+{len(sharing)} in the same slot)"
                theme.paint(win, y, 7 + bar,
                            truncate(label, max(0, width - bar - 8)),
                            "editbold" if mine else "edit")

    # -- what is under the cursor --------------------------------------------
    def _draw_legend(self, win, y: int, width: int) -> None:
        legend = self.legend()
        x = 1
        mode = COLOUR_MODES[self.mode]
        x = theme.paint(win, y, x, f"{mode}: ", "editdim")
        if mode == "off":
            theme.paint(win, y, x, "colouring off", "editdim")
            return
        entries = legend.entries()
        for index, swatch in enumerate(entries):
            text = f" {swatch.name} {swatch.count:,}  "
            if x + 2 + len(text) > width - 8 and index < len(entries) - 1:
                theme.paint(win, y, x, f"+{len(entries) - index} more",
                            "editdim")
                break
            self._put(win, y, x, DEVICE * 2, swatch.colour, swatch.colour,
                      f"{swatch.letter}=")
            x = theme.paint(win, y, x + 2, truncate(text, max(0, width - x - 2)),
                            "edit")

    def _draw_inspector(self, win, y: int, width: int) -> None:
        element = self.cursor
        assert element is not None          # there is one while there is a focus
        head = f"{element.kind} {element.id}"
        if element.name != element.id:
            head += f' "{element.name}"'
        head += f"  {element.path}"
        if element.kind == "rack":
            used = sum(child.u_size for child in element.children)
            head += f"  {element.u_height}U, {used}U used"
        elif element.u_size:
            span = (f"U{element.u_at}" if element.u_size == 1 else
                    f"U{element.u_at}-{element.u_at + element.u_size - 1}")
            head += f"  {span}"
        if element.count > 1:
            head += "  holds " + _tally(self.kinds_under(element))
        head += f"  line {element.line}"
        theme.paint(win, y, 0, truncate(" " + head, width), "editbold", width)
        parts = []
        inherited = False
        for key, value, passed in element.attributes():
            parts.append(f"{'^' if passed else ''}{key}={value}")
            inherited = inherited or passed
        for tag, passed in element.all_tags():
            parts.append(f"{'^' if passed else ''}+{tag}")
            inherited = inherited or passed
        text = "  ".join(parts) if parts else "(no attributes or tags)"
        if inherited:
            text += "   (^ inherited)"
        theme.paint(win, y + 1, 0, truncate(" " + text, width), "edit", width)

    def _draw_footer(self, win, y: int, width: int) -> None:
        parts = []
        if self.notice:
            parts.append(self.notice)
        problems = self.layout.problems
        if problems:
            parts.append(f"! {len(problems)} not read: {problems[0]}")
        if self.ran_out:
            parts.append("colours ran out: letters shown")
        if self.error is not None or self.focus is None:
            parts.append("[q]uit")
        else:
            parts.append(f"arrows move  Enter in  Bksp out  "
                         f"[c]olour: {COLOUR_MODES[self.mode]}  [q]uit")
        theme.paint(win, y, 0, truncate(" " + "  |  ".join(parts) + " ", width),
                    "keybar", width)

    # -- the key loop --------------------------------------------------------
    def run(self, stdscr) -> None:
        curses.curs_set(0)
        height, width = stdscr.getmaxyx()
        win = curses.newwin(height, width, 0, 0)
        win.keypad(True)
        moves = {
            curses.KEY_LEFT: (-1, 0), ord("h"): (-1, 0),
            curses.KEY_RIGHT: (1, 0), ord("l"): (1, 0),
            curses.KEY_UP: (0, -1), ord("k"): (0, -1),
            curses.KEY_DOWN: (0, 1), ord("j"): (0, 1),
        }
        while True:
            self.draw(win)
            curses.doupdate()
            key = read_key(win)
            self.notice = ""
            if key in (ord("q"), ord("Q"), curses.KEY_F3, curses.KEY_F10):
                break
            if key == 27 or key in BACKSPACE:
                if not self.zoom_out():
                    if key == 27:
                        break
                    self.notice = "this is the whole floor"
            elif key in moves:
                self.move(*moves[key])
            elif key in ENTER:
                self.zoom_in()
            elif key in (ord("c"), ord("C")):
                self.cycle_colour()


def _tally(kinds: dict[str, int]) -> str:
    """``row: 4  rack: 24  node: 504`` -- counts by kind, outermost first."""
    return "  ".join(f"{kind}: {count:,}" for kind, count in kinds.items())


def _height(element: Element) -> int:
    """How tall something outside a rack is drawn, in U: its ``u=``, read as
    dcviz reads it, or one."""
    return int_attr(element.attrs.get("u"), 1, NUMBERS["u"])
