"""The pane plugin API.

A *pane plugin* takes over one of the two panes: it draws inside that pane's
rectangle and receives the keys typed while its pane is active.  Through its
:class:`PluginContext` it can see and manipulate the **opposite pane** -- its
filesystem (local or remote), its current directory and its entries -- which is
what lets a plugin "do work" on whatever the user has open next to it.

Writing a plugin
----------------
Create a ``.py`` file in ``~/.config/meridian-commander/plugins/`` (or add it to
``meridian_commander/plugins/`` in the source tree) containing a subclass of
:class:`InputOutputPlugin` and implement ``process()``::

    from meridian_commander.plugin_api import InputOutputPlugin

    class Shout(InputOutputPlugin):
        name = "Shout"
        description = "Uppercase whatever you type"
        prompt = "say> "

        def process(self, line):
            return [line.upper()]

That is a complete plugin: Meridian Commander discovers it, lists it in the
plugin menu (``p`` or F11), and gives it the classic two-part layout -- a
scrolling **output area** on top and an **input line** at the bottom.  Each
time the user presses Enter, ``process()`` is called with the input; whatever
it returns (a string or a list of strings) is appended to the output, and
``self.print(...)`` can be used to emit output at any point during processing.

A plug-in whose input is a *vocabulary* rather than free text should say so,
by listing its :class:`Command` s -- then ``F2`` offers them for a keystroke
each and the user need not remember how this particular plug-in spells
``log``::

    class Shout(InputOutputPlugin):
        commands = (
            Command("loud", "shout the line back"),
            Command("file", "shout a file's name", arg="path"),
        )

The menu chains: a command with ``arg="path"`` follows up with the other
pane's listing, ``choices=(...)`` with a fixed list, ``arg="options"`` with
whatever :meth:`InputOutputPlugin.command_options` returns, and ``arg="text"``
simply primes the input line for the part only the user can supply.  Typing
still works exactly as before -- the menu builds the same line and submits it.

For full control of drawing and keys, subclass :class:`PanePlugin` directly
and implement ``draw()`` and ``handle_key()``.
"""

from __future__ import annotations

import curses

from dataclasses import dataclass, field

from . import theme


