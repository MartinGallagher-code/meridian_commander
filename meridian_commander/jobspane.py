# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""``J``: the background jobs, in this pane, kept up to date as they run.

Each job gets two lines: its number, state and what it is doing, then how
far it has got, how fast, and the file it is on.  ``p`` pauses or resumes
the job under the cursor, ``c`` cancels it, and ``Enter`` shows what went
wrong in it -- errors stay with their job rather than popping up over
whatever is on the screen when they happen.  ``J``, ``Esc`` or ``q`` give the
pane back.
"""

from __future__ import annotations

import curses

from . import dialogs, theme
from .jobs import FINISHED, Job
from .plugin_api import PanePlugin
from .util import human_size


class JobsPane(PanePlugin):
    """The jobs list, drawn from the queue every time the screen is."""

    name = "Jobs"
    description = "Background copies and moves"

    def on_start(self) -> None:
        self.row = 0
        #: Redraw on the poll, so progress moves without a key being pressed.
        self.wants_timer = True

    def jobs(self) -> list[Job]:
        return self.ctx.app.jobs.jobs

    def selected(self) -> Job | None:
        jobs = self.jobs()
        if not jobs:
            return None
        self.row = max(0, min(self.row, len(jobs) - 1))
        return jobs[self.row]

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

    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if h < 4 or w < 12:
            return
        self.put(stdscr, y, x, w, " Jobs ", theme.attr("keybar"))
        current = self.selected()
        jobs = self.jobs()
        room = (h - 2) // 2
        first = max(0, self.row - room + 1)
        row = y + 1
        for job in jobs[first:first + room]:
            attr = curses.A_REVERSE if job is current else None
            top, bottom = self.lines_for(job)
            self.put(stdscr, row, x, w, top, attr)
            self.put(stdscr, row + 1, x, w, bottom, attr)
            row += 2
        if not jobs:
            self.put(stdscr, row, x, w,
                     " No jobs. F5 or F6 with Background on starts one.")
            row += 1
        while row < y + h - 1:
            self.put(stdscr, row, x, w, "")
            row += 1
        self.put(stdscr, y + h - 1, x, w,
                 " p: pause  c: cancel  Enter: errors  J: close ",
                 theme.attr("keybar"))

    # -- keys -----------------------------------------------------------------
    def handle_key(self, key: int | str):
        if key in (27, ord("q")):
            return False
        if key in (9, ord("J"), curses.KEY_F10):
            return None                       # Tab, J and F10 are the app's
        job = self.selected()
        if key in (curses.KEY_DOWN, ord("j")):
            self.row += 1
        elif key in (curses.KEY_UP, ord("k")):
            self.row = max(0, self.row - 1)
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
