"""The pane plugin API.

A *pane plugin* takes over one of the two panes: it draws inside that pane's
rectangle and receives the keys typed while its pane is active.  Through its
:class:`PluginContext` it can see and manipulate the **opposite pane** -- its
filesystem (local or remote), its current directory and its entries -- which is
what lets a plugin "do work" on whatever the user has open next to it.

Writing a plugin
----------------
Create a ``.py`` file in ``~/.config/martin-commander/plugins/`` (or add it to
``martin_commander/plugins/`` in the source tree) containing a subclass of
:class:`InputOutputPlugin` and implement ``process()``::

    from martin_commander.plugin_api import InputOutputPlugin

    class Shout(InputOutputPlugin):
        name = "Shout"
        description = "Uppercase whatever you type"
        prompt = "say> "

        def process(self, line):
            return [line.upper()]

That is a complete plugin: Martin Commander discovers it, lists it in the
plugin menu (``p`` or F11), and gives it the classic two-part layout -- a
scrolling **output area** on top and an **input line** at the bottom.  Each
time the user presses Enter, ``process()`` is called with the input; whatever
it returns (a string or a list of strings) is appended to the output, and
``self.print(...)`` can be used to emit output at any point during processing.

For full control of drawing and keys, subclass :class:`PanePlugin` directly
and implement ``draw()`` and ``handle_key()``.
"""

from __future__ import annotations

import curses
from dataclasses import dataclass


@dataclass
class PluginContext:
    """What a plugin can see of the application.

    ``own_panel`` is the pane the plugin is running in (its listing is hidden
    while the plugin owns the pane); ``other_panel`` is the opposite pane the
    plugin is meant to work with.
    """

    app: object
    own_panel: object
    other_panel: object

    # -- opposite pane ------------------------------------------------------
    @property
    def other_fs(self):
        """The opposite pane's filesystem backend (local, SFTP, SSH, FTP)."""
        return self.other_panel.fs

    @property
    def other_path(self) -> str:
        """The opposite pane's current directory."""
        return self.other_panel.path

    def other_entries(self):
        """The opposite pane's current entries (excluding '..')."""
        return [e for e in self.other_panel.entries if e.name != ".."]

    def other_selected(self):
        """Entries tagged in the opposite pane (or the cursor entry)."""
        return self.other_panel.selected_entries()

    def refresh_other(self) -> None:
        """Reload the opposite pane after the plugin changed its contents."""
        self.other_panel.refresh()

    # -- own pane (as it was before the plugin took over) --------------------
    @property
    def own_fs(self):
        return self.own_panel.fs

    @property
    def own_path(self) -> str:
        return self.own_panel.path

    # -- application services -------------------------------------------------
    def set_status(self, text: str) -> None:
        """Show ``text`` in the application's status line."""
        try:
            self.app._set_message(text)
        except Exception:
            pass


class PanePlugin:
    """Base class for pane plugins.

    Subclasses must provide ``name`` and ``description`` class attributes and
    implement :meth:`draw` and :meth:`handle_key`.
    """

    #: Shown in the plugin menu and the pane header.
    name = ""
    #: One line shown next to the name in the plugin menu.
    description = ""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.on_start()

    # -- lifecycle ----------------------------------------------------------
    def on_start(self) -> None:
        """Called once when the plugin opens.  Override for setup."""

    def on_exit(self) -> None:
        """Called when the plugin closes.  Override to release resources."""

    # -- interface the application drives -------------------------------------
    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        """Draw the plugin inside its pane's rectangle."""
        raise NotImplementedError

    def handle_key(self, key: int):
        """Handle a key pressed while the plugin's pane is active.

        Return ``True`` if the key was consumed, ``False`` to close the plugin
        and give the pane back to the file listing, or ``None`` to let the
        application handle the key (e.g. Tab to switch panes).
        """
        raise NotImplementedError

    # -- small drawing helper -------------------------------------------------
    @staticmethod
    def put(stdscr, y: int, x: int, w: int, text: str, attr: int = 0,
            pad: bool = True) -> None:
        """Write ``text`` clipped to ``w`` columns, ignoring edge errors."""
        if w <= 0:
            return
        s = text[:w]
        if pad:
            s = s.ljust(w)
        try:
            stdscr.addstr(y, x, s, attr)
        except curses.error:
            pass


