"""Shared test harness: screen stand-ins, scripted dialogs and fake backends.

The guiding rule is that only the *terminal* and the *blocking dialogs* are
ever replaced.  Panels, filesystems, plug-ins and on-disk files stay real, so
assertions are about what the application actually did rather than about which
mock it happened to call.
"""

from __future__ import annotations

import curses
import os
import pty

import pytest

from meridian_commander import dialogs
from meridian_commander.filesystems import LocalFileSystem


def write(path: str, content: str, mtime: float | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


class _StubScreen:
    """A screen for handlers that never draw.  Only the size is ever asked."""

    def __init__(self, height: int = 24, width: int = 80) -> None:
        self._size = (height, width)

    def getmaxyx(self):
        return self._size


class _ScriptedDialogs:
    """Scripted stand-ins for the blocking dialogs.

    Each helper answers from its script and records what it was asked, so a
    test can assert both on the choice made and on the text the user saw.
    A menu answer given as a string picks the first option containing it,
    which keeps tests readable and independent of option ordering.
    """

    def __init__(self, monkeypatch, menu=(), prompt=(), confirm=()):
        self.menu_answers = list(menu)
        self.prompt_answers = list(prompt)
        self.confirm_answers = list(confirm)
        self.menus: list[tuple] = []
        self.prompts: list[str] = []
        self.messages: list[tuple] = []
        monkeypatch.setattr(dialogs, "menu", self._menu)
        monkeypatch.setattr(dialogs, "prompt", self._prompt)
        monkeypatch.setattr(dialogs, "confirm", self._confirm)
        monkeypatch.setattr(dialogs, "message", self._message)

    def _menu(self, stdscr, title, options):
        self.menus.append((title, list(options)))
        answer = self.menu_answers.pop(0)
        if isinstance(answer, str):
            return next(i for i, o in enumerate(options) if answer in o)
        return answer

    def _prompt(self, stdscr, title, label, default="", is_password=False):
        self.prompts.append(label)
        return self.prompt_answers.pop(0)

    def _confirm(self, stdscr, title, text, default_yes=False):
        self.messages.append((title, text, False))
        return self.confirm_answers.pop(0)

    def _message(self, stdscr, title, text, error=False):
        self.messages.append((title, text, error))

    @property
    def last_message(self) -> str:
        return self.messages[-1][1] if self.messages else ""

    @property
    def all_text(self) -> str:
        return "\n".join(text for _, text, _ in self.messages)


class _FakeRemoteBackend(LocalFileSystem):
    """A local backend wearing an SFTP name tag.

    Real enough to back a panel (it lists actual directories) while matching
    a remote preset, which is what the reuse and reconnect paths key on.
    """

    scheme = "sftp"

    def __init__(self, host="web1", username="deploy", port=22):
        super().__init__()
        self.host = host
        self.typed_username = username
        self.port = port
        self.key_filename = ""

    def label(self) -> str:
        return f"sftp://{self.typed_username}@{self.host}"


# -- real curses screens -------------------------------------------------------
#
# Drawing code is tested against a real curses screen on a pseudo-terminal
# rather than a stand-in window.  Only a real window reports the errors that
# overflowing it produces, so only a real window can catch a regression in the
# code that guards against them.

def with_curses_screen(rows: int, cols: int, fn):
    """Run ``fn(stdscr)`` on a real curses screen ``rows`` x ``cols``."""
    master, slave = pty.openpty()
    saved_out, saved_in = os.dup(1), os.dup(0)
    saved_term = os.environ.get("TERM")
    os.environ["TERM"] = "xterm"
    os.dup2(slave, 1)
    os.dup2(slave, 0)
    started = False
    try:
        try:
            stdscr = curses.initscr()
        except curses.error as exc:   # a machine with no terminfo database
            pytest.skip(f"curses screen unavailable: {exc}")
        started = True
        curses.start_color()
        curses.resizeterm(rows, cols)
        return fn(stdscr)
    finally:
        if started:
            curses.endwin()
        os.dup2(saved_out, 1)
        os.dup2(saved_in, 0)
        for fd in (saved_out, saved_in, master, slave):
            os.close(fd)
        if saved_term is None:
            os.environ.pop("TERM", None)
        else:
            os.environ["TERM"] = saved_term


class _ScriptedWindow:
    """A real curses window with its keystrokes scripted and draws recorded."""

    def __init__(self, win, keys, fail_draws: bool = False):
        self._win = win
        self._keys = list(keys)
        self._fail_draws = fail_draws
        self.drawn: list[tuple] = []

    def getch(self):
        return self._keys.pop(0)

    def addstr(self, *args):
        self.drawn.append(args)
        # Only the body rows are refused: the frame and title are drawn by
        # _box, which bounds its own writes and is not what is under test.
        if self._fail_draws and args[0] >= 2:
            raise curses.error("addwstr() returned ERR")
        return self._win.addstr(*args)

    def __getattr__(self, name):
        return getattr(self._win, name)


class _KeyScript:
    """A screen whose getch() replays a script, for full-screen key loops.

    Anything the code under test draws goes to the real window underneath, so
    the drawing is genuinely exercised; only the input is synthetic.
    """

    def __init__(self, win, keys, fail_draws: bool = False):
        self._win = win
        self._keys = list(keys)
        self._fail_draws = fail_draws
        self.drawn: list[tuple] = []

    def getch(self):
        if not self._keys:
            return 27          # nothing scripted left: behave as Escape
        return self._keys.pop(0)

    def addstr(self, *args):
        self.drawn.append(args)
        if self._fail_draws:
            raise curses.error("addwstr() returned ERR")
        # Errors from the real window are deliberately *not* swallowed here:
        # the code under test has its own guards, and hiding the error would
        # hide whether those guards work.
        return self._win.addstr(*args)

    def __getattr__(self, name):
        return getattr(self._win, name)

    @property
    def text(self) -> str:
        return "\n".join(str(a) for a in self.drawn)


def scripted_menu(monkeypatch, keys, fail_draws=False):
    """Make ``dialogs._center`` hand back a key-scripted real window.

    Returns a one-element dict that will hold the window once the dialog runs.
    """
    real_center = dialogs._center
    captured: dict = {}

    def scripted_center(stdscr, height, width):
        window = _ScriptedWindow(real_center(stdscr, height, width), keys,
                                 fail_draws)
        captured["window"] = window
        return window

    monkeypatch.setattr(dialogs, "_center", scripted_center)
    return captured


def run_menu(monkeypatch, rows, options, keys, fail_draws=False, title="Presets"):
    """Drive ``dialogs.menu`` on an ``rows``-high screen; return (choice, draws)."""
    captured = scripted_menu(monkeypatch, keys, fail_draws)
    choice = with_curses_screen(
        rows, 60, lambda stdscr: dialogs.menu(stdscr, title, options))
    return choice, captured["window"].drawn


def script_newwin(monkeypatch, keys, fail_draws: bool = False):
    """Patch ``curses.newwin`` so windows created replay ``keys``.

    Full-screen components build their own window and read keys from it.  The
    window handed back is real -- everything drawn goes through curses and is
    bounds-checked by it -- only the input is scripted.  Returns a dict that
    collects the windows as they are created.
    """
    real_newwin = curses.newwin
    captured: dict = {"windows": []}

    def fake_newwin(*args, **kwargs):
        window = _KeyScript(real_newwin(*args, **kwargs), keys, fail_draws)
        captured["windows"].append(window)
        captured["window"] = window
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    return captured
