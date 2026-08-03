"""Draw images in a terminal, as coloured half-blocks.

A terminal cell is about twice as tall as it is wide, so treating one cell as
one pixel throws away half the resolution and squashes the picture.  Writing
``U+2580 UPPER HALF BLOCK`` instead gives two pixels per cell: the character's
foreground paints the top half and its background paints the bottom.  An
80x24 terminal is then 80x48 pixels, and square ones at that.

Colour comes from the xterm 256-colour palette -- the 6x6x6 cube plus its
24 greys -- because that is what terminals agree on.  Pairs are allocated
lazily as they are needed and cached: a full screen uses at most a few
thousand distinct (top, bottom) combinations, far fewer after quantisation,
while every possible pair would be 65536 and not all terminals have room.

Without colour there is still the luminance ramp, which is worth more than it
sounds: line art, diagrams and screenshots read perfectly well in twelve
shades of ASCII.
"""

from __future__ import annotations

import curses

from .filesystems import FileSystem
from .image import Image, ImageError, MAX_BYTES, decode
from .util import ljust, truncate

#: Upper half block: foreground is the top pixel, background the bottom.
HALF_BLOCK = "▀"

#: Darkest to lightest.  Ten steps is plenty; the eye cannot use more from a
#: single glyph, and a longer ramp mostly adds characters that look alike.
RAMP = " .:-=+*#%@"

#: The 6x6x6 colour cube's axis values, and where the 24 greys start.
CUBE_STEPS = (0, 95, 135, 175, 215, 255)
CUBE_BASE = 16
GREY_BASE = 232
GREY_COUNT = 24

#: Zoom steps, as a percentage of "fit the window".
ZOOM_STEPS = (100, 200, 400, 800)

#: Colours needed before the half-block rendering is worth using.  The palette
#: below indexes the xterm-256 cube, which an 8- or 16-colour terminal simply
#: does not have; there is no useful approximation of a photograph in eight
#: colours, so those terminals get the luminance ramp instead.
COLOURS_NEEDED = 256


def full_colour() -> bool:
    """Whether this terminal has the palette the half-block drawing needs."""
    return curses.has_colors() and getattr(curses, "COLORS", 0) >= COLOURS_NEEDED


def cube_index(value: int) -> int:
    """The 6x6x6 cube axis nearest to an 8-bit channel value."""
    best = 0
    for i, step in enumerate(CUBE_STEPS):
        if abs(step - value) < abs(CUBE_STEPS[best] - value):
            best = i
    return best


