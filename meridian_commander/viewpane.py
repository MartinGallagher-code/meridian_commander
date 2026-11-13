# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Viewing and editing in a pane: ``F3`` and ``F4`` with ``[ui] open_in = pane``.

Full-screen, a viewer or the editor takes the whole terminal until it closes,
and the file manager waits behind it.  With **Options > View and edit in a
pane** on, it opens in a pane instead (MC-025), shown alone at full width,
and one key brings the listing back beside it:

* ``z`` in a viewer, or ``Ctrl-]`` then ``z`` anywhere (the editor types a
  ``z``, and the terminal sends one to the program), switches between that
  and two panes -- the file on one side, the listing it came from on the
  other.  That is single mode (``z`` in a file pane), turned on for the
  view's sake and put back as it was when the view closes.
* ``Ctrl-]`` then ``Tab`` gives the keyboard to the listing, which keeps the
  view in sight beside it; ``Tab`` in the listing comes back.
* ``Ctrl-]`` then ``q`` closes it, as the view's own keys do.

The same rule as the terminal's, because the terminal works this way already:
one menu key that no viewer, editor or program uses for anything else.

Two kinds of pane do it.  :class:`ViewPane` hosts any of the nine viewers, or
the built-in editor, by handing it a window over the pane: each one already
draws into whatever window it is given and keeps its keys in a method of its
own, so a viewer in a pane has exactly the keys it has full-screen.
:class:`ProgramPane` is the in-pane terminal running an outside pager or
editor -- ``less``, ``vim``, whatever Options > Viewer or Editor names -- on
the file, or on a private copy of a remote one
(:class:`~meridian_commander.extedit.Session`), and closes when the program
exits.

*Unsaved work is asked about.*  Full-screen, nothing else could happen while
the editor was open.  In a pane the application can quit, and something else
can be opened over it; both ask the pane first (:meth:`may_close`).  Deleting,
moving or overwriting the file from the other pane is refused while it is
open with changes not saved (:meth:`unsaved_file`).
"""

from __future__ import annotations

import curses
import os
import time

from . import dialogs, theme
from .browsers import viewer_for
from .editor import Editor
from .imageview import ImageView
from .plugin_api import PanePlugin
from .plugins.terminal import MENU_KEY, TerminalPlugin

#: The footer while ``Ctrl-]`` waits for the key after it.  Half of an
#: 80-column screen, which is what a pane has in the two-pane view.
MENU_HINT = " Ctrl-]: Tab listing  z panes  q close "

#: Keys that show a picture full-screen, with its real pixels.  In a pane it
#: is drawn in half-blocks, as quick view draws it: sixel and kitty pictures
#: are written past curses, and could stay on the screen after the pane is
#: drawn over.
FULL_SCREEN_KEYS = (10, 13, curses.KEY_ENTER, ord("g"), ord("G"))

#: What the pane's child exits with when the program cannot be run at all --
#: and what a shell says of a command it cannot find.
EXEC_FAILED = 127

#: How long a program that has closed its terminal is given to exit, so its
#: exit status can be read.  Bounded: a program that closes its terminal and
#: carries on must not freeze the file manager waiting for it.
REAP_SECONDS = 1.0


class ViewPane(PanePlugin):
    """A viewer, or the built-in editor, in a pane of its own."""

    name = "View"
    description = "A file in its viewer, or in the editor, in this pane"
    # The view wants the whole width; the listing beside it is the user's
    # choice (z), not something the view works on.
    uses_other_pane = False

    def __init__(self, ctx, view) -> None:
        self.view = view
        super().__init__(ctx)

    def on_start(self) -> None:
        #: Ctrl-] was pressed: the next key is the pane's own.
        self.menu = False
        #: The pane's height and width as last drawn, for paging.
        self.size = (24, 80)
        #: The pane the file came from, and the layout from before the view
        #: opened.  The application fills them in and puts both back when
        #: the view closes (``App._view_closed``).
        self.source = None
        self.layout = None
        self._win = None
        self._box: tuple[int, int, int, int] | None = None
        if isinstance(self.view, ImageView):
            self.view.graphics = False

    @property
    def editing(self) -> bool:
        return isinstance(self.view, Editor)

    @property
    def closed_note(self) -> str:
        """What the hint line says once the view has closed."""
        return f"Closed {self.view.name}"

    # -- drawing ------------------------------------------------------------
    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if h < 3 or w < 8:
            return
        self.size = (h, w)
        if self._box != (y, x, h, w):
            # A window over the pane, sharing the screen beneath it, as quick
            # view gives its viewers; remade only when the pane moves.
            self._win = stdscr.subwin(h, w, y, x)
            self._box = (y, x, h, w)
        self.view.draw(self._win)
        if self.menu:
            self.put(stdscr, y + h - 1, x, w, MENU_HINT, theme.attr("keybar"))

    def cursor(self) -> tuple[int, int] | None:
        """Where the terminal's cursor goes: the editor's, on the screen."""
        spot = getattr(self.view, "cursor_at", None)
        if spot is None or self.menu or self._box is None:
            return None
        return self._box[0] + spot[0], self._box[1] + spot[1]

    # -- keys ---------------------------------------------------------------
    def handle_key(self, key: int | str):
        if key == curses.KEY_MOUSE:
            # The application's: the wheel scrolls the view, and a click on
            # the listing gives it the keyboard.
            self.menu = False
            return None
        if self.menu:
            return self._menu_key(key)
        if key == MENU_KEY:
            self.menu = True
            return True
        height, width = self.size
        if self.editing:
            saves = self.view.saves
            self.view.message = ""
            done = self.view.handle_key(key, page=max(1, height - 3)) == "quit"
            if self.view.saves != saves and self.source is not None:
                # Saved: the listing beside it shows the new size and time.
                self.ctx.app._reload_after_change(self.source)
            return not done or not self.may_close()
        if key in (ord("z"), ord("Z")):
            self.ctx.toggle_single()
            return True
        if isinstance(self.view, ImageView) and key in FULL_SCREEN_KEYS:
            self._full_screen()
            return True
        return bool(self.view.handle_key(key, self.ctx.app.stdscr,
                                         height, width))

    def _menu_key(self, key: int | str):
        """The key after Ctrl-]: one of the pane's own commands."""
        self.menu = False
        if key == 9:                                  # Tab: the listing
            self.ctx.focus_other()
        elif key in (ord("z"), ord("Z")):             # one pane or two
            self.ctx.toggle_single()
        elif key in (ord("q"), ord("Q")):             # close
            return not self.may_close()
        # Esc, or anything else: the menu simply closes.
        return True

    def _full_screen(self) -> None:
        """The picture full-screen, with its real pixels, as F3 shows it."""
        stdscr = self.ctx.app.stdscr
        viewer_for(self.view.fs, self.view.path).run(stdscr)
        curses.curs_set(0)
        # Pixels are invisible to curses, which would otherwise leave them
        # wherever it believes the screen already shows a blank.
        stdscr.clearok(True)

    # -- closing ------------------------------------------------------------
    def unsaved_file(self):
        """``(fs, path)`` of the file open with changes not saved, or None."""
        if self.editing and self.view.dirty:
            return self.view.fs, self.view.path
        return None

    def may_close(self) -> bool:
        if self.unsaved_file() is None:
            return True
        return dialogs.confirm(self.ctx.app.stdscr, "Unsaved changes",
                               f"{self.view.name} has unsaved changes.\n"
                               f"Discard them?")

    def on_exit(self) -> None:
        self.ctx.app._view_closed(self)


