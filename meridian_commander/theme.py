"""The Turbo Vision colour engine: sixteen colours, named roles, and chrome.

Everything the application draws is expressed in the sixteen colours an EGA
card could show, because that is the whole reason the Borland IDEs looked the
way they did.  A *scheme* names roles ("what colour is a dialog button"); this
module turns those names into curses attributes, and hands out the handful of
drawing primitives -- framed boxes, drop shadows, scrollbars, hot-key captions
-- that the rest of the program builds its screens from.

The rules the look depends on, in one place:

* Backgrounds are explicit.  A terminal's own colours are never borrowed, so
  the desktop is Borland blue whatever the user's palette happens to be.
* A role that cannot be given a colour pair -- a monochrome terminal, or one
  that has run out of pairs -- falls back to a monochrome attribute chosen for
  that role, so the layout survives even when the colours do not.
* Frames are drawn with box characters when the locale can encode them and
  with ASCII when it cannot, which keeps the program usable over a connection
  that is still speaking Latin-1.
"""

from __future__ import annotations

import codecs
import curses
import locale
import os

# ---------------------------------------------------------------------------
# The palette
# ---------------------------------------------------------------------------

#: The IBM EGA/VGA text palette: name -> (ANSI colour index, bright).
#: The indices are the ANSI order, which is not the EGA order -- brown and
#: cyan swap places, and the bright half is offset by eight.
EGA: dict[str, tuple[int, bool]] = {
    "black": (0, False),
    "blue": (4, False),
    "green": (2, False),
    "cyan": (6, False),
    "red": (1, False),
    "magenta": (5, False),
    "brown": (3, False),
    "lightgray": (7, False),
    "darkgray": (0, True),
    "lightblue": (4, True),
    "lightgreen": (2, True),
    "lightcyan": (6, True),
    "lightred": (1, True),
    "lightmagenta": (5, True),
    "yellow": (3, True),
    "white": (7, True),
}

# ---------------------------------------------------------------------------
# The roles
# ---------------------------------------------------------------------------
#
# Each role is (foreground, background, attributes, monochrome attributes).
# The monochrome column is what the role becomes on a terminal with no colour
# at all: the point is that the *shape* of the screen -- which strip is chrome,
# which row is the highlight bar -- still reads.

_NORMAL = curses.A_NORMAL

