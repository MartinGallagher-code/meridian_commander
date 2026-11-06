# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""A terminal emulator's screen: what a program's output looks like.

:class:`Screen` takes the bytes a program writes to its terminal and keeps
the picture they describe -- a grid of rows and columns, each cell a character
with its colours and attributes, plus a cursor, the terminal modes, a second
"alternate" screen for full-screen programs, and a scrollback of the lines
that went off the top.  The in-pane terminal draws that grid; nothing in here
knows about curses, so every sequence can be tested as bytes in and cells out.

What it implements is what the ``xterm-256color`` terminfo entry promises,
because that is the ``TERM`` the shell is given and so the description every
program will believe:

* cursor movement and addressing (CUU/CUD/CUF/CUB, CNL/CPL, CHA/HPA, VPA,
  CUP/HVP), origin mode, save and restore (DECSC/DECRC, ``CSI s``/``CSI u``);
* erasing (ED 0-3, EL 0-2, ECH), insert and delete of characters and lines
  (ICH/DCH, IL/DL), scrolling (SU/SD, IND/RI/NEL) inside a scroll region
  (DECSTBM), and REP;
* auto-wrap with the "pending wrap" rule at the last column, insert mode,
  tab stops (HTS/TBC/CHT/CBT);
* SGR: bold, dim, italic, underline, blink, reverse, invisible, strike, the
  sixteen colours, the 256-colour palette and 24-bit colour, in both the
  ``;`` and ``:`` spellings, with erases in the current background (``bce``);
* the alternate screen (modes 47, 1047, 1048, 1049), cursor-key and keypad
  modes, cursor visibility, reverse video, bracketed paste;
* the DEC line-drawing set (``ESC ( 0``, SO/SI) and the UK set;
* wide (East Asian) and combining characters;
* the replies programs wait for: device status (``CSI 5n``, ``CSI 6n``),
  device attributes (``CSI c``, ``CSI > c``) and the text-area size.

Everything else is parsed and dropped -- OSC titles and colour queries, DCS
strings, mouse modes -- so that it cannot reach the screen as text.

It is also built to survive output that was never meant for a terminal: a
``cat`` of a binary file.  Every count and position is clamped to the screen,
an unterminated sequence never holds more than a few dozen characters, and a
control character abandons an unterminated string the way a real terminal
does, so one stray ``ESC ]`` cannot swallow the rest of the session.
"""

from __future__ import annotations

import codecs
import collections
import functools
import re
import unicodedata

#: Lines kept above the screen for scrolling back.
SCROLLBACK = 2000

#: How many characters of parameters a CSI sequence may carry before the rest
#: are dropped.  Real ones carry a handful; an unterminated one in binary
#: output carries the rest of the file.  The sequence is still swallowed up to
#: its final byte, so the stream resynchronises exactly where it should.
MAX_CSI_PARAMS = 64

#: How many numbers a sequence is read for.  SGR is the only one that uses
#: more than two, and twenty-odd attributes in one is already absurd.
MAX_PARAMS = 32

#: The largest number a parameter can stand for.  Every count and position is
#: clamped to the screen later; this only stops a hundred-digit number from
#: being carried around as an integer of that size.
MAX_NUMBER = 65535

#: How many combining marks one cell may collect.  A run of thousands of them
#: on one base character is a known way to make a terminal's cell grow without
#: bound; nothing legible needs more than a handful.
MAX_MARKS = 8

#: How many characters one REP may produce: a screenful is the most it can
#: ever show, and a count in the millions must not turn into that much work.
MAX_REPEAT_SCREENS = 1

#: How many bytes of replies (cursor reports and the like) may wait to be
#: written back.  A program that floods ``CSI 6n`` without reading the answers
#: would otherwise grow this list for as long as it ran.
MAX_REPLY_BYTES = 4096

#: The colour a cell has when nothing set one: the outer terminal's own.
DEFAULT = -1
#: Set on a 24-bit colour, so ``RGB | 0xRRGGBB`` cannot be mistaken for one of
#: the 256 palette indices.
RGB = 1 << 24

# Attribute flags, one bit each.
BOLD = 1
DIM = 2
ITALIC = 4
UNDERLINE = 8
BLINK = 16
REVERSE = 32
INVISIBLE = 64
STRIKE = 128

#: (foreground, background, flags) -- the attribute of a cell, and of the pen.
Attr = tuple
#: A cell nothing has been written to.
PLAIN: Attr = (DEFAULT, DEFAULT, 0)

#: The DEC Special Graphics set, as ``ESC ( 0`` selects it: the letters that
#: draw boxes.  ``x`` is a vertical line, ``q`` a horizontal one, and so on --
#: which is why a program drawing a border on a terminal that ignores the
#: switch shows ``lqqqk`` instead.
DEC_GRAPHICS = str.maketrans(
    "_`abcdefghijklmnopqrstuvwxyz{|}~",
    " ◆▒␉␌␍␊°±␤␋"
    "┘┐┌└┼⎺⎻─⎼⎽├"
    "┤┴┬│≤≥π≠£·",
)
#: The UK set (``ESC ( A``): ASCII with a pound sign in place of the hash.
UK = str.maketrans("#", "£")

# Parser states.
GROUND, ESCAPE, CSI, OSC, OSC_ESC, STRING, STRING_ESC = range(7)

#: A run of characters that are printed, as opposed to acted on: everything
#: but the C0 and C1 controls and DEL.
_TEXT = re.compile(r"[^\x00-\x1f\x7f-\x9f]+")
#: A complete, ordinary CSI sequence, recognised in one step instead of one
#: character at a time -- colour changes are most of what ``ls --color`` and
#: a syntax-highlighted ``git diff`` send, so this is the common case.
_CSI = re.compile(r"\x1b\[([0-?]{0,%d})([ -/]{0,2})([@-~])" % MAX_CSI_PARAMS)


@functools.lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    """How many columns ``ch`` takes: 0 for a combining mark, 2 for a wide one.

    The standard library has no ``wcwidth``, so this is the usual
    approximation from the Unicode data it does have: combining marks and
    format characters take no room, East Asian Wide and Fullwidth characters
    take two, and everything else takes one.
    """
    if ch < "̀":
        return 1
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _number(text: str) -> int:
    """One parameter as a number; an empty or malformed one reads as 0."""
    return min(MAX_NUMBER, int(text)) if text.isdigit() else 0


class Line:
    """One row of cells: a character and an attribute per column.

    The right half of a wide character is an empty string, so joining the
    characters gives the text and the two lists always have one entry per
    column.  ``wrapped`` records that the line continued onto the next one
    because it ran out of room, rather than ending with a newline.
    """

    __slots__ = ("chars", "attrs", "wrapped")

    def __init__(self, cols: int, attr: Attr = PLAIN) -> None:
        self.chars = [" "] * cols
        self.attrs = [attr] * cols
        self.wrapped = False

    def text(self) -> str:
        """The line's characters, without the trailing blanks."""
        return "".join(self.chars).rstrip()

    def resize(self, cols: int) -> None:
        width = len(self.chars)
        if cols < width:
            del self.chars[cols:]
            del self.attrs[cols:]
            # A wide character cut in half by the new edge cannot be drawn.
            if self.chars and char_width(self.chars[-1][:1] or " ") == 2:
                self.chars[-1] = " "
        elif cols > width:
            self.chars.extend([" "] * (cols - width))
            self.attrs.extend([PLAIN] * (cols - width))


