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

from . import theme


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
    max_y, max_x = stdscr.getmaxyx()
    width = min(width, max_x - 2)
    height = min(height, max_y - 2)
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


def message(stdscr, title: str, text: str, error: bool = False) -> None:
    """Show a modal message; dismissed with any key."""
    lines = text.split("\n")
    width = max(len(title) + 8, max((len(l) for l in lines), default=0) + 6, 34)
    height = len(lines) + 5
    win = _center(stdscr, height, width)
    _box(win, title, "press any key")
    role = "dialogerror" if error else "dialog"
    win_h, win_w = win.getmaxyx()
    body = lines[: max(0, win_h - 5)]
    for i, line in enumerate(body):
        theme.paint(win, 1 + i, 3, line[: win_w - 6], role)
    _buttons(win, min(win_h - 3, len(body) + 2), [("~O~K", True)], win_w)
    win.refresh()
    win.getch()


def confirm(stdscr, title: str, text: str, default_yes: bool = False) -> bool:
    """Yes/No confirmation.  Returns True for yes."""
    lines = text.split("\n")
    width = max(len(title) + 8, max((len(l) for l in lines), default=0) + 6, 36)
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
        k = win.getch()
        if k in (curses.KEY_LEFT, curses.KEY_RIGHT, 9):
            choice = not choice
        elif k in (ord("y"), ord("Y")):
            return True
        elif k in (ord("n"), ord("N"), 27):
            return False
        elif k in (10, 13, curses.KEY_ENTER):
            return choice


def prompt(stdscr, title: str, label: str, default: str = "",
           is_password: bool = False) -> str | None:
    """Single-line text input.  Returns the string, or None if cancelled."""
    width = max(len(title) + 8, len(label) + 6, 50)
    height = 5
    win = _center(stdscr, height, width)
    buf = list(default)
    pos = len(buf)
    curses.curs_set(1)
    try:
        while True:
            _box(win, title, "Enter=OK  Esc=Cancel")
            win_h, w = win.getmaxyx()
            theme.paint(win, 1, 3, label[: w - 6], "dialog")
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
            k = win.getch()
            if k in (10, 13, curses.KEY_ENTER):
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
            elif 32 <= k < 127:
                buf.insert(pos, chr(k))
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
         keys: list[str | None] | None = None) -> int | None:
    """Vertical single-choice menu.  Returns the selected index or None.

    A list too long for the screen (a well stocked preset list on a short
    terminal) scrolls instead of overflowing the window, and grows a Turbo
    Vision scrollbar down its right-hand edge when it does.

    ``keys`` gives a shortcut letter per option (``None`` for none), drawn down
    the left of the list in the accelerator colour.  Pressing one *chooses*
    that option there and then rather than moving the highlight to it -- the
    whole point is to skip the walk down the list -- so a menu of saved
    locations answers to a keystroke.
    """
    gutter = 3 if keys else 0     # "x  " down the left of every row
    width = max(len(title) + 8,
                max((len(o) for o in options), default=0) + 7 + gutter, 32)
    win = _center(stdscr, len(options) + 2, width)
    win_h, win_w = win.getmaxyx()
    body = max(1, win_h - 2)     # rows available for options
    row_w = win_w - 4            # one column kept for the scrollbar
    idx = 0
    top = 0
    while True:
        # Keep the cursor inside the visible window.
        top = max(min(top, idx), idx - body + 1, 0)
        scrolling = len(options) > body
        _box(win, title, f"{idx + 1}/{len(options)}" if scrolling else "")
        for row in range(min(body, len(options) - top)):
            i = top + row
            selected = i == idx
            role = "sel" if selected else "input"
            prefix = ""
            if keys:
                # Options without a letter still indent, so one row without a
                # shortcut does not step the whole column sideways.
                prefix = f"{keys[i]}  " if keys[i] else "   "
            text = f" {prefix}{options[i]} ".ljust(row_w)[:row_w]
            theme.paint(win, 1 + row, 1, text, role)
            if keys and keys[i]:
                theme.paint(win, 1 + row, 2, keys[i],
                            "selhot" if selected else "dialoghot")
        theme.scrollbar(win, 1, win_w - 2, body, top, body, len(options))
        win.refresh()
        k = win.getch()
        if keys and 32 <= k < 127:
            pressed = chr(k).lower()
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
    """The next item that can hold the highlight, wrapping around."""
    n = len(items)
    if n == 0:
        return 0
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
    entries = [i for i in items]
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

        k = win.getch()
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
        elif 32 <= k < 127:
            # An accelerator letter selects and activates in one keystroke.
            letter = chr(k).lower()
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
        try:
            k = self.win.getch()
        except curses.error:
            k = -1
        if k in (27, ord("q"), ord("Q")):
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