@dataclass(frozen=True)
class Command:
    """One entry in an :class:`InputOutputPlugin`'s ``F2`` command menu.

    A plug-in that lists its commands this way gets them for a keystroke
    instead of for a typed word, which is the difference between "F2, l" and
    remembering that this particular plug-in spells it ``log``.

    ``arg`` says what the command still needs once it is chosen:

    ``None``      run it there and then;
    ``"text"``    put ``verb `` on the input line and let the user type the
                  rest (a commit message, a pattern -- something only they
                  know);
    ``"path"``    offer the other pane's entries as a second menu, with an
                  "everything" row for the commands that also work bare;
    ``"options"`` offer whatever :meth:`InputOutputPlugin.command_options`
                  returns for this command -- the table's column names, say.

    ``choices`` is the fixed-list version of ``"options"``: give it here when
    the answers are known up front (``lower|upper|title``).
    """

    verb: str
    help: str = ""
    arg: str | None = None
    choices: tuple[str, ...] = field(default_factory=tuple)
    #: Menu text, when the verb alone is not it (an Enter-only command has
    #: no verb to show).
    label: str = ""
    #: Whether a ``"path"`` picker offers its "everything" row -- false for
    #: the commands that insist on a path (``git add``, ``tail``).
    allow_bare: bool = True

    @property
    def menu_label(self) -> str:
        return self.label or self.verb


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

    def focus_other(self) -> None:
        """Move keyboard focus to the opposite pane (the plugin keeps running).

        Lets a plugin that consumes Tab (like the terminal, where Tab must
        reach the shell for completion) still offer a switch-pane key.
        """
        try:
            self.app.active = self.other_panel
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
    def put(stdscr, y: int, x: int, w: int, text: str, attr: int | None = None,
            pad: bool = True) -> None:
        """Write ``text`` clipped to ``w`` columns, ignoring edge errors.

        With no attribute given the pane's own colours are used, so a plug-in
        that just prints lines lands on the same blue field as a directory
        listing rather than on whatever the terminal's default happens to be.
        """
        if w <= 0:
            return
        s = text[:w]
        if pad:
            s = s.ljust(w)
        try:
            stdscr.addstr(y, x, s,
                          theme.attr("panel") if attr is None else attr)
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

    @property
    def greeting(self) -> str:
        """Greeting printed when the plugin opens; "" for none.

        A property rather than a plain attribute because nearly every
        plug-in's greeting depends on what the panes hold when it opens, and
        a subclass cannot narrow a writeable attribute to a computed one.
        """
        return ""

    #: Commands offered by the ``F2`` menu -- a tuple of :class:`Command`.
    #: Left empty, the plug-in is typing-only and ``F2`` does nothing.
    commands: tuple[Command, ...] = ()

    #: Row offered by a path picker for the commands that also run bare.
    ANY_PATH = "(everything -- no path)"

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

    def command_options(self, command: Command) -> list[str] | None:
        """Answers for a ``arg="options"`` command, or ``None`` if there are
        none to offer.  Override in plug-ins whose choices are discovered
        rather than fixed -- a table's column names, for instance."""
        return None

    # -- the command menu ------------------------------------------------------
    def _menu(self, title: str, options: list[str]):
        """Show a chooser and return the chosen index, or ``None``."""
        from . import dialogs

        stdscr = getattr(self.ctx.app, "stdscr", None)
        if stdscr is None:          # no screen to draw on (headless/tests)
            return None
        return dialogs.menu(stdscr, title, options,
                            dialogs.accelerators(options))

    def command_menu(self) -> None:
        """Pick a command by keystroke and run it (``F2``).

        Each step is its own small menu: the command, then whatever it still
        needs.  Cancelling any of them leaves the input line untouched, so
        the menu is safe to open just to see what a plug-in can do.
        """
        if not self.commands:
            return
        commands = list(self.commands)
        width = max(len(c.menu_label) for c in commands)
        labels = [f"{c.menu_label:<{width}}  {c.help}".rstrip()
                  for c in commands]
        index = self._menu(f"{self.name}: commands", labels)
        if index is None:
            return
        command = commands[index]

        if command.arg == "text":
            # Only the user knows the rest; hand them a primed input line.
            self.buf = list(f"{command.verb} " if command.verb else "")
            self.pos = len(self.buf)
            return

        argument = self._command_argument(command)
        if argument is None:
            return                  # cancelled at the second menu
        self._run(f"{command.verb} {argument}".strip() if argument
                  else command.verb)

    def _command_argument(self, command: Command):
        """The second menu for a command that needs one, or ``None``."""
        if command.choices:
            options = list(command.choices)
        elif command.arg == "path":
            options = [e.name for e in self.ctx.other_entries()]
            if not options:
                self.print("(nothing in the other pane to choose from)")
                return None
            if command.allow_bare:
                options = [self.ANY_PATH] + options
        elif command.arg == "options":
            options = self.command_options(command) or []
            if not options:
                self.print(f"(nothing to choose for '{command.verb}')")
                return None
        else:
            return ""               # the command needs no argument

        index = self._menu(f"{command.verb}:", options)
        if index is None:
            return None
        chosen = options[index]
        return "" if chosen == self.ANY_PATH else chosen

    def _run(self, text: str) -> None:
        """Put ``text`` on the input line and submit it, as if typed."""
        self.buf = list(text)
        self.pos = len(self.buf)
        self._submit()

    # -- key handling ---------------------------------------------------------
    def handle_key(self, key: int):
        if key == 27:  # Esc closes the plugin
            self.on_exit()
            return False
        if key == 9:   # Tab: let the app switch panes
            return None
        if key == curses.KEY_F2 and self.commands:
            self.command_menu()
            return True
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
        self.put(stdscr, y, x, w, title, theme.attr("keybar"))

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
        self.put(stdscr, sep_y, x, w, theme.glyph("h1") * w,
                 theme.attr("framenc"))

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
            self.put(stdscr, cy, x + cx, 1, ch, theme.attr("inputcursor"),
                     pad=False)

        if self.busy:
            status = " working... "
        elif self.commands:
            status = (" F2 commands   Enter run   Esc close"
                      "   PgUp/PgDn scroll   Up/Down history ")
        else:
            status = (" Enter run   Esc close   PgUp/PgDn scroll"
                      "   Up/Down history ")
        self.put(stdscr, y + h - 1, x, w, status, theme.attr("keybar"))