ROLES: dict[str, tuple[str, str, int, int]] = {
    # -- the desktop behind the panels ------------------------------------
    "desktop": ("darkgray", "blue", 0, _NORMAL),
    "hint": ("lightgray", "blue", 0, _NORMAL),
    "hintkey": ("yellow", "blue", curses.A_BOLD, curses.A_BOLD),

    # -- the grey chrome: menu bar and key bar ----------------------------
    "menu": ("black", "lightgray", 0, curses.A_REVERSE),
    "menuhot": ("red", "lightgray", 0, curses.A_REVERSE | curses.A_BOLD),
    "menusel": ("white", "green", 0, _NORMAL),
    "menuselhot": ("lightred", "green", 0, curses.A_BOLD),
    "menudim": ("darkgray", "lightgray", 0, curses.A_REVERSE | curses.A_DIM),
    "menushortcut": ("black", "lightgray", 0, curses.A_REVERSE),
    "menusep": ("darkgray", "lightgray", 0, curses.A_REVERSE),
    "clock": ("black", "lightgray", 0, curses.A_REVERSE),
    "keybar": ("black", "lightgray", 0, curses.A_REVERSE),
    "keybarkey": ("black", "lightgray", curses.A_BOLD,
                  curses.A_REVERSE | curses.A_BOLD),

    # -- window frames -----------------------------------------------------
    "frame": ("lightgray", "blue", 0, curses.A_BOLD),
    "framenc": ("darkgray", "blue", 0, curses.A_DIM),
    "title": ("yellow", "blue", curses.A_BOLD, curses.A_BOLD),
    "titlenc": ("lightgray", "blue", 0, curses.A_DIM),
    "scroll": ("lightgray", "blue", 0, curses.A_DIM),
    "thumb": ("yellow", "blue", 0, curses.A_BOLD),

    # -- a directory listing ----------------------------------------------
    "panel": ("lightgray", "blue", 0, _NORMAL),
    "paneldir": ("white", "blue", curses.A_BOLD, curses.A_BOLD),
    "panellink": ("lightcyan", "blue", 0, curses.A_DIM),
    "paneltag": ("yellow", "blue", curses.A_BOLD, curses.A_BOLD),
    "panelcursor": ("black", "cyan", 0, curses.A_REVERSE),
    "panelcursordir": ("black", "cyan", curses.A_BOLD,
                       curses.A_REVERSE | curses.A_BOLD),
    "panelcursortag": ("yellow", "cyan", curses.A_BOLD,
                       curses.A_REVERSE | curses.A_BOLD),
    "panelhead": ("lightcyan", "blue", 0, curses.A_UNDERLINE),
    "panelinfo": ("lightgray", "blue", 0, _NORMAL),
    "panelerror": ("lightred", "blue", curses.A_BOLD, curses.A_BOLD),

    # -- dialogs -----------------------------------------------------------
    "dialog": ("black", "lightgray", 0, curses.A_REVERSE),
    "dialogframe": ("black", "lightgray", 0, curses.A_REVERSE),
    "dialogtitle": ("black", "lightgray", curses.A_BOLD,
                    curses.A_REVERSE | curses.A_BOLD),
    "dialoghot": ("red", "lightgray", 0, curses.A_REVERSE | curses.A_BOLD),
    "dialogdim": ("darkgray", "lightgray", 0, curses.A_REVERSE | curses.A_DIM),
    "dialogerror": ("red", "lightgray", curses.A_BOLD,
                    curses.A_REVERSE | curses.A_BOLD),
    "input": ("white", "blue", 0, _NORMAL),
    "inputcursor": ("blue", "white", 0, curses.A_REVERSE),
    "button": ("black", "green", 0, _NORMAL),
    "buttonsel": ("black", "lightgreen", curses.A_BOLD, curses.A_REVERSE),
    "buttonhot": ("red", "green", 0, curses.A_BOLD),
    "buttonhotsel": ("red", "lightgreen", curses.A_BOLD,
                     curses.A_REVERSE | curses.A_BOLD),
    "sel": ("white", "green", 0, curses.A_REVERSE),
    "selhot": ("lightred", "green", 0, curses.A_REVERSE | curses.A_BOLD),
    "shadow": ("darkgray", "black", 0, curses.A_DIM),

    # -- the text surfaces: viewer, editor, tabular views ------------------
    "edit": ("yellow", "blue", 0, _NORMAL),
    "editdim": ("lightgray", "blue", 0, curses.A_DIM),
    "editnum": ("darkgray", "blue", 0, curses.A_DIM),
    "editmark": ("black", "lightgray", 0, curses.A_REVERSE),
    "editmatch": ("white", "brown", 0, curses.A_REVERSE),
    "editbold": ("white", "blue", curses.A_BOLD, curses.A_BOLD),
    "editem": ("lightcyan", "blue", 0,
               getattr(curses, "A_ITALIC", curses.A_UNDERLINE)),
    "editcode": ("black", "lightgray", 0, curses.A_REVERSE),
    "editlink": ("lightgreen", "blue", 0, curses.A_UNDERLINE),
    "editheading": ("lightgreen", "blue", curses.A_BOLD, curses.A_BOLD),
}

#: The scheme used when none is asked for, or when one is asked for by a name
#: nobody has.
DEFAULT_SCHEME = "turbo"

