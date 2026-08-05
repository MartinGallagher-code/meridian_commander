"""Real pixels in the terminal: the Sixel and kitty graphics protocols.

:mod:`~meridian_commander.imageview` draws with half-block characters, which
works in any terminal and is always the fallback.  But a cell is a coarse
pixel, and the xterm-256 palette is a coarse colour, so a photograph comes out
recognisable rather than right.  Terminals that speak a graphics protocol can
be handed the actual bitmap instead.

Two protocols cover the field:

* **Sixel** -- xterm, foot, mlterm, WezTerm, mintty, contour, iTerm2 and
  recent Windows Terminal.  A palette of at most 256 colours and a run of
  six-pixel-tall columns.
* **kitty** -- kitty, Ghostty, Konsole, WezTerm.  Takes raw RGB, so there is
  nothing to quantise and nothing to lose.

Both are written here from the RGB buffer :mod:`~meridian_commander.image`
already produces, so this module adds no dependency: the standard library has
base64 and that is all either protocol needs.

The awkward part is not the encoding, it is living with curses.  curses owns
the screen and knows nothing about these escape sequences, so anything drawn
this way is invisible to it and is wiped the next time it repaints the cells
underneath.  :func:`emit` therefore writes straight to the terminal *after*
curses has flushed, and the caller redraws the picture on every pass rather
than assuming it survived.
"""

from __future__ import annotations

import base64
import os
import sys

#: Protocol names, in the order :func:`detect` prefers them.  kitty comes
#: first where both are available: it takes RGB directly, so it neither
#: quantises to 256 colours nor pays for the palette.
KITTY = "kitty"
SIXEL = "sixel"
PROTOCOLS = (KITTY, SIXEL)

#: Consulted before anything is sniffed.  "off" forces the half-block
#: renderer, a protocol name forces that protocol, "auto" (or unset) detects.
OVERRIDE_ENV = "MERIDIAN_GRAPHICS"

#: Assumed when the terminal will not say how big a cell is.  8x16 is the
#: classic VGA text cell and close enough that a picture sized with it is a
#: little off rather than wrong; refusing to draw would be worse.
DEFAULT_CELL = (8, 16)

#: kitty's escape carries at most 4096 bytes of base64 per chunk.
KITTY_CHUNK = 4096

#: Sixel colour registers.  256 is what the protocol guarantees and what the
#: xterm-256 quantiser produces anyway.
SIXEL_COLOURS = 256


# ---------------------------------------------------------------------------
# Which protocol, if any
# ---------------------------------------------------------------------------

def detect(env: dict | None = None) -> str | None:
    """The graphics protocol this terminal speaks, or None for neither.

    Sniffed from the environment rather than by querying the terminal.  A
    query means writing an escape and reading the reply, which races with
    curses over the same input stream and hangs on a terminal that answers
    neither way -- a bad trade for a feature that has a working fallback.
    The environment is less certain but never blocks, and ``MERIDIAN_GRAPHICS``
    is there for the terminal we guess wrong about.
    """
    env = os.environ if env is None else env

    forced = (env.get(OVERRIDE_ENV) or "").strip().lower()
    if forced in ("off", "none", "no", "0"):
        return None
    if forced in PROTOCOLS:
        return forced

    term = (env.get("TERM") or "").lower()
    program = (env.get("TERM_PROGRAM") or "").lower()

    # kitty and the terminals that implement its protocol.
    if env.get("KITTY_WINDOW_ID") or "kitty" in term or "ghostty" in term:
        return KITTY
    if program in ("ghostty", "kitty"):
        return KITTY

    # Sixel.  A TERM naming it settles the matter; otherwise it is the
    # terminals known to ship it on by default.
    if "sixel" in term or "mlterm" in term or "foot" in term:
        return SIXEL
    if program in ("wezterm", "iterm.app", "mintty", "contour"):
        return SIXEL
    if env.get("WEZTERM_EXECUTABLE") or env.get("WEZTERM_PANE"):
        return SIXEL

    return None