class ProgramPane(TerminalPlugin):
    """An outside pager or editor, running on the file in the pane's terminal.

    Always a local pty, whatever the pane is on: a remote file is fetched to
    a private copy by the :class:`~meridian_commander.extedit.Session`, and
    the program runs on that, as it does full-screen.  When the program exits
    the pane closes by itself (``wants_close``), with what the session has to
    say on the hint line -- unless it exited with an error, which stays on the
    screen until a key is pressed, so the reason can be read.
    """

    name = "Program"
    description = "An outside pager or editor, on a file, in this pane"

    def __init__(self, ctx, session, command: list[str],
                 cwd: str | None) -> None:
        self.session = session
        self.command = command
        self.cwd = cwd
        #: What the hint line says once the pane has closed.
        self.closed_note = ""
        #: Set when the program has exited cleanly: the application closes
        #: the pane on its next poll, as a full-screen run would have ended.
        self.wants_close = False
        self.source = None
        self.layout = None
        super().__init__(ctx)

    def _launch(self) -> None:
        self._start_local(self.cwd or ".", self.command)

    @property
    def editing(self) -> bool:
        return self.session.editing

    def _caption(self) -> str:
        return f" [{self.session.argv[0]}] {self.session.name} "

    def _menu_key(self, key: int | str):
        if key in (ord("q"), ord("Q")) and not self.may_close():
            self.menu = False
            return True
        return super()._menu_key(key)

    def _finish(self) -> None:
        if self.done:
            return
        status = self._reap()
        # An exit status that could not be read is taken as a clean one: the
        # program did start, and what it saved is still worth writing back.
        ran = None if status == EXEC_FAILED else (status or 0)
        self.closed_note = self.session.finish(ran)
        self.session.close()
        super()._finish()
        if status in (0, None):
            self.wants_close = True
        else:
            self.status = f"[{self.closed_note} -- any key closes]"

    def _reap(self) -> int | None:
        """The program's exit status, waiting a moment for it; None if not."""
        if self._pid is None:
            return self.exit_status
        deadline = time.monotonic() + REAP_SECONDS
        while True:
            try:
                pid, status = os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                return None
            if pid == self._pid:
                self._pid = None
                self.exit_status = os.waitstatus_to_exitcode(status)
                return self.exit_status
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def unsaved_file(self):
        """``(fs, path)`` while an editor is still open on the file, or None.

        Whether it holds unsaved changes cannot be known from outside, so an
        editor still running is taken to have some.
        """
        if self.session.editing and not self.done:
            return self.session.fs, self.session.path
        return None

    def may_close(self) -> bool:
        if self.unsaved_file() is None:
            return True
        return dialogs.confirm(self.ctx.app.stdscr, "Still editing",
                               f"{self.session.argv[0]} is still open on "
                               f"{self.session.name}.\nClose it, losing "
                               f"anything it has not saved?")

    def on_exit(self) -> None:
        super().on_exit()
        if not self.done:
            # Closed before the program ended: it has been told to go, and
            # what it had already saved is still written back.
            self.done = True
            self.closed_note = self.session.finish(0)
        self.session.close()
        self.ctx.app._view_closed(self)
