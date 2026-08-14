"""Built-in plugin: a process browser and killer for the pane's host.

Like the terminal, the plugin follows the pane's location:

* **local pane** -- lists the local machine's processes by running ``ps``;
* **SFTP / SSH pane** -- runs ``ps`` over the pane's existing paramiko
  connection, so it shows the *remote* host's processes without dialling
  or authenticating again;
* FTP panes have no shell and are refused with a clear message.

The listing refreshes itself every couple of seconds (configurable), sorts
by any column, filters by name, and kills the process under the cursor
(``F8``/``k``/``Del``) after asking which signal to send.  ``ps`` is used
rather than ``/proc`` so exactly the same code serves local and remote
hosts; the one consequence is that the CPU column is the average over each
process's lifetime, as ``ps`` reports it, not an instantaneous figure.

Configure the refresh interval in ``config.ini``::

    [plugin:processes]
    refresh = 2
"""

from __future__ import annotations

import curses
import os
import signal
import time

from .. import theme
from ..config import plugin_settings
from ..plugin_api import PanePlugin
from ..util import human_size

DEFAULTS = {
    "refresh": 2,          # seconds between automatic refreshes
}

#: Column sets tried in order: the rich POSIX format first, then a minimal
#: one for hosts whose ``ps`` lacks the optional columns (busybox and kin).
PS_COLUMNS = (
    ("pid", "user", "pcpu", "pmem", "rss", "etime", "args"),
    ("pid", "user", "args"),
)

#: Signals offered by the kill prompt, in the order the footer names them.
SIGNALS = {
    "TERM": signal.SIGTERM,
    "KILL": signal.SIGKILL,
    "HUP": signal.SIGHUP,
    "INT": signal.SIGINT,
}

#: Sort key per key press: (attribute, descending-by-default).
SORT_KEYS = {
    ord("c"): ("cpu", True),
    ord("m"): ("mem", True),
    ord("t"): ("seconds", True),
    ord("p"): ("pid", False),
    ord("n"): ("command", False),
    ord("u"): ("user", False),
}


class Process:
    """One row of the listing."""

    __slots__ = ("pid", "user", "cpu", "mem", "rss", "etime", "command")

    def __init__(self, pid: int, user: str, cpu: float | None = None,
                 mem: float | None = None, rss: int | None = None,
                 etime: str = "", command: str = "") -> None:
        self.pid = pid
        self.user = user
        self.cpu = cpu
        self.mem = mem
        self.rss = rss          # kilobytes, as ps reports it
        self.etime = etime
        self.command = command

    @property
    def seconds(self) -> int:
        """The elapsed time as seconds, for sorting."""
        return _etime_seconds(self.etime)


def _etime_seconds(etime: str) -> int:
    """Parse ps's ``[[dd-]hh:]mm:ss`` elapsed-time format."""
    text = etime.strip()
    if not text:
        return 0
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return 0
    total = 0
    try:
        for part in text.split(":"):
            total = total * 60 + int(part)
    except ValueError:
        return 0
    return days * 86400 + total


def _number(text: str) -> float:
    """Parse a ps numeric field, tolerating a locale's decimal comma."""
    return float(text.replace(",", "."))


def parse_ps(text: str, columns) -> list[Process]:
    """Parse headerless ``ps -o`` output produced with ``columns``.

    Lines that do not parse (a header slipping through, a garbled row) are
    skipped rather than failing the whole listing.
    """
    rich = len(columns) > 3
    procs: list[Process] = []
    for line in text.splitlines():
        fields = line.split(None, len(columns) - 1)
        if len(fields) != len(columns):
            continue
        try:
            if rich:
                pid, user, pcpu, pmem, rss, etime, args = fields
                procs.append(Process(int(pid), user, _number(pcpu),
                                     _number(pmem), int(rss), etime, args))
            else:
                pid, user, args = fields
                procs.append(Process(int(pid), user, command=args))
        except ValueError:
            continue
    return procs


