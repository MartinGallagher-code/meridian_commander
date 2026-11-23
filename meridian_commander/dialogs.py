# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Reusable curses dialogs: messages, prompts, menus, drop-downs, progress.

These are deliberately self-contained helpers that draw a centred window over
the main screen, run their own tiny input loop, and return a value.  Keeping
them here keeps :mod:`meridian_commander.app` focused on file-manager logic.

They are drawn the way Turbo Vision drew them, because that is what the rest
of the application looks like: a grey box with a double-line frame, a close
box in the top-left corner, the caption between ``╡ ╞`` brackets, red
accelerator letters, blue input fields, green buttons with their own little
drop shadow, and the whole box casting a shadow two columns right and one row
down onto whatever it covers.  The colours come from
:mod:`meridian_commander.theme`, so a monochrome terminal still gets the
layout and loses only the paint.
"""

from __future__ import annotations

import curses
import textwrap
from dataclasses import dataclass
from typing import Callable, List, Sequence, Tuple

from . import theme
from .util import read_key, typed_char


def _cast_shadow(stdscr, y: int, x: int, height: int, width: int) -> None:
    """Darken the cells the dialog's shadow falls on, on the screen below it.

    The shadow cannot live in the dialog's own window -- it falls *outside*
    it -- so it is painted onto the screen underneath before the window is
    created.  The application's next full redraw wipes it, which is exactly
    when the dialog has gone.
    """
    try:
        theme.shadow(stdscr, y, x, height, width)
        stdscr.noutrefresh()
    except (curses.error, AttributeError):
        # A stand-in screen in a test, or a terminal too small for the cells
        # the shadow would fall on.  Neither is worth refusing the dialog for.
        pass


def _center(stdscr, height: int, width: int):
    """A centred window, never larger than the screen and never smaller than
    one cell.

    The clamp at the bottom is what keeps a dialog from taking the
    application down with it: on a terminal one row tall (or one column
    wide) the room left over is negative, and ``newwin`` given a negative
    size raises rather than returning a window.  The application refuses to
    *draw* a screen that small, but the keys still work, so a prompt could
    still be asked for -- and it ended in a traceback.  A one-cell dialog is
    useless, and better than that.
    """
    max_y, max_x = stdscr.getmaxyx()
    width = max(1, min(width, max_x - 2))
    height = max(1, min(height, max_y - 2))
    y = max(0, (max_y - height) // 2)
    x = max(0, (max_x - width) // 2)
    _cast_shadow(stdscr, y, x, height, width)
    win = curses.newwin(height, width, y, x)
    win.keypad(True)
    theme.background(win, "dialog")
    win.attrset(theme.attr("dialog"))
    return win


def _box(win, title: str = "", footer: str = "") -> None:
    """Erase the window and draw the framed grey box the dialogs live in."""
    win.erase()
    h, w = win.getmaxyx()
    theme.frame(win, 0, 0, h, w, "dialogframe", title=title,
                title_role="dialogtitle", footer=footer,
                footer_role="dialogdim")


def _buttons(win, y: int, specs: list[tuple[str, bool]], width: int) -> None:
    """A centred row of push buttons; ``specs`` is (caption, focused) pairs."""
    widths = [max(10, len(theme.strip_hotkey(caption)) + 4)
              for caption, _ in specs]
    total = sum(widths) + 2 * (len(widths) - 1)
    x = max(2, (width - total) // 2)
    for (caption, focused), bw in zip(specs, widths):
        x = theme.button(win, y, x, caption, width=bw, focused=focused)


# -- scrolling text ------------------------------------------------------------
#
# A message and the help both show text that may be taller than the screen.
# Each lays its text out as columns of rows for the width it is given, and
# one loop draws whichever rows are in view and scrolls through the rest.

#: One run of text in a row: its column within the row, the text, its role.
Segment = Tuple[int, str, str]
#: A row of text: the runs it is painted as, left to right.
Row = Tuple[Segment, ...]
#: A column of rows: where it starts in the text area, how wide it may paint,
#: and its rows, top to bottom.
Column = Tuple[int, int, List[Row]]

#: Keys that close a dialog whose text scrolls: Esc and Enter.  Any other key
#: either scrolls it or leaves it where it is.
CLOSE_KEYS = (27, 10, 13, curses.KEY_ENTER)

#: Said in the footer while the text scrolls, after the position; and by the
#: help, which only these keys close, even when it does not scroll.
CLOSE_HINT = "Esc/Enter close"
#: Said after that, when the footer has room for all of it.
SCROLL_HINT = "Up/Down PgUp/PgDn Home/End"


def _scrolled(key: int | str, top: int, body: int, total: int) -> int | None:
    """Where a scroll key moves the top row, or None for any other key.

    Not clamped here: :func:`_pager` clamps the top row every time it draws,
    since a resize can move the ends as well as a key.
    """
    if key in (curses.KEY_UP, ord("k")):
        top -= 1
    elif key in (curses.KEY_DOWN, ord("j")):
        top += 1
    elif key == curses.KEY_PPAGE:
        top -= max(1, body)
    elif key == curses.KEY_NPAGE:
        top += max(1, body)
    elif key == curses.KEY_HOME:
        top = 0
    elif key == curses.KEY_END:
        top = total
    else:
        return None
    return top


def _pager(stdscr, title: str, width: int,
           layout: Callable[[int], List[Column]], idle_footer: str,
           any_key: bool) -> None:
    """Show text laid out by ``layout``, scrolling whatever does not fit.

    ``layout(text_width)`` returns the text as :data:`Column` s for a text
    area that many cells wide (at least one).  The window asks for ``width``
    and for as many rows as the text has at that width, and the screen clamps
    both.  Text that fits is drawn as a message always was, with
    ``idle_footer`` and an OK button; ``any_key`` closes it with any key.
    Text that does not fit grows a scrollbar and its position in the footer,
    the scroll keys move it, and only :data:`CLOSE_KEYS` close it -- which is
    the point of scrolling: a key meant to move the text must not throw it
    away.

    A terminal resized while it is open gets the dialog drawn again for the
    new size, rather than the old window left hanging off its edge.
    """
    laid: dict = {}

    def columns(text_width: int) -> list[Column]:
        text_width = max(1, text_width)
        if text_width not in laid:
            laid[text_width] = layout(text_width)
        return laid[text_width]

    def rows_in(cols: list[Column]) -> int:
        return max((len(rows) for _, _, rows in cols), default=0)

    def open_window():
        # As wide as asked for and the screen allows -- or narrower, when the
        # layout stops short of that (the help's columns have a widest).
        room = max(1, min(width, stdscr.getmaxyx()[1] - 2))
        cols = columns(room - 6)
        used = max((x + col_w for x, col_w, _ in cols), default=0) + 6
        return _center(stdscr, rows_in(cols) + 5, min(room, used))

    win = open_window()
    top = 0
    while True:
        win_h, win_w = win.getmaxyx()
        cols = columns(win_w - 6)
        total = rows_in(cols)
        body = max(0, win_h - 5)
        # As menu() clamps its list: never above the first row, and never so
        # far down that the last row leaves the bottom of the view.
        top = max(0, min(top, total - body))
        scrolling = total > body
        shown = min(body, total - top)
        footer = idle_footer
        if scrolling:
            footer = f"{top + shown}/{total}  {CLOSE_HINT}"
            # The keys only whole: the frame cuts a footer too long for it,
            # and half a key name reads as a fault.  The frame's footer has
            # its two brackets, two spaces, and a cell short of the inside.
            if len(footer) + 2 + len(SCROLL_HINT) + 4 <= win_w - 3:
                footer += f"  {SCROLL_HINT}"
        _box(win, title, footer)
        for x, col_w, rows in cols:
            for i, row in enumerate(rows[top:top + shown]):
                for offset, text, role in row:
                    room = max(0, min(col_w, win_w - 6 - x) - offset)
                    theme.paint(win, 1 + i, 3 + x + offset, text[:room], role)
        if scrolling:
            theme.scrollbar(win, 1, win_w - 2, body, top, body, total)
        _buttons(win, min(win_h - 3, min(total, body) + 2), [("~O~K", True)],
                 win_w)
        win.refresh()
        k = read_key(win)
        if k in CLOSE_KEYS or (any_key and not scrolling):
            return
        if k == curses.KEY_RESIZE:
            # What was behind the dialog first, so nothing of the old window
            # is left where the new one does not reach.
            stdscr.touchwin()
            stdscr.noutrefresh()
            win = open_window()
            continue
        moved = _scrolled(k, top, body, total)
        if moved is not None:
            top = moved


def message(stdscr, title: str, text: str, error: bool = False) -> None:
    """Show a modal message.

    One that fits on the screen is dismissed with any key, as it always was.
    One taller than the screen scrolls instead of being cut off: Up/Down
    (``j``/``k``), PgUp/PgDn and Home/End move it, a scrollbar and the
    footer say where it is, and Esc or Enter closes it.
    """
    lines = text.split("\n")
    width = max(len(title) + 8,
                max((len(line) for line in lines), default=0) + 6, 34)
    role = "dialogerror" if error else "dialog"
    rows: list[Row] = [((0, line, role),) for line in lines]

    def layout(text_width: int) -> list[Column]:
        # Each line keeps its own breaks and is clipped, never re-wrapped:
        # a message is often a listing, and its columns must stay columns.
        return [(0, text_width, rows)]

    _pager(stdscr, title, width, layout, "press any key", any_key=True)


# -- the help ------------------------------------------------------------------

@dataclass(frozen=True)
class HelpSection:
    """A titled part of the help: (keys, what they do) pairs, and a note.

    The descriptions are prose, not pre-wrapped: :func:`help_layout` wraps
    them to whatever width the columns come to.
    """

    title: str
    entries: Tuple[Tuple[str, str], ...]
    note: str = ""


#: Entries sit this far in from their section's title.
HELP_INDENT = 2
#: The keys' column: a description starts this far past the indent, and keys
#: as long as this or longer get a row of their own above it.
HELP_KEYS = 15
#: The narrowest a description may be wrapped to beside its keys.  Narrower
#: than that, the keys go on a row of their own and it goes under them.
HELP_TEXT_MIN = 20
#: Blank columns between the help's two columns.
HELP_GUTTER = 3
#: The narrowest column the help is split into two of.  Narrower than that
#: (a terminal under 151 columns) it stays one column, still scrolling.
HELP_COLUMN_MIN = 70
#: The widest a column grows: on a very wide terminal the help stops widening
#: rather than stretching its lines out of reading range.
HELP_COLUMN_MAX = 100


def _wrap(text: str, width: int) -> list[str]:
    # Hyphens are not break points here: "Ctrl-R" and "F-keys" stay whole.
    return textwrap.wrap(text, max(1, width), break_on_hyphens=False) or [""]


def _help_section(section: HelpSection, width: int) -> list[Row]:
    """One section's rows at ``width``: the title, the note, the entries."""
    beside = width >= HELP_INDENT + HELP_KEYS + HELP_TEXT_MIN
    hang = (HELP_INDENT + HELP_KEYS if beside
            else max(0, min(2 * HELP_INDENT, width - 1)))
    indent = min(HELP_INDENT, hang)
    rows: list[Row] = [((0, section.title, "dialogtitle"),)]
    if section.note:
        rows += [((indent, line, "dialog"),)
                 for line in _wrap(section.note, width - indent)]
    for keys, text in section.entries:
        lines = _wrap(text, width - hang)
        keys_cell = (indent, keys, "dialoghot")
        if beside and len(keys) < HELP_KEYS:
            rows.append((keys_cell, (hang, lines[0], "dialog")))
            lines = lines[1:]
        else:
            rows.append((keys_cell,))
        rows += [((hang, line, "dialog"),) for line in lines]
    return rows