def cell_pixels(fd: int = 1) -> tuple[int, int] | None:
    """How many pixels wide and tall one character cell is, or None.

    From ``TIOCGWINSZ``, whose last two fields are the window's pixel size --
    zero on the terminals that do not track it, which is why this can answer
    None.  Both protocols need it: they are told a size in pixels but occupy a
    whole number of cells, and getting the ratio wrong skews the picture.
    """
    try:
        import fcntl
        import struct
        import termios
    except ImportError:                      # not a POSIX terminal
        return None
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, xpixels, ypixels = struct.unpack("HHHH", packed)
    except Exception:
        return None
    if rows and cols and xpixels and ypixels:
        return max(1, xpixels // cols), max(1, ypixels // rows)
    return None


# ---------------------------------------------------------------------------
# Sixel
# ---------------------------------------------------------------------------
#
# A sixel is a column of six vertically stacked pixels encoded in one
# character: bit 0 is the top pixel, and the byte is 0x3f plus the bitmask, so
# every value lands in printable ASCII.  The image is therefore written in
# bands six rows tall.  Within a band each colour is drawn in its own pass --
# select it with "#n", lay down the sixels it covers, then "$" to return to
# the left of the same band for the next colour -- and "-" ends the band.
#
# Only the colours actually present in a band are passed over, and runs are
# compressed with "!count", which together are what keep the output a sensible
# size: without them every band would cost width bytes per palette entry.

SIXEL_START = b"\x1bPq"
SIXEL_END = b"\x1b\\"


def _percent(value: int) -> int:
    """An 8-bit channel as the whole percent nearest to it."""
    return (value * 100 + 127) // 255


def _sixel_runs(masks: list[int]) -> bytes:
    """One colour's row of six-pixel columns, run-length encoded.

    Trailing empty columns paint nothing and are dropped -- *before* encoding,
    not after.  Trimming them from the finished string would take the "?" that
    a repeat introducer was counting and leave "!5" behind with nothing to
    repeat, which reads as a run of whatever byte happens to follow.
    """
    end = len(masks)
    while end and masks[end - 1] == 0:
        end -= 1
    masks = masks[:end]

    out = bytearray()
    run_char = -1
    run = 0

    def flush() -> None:
        if run <= 0:
            return
        char = 0x3F + run_char
        # "!1x" is longer than "x" and "!2x" no shorter than "xx"; below four
        # the repeat introducer costs more than it saves.
        if run < 4:
            out.extend(bytes([char]) * run)
        else:
            out.extend(b"!%d%c" % (run, char))

    for mask in masks:
        if mask == run_char:
            run += 1
            continue
        flush()
        run_char, run = mask, 1
    flush()
    return bytes(out)


def sixel_encode(rows: list[list[tuple[int, int, int]]], width: int, height: int,
                 index_of, rgb_of) -> bytes:
    """A complete sixel sequence for ``rows`` of RGB triples.

    ``index_of(r, g, b)`` maps a pixel to a palette entry and ``rgb_of(index)``
    maps that entry back to the colour to declare for it.  Passing them in
    keeps the quantiser out of here: :mod:`imageview` already has one for the
    half-block renderer, and using the same one means the two views of an image
    differ in resolution rather than in hue.
    """
    out = bytearray(SIXEL_START)
    # Raster attributes: square pixels (1:1), then the size, so the terminal
    # knows how much room to leave before any pixels arrive.
    out += b'"1;1;%d;%d' % (max(0, width), max(0, height))

    registers: dict[int, int] = {}

    def register(colour: int) -> int:
        """The sixel register holding ``colour``, declaring it on first use."""
        known = registers.get(colour)
        if known is not None:
            return known
        number = len(registers)
        if number >= SIXEL_COLOURS:
            # Out of registers: reuse the first.  Only reachable if the
            # quantiser hands back more colours than it promised.
            return 0
        registers[colour] = number
        r, g, b = rgb_of(colour)
        # Sixel channels are percentages, not bytes, so 0-255 cannot survive
        # the trip exactly -- 95 is 37.25%, and only whole percents may be
        # written.  Rounding rather than truncating keeps the error under
        # half a percent instead of biasing every channel downwards.
        out.extend(b"#%d;2;%d;%d;%d" % (number, _percent(r), _percent(g),
                                        _percent(b)))
        return number

    for top in range(0, height, 6):
        band: dict[int, list[int]] = {}
        for offset in range(6):
            y = top + offset
            if y >= height:
                break
            row = rows[y]
            bit = 1 << offset
            for x in range(min(width, len(row))):
                colour = index_of(*row[x])
                masks = band.get(colour)
                if masks is None:
                    masks = band[colour] = [0] * width
                masks[x] |= bit

        for position, (colour, masks) in enumerate(sorted(band.items())):
            if position:
                out += b"$"                  # back to the left of this band
            out += b"#%d" % register(colour)
            out += _sixel_runs(masks)
        out += b"-"                          # next band

    out += SIXEL_END
    return bytes(out)


# ---------------------------------------------------------------------------
# kitty
# ---------------------------------------------------------------------------
#
# One escape per chunk: a=T transmits and displays in one go, f=24 says the
# payload is raw RGB triples, s/v are its pixel dimensions and c/r the cells
# to draw it in.  m=1 means another chunk follows.
#
# q=2 matters more than it looks: without it the terminal replies to every
# transmission, and those replies arrive on the input stream curses is reading
# keys from, where they show up as a burst of junk keystrokes.

KITTY_START = b"\x1b_G"
KITTY_END = b"\x1b\\"


def kitty_encode(pixels: bytes, width: int, height: int,
                 cols: int, rows: int) -> bytes:
    """A kitty graphics sequence displaying ``width`` x ``height`` RGB pixels."""
    payload = base64.b64encode(pixels)
    out = bytearray()
    first = True
    view = memoryview(payload)
    # A zero-byte image still needs one (empty) chunk, or nothing is sent.
    for start in range(0, max(1, len(payload)), KITTY_CHUNK):
        chunk = bytes(view[start:start + KITTY_CHUNK])
        more = 1 if start + KITTY_CHUNK < len(payload) else 0
        out += KITTY_START
        if first:
            out += b"a=T,q=2,f=24,s=%d,v=%d,c=%d,r=%d,m=%d" % (
                width, height, cols, rows, more)
            first = False
        else:
            out += b"m=%d" % more
        out += b";" + chunk + KITTY_END
    return bytes(out)


def kitty_clear() -> bytes:
    """Delete every image kitty is showing, so a redraw leaves nothing behind."""
    return KITTY_START + b"a=d,q=2" + KITTY_END


# ---------------------------------------------------------------------------
# Putting it on the screen
# ---------------------------------------------------------------------------

def cursor_to(row: int, col: int) -> bytes:
    """Move the cursor, in the 0-based coordinates curses uses."""
    return b"\x1b[%d;%dH" % (row + 1, col + 1)


def emit(payload: bytes, row: int, col: int, write=None) -> None:
    """Park the cursor at ``(row, col)`` and write a graphics sequence there.

    Call this *after* ``curses.doupdate()``.  curses cannot see any of this and
    will happily paint over it on the next repaint, which is exactly what makes
    a stale picture disappear -- so the caller redraws every pass instead of
    trying to keep one alive.
    """
    if write is None:
        stream = getattr(sys.stdout, "buffer", None)
        if stream is None:                   # a text-only stand-in under test
            return
        write = stream.write
        flush = stream.flush
    else:
        flush = getattr(write, "flush", None)
    write(cursor_to(row, col) + payload)
    if flush is not None:
        try:
            flush()
        except Exception:
            pass
