"""Built-in plugin: a pseudo-terminal running *inside* the pane.

The terminal appears in the pane's rectangle (the other pane keeps working
normally) and follows the pane's location:

* **local pane**   -- forks a real pty running your shell, started in the
  pane's current directory;
* **SFTP / SSH pane** -- opens an interactive shell channel on the pane's
  existing paramiko connection (``invoke_shell``) and ``cd``-s into the
  pane's directory.  Nothing extra to configure -- it reuses the login you
  already made for browsing;
* FTP panes have no shell and are refused with a clear message.

Rendering uses a small built-in VT100-subset emulator (carriage-return
overwrite, backspace, tabs, erase-line/screen, cursor column moves; SGR
colour codes are stripped).  That covers prompts, line editing and ordinary
command output well.  Full-screen programs (vim, htop, ...) want a real
terminal -- run those with `!` (full-screen shell) instead.

Keys: everything is sent to the shell, including Tab (completion).
``Ctrl-]`` switches focus to the other pane while the terminal keeps
running -- Tab back (or click) to return to it.  ``F10`` closes the
terminal, as does exiting the shell (``exit`` / Ctrl-D).  PgUp/PgDn
scroll the local scrollback.
"""

from __future__ import annotations

import curses
import errno
import os
import shlex
import signal
import struct

from ..plugin_api import PanePlugin

SCROLLBACK = 2000


