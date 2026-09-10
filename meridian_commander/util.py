"""Small formatting and text helpers used throughout the UI."""

from __future__ import annotations

import curses
import time


def typed_char(key: int) -> str | None:
    """The character ``key`` stands for, or ``None`` when it is not text.

    ``getch`` answers with two different kinds of number.  Below 256 it is a
    character (or a control code); from ``KEY_MIN`` up it is a *key code* --
    a terminal resize, a mouse report, a function key nothing is bound to --
    and the two are told apart only by that boundary.

    Every place that took "anything above space" as text has had to be fixed
    for the same reason: ``KEY_RESIZE`` arrives as 410, and turning it into
    ``chr(410)`` typed a stray letter into whatever was accepting input --
    the file being edited, a filter box, the shell running in a pane.  Asking
    here instead of writing the comparison again is what stops the next one.
    """
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