#: The bundled schemes.  A scheme lists only what it changes; everything else
#: falls through to :data:`ROLES`, so a new scheme is a short dict rather than
#: a second copy of the whole table.
SCHEMES: dict[str, dict[str, tuple[str, str, int, int]]] = {
    # Turbo C++ 3.0: yellow on blue, grey chrome.  The reference look.
    "turbo": {},

    # After hours.  Black ground, the same grey chrome, easier on a modern
    # panel that renders Borland blue rather brighter than a CRT did.
    "midnight": {
        "desktop": ("darkgray", "black", 0, _NORMAL),
        "hint": ("lightgray", "black", 0, _NORMAL),
        "hintkey": ("yellow", "black", curses.A_BOLD, curses.A_BOLD),
        "frame": ("darkgray", "black", 0, curses.A_BOLD),
        "framenc": ("darkgray", "black", 0, curses.A_DIM),
        "title": ("yellow", "black", curses.A_BOLD, curses.A_BOLD),
        "titlenc": ("lightgray", "black", 0, curses.A_DIM),
        "scroll": ("darkgray", "black", 0, curses.A_DIM),
        "thumb": ("yellow", "black", 0, curses.A_BOLD),
        "panel": ("lightgray", "black", 0, _NORMAL),
        "paneldir": ("lightcyan", "black", curses.A_BOLD, curses.A_BOLD),
        "panellink": ("cyan", "black", 0, curses.A_DIM),
        "paneltag": ("yellow", "black", curses.A_BOLD, curses.A_BOLD),
        "panelcursor": ("black", "lightgray", 0, curses.A_REVERSE),
        "panelcursordir": ("black", "lightgray", curses.A_BOLD,
                           curses.A_REVERSE | curses.A_BOLD),
        "panelcursortag": ("brown", "lightgray", curses.A_BOLD,
                           curses.A_REVERSE | curses.A_BOLD),
        "panelhead": ("lightcyan", "black", 0, curses.A_UNDERLINE),
        "panelinfo": ("lightgray", "black", 0, _NORMAL),
        "panelerror": ("lightred", "black", curses.A_BOLD, curses.A_BOLD),
        "input": ("white", "darkgray", 0, _NORMAL),
        "inputcursor": ("darkgray", "white", 0, curses.A_REVERSE),
        "edit": ("lightgray", "black", 0, _NORMAL),
        "editdim": ("darkgray", "black", 0, curses.A_DIM),
        "editnum": ("darkgray", "black", 0, curses.A_DIM),
        "editbold": ("white", "black", curses.A_BOLD, curses.A_BOLD),
        "editem": ("lightcyan", "black", 0,
                   getattr(curses, "A_ITALIC", curses.A_UNDERLINE)),
        "editlink": ("lightgreen", "black", 0, curses.A_UNDERLINE),
        "editheading": ("lightgreen", "black", curses.A_BOLD, curses.A_BOLD),
    },

    # A monochrome adapter, for the purists -- and for anyone reading over a
    # connection that mangles colour.  Everything is shape and emphasis.
    "mono": {},
}

#: ``mono`` is the monochrome column of the table, whatever the terminal can
#: do; naming it as a scheme is how a user asks for that on purpose.
_FORCED_MONO = ("mono",)


# ---------------------------------------------------------------------------
# Glyphs
# ---------------------------------------------------------------------------

UNICODE_GLYPHS = {
    "h": "═", "v": "║", "tl": "╔", "tr": "╗", "bl": "╚", "br": "╝",
    "h1": "─", "v1": "│", "tl1": "┌", "tr1": "┐", "bl1": "└", "br1": "┘",
    "lt1": "├", "rt1": "┤",
    "lcap": "╡", "rcap": "╞",
    "close": "[■]", "shade": "░", "block": "█",
    "up": "▲", "down": "▼", "submenu": "►", "check": "√", "bullet": "•",
    "tag": "*", "link": "@",
}

ASCII_GLYPHS = {
    "h": "=", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+",
    "h1": "-", "v1": "|", "tl1": "+", "tr1": "+", "bl1": "+", "br1": "+",
    "lt1": "+", "rt1": "+",
    "lcap": "[", "rcap": "]",
    "close": "[x]", "shade": ".", "block": "#",
    "up": "^", "down": "v", "submenu": ">", "check": "x", "bullet": "*",
    "tag": "*", "link": "@",
}


def _encoding_handles_box_characters() -> bool:
    """Whether the locale's encoding can carry the box-drawing characters.

    ``MERIDIAN_ASCII=1`` forces the plain set, which is the escape hatch for a
    terminal that claims UTF-8 and then draws it badly.
    """
    if os.environ.get("MERIDIAN_ASCII"):
        return False
    try:
        name = codecs.lookup(locale.getpreferredencoding(False) or "").name
    except (LookupError, ValueError, TypeError):
        return False
    return name in ("utf-8", "utf-16", "utf-32")


