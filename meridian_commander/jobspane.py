# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""``J``: the background jobs, in this pane, kept up to date as they run.

Each job gets two lines: its number, state and what it is doing, then how
far it has got, how fast, and the file it is on.  ``p`` pauses or resumes
the job under the cursor, ``c`` cancels it, and ``Enter`` shows what went
wrong in it -- errors stay with their job rather than popping up over
whatever is on the screen when they happen.  ``J``, ``Esc`` or ``q`` give the
pane back.

The background commands (``&``, MC-027) are listed under the copies and
moves, numbered ``&1``, ``&2``: running, stopped, or exited with their
status.  On a command ``Enter`` shows its terminal in this pane, ``k`` (or
``c``) stops it after asking, and ``d`` removes one that has ended.  ``k``
still moves the cursor up on a copy or a move.
"""

from __future__ import annotations

import curses

from . import dialogs, theme
from .background import Command
from .jobs import FINISHED, Job
from .plugin_api import PanePlugin
from .util import human_size


class JobsPane(PanePlugin):
    """The jobs list, drawn from the queue every time the screen is."""

    name = "Jobs"
    description = "Background copies, moves and commands"

    def on_start(self) -> None:
        self.row = 0
        #: Redraw on the poll, so progress moves without a key being pressed.
        self.wants_timer = True

    def entries(self) -> list[Job | Command]:
        """The copies and moves, then the commands."""
        app = self.ctx.app
        return [*app.jobs.jobs, *app.commands.commands]

    def selected(self) -> Job | Command | None:
        entries = self.entries()
        if not entries:
            return None
        self.row = max(0, min(self.row, len(entries) - 1))
        return entries[self.row]

    # -- drawing ---------------------------------------------------------------
    @staticmethod
    def lines_for(job: Job) -> tuple[str, str]:
        first = f"{job.number:>2}. {job.state_text():<9} {job.title()}"
        second = f"    {job.progress_text()}"
        speed = job.speed()
        if speed and job.state not in FINISHED:
            second += f"  {human_size(int(speed)).strip()}/s"
        if job.current:
            second += f"  {job.current}"
        if job.errors:
            count = len(job.errors)
            second += f"  {count} error{'' if count == 1 else 's'}: Enter"
        elif job.note:
            second += f"  ({job.note})"
        return first, second

    @staticmethod
    def lines_for_command(command: Command) -> tuple[str, str]:
        first = (f"&{command.number}. {command.state_text():<9} "
                 f"{command.name}: {command.line}")
        verb = "up" if command.running() else "ran"
        second = f"    in {command.directory}  {verb} {command.age()}"
        if command.viewer is not None:
            second += "  (shown)"
        return first, second

    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if h < 4 or w < 12:
            return
        self.put(stdscr, y, x, w, " Jobs ", theme.attr("keybar"))
        current = self.selected()
        entries = self.entries()
        room = (h - 2) // 2
        first = max(0, self.row - room + 1)
        row = y + 1
        for entry in entries[first:first + room]:
            attr = curses.A_REVERSE if entry is current else None
            if isinstance(entry, Command):
                top, bottom = self.lines_for_command(entry)
            else:
                top, bottom = self.lines_for(entry)
            self.put(stdscr, row, x, w, top, attr)
            self.put(stdscr, row + 1, x, w, bottom, attr)
            row += 2
        if not entries:
            self.put(stdscr, row, x, w,
                     " No jobs. F5 or F6 with Background on starts one;"
                     " & runs a command.")
            row += 1
        while row < y + h - 1:
            self.put(stdscr, row, x, w, "")
            row += 1
        if isinstance(current, Command):
            keys = " Enter: show  k: stop  d: remove  J: close "
        else:
            keys = " p: pause  c: cancel  Enter: errors  J: close "
        self.put(stdscr, y + h - 1, x, w, keys, theme.attr("keybar"))

    # -- keys -----------------------------------------------------------------
    def handle_key(self, key: int | str):
        if key in (27, ord("q")):
            return False
        if key in (9, ord("J"), curses.KEY_F10):
            return None                       # Tab, J and F10 are the app's
        job = self.selected()
        if key in (curses.KEY_DOWN, ord("j")):
            self.row += 1
        elif key == curses.KEY_UP or (key == ord("k")
                                      and not isinstance(job, Command)):
            self.row = max(0, self.row - 1)
        elif isinstance(job, Command):
            self._command_key(job, key)
        elif job is None:
            pass
        elif key == ord("p"):
            if job.paused:
                job.resume()
            else:
                job.pause()
        elif key in (ord("c"), curses.KEY_DC):
            if job.active() and dialogs.confirm(
                    self.ctx.app.stdscr, "Cancel job",
                    f"Cancel job {job.number}?\n{job.title()}"):
                job.cancel()
        elif key in (10, 13, curses.KEY_ENTER):
            text = "\n".join(job.errors[:12]) or job.note or "No errors."
            dialogs.message(self.ctx.app.stdscr, f"Job {job.number}",
                            f"{job.title()}\n\n{text}",
                            error=bool(job.errors))
        return True

    def _command_key(self, command: Command, key: int | str) -> None:
        """A key on a background command's row."""
        app = self.ctx.app
        if key in (10, 13, curses.KEY_ENTER):
            app._show_command(command)        # in this pane, in place of us
        elif key in (ord("k"), ord("c"), curses.KEY_DC):
            app._stop_command(command)
        elif key == ord("d"):
            app._remove_command(command)