class InputOutputPlugin(PanePlugin):
    """The classic plugin layout: output on top, an input line at the bottom.

    The user types into the input line and presses Enter; :meth:`process` is
    called with the text and its result is appended to the output area.  The
    output scrolls with PgUp/PgDn, previous inputs are recalled with the
    Up/Down arrows, and Esc closes the plugin.  Tab is passed back to the
    application so the user can still switch panes.
    """

    #: Prompt shown in front of the input line.
    prompt = "> "
    #: Greeting printed when the plugin opens (override or set to "").
    greeting = ""

    def on_start(self) -> None:
        self.output: list[str] = []
        self.scroll = 0            # 0 = pinned to the bottom
        self.buf: list[str] = []
        self.pos = 0
        self.history: list[str] = []
        self.hist_idx: int | None = None
        self.busy = False
        if self.greeting:
            self.print(self.greeting)

    # -- for subclasses -------------------------------------------------------
    def process(self, line: str):
        """Handle one submitted input line.

        May return a string or a list of strings to append to the output, call
        :meth:`print` directly, or both.  Exceptions are caught and shown in
        the output area.
        """
        raise NotImplementedError

    def print(self, text: str) -> None:
        """Append text (possibly multi-line) to the output area."""
        for line in str(text).splitlines() or [""]:
            self.output.append(line)
        self.scroll = 0

    # -- key handling ---------------------------------------------------------
    def handle_key(self, key: int):
        if key == 27:  # Esc closes the plugin
            self.on_exit()
            return False
        if key == 9:   # Tab: let the app switch panes
            return None
        if key in (10, 13, curses.KEY_ENTER):
            self._submit()
            return True
        if key == curses.KEY_PPAGE:
            self.scroll = min(self.scroll + 10, max(0, len(self.output) - 1))
            return True
        if key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 10)
            return True
        if key == curses.KEY_UP:
            self._history(-1)
            return True
        if key == curses.KEY_DOWN:
            self._history(1)
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.pos > 0:
                del self.buf[self.pos - 1]
                self.pos -= 1
            return True
        if key == curses.KEY_DC:
            if self.pos < len(self.buf):
                del self.buf[self.pos]
            return True
        if key == curses.KEY_LEFT:
            self.pos = max(0, self.pos - 1)
            return True
        if key == curses.KEY_RIGHT:
            self.pos = min(len(self.buf), self.pos + 1)
            return True
        if key == curses.KEY_HOME:
            self.pos = 0
            return True
        if key == curses.KEY_END:
            self.pos = len(self.buf)
            return True
        if key == 21:  # Ctrl-U clears the input line
            self.buf = []
            self.pos = 0
            return True
        if 32 <= key < 127:
            self.buf.insert(self.pos, chr(key))
            self.pos += 1
            return True
        if key > 127:
            try:
                self.buf.insert(self.pos, chr(key))
                self.pos += 1
            except ValueError:
                pass
            return True
        return True

    def _history(self, step: int) -> None:
        if not self.history:
            return
        if self.hist_idx is None:
            if step > 0:
                return
            self.hist_idx = len(self.history) - 1
        else:
            self.hist_idx += step
        if self.hist_idx < 0:
            self.hist_idx = 0
        if self.hist_idx >= len(self.history):
            self.hist_idx = None
            self.buf = []
            self.pos = 0
            return
        self.buf = list(self.history[self.hist_idx])
        self.pos = len(self.buf)

    def _submit(self) -> None:
        line = "".join(self.buf)
        self.buf = []
        self.pos = 0
        self.hist_idx = None
        if line.strip():
            self.history.append(line)
        self.print(self.prompt + line)
        self.busy = True
        try:
            result = self.process(line)
        except Exception as exc:
            self.print(f"error: {exc}")
            result = None
        finally:
            self.busy = False
        if result is None:
            return
        if isinstance(result, str):
            self.print(result)
        else:
            try:
                for item in result:
                    self.print(str(item))
            except TypeError:
                self.print(str(result))

    # -- drawing ----------------------------------------------------------------
    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        title = f" [plugin] {self.name} "
        self.put(stdscr, y, x, w, title, curses.A_REVERSE)

        out_h = h - 5          # header, separator, 2 input rows, hint bar
        if out_h < 1:
            return

        # Output area, pinned to the bottom minus the scroll offset.
        end = max(0, len(self.output) - self.scroll)
        start = max(0, end - out_h)
        visible = self.output[start:end]
        for row in range(out_h):
            text = visible[row] if row < len(visible) else ""
            self.put(stdscr, y + 1 + row, x, w, text)

        sep_y = y + 1 + out_h
        self.put(stdscr, sep_y, x, w, "-" * w, curses.A_DIM)

        # Input area: two rows, wrapping long input.
        avail = max(1, 2 * w - len(self.prompt) - 1)
        text = "".join(self.buf)
        shown = text[-avail:]
        full = self.prompt + shown
        line1, line2 = full[:w], full[w : 2 * w]
        self.put(stdscr, sep_y + 1, x, w, line1)
        self.put(stdscr, sep_y + 2, x, w, line2)
        # Draw the cursor as a reverse cell.
        cur = len(self.prompt) + min(self.pos, len(shown))
        cy, cx = (sep_y + 1, cur) if cur < w else (sep_y + 2, cur - w)
        if cx < w:
            ch = full[cur] if cur < len(full) else " "
            self.put(stdscr, cy, x + cx, 1, ch, curses.A_REVERSE, pad=False)

        status = " working... " if self.busy else \
            " Enter run   Esc close   PgUp/PgDn scroll   Up/Down history "
        self.put(stdscr, y + h - 1, x, w, status, curses.A_REVERSE)