#: The glyph set in force.  Chosen once, at import, and re-chosen by
#: :func:`init` in case the program set the locale after importing us.
GLYPHS = UNICODE_GLYPHS if _encoding_handles_box_characters() else ASCII_GLYPHS


def glyph(name: str) -> str:
    return GLYPHS.get(name, " ")


# ---------------------------------------------------------------------------
# Building the attributes
# ---------------------------------------------------------------------------

#: role -> curses attribute, filled in by :func:`init`.
_attrs: dict[str, int] = {}
#: The lowest pair number this module has not claimed, so other code that
#: allocates pairs of its own (the image viewer) can start above us.
_next_pair: int = 1
#: The scheme currently in force.
current: str = DEFAULT_SCHEME
#: Whether the roles are being drawn in colour rather than from the mono column.
_in_colour: bool = False


def _colours() -> int:
    return getattr(curses, "COLORS", 8) or 8


def _foreground(name: str) -> tuple[int, int]:
    """A colour number and any attribute needed to reach the bright half."""
    base, bright = EGA.get(name, EGA["lightgray"])
    if not bright:
        return base, 0
    if _colours() >= 16:
        return base + 8, 0
    # Eight colours: bold is how a terminal has always spelled "bright".
    return base, curses.A_BOLD


def _background(name: str) -> int:
    """A background colour number, dimmed to the base half when it must be."""
    base, bright = EGA.get(name, EGA["black"])
    if bright and _colours() >= 16:
        return base + 8
    return base


def _resolve(scheme: dict, role: str) -> tuple[str, str, int, int]:
    return scheme.get(role) or ROLES[role]


def init(scheme: str | None = None) -> str:
    """Allocate the colour pairs for ``scheme`` and return the name used.

    Safe to call on a terminal with no colour, one that refuses ``init_pair``,
    and one that has fewer pairs than there are roles: each of those simply
    leaves more roles on their monochrome fallback.  Callers do not have to
    check -- that is the point of routing every attribute through :func:`attr`.
    """
    global _attrs, _next_pair, current, _in_colour, GLYPHS

    GLYPHS = UNICODE_GLYPHS if _encoding_handles_box_characters() \
        else ASCII_GLYPHS
    name = scheme if scheme in SCHEMES else DEFAULT_SCHEME
    current = name
    table = SCHEMES[name]
    _attrs = {}
    _next_pair = 1

    try:
        colour = bool(curses.has_colors())
    except curses.error:
        colour = False
    _in_colour = colour and name not in _FORCED_MONO
    if not _in_colour:
        return name

    pairs: dict[tuple[int, int], int] = {}
    limit = getattr(curses, "COLOR_PAIRS", 64) or 64
    for role in ROLES:
        fg_name, bg_name, extra, mono = _resolve(table, role)
        fg, bright = _foreground(fg_name)
        bg = _background(bg_name)
        index = pairs.get((fg, bg))
        if index is None:
            if _next_pair >= limit:
                continue                      # out of pairs: keep the mono one
            try:
                curses.init_pair(_next_pair, fg, bg)
            except (curses.error, ValueError, OverflowError):
                # A terminal that refuses one pair will refuse the rest, but
                # there is nothing to gain from finding that out role by role.
                _in_colour = False
                _attrs = {}
                return name
            index = _next_pair
            pairs[(fg, bg)] = index
            _next_pair += 1
        try:
            _attrs[role] = curses.color_pair(index) | extra | bright
        except (curses.error, ValueError, OverflowError):
            # No screen behind the terminal after all: leave every role on
            # its monochrome fallback rather than half-painting the program.
            _in_colour = False
            _attrs = {}
            return name
    return name


def attr(role: str) -> int:
    """The curses attribute for a role, monochrome fallback included."""
    found = _attrs.get(role)
    if found is not None:
        return found
    spec = SCHEMES.get(current, {}).get(role) or ROLES.get(role)
    return spec[3] if spec else curses.A_NORMAL


def in_colour() -> bool:
    """Whether the roles resolved to real colour pairs."""
    return _in_colour