class TermEmulator:
    """A tiny line-oriented VT100-subset screen model.

    Maintains a scrollback of finished lines plus the current line and a
    cursor column.  Understands \\r, \\n, \\b, \\t, CSI K (erase to end of
    line), CSI J (clear screen), CSI C/D (cursor right/left) and strips all
    other CSI/OSC sequences (colours, titles, bracketed paste, ...).
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.cur = ""
        self.col = 0
        self._state = ""      # "", "esc", "csi", "osc"
        self._buf = ""
        import codecs

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    # -- input ----------------------------------------------------------
    def feed(self, data: bytes) -> None:
        for ch in self._decoder.decode(data):
            self._feed_char(ch)

    def _feed_char(self, ch: str) -> None:
        if self._state == "esc":
            if ch == "[":
                self._state, self._buf = "csi", ""
            elif ch == "]":
                self._state = "osc"
            else:
                self._state = ""      # single-char escape: ignore
            return
        if self._state == "csi":
            if "@" <= ch <= "~":
                self._csi(self._buf, ch)
                self._state = ""
            else:
                self._buf += ch
            return
        if self._state == "osc":
            if ch in ("\x07",):       # BEL terminates OSC
                self._state = ""
            elif ch == "\x1b":
                self._state = "osc_esc"
            return
        if self._state == "osc_esc":  # ESC \ (ST) terminates OSC
            self._state = "" if ch == "\\" else "osc"
            return

        if ch == "\x1b":
            self._state = "esc"
        elif ch == "\r":
            self.col = 0
        elif ch == "\n":
            self._newline()
        elif ch == "\b":
            self.col = max(0, self.col - 1)
        elif ch == "\t":
            self.col = (self.col // 8 + 1) * 8
            if len(self.cur) < self.col:
                self.cur = self.cur.ljust(self.col)
        elif ch == "\x07":
            pass                       # bell
        elif ch >= " ":
            if len(self.cur) < self.col:
                self.cur = self.cur.ljust(self.col)
            self.cur = self.cur[: self.col] + ch + self.cur[self.col + 1 :]
            self.col += 1

    def _csi(self, params: str, final: str) -> None:
        def num(default: int = 1) -> int:
            try:
                return int(params.split(";")[0] or default)
            except ValueError:
                return default

        if final == "K":              # erase (to end of) line
            self.cur = self.cur[: self.col]
        elif final == "J":            # clear screen
            self.lines = []
            self.cur = ""
            self.col = 0
        elif final == "C":            # cursor right
            self.col += num()
        elif final == "D":            # cursor left
            self.col = max(0, self.col - num())
        elif final in ("H", "f"):     # cursor home -- treat as column reset
            self.col = 0
        # everything else (m/SGR colours, h/l modes, ...) is ignored

    def _newline(self) -> None:
        self.lines.append(self.cur)
        if len(self.lines) > SCROLLBACK:
            del self.lines[: len(self.lines) - SCROLLBACK]
        self.cur = ""
        self.col = 0

    # -- output ---------------------------------------------------------
    def visible(self, height: int, scroll: int = 0):
        """The last ``height`` rows, ``scroll`` lines back from the bottom."""
        all_lines = self.lines + [self.cur]
        end = max(0, len(all_lines) - scroll)
        start = max(0, end - height)
        return all_lines[start:end]


class TerminalPlugin(PanePlugin):
    name = "Terminal"
    description = "Shell inside this pane (local pty, or SSH for remote panes)"
    wants_timer = True     # the app polls tick() so output flows while idle

    SWITCH_KEY = 29        # Ctrl-]: focus the other pane, keep the shell alive
    CLOSE_KEY = curses.KEY_F10

    def on_start(self) -> None:
        self.term = TermEmulator()
        self.scroll = 0
        self.done = False
        self.status = ""
        self._rows = 24
        self._cols = 80
        self._pid = None       # local child pid
        self._fd = None        # local pty master
        self._chan = None      # paramiko channel

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
        if pid == 0:  # child
            try:
                os.chdir(path)
            except OSError:
                pass
            os.environ["TERM"] = "vt100"
            os.execvp(shell, [shell])
            os._exit(127)  # pragma: no cover
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
        chan.get_pty(term="vt100", width=self._cols, height=self._rows)
        chan.invoke_shell()
        chan.setblocking(False)
        self._chan = chan
        # Jump to the pane's directory in the remote shell.
        chan.sendall(f"cd {shlex.quote(path)}\n".encode())

    # -- pumping ------------------------------------------------------------
    def tick(self) -> None:
        """Pull any pending shell output into the emulator (non-blocking)."""
        if self.done:
            return
        if self._fd is not None:
            while True:
                try:
                    data = os.read(self._fd, 65536)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        break
                    data = b""
                if not data:
                    self._finish()
                    return
                self.term.feed(data)
                if len(data) < 65536:
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
            while self._chan.recv_ready():
                data = self._chan.recv(65536)
                if not data:
                    break
                self.term.feed(data)
            if self._chan.exit_status_ready() or self._chan.closed:
                # Drain what's left, then mark finished.
                try:
                    while self._chan.recv_ready():
                        self.term.feed(self._chan.recv(65536))
                except Exception:
                    pass
                self._finish()

    def _finish(self) -> None:
        if not self.done:
            self.done = True
            self.status = "[process exited -- press any key to close]"

    # -- keys ----------------------------------------------------------------
    def handle_key(self, key: int):
        if self.done:
            self.on_exit()
            return False
        if key == self.SWITCH_KEY:  # Ctrl-]: hand focus to the other pane
            self.ctx.focus_other()
            return True
        if key == self.CLOSE_KEY:   # F10: close the terminal
            self.on_exit()
            return False
        if key == curses.KEY_PPAGE:
            self.scroll = min(self.scroll + 10,
                              max(0, len(self.term.lines) - 1))
            return True
        if key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 10)
            return True

        data = self._encode_key(key)
        if data:
            self.scroll = 0
            self._send(data)
        return True

    @staticmethod
    def _encode_key(key: int) -> bytes:
        specials = {
            curses.KEY_UP: b"\x1b[A",
            curses.KEY_DOWN: b"\x1b[B",
            curses.KEY_RIGHT: b"\x1b[C",
            curses.KEY_LEFT: b"\x1b[D",
            curses.KEY_HOME: b"\x1b[H",
            curses.KEY_END: b"\x1b[F",
            curses.KEY_DC: b"\x1b[3~",
        }
        if key in specials:
            return specials[key]
        if key in (curses.KEY_BACKSPACE, 127, 8):
            return b"\x7f"
        if key in (10, 13, curses.KEY_ENTER):
            return b"\r"
        if 0 <= key < 32:              # control keys (^C, ^D, ^Z, ^L, Tab...)
            return bytes([key])
        if 32 <= key < 0x110000:
            try:
                return chr(key).encode("utf-8")
            except ValueError:
                return b""
        return b""

    def _send(self, data: bytes) -> None:
        try:
            if self._fd is not None:
                os.write(self._fd, data)
            elif self._chan is not None:
                self._chan.sendall(data)
        except Exception:
            self._finish()

    # -- drawing ---------------------------------------------------------------
    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        rows, cols = h - 1, w
        if (rows, cols) != (self._rows, self._cols) and rows > 0 and cols > 0:
            self._rows, self._cols = rows, cols
            self._resize(rows, cols)

        fs = self.ctx.own_fs
        where = f"{fs.label()}:{self.ctx.own_path}"
        title = f" [terminal] {where}  Ctrl-]:switch pane  F10:close "
        self.put(stdscr, y, x, w, title, curses.A_REVERSE)

        visible = self.term.visible(rows, self.scroll)
        pad = rows - len(visible)
        for row in range(rows):
            text = visible[row - pad] if row >= pad else ""
            self.put(stdscr, y + 1 + row, x, w, text)

        if self.status:
            self.put(stdscr, y + h - 1, x, w, f" {self.status} ",
                     curses.A_REVERSE)
        elif self.scroll == 0 and visible:
            # Draw the cursor as a reverse cell on the last rendered row.
            cy = y + 1 + pad + len(visible) - 1
            cx = x + min(self.term.col, w - 1)
            line = visible[-1]
            ch = line[self.term.col] if self.term.col < len(line) else " "
            self.put(stdscr, cy, cx, 1, ch, curses.A_REVERSE, pad=False)

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