def _stacked(blocks: list[list[Row]]) -> list[Row]:
    """Blocks of rows one under another, a blank row between each two."""
    rows: list[Row] = []
    for block in blocks:
        if rows:
            rows.append(())
        rows += block
    return rows


def help_layout(sections: Sequence[HelpSection], width: int,
                intro: str = "") -> list[Column]:
    """The help laid out for a text area ``width`` cells wide.

    Two columns side by side where each can be :data:`HELP_COLUMN_MIN` wide,
    otherwise one.  Two columns split the sections into two runs of about
    equal height, at a section boundary, so no section is broken across
    them; the columns then scroll together, row by row.
    """
    width = max(1, width)
    two = (width - HELP_GUTTER) // 2 >= HELP_COLUMN_MIN and len(sections) > 1
    col_w = min((width - HELP_GUTTER) // 2 if two else width, HELP_COLUMN_MAX)
    blocks = [_help_section(section, col_w) for section in sections]
    if intro:
        blocks.insert(0, [((0, line, "dialog"),)
                          for line in _wrap(intro, col_w)])
    if not two:
        return [(0, col_w, _stacked(blocks))]
    # The intro stays above the first section, so a split never falls
    # between them.
    first = 2 if intro else 1
    split = min(range(first, len(blocks)),
                key=lambda at: max(len(_stacked(blocks[:at])),
                                   len(_stacked(blocks[at:]))))
    return [(0, col_w, _stacked(blocks[:split])),
            (col_w + HELP_GUTTER, col_w, _stacked(blocks[split:]))]


def help_screen(stdscr, title: str, sections: Sequence[HelpSection],
                intro: str = "") -> None:
    """Show the help: titled sections of keys, in two columns where they fit.

    It scrolls like a long :func:`message` -- Up/Down (``j``/``k``),
    PgUp/PgDn, Home/End -- and only Esc or Enter closes it, even when it all
    fits: every other key is one a reader may press looking for something,
    and the help should still be there afterwards.
    """
    width = 2 * HELP_COLUMN_MAX + HELP_GUTTER + 6
    _pager(stdscr, title, width,
           lambda text_width: help_layout(sections, text_width, intro),
           CLOSE_HINT, any_key=False)


def confirm(stdscr, title: str, text: str, default_yes: bool = False) -> bool:
    """Yes/No confirmation.  Returns True for yes."""
    lines = text.split("\n")
    width = max(len(title) + 8,
                max((len(line) for line in lines), default=0) + 6, 36)
    height = len(lines) + 5
    win = _center(stdscr, height, width)
    choice = default_yes
    while True:
        _box(win, title)
        win_h, win_w = win.getmaxyx()
        body = lines[: max(0, win_h - 5)]
        for i, line in enumerate(body):
            theme.paint(win, 1 + i, 3, line[: win_w - 6], "dialog")
        _buttons(win, min(win_h - 3, len(body) + 2),
                 [("~Y~es", choice), ("~N~o", not choice)], win_w)
        win.refresh()
        k = read_key(win)
        if k in (curses.KEY_LEFT, curses.KEY_RIGHT, 9):
            choice = not choice
        elif k in (ord("y"), ord("Y")):
            return True
        elif k in (ord("n"), ord("N"), 27):
            return False
        elif k in (10, 13, curses.KEY_ENTER):
            return choice


@dataclass
class Toggle:
    """A switch drawn under a prompt's field, flipped with Ctrl and its key.

    ``on`` is read back once the prompt returns: the prompt flips it in place,
    so the caller holds the answer without a second return value to unpack.
    """

    label: str
    key: str
    on: bool = True

    @property
    def code(self) -> int:
        """The key code Ctrl and ``key`` arrive as."""
        return ord(self.key.lower()) & 0x1F


def prompt(stdscr, title: str, label: str, default: str = "",
           is_password: bool = False,
           toggles: list[Toggle] | None = None,
           notes: list[str] | None = None) -> str | None:
    """Single-line text input.  Returns the string, or None if cancelled.

    ``toggles`` are switches listed under the field, each flipped by Ctrl and
    its key while the field is being typed into -- a letter on its own is
    text, so it cannot be the key.  ``notes`` are lines said under them: what
    the answer will mean, or a caveat about it -- the save of a table read
    only in part says so there.
    """
    toggles = toggles or []
    notes = notes or []
    width = max(len(title) + 8, len(label) + 6, 50,
                *(len(note) + 6 for note in notes))
    height = 5 + len(toggles) + len(notes)
    win = _center(stdscr, height, width)
    buf = list(default)
    pos = len(buf)
    curses.curs_set(1)
    try:
        while True:
            _box(win, title, "Enter=OK  Esc=Cancel")
            win_h, w = win.getmaxyx()
            theme.paint(win, 1, 3, label[: w - 6], "dialog")
            for row, toggle in enumerate(toggles[: max(0, win_h - 5)]):
                mark = "x" if toggle.on else " "
                text = f"[{mark}] {toggle.label}   Ctrl-{toggle.key.upper()}"
                theme.paint(win, 3 + row, 3, text[: w - 6], "dialog")
            room = max(0, win_h - 5 - len(toggles))
            for row, note in enumerate(notes[:room], start=3 + len(toggles)):
                theme.paint(win, row, 3, note[: w - 6], "dialog")
            field_w = w - 6
            shown = "".join("*" if is_password else c for c in buf)
            start = max(0, pos - field_w + 1)
            visible = shown[start : start + field_w]
            theme.paint(win, 2, 3, visible, "input", field_w)
            # The caret is a cell of the field in reverse, the way an input
            # line looked before terminals grew a cursor of their own.
            caret = min(pos - start, field_w - 1)
            theme.paint(win, 2, 3 + caret,
                        visible[caret] if caret < len(visible) else " ",
                        "inputcursor")
            win.move(2, 3 + caret)
            win.refresh()
            k = read_key(win)
            flipped = next((t for t in toggles if k == t.code), None)
            if flipped is not None:
                flipped.on = not flipped.on
            elif k in (10, 13, curses.KEY_ENTER):
                return "".join(buf)
            elif k == 27:
                return None
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
            elif k == curses.KEY_DC:
                if pos < len(buf):
                    del buf[pos]
            elif k == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif k == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif k == curses.KEY_HOME:
                pos = 0
            elif k == curses.KEY_END:
                pos = len(buf)
            else:
                # Every typed character, not just ASCII: this is the field a
                # file gets renamed in, and a name with an accent in it is an
                # ordinary name.
                char = typed_char(k)
                if char is not None:
                    buf.insert(pos, char)
                    pos += 1
    finally:
        curses.curs_set(0)


# Menu keys that are spoken for: j and k move the highlight, so neither can
# also mean "pick the entry called that".
NAVIGATION_KEYS = "jk"


def accelerators(names: list[str], reserved: str = "") -> list[str | None]:
    """One shortcut letter per name -- its own initial wherever that is free.

    The first letter is the one worth remembering ("w" for "work"), so every
    name gets a shot at its own before anything falls back to the alphabet in
    order; otherwise a name early in the list could take, as a fallback, the
    letter a later one is actually named for.

    ``reserved`` holds letters the caller has already promised elsewhere in the
    same menu.  Names left over at the end -- a list longer than the alphabet --
    get ``None``: unreachable by letter, still reachable with the arrow keys,
    which is the honest answer rather than a second letter meaning two things.
    """
    taken = set(NAVIGATION_KEYS) | set(reserved)
    keys: list[str | None] = [None] * len(names)

    for i, name in enumerate(names):
        initial = name[:1].lower()
        if initial.isascii() and initial.isalpha() and initial not in taken:
            taken.add(initial)
            keys[i] = initial

    spare = (c for c in "abcdefghijklmnopqrstuvwxyz" if c not in taken)
    for i, key in enumerate(keys):
        if key is None:
            keys[i] = next(spare, None)
    return keys


def menu(stdscr, title: str, options: list[str],
         keys: list[str | None] | None = None, text: str = "") -> int | None:
    """Vertical single-choice menu.  Returns the selected index or None.

    A list too long for the screen (a well stocked preset list on a short
    terminal) scrolls instead of overflowing the window, and grows a Turbo
    Vision scrollbar down its right-hand edge when it does.

    ``keys`` gives a shortcut letter per option (``None`` for none), drawn down
    the left of the list in the accelerator colour.  Pressing one *chooses*
    that option there and then rather than moving the highlight to it -- the
    whole point is to skip the walk down the list -- so a menu of saved
    locations answers to a keystroke.

    ``text`` is shown above the options, a blank row under it: what the
    choice is about, when the title alone cannot say it.  A short screen
    gives the options the room first and cuts the text.
    """
    header = text.split("\n") + [""] if text else []
    gutter = 3 if keys else 0     # "x  " down the left of every row
    width = max(len(title) + 8,
                max((len(o) for o in options), default=0) + 7 + gutter,
                max((len(line) for line in header), default=0) + 6, 32)
    win = _center(stdscr, len(options) + 2 + len(header), width)
    win_h, win_w = win.getmaxyx()
    first = 1 + min(len(header), max(0, win_h - 3))   # the first option's row
    body = max(1, win_h - 1 - first)     # rows available for options
    row_w = win_w - 4            # one column kept for the scrollbar
    idx = 0
    top = 0
    while True:
        # Keep the cursor inside the visible window.
        top = max(min(top, idx), idx - body + 1, 0)
        scrolling = len(options) > body
        _box(win, title, f"{idx + 1}/{len(options)}" if scrolling else "")
        for row, line in enumerate(header[: first - 1]):
            theme.paint(win, 1 + row, 3, line[: win_w - 6], "dialog")
        for row in range(min(body, len(options) - top)):
            i = top + row
            selected = i == idx
            role = "sel" if selected else "input"
            prefix = ""
            if keys:
                # Options without a letter still indent, so one row without a
                # shortcut does not step the whole column sideways.
                prefix = f"{keys[i]}  " if keys[i] else "   "
            label = f" {prefix}{options[i]} ".ljust(row_w)[:row_w]
            theme.paint(win, first + row, 1, label, role)
            if keys and keys[i]:
                theme.paint(win, first + row, 2, keys[i],
                            "selhot" if selected else "dialoghot")
        theme.scrollbar(win, first, win_w - 2, body, top, body, len(options))
        win.refresh()
        k = read_key(win)
        pressed = (typed_char(k) or "").lower()
        if keys and pressed:
            for i, key in enumerate(keys):
                if key == pressed:
                    return i
        if k in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(options)
        elif k in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(options)
        elif k == curses.KEY_PPAGE:
            idx = max(0, idx - body)
        elif k == curses.KEY_NPAGE:
            idx = min(len(options) - 1, idx + body)
        elif k == curses.KEY_HOME:
            idx = 0
        elif k == curses.KEY_END:
            idx = len(options) - 1
        elif k in (10, 13, curses.KEY_ENTER):
            return idx
        elif k == 27:
            return None


# -- drop-down menus -----------------------------------------------------------
#
# The menu bar's own widget.  Kept here with the other modal windows because it
# is one: it opens over the desktop, owns the keyboard while it is up, and
# hands back the name of whatever was chosen.

#: What a drop-down returns when the user arrows off its left or right edge --
#: the menu bar reads these and opens the neighbouring menu.
PREVIOUS_MENU = "<prev>"
NEXT_MENU = "<next>"


def _first_selectable(items: list[dict], start: int, step: int) -> int:
    """The next item that can hold the highlight, wrapping around.

    ``items`` is never empty -- :func:`dropdown` declines to open one that is
    -- and ``start`` comes back unchanged when nothing in the menu can hold
    the highlight at all.
    """
    n = len(items)
    i = start
    for _ in range(n):
        i = (i + step) % n
        if not items[i].get("sep") and not items[i].get("disabled"):
            return i
    return start


def dropdown(stdscr, items: list[dict], y: int, x: int) -> str | None:
    """Open a drop-down at (y, x) and return the chosen item's name.

    Each item is a dict of ``label`` (with a ``~H~ot`` marker), ``name`` (what
    is returned), and optionally ``key`` (shortcut text, right-aligned),
    ``sep``, ``disabled`` and ``checked``.  Returns None when the user gives
    up, or one of the sentinels above when they arrow off the sides.
    """
    entries = list(items)
    if not entries:
        return None
    width = max(
        len(theme.strip_hotkey(i.get("label", "")))
        + (len(i.get("key", "")) + 4 if i.get("key") else 0) + 4
        for i in entries)
    width = max(width, 14)
    height = len(entries) + 2

    max_y, max_x = stdscr.getmaxyx()
    # Keep the menu on the desktop, opening upwards if there is no room below.
    if y + height > max_y:
        y = max(0, max_y - height)
    x = min(x, max(0, max_x - width))
    _cast_shadow(stdscr, y, x, height, width)
    win = curses.newwin(height, min(width, max_x - x), y, x)
    win.keypad(True)
    theme.background(win, "menu")

    sel = _first_selectable(entries, -1, 1)
    while True:
        win.erase()
        h, w = win.getmaxyx()
        theme.frame(win, 0, 0, h, w, "menu", double=False)
        for i, item in enumerate(entries):
            row = 1 + i
            if item.get("sep"):
                # A rule that meets the frame on both sides, as a drop-down's
                # separators did rather than floating inside it.
                theme.paint(win, row, 0,
                            theme.glyph("lt1") + theme.glyph("h1") * (w - 2)
                            + theme.glyph("rt1"), "menusep")
                continue
            chosen = i == sel
            if item.get("disabled"):
                role = "menudim"
                hot = "menudim"
            else:
                role = "menusel" if chosen else "menu"
                hot = "menuselhot" if chosen else "menuhot"
            theme.fill(win, row, 1, w - 2, role)
            theme.paint_caption(win, row, 3, item.get("label", ""), role, hot)
            if item.get("checked"):
                theme.paint(win, row, 1, theme.glyph("check"), role)
            shortcut = item.get("key", "")
            if shortcut and len(shortcut) + 4 < w:
                theme.paint(win, row, w - 1 - len(shortcut) - 1, shortcut,
                            role if item.get("disabled") else
                            ("menusel" if chosen else "menushortcut"))
        win.refresh()

        k = read_key(win)
        if k in (27, 3):
            return None
        if k == curses.KEY_UP:
            sel = _first_selectable(entries, sel, -1)
        elif k == curses.KEY_DOWN:
            sel = _first_selectable(entries, sel, 1)
        elif k == curses.KEY_HOME:
            sel = _first_selectable(entries, -1, 1)
        elif k == curses.KEY_END:
            sel = _first_selectable(entries, len(entries), -1)
        elif k == curses.KEY_LEFT:
            return PREVIOUS_MENU
        elif k == curses.KEY_RIGHT:
            return NEXT_MENU
        elif k in (10, 13, curses.KEY_ENTER, ord(" ")):
            item = entries[sel]
            if not item.get("sep") and not item.get("disabled"):
                return item.get("name", "")
        elif typed_char(k) is not None:
            # An accelerator letter selects and activates in one keystroke.
            # Accelerators are ASCII, so a character from outside it simply
            # matches nothing rather than needing a guard of its own.
            letter = (typed_char(k) or "").lower()
            for item in entries:
                if theme.hotkey_letter(item.get("label", "")) == letter:
                    if item.get("disabled"):
                        break
                    return item.get("name", "")


def connect_dialog(stdscr) -> dict | None:
    """Collect the details needed to open a remote connection.

    Returns a dict describing the connection, or None if cancelled.
    """
    kind = menu(stdscr, "Open location",
                ["Local disk", "SFTP (SSH file transfer)",
                 "SSH (shell -- for servers with SFTP disabled)",
                 "FTP", "Cancel"])
    if kind is None or kind == 4:
        return None
    if kind == 0:
        return {"scheme": "local"}
    scheme = {1: "sftp", 2: "ssh", 3: "ftp"}[kind]

    title = f"{scheme.upper()} connection"
    if scheme in ("sftp", "ssh"):
        # Everything except the host is optional: ~/.ssh/config aliases,
        # user@host forms, agent + default keys and per-host IdentityFile
        # entries are all honoured, exactly like the ssh command.
        host = prompt(stdscr, title, "Host (name, alias, or user@host):")
        if not host:
            return None
        default_user = ""
        if "@" in host:
            default_user, _, host = host.rpartition("@")
        user = prompt(stdscr, title,
                      "Username (blank = ssh config / current user):",
                      default=default_user)
        if user is None:
            return None
        port_s = prompt(stdscr, title, "Port (blank/22 = ssh config):",
                        default="22")
        if port_s is None:
            return None
        try:
            port = int(port_s) if port_s.strip() else 22
        except ValueError:
            port = 22

        result = {"scheme": scheme, "host": host,
                  "username": user or None, "port": port}
        keyfile = prompt(stdscr, title,
                         "Key file (blank = ssh config / agent / ~/.ssh/id_*):",
                         default="")
        if keyfile is None:
            return None
        result["key_filename"] = keyfile or None
        pw = prompt(stdscr, title,
                    "Password (blank = keys/agent -- usually just press Enter):",
                    is_password=True)
        if pw is None:
            return None
        result["password"] = pw or None
    else:
        host = prompt(stdscr, title, "Host:")
        if not host:
            return None
        user = prompt(stdscr, title, "Username:", default="anonymous")
        if user is None:
            return None
        port_s = prompt(stdscr, title, "Port:", default="21")
        if port_s is None:
            return None
        try:
            port = int(port_s)
        except ValueError:
            port = 21
        result = {"scheme": scheme, "host": host, "username": user, "port": port}
        pw = prompt(stdscr, "FTP connection", "Password:", is_password=True)
        if pw is None:
            return None
        result["password"] = pw or ""

    return result


#: Width of the travelling block in an indeterminate progress bar.
MARCH_WIDTH = 8


def _marching_bar(width: int, tick: int) -> str:
    """An indeterminate bar: a block that bounces from end to end.

    Bouncing rather than wrapping, because a block that vanishes off one edge
    and reappears at the other reads as a redraw glitch, while one that turns
    around reads as something still working.
    """
    if width <= 0:
        return ""
    block, track = theme.glyph("block"), theme.glyph("shade")
    size = min(MARCH_WIDTH, width)
    span = width - size
    if span <= 0:
        return block * width
    # A full cycle is out and back; fold the second half onto the first.
    at = tick % (span * 2)
    if at > span:
        at = span * 2 - at
    return track * at + block * size + track * (span - at)


class ProgressDialog:
    """A cancellable progress window for long copy/move/sync operations.

    Drawn from the main thread; the running operation reports into it via
    :meth:`update` and polls :meth:`cancelled` between chunks.
    """

    def __init__(self, stdscr, title: str) -> None:
        self.stdscr = stdscr
        self.title = title
        self.win = _center(stdscr, 8, 60)
        self.win.nodelay(True)
        self._cancelled = False
        self.overall = ""
        self.detail = ""
        self.cur = 0
        self.total = 0
        self.ticks = 0          # advances the indeterminate bar

    def set_overall(self, text: str) -> None:
        self.overall = text

    def update(self, cur: int, total: int, label: str) -> None:
        self.cur = cur
        self.total = total
        self.detail = label
        self._poll_keys()
        self.draw()

    def _poll_keys(self) -> None:
        # No guard of its own: read_key already answers -1 for "nothing is
        # waiting", which on a non-blocking window is what curses raises.
        if read_key(self.win) in (27, ord("q"), ord("Q")):
            self._cancelled = True

    def cancelled(self) -> bool:
        self._poll_keys()
        return self._cancelled

    def draw(self) -> None:
        win = self.win
        _box(win, self.title, "Esc/Q = cancel")
        w = win.getmaxyx()[1]
        theme.paint(win, 2, 2, self.overall[: w - 4], "dialog", w - 4)
        theme.paint(win, 3, 2, self.detail[: w - 4], "dialogdim", w - 4)
        bar_w = w - 12
        if self.total > 0:
            frac = max(0.0, min(1.0, self.cur / self.total))
            bar = theme.progress_bar(bar_w, frac)
            pct = f"{int(frac * 100):3d}%"
        else:
            # No denominator -- a directory scan does not know how much is
            # left until it is done. A bar stuck at 0% reads as a hang, so a
            # block travels instead: motion is the message.
            bar, pct = _marching_bar(bar_w, self.ticks), "    "
        self.ticks += 1
        theme.paint(win, 5, 2, "[", "dialogdim")
        theme.paint(win, 5, 3, bar, "thumb")
        theme.paint(win, 5, 3 + bar_w, "]", "dialogdim")
        theme.paint(win, 5, w - 6, pct, "dialog")
        win.noutrefresh()
        curses.doupdate()

    def close(self) -> None:
        self.win.nodelay(False)


# -- permissions ---------------------------------------------------------------

@dataclass
class AttributeChoice:
    """What the attributes dialog was told: bits to set and clear, and the
    owner, group and recursion it ended with."""

    on: int
    off: int
    owner: str
    group: str
    recursive: bool


#: The rows and columns of the box grid, highest bit first.
_WHO = ("Owner", "Group", "Other")
_WHAT = ("read", "write", "exec")


def _bit(row: int, col: int) -> int:
    return 1 << (8 - (row * 3 + col))


def attributes(stdscr, title: str, subject: str, on: int, off: int,
               owner: str = "", group: str = "", can_chown: bool = False,
               has_dirs: bool = False) -> AttributeChoice | None:
    """Show and set permission bits as ``rwx`` boxes and in octal.

    ``on`` and ``off`` are the bits every entry agrees on; a bit in neither is
    mixed, drawn ``[~]``, and stays each entry's own unless it is toggled.
    The arrows move between the boxes and Space flips one; typing octal
    digits sets all nine at once, the last three typed counting -- the boxes
    and the number are two views of one value, so each follows the other.
    ``o`` and ``g`` ask for a new owner or group where ``can_chown`` allows,
    ``r`` switches recursion on for directories, Enter applies, Esc cancels.
    """
    from .attributes import octal_text

    width, height = 54, 14
    win = _center(stdscr, height, width)
    row, col = 0, 0
    typed = ""
    recursive = False
    while True:
        _box(win, title, "Space toggle  0-7 octal  Enter=OK  Esc=Cancel")
        win_h, w = win.getmaxyx()
        theme.paint(win, 1, 3, subject[: w - 6], "dialoghot")
        theme.paint(win, 3, 14, "  ".join(f"{what:<5}" for what in _WHAT),
                    "dialogdim")
        for r, who in enumerate(_WHO):
            theme.paint(win, 4 + r, 3, who, "dialog")
            for c in range(3):
                bit = _bit(r, c)
                mark = "x" if on & bit else " " if off & bit else "~"
                theme.paint(win, 4 + r, 14 + c * 7, f"[{mark}]",
                            "sel" if (r, c) == (row, col) else "input")
        theme.paint(win, 8, 3, f"Octal      {octal_text(on, off)}", "dialog")
        who = f"Owner: {owner or '(various)'}   Group: {group or '(various)'}"
        if can_chown:
            who += "   o / g"
        theme.paint(win, 9, 3, who[: w - 6], "dialog")
        if has_dirs:
            theme.paint(win, 10, 3,
                        f"[{'x' if recursive else ' '}] Recursive   r",
                        "dialog")
        win.refresh()
        k = read_key(win)
        char = typed_char(k) or ""
        if k == 27:
            return None
        elif k in (10, 13, curses.KEY_ENTER):
            return AttributeChoice(on, off, owner, group, recursive)
        elif k == curses.KEY_UP:
            row = (row - 1) % 3
        elif k == curses.KEY_DOWN:
            row = (row + 1) % 3
        elif k == curses.KEY_LEFT:
            col = (col - 1) % 3
        elif k in (curses.KEY_RIGHT, 9):
            col = (col + 1) % 3
        elif char in (" ", "x"):
            bit = _bit(row, col)
            # A mixed box turns on first, as the commanders do it.
            if on & bit:
                on, off = on & ~bit, off | bit
            else:
                on, off = on | bit, off & ~bit
            typed = ""
        elif char and char in "01234567":
            typed = (typed + char)[-3:]
            on = int(typed, 8)
            off = 0o777 & ~on
        elif char == "o" and can_chown:
            answer = prompt(stdscr, title, "New owner:", default=owner)
            owner = answer.strip() if answer else owner
        elif char == "g" and can_chown:
            answer = prompt(stdscr, title, "New group:", default=group)
            group = answer.strip() if answer else group
        elif char == "r" and has_dirs:
            recursive = not recursive
