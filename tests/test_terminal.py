# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The in-pane terminal: the plug-in that runs a shell on a pty or an SSH
channel, sends it keys and draws its screen.  The emulator itself is
``test_vt.py``."""

from __future__ import annotations

import curses
import errno
import os
import time

import pytest

from meridian_commander import theme, vt
from meridian_commander.plugin_api import PluginContext
from meridian_commander.plugins import terminal
from meridian_commander.plugins.terminal import (
    MENU_KEY, Colours, TerminalPlugin, encode_key, nearest16,
)

from support import _FakeRemoteBackend, terminal_text, with_curses_screen


# -- keys ----------------------------------------------------------------------

F = curses.KEY_F0


@pytest.mark.parametrize("key, normal, application", [
    (curses.KEY_UP, b"\x1b[A", b"\x1bOA"),
    (curses.KEY_DOWN, b"\x1b[B", b"\x1bOB"),
    (curses.KEY_RIGHT, b"\x1b[C", b"\x1bOC"),
    (curses.KEY_LEFT, b"\x1b[D", b"\x1bOD"),
    (curses.KEY_HOME, b"\x1b[H", b"\x1bOH"),
    (curses.KEY_END, b"\x1b[F", b"\x1bOF"),
    (curses.KEY_A1, b"\x1b[H", b"\x1bOH"),
    (curses.KEY_C1, b"\x1b[F", b"\x1bOF"),
    (curses.KEY_B2, b"\x1b[E", b"\x1bOE"),
])
def test_cursor_keys_follow_the_programs_mode(key, normal, application):
    """vim, less and every curses program switch to application cursor keys
    (DECCKM) on start-up and expect ESC O then."""
    assert encode_key(key) == normal
    assert encode_key(key, app_cursor=True) == application


@pytest.mark.parametrize("key, expected", [
    (curses.KEY_IC, b"\x1b[2~"), (curses.KEY_DC, b"\x1b[3~"),
    (curses.KEY_PPAGE, b"\x1b[5~"), (curses.KEY_NPAGE, b"\x1b[6~"),
    (curses.KEY_A3, b"\x1b[5~"), (curses.KEY_C3, b"\x1b[6~"),
    (F + 1, b"\x1bOP"), (F + 2, b"\x1bOQ"), (F + 3, b"\x1bOR"), (F + 4, b"\x1bOS"),
    (F + 5, b"\x1b[15~"), (F + 6, b"\x1b[17~"), (F + 7, b"\x1b[18~"),
    (F + 8, b"\x1b[19~"), (F + 9, b"\x1b[20~"), (F + 10, b"\x1b[21~"),
    (F + 11, b"\x1b[23~"), (F + 12, b"\x1b[24~"),
    (F + 13, b"\x1b[1;2P"), (F + 16, b"\x1b[1;2S"),
    (F + 17, b"\x1b[15;2~"), (F + 24, b"\x1b[24;2~"),
    (curses.KEY_BTAB, b"\x1b[Z"),
    (curses.KEY_SR, b"\x1b[1;2A"), (curses.KEY_SF, b"\x1b[1;2B"),
    (curses.KEY_SRIGHT, b"\x1b[1;2C"), (curses.KEY_SLEFT, b"\x1b[1;2D"),
    (curses.KEY_SHOME, b"\x1b[1;2H"), (curses.KEY_SEND, b"\x1b[1;2F"),
    (curses.KEY_SIC, b"\x1b[2;2~"), (curses.KEY_SDC, b"\x1b[3;2~"),
    (curses.KEY_SPREVIOUS, b"\x1b[5;2~"), (curses.KEY_SNEXT, b"\x1b[6;2~"),
    (curses.KEY_BACKSPACE, b"\x7f"), (127, b"\x7f"), (8, b"\x7f"),
    (10, b"\r"), (13, b"\r"), (curses.KEY_ENTER, b"\r"),
    (3, b"\x03"), (9, b"\t"), (27, b"\x1b"), (0, b"\x00"),
    (ord("a"), b"a"), (ord("é"), "é".encode()),
])
def test_key_encoding(key, expected):
    """Every key reaches the program as xterm would send it: F1-F12, PgUp,
    PgDn and Shift-Tab included, which full-screen programs need."""
    assert encode_key(key) == expected


def test_the_keypad_enter_follows_the_keypad_mode():
    assert encode_key(curses.KEY_ENTER, app_keypad=True) == b"\x1bOM"


@pytest.mark.parametrize("key", [
    curses.KEY_RESIZE, curses.KEY_MOUSE, 0x110000, 0xD800,
])
def test_key_codes_that_are_not_keys_send_nothing(key):
    """An unbound key code was once encoded as a character and sent to the
    shell, where the next Enter would have run it."""
    assert encode_key(key) == b""


@pytest.mark.parametrize("key", ["日", "ā", "€"])
def test_a_character_no_number_can_carry_reaches_the_shell(key):
    # From U+0100 up read_key hands over the string itself; the control-key
    # test in front of this one is an ordering comparison, which a str would
    # have raised on rather than fallen past.
    assert encode_key(key) == key.encode("utf-8")


# -- a plug-in on a fake SSH channel --------------------------------------------

class _FakeChannel:
    def __init__(self, reads=(), closed=False, exit_ready=False):
        self.reads = list(reads)
        self.sent = b""
        self.closed = closed
        self._exit_ready = exit_ready
        self.pty = None
        self.resized = None
        self.shell = False
        self.blocking = None

    def get_pty(self, term, width, height):
        self.pty = (term, width, height)

    def invoke_shell(self):
        self.shell = True

    def setblocking(self, flag):
        self.blocking = flag

    def sendall(self, data):
        self.sent += data

    def recv_ready(self):
        return bool(self.reads)

    def recv(self, size):
        return self.reads.pop(0)

    def exit_status_ready(self):
        return self._exit_ready

    def resize_pty(self, width, height):
        self.resized = (width, height)

    def close(self):
        self.closed = True


class _FakeTransport:
    def __init__(self, channel, active=True):
        self.channel = channel
        self._active = active

    def is_active(self):
        return self._active

    def open_session(self):
        return self.channel


class _FakeSSHClient:
    def __init__(self, transport):
        self._transport = transport

    def get_transport(self):
        return self._transport


def _remote_ctx(app, channel, transport=None, client=...):
    backend = _FakeRemoteBackend()
    if client is ...:
        client = _FakeSSHClient(transport or _FakeTransport(channel))
    backend._client = client
    app.left.fs = backend
    return PluginContext(app=app, own_panel=app.left, other_panel=app.right)


@pytest.fixture
def remote(app):
    """A terminal on a fake SSH channel, with the keystrokes it sent so far
    forgotten (the opening cd)."""
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    channel.sent = b""
    yield plugin, channel
    plugin.on_exit()


def _history(plugin, count=50):
    plugin.screen.feed(b"".join(b"line %d\r\n" % i for i in range(count)))


# -- the remote shell ----------------------------------------------------------

def test_a_remote_terminal_opens_a_shell_and_changes_directory(app, tmp_path):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    assert channel.shell is True
    assert channel.pty == ("xterm-256color", 80, 24)
    assert channel.blocking is False
    assert b"cd " in channel.sent
    assert str(tmp_path / "left").encode() in channel.sent
    plugin.on_exit()
    assert channel.closed is True


def test_a_remote_pane_with_no_connection_is_refused(app):
    with pytest.raises(RuntimeError, match="no SSH connection"):
        TerminalPlugin(_remote_ctx(app, None, client=None))


def test_a_remote_pane_whose_connection_died_is_refused(app):
    channel = _FakeChannel()
    with pytest.raises(RuntimeError, match="no longer active"):
        TerminalPlugin(_remote_ctx(app, channel,
                                   transport=_FakeTransport(channel, active=False)))


def test_a_remote_pane_with_no_transport_is_refused(app):
    client = _FakeSSHClient(None)
    with pytest.raises(RuntimeError, match="no longer active"):
        TerminalPlugin(_remote_ctx(app, None, client=client))


def test_an_ftp_pane_has_no_shell(app):
    class _Ftpish(type(app.left.fs)):
        scheme = "ftp"

    app.left.fs = _Ftpish()
    ctx = PluginContext(app=app, own_panel=app.left, other_panel=app.right)
    with pytest.raises(RuntimeError, match="FTP has no shell"):
        TerminalPlugin(ctx)


def test_a_remote_terminal_pumps_output(remote):
    plugin, channel = remote
    channel.reads = [b"\x1b[31mhello\x1b[0m\r\n", b""]
    plugin.tick()
    assert plugin.screen.display()[0] == "hello"
    assert plugin.screen.lines[0].attrs[0] == (1, vt.DEFAULT, 0)


def test_the_programs_questions_are_answered_over_the_channel(remote):
    plugin, channel = remote
    channel.reads = [b"\x1b[3;4H\x1b[6n\x1b[c"]
    plugin.tick()
    assert channel.sent == b"\x1b[3;4R\x1b[?62;22c"


def test_a_flood_of_remote_output_is_taken_a_slice_at_a_time(remote,
                                                              monkeypatch):
    plugin, channel = remote
    monkeypatch.setattr(terminal, "FEED_SECONDS", 0)
    channel.recv_ready = lambda: True
    channel.recv = lambda size: b"x" * 100
    plugin.tick()                         # returns, though output never ends
    assert plugin.screen.display()[0] == ""      # nothing fed in no time


def test_a_remote_terminal_notices_the_shell_exiting(app):
    channel = _FakeChannel(reads=[b"bye\r\n"], exit_ready=True)
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    plugin.tick()
    assert plugin.done is True
    assert plugin.screen.display()[0] == "bye"
    plugin.on_exit()


def test_a_remote_terminal_notices_a_closed_channel(remote):
    plugin, channel = remote
    channel.closed = True
    plugin.tick()
    assert plugin.done is True


def test_a_remote_drain_that_fails_still_finishes(app):
    channel = _FakeChannel(exit_ready=True)
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    calls = []

    def explode():
        calls.append(1)
        if len(calls) > 1:
            raise OSError("connection reset")
        return False

    channel.recv_ready = explode
    plugin.tick()
    assert plugin.done is True
    plugin.on_exit()


def test_a_remote_drain_feeds_what_was_still_buffered(app):
    channel = _FakeChannel(reads=[b"last words\r\n"], exit_ready=True)
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    ready = iter([False, True, False])
    channel.recv_ready = lambda: next(ready, False)
    plugin.tick()
    assert plugin.done is True
    assert plugin.screen.display()[0] == "last words"
    plugin.on_exit()


def test_a_remote_send_that_fails_ends_the_terminal(remote):
    plugin, channel = remote

    def explode(data):
        raise OSError("broken pipe")

    channel.sendall = explode
    plugin.handle_key(ord("x"))
    assert plugin.done is True


def test_a_remote_channel_that_fails_to_close_is_ignored(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    channel.close = lambda: 1 / 0
    plugin.on_exit()
    assert plugin._chan is None


def test_keys_reach_the_remote_shell(remote):
    plugin, channel = remote
    plugin.handle_key(ord("l"))
    plugin.handle_key(10)
    plugin.handle_key(curses.KEY_F0 + 10)       # F10 is the program's now
    plugin.handle_key(curses.KEY_PPAGE)         # and so are PgUp/PgDn
    assert channel.sent == b"l\r\x1b[21~\x1b[5~"
    assert plugin.done is False


def test_arrow_keys_follow_application_cursor_mode(remote):
    plugin, channel = remote
    plugin.screen.feed(b"\x1b[?1h")
    plugin.handle_key(curses.KEY_UP)
    plugin.screen.feed(b"\x1b[?1l")
    plugin.handle_key(curses.KEY_UP)
    assert channel.sent == b"\x1bOA\x1b[A"


def test_a_key_with_nothing_to_send_sends_nothing(remote):
    plugin, channel = remote
    assert plugin.handle_key(curses.KEY_RESIZE) is True
    assert channel.sent == b""


# -- the Ctrl-] menu -------------------------------------------------------------

def test_ctrl_bracket_then_tab_switches_pane(remote, app):
    plugin, channel = remote
    assert plugin.handle_key(MENU_KEY) is True
    assert plugin.menu is True
    assert plugin.handle_key(9) is True
    assert app.active is app.right
    assert plugin.menu is False
    assert channel.sent == b""


@pytest.mark.parametrize("key", [ord("q"), ord("Q")])
def test_ctrl_bracket_then_q_closes(remote, key):
    plugin, channel = remote
    plugin.handle_key(MENU_KEY)
    assert plugin.handle_key(key) is False
    assert channel.closed is True


@pytest.mark.parametrize("key", [curses.KEY_PPAGE, ord("[")])
def test_ctrl_bracket_then_pgup_scrolls_back(remote, key):
    plugin, channel = remote
    _history(plugin)
    plugin.handle_key(MENU_KEY)
    plugin.handle_key(key)
    assert plugin.scroll == plugin._page()
    assert channel.sent == b""


def test_ctrl_bracket_twice_sends_one_to_the_program(remote):
    plugin, channel = remote
    plugin.handle_key(MENU_KEY)
    plugin.handle_key(MENU_KEY)
    assert channel.sent == b"\x1d"
    assert plugin.menu is False


@pytest.mark.parametrize("key", [27, ord("x"), curses.KEY_F0 + 10])
def test_any_other_key_just_closes_the_menu(remote, key):
    plugin, channel = remote
    plugin.handle_key(MENU_KEY)
    assert plugin.handle_key(key) is True
    assert plugin.menu is False
    assert channel.sent == b""
    assert plugin.done is False


# -- scrolling back ------------------------------------------------------------

def test_scrolling_back_with_the_keys(remote):
    plugin, channel = remote
    _history(plugin, 200)
    page = plugin._page()
    plugin.handle_key(MENU_KEY)
    plugin.handle_key(curses.KEY_PPAGE)
    plugin.handle_key(curses.KEY_PPAGE)
    assert plugin.scroll == 2 * page
    plugin.handle_key(curses.KEY_NPAGE)
    assert plugin.scroll == page
    plugin.handle_key(curses.KEY_UP)
    assert plugin.scroll == page + 1
    plugin.handle_key(curses.KEY_DOWN)
    assert plugin.scroll == page
    plugin.handle_key(curses.KEY_HOME)
    assert plugin.scroll == len(plugin.screen.scrollback)
    assert channel.sent == b""


def test_any_other_key_returns_to_the_live_screen_and_is_not_sent(remote):
    plugin, channel = remote
    _history(plugin)
    plugin.wheel(5)
    assert plugin.handle_key(10) is True
    assert plugin.scroll == 0
    assert channel.sent == b""


def test_paging_forward_past_the_bottom_is_the_live_screen(remote):
    plugin, _ = remote
    _history(plugin)
    plugin.wheel(3)
    plugin.handle_key(curses.KEY_NPAGE)
    assert plugin.scroll == 0
    plugin.handle_key(ord("x"))           # live again: keys reach the shell
    assert plugin.scroll == 0


def test_the_menu_opens_from_the_history_too(remote, app):
    plugin, _ = remote
    _history(plugin)
    plugin.wheel(5)
    plugin.handle_key(MENU_KEY)
    assert plugin.menu is True
    plugin.handle_key(9)
    assert app.active is app.right
    assert plugin.scroll == 5


def test_the_wheel_scrolls_and_is_bounded(remote):
    plugin, _ = remote
    _history(plugin, 40)
    plugin.wheel(3)
    assert plugin.scroll == 3
    plugin.wheel(1000)
    assert plugin.scroll == len(plugin.screen.scrollback)
    plugin.wheel(-1000)
    assert plugin.scroll == 0


def test_the_wheel_does_nothing_once_the_shell_has_gone(remote):
    plugin, _ = remote
    _history(plugin)
    plugin._finish()
    plugin.wheel(3)
    assert plugin.scroll == 0


def test_the_alternate_screen_has_no_history_to_scroll(remote):
    plugin, _ = remote
    _history(plugin)
    plugin.screen.feed(b"\x1b[?1049h")
    plugin.wheel(5)
    assert plugin.scroll == 0


def test_a_scrolled_back_view_stays_put_as_output_arrives(remote):
    plugin, channel = remote
    _history(plugin)
    plugin.wheel(5)
    channel.reads = [b"more\r\n" * 3]
    plugin.tick()
    assert plugin.scroll == 8


# -- busy polling --------------------------------------------------------------

def test_the_terminal_asks_for_quick_polls_only_while_busy(remote, monkeypatch):
    plugin, channel = remote
    assert plugin.poll_ms is None
    plugin.handle_key(ord("x"))
    assert plugin.poll_ms == terminal.FAST_POLL_MS
    monkeypatch.setattr(terminal, "BUSY_SECONDS", 0)
    assert plugin.poll_ms is None
    monkeypatch.undo()
    channel.reads = [b"output"]
    plugin.tick()
    assert plugin.poll_ms == terminal.FAST_POLL_MS
    plugin._finish()
    assert plugin.poll_ms is None


# -- the local shell -----------------------------------------------------------

@pytest.fixture
def local_ctx(app):
    return PluginContext(app=app, own_panel=app.left, other_panel=app.right)


@pytest.fixture
def local(local_ctx, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    yield plugin
    plugin.on_exit()


def _wait_for(plugin, predicate, tries=500):
    """Pump the plug-in until ``predicate`` holds (the shell is a real pty)."""
    for _ in range(tries):
        plugin.tick()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _type(plugin, text):
    for ch in text:
        plugin.handle_key(ord(ch))
    plugin.handle_key(10)


def _shown(plugin) -> str:
    return terminal_text(plugin.screen)


def test_a_local_terminal_runs_a_real_shell(local):
    _type(local, "echo marker-$((6*7))")
    assert _wait_for(local, lambda: "marker-42" in _shown(local))


def test_a_local_terminal_starts_in_the_panes_directory(local, tmp_path):
    _type(local, "pwd")
    assert _wait_for(local, lambda: str(tmp_path / "left") in _shown(local))


def test_the_shell_is_told_the_terminal_it_has(local, monkeypatch):
    _type(local, 'echo "term=$TERM"')
    assert _wait_for(local, lambda: "term=xterm-256color" in _shown(local))


def test_escape_sequences_from_a_real_shell_reach_the_grid(local):
    _type(local, r"printf '\033[2J\033[3;5H\033[1;32mgreen\033[0m'")

    def drawn():
        line = local.screen.lines[2]
        return line.text().startswith("    green")

    assert _wait_for(local, drawn)
    assert local.screen.lines[2].attrs[4] == (2, vt.DEFAULT, vt.BOLD)


def test_a_resize_reaches_the_shell(local):
    _render(local, h=12, w=50)
    _type(local, "stty size")
    assert _wait_for(local, lambda: "10 50" in _shown(local))


def test_the_programs_questions_are_answered_on_the_pty(local):
    # The shell prints a cursor-position request; the answer arrives as
    # input, which the terminal driver echoes back as ^[[1;1R.
    _type(local, r"printf '\033[H\033[6n'; sleep 1")
    assert _wait_for(local, lambda: "^[[1;1R" in _shown(local))


def test_a_local_terminal_notices_the_shell_exiting(local):
    _type(local, "exit")
    assert _wait_for(local, lambda: local.done)
    assert "process exited" in local.status
    assert local.handle_key(ord("x")) is False


def test_a_local_terminal_in_a_directory_that_has_gone(local_ctx, monkeypatch,
                                                       tmp_path):
    monkeypatch.setenv("SHELL", "/bin/sh")
    local_ctx.own_panel.path = str(tmp_path / "vanished")
    plugin = TerminalPlugin(local_ctx)
    try:
        _type(plugin, "echo alive")
        assert _wait_for(plugin, lambda: "alive" in _shown(plugin))
    finally:
        plugin.on_exit()


def test_closing_a_local_terminal_twice_is_harmless(local):
    local.on_exit()
    local.on_exit()
    assert local._fd is None and local._pid is None


def test_a_read_error_that_is_not_would_block_ends_the_terminal(local,
                                                                monkeypatch):
    def broken(fd, size):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(os, "read", broken)
    local.tick()
    assert local.done is True


def test_a_write_that_fails_ends_the_terminal(local, monkeypatch):
    monkeypatch.setattr(os, "write", lambda fd, data: 1 / 0)
    local.handle_key(ord("x"))
    assert local.done is True


def test_a_program_that_is_not_reading_does_not_end_the_terminal(local,
                                                                  monkeypatch):
    """A full input buffer: what is typed is dropped, the session lives."""
    def full(fd, data):
        raise BlockingIOError(errno.EAGAIN, "resource temporarily unavailable")

    monkeypatch.setattr(os, "write", full)
    local.handle_key(ord("x"))
    assert local.done is False


def test_a_short_write_is_finished_off(local, monkeypatch):
    written = []

    def one_byte(fd, data):
        written.append(data[:1])
        return 1

    monkeypatch.setattr(os, "write", one_byte)
    local.handle_key(curses.KEY_UP)
    assert b"".join(written) == b"\x1b[A"


def test_a_flood_of_local_output_is_taken_a_slice_at_a_time(local,
                                                            monkeypatch):
    """cat of a huge file: tick must return, so Ctrl-C can get through."""
    monkeypatch.setattr(terminal, "FEED_SECONDS", 0)
    reads = []

    def endless(fd, size):
        reads.append(1)
        return b"x" * size

    monkeypatch.setattr(os, "read", endless)
    monkeypatch.setattr(os, "waitpid", lambda pid, flags: (0, 0))
    local.tick()
    assert len(reads) == 1


def test_a_vanished_child_is_not_an_error(local, monkeypatch):
    def reaped(pid, flags):
        raise ChildProcessError("no child processes")

    monkeypatch.setattr(os, "waitpid", reaped)
    local.tick()                        # must not raise
    monkeypatch.undo()


def test_a_reaped_child_ends_the_terminal(local, monkeypatch):
    """The other way a shell's exit arrives: waitpid notices it first.

    Two paths end the terminal and which one wins is a race; stubbing both
    syscalls picks the winner deliberately, so the branch is covered on
    purpose rather than by luck.
    """
    def would_block(fd, size):
        raise OSError(errno.EAGAIN, "resource temporarily unavailable")

    monkeypatch.setattr(os, "read", would_block)
    monkeypatch.setattr(os, "waitpid", lambda pid, flags: (pid, 0))
    local.tick()
    assert local.done is True
    assert local._pid is None
    assert "process exited" in local.status


def test_tick_does_nothing_once_finished(local):
    local._finish()
    local._finish()                     # already done: status unchanged
    local.tick()
    assert local.status.count("process exited") == 1


def test_a_local_resize_that_fails_is_ignored(local):
    local.on_exit()
    local._fd = -1                       # a closed descriptor
    local._resize(10, 20)                # must not raise
    local._fd = None


def test_closing_a_descriptor_that_has_already_gone_is_ignored(local):
    local.on_exit()
    local._fd = -1                       # never a valid descriptor
    local.on_exit()                      # must not raise
    assert local._fd is None


def test_signalling_a_child_that_has_already_gone_is_ignored(local,
                                                             monkeypatch):
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(
        ProcessLookupError("no such process")))
    local.on_exit()                      # must not raise
    assert local._pid is None
    monkeypatch.undo()


# -- drawing -------------------------------------------------------------------

def _render(plugin, h=10, w=40, colours=None, inspect=None):
    """Draw the plug-in on a real curses screen; the rows as text, and
    whatever ``inspect(stdscr)`` makes of the result."""
    def draw(stdscr):
        plugin.draw(stdscr, 0, 0, h, w)
        text = "\n".join(stdscr.instr(row, 0, w).decode() for row in range(h))
        return text, (inspect(stdscr) if inspect else None)

    return with_curses_screen(h + 2, w + 2, draw, colours=colours)


def _attr_at(y, x):
    return lambda stdscr: stdscr.inch(y, x) & ~0xFF


def test_drawing_shows_the_header_the_screen_and_the_footer(remote):
    plugin, _ = remote
    plugin.screen.feed(b"first\r\nsecond\r\n")
    text, _ = _render(plugin)
    lines = text.split("\n")
    assert "[terminal]" in lines[0]
    assert lines[1].startswith("first")
    assert lines[2].startswith("second")
    assert lines[-1] == " Ctrl-]: Tab pane  q close  PgUp scroll "


def test_drawing_resizes_the_screen_and_the_remote_pty(remote):
    plugin, channel = remote
    _render(plugin, h=12, w=50)
    assert (plugin.screen.rows, plugin.screen.cols) == (10, 50)
    assert channel.resized == (50, 10)


def test_a_resize_the_channel_refuses_is_ignored(remote):
    plugin, channel = remote
    channel.resize_pty = lambda width, height: 1 / 0
    _render(plugin, h=14, w=52)              # must not raise


def test_drawing_a_pane_too_short_for_any_screen(remote):
    plugin, _ = remote

    def draw(stdscr):
        plugin.draw(stdscr, 0, 0, 2, 40)     # h - 2 leaves no rows
        return True

    assert with_curses_screen(10, 42, draw) is True


def test_the_footer_shows_the_menu_the_history_and_the_exit(remote):
    plugin, _ = remote
    plugin.handle_key(MENU_KEY)
    assert "Tab: pane  q: close  PgUp: scroll  Esc" in _render(plugin)[0]
    plugin.handle_key(27)
    _history(plugin)
    plugin.wheel(4)
    assert "scrollback, 4 lines up  Esc: live" in _render(plugin)[0]
    plugin._finish()
    assert "process exited" in _render(plugin, w=60)[0]


def test_scrolling_back_shows_the_history(remote):
    plugin, _ = remote
    _render(plugin)                          # 8 rows by 40
    _history(plugin, 30)
    plugin.wheel(3)
    lines = _render(plugin)[0].split("\n")
    assert lines[1].startswith("line 20")
    assert lines[8].startswith("line 27")


def test_the_history_follows_a_shrinking_screen_back_to_its_bounds(remote):
    plugin, _ = remote
    _render(plugin)
    _history(plugin, 30)
    plugin.wheel(10)
    plugin.screen.feed(b"\x1b[3J")           # the history is cleared
    _render(plugin)
    assert plugin.scroll == 0


def test_lines_from_a_wider_or_narrower_screen_are_cut_or_padded(remote):
    plugin, _ = remote
    _render(plugin, w=20)
    plugin.screen.feed("abcdefghijklmnopqr日\r\n".encode())
    plugin.screen.feed(b"short\r\n" + b"x\r\n" * 10)
    _render(plugin, w=40)                     # history is 20 wide now
    plugin.wheel(len(plugin.screen.scrollback))
    text, _ = _render(plugin, w=19)
    lines = text.split("\n")
    assert lines[1] == "abcdefghijklmnopqr "  # the wide character cut off


def test_text_is_drawn_in_its_colours(remote):
    plugin, _ = remote
    plugin.screen.feed(b"\x1b[31;44mR\x1b[0mP\x1b[1;4;7mB")
    _, red = _render(plugin, inspect=_attr_at(1, 0))
    pair = curses.pair_number(red)
    assert pair != 0
    _, plain = _render(plugin, inspect=_attr_at(1, 1))
    assert curses.pair_number(plain) == 0           # the terminal's own colours
    _, styled = _render(plugin, inspect=_attr_at(1, 2))
    assert styled & curses.A_BOLD and styled & curses.A_UNDERLINE
    assert styled & curses.A_REVERSE


def test_the_cursor_is_drawn_reversed_when_the_pane_is_active(remote, app):
    plugin, _ = remote
    plugin.screen.feed(b"prompt$ ")
    _, attr = _render(plugin, inspect=_attr_at(1, 8))
    assert attr & curses.A_REVERSE
    app.active = app.right
    _, attr = _render(plugin, inspect=_attr_at(1, 8))
    assert not attr & curses.A_REVERSE


@pytest.mark.parametrize("state", ["hidden", "menu", "scrolled", "done"])
def test_the_cursor_is_not_drawn_when_it_should_not_be(remote, state):
    plugin, _ = remote
    plugin.screen.feed(b"prompt$ ")
    if state == "hidden":
        plugin.screen.feed(b"\x1b[?25l")
    elif state == "menu":
        plugin.handle_key(MENU_KEY)
    elif state == "scrolled":
        _history(plugin)
        plugin.screen.feed(b"\x1b[Hprompt$ ")
        plugin.wheel(1)
    else:
        plugin._finish()
    _, attr = _render(plugin, inspect=_attr_at(1 + plugin.screen.y, 8))
    assert not attr & curses.A_REVERSE


def test_reverse_video_inverts_the_screen_and_the_cursor(remote):
    plugin, _ = remote
    plugin.screen.feed(b"ab\x1b[?5h")
    _, attr = _render(plugin, inspect=_attr_at(1, 0))
    assert attr & curses.A_REVERSE
    _, cursor = _render(plugin, inspect=_attr_at(1, 2))
    assert not cursor & curses.A_REVERSE


def test_invisible_text_is_drawn_as_blanks(remote):
    plugin, _ = remote
    plugin.screen.feed(b"\x1b[8msecret\x1b[0m shown")
    assert _render(plugin)[0].split("\n")[1].startswith("       shown")


def test_a_wide_character_under_the_cursor(remote):
    plugin, _ = remote
    plugin.screen.feed("日本\x1b[1;1H".encode())
    text, attr = _render(plugin, inspect=_attr_at(1, 0))
    assert attr & curses.A_REVERSE
    assert "日本" in text.split("\n")[1] or "日" in text


def test_box_drawing_falls_back_to_ascii_when_the_terminal_cannot_show_it(
        remote, monkeypatch):
    plugin, _ = remote
    plugin.screen.feed(b"\x1b(0lqk\r\nx x\r\nmqj\x1b(B")

    def draw(stdscr):
        # After the screen's own theme.init(), which picks the glyphs.
        monkeypatch.setattr(theme, "GLYPHS", theme.ASCII_GLYPHS)
        plugin.draw(stdscr, 0, 0, 10, 40)
        return [stdscr.instr(row, 0, 3).decode() for row in range(1, 4)]

    assert with_curses_screen(12, 42, draw) == ["+-+", "| |", "+-+"]


def test_the_ascii_fallback_table():
    assert terminal._ascii_fallback("─") == "-"
    assert terminal._ascii_fallback("│") == "|"
    assert terminal._ascii_fallback("┼") == "+"
    assert terminal._ascii_fallback("") == "?"        # no name at all


def test_a_cell_curses_refuses_does_not_stop_the_drawing(remote, monkeypatch):
    plugin, _ = remote
    plugin.screen.feed(b"ok")

    def draw(stdscr):
        real = stdscr.addstr

        class _Refusing:
            def __getattr__(self, name):
                return getattr(stdscr, name)

            def addstr(self, y, x, text, attr=0):
                if text.startswith("ok"):
                    raise curses.error("refused")
                return real(y, x, text, attr)

        plugin.draw(_Refusing(), 0, 0, 10, 40)
        return True

    assert with_curses_screen(12, 42, draw) is True


# -- colours -------------------------------------------------------------------

def _colours(fn, count=None):
    """Run ``fn(Colours())`` on a real colour screen."""
    return with_curses_screen(10, 40, lambda stdscr: fn(Colours()),
                              colours=count)


def test_the_default_colours_are_the_terminals_own():
    assert _colours(lambda c: curses.pair_number(c.attr(vt.PLAIN))) == 0


def _content(attr):
    return curses.pair_content(curses.pair_number(attr))


def test_256_colours_are_used_as_they_are():
    assert _colours(lambda c: _content(c.attr((196, 21, 0)))) == (196, 21)


def test_truecolour_maps_to_the_nearest_of_256():
    def check(c):
        return _content(c.attr((vt.RGB | 0xFF0000, vt.DEFAULT, 0))), c.default_bg

    pair, background = _colours(check)
    assert pair == (196, background)


def test_on_sixteen_colours_the_palette_maps_to_the_nearest_of_sixteen():
    def check(c):
        bright_red = c.attr((196, 1, 0))
        rgb_blue = c.attr((vt.RGB | 0x0000F0, 1, 0))
        low = c.attr((3, 1, 0))
        return [_content(a) for a in (bright_red, rgb_blue, low)]

    assert _colours(check, count=16) == [(9, 1), (4, 1), (3, 1)]


def test_on_eight_colours_bright_becomes_bold():
    def check(c):
        attr = c.attr((9, 12, 0))
        return curses.pair_content(curses.pair_number(attr)), bool(
            attr & curses.A_BOLD)

    assert _colours(check, count=8) == ((1, 4), True)


def test_on_eight_colours_dim_ones_stay_plain():
    def check(c):
        attr = c.attr((2, 5, 0))
        return _content(attr), bool(attr & curses.A_BOLD)

    assert _colours(check, count=8) == ((2, 5), False)


def test_with_the_terminals_default_colours_they_are_asked_for(monkeypatch):
    """After use_default_colors() pair 0 is (-1, -1): the outer terminal's
    own colours, which is what an uncoloured cell is drawn in."""
    def check(stdscr):
        monkeypatch.setattr(curses, "pair_content", lambda n: (-1, -1))
        colours = Colours()
        monkeypatch.undo()
        return (colours.default_fg, colours.default_bg,
                curses.pair_number(colours.attr(vt.PLAIN)))

    assert with_curses_screen(10, 40, check) == (-1, -1, 0)


def test_without_colour_only_the_attributes_are_kept():
    def check(c):
        return c.attr((1, 2, vt.BOLD | vt.UNDERLINE))

    attr = _colours(check, count=2)
    assert curses.pair_number(attr) == 0
    assert attr & curses.A_BOLD and attr & curses.A_UNDERLINE


def test_a_monochrome_scheme_keeps_the_attributes_only(monkeypatch):
    def check(stdscr):
        theme.init("mono")
        try:
            colours = Colours()
            return colours.attr((1, 2, vt.REVERSE)), colours.current()
        finally:
            theme.init()

    attr, current = with_curses_screen(10, 40, check)
    assert curses.pair_number(attr) == 0 and attr & curses.A_REVERSE
    assert current is True


def test_without_the_terminals_default_colours_white_on_black_stands_in(
        monkeypatch):
    def check(stdscr):
        monkeypatch.setattr(curses, "pair_content",
                            lambda n: (_ for _ in ()).throw(curses.error()))
        colours = Colours()
        plain = colours.attr(vt.PLAIN)
        red = colours.attr((1, vt.DEFAULT, 0))
        monkeypatch.undo()
        return (colours.default_fg, colours.default_bg,
                curses.pair_number(plain),
                curses.pair_content(curses.pair_number(red)))

    assert with_curses_screen(10, 40, check) == (
        curses.COLOR_WHITE, curses.COLOR_BLACK, 0, (1, curses.COLOR_BLACK))


def test_running_out_of_pairs_takes_the_nearest_one_made():
    def check(stdscr):
        pairs = theme.shared_pairs()
        pairs.limit = pairs._next + 2          # room for two more
        colours = Colours()
        red = curses.pair_number(colours.attr((196, vt.DEFAULT, 0)))
        blue = curses.pair_number(colours.attr((21, vt.DEFAULT, 0)))
        darker_red = curses.pair_number(colours.attr((160, vt.DEFAULT, 0)))
        near_white = curses.pair_number(colours.attr((231, 16, 0)))
        return red, blue, darker_red, near_white, pairs.exhausted

    red, blue, darker_red, near_white, exhausted = with_curses_screen(
        10, 40, check)
    assert exhausted is True
    assert darker_red == red
    assert near_white == 0                     # white on black: the default
    assert blue != red


def test_a_new_colour_scheme_makes_the_colours_again(remote):
    plugin, _ = remote

    def check(stdscr):
        plugin.draw(stdscr, 0, 0, 10, 40)
        first = plugin._colours
        theme.init()
        plugin.draw(stdscr, 0, 0, 10, 40)
        return first is not plugin._colours, plugin._colours.current()

    assert with_curses_screen(12, 42, check) == (True, True)


def test_nearest16():
    assert nearest16((250, 5, 5)) == 9
    assert nearest16((0, 0, 0)) == 0
    assert nearest16((128, 128, 128)) == 8


def test_output_wrapped_by_a_narrow_pane_reads_as_one_line():
    """What the tests search: a long path is one line, however it wrapped."""
    screen = vt.Screen(4, 10)
    screen.feed(b"/a/rather/long/path/name\r\nnext\r\n" + b"x\r\n" * 5)
    assert terminal_text(screen).split("\n")[:2] == [
        "/a/rather/long/path/name", "next"]
    unfinished = vt.Screen(2, 4)
    unfinished.feed(b"abcdefgh")
    assert terminal_text(unfinished) == "abcdefgh"