def next_free_pair() -> int:
    """The first colour pair number this module has not taken.

    The image viewer allocates pairs of its own for the picture; starting
    above ours is what stops a photograph from repainting the desktop.
    """
    return _next_pair


def names() -> list[str]:
    """The schemes a user can choose from."""
    return sorted(SCHEMES)


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------
#
# Every one of these swallows curses.error.  Writing to the bottom-right cell
# of a window always fails, a resized terminal fails for a moment, and none of
# it is worth taking the application down for: the screen is redrawn from
# scratch on the next keystroke anyway.

def paint(win, y: int, x: int, text: str, role: str | int,
          width: int | None = None) -> int:
    """Write ``text`` at (y, x) in ``role``, padded or clipped to ``width``.

    Returns the column just past what was written, so callers can chain.
    """
    if width is not None:
        text = text[:width].ljust(width) if width > 0 else ""
    if not text:
        return x
    a = role if isinstance(role, int) else attr(role)
    try:
        win.addstr(y, x, text, a)
    except curses.error:
        pass
    return x + len(text)


def fill(win, y: int, x: int, width: int, role: str | int,
         char: str = " ") -> None:
    """Cover ``width`` cells of row ``y`` with one character."""
    if width > 0:
        paint(win, y, x, char * width, role)


def background(win, role: str) -> None:
    """Make ``role`` the window's background, so erase() clears to it."""
    try:
        win.bkgd(" ", attr(role))
    except curses.error:
        pass


def hotkey(caption: str) -> tuple[str, int]:
    """Split a ``~H~ot`` caption into its text and the index of the hot letter.

    The marker is the one Turbo Vision used in its resource files, and it is
    kept here for the same reason: the caption and its accelerator are one
    string, so they cannot drift apart.
    """
    start = caption.find("~")
    if start < 0:
        return caption, -1
    end = caption.find("~", start + 1)
    if end < 0:
        return caption.replace("~", ""), -1
    text = caption[:start] + caption[start + 1:end] + caption[end + 1:]
    return text, start


def strip_hotkey(caption: str) -> str:
    return hotkey(caption)[0]


def hotkey_letter(caption: str) -> str:
    """The accelerator letter of a caption, lowercased, or ``""``."""
    text, index = hotkey(caption)
    return text[index].lower() if index >= 0 else ""


def paint_caption(win, y: int, x: int, caption: str, role: str | int,
                  hot_role: str | int) -> int:
    """Write a caption, its accelerator letter in the second role."""
    text, index = hotkey(caption)
    end = paint(win, y, x, text, role)
    if index >= 0:
        paint(win, y, x + index, text[index], hot_role)
    return end