def palette_index(r: int, g: int, b: int) -> int:
    """The xterm-256 colour nearest to an RGB triple.

    The greys are checked as well as the cube, not out of fussiness: the cube
    holds only six grey points, so without them a photograph's midtones
    visibly step, and text screenshots band badly.
    """
    ri, gi, bi = cube_index(r), cube_index(g), cube_index(b)
    cube = (CUBE_STEPS[ri], CUBE_STEPS[gi], CUBE_STEPS[bi])
    cube_error = sum((a - c) ** 2 for a, c in zip((r, g, b), cube))

    # The grey ramp runs 8, 18, 28 ... 238 in even steps of ten.
    level = max(0, min(GREY_COUNT - 1, ((r + g + b) // 3 - 8) // 10))
    shade = 8 + level * 10
    grey_error = sum((a - shade) ** 2 for a in (r, g, b))
    if grey_error < cube_error:
        return GREY_BASE + level
    return CUBE_BASE + 36 * ri + 6 * gi + bi


def luminance(r: int, g: int, b: int) -> int:
    """Perceived brightness, 0-255, on the usual Rec. 601 weights."""
    return (r * 299 + g * 587 + b * 114) // 1000


class Pairs:
    """Lazily allocated curses colour pairs, keyed by (foreground, background).

    curses has a fixed number of pairs and no way to free one, so they are
    handed out on demand and reused.  When they run out the last colour is
    reused rather than failing: a slightly wrong shade in the corner of a
    photograph is not worth refusing to draw over.
    """

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit if limit is not None else getattr(
            curses, "COLOR_PAIRS", 256)
        self._pairs: dict[tuple[int, int], int] = {}
        self._next = 1
        self.exhausted = False

    def get(self, fg: int, bg: int) -> int:
        key = (fg, bg)
        found = self._pairs.get(key)
        if found is not None:
            return found
        if self._next >= self.limit:
            self.exhausted = True
            # Reuse whatever was allocated first; the alternative is not
            # drawing, and one wrong cell beats a blank screen.
            return 1 if self._pairs else 0
        index = self._next
        self._next += 1
        try:
            curses.init_pair(index, fg, bg)
        except (curses.error, ValueError, OverflowError):
            # Which of these a colour number past the terminal's range raises
            # depends on the Python version -- 3.9 gives curses.error, later
            # versions ValueError, and a very large number OverflowError.
            # None of them is a reason to stop drawing.
            self.exhausted = True
            return 0
        self._pairs[key] = index
        return index

    def __len__(self) -> int:
        return len(self._pairs)


def sample(image: Image, frame: int, scale_w: int, scale_h: int,
           off_x: int, off_y: int, out_w: int, out_h: int) -> list[list]:
    """``out_w`` x ``out_h`` RGB triples from a view of the scaled image.

    The image is treated as though resampled to ``scale_w`` x ``scale_h``, and
    the window starting at ``(off_x, off_y)`` in that space is what comes back.
    Only the visible pixels are computed, so zooming in costs no more than
    zooming out -- which matters, because the scaled image at maximum zoom is
    far larger than the screen.

    Nearest-neighbour rather than an averaging filter, deliberately: at these
    sizes an average turns the one-pixel lines in a diagram or a screenshot
    into grey smudges, while nearest-neighbour keeps them.
    """
    pixels = image.frames[frame].pixels
    rows = []
    for y in range(out_h):
        at_y = y + off_y
        source_y = min(image.height - 1, max(0, at_y * image.height // scale_h))
        base = source_y * image.width * 3
        row = []
        for x in range(out_w):
            at_x = x + off_x
            source_x = min(image.width - 1,
                           max(0, at_x * image.width // scale_w))
            at = base + source_x * 3
            row.append((pixels[at], pixels[at + 1], pixels[at + 2]))
        rows.append(row)
    return rows


def fit(image_w: int, image_h: int, cols: int, rows: int,
        zoom: int = 100) -> tuple[int, int]:
    """Pixel size that fits ``cols`` x ``rows`` cells, keeping the aspect ratio.

    Rows count double because of the half-block: a cell is two pixels tall.
    """
    if image_w <= 0 or image_h <= 0 or cols <= 0 or rows <= 0:
        return 0, 0
    room_w, room_h = cols, rows * 2
    scale = min(room_w / image_w, room_h / image_h) * zoom / 100
    return max(1, round(image_w * scale)), max(1, round(image_h * scale))


class ImageView:
    """A full-screen image browser: half-block colour, panning and zoom."""

    def __init__(self, fs: FileSystem, path: str) -> None:
        self.fs = fs
        self.path = path
        self.name = fs.basename(path)
        self.error: str | None = None
        self.image: Image | None = None
        self.frame = 0
        self.zoom = 0                   # index into ZOOM_STEPS
        self.off_x = 0
        self.off_y = 0
        self.colour = True
        self.notice = ""
        self.pairs: Pairs | None = None
        self._load()

    def _load(self) -> None:
        try:
            with self.fs.open_read(self.path) as reader:
                data = reader.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ImageError(
                    f"image is larger than {MAX_BYTES // (1024 * 1024)} MB")
            self.image = decode(data)
        except ImageError as exc:
            self.error = str(exc)
        except Exception as exc:        # the backend failed, or the file went
            self.error = str(exc)

    # -- geometry -----------------------------------------------------------
    def scaled(self, cols: int, rows: int) -> tuple[int, int]:
        """The image's size in pixels at the current zoom."""
        if self.image is None:
            return 0, 0
        return fit(self.image.width, self.image.height, cols, rows,
                   ZOOM_STEPS[self.zoom])

    def clamp(self, cols: int, rows: int) -> None:
        """Keep the view inside the image, and centre it when it fits."""
        scale_w, scale_h = self.scaled(cols, rows)
        self.off_x = max(0, min(self.off_x, scale_w - cols))
        self.off_y = max(0, min(self.off_y, scale_h - rows * 2))
        if scale_w <= cols:
            self.off_x = -((cols - scale_w) // 2)
        if scale_h <= rows * 2:
            self.off_y = -((rows * 2 - scale_h) // 2)

    def pan(self, dx: int, dy: int, cols: int, rows: int) -> None:
        self.off_x += dx
        self.off_y += dy
        self.clamp(cols, rows)

    def set_zoom(self, index: int, cols: int, rows: int) -> None:
        """Change zoom about the centre of the view, not about its corner."""
        old_w, old_h = self.scaled(cols, rows)
        centre_x = (self.off_x + cols / 2) / max(1, old_w)
        centre_y = (self.off_y + rows) / max(1, old_h)
        self.zoom = max(0, min(len(ZOOM_STEPS) - 1, index))
        new_w, new_h = self.scaled(cols, rows)
        self.off_x = round(centre_x * new_w - cols / 2)
        self.off_y = round(centre_y * new_h - rows)
        self.clamp(cols, rows)

    def step_frame(self, delta: int) -> None:
        if self.image is None or not self.image.animated:
            self.notice = " this image has only one frame "
            return
        self.frame = (self.frame + delta) % len(self.image.frames)

    # -- drawing ------------------------------------------------------------
    def cell(self, top, bottom) -> tuple[str, int]:
        """The character and attribute for one cell, given its two pixels."""
        if not self.colour or not full_colour():
            # One glyph, chosen by the mean brightness of the two pixels.
            level = (luminance(*top) + luminance(*bottom)) // 2
            return RAMP[level * (len(RAMP) - 1) // 255], curses.A_NORMAL
        assert self.pairs is not None
        pair = self.pairs.get(palette_index(*top), palette_index(*bottom))
        return HALF_BLOCK, curses.color_pair(pair)

    def draw(self, win) -> None:
        win.erase()
        height, width = win.getmaxyx()
        body = max(0, height - 2)
        self._draw_title(win, width)
        if self.error or self.image is None:
            self._draw_lines(win, width, body,
                             [f"Cannot show this image: {self.error}"])
        elif not self.image.frames:
            self._draw_lines(win, width, body, [
                f"{self.image.kind}"
                + (f", {self.image.size[0]}x{self.image.size[1]}"
                   if self.image.width else ""),
                "",
                self.image.note,
            ])
        else:
            self._draw_image(win, width, body)
        self._draw_footer(win, height, width)
        win.noutrefresh()

    def _draw_lines(self, win, width: int, body: int, lines) -> None:
        for i, line in enumerate(lines):
            if i + 1 > body:
                break
            try:
                win.addstr(i + 1, 2, truncate(line, max(0, width - 4)))
            except curses.error:
                pass

    def _draw_image(self, win, width: int, body: int) -> None:
        if body <= 0 or width <= 0:
            return
        if self.pairs is None:
            self.pairs = Pairs()
        self.clamp(width, body)
        scale_w, scale_h = self.scaled(width, body)
        rows = sample(self.image, self.frame, scale_w, scale_h,
                      self.off_x, self.off_y, width, body * 2)
        blank = (0, 0, 0)
        for y in range(body):
            for x in range(width):
                # Outside the image (when it is smaller than the window) the
                # cell is left as the terminal's own background.
                if not self._inside(x, y * 2, scale_w, scale_h) and \
                        not self._inside(x, y * 2 + 1, scale_w, scale_h):
                    continue
                top = rows[y * 2][x] if self._inside(x, y * 2, scale_w,
                                                     scale_h) else blank
                bottom = (rows[y * 2 + 1][x]
                          if self._inside(x, y * 2 + 1, scale_w, scale_h)
                          else blank)
                glyph, attr = self.cell(top, bottom)
                try:
                    win.addstr(y + 1, x, glyph, attr)
                except curses.error:
                    # The bottom-right cell of a window cannot be written to
                    # without scrolling; every terminal refuses it.
                    pass

    def _inside(self, x: int, y: int, scale_w: int, scale_h: int) -> bool:
        return (0 <= x + self.off_x < scale_w
                and 0 <= y + self.off_y < scale_h)

    def _draw_title(self, win, width: int) -> None:
        title = f" Image: {self.name}"
        if self.image is not None and not self.error:
            width_, height_ = self.image.size
            title += f" -- {self.image.kind} {width_}x{height_}"
            if self.image.detail:
                title += f" ({self.image.detail})"
            if self.image.reduced:
                title += " shown at 1/8"
        title += " "
        win.attrset(curses.A_REVERSE)
        try:
            win.addstr(0, 0, ljust(title, width))
        except curses.error:
            pass
        win.attrset(curses.A_NORMAL)

    def _draw_footer(self, win, height: int, width: int) -> None:
        if self.notice:
            hint = self.notice
        elif self.error or self.image is None or not self.image.frames:
            hint = "[q]uit"
        else:
            parts = [f"{ZOOM_STEPS[self.zoom]}%"]
            if self.image.animated:
                parts.append(f"frame {self.frame + 1}/{len(self.image.frames)}")
                if self.image.truncated:
                    parts.append("[truncated]")
            if not full_colour():
                parts.append("no 256-colour palette")
            elif not self.colour:
                parts.append("ASCII")
            if self.pairs is not None and self.pairs.exhausted:
                parts.append("colours exhausted")
            parts.append("arrows pan  +/- zoom  [c]olour  n/p frame  [q]uit")
            hint = "  ".join(parts)
        win.attrset(curses.A_REVERSE)
        try:
            win.addstr(height - 1, 0, ljust(f" {hint} ", width))
        except curses.error:
            pass
        win.attrset(curses.A_NORMAL)

    # -- interaction --------------------------------------------------------
    def run(self, stdscr) -> None:
        curses.curs_set(0)
        height, width = stdscr.getmaxyx()
        win = curses.newwin(height, width, 0, 0)
        win.keypad(True)
        while True:
            self.draw(win)
            curses.doupdate()
            height, width = stdscr.getmaxyx()
            body = max(1, height - 2)
            step = max(1, width // 8)
            key = win.getch()
            self.notice = ""
            if key in (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10):
                break
            elif key in (curses.KEY_LEFT, ord("h")):
                self.pan(-step, 0, width, body)
            elif key in (curses.KEY_RIGHT, ord("l")):
                self.pan(step, 0, width, body)
            elif key in (curses.KEY_UP, ord("k")):
                self.pan(0, -step, width, body)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.pan(0, step, width, body)
            elif key in (ord("+"), ord("=")):
                self.set_zoom(self.zoom + 1, width, body)
            elif key in (ord("-"), ord("_")):
                self.set_zoom(self.zoom - 1, width, body)
            elif key in (ord("f"), ord("F"), curses.KEY_HOME):
                self.set_zoom(0, width, body)
            elif key in (ord("n"), ord(" ")):
                self.step_frame(1)
            elif key in (ord("p"), ord("N")):
                self.step_frame(-1)
            elif key in (ord("c"), ord("C")):
                self.colour = not self.colour
