"""Reusable curses dialogs: messages, prompts, menus and progress bars.

These are deliberately self-contained helpers that draw a centered window over
the main screen, run their own tiny input loop, and return a value.  Keeping
them here keeps :mod:`meridian_commander.app` focused on file-manager logic.
"""

from __future__ import annotations

import curses


def _center(stdscr, height: int, width: int):
    max_y, max_x = stdscr.getmaxyx()
    width = min(width, max_x - 2)
    height = min(height, max_y - 2)
    y = max(0, (max_y - height) // 2)
    x = max(0, (max_x - width) // 2)
    win = curses.newwin(height, width, y, x)
    win.keypad(True)
    win.attrset(curses.A_NORMAL)
    return win


def _box(win, title: str = "") -> None:
    win.erase()
    win.box()
    if title:
        h, w = win.getmaxyx()
        t = f" {title} "
        win.addstr(0, max(1, (w - len(t)) // 2), t[: w - 2], curses.A_BOLD)


def message(stdscr, title: str, text: str, error: bool = False) -> None:
    """Show a modal message; dismissed with any key."""
    lines = text.split("\n")
    width = max(len(title) + 4, max((len(l) for l in lines), default=0) + 4, 30)
    height = len(lines) + 4
    win = _center(stdscr, height, width)
    _box(win, title)
    attr = curses.A_BOLD if error else curses.A_NORMAL
    win_h, win_w = win.getmaxyx()
    for i, line in enumerate(lines[: win_h - 3]):
        try:
            win.addstr(2 + i, 2, line[: win_w - 4], attr)
        except curses.error:
            pass
    win.addstr(win_h - 1, 2, " press any key ", curses.A_DIM)
    win.refresh()
    win.getch()


def confirm(stdscr, title: str, text: str, default_yes: bool = False) -> bool:
    """Yes/No confirmation.  Returns True for yes."""
    lines = text.split("\n")
    width = max(len(title) + 4, max((len(l) for l in lines), default=0) + 4, 34)
    height = len(lines) + 5
    win = _center(stdscr, height, width)
    choice = default_yes
    while True:
        _box(win, title)
        win_h, win_w = win.getmaxyx()
        for i, line in enumerate(lines[: win_h - 4]):
            try:
                win.addstr(2 + i, 2, line[: win_w - 4])
            except curses.error:
                pass
        yes = "[ Yes ]"
        no = "[ No ]"
        y = win_h - 2
        win.addstr(y, 4, yes, curses.A_REVERSE if choice else curses.A_NORMAL)
        win.addstr(y, 4 + len(yes) + 2, no,
                   curses.A_REVERSE if not choice else curses.A_NORMAL)
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
    width = max(len(title) + 4, len(label) + 6, 50)
    height = 6
    win = _center(stdscr, height, width)
    buf = list(default)
    pos = len(buf)
    curses.curs_set(1)
    try:
        while True:
            _box(win, title)
            w = win.getmaxyx()[1]
            win.addstr(1, 2, label[: w - 4])
            field_w = w - 4
            shown = "".join("*" if is_password else c for c in buf)
            start = max(0, pos - field_w + 1)
            win.attrset(curses.A_UNDERLINE)
            win.addstr(3, 2, shown[start : start + field_w].ljust(field_w))
            win.attrset(curses.A_NORMAL)
            win.addstr(height - 1, 2, " Enter=OK  Esc=Cancel ", curses.A_DIM)
            win.move(3, 2 + min(pos - start, field_w - 1))
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
    terminal) scrolls instead of overflowing the window.

    ``keys`` gives a shortcut letter per option (``None`` for none), drawn down
    the left of the list.  Pressing one *chooses* that option there and then
    rather than moving the highlight to it -- the whole point is to skip the
    walk down the list -- so a menu of saved locations answers to a keystroke.
    """
    gutter = 3 if keys else 0     # "x  " down the left of every row
    width = max(len(title) + 4,
                max((len(o) for o in options), default=0) + 6 + gutter, 30)
    win = _center(stdscr, len(options) + 4, width)
    win_h, win_w = win.getmaxyx()
    body = max(1, win_h - 4)     # rows available for options
    idx = 0
    top = 0
    while True:
        # Keep the cursor inside the visible window.
        top = max(min(top, idx), idx - body + 1, 0)
        _box(win, title)
        for row in range(min(body, len(options) - top)):
            i = top + row
            attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
            prefix = ""
            if keys:
                # Options without a letter still indent, so one row without a
                # shortcut does not step the whole column sideways.
                prefix = f"{keys[i]}  " if keys[i] else "   "
            text = f" {prefix}{options[i]} ".ljust(win_w - 4)[: win_w - 4]
            try:
                win.addstr(2 + row, 2, text, attr)
            except curses.error:
                pass
        if len(options) > body:
            try:
                win.addstr(win_h - 1, 2, f" {idx + 1}/{len(options)} ",
                           curses.A_DIM)
            except curses.error:
                pass
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
    block = min(MARCH_WIDTH, width)
    span = width - block
    if span <= 0:
        return "#" * width
    # A full cycle is out and back; fold the second half onto the first.
    at = tick % (span * 2)
    if at > span:
        at = span * 2 - at
    return "-" * at + "#" * block + "-" * (span - at)


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
        _box(win, self.title)
        w = win.getmaxyx()[1]
        win.addstr(2, 2, self.overall[: w - 4].ljust(w - 4))
        win.addstr(3, 2, self.detail[: w - 4].ljust(w - 4))
        bar_w = w - 6
        if self.total > 0:
            frac = max(0.0, min(1.0, self.cur / self.total))
            filled = int(bar_w * frac)
            bar = "#" * filled + "-" * (bar_w - filled)
            pct = f"{int(frac * 100):3d}%"
        else:
            # No denominator -- a directory scan does not know how much is
            # left until it is done. A bar stuck at 0% reads as a hang, so a
            # block travels instead: motion is the message.
            bar, pct = _marching_bar(bar_w, self.ticks), "    "
        self.ticks += 1
        win.addstr(5, 2, f"[{bar}]"[: w - 2])
        win.addstr(5, w - 6, pct)
        win.addstr(6, 2, " Esc/Q = cancel ", curses.A_DIM)
        win.noutrefresh()
        curses.doupdate()

    def close(self) -> None:
        self.win.nodelay(False)
