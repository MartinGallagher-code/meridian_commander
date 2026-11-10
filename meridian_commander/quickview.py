# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Quick view: the other pane shows the file under the cursor, in its viewer.

``Ctrl-Q`` turns the pane opposite the one being browsed into the viewer that
``F3`` would open for the file under the cursor -- text, Markdown, a document,
a deck, a sheet, a table, a PDF, an image -- and moving the cursor shows the
next file there.  It is ``h`` (:mod:`~meridian_commander.peek`) grown up: the
same pane-opposite arrangement, following the same cursor, but with every
viewer instead of head and tail.

Each viewer already draws into whatever window it is given, at that window's
size, so none of them had to change: the quick view hands one a window over
the pane.  Two things are different from ``F3``:

*It does not wait on the network for every arrow key.*  A remote file is
fetched only once the cursor has rested on it for :data:`SETTLE_SECONDS`, and
a file a viewer has to read whole is fetched only up to a size; past it the
pane says how large it is and waits to be asked.  Text is read only as far as
:data:`TEXT_LIMIT`, which is a pane's worth many times over.

*Images are drawn in half-blocks.*  Sixel and kitty pictures are written
straight to the terminal, past curses, and curses repaints only the cells it
believes have changed -- so the pixels of the last picture can stay on the
screen when the cursor moves on.  Half-blocks are curses cells and go when
they are drawn over.  ``Enter`` opens the full viewer, with the real pixels.
"""

from __future__ import annotations

import curses
import time

from . import theme
from .browsers import has_own_browser, viewer_for
from .imageview import ImageView
from .plugin_api import PanePlugin
from .util import human_size
from .viewer import Viewer

#: How long the cursor must rest on a remote file before it is fetched.
SETTLE_SECONDS = 0.3
#: The largest file a viewer that reads it whole is handed without asking.
LOCAL_LIMIT = 32 * 1024 * 1024
REMOTE_LIMIT = 2 * 1024 * 1024
#: The most of a text file the quick view reads.
TEXT_LIMIT = 256 * 1024


class _Glance(Viewer):
    """The text viewer, reading only as much as a pane can use."""

    LIMIT = TEXT_LIMIT


class QuickViewPane(PanePlugin):
    """A pane showing the *other* pane's file, in the viewer F3 would use."""

    name = "Quick view"
    description = "The file under the other pane's cursor, in its viewer"

    def on_start(self) -> None:
        self.view = None
        self.lines: list[str] = []
        self.title = ""
        self.forced = False
        #: (path, backend) of what is shown, to notice when the cursor moves.
        self._source: tuple[str, int] | None = None
        self._since = 0.0
        self.wants_timer = False
        self._win = None
        self._win_box: tuple[int, int, int, int] | None = None

    # -- what is being looked at -------------------------------------------
    def target(self):
        """The other pane's entry and its path, or ``None`` for nothing."""
        entry = self.ctx.other_panel.current()
        if entry is None or entry.name == "..":
            return None
        return entry, self.ctx.other_fs.join(self.ctx.other_path, entry.name)

    def remote(self) -> bool:
        return getattr(self.ctx.other_fs, "scheme", "") != "local"

    def follow(self) -> None:
        """Start over when the other pane's cursor has moved to something new."""
        found = self.target()
        signature = (found[1] if found else "", id(self.ctx.other_fs))
        if signature == self._source:
            return
        self._source = signature
        self.view = None
        self.forced = False
        self.lines = []
        self._since = time.monotonic()
        self.title = found[0].name if found else ""
        if found is None:
            self.lines = ["Nothing under the cursor."]
        elif self.remote():
            # Arrowing past ten files on a server should not fetch ten files.
            self.wants_timer = True
            self.lines = ["Waiting for the cursor to settle..."]
        else:
            self.load()

    def tick(self) -> None:
        """Fetch a remote file once the cursor has rested on it."""
        if self.wants_timer and time.monotonic() - self._since >= SETTLE_SECONDS:
            self.wants_timer = False
            self.load()

    def load(self) -> None:
        """Open the viewer for the target, or say why not."""
        found = self.target()
        self.wants_timer = False
        if found is None:
            return
        entry, path = found
        fs = self.ctx.other_fs
        self.view = None
        try:
            if entry.is_dir:
                self.lines = self._summary(fs, path)
                return
            if not has_own_browser(entry.name):
                self.view = _Glance(fs, path)
                return
            limit = REMOTE_LIMIT if self.remote() else LOCAL_LIMIT
            if (entry.size or 0) > limit and not self.forced:
                self.lines = [f"Large file: {human_size(entry.size).strip()}.",
                              "", "Tab, then Enter, to load it here."]
                return
            view = viewer_for(fs, path)
            if isinstance(view, ImageView):
                view.graphics = False
            self.view = view
        except Exception as exc:
            self.lines = [f"Cannot show it: {exc}"]

    @staticmethod
    def _summary(fs, path: str) -> list[str]:
        entries = fs.listdir(path)
        dirs = sum(1 for e in entries if e.is_dir)
        files = len(entries) - dirs
        total = sum(e.size or 0 for e in entries if not e.is_dir)
        return [f"{len(entries)} entries: {dirs} director"
                f"{'y' if dirs == 1 else 'ies'}, {files} file"
                f"{'' if files == 1 else 's'}",
                f"{human_size(total).strip()} in its files, not counting "
                f"what is under the directories."]

    # -- the pane ----------------------------------------------------------
    def _window(self, stdscr, y: int, x: int, h: int, w: int):
        """A window over the pane, shared with the screen beneath it."""
        if self._win_box != (y, x, h, w):
            self._win = stdscr.subwin(h, w, y, x)
            self._win_box = (y, x, h, w)
        return self._win

    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if h < 3 or w < 8:
            return
        self.follow()
        if self.view is not None:
            self.view.draw(self._window(stdscr, y, x, h, w))
        else:
            self.put(stdscr, y, x, w, f" Quick view: {self.title} ",
                     theme.attr("keybar"))
            for row in range(1, h - 1):
                index = row - 1
                text = self.lines[index] if index < len(self.lines) else ""
                self.put(stdscr, y + row, x, w, f" {text}")
        self.put(stdscr, y + h - 1, x, w,
                 " Tab: in  Enter: full view  Ctrl-Q: off ",
                 theme.attr("keybar"))

    def handle_key(self, key: int | str):
        if key in (27, ord("q")):
            return False                       # give the pane back
        if key in (10, 13, curses.KEY_ENTER):
            if self.view is None and self.target() is not None:
                self.forced = True
                self.load()
            elif self.view is not None:
                found = self.target()
                assert found is not None
                full = viewer_for(self.ctx.other_fs, found[1])
                full.run(self.ctx.app.stdscr)
                curses.curs_set(0)
            return True
        view = self.view
        lines = getattr(view, "lines", None)
        if lines is None:
            return None                        # Tab, Ctrl-Q: the app's keys
        moves = {curses.KEY_DOWN: 1, curses.KEY_UP: -1,
                 curses.KEY_NPAGE: 10, curses.KEY_PPAGE: -10}
        if key in moves:
            view.top = max(0, min(view.top + moves[key], max(0, len(lines) - 1)))
        elif key == curses.KEY_HOME:
            view.top = 0
        elif key == curses.KEY_END:
            view.top = max(0, len(lines) - 1)
        else:
            return None
        return True
