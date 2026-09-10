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

from . import theme
from .filesystems import FileSystem
from .util import read_key, typed_char

MAX_EDIT_BYTES = 8 * 1024 * 1024

#: Columns a tab advances to when the buffer is drawn.  One constant rather
#: than the number written twice: the cursor is placed by expanding the text
#: in front of it exactly as the row itself is expanded, and the two drifting
#: apart is what put the cursor in the wrong column to begin with.
TAB_WIDTH = 4


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
        #: What ended the lines in the file as it was read, so saving puts
        #: them back the same way.  Everything in between works in "\n"; a
        #: file written on Windows would otherwise come back with every line
        #: ending rewritten because one character was typed into it.  (Not
        #: "newline": that is the Enter key's method.)
        self.line_ending = "\n"
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
        # CRLF is the one worth keeping: it is what a file from Windows has,
        # and it survives the round trip.  A lone CR (a file from before OS X)
        # is read the same way and saved as LF, which is the conversion the
        # "Normalise text" plug-in exists to make deliberately.
        if "\r\n" in text:
            self.line_ending = "\r\n"
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # split() always yields at least one element, so the buffer is never
        # empty and always has a line for the cursor to sit on.
        self.lines = text.split("\n")

    # -- persistence ------------------------------------------------------
    def save(self) -> bool:
        if self.readonly:
            self.message = "Read-only: cannot save"
            return False
        data = self.line_ending.join(self.lines).encode("utf-8")
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

    def display_col(self, index: int, cx: int) -> int:
        """Which screen column ``cx`` of line ``index`` is drawn at.

        A tab is one character in the buffer and up to four columns on the
        screen, so a line with tabs in it -- a Makefile, most C -- had its
        cursor drawn one column short for each tab in front of it.
        """
        line = self.lines[index] if 0 <= index < len(self.lines) else ""
        return len(line[:cx].expandtabs(TAB_WIDTH))

    def home(self) -> None:
        self.cx = 0

    def end(self) -> None:
        self.cx = len(self.lines[self.cy])

    # -- rendering --------------------------------------------------------
    def draw(self, win) -> None:
        theme.background(win, "edit")
        win.erase()
        height, width = win.getmaxyx()
        body_h = height - 2

        flag = "*" if self.dirty else " "
        ro = " [RO]" if self.readonly else ""
        title = f" Edit{ro}: {self.name} {flag}"
        theme.paint(win, 0, 0, title, "keybar", width)

        # Keep the cursor on screen.
        if self.cy < self.top:
            self.top = self.cy
        elif self.cy >= self.top + body_h:
            self.top = self.cy - body_h + 1
        gutter = len(str(len(self.lines))) + 1 if self.show_line_numbers else 0
        text_w = width - gutter
        # Scrolling is in screen columns, because that is what the rows below
        # are sliced in: the expanded line, not the buffer's own characters.
        col = self.display_col(self.cy, self.cx)
        if col < self.left:
            self.left = col
        elif col >= self.left + text_w:
            self.left = col - text_w + 1

        for row in range(body_h):
            idx = self.top + row
            if idx >= len(self.lines):
                break
            y = row + 1
            if self.show_line_numbers:
                num = str(idx + 1).rjust(gutter - 1)
                theme.paint(win, y, 0, num + " ", "editnum")
            line = self.lines[idx].expandtabs(TAB_WIDTH)
            visible = line[self.left : self.left + text_w]
            theme.paint(win, y, gutter, visible, "edit")

        hint = " F2/^S save  F10/^Q quit  ^Y/^K del-line  ^L numbers "
        status = self.message or hint
        theme.paint(win, height - 1, 0, status, "keybar", width)

        # Position the hardware cursor.
        scr_y = self.cy - self.top + 1
        scr_x = gutter + (col - self.left)
        if 1 <= scr_y < height - 1 and 0 <= scr_x < width:
            win.move(scr_y, scr_x)
        win.noutrefresh()

    # -- key handling -------------------------------------------------------
    def handle_key(self, key: int | str, page: int = 20) -> str | None:
        """Apply one key press to the buffer.

        Returns ``"quit"`` when the user asked to leave (the caller decides
        about unsaved changes), otherwise ``None``.  Every command has an
        alias that survives terminals which intercept control keys -- the
        VS Code integrated terminal in particular treats Ctrl-K as a chord
        prefix and reserves Ctrl-Q, so save/quit/delete-line are also on
        F2 / F10 / Ctrl-Y (and Ctrl-O saves, nano-style).
        """
        if key in (17, curses.KEY_F10):          # Ctrl-Q / F10: quit
            return "quit"
        elif key in (19, 15, curses.KEY_F2):     # Ctrl-S / Ctrl-O / F2: save
            self.save()
        elif key in (11, 25):                    # Ctrl-K / Ctrl-Y: delete line
            self._delete_line()
        elif key == 12:                          # Ctrl-L: toggle line numbers
            self.show_line_numbers = not self.show_line_numbers
        elif key == curses.KEY_UP:
            self.move(-1, 0)
        elif key == curses.KEY_DOWN:
            self.move(1, 0)
        elif key == curses.KEY_LEFT:
            self.move(0, -1)
        elif key == curses.KEY_RIGHT:
            self.move(0, 1)
        elif key == curses.KEY_HOME:
            self.home()
        elif key == curses.KEY_END:
            self.end()
        elif key == curses.KEY_NPAGE:
            self.move(page, 0)
        elif key == curses.KEY_PPAGE:
            self.move(-page, 0)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.backspace()
        elif key == curses.KEY_DC:
            self.delete()
        elif key in (10, 13, curses.KEY_ENTER):
            self.newline()
        elif key == 9:  # Tab
            self.insert_char("    ")
        else:
            # Text, and only text: a key code (a resize, a mouse report, an
            # unbound function key) is not a character to type into the file.
            char = typed_char(key)
            if char is not None:
                self.insert_char(char)
        return None

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
                key = read_key(win)
                self.message = ""
                if self.handle_key(key, page=height - 3) == "quit":
                    if self.dirty and not self._confirm_discard(win):
                        continue
                    break
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
        # Filling the last row writes into the bottom-right cell, which curses
        # always refuses; the prompt is still readable without it.
        theme.paint(win, height - 1, 0, prompt, "dialogerror", width)
        win.refresh()
        while True:
            k = read_key(win)
            if k in (ord("y"), ord("Y")):
                return True
            if k in (ord("n"), ord("N"), 27):
                return False