class _Saved:
    """What DECSC saves: where the cursor is and how it will write."""

    __slots__ = ("x", "y", "pen", "origin", "charsets", "gl", "pending_wrap")

    def __init__(self, screen: Screen) -> None:
        self.x = screen.x
        self.y = screen.y
        self.pen = screen.pen
        self.origin = screen.origin
        self.charsets = list(screen.charsets)
        self.gl = screen.gl
        self.pending_wrap = screen.pending_wrap


class Screen:
    """The screen a program draws on, fed with :meth:`feed`.

    ``lines`` is the screen being shown (the main one, or the alternate one
    while a full-screen program has it), ``x`` and ``y`` the cursor, and
    ``scrollback`` the lines that scrolled off the top of the main screen,
    oldest first.  Answers to the program's queries collect in ``replies``
    for the caller to write back, through :meth:`take_replies`.
    """

    def __init__(self, rows: int = 24, cols: int = 80,
                 scrollback: int = SCROLLBACK) -> None:
        self.rows = max(1, rows)
        self.cols = max(1, cols)
        self.scrollback: collections.deque[Line] = collections.deque(
            maxlen=max(0, scrollback))
        #: How many lines have ever gone into the scrollback, so a viewer
        #: scrolled back can stay on the same lines as more arrive beneath.
        self.history_count = 0
        self.replies: list[bytes] = []
        self._reply_bytes = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = GROUND
        self._params = ""
        self._inter = ""
        self._string_state = OSC
        self.reset()

    # -- state -------------------------------------------------------------
    def reset(self) -> None:
        """Everything back to power-on (RIS), except the scrollback."""
        self.main = [Line(self.cols) for _ in range(self.rows)]
        self.alt = [Line(self.cols) for _ in range(self.rows)]
        self.lines = self.main
        self.alt_active = False
        self.x = 0
        self.y = 0
        self.pending_wrap = False
        self.pen: Attr = PLAIN
        self.top = 0
        self.bottom = self.rows - 1
        self.tabs = set(range(8, self.cols, 8))
        self.autowrap = True
        self.origin = False
        self.insert = False
        self.newline_mode = False
        self.app_cursor = False
        self.app_keypad = False
        self.cursor_visible = True
        self.reverse_video = False
        self.bracketed_paste = False
        self.charsets = ["B", "B", "B", "B"]
        self.gl = 0
        self._single_shift: int | None = None
        self._last_char = " "
        self._saved: dict[bool, _Saved | None] = {False: None, True: None}

    def _soft_reset(self) -> None:
        """DECSTR: the modes back to their defaults, the screen left alone."""
        self.cursor_visible = True
        self.insert = False
        self.origin = False
        self.autowrap = True
        self.app_cursor = False
        self.app_keypad = False
        self.top = 0
        self.bottom = self.rows - 1
        self.pen = PLAIN
        self.charsets = ["B", "B", "B", "B"]
        self.gl = 0
        self.pending_wrap = False
        self._saved[self.alt_active] = None

    def display(self) -> list[str]:
        """The text of every row on screen, trailing blanks trimmed."""
        return [line.text() for line in self.lines]

    def take_replies(self) -> bytes:
        """What the program asked to be told, emptied as it is taken."""
        data = b"".join(self.replies)
        self.replies = []
        self._reply_bytes = 0
        return data

    def _reply(self, data: bytes) -> None:
        if self._reply_bytes + len(data) <= MAX_REPLY_BYTES:
            self.replies.append(data)
            self._reply_bytes += len(data)

    # -- input -------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        """Take the next bytes of the program's output.

        A UTF-8 character or an escape sequence split across two calls is
        put back together, so the caller can pass reads as they arrive.
        """
        self.feed_text(self._decoder.decode(data))

    def feed_text(self, text: str) -> None:
        """Take output that has already been decoded."""
        i = 0
        end = len(text)
        while i < end:
            if self._state == GROUND:
                match = _TEXT.match(text, i)
                if match:
                    self._print(match.group())
                    i = match.end()
                    continue
                ch = text[i]
                if ch == "\x1b":
                    csi = _CSI.match(text, i)
                    if csi:
                        self._dispatch_csi(*csi.groups())
                        i = csi.end()
                        continue
                    self._state = ESCAPE
                    self._inter = ""
                else:
                    self._control(ch)
            else:
                self._step(text[i])
            i += 1

    def _step(self, ch: str) -> None:
        """One character of an escape sequence or a string, in progress."""
        state = self._state
        if state == ESCAPE:
            if ch == "\x1b":
                self._inter = ""
            elif ch in ("\x18", "\x1a"):          # CAN, SUB: abandon it
                self._state = GROUND
            elif ch < " ":
                self._control(ch)
            elif ch <= "/":
                if len(self._inter) < 2:
                    self._inter += ch
            elif ch <= "~":
                self._state = GROUND
                if not self._inter and ch == "[":
                    self._state = CSI
                    self._params = ""
                elif not self._inter and ch == "]":
                    self._state = self._string_state = OSC
                elif not self._inter and ch in "PX^_":
                    # DCS, SOS, PM and APC: strings nothing here acts on.
                    self._state = self._string_state = STRING
                else:
                    self._dispatch_esc(self._inter, ch)
            else:
                # DEL, or a character no escape sequence contains: give up on
                # the sequence and treat the character as output.
                self._state = GROUND
                if ch >= "\xa0":
                    self._print(ch)
        elif state == CSI:
            if "@" <= ch <= "~":
                self._state = GROUND
                self._dispatch_csi(self._params, self._inter, ch)
            elif "0" <= ch <= "?":
                if len(self._params) < MAX_CSI_PARAMS:
                    self._params += ch
            elif " " <= ch <= "/":
                if len(self._inter) < 2:
                    self._inter += ch
            elif ch == "\x1b":
                self._state = ESCAPE
                self._inter = ""
            elif ch in ("\x18", "\x1a"):
                self._state = GROUND
            elif ch < " ":
                # A control character inside a sequence is acted on and the
                # sequence carries on: what vttest checks, and what xterm does.
                self._control(ch)
            # Anything else -- DEL, or text that arrived mid-sequence -- is
            # swallowed until the final byte resynchronises the stream.
        elif state in (OSC, STRING):
            if ch == "\x1b":
                self._state = OSC_ESC if state == OSC else STRING_ESC
            elif ch < " ":
                # A string is text, so a control character means it was never
                # terminated -- binary output, or a program killed mid-title.
                # BEL is the usual end of an OSC; anything else is acted on,
                # the way a real terminal does.  Swallowing on regardless is
                # how one stray "ESC ]" in a "cat" once blanked the rest of a
                # session.
                self._state = GROUND
                if ch != "\x07":
                    self._control(ch)
            # The string's text itself is dropped: titles, colour queries and
            # DCS requests are not acted on, and keeping them would let an
            # unterminated one grow without bound.
        else:                                      # OSC_ESC, STRING_ESC
            # ESC \ is the string terminator; any other escape inside a string
            # is part of the string, which carries on.
            self._state = GROUND if ch == "\\" else self._string_state

    # -- printing ----------------------------------------------------------
    def _print(self, text: str) -> None:
        if self._single_shift is not None:
            # SS2/SS3: one character from G2 or G3, then back to GL.
            shift = self._single_shift
            self._single_shift = None
            self._emit(self._translate(text[0], self.charsets[shift]))
            text = text[1:]
            if not text:
                return
        self._emit(self._translate(text, self.charsets[self.gl]))

    @staticmethod
    def _translate(text: str, charset: str) -> str:
        if charset == "0":
            return text.translate(DEC_GRAPHICS)
        if charset == "A":
            return text.translate(UK)
        return text

    def _emit(self, text: str) -> None:
        """Put already-translated characters on the screen at the cursor."""
        if text.isascii() and not self.insert:
            self._emit_narrow(text)
        else:
            for ch in text:
                self._emit_char(ch)

    def _emit_narrow(self, text: str) -> None:
        """The fast path: characters that are one column each, overwriting.

        Whole runs are copied into the line at once, which is most of what
        makes ``cat`` of a large file bearable in Python.
        """
        cols = self.cols
        pen = self.pen
        self._last_char = text[-1]
        while text:
            if self.pending_wrap:
                self._wrap()
            line = self.lines[self.y]
            x = self.x
            room = cols - x
            if len(text) > room and not self.autowrap:
                # With no wrapping everything past the margin lands on the
                # last cell, so only the last character of the excess counts.
                text = text[:room - 1] + text[-1]
            count = min(room, len(text))
            end = x + count
            self._guard(line, x)
            self._guard(line, end)
            line.chars[x:end] = text[:count]
            line.attrs[x:end] = [pen] * count
            text = text[count:]
            if end >= cols:
                self.x = cols - 1
                self.pending_wrap = self.autowrap
            else:
                self.x = end

    def _emit_char(self, ch: str) -> None:
        """One character of any width, with insert mode and combining marks."""
        width = char_width(ch)
        if width == 0:
            self._combine(ch)
            return
        cols = self.cols
        if width == 2 and cols < 2:
            ch, width = "?", 1
        if self.pending_wrap:
            self._wrap()
        if width == 2 and self.x == cols - 1:
            # A wide character does not fit in the last column: it moves to
            # the next line whole, or, with no wrapping, back one column.
            if self.autowrap:
                self._guard(self.lines[self.y], self.x)
                self.lines[self.y].chars[self.x] = " "
                self._wrap()
            else:
                self.x = cols - 2
        line = self.lines[self.y]
        x = self.x
        if self.insert:
            self._insert_blanks(line, x, width)
        self._guard(line, x)
        self._guard(line, x + width)
        line.chars[x] = ch
        line.attrs[x] = self.pen
        if width == 2:
            line.chars[x + 1] = ""
            line.attrs[x + 1] = self.pen
        self._last_char = ch
        if x + width >= cols:
            self.x = cols - 1
            self.pending_wrap = self.autowrap
        else:
            self.x = x + width

    def _combine(self, mark: str) -> None:
        """Add a combining mark to the character just written."""
        x = self.x if self.pending_wrap else self.x - 1
        if x < 0:
            return                       # nothing to combine with: dropped
        chars = self.lines[self.y].chars
        if chars[x] == "":
            x -= 1                       # the right half of a wide character
        if len(chars[x]) <= MAX_MARKS:
            chars[x] += mark

    def _guard(self, line: Line, x: int) -> None:
        """Blank a wide character that an edit at column ``x`` would split.

        Half a wide character cannot be drawn, so writing over either half,
        or inserting or deleting between them, takes the whole character
        away -- what xterm does too.
        """
        chars = line.chars
        if 0 < x < len(chars) and chars[x] == "":
            chars[x - 1] = " "
            chars[x] = " "

    def _wrap(self) -> None:
        self.lines[self.y].wrapped = True
        self.x = 0
        self.pending_wrap = False
        self._index()

    # -- control characters --------------------------------------------------
    def _control(self, ch: str) -> None:
        if ch in "\n\x0b\x0c":
            self._index()
            if self.newline_mode:
                self.x = 0
        elif ch == "\r":
            self.x = 0
            self.pending_wrap = False
        elif ch == "\b":
            self._cursor_back([1])
        elif ch == "\t":
            self._tab(1)
        elif ch == "\x0e":                 # SO: G1 into use
            self.gl = 1
        elif ch == "\x0f":                 # SI: back to G0
            self.gl = 0
        # BEL, NUL and the rest of the C0 and C1 controls do nothing here.

    def _index(self) -> None:
        """Down a line, scrolling the region when at its bottom (IND)."""
        self.pending_wrap = False
        if self.y == self.bottom:
            self._scroll_up(1)
        elif self.y < self.rows - 1:
            self.y += 1

    def _reverse_index(self) -> None:
        """Up a line, scrolling the region down when at its top (RI)."""
        self.pending_wrap = False
        if self.y == self.top:
            self._scroll_down(1)
        elif self.y > 0:
            self.y -= 1

    def _tab(self, count: int) -> None:
        self.pending_wrap = False
        x = self.x
        for _ in range(min(count, self.cols)):
            later = [stop for stop in self.tabs if stop > x]
            x = min(later) if later else self.cols - 1
        self.x = x

    def _back_tab(self, count: int) -> None:
        self.pending_wrap = False
        x = self.x
        for _ in range(min(count, self.cols)):
            earlier = [stop for stop in self.tabs if stop < x]
            x = max(earlier) if earlier else 0
        self.x = x

    # -- scrolling -----------------------------------------------------------
    def _blank(self) -> Attr:
        """What an erased cell looks like: the pen's background, no more.

        ``xterm-256color`` promises ``bce`` (background colour erase), so a
        program that sets a background and clears the screen expects that
        colour everywhere -- which is how full-screen programs paint their
        backgrounds.
        """
        return (DEFAULT, self.pen[1], 0)

    def _scroll_up(self, count: int) -> None:
        top, bottom = self.top, self.bottom
        count = min(count, bottom - top + 1)
        lines = self.lines
        gone = lines[top:top + count]
        del lines[top:top + count]
        blank = self._blank()
        lines[bottom - count + 1:bottom - count + 1] = [
            Line(self.cols, blank) for _ in range(count)]
        # Only what leaves the top of the whole main screen is history: lines
        # scrolled out of a region further down, or off the alternate screen,
        # are a program redrawing its own window.
        if top == 0 and not self.alt_active:
            self.scrollback.extend(gone)
            self.history_count += len(gone)

    def _scroll_down(self, count: int) -> None:
        top, bottom = self.top, self.bottom
        count = min(count, bottom - top + 1)
        lines = self.lines
        del lines[bottom - count + 1:bottom + 1]
        blank = self._blank()
        lines[top:top] = [Line(self.cols, blank) for _ in range(count)]

    # -- escape sequences ----------------------------------------------------
    def _dispatch_esc(self, inter: str, final: str) -> None:
        if inter:
            if inter in ("(", ")", "*", "+"):
                self.charsets["()*+".index(inter)] = final
            elif inter == "#" and final == "8":
                self._alignment_test()
            # ESC % G (UTF-8) and the rest: UTF-8 is all this ever speaks.
            return
        if final == "7":
            self._save_cursor()
        elif final == "8":
            self._restore_cursor()
        elif final == "D":
            self._index()
        elif final == "E":
            self.x = 0
            self._index()
        elif final == "M":
            self._reverse_index()
        elif final == "H":
            self.tabs.add(self.x)
        elif final == "c":
            self.reset()
        elif final == "=":
            self.app_keypad = True
        elif final == ">":
            self.app_keypad = False
        elif final in ("N", "O"):
            self._single_shift = 2 if final == "N" else 3
        elif final in ("n", "o"):
            self.gl = 2 if final == "n" else 3
        # ESC \ on its own (a stray string terminator) and the rest: nothing.

    def _alignment_test(self) -> None:
        """DECALN: fill the screen with E, the old way of lining up a CRT."""
        for line in self.lines:
            line.chars[:] = ["E"] * self.cols
            line.attrs[:] = [PLAIN] * self.cols
            line.wrapped = False
        self.top, self.bottom = 0, self.rows - 1
        self.origin = False
        self.x = self.y = 0
        self.pending_wrap = False

    def _save_cursor(self) -> None:
        self._saved[self.alt_active] = _Saved(self)

    def _restore_cursor(self) -> None:
        saved = self._saved[self.alt_active]
        if saved is None:
            # Nothing saved: xterm restores the power-on state.
            self.x = self.y = 0
            self.pen = PLAIN
            self.origin = False
            self.charsets = ["B", "B", "B", "B"]
            self.gl = 0
            self.pending_wrap = False
            return
        self.x = min(saved.x, self.cols - 1)
        self.y = min(saved.y, self.rows - 1)
        self.pen = saved.pen
        self.origin = saved.origin
        self.charsets = list(saved.charsets)
        self.gl = saved.gl
        self.pending_wrap = saved.pending_wrap and self.x == self.cols - 1

    # -- CSI sequences -------------------------------------------------------
    def _dispatch_csi(self, params: str, inter: str, final: str) -> None:
        marker = ""
        if params[:1] in ("<", "=", ">", "?"):
            marker, params = params[0], params[1:]
        if inter:
            if inter == "!" and final == "p" and not marker:
                self._soft_reset()
            # " q" (cursor shape), "$p" (mode queries) and the rest: dropped.
            return
        if final == "m" and not marker:
            self._sgr(params)
            return
        args = [_number(part.split(":")[0])
                for part in params.split(";")[:MAX_PARAMS]]
        if marker == "?":
            if final in ("h", "l"):
                self._dec_modes(args, final == "h")
            elif final == "n" and args[0] == 6:
                self._reply(b"\x1b[?%d;%dR" % self._position())
            elif final in ("J", "K"):          # DECSED, DECSEL: as ED, EL
                self._HANDLERS[final](self, args)
            return
        if marker == ">":
            if final == "c":
                # Secondary device attributes: a VT220, firmware 10.
                self._reply(b"\x1b[>1;10;0c")
            return
        if marker:
            return
        handler = self._HANDLERS.get(final)
        if handler is not None:
            handler(self, args)

    def _position(self) -> tuple[int, int]:
        """The cursor as the program counts it: from 1, and from the region
        top in origin mode."""
        row = self.y - (self.top if self.origin else 0)
        return row + 1, self.x + 1

    def _move_to(self, row: int, col: int) -> None:
        """Absolute positioning, from 0, origin mode taken into account."""
        if self.origin:
            row = max(self.top, min(self.bottom, row + self.top))
        else:
            row = max(0, min(self.rows - 1, row))
        self.y = row
        self.x = max(0, min(self.cols - 1, col))
        self.pending_wrap = False

    def _cursor_up(self, args) -> None:
        floor = self.top if self.y >= self.top else 0
        self.y = max(floor, self.y - (args[0] or 1))
        self.pending_wrap = False

    def _cursor_down(self, args) -> None:
        ceiling = self.bottom if self.y <= self.bottom else self.rows - 1
        self.y = min(ceiling, self.y + (args[0] or 1))
        self.pending_wrap = False

    def _cursor_forward(self, args) -> None:
        self.x = min(self.cols - 1, self.x + (args[0] or 1))
        self.pending_wrap = False

    def _cursor_back(self, args) -> None:
        # With a wrap pending the cursor is, in effect, one past the last
        # column, so the first step back lands on the last column itself --
        # as in VTE and tmux.
        x = self.cols if self.pending_wrap else self.x
        self.x = max(0, x - (args[0] or 1))
        self.pending_wrap = False

    def _next_line(self, args) -> None:
        self._cursor_down(args)
        self.x = 0

    def _previous_line(self, args) -> None:
        self._cursor_up(args)
        self.x = 0

    def _column(self, args) -> None:
        self.x = max(0, min(self.cols - 1, (args[0] or 1) - 1))
        self.pending_wrap = False

    def _row(self, args) -> None:
        self._move_to((args[0] or 1) - 1, self.x)

    def _position_cursor(self, args) -> None:
        row = args[0] or 1
        col = (args[1] if len(args) > 1 else 0) or 1
        self._move_to(row - 1, col - 1)

    def _here(self) -> int:
        """Where an erase "from the cursor" starts.

        With a wrap pending the cursor is past the last column, so nothing on
        its line is to the right of it.  top writes lines that exactly fill
        the width and then erases to the end of each: erasing from the last
        column instead took the last character of every such line away.
        """
        return self.cols if self.pending_wrap else self.x

    def _erase_display(self, args) -> None:
        mode = args[0]
        blank = self._blank()
        if mode == 0:
            self._erase(self.lines[self.y], self._here(), self.cols, blank)
            for line in self.lines[self.y + 1:]:
                self._erase(line, 0, self.cols, blank)
        elif mode == 1:
            for line in self.lines[:self.y]:
                self._erase(line, 0, self.cols, blank)
            self._erase(self.lines[self.y], 0, self.x + 1, blank)
        elif mode == 2:
            for line in self.lines:
                self._erase(line, 0, self.cols, blank)
        elif mode == 3:
            self.scrollback.clear()

    def _erase_line(self, args) -> None:
        mode = args[0]
        line = self.lines[self.y]
        blank = self._blank()
        if mode == 0:
            self._erase(line, self._here(), self.cols, blank)
        elif mode == 1:
            self._erase(line, 0, self.x + 1, blank)
        elif mode == 2:
            self._erase(line, 0, self.cols, blank)

    def _erase_chars(self, args) -> None:
        start = self._here()
        self._erase(self.lines[self.y], start,
                    min(self.cols, start + (args[0] or 1)), self._blank())

    def _erase(self, line: Line, start: int, end: int, blank: Attr) -> None:
        # A pending wrap survives an erase: the next character still goes to
        # the next line, as it does in VTE and tmux.
        self._guard(line, start)
        self._guard(line, end)
        line.chars[start:end] = [" "] * (end - start)
        line.attrs[start:end] = [blank] * (end - start)
        if end >= self.cols:
            line.wrapped = False

    def _insert_chars(self, args) -> None:
        if not self.pending_wrap:      # past the last column: nothing to move
            self._insert_blanks(self.lines[self.y], self.x, args[0] or 1)

    def _insert_blanks(self, line: Line, x: int, count: int) -> None:
        cols = self.cols
        count = min(count, cols - x)
        self._guard(line, x)
        blank = self._blank()
        line.chars[x:x] = [" "] * count
        line.attrs[x:x] = [blank] * count
        del line.chars[cols:]
        del line.attrs[cols:]
        # A wide character pushed half off the end cannot be drawn either.
        if char_width(line.chars[-1][:1] or " ") == 2:
            line.chars[-1] = " "

    def _delete_chars(self, args) -> None:
        if self.pending_wrap:          # past the last column: nothing to take
            return
        line = self.lines[self.y]
        x = self.x
        count = min(args[0] or 1, self.cols - x)
        self._guard(line, x)
        self._guard(line, x + count)
        del line.chars[x:x + count]
        del line.attrs[x:x + count]
        blank = self._blank()
        line.chars.extend([" "] * count)
        line.attrs.extend([blank] * count)

    def _insert_lines(self, args) -> None:
        if not self.top <= self.y <= self.bottom:
            return
        count = min(args[0] or 1, self.bottom - self.y + 1)
        lines = self.lines
        del lines[self.bottom - count + 1:self.bottom + 1]
        blank = self._blank()
        lines[self.y:self.y] = [Line(self.cols, blank) for _ in range(count)]
        self.x = 0
        self.pending_wrap = False

    def _delete_lines(self, args) -> None:
        if not self.top <= self.y <= self.bottom:
            return
        count = min(args[0] or 1, self.bottom - self.y + 1)
        lines = self.lines
        del lines[self.y:self.y + count]
        blank = self._blank()
        at = self.bottom - count + 1
        lines[at:at] = [Line(self.cols, blank) for _ in range(count)]
        self.x = 0
        self.pending_wrap = False

    def _scroll_up_cmd(self, args) -> None:
        self._scroll_up(args[0] or 1)

    def _scroll_down_cmd(self, args) -> None:
        if len(args) > 1:
            return              # CSI ... T with five numbers: mouse tracking
        self._scroll_down(args[0] or 1)

    def _repeat(self, args) -> None:
        count = min(args[0] or 1, self.rows * self.cols * MAX_REPEAT_SCREENS)
        self._emit(self._last_char * count)

    def _device_attributes(self, args) -> None:
        if args[0] == 0:
            # A VT220 with ANSI colour: what the programs that ask need to
            # hear, and nothing that invites them to ask for more.
            self._reply(b"\x1b[?62;22c")

    def _clear_tabs(self, args) -> None:
        if args[0] == 0:
            self.tabs.discard(self.x)
        elif args[0] == 3:
            self.tabs.clear()

    def _set_modes(self, args) -> None:
        self._ansi_modes(args, True)

    def _reset_modes(self, args) -> None:
        self._ansi_modes(args, False)

    def _ansi_modes(self, args, on: bool) -> None:
        for mode in args:
            if mode == 4:
                self.insert = on
            elif mode == 20:
                self.newline_mode = on

    def _status_report(self, args) -> None:
        if args[0] == 5:
            self._reply(b"\x1b[0n")              # "terminal OK"
        elif args[0] == 6:
            self._reply(b"\x1b[%d;%dR" % self._position())

    def _set_region(self, args) -> None:
        top = args[0] or 1
        bottom = (args[1] if len(args) > 1 else 0) or self.rows
        bottom = min(bottom, self.rows)
        if top >= bottom:
            return              # not a region at all: xterm ignores it too
        self.top, self.bottom = top - 1, bottom - 1
        self._move_to(0, 0)

    def _save_cursor_cmd(self, args) -> None:
        self._save_cursor()

    def _restore_cursor_cmd(self, args) -> None:
        self._restore_cursor()

    def _window_ops(self, args) -> None:
        if args[0] == 18:
            self._reply(b"\x1b[8;%d;%dt" % (self.rows, self.cols))
        # Resizing, moving, iconifying and the title stack are not ours to do.

    def _tab_forward(self, args) -> None:
        self._tab(args[0] or 1)

    def _tab_back(self, args) -> None:
        self._back_tab(args[0] or 1)

    _HANDLERS = {
        "@": _insert_chars,
        "A": _cursor_up,
        "B": _cursor_down,
        "C": _cursor_forward,
        "D": _cursor_back,
        "E": _next_line,
        "F": _previous_line,
        "G": _column,
        "H": _position_cursor,
        "I": _tab_forward,
        "J": _erase_display,
        "K": _erase_line,
        "L": _insert_lines,
        "M": _delete_lines,
        "P": _delete_chars,
        "S": _scroll_up_cmd,
        "T": _scroll_down_cmd,
        "X": _erase_chars,
        "Z": _tab_back,
        "`": _column,
        "a": _cursor_forward,
        "b": _repeat,
        "c": _device_attributes,
        "d": _row,
        "e": _cursor_down,
        "f": _position_cursor,
        "g": _clear_tabs,
        "h": _set_modes,
        "l": _reset_modes,
        "n": _status_report,
        "r": _set_region,
        "s": _save_cursor_cmd,
        "t": _window_ops,
        "u": _restore_cursor_cmd,
    }

    # -- modes ---------------------------------------------------------------
    def _dec_modes(self, args, on: bool) -> None:
        for mode in args:
            if mode == 1:
                self.app_cursor = on
            elif mode == 5:
                self.reverse_video = on
            elif mode == 6:
                self.origin = on
                self._move_to(0, 0)
            elif mode == 7:
                self.autowrap = on
                if not on:
                    self.pending_wrap = False
            elif mode == 25:
                self.cursor_visible = on
            elif mode == 66:
                self.app_keypad = on
            elif mode == 47:
                self._use_alternate(on)
            elif mode == 1047:
                if not on and self.alt_active:
                    self._clear_alternate()
                self._use_alternate(on)
            elif mode == 1048:
                if on:
                    self._save_cursor()
                else:
                    self._restore_cursor()
            elif mode == 1049:
                self._alternate_with_cursor(on)
            elif mode == 2004:
                self.bracketed_paste = on
            # Mouse reporting, focus events, the 132-column switch and the
            # rest are accepted and ignored.

    def _use_alternate(self, on: bool) -> None:
        self.alt_active = on
        self.lines = self.alt if on else self.main
        self.pending_wrap = False

    def _clear_alternate(self) -> None:
        blank = self._blank()
        for line in self.alt:
            self._erase(line, 0, self.cols, blank)

    def _alternate_with_cursor(self, on: bool) -> None:
        """Mode 1049: what vim, less and htop use, and why they leave the
        shell's screen as it was when they exit.

        Setting it saves the cursor and switches to a cleared alternate
        screen; resetting it switches back to the main screen -- untouched all
        the while -- and puts the cursor back.  Each only acts when it changes
        screens: set twice, the alternate screen is not cleared again, and a
        stray reset on the main screen does not send the cursor anywhere.
        """
        if on:
            if not self.alt_active:
                self._save_cursor()
                self._use_alternate(True)
                self._clear_alternate()
        elif self.alt_active:
            self._use_alternate(False)
            self._restore_cursor()

    # -- SGR -----------------------------------------------------------------
    def _sgr(self, params: str) -> None:
        fg, bg, flags = self.pen
        parts = params.split(";")[:MAX_PARAMS]
        i = 0
        while i < len(parts):
            part = parts[i]
            i += 1
            if ":" in part:
                # The ITU spelling, with its own sub-parameters:
                # 38:5:n, 38:2::r:g:b (or 38:2:r:g:b), and 4:n underline styles.
                sub = part.split(":")
                code = _number(sub[0])
                if code in (38, 48):
                    colour = _colon_colour(sub[1:])
                    if colour is not None:
                        if code == 38:
                            fg = colour
                        else:
                            bg = colour
                elif code == 4:
                    if _number(sub[1]):
                        flags |= UNDERLINE
                    else:
                        flags &= ~UNDERLINE
                continue
            code = _number(part)
            if code == 0:
                fg, bg, flags = DEFAULT, DEFAULT, 0
            elif code in _SGR_ON:
                flags |= _SGR_ON[code]
            elif code in _SGR_OFF:
                flags &= ~_SGR_OFF[code]
            elif 30 <= code <= 37:
                fg = code - 30
            elif 40 <= code <= 47:
                bg = code - 40
            elif 90 <= code <= 97:
                fg = code - 90 + 8
            elif 100 <= code <= 107:
                bg = code - 100 + 8
            elif code == 39:
                fg = DEFAULT
            elif code == 49:
                bg = DEFAULT
            elif code in (38, 48, 58):
                # The semicolon spelling: the colour's numbers follow as
                # parameters of their own, and are consumed even for 58
                # (underline colour, not drawn) so they are not read as
                # attributes.
                colour, used = _semicolon_colour(parts, i)
                i += used
                if colour is not None and code != 58:
                    if code == 38:
                        fg = colour
                    else:
                        bg = colour
        self.pen = (fg, bg, flags)

    # -- size ----------------------------------------------------------------
    def resize(self, rows: int, cols: int) -> None:
        """Take a new size.  Lines are cut or padded, not re-flowed.

        A shorter screen keeps the cursor's line: lines below the cursor go
        first, then lines from the top, which on the main screen go into the
        scrollback -- the shell's prompt stays in view, as in xterm.
        """
        rows, cols = max(1, rows), max(1, cols)
        if (rows, cols) == (self.rows, self.cols):
            return
        old_cols = self.cols
        for buffer in (self.main, self.alt):
            for line in buffer:
                line.resize(cols)
        self.cols = cols
        main_cursor = self.y
        saved_main = self._saved[False]
        if self.alt_active:
            main_cursor = saved_main.y if saved_main is not None else 0
        dropped = self._fit(self.main, rows, main_cursor, history=True)
        alt_dropped = self._fit(self.alt, rows,
                                self.y if self.alt_active else 0,
                                history=False)
        self.lines = self.alt if self.alt_active else self.main
        self.y -= alt_dropped if self.alt_active else dropped
        if saved_main is not None:
            saved_main.y -= dropped
        saved_alt = self._saved[True]
        if saved_alt is not None:
            saved_alt.y -= alt_dropped
        self.rows = rows
        self.top, self.bottom = 0, rows - 1
        self.x = max(0, min(cols - 1, self.x))
        self.y = max(0, min(rows - 1, self.y))
        self.pending_wrap = False
        for saved in self._saved.values():
            if saved is not None:
                saved.x = max(0, min(cols - 1, saved.x))
                saved.y = max(0, min(rows - 1, saved.y))
        self.tabs = {stop for stop in self.tabs if stop < cols}
        start = (old_cols + 7) // 8 * 8
        self.tabs.update(range(max(8, start), cols, 8))

    def _fit(self, lines: list[Line], rows: int, cursor: int,
             history: bool) -> int:
        """Grow or shrink one screen's lines to ``rows``; return how many went
        off the top."""
        have = len(lines)
        if rows > have:
            lines.extend(Line(self.cols) for _ in range(rows - have))
            return 0
        drop = max(0, cursor - (rows - 1))
        if history and drop:
            self.scrollback.extend(lines[:drop])
            self.history_count += drop
        del lines[:drop]
        del lines[rows:]
        return drop


#: SGR codes that turn a flag on, and those that turn flags off.
_SGR_ON = {1: BOLD, 2: DIM, 3: ITALIC, 4: UNDERLINE, 5: BLINK, 6: BLINK,
           7: REVERSE, 8: INVISIBLE, 9: STRIKE, 21: UNDERLINE}
_SGR_OFF = {22: BOLD | DIM, 23: ITALIC, 24: UNDERLINE, 25: BLINK,
            27: REVERSE, 28: INVISIBLE, 29: STRIKE}


def _rgb(red: str, green: str, blue: str) -> int | None:
    values = [_number(red), _number(green), _number(blue)]
    if max(values) > 255:
        return None
    return RGB | (values[0] << 16) | (values[1] << 8) | values[2]


def _semicolon_colour(parts: list[str], i: int) -> tuple[int | None, int]:
    """The colour that ``38;`` or ``48;`` introduces, and how many of the
    following parameters it took."""
    if i >= len(parts):
        return None, 0
    kind = _number(parts[i])
    if kind == 5:
        if i + 1 >= len(parts):
            return None, len(parts) - i
        index = _number(parts[i + 1])
        return (index if index <= 255 else None), 2
    if kind == 2:
        if i + 3 >= len(parts):
            return None, len(parts) - i
        return _rgb(*parts[i + 1:i + 4]), 4
    return None, 1


def _colon_colour(sub: list[str]) -> int | None:
    """The colour in ``38:5:n``, ``38:2:r:g:b`` or ``38:2:space:r:g:b``.

    ``sub`` is what followed the 38 or 48, so it always has the kind at least:
    the part it came from had a colon in it.
    """
    kind = _number(sub[0])
    if kind == 5 and len(sub) >= 2:
        index = _number(sub[1])
        return index if index <= 255 else None
    if kind == 2 and len(sub) >= 4:
        # With a colour-space id the channels are the last three.
        channels = sub[2:5] if len(sub) >= 5 else sub[1:4]
        return _rgb(*channels)
    return None
