"""A small but real modal-free text editor.

It supports the operations one actually needs while file managing: moving the
cursor, inserting and deleting text, splitting/joining lines with Enter and
Backspace, and saving.  Like the viewer it reads and writes through the
filesystem abstraction, so remote files can be edited in place.

Editing is line-buffer based (a list of strings).  That keeps the logic simple
and is perfectly adequate for the config-file-sized edits a file manager is used
for; it is not trying to be a replacement for a full programmer's editor.
"""

from __future__ import annotations

import curses

from .filesystems import FileSystem

MAX_EDIT_BYTES = 8 * 1024 * 1024


class Editor:
    def __init__(self, fs: FileSystem, path: str) -> None:
        self.fs = fs
        self.path = path
        self.name = fs.basename(path)
        self.lines: list[str] = [""]
        self.cy = 0          # cursor line
        self.cx = 0          # cursor column
        self.top = 0         # first visible line
        self.left = 0        # horizontal scroll
        self.dirty = False
        self.message = ""
        self.error: str | None = None
        self.readonly = False
        self.show_line_numbers = True
        self._load()

    def _load(self) -> None:
        if not self.fs.exists(self.path):
            self.lines = [""]
            self.message = "New file"
            return
        try:
            reader = self.fs.open_read(self.path)
            try:
                data = reader.read(MAX_EDIT_BYTES + 1)
            finally:
                reader.close()
        except Exception as exc:
            self.error = str(exc)
            self.readonly = True
            self.lines = [f"Cannot open: {exc}"]
            return
        if len(data) > MAX_EDIT_BYTES:
            self.readonly = True
            self.message = "File too large -- read only"
            data = data[:MAX_EDIT_BYTES]
        text = data.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = text.split("\n") or [""]
        if not self.lines:
            self.lines = [""]

    # -- persistence ------------------------------------------------------
    def save(self) -> bool:
        if self.readonly:
            self.message = "Read-only: cannot save"
            return False
        data = "\n".join(self.lines).encode("utf-8")
        try:
            writer = self.fs.open_write(self.path)
            try:
                writer.write(data)
            finally:
                writer.close()
        except Exception as exc:
            self.message = f"Save failed: {exc}"
            return False
        self.dirty = False
        self.message = f"Saved {len(data)} bytes"
        return True

    # -- editing primitives ----------------------------------------------
    def _cur_line(self) -> str:
        return self.lines[self.cy]

    def insert_char(self, ch: str) -> None:
        if self.readonly:
            return
        line = self.lines[self.cy]
        self.lines[self.cy] = line[: self.cx] + ch + line[self.cx :]
        self.cx += len(ch)
        self.dirty = True

    def newline(self) -> None:
        if self.readonly:
            return
        line = self.lines[self.cy]
        before, after = line[: self.cx], line[self.cx :]
        # Preserve leading indentation for the new line.
        indent = line[: len(line) - len(line.lstrip(" "))]
        self.lines[self.cy] = before
        self.lines.insert(self.cy + 1, indent + after)
        self.cy += 1
        self.cx = len(indent)
        self.dirty = True

    def backspace(self) -> None:
        if self.readonly:
            return
        if self.cx > 0:
            line = self.lines[self.cy]
            self.lines[self.cy] = line[: self.cx - 1] + line[self.cx :]
            self.cx -= 1
            self.dirty = True
        elif self.cy > 0:
            prev = self.lines[self.cy - 1]
            self.cx = len(prev)
            self.lines[self.cy - 1] = prev + self.lines[self.cy]
            del self.lines[self.cy]
            self.cy -= 1
            self.dirty = True

    def delete(self) -> None:
        if self.readonly:
            return
        line = self.lines[self.cy]
        if self.cx < len(line):
            self.lines[self.cy] = line[: self.cx] + line[self.cx + 1 :]
            self.dirty = True
        elif self.cy < len(self.lines) - 1:
            self.lines[self.cy] = line + self.lines[self.cy + 1]
            del self.lines[self.cy + 1]
            self.dirty = True

    # -- cursor movement --------------------------------------------------
    def _clamp_cx(self) -> None:
        self.cx = max(0, min(self.cx, len(self.lines[self.cy])))

    def move(self, dy: int, dx: int) -> None:
        if dy:
            self.cy = max(0, min(self.cy + dy, len(self.lines) - 1))
            self._clamp_cx()
        if dx:
            self.cx += dx
            if self.cx < 0:
                if self.cy > 0:
                    self.cy -= 1
                    self.cx = len(self.lines[self.cy])
                else:
                    self.cx = 0
            elif self.cx > len(self.lines[self.cy]):
                if self.cy < len(self.lines) - 1:
                    self.cy += 1
                    self.cx = 0
                else:
                    self.cx = len(self.lines[self.cy])

    def home(self) -> None:
        self.cx = 0

    def end(self) -> None:
        self.cx = len(self.lines[self.cy])

    # -- rendering --------------------------------------------------------
    def draw(self, win) -> None:
        win.erase()
        height, width = win.getmaxyx()
        body_h = height - 2

        flag = "*" if self.dirty else " "
        ro = " [RO]" if self.readonly else ""
        title = f" Edit{ro}: {self.name} {flag}"
        win.attrset(curses.A_REVERSE)
        win.addstr(0, 0, title.ljust(width)[:width])
        win.attrset(curses.A_NORMAL)

        # Keep the cursor on screen.
        if self.cy < self.top:
            self.top = self.cy
        elif self.cy >= self.top + body_h:
            self.top = self.cy - body_h + 1
        gutter = len(str(len(self.lines))) + 1 if self.show_line_numbers else 0
        text_w = width - gutter
        if self.cx < self.left:
            self.left = self.cx
        elif self.cx >= self.left + text_w:
            self.left = self.cx - text_w + 1

        for row in range(body_h):
            idx = self.top + row
            if idx >= len(self.lines):
                break
            y = row + 1
            if self.show_line_numbers:
                num = str(idx + 1).rjust(gutter - 1)
                win.attrset(curses.A_DIM)
                win.addstr(y, 0, num + " ")
                win.attrset(curses.A_NORMAL)
            line = self.lines[idx].expandtabs(4)
            visible = line[self.left : self.left + text_w]
            try:
                win.addstr(y, gutter, visible)
            except curses.error:
                pass

        hint = " ^S save  ^Q quit  ^K del-line  ^L numbers  arrows/PgUp/PgDn "
        status = self.message or hint
        win.attrset(curses.A_REVERSE)
        try:
            win.addstr(height - 1, 0, status.ljust(width)[:width])
        except curses.error:
            pass
        win.attrset(curses.A_NORMAL)

        # Position the hardware cursor.
        scr_y = self.cy - self.top + 1
        scr_x = gutter + (self.cx - self.left)
        if 1 <= scr_y < height - 1 and 0 <= scr_x < width:
            win.move(scr_y, scr_x)
        win.noutrefresh()

    # -- main loop --------------------------------------------------------
    def run(self, stdscr) -> None:
        curses.curs_set(1)
        height, width = stdscr.getmaxyx()
        win = curses.newwin(height, width, 0, 0)
        win.keypad(True)
        try:
            while True:
                self.draw(win)
                curses.doupdate()
                key = win.getch()
                self.message = ""
                if key in (17,):  # Ctrl-Q
                    if self.dirty and not self._confirm_discard(win):
                        continue
                    break
                elif key in (19,):  # Ctrl-S
                    self.save()
                elif key in (11,):  # Ctrl-K delete line
                    self._delete_line()
                elif key in (12,):  # Ctrl-L toggle line numbers
                    self.show_line_numbers = not self.show_line_numbers
                elif key in (curses.KEY_UP,):
                    self.move(-1, 0)
                elif key in (curses.KEY_DOWN,):
                    self.move(1, 0)
                elif key in (curses.KEY_LEFT,):
                    self.move(0, -1)
                elif key in (curses.KEY_RIGHT,):
                    self.move(0, 1)
                elif key == curses.KEY_HOME:
                    self.home()
                elif key == curses.KEY_END:
                    self.end()
                elif key == curses.KEY_NPAGE:
                    self.move(height - 3, 0)
                elif key == curses.KEY_PPAGE:
                    self.move(-(height - 3), 0)
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    self.backspace()
                elif key in (curses.KEY_DC,):
                    self.delete()
                elif key in (10, 13, curses.KEY_ENTER):
                    self.newline()
                elif key == 9:  # Tab
                    self.insert_char("    ")
                elif 32 <= key < 127:
                    self.insert_char(chr(key))
                elif key > 127:
                    try:
                        self.insert_char(chr(key))
                    except ValueError:
                        pass
        finally:
            curses.curs_set(0)

    def _delete_line(self) -> None:
        if self.readonly:
            return
        if len(self.lines) == 1:
            self.lines[0] = ""
        else:
            del self.lines[self.cy]
            self.cy = min(self.cy, len(self.lines) - 1)
        self.cx = 0
        self.dirty = True

    def _confirm_discard(self, win) -> bool:
        height, width = win.getmaxyx()
        prompt = " Unsaved changes. Discard? (y/n) "
        win.attrset(curses.A_REVERSE)
        win.addstr(height - 1, 0, prompt.ljust(width)[:width])
        win.attrset(curses.A_NORMAL)
        win.refresh()
        while True:
            k = win.getch()
            if k in (ord("y"), ord("Y")):
                return True
            if k in (ord("n"), ord("N"), 27):
                return False
