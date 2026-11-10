# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Built-in plugin: a terminal running *inside* the pane.

The terminal appears in the pane's rectangle (the other pane keeps working
normally) and follows the pane's location:

* **local pane**   -- forks a real pty running your shell, started in the
  pane's current directory;
* **SFTP / SSH pane** -- opens an interactive shell channel on the pane's
  existing paramiko connection (``invoke_shell``) and ``cd``-s into the
  pane's directory.  Nothing extra to configure -- it reuses the login you
  already made for browsing;
* FTP panes have no shell and are refused with a clear message.

It is a real terminal emulator (:mod:`meridian_commander.vt`): colours,
cursor addressing, scroll regions and the alternate screen all work, so
``ls --color``, vim, less, man and top run in the pane and leave it as it
was when they exit.  The shell is told ``TERM=xterm-256color``, which is what
the emulator implements.  ``!`` still hands a program the whole real terminal.

Keys: every key goes to the program -- F1-F12, PgUp/PgDn, Esc, and the
arrows, Home and End with Ctrl, Alt or Shift included.
``Ctrl-]`` is the one key the terminal keeps, and it opens a small menu in the
pane's footer: ``Tab`` switches to the other pane (the shell keeps running;
Tab or a click comes back), ``z`` zooms the terminal to the full width and
back (single mode, as ``z`` does in a file pane), ``q`` closes the terminal,
``PgUp`` scrolls back through what went off the top, and ``Ctrl-]`` again
sends a literal Ctrl-] to the program.  The mouse wheel scrolls back too, and
a click on the other pane switches to it.  Exiting the shell (``exit`` /
Ctrl-D) closes it as well, and ends a zoom made with ``Ctrl-]`` ``z``.
"""

from __future__ import annotations

import curses
import errno
import os
import shlex
import signal
import struct
import time
import unicodedata

from .. import theme, vt
from ..imageview import palette_index, palette_rgb
from ..plugin_api import PanePlugin
from ..util import typed_char

#: The terminal type the shell is told, local and remote: what :mod:`vt`
#: implements, and an entry nearly every host has in its terminfo.
TERM = "xterm-256color"

#: How much is read from the pty at a time.
CHUNK = 65536

#: How long one poll may spend taking output in.  A ``cat`` of a large file
#: produces output faster than it can be drawn; without a limit the pane kept
#: reading and never got back to the keyboard, so Ctrl-C could not get
#: through until the file ended.
FEED_SECONDS = 0.05

#: How often the application polls while the program is busy -- output has
#: arrived or a key was sent in the last :data:`BUSY_SECONDS` -- so that
#: typing echoes at once rather than on the next idle poll, 120 ms later.
FAST_POLL_MS = 10
BUSY_SECONDS = 0.5

#: The key that opens the terminal's own menu: Ctrl-], telnet's escape key,
#: and one no shell or full-screen program binds.
MENU_KEY = 29

#: The sixteen colours as xterm draws them, for finding the nearest one when
#: the outer terminal has fewer than 256.
BASE16 = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)

# -- keys ----------------------------------------------------------------------

#: Cursor keys: the final letter, after ``ESC [`` normally and ``ESC O`` once
#: the program has asked for application cursor keys (DECCKM) -- which is
#: what vim, less and every curses program do on start-up.
_CURSOR_KEYS = {
    curses.KEY_UP: b"A", curses.KEY_DOWN: b"B",
    curses.KEY_RIGHT: b"C", curses.KEY_LEFT: b"D",
    curses.KEY_HOME: b"H", curses.KEY_END: b"F",
    curses.KEY_A1: b"H", curses.KEY_C1: b"F",       # keypad 7 and 1
    curses.KEY_B2: b"E",                             # keypad 5
}
#: Keys sent as ``ESC [ n ~``, as xterm sends them.
_TILDE_KEYS = {
    curses.KEY_IC: 2, curses.KEY_DC: 3,
    curses.KEY_PPAGE: 5, curses.KEY_NPAGE: 6,
    curses.KEY_A3: 5, curses.KEY_C3: 6,             # keypad 9 and 3
    curses.KEY_F0 + 5: 15, curses.KEY_F0 + 6: 17, curses.KEY_F0 + 7: 18,
    curses.KEY_F0 + 8: 19, curses.KEY_F0 + 9: 20, curses.KEY_F0 + 10: 21,
    curses.KEY_F0 + 11: 23, curses.KEY_F0 + 12: 24,
}
#: F1-F4, which xterm sends as ``ESC O P`` to ``ESC O S``.
_SS3_KEYS = {
    curses.KEY_F0 + 1: b"P", curses.KEY_F0 + 2: b"Q",
    curses.KEY_F0 + 3: b"R", curses.KEY_F0 + 4: b"S",
}
#: Shifted keys, in xterm's modifier form (2 = Shift).  curses reports Shift
#: with F1-F12 as F13-F24.
_SHIFTED_KEYS = {
    curses.KEY_SR: b"\x1b[1;2A", curses.KEY_SF: b"\x1b[1;2B",
    curses.KEY_SRIGHT: b"\x1b[1;2C", curses.KEY_SLEFT: b"\x1b[1;2D",
    curses.KEY_SHOME: b"\x1b[1;2H", curses.KEY_SEND: b"\x1b[1;2F",
    curses.KEY_SIC: b"\x1b[2;2~", curses.KEY_SDC: b"\x1b[3;2~",
    curses.KEY_SPREVIOUS: b"\x1b[5;2~", curses.KEY_SNEXT: b"\x1b[6;2~",
    curses.KEY_BTAB: b"\x1b[Z",
}
for _n, _letter in zip(range(13, 17), b"PQRS"):
    _SHIFTED_KEYS[curses.KEY_F0 + _n] = b"\x1b[1;2%c" % _letter
for _n, _code in zip(range(17, 25), (15, 17, 18, 19, 20, 21, 23, 24)):
    _SHIFTED_KEYS[curses.KEY_F0 + _n] = b"\x1b[%d;2~" % _code

#: Ctrl-, Alt- and the other modified cursor keys, by their terminfo names.
#: ``kLFT5`` is Ctrl-Left: the number is xterm's modifier code, 1 plus 1 for
#: Shift, 2 for Alt and 4 for Ctrl.  It is sent as ``ESC [1;5D``, and Ins,
#: Del, PgUp and PgDn as ``ESC [3;5~`` -- in either cursor-key mode, as xterm
#: does.
_MODIFIED_LETTERS = {"kUP": b"A", "kDN": b"B", "kRIT": b"C", "kLFT": b"D",
                     "kHOM": b"H", "kEND": b"F"}
_MODIFIED_TILDES = {"kIC": 2, "kDC": 3, "kPRV": 5, "kNXT": 6}

#: How far past ``KEY_MAX`` to look for them.  ncurses numbers a terminal's
#: extended keys from there, one number for each extended string in its
#: terminfo entry: on xterm they end at 577, and no entry has a thousand.
_EXTENDED_KEYS = 1024


def modified_sequence(name: str) -> bytes | None:
    """What xterm sends for the modified key terminfo calls ``name``.

    The program is told it is talking to xterm, so this is what it gets,
    whatever the outer terminal sent for the key -- rxvt's Ctrl-Left is
    ``ESC O d``.  A name with no number is the key with Shift: rxvt calls
    Shift-Up ``kUP``, as standard terminfo calls Shift-Left ``kLFT``.
    """
    base = name.rstrip("2345678")
    modifier = name[len(base):]
    if len(modifier) > 1:
        return None
    level = int(modifier or 2)
    if base in _MODIFIED_LETTERS:
        return b"\x1b[1;%d%s" % (level, _MODIFIED_LETTERS[base])
    if base in _MODIFIED_TILDES:
        return b"\x1b[%d;%d~" % (_MODIFIED_TILDES[base], level)
    return None


def modified_keys() -> dict[int, bytes]:
    """The modified cursor keys the outer terminal has, by the key code curses
    reports for each, and what to send for it.

    curses gives these keys their numbers when it starts, from the extended
    names in the terminal's terminfo entry, so the numbers are not fixed: on
    xterm Ctrl-Left is 554, another terminal numbers it differently, and
    screen or vt100 name none at all.  So each number is looked up by name.
    ncurses numbers them the first time a window is put in keypad mode, which
    the application does before anything else; until then, and before curses
    has started at all, the table is empty.
    """
    table: dict[int, bytes] = {}
    for code in range(curses.KEY_MAX, curses.KEY_MAX + _EXTENDED_KEYS):
        try:
            name = curses.keyname(code)
        except curses.error:
            return {}
        sequence = modified_sequence(name.decode("ascii", "replace"))
        if sequence is not None:
            table[code] = sequence
    return table


def encode_key(key: int | str, app_cursor: bool = False,
               app_keypad: bool = False,
               modified: dict[int, bytes] | None = None) -> bytes:
    """The bytes xterm would send for ``key``, in the program's key modes.

    ``modified`` is the outer terminal's own numbering of its Ctrl- and Alt-
    keys, from :func:`modified_keys`.  Nothing comes back for a key code with
    no meaning to a program -- a resize, a mouse report, a modified key the
    terminal did not name -- because encoding one sent a stray letter to the
    shell, where the next Enter would have run it.
    """
    if isinstance(key, int) and modified and key in modified:
        return modified[key]
    if key in _CURSOR_KEYS:
        lead = b"\x1bO" if app_cursor else b"\x1b["
        return lead + _CURSOR_KEYS[key]
    if key in _TILDE_KEYS:
        return b"\x1b[%d~" % _TILDE_KEYS[key]
    if key in _SS3_KEYS:
        return b"\x1bO" + _SS3_KEYS[key]
    if key in _SHIFTED_KEYS:
        return _SHIFTED_KEYS[key]
    if key == curses.KEY_ENTER:
        # The keypad's Enter: its own sequence in application keypad mode.
        return b"\x1bOM" if app_keypad else b"\r"
    if key in (curses.KEY_BACKSPACE, 127, 8):
        return b"\x7f"
    if key in (10, 13):
        return b"\r"
    # A control key is a number and only ever a number; a character from
    # U+0100 up arrives as a str, and comparing the two raises.
    if isinstance(key, int) and 0 <= key < 32:
        return bytes([key])
    char = typed_char(key)
    return char.encode("utf-8") if char is not None else b""


# -- colours -------------------------------------------------------------------

#: The emulator's attribute flags as curses attributes.  Strike-through has no
#: curses attribute, and invisible text is drawn as blanks instead of relying
#: on A_INVIS, which some terminals ignore.
_FLAGS = (
    (vt.BOLD, curses.A_BOLD), (vt.DIM, curses.A_DIM),
    (vt.ITALIC, getattr(curses, "A_ITALIC", 0)),
    (vt.UNDERLINE, curses.A_UNDERLINE), (vt.BLINK, curses.A_BLINK),
    (vt.REVERSE, curses.A_REVERSE),
)


def _rgb_of(index: int) -> tuple[int, int, int]:
    """The colour a palette index stands for."""
    return BASE16[index] if index < 16 else palette_rgb(index)


def nearest16(rgb: tuple[int, int, int]) -> int:
    """The one of the sixteen colours nearest to ``rgb``."""
    return min(range(16), key=lambda i: sum(
        (a - b) ** 2 for a, b in zip(BASE16[i], rgb)))


class Colours:
    """Turns the emulator's attributes into curses attributes.

    Colours come down to what the outer terminal has: 256-colour and 24-bit
    colours map to the nearest of 256, or of sixteen, or of eight (with bold
    standing in for bright, as the theme does); with no colour at all only the
    attributes are kept.  The default colours are the outer terminal's own --
    usually white on black -- rather than the pane's blue, because
    ``ls --color`` paints directories blue and every program's colour scheme
    assumes the terminal's background.

    Pairs come from the application's shared allocator.  When that runs out,
    a colour gets the nearest pair already made rather than a wrong one.
    """

    def __init__(self) -> None:
        self.pairs = theme.shared_pairs() if theme.in_colour() else None
        self.count = getattr(curses, "COLORS", 0) or 0
        if self.count < 8:
            self.pairs = None
        try:
            defaults = tuple(curses.pair_content(0)) == (-1, -1)
        except (curses.error, ValueError):
            defaults = False
        # Without use_default_colors() there is no "the terminal's own
        # colour" to ask for, so the default is spelt out: white on black.
        self.default_fg = -1 if defaults else curses.COLOR_WHITE
        self.default_bg = -1 if defaults else curses.COLOR_BLACK
        self._cache: dict[tuple, int] = {}

    def current(self) -> bool:
        """Whether this is still right for the screen: a new colour scheme
        hands every pair out again and makes a new allocator."""
        wanted = theme.shared_pairs() if theme.in_colour() else None
        return self.pairs is wanted or (self.pairs is None and self.count < 8)

    def attr(self, cell: tuple) -> int:
        found = self._cache.get(cell)
        if found is None:
            found = self._cache[cell] = self._compute(cell)
        return found

    def _compute(self, cell: tuple) -> int:
        fg, bg, flags = cell
        result = 0
        for flag, attribute in _FLAGS:
            if flags & flag:
                result |= attribute
        if self.pairs is None:
            return result
        fore, bright = self._colour(fg, self.default_fg)
        back, _ = self._colour(bg, self.default_bg)
        if bright:
            result |= curses.A_BOLD
        return result | curses.color_pair(self._pair(fore, back))

    def _colour(self, value: int, default: int) -> tuple[int, bool]:
        """A curses colour number for ``value``, and whether it needs bold to
        reach the bright half on an eight-colour terminal."""
        if value == vt.DEFAULT:
            return default, False
        if value & vt.RGB:
            rgb = ((value >> 16) & 255, (value >> 8) & 255, value & 255)
            index = palette_index(*rgb) if self.count >= 256 else nearest16(rgb)
        else:
            index = value
        if self.count >= 256:
            return index, False
        if index >= 16:
            index = nearest16(palette_rgb(index))
        if self.count >= 16:
            return index, False
        if index >= 8:
            return index - 8, True
        return index, False

    def _pair(self, fg: int, bg: int) -> int:
        if (fg, bg) == (self.default_fg, self.default_bg):
            return 0
        assert self.pairs is not None
        found = self.pairs.find(fg, bg)
        if found is not None:
            return found
        return self._nearest(fg, bg)

    def _nearest(self, fg: int, bg: int) -> int:
        """The pair already made whose colours are closest to these."""
        def rgb(colour: int, default: tuple[int, int, int]):
            return default if colour < 0 else _rgb_of(colour)

        def distance(a, b) -> int:
            return sum((x - y) ** 2 for x, y in zip(a, b))

        white, black = BASE16[7], BASE16[0]
        want_fg, want_bg = rgb(fg, white), rgb(bg, black)
        assert self.pairs is not None
        best, best_score = 0, distance(want_fg, white) + distance(want_bg, black)
        for (have_fg, have_bg), pair in self.pairs.allocated():
            score = (distance(want_fg, rgb(have_fg, white))
                     + distance(want_bg, rgb(have_bg, black)))
            if score < best_score:
                best, best_score = pair, score
        return best


def _ascii_fallback(ch: str) -> str:
    """A box-drawing character as ASCII, for a terminal that cannot show it."""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return "?"
    if "HORIZONTAL" in name and "VERTICAL" not in name or "SCAN LINE" in name:
        return "-"
    if "VERTICAL" in name and "HORIZONTAL" not in name:
        return "|"
    return "+"


#: The DEC line-drawing characters, and the rest of the box-drawing block, in
#: ASCII: what a program's borders turn into when the outer terminal's
#: encoding cannot show the real ones, as the app's own frames do.
ASCII_BOX = {code: _ascii_fallback(chr(code)) for code in range(0x2500, 0x2580)}
ASCII_BOX.update({ord(k): v for k, v in {
    "◆": "+", "▒": "#", "°": "o", "±": "+",
    "≤": "<", "≥": ">", "π": "p", "≠": "!",
    "£": "L", "·": ".", " ": " ",
    "⎺": "-", "⎻": "-", "⎼": "-", "⎽": "_",
}.items()})


# -- the plug-in ---------------------------------------------------------------

class TerminalPlugin(PanePlugin):
    name = "Terminal"
    description = "Shell inside this pane (local pty, or SSH for remote panes)"
    # A shell wants the whole width.  Its one use of the other pane, Ctrl-]
    # then Tab, goes through focus_other, which keeps it in view beside that
    # pane.
    uses_other_pane = False
    wants_timer = True     # the app polls tick() so output flows while idle

    def on_start(self) -> None:
        self.screen = vt.Screen(24, 80)
        self.scroll = 0         # lines scrolled back; 0 is the live screen
        self.menu = False       # Ctrl-] was pressed: the next key is ours
        self.done = False
        self.status = ""
        self._rows = 24
        self._cols = 80
        self._pid = None        # local child pid
        self._fd = None         # local pty master
        self._chan = None       # paramiko channel
        self._busy_at = 0.0
        self._colours: Colours | None = None
        self._modified = modified_keys()

        fs = self.ctx.own_fs
        path = self.ctx.own_path
        kind = getattr(fs, "scheme", "")
        if kind == "local":
            self._start_local(path)
        elif kind in ("sftp", "ssh"):
            self._start_remote(fs, path)
        else:
            raise RuntimeError(
                "No shell available for this pane type (FTP has no shell); "
                "open the terminal from a local, SFTP or SSH pane."
            )

    # -- backends ---------------------------------------------------------
    def _start_local(self, path: str) -> None:
        import fcntl
        import pty

        shell = os.environ.get("SHELL", "/bin/sh")
        pid, fd = pty.fork()
        # pragma: no cover -- this branch runs only in the forked child, which
        # replaces its own process image with the shell a few lines later, so
        # no in-process coverage tracer can ever observe it.
        if pid == 0:  # child  # pragma: no cover
            try:
                os.chdir(path)
            except OSError:
                pass
            os.environ["TERM"] = TERM
            # The size comes from the pty, and follows the pane; a LINES or
            # COLUMNS inherited from the outer shell would pin it instead.
            os.environ.pop("LINES", None)
            os.environ.pop("COLUMNS", None)
            os.execvp(shell, [shell])
            os._exit(127)
        self._pid = pid
        self._fd = fd
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def _start_remote(self, fs, path: str) -> None:
        client = getattr(fs, "_client", None)
        if client is None:
            raise RuntimeError("This remote pane has no SSH connection.")
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("The SSH connection is no longer active.")
        chan = transport.open_session()
        chan.get_pty(term=TERM, width=self._cols, height=self._rows)
        chan.invoke_shell()
        chan.setblocking(False)
        self._chan = chan
        # Jump to the pane's directory in the remote shell.
        chan.sendall(f"cd {shlex.quote(path)}\n".encode())

    # -- pumping ------------------------------------------------------------
    def tick(self) -> None:
        """Take in pending output, for at most :data:`FEED_SECONDS`."""
        if self.done:
            return
        deadline = time.monotonic() + FEED_SECONDS
        if self._fd is not None:
            while True:
                try:
                    data = os.read(self._fd, CHUNK)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        break
                    data = b""
                if not data:
                    self._finish()
                    return
                self._feed(data)
                if len(data) < CHUNK or time.monotonic() >= deadline:
                    break
            if self._pid is not None:
                try:
                    pid, _status = os.waitpid(self._pid, os.WNOHANG)
                    if pid == self._pid:
                        self._pid = None
                        self._finish()
                except ChildProcessError:
                    pass
        elif self._chan is not None:
            while self._chan.recv_ready() and time.monotonic() < deadline:
                data = self._chan.recv(CHUNK)
                if not data:
                    break
                self._feed(data)
            if self._chan.exit_status_ready() or self._chan.closed:
                # Drain what's left, then mark finished.
                try:
                    while self._chan.recv_ready():
                        self._feed(self._chan.recv(CHUNK))
                except Exception:
                    pass
                self._finish()

    def _feed(self, data: bytes) -> None:
        before = self.screen.history_count
        self.screen.feed(data)
        self._busy_at = time.monotonic()
        if self.scroll:
            # Keep a scrolled-back view on the same lines while output
            # pushes more into the history beneath it.
            added = self.screen.history_count - before
            self.scroll = min(self._max_scroll(), self.scroll + added)
        replies = self.screen.take_replies()
        if replies:
            self._send(replies)

    @property
    def poll_ms(self) -> int | None:
        """How soon the application should poll again, while busy."""
        if not self.done and time.monotonic() - self._busy_at < BUSY_SECONDS:
            return FAST_POLL_MS
        return None

    def _finish(self) -> None:
        if not self.done:
            self.done = True
            self.menu = False
            self.status = "[process exited -- press any key to close]"

    # -- keys ----------------------------------------------------------------
    def handle_key(self, key: int | str):
        if key == curses.KEY_MOUSE:
            # The application's: it turns the wheel into wheel() and a click
            # on the other pane into focus.  Taking it here meant neither ever
            # happened, and while scrolled back a wheel report counted as
            # "any other key" and dropped the view back to the live screen.
            self.menu = False
            return None
        if self.done:
            self.on_exit()
            return False
        if self.menu:
            return self._menu_key(key)
        if self.scroll:
            return self._scroll_key(key)
        if key == MENU_KEY:
            self.menu = True
            return True
        data = encode_key(key, self.screen.app_cursor, self.screen.app_keypad,
                          self._modified)
        if data:
            self._busy_at = time.monotonic()
            self._send(data)
        return True

    def _menu_key(self, key: int | str):
        """The key after Ctrl-]: one of the terminal's own commands."""
        self.menu = False
        if key == 9:                                 # Tab: the other pane
            self.ctx.focus_other()
        elif key in (ord("z"), ord("Z")):            # zoom: one pane or two
            # The app's own z, which the shell would otherwise get.  The
            # new width reaches the program on the next draw, as a resize.
            self.ctx.toggle_single()
        elif key in (ord("q"), ord("Q")):            # close the terminal
            self.on_exit()
            return False
        elif key in (curses.KEY_PPAGE, ord("[")):    # scroll back
            self._scroll_by(self._page())
        elif key == MENU_KEY:                        # Ctrl-] for the program
            self._send(b"\x1d")
        # Esc, or anything else: the menu simply closes.
        return True

    def _scroll_key(self, key: int | str):
        """Keys while scrolled back: they move the view, and reach nothing."""
        if key == curses.KEY_PPAGE:
            self._scroll_by(self._page())
        elif key == curses.KEY_NPAGE:
            self._scroll_by(-self._page())
        elif key == curses.KEY_UP:
            self._scroll_by(1)
        elif key == curses.KEY_DOWN:
            self._scroll_by(-1)
        elif key == curses.KEY_HOME:
            self.scroll = self._max_scroll()
        elif key == MENU_KEY:
            self.menu = True
        else:
            # Any other key goes back to the live screen -- and is not sent,
            # so a key pressed to leave the history cannot run anything.
            self.scroll = 0
        return True

    def wheel(self, lines: int) -> None:
        """The mouse wheel: ``lines`` back into the history, or forward."""
        if not self.done:
            self._scroll_by(lines)

    def _page(self) -> int:
        return max(1, self._rows - 1)

    def _max_scroll(self) -> int:
        # The alternate screen has no history: what a full-screen program
        # scrolls off is its own business, not the shell's.
        return 0 if self.screen.alt_active else len(self.screen.scrollback)

    def _scroll_by(self, lines: int) -> None:
        self.scroll = max(0, min(self._max_scroll(), self.scroll + lines))

    def _send(self, data: bytes) -> None:
        try:
            if self._fd is not None:
                while data:
                    data = data[os.write(self._fd, data):]
            elif self._chan is not None:
                self._chan.sendall(data)
        except BlockingIOError:
            # The program is not reading its input and the pty's buffer is
            # full.  Dropping these bytes is better than ending the session.
            pass
        except Exception:
            self._finish()

    # -- drawing ---------------------------------------------------------------
    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        # Match the file-pane layout: header row on top, status bar on the
        # bottom, the screen in between -- so the terminal's area lines up
        # exactly with the listing in the other pane.
        rows, cols = h - 2, w
        if rows < 1 or cols < 1:
            return
        if (rows, cols) != (self._rows, self._cols):
            self._rows, self._cols = rows, cols
            self.screen.resize(rows, cols)
            self._resize(rows, cols)
        # The history can shrink under a scrolled-back view (ED 3, or a
        # program switching to the alternate screen, which has none).
        self._scroll_by(0)

        fs = self.ctx.own_fs
        where = f"{fs.label()}:{self.ctx.own_path}"
        self.put(stdscr, y, x, w, f" [terminal] {where} ", theme.attr("keybar"))

        if self._colours is None or not self._colours.current():
            self._colours = Colours()
        screen = self.screen
        if self.scroll:
            # The history, then the main screen under it: there are always at
            # least a screenful of lines, since the scroll never goes further
            # back than the history is long.
            history = list(screen.scrollback) + screen.main
            end = len(history) - self.scroll
            lines = history[end - rows:end]
        else:
            lines = screen.lines
        for row, line in enumerate(lines):
            self._draw_line(stdscr, y + 1 + row, x, cols, line)

        app = getattr(self.ctx, "app", None)
        active = getattr(app, "active", None) is self.ctx.own_panel
        if (active and not self.scroll and not self.done and not self.menu
                and screen.cursor_visible):
            self._draw_cursor(stdscr, y + 1, x, rows, cols)

        self.put(stdscr, y + h - 1, x, w, self._footer(), theme.attr("keybar"))

    def _footer(self) -> str:
        # Each fits the forty columns of half an 80-column screen.
        if self.status:
            return f" {self.status} "
        if self.menu:
            return " Tab pane  z zoom  q close  PgUp scroll "
        if self.scroll:
            return f" scrollback, {self.scroll} lines up  Esc: live "
        return " Ctrl-]: Tab pane  q close  PgUp scroll "

    def _draw_line(self, stdscr, y: int, x: int, cols: int,
                   line: vt.Line) -> None:
        """One row of the screen, as runs of cells that share an attribute.

        A line from the history can be wider or narrower than the pane, from
        before a resize: it is cut, or padded with blanks.
        """
        colours = self._colours
        assert colours is not None
        reverse = curses.A_REVERSE if self.screen.reverse_video else 0
        chars, attrs = line.chars, line.attrs
        width = min(len(chars), cols)
        if width < len(chars) and chars[width] == "":
            # A line from a wider screen, cut through a wide character.
            chars = chars[:width - 1] + [" "]
        col = 0
        while col < width:
            attr = attrs[col]
            start = col
            while col < width and attrs[col] == attr:
                col += 1
            text = "".join(chars[start:col])
            if attr[2] & vt.INVISIBLE:
                text = " " * (col - start)
            self._put_run(stdscr, y, x + start, text, colours.attr(attr) ^ reverse)
        if width < cols:
            self._put_run(stdscr, y, x + width, " " * (cols - width),
                          colours.attr(vt.PLAIN) ^ reverse)

    @staticmethod
    def _put_run(stdscr, y: int, x: int, text: str, attr: int) -> None:
        if theme.GLYPHS is theme.ASCII_GLYPHS:
            text = text.translate(ASCII_BOX)
        try:
            stdscr.addstr(y, x, text, attr)
        except curses.error:
            # The bottom-right cell of the screen cannot be written without
            # scrolling, and a character the outer terminal cannot show is
            # refused: the rest of the line is still worth drawing.
            pass

    def _draw_cursor(self, stdscr, y: int, x: int, rows: int, cols: int) -> None:
        """The cursor, as the cell under it in reverse."""
        screen = self.screen
        cy, cx = min(screen.y, rows - 1), min(screen.x, cols - 1)
        line = screen.lines[cy]
        ch = line.chars[cx] or " "
        assert self._colours is not None
        attr = self._colours.attr(line.attrs[cx]) ^ curses.A_REVERSE
        if screen.reverse_video:
            attr ^= curses.A_REVERSE
        self._put_run(stdscr, y + cy, x + cx, ch, attr)

    def _resize(self, rows: int, cols: int) -> None:
        if self._fd is not None:
            try:
                import fcntl
                import termios

                fcntl.ioctl(self._fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
            except Exception:
                pass
        elif self._chan is not None:
            try:
                self._chan.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    # -- lifecycle ---------------------------------------------------------------
    def on_exit(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGHUP)
                os.waitpid(self._pid, os.WNOHANG)
            except (OSError, ChildProcessError):
                pass
            self._pid = None
        if self._chan is not None:
            try:
                self._chan.close()
            except Exception:
                pass
            self._chan = None