class ProcessesPlugin(PanePlugin):
    name = "Processes"
    description = "Browse and kill processes on this pane's host (local or SSH)"
    wants_timer = True     # the app polls tick() so the listing auto-refreshes

    def on_start(self) -> None:
        self.cfg = plugin_settings("processes", DEFAULTS)
        self.procs: list[Process] = []
        self.view: list[Process] = []
        self.cursor = 0
        self.scroll = 0
        self.sort_attr, self.sort_desc = "cpu", True
        self.filter = ""
        self.mode = "list"          # "list", "filter" or "confirm"
        self.pending: Process | None = None
        self.status = ""
        self.error = ""
        self.columns = None          # decided by the first refresh
        self._last_refresh = 0.0

        fs = self.ctx.own_fs
        kind = getattr(fs, "scheme", "")
        if kind == "local":
            self._remote = None
        elif kind in ("sftp", "ssh"):
            client = getattr(fs, "_client", None)
            transport = client.get_transport() if client is not None else None
            if transport is None or not transport.is_active():
                raise RuntimeError("The SSH connection is no longer active.")
            self._remote = client
        else:
            raise RuntimeError(
                "No process access for this pane type (FTP has no shell); "
                "open Processes from a local, SFTP or SSH pane."
            )
        self.refresh()

    # -- running ps ---------------------------------------------------------
    def _run_ps(self, columns) -> str:
        argv = ["ps", "-A"]
        for column in columns:
            argv += ["-o", f"{column}="]
        if self._remote is None:
            import subprocess

            done = subprocess.run(argv, capture_output=True, text=True)
            if done.returncode != 0:
                raise RuntimeError(done.stderr.strip() or
                                   f"ps failed ({done.returncode})")
            return done.stdout
        _in, stdout, stderr = self._remote.exec_command(" ".join(argv),
                                                        timeout=15)
        out = stdout.read().decode("utf-8", errors="replace")
        if stdout.channel.recv_exit_status() != 0:
            err = stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(err or "ps failed")
        return out

    def refresh(self) -> None:
        """Re-run ps, keeping the cursor on the same process if it survives."""
        self._last_refresh = time.monotonic()
        tried = (self.columns,) if self.columns else PS_COLUMNS
        error = None
        for columns in tried:
            try:
                procs = parse_ps(self._run_ps(columns), columns)
            except Exception as exc:
                error = exc
                continue
            if procs:
                self.columns = columns
                self.procs = procs
                self.error = ""
                self._rebuild_view()
                return
        self.error = str(error) if error else "ps returned nothing"

    def tick(self) -> None:
        """Refresh on the configured cadence (paused while a prompt is open)."""
        if self.mode != "list":
            return
        if time.monotonic() - self._last_refresh >= float(self.cfg["refresh"]):
            self.refresh()

    # -- the visible slice --------------------------------------------------
    def _rebuild_view(self) -> None:
        anchor = self.view[self.cursor].pid if self.cursor < len(self.view) \
            else None
        procs = self.procs
        if self.filter:
            needle = self.filter.lower()
            procs = [p for p in procs
                     if needle in p.command.lower() or needle in p.user.lower()]
        key = self.sort_attr

        def sort_value(proc: Process):
            value = getattr(proc, key)
            if isinstance(value, str):
                return value.lower()
            return -1.0 if value is None else value

        self.view = sorted(procs, key=sort_value, reverse=self.sort_desc)
        self.cursor = 0
        if anchor is not None:
            for index, proc in enumerate(self.view):
                if proc.pid == anchor:
                    self.cursor = index
                    break

    # -- killing ------------------------------------------------------------
    def _kill(self, proc: Process, signame: str) -> None:
        try:
            if self._remote is None:
                os.kill(proc.pid, SIGNALS[signame])
            else:
                command = f"kill -{int(SIGNALS[signame])} {proc.pid}"
                _in, stdout, stderr = self._remote.exec_command(command,
                                                                timeout=15)
                if stdout.channel.recv_exit_status() != 0:
                    err = stderr.read().decode("utf-8",
                                               errors="replace").strip()
                    raise RuntimeError(err or "kill exited nonzero")
        except Exception as exc:
            self.status = f"kill {proc.pid} failed: {exc}"
            return
        self.status = f"sent SIG{signame} to {proc.pid}"
        self.refresh()

    # -- keys ---------------------------------------------------------------
    def handle_key(self, key: int):
        if self.mode == "confirm":
            return self._confirm_key(key)
        if self.mode == "filter":
            return self._filter_key(key)
        return self._list_key(key)

    def _list_key(self, key: int):
        self.status = ""
        if key == 9:               # Tab: let the app switch panes
            return None
        if key in (27, curses.KEY_F10):
            self.on_exit()
            return False
        if key == curses.KEY_UP:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_DOWN:
            self.cursor = min(max(0, len(self.view) - 1), self.cursor + 1)
        elif key == curses.KEY_PPAGE:
            self.cursor = max(0, self.cursor - 10)
        elif key == curses.KEY_NPAGE:
            self.cursor = min(max(0, len(self.view) - 1), self.cursor + 10)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = max(0, len(self.view) - 1)
        elif key in (curses.KEY_F8, curses.KEY_DC, ord("k")):
            if self.cursor < len(self.view):
                self.pending = self.view[self.cursor]
                self.mode = "confirm"
        elif key == ord("/"):
            self.mode = "filter"
        elif key == ord("r"):
            self.refresh()
        elif key in SORT_KEYS:
            attr, descending = SORT_KEYS[key]
            if attr == self.sort_attr:
                self.sort_desc = not self.sort_desc
            else:
                self.sort_attr, self.sort_desc = attr, descending
            self._rebuild_view()
        return True

    def _filter_key(self, key: int):
        if key == 27:              # Esc: clear the filter and leave the mode
            self.filter = ""
            self.mode = "list"
            self._rebuild_view()
        elif key in (10, 13, curses.KEY_ENTER):
            self.mode = "list"
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.filter = self.filter[:-1]
            self._rebuild_view()
        elif key == 21:            # Ctrl-U clears the filter
            self.filter = ""
            self._rebuild_view()
        elif key == 9:
            return None
        elif 32 <= key < 0x110000:
            self.filter += chr(key)
            self._rebuild_view()
        return True

    def _confirm_key(self, key: int):
        proc, self.pending = self.pending, None
        self.mode = "list"
        if proc is None:
            return True
        if key in (10, 13, curses.KEY_ENTER, ord("y"), ord("Y")):
            self._kill(proc, "TERM")
        elif key == ord("9"):
            self._kill(proc, "KILL")
        elif key == ord("1"):
            self._kill(proc, "HUP")
        elif key == ord("2"):
            self._kill(proc, "INT")
        # anything else (Esc, n, ...) cancels silently
        return True

    # -- drawing ------------------------------------------------------------
    def _format(self, proc: Process, width: int) -> str:
        rich = self.columns is None or len(self.columns) > 3
        cells = [f"{proc.pid:>7d}", f"{proc.user[:10]:<10}"]
        if rich:
            cells += [
                "     " if proc.cpu is None else f"{proc.cpu:5.1f}",
                "     " if proc.mem is None else f"{proc.mem:5.1f}",
                "      " if proc.rss is None
                else human_size(proc.rss * 1024),
                f"{proc.etime:>11}",
            ]
        cells.append(proc.command)
        return " ".join(cells)[:width]

    def _header(self) -> str:
        rich = self.columns is None or len(self.columns) > 3
        cells = [f"{'PID':>7}", f"{'USER':<10}"]
        if rich:
            cells += [f"{'CPU%':>5}", f"{'MEM%':>5}", f"{'RSS':>6}",
                      f"{'TIME':>11}"]
        cells.append("COMMAND")
        return " ".join(cells)

    def _footer(self) -> str:
        if self.mode == "confirm" and self.pending is not None:
            name = self.pending.command.split()[0] if self.pending.command \
                else "?"
            return (f" Kill {self.pending.pid} ({os.path.basename(name)}):"
                    f" Enter=TERM  9=KILL  1=HUP  2=INT  Esc=cancel ")
        if self.mode == "filter":
            return f" filter> {self.filter} "
        if self.status:
            return f" {self.status} "
        return (" F8 kill   / filter   c/m/t/p/n/u sort   r refresh"
                "   Esc close ")

    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if h < 3:
            return
        fs = self.ctx.own_fs
        arrow = "-" if self.sort_desc else "+"
        title = (f" [processes] {fs.label()} -- {len(self.view)} shown,"
                 f" sort {arrow}{self.sort_attr} ")
        self.put(stdscr, y, x, w, title, theme.attr("keybar"))
        self.put(stdscr, y + 1, x, w, self._header(),
                 theme.attr("panelhead"))

        rows = h - 3               # title, header, footer
        if rows > 0:
            if self.cursor < self.scroll:
                self.scroll = self.cursor
            if self.cursor >= self.scroll + rows:
                self.scroll = self.cursor - rows + 1
            visible = self.view[self.scroll:self.scroll + rows]
            for row in range(rows):
                if row < len(visible):
                    proc = visible[row]
                    attr = theme.attr("panelcursor") \
                        if self.scroll + row == self.cursor else None
                    self.put(stdscr, y + 2 + row, x, w,
                             self._format(proc, w), attr)
                elif row == 0 and not visible:
                    text = f"  ! {self.error}" if self.error \
                        else "  (no processes match)"
                    self.put(stdscr, y + 2, x, w, text,
                             theme.attr("panelerror") if self.error
                             else None)
                else:
                    self.put(stdscr, y + 2 + row, x, w, "")

        self.put(stdscr, y + h - 1, x, w, self._footer(),
                 theme.attr("keybar"))
