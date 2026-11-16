# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""A background command's terminal, shown in a pane (MC-027).

The pane is the in-pane terminal (``t``) looking at a screen it does not
own: the emulator, the colours, the keys and the ``Ctrl-]`` menu are the
terminal plug-in's, while the process, its pty and its
:class:`~meridian_commander.vt.Screen` belong to the
:class:`~meridian_commander.background.Command`, which keeps running and
keeps its screen up to date whether or not a pane shows it.

So closing this pane, in any of the ways a pane's plug-in is closed, only
detaches the view (:meth:`CommandPane.on_exit`).  ``Ctrl-]`` ``q`` hides the
terminal and gives the pane its listing back; ``Ctrl-]`` ``k`` stops the
command, after asking.  Every other key goes to the program -- ``Ctrl-C``
included, which reaches it through its own terminal as it would at a prompt.

Once the command has ended its last screen stays readable: ``PgUp`` and the
mouse wheel scroll back through its history, and any other key gives the
pane back.
"""

from __future__ import annotations

import curses
import time

from .plugins.terminal import MENU_KEY, TerminalPlugin


class CommandPane(TerminalPlugin):
    """The view of one background command, in the pane that shows it."""

    name = "Command"
    description = "A background command's terminal, in this pane"

    def __init__(self, ctx, command) -> None:
        self.command = command
        super().__init__(ctx)

    def _launch(self) -> None:
        """Attach to the command's screen rather than starting anything.

        The pane's size is compared with the screen's own on the first draw,
        so a pane of a different size resizes the screen and tells the
        program, which redraws to fit.
        """
        command = self.command
        self.screen = command.screen
        self._rows, self._cols = command.screen.rows, command.screen.cols
        self._seen = command.output
        self._history = command.screen.history_count
        command.viewer = self
        self.tick()

    # -- pumping --------------------------------------------------------------
    def tick(self) -> None:
        """Notice new output and the end; the reading is the command's."""
        command = self.command
        history = command.screen.history_count
        if self.scroll and history != self._history:
            # Keep a scrolled-back view on the same lines as output pushes
            # more history in beneath it.
            self.scroll = min(self._max_scroll(),
                              self.scroll + history - self._history)
        self._history = history
        if command.output != self._seen:
            self._seen = command.output
            self._busy_at = time.monotonic()
        if command.ended and not self.done:
            self._finish()

    def _finish(self) -> None:
        if not self.done:
            self.done = True
            self.menu = False
            self.status = (f"[{self.command.state_text()} -- "
                           f"PgUp history, any key closes]")

    # -- keys ------------------------------------------------------------------
    def handle_key(self, key: int | str):
        if not self.done or key == curses.KEY_MOUSE:
            return super().handle_key(key)
        # Ended: there is nothing to type to, but its history can be read.
        if self.menu:
            return self._menu_key(key)
        if key == MENU_KEY:
            self.menu = True
            return True
        if self.scroll or key == curses.KEY_PPAGE:
            return self._scroll_key(key)
        return False

    def _menu_key(self, key: int | str):
        """The key after Ctrl-]: ``q`` hides, ``k`` stops, the rest as ``t``."""
        if key in (ord("q"), ord("Q")):
            self.menu = False
            return False                  # the app detaches the view
        if key in (ord("k"), ord("K")):
            self.menu = False
            self.ctx.app._stop_command(self.command)
            return True
        return super()._menu_key(key)

    def wheel(self, lines: int) -> None:
        """The mouse wheel scrolls back, an ended command's history too."""
        self._scroll_by(lines)

    def _send(self, data: bytes) -> None:
        self.command.send(data)

    # -- drawing ------------------------------------------------------------------
    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        # Under the command's lock: its reader thread feeds the same screen.
        with self.command.lock:
            super().draw(stdscr, y, x, h, w)

    def _resize(self, rows: int, cols: int) -> None:
        self.command.set_size(rows, cols)

    def _caption(self) -> str:
        command = self.command
        return (f" [{command.name}] {command.state_text()} "
                f"in {command.directory} ")

    def _footer(self) -> str:
        # Each fits the forty columns of half an 80-column screen.
        if self.menu:
            if self.done:
                return " Tab pane  z zoom  q close  PgUp scroll "
            return " Tab pane  z zoom  q hide  k stop  PgUp "
        if self.scroll:
            return f" scrollback, {self.scroll} lines up  Esc: live "
        if self.status:
            return f" {self.status} "
        return " Ctrl-]: Tab pane  q hide  k stop "

    # -- lifecycle ------------------------------------------------------------------
    @property
    def closed_note(self) -> str:
        """What the hint line says once the pane has its listing back."""
        command = self.command
        if command.running():
            return (f"Command &{command.number} '{command.name}' still "
                    f"running -- J shows it")
        return f"Command &{command.number} '{command.name}' {command.state_text()}"

    def on_exit(self) -> None:
        """Detach from the command, which goes on running."""
        if self.command.viewer is self:
            self.command.viewer = None
