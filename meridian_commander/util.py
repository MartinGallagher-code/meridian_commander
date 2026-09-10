"""Small formatting and text helpers used throughout the UI."""

from __future__ import annotations

import curses
import time


def read_key(win) -> int | str:
    """The next key from ``win``, decoded from the terminal's encoding.

    ``getch`` answers in *bytes*.  In a UTF-8 terminal one press of "e-acute"
    is two of them, and two bytes became two Latin-1 characters: the editor
    typed "AA" shapes nobody asked for and *saved* them, so a file with an
    accent in it came back double-encoded.  ``get_wch`` decodes the sequence
    instead, and everything past this point sees one character.

    The number protocol is kept.  A decoded character below U+0100 comes back
    as its ordinal -- exactly what ``getch`` returned for it before -- so
    every ``key == 27`` and ``ord("q")`` still means what it did, and U+00E9
    now arrives whole through that same path.  Only a character the protocol
    cannot carry, from U+0100 up, comes back as the ``str`` itself: those
    collide with ``curses.KEY_MIN``, which is why the boundary in
    :func:`typed_char` is where it is.

    A window with no ``get_wch`` falls back to ``getch``.  A wait that times
    out is -1 either way: ``get_wch`` reports it by raising rather than by
    returning.
    """
    reader = getattr(win, "get_wch", None)
    if reader is None:
        return win.getch()
    try:
        key = reader()
    except curses.error:
        return -1
    if isinstance(key, str):
        return ord(key) if ord(key) < 256 else key
    return key


def typed_char(key: int | str) -> str | None:
    """The character ``key`` stands for, or ``None`` when it is not text.

    :func:`read_key` answers with two different kinds of value.  A number
    below 256 is a character (or a control code); from ``KEY_MIN`` up it is a
    *key code* -- a terminal resize, a mouse report, a function key nothing is
    bound to -- and the two are told apart only by that boundary.  A ``str``
    is a character from U+0100 up, which is on the far side of that boundary
    and so cannot be a number at all.

    Every place that took "anything above space" as text has had to be fixed
    for the same reason: ``KEY_RESIZE`` arrives as 410, and turning it into
    ``chr(410)`` typed a stray letter into whatever was accepting input --
    the file being edited, a filter box, the shell running in a pane.  Asking
    here instead of writing the comparison again is what stops the next one.
    """
    if isinstance(key, str):
        return key
    if 32 <= key < 127 or 127 < key < curses.KEY_MIN:
        return chr(key)
    return None


def human_size(size: int | None) -> str:
    """Format a byte count into a short, right-alignable string."""
    if size is None:
        return "     ?"
    if size < 1024:
        return f"{size:6d}"
    value = float(size)
    for unit in ("K", "M", "G", "T", "P", "E"):
        value /= 1024.0
        if value < 1024.0:
            if value < 10:
                return f"{value:5.1f}{unit}"
            return f"{value:5.0f}{unit}"
    return f"{value:5.0f}E"


def human_time(mtime: float | None) -> str:
    """Format a modification time compactly (date, or time if this year)."""
    if not mtime:
        return "     ?      "
    try:
        lt = time.localtime(mtime)
    except (OSError, ValueError, OverflowError):
        return "     ?      "
    now = time.localtime()
    if lt.tm_year == now.tm_year:
        return time.strftime("%b %d %H:%M", lt)
    return time.strftime("%b %d  %Y", lt)


def truncate(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` columns, marking cuts with an ellipsis."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "~"
    return text[: width - 1] + "~"


def ljust(text: str, width: int) -> str:
    return truncate(text, width).ljust(width)


def rjust(text: str, width: int) -> str:
    text = truncate(text, width)
    return text.rjust(width)