def _title_parts(caption: str, width: int, close: bool,
                 double: bool) -> tuple[str, int, int]:
    """A top border's text, and where the caption sits within it.

    Centred the way Turbo Vision centred it -- against the whole border, not
    against the space left over after the close box.  The caption's offset
    comes back with the text because the two glyph sets bracket it with
    different characters, and hunting for them afterwards finds the close box.
    """
    h = glyph("h" if double else "h1")
    box = glyph("close") if close else ""
    if not caption:
        return box + h * max(0, width - len(box)), 0, 0
    label = f"{glyph('lcap')} {caption} {glyph('rcap')}"
    if len(box) + len(label) + 2 > width:
        # No room to centre anything: keep the caption, lose the decoration.
        text = (box + h + caption)[:width]
        return text, len(box) + 1, max(0, len(text) - len(box) - 1)
    lead = max(1, (width - len(label)) // 2 - len(box))
    text = box + h * lead + label
    return (text + h * max(0, width - len(text)), len(box) + lead, len(label))


def frame_title(caption: str, width: int, close: bool = True) -> str:
    """The text of a window's top border: close box, then ``╡ caption ╞``."""
    return _title_parts(caption, width, close, True)[0]


def frame(win, y: int, x: int, height: int, width: int, role: str,
          title: str = "", title_role: str | None = None,
          double: bool = True, close: bool = True,
          footer: str = "", footer_role: str | None = None) -> None:
    """Draw a Turbo Vision window frame, caption and all.

    ``double`` picks the double-line border an active window gets; an inactive
    one is drawn single, which is how the IDE showed you where the keyboard
    was without using colour alone.
    """
    if height < 2 or width < 2:
        return
    suffix = "" if double else "1"
    h, v = glyph("h" + suffix), glyph("v" + suffix)
    tl, tr = glyph("tl" + suffix), glyph("tr" + suffix)
    bl, br = glyph("bl" + suffix), glyph("br" + suffix)
    inner = width - 2

    paint(win, y, x, tl + h * inner + tr, role)
    for row in range(y + 1, y + height - 1):
        paint(win, row, x, v, role)
        paint(win, row, x + width - 1, v, role)
    paint(win, y + height - 1, x, bl + h * inner + br, role)

    if title:
        text, at, length = _title_parts(title, inner, close, double)
        paint(win, y, x + 1, text, role, inner)
        # The caption is written again in its own colour, on top of the border
        # run it was centred into.
        if length:
            paint(win, y, x + 1 + at, text[at:at + length], title_role or role)
    if footer:
        text = f"{glyph('lcap')} {footer} {glyph('rcap')}"[:inner]
        paint(win, y + height - 1, x + 2, text, footer_role or role)


def darken(win, y: int, x: int, count: int) -> None:
    """Recolour ``count`` cells as shadow, leaving what they say alone.

    ``chgat`` rather than a write, because that is what a shadow does to what
    it falls on: the text underneath stays there, in the dark.
    """
    max_y, max_x = win.getmaxyx()
    if not (0 <= y < max_y) or x >= max_x:
        return
    x = max(0, x)
    count = min(count, max_x - x)
    if count <= 0:
        return
    try:
        win.chgat(y, x, count, attr("shadow"))
    except curses.error:
        pass


def shadow(win, y: int, x: int, height: int, width: int) -> None:
    """Darken the cells a window's drop shadow falls on.

    Two columns to the right and one row below, as Turbo Vision drew it.
    """
    for row in range(y + 1, y + height + 1):
        darken(win, row, x + width, 2)
    darken(win, y + height, x + 2, width)


def scrollbar(win, y: int, x: int, height: int, top: int, visible: int,
              total: int, role: str = "scroll",
              thumb_role: str = "thumb") -> None:
    """A Turbo Vision scrollbar: arrows at the ends, a block on a shaded track.

    Always drawn, even when everything fits, so a window does not change width
    the moment a directory grows past a screenful.
    """
    if height < 2:
        return
    paint(win, y, x, glyph("up"), role)
    paint(win, y + height - 1, x, glyph("down"), role)
    track = height - 2
    if track <= 0:
        return
    for row in range(track):
        paint(win, y + 1 + row, x, glyph("shade"), role)
    if total <= visible or total <= 0:
        return
    span = max(1, total - visible)
    at = min(track - 1, max(0, (top * (track - 1)) // span))
    paint(win, y + 1 + at, x, glyph("block"), thumb_role)


def progress_bar(width: int, fraction: float) -> str:
    """A filled bar ``width`` cells wide -- blocks over a shaded track."""
    if width <= 0:
        return ""
    filled = int(round(width * max(0.0, min(1.0, fraction))))
    return glyph("block") * filled + glyph("shade") * (width - filled)


def button(win, y: int, x: int, caption: str, width: int = 0,
           focused: bool = False, disabled: bool = False) -> int:
    """A Turbo Vision push button, drop shadow and all.

    The shadow is the button's own: one cell to the right, and a run along the
    bottom edge shifted right by one.  It is the detail that makes a row of
    green rectangles read as buttons rather than as coloured text.
    """
    text = strip_hotkey(caption)
    width = width or len(text) + 4
    if disabled:
        role, hot = "dialogdim", "dialogdim"
    elif focused:
        role, hot = "buttonsel", "buttonhotsel"
    else:
        role, hot = "button", "buttonhot"

    fill(win, y, x, width, role)
    paint_caption(win, y, x + max(0, (width - len(text)) // 2), caption,
                  role, hot)
    # A button is one row tall, so its shadow starts on its own row rather
    # than below it: one cell off the right end, then the row underneath.
    darken(win, y, x + width, 1)
    darken(win, y + 1, x + 1, width)
    return x + width + 2
