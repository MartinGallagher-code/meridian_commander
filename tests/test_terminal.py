"""The in-pane terminal: the vt100 emulator and the plug-in that drives it."""

from __future__ import annotations

import curses
import errno
import os

import pytest

from meridian_commander.plugin_api import PluginContext
from meridian_commander.plugins import terminal as terminal_mod
from meridian_commander.plugins.terminal import TerminalPlugin, TermEmulator

from support import _FakeRemoteBackend, with_curses_screen


# -- the emulator --------------------------------------------------------------

def _fed(*chunks) -> TermEmulator:
    term = TermEmulator()
    for chunk in chunks:
        term.feed(chunk if isinstance(chunk, bytes) else chunk.encode())
    return term


def test_plain_text_and_newlines():
    term = _fed(b"hello\nworld")
    assert term.lines == ["hello"]
    assert term.cur == "world"
    assert term.col == 5


def test_carriage_return_rewrites_the_line():
    term = _fed(b"hello\rbye")
    assert term.cur == "byelo"


def test_backspace_moves_back_but_not_past_the_start():
    term = _fed(b"ab\b\b\b\bZ")
    assert term.cur == "Zb"


def test_tabs_advance_to_the_next_stop():
    term = _fed(b"a\tb")
    assert term.cur == "a       b"
    assert term.col == 9


def test_a_tab_on_an_empty_line_pads_it():
    term = _fed(b"\tx")
    assert term.cur.startswith("        x")


def test_the_bell_is_ignored():
    assert _fed(b"a\x07b").cur == "ab"


def test_writing_past_the_end_of_a_line_pads_it():
    term = TermEmulator()
    term.feed(b"ab")
    term.col = 5
    term.feed(b"Z")
    assert term.cur == "ab   Z"


def test_undecodable_bytes_are_replaced():
    # The decoder holds an incomplete sequence until the next byte proves it bad.
    assert "�" in _fed(b"caf\xe9", b"e").cur


def test_a_character_split_across_two_feeds_is_reassembled():
    term = TermEmulator()
    term.feed("é".encode()[:1])
    term.feed("é".encode()[1:])
    assert term.cur == "é"


def test_scrollback_is_capped(monkeypatch):
    monkeypatch.setattr(terminal_mod, "SCROLLBACK", 5)
    term = _fed(b"".join(f"line {i}\n".encode() for i in range(20)))
    assert len(term.lines) == 5
    assert term.lines[-1] == "line 19"


# -- escape sequences ----------------------------------------------------------

def test_erase_to_end_of_line():
    term = _fed(b"hello\r\x1b[K")
    assert term.cur == ""
    term = _fed(b"hello\r\x1b[3C\x1b[K")
    assert term.cur == "hel"


def test_clear_screen():
    term = _fed(b"one\ntwo\x1b[2J")
    assert term.lines == []
    assert term.cur == ""
    assert term.col == 0


def test_cursor_right_and_left():
    term = _fed(b"abcdef\x1b[3D")
    assert term.col == 3
    term.feed(b"\x1b[2C")
    assert term.col == 5


def test_cursor_left_stops_at_the_margin():
    term = _fed(b"abc\x1b[99D")
    assert term.col == 0


@pytest.mark.parametrize("final", [b"H", b"f"])
def test_cursor_home_resets_the_column(final):
    term = _fed(b"abcdef\x1b[" + final)
    assert term.col == 0


def test_a_csi_parameter_that_is_not_a_number_uses_the_default():
    # "-" is below "@" so it accumulates as a parameter rather than ending it.
    term = _fed(b"abcdef\x1b[--D")
    assert term.col == 5          # int("--") fails: a default move of one


def test_an_empty_csi_parameter_uses_the_default():
    term = _fed(b"abcdef\x1b[D")
    assert term.col == 5


def test_a_multi_parameter_csi_uses_the_first():
    term = _fed(b"abcdef\x1b[2;9D")
    assert term.col == 4


def test_colour_and_mode_sequences_are_ignored():
    term = _fed(b"\x1b[1;31mred\x1b[0m\x1b[?25h!")
    assert term.cur == "red!"


def test_a_single_character_escape_is_ignored():
    assert _fed(b"a\x1bMb").cur == "ab"


def test_an_osc_sequence_is_swallowed_up_to_the_bell():
    term = _fed(b"\x1b]0;window title\x07visible")
    assert term.cur == "visible"


def test_an_osc_sequence_can_end_with_a_string_terminator():
    term = _fed(b"\x1b]0;title\x1b\\visible")
    assert term.cur == "visible"


def test_an_escape_inside_an_osc_that_is_not_a_terminator_stays_in_osc():
    term = _fed(b"\x1b]0;ti\x1bXtle\x07after")
    assert term.cur == "after"


# -- the visible window --------------------------------------------------------

def test_visible_returns_the_last_rows():
    term = _fed(b"".join(f"line {i}\n".encode() for i in range(10)))
    assert term.visible(3) == ["line 8", "line 9", ""]


def test_visible_scrolled_back():
    term = _fed(b"".join(f"line {i}\n".encode() for i in range(10)))
    assert term.visible(2, scroll=3) == ["line 6", "line 7"]


def test_visible_of_a_short_buffer_is_all_of_it():
    assert _fed(b"only").visible(10) == ["only"]


# -- the plug-in, on a local pty -----------------------------------------------

@pytest.fixture
def local_ctx(app):
    return PluginContext(app=app, own_panel=app.left, other_panel=app.right)


def _wait_for(plugin, predicate, tries=400):
    """Pump the plug-in until ``predicate`` holds (the shell is a real pty)."""
    import time

    for _ in range(tries):
        plugin.tick()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_local_terminal_runs_a_real_shell(local_ctx, monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        plugin.handle_key(ord("e"))
        for ch in "cho marker-value":
            plugin.handle_key(ord(ch))
        plugin.handle_key(10)
        assert _wait_for(plugin, lambda: "marker-value" in "\n".join(
            plugin.term.lines + [plugin.term.cur]))
    finally:
        plugin.on_exit()


def test_a_local_terminal_starts_in_the_panes_directory(local_ctx, monkeypatch,
                                                        tmp_path):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        for ch in "pwd":
            plugin.handle_key(ord(ch))
        plugin.handle_key(10)
        assert _wait_for(plugin, lambda: str(tmp_path / "left") in "\n".join(
            plugin.term.lines))
    finally:
        plugin.on_exit()


def test_a_local_terminal_notices_the_shell_exiting(local_ctx, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        for ch in "exit":
            plugin.handle_key(ord(ch))
        plugin.handle_key(10)
        assert _wait_for(plugin, lambda: plugin.done)
        assert "process exited" in plugin.status
        # Any key then closes the pane.
        assert plugin.handle_key(ord("x")) is False
    finally:
        plugin.on_exit()


def test_a_local_terminal_in_a_directory_that_has_gone(local_ctx, monkeypatch,
                                                       tmp_path):
    monkeypatch.setenv("SHELL", "/bin/sh")
    local_ctx.own_panel.path = str(tmp_path / "vanished")
    plugin = TerminalPlugin(local_ctx)
    try:
        # The chdir fails in the child but the shell still starts.
        for ch in "echo alive":
            plugin.handle_key(ord(ch))
        plugin.handle_key(10)
        assert _wait_for(plugin, lambda: "alive" in "\n".join(
            plugin.term.lines + [plugin.term.cur]))
    finally:
        plugin.on_exit()


def test_closing_a_local_terminal_twice_is_harmless(local_ctx, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    plugin.on_exit()
    plugin.on_exit()
    assert plugin._fd is None and plugin._pid is None


def test_a_read_error_that_is_not_would_block_ends_the_terminal(local_ctx,
                                                                monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        def broken(fd, size):
            raise OSError(errno.EIO, "input/output error")

        monkeypatch.setattr(os, "read", broken)
        plugin.tick()
        assert plugin.done is True
    finally:
        plugin.on_exit()


def test_a_write_that_fails_ends_the_terminal(local_ctx, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        monkeypatch.setattr(os, "write", lambda fd, data: 1 / 0)
        plugin.handle_key(ord("x"))
        assert plugin.done is True
    finally:
        plugin.on_exit()


def test_a_vanished_child_is_not_an_error(local_ctx, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        def reaped(pid, flags):
            raise ChildProcessError("no child processes")

        monkeypatch.setattr(os, "waitpid", reaped)
        plugin.tick()                  # must not raise
    finally:
        plugin._pid = None
        plugin.on_exit()


def test_tick_does_nothing_once_finished(local_ctx, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        plugin._finish()
        plugin._finish()               # already done: status unchanged
        plugin.tick()
        assert plugin.status.count("process exited") == 1
    finally:
        plugin.on_exit()


# -- the plug-in, on a remote pane ---------------------------------------------

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


def test_a_remote_terminal_opens_a_shell_and_changes_directory(app, tmp_path):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    assert channel.shell is True
    assert channel.pty == ("vt100", 80, 24)
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


def test_an_ftp_pane_has_no_shell(app, monkeypatch):
    class _Ftpish(type(app.left.fs)):
        scheme = "ftp"

    app.left.fs = _Ftpish()
    ctx = PluginContext(app=app, own_panel=app.left, other_panel=app.right)
    with pytest.raises(RuntimeError, match="FTP has no shell"):
        TerminalPlugin(ctx)


def test_a_remote_terminal_pumps_output(app):
    channel = _FakeChannel(reads=[b"hello\n", b""])
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    plugin.tick()
    assert "hello" in plugin.term.lines
    plugin.on_exit()


def test_a_remote_terminal_notices_the_shell_exiting(app):
    channel = _FakeChannel(reads=[b"bye\n"], exit_ready=True)
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    plugin.tick()
    assert plugin.done is True
    assert "bye" in plugin.term.lines
    plugin.on_exit()


def test_a_remote_terminal_notices_a_closed_channel(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    channel.closed = True
    plugin.tick()
    assert plugin.done is True
    plugin.on_exit()


def test_a_remote_drain_that_fails_still_finishes(app):
    channel = _FakeChannel(exit_ready=True)
    plugin = TerminalPlugin(_remote_ctx(app, channel))

    # The first poll is clear; the drain after the exit status then fails.
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


def test_a_remote_send_that_fails_ends_the_terminal(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))

    def explode(data):
        raise OSError("broken pipe")

    channel.sendall = explode
    plugin.handle_key(ord("x"))
    assert plugin.done is True
    plugin.on_exit()


def test_a_remote_channel_that_fails_to_close_is_ignored(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    channel.close = lambda: 1 / 0
    plugin.on_exit()
    assert plugin._chan is None


# -- keys ----------------------------------------------------------------------

@pytest.mark.parametrize("key, expected", [
    (curses.KEY_UP, b"\x1b[A"),
    (curses.KEY_DOWN, b"\x1b[B"),
    (curses.KEY_RIGHT, b"\x1b[C"),
    (curses.KEY_LEFT, b"\x1b[D"),
    (curses.KEY_HOME, b"\x1b[H"),
    (curses.KEY_END, b"\x1b[F"),
    (curses.KEY_DC, b"\x1b[3~"),
    (curses.KEY_BACKSPACE, b"\x7f"),
    (127, b"\x7f"),
    (8, b"\x7f"),
    (10, b"\r"),
    (13, b"\r"),
    (3, b"\x03"),
    (9, b"\t"),
    (ord("a"), b"a"),
    (ord("é"), "é".encode()),
    (0x110000, b""),
])
def test_key_encoding(key, expected):
    assert TerminalPlugin._encode_key(key) == expected


def test_a_surrogate_key_encodes_to_nothing():
    assert TerminalPlugin._encode_key(0xD800) == b""


def test_keys_reach_the_remote_shell(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    channel.sent = b""
    plugin.handle_key(ord("l"))
    plugin.handle_key(10)
    assert channel.sent == b"l\r"
    plugin.on_exit()


def test_switch_and_close_keys(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    assert plugin.handle_key(TerminalPlugin.SWITCH_KEY) is True
    assert app.active is app.right
    assert plugin.handle_key(TerminalPlugin.CLOSE_KEY) is False
    assert channel.closed is True


def test_scrollback_keys(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    plugin.term.feed(b"".join(f"line {i}\n".encode() for i in range(50)))
    assert plugin.handle_key(curses.KEY_PPAGE) is True
    assert plugin.scroll == 10
    plugin.handle_key(curses.KEY_PPAGE)
    assert plugin.scroll == 20
    plugin.handle_key(curses.KEY_NPAGE)
    assert plugin.scroll == 10
    # Typing jumps back to the live view.
    plugin.handle_key(ord("x"))
    assert plugin.scroll == 0
    plugin.on_exit()


def test_scrollback_is_bounded_by_the_buffer(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    plugin.term.feed(b"one\ntwo\n")
    for _ in range(5):
        plugin.handle_key(curses.KEY_PPAGE)
    assert plugin.scroll == max(0, len(plugin.term.lines) - 1)
    for _ in range(5):
        plugin.handle_key(curses.KEY_NPAGE)
    assert plugin.scroll == 0
    plugin.on_exit()


# -- drawing -------------------------------------------------------------------

def _render(plugin, h=10, w=40):
    def draw(stdscr):
        plugin.draw(stdscr, 0, 0, h, w)
        return "\n".join(stdscr.instr(row, 0, w).decode() for row in range(h))

    return with_curses_screen(h + 2, w + 2, draw)


def test_drawing_shows_the_header_output_and_hint(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    plugin.term.feed(b"first\nsecond\n")
    rendered = _render(plugin)
    assert "[terminal]" in rendered
    assert "first" in rendered and "second" in rendered
    assert "Ctrl-] switch pane" in rendered
    plugin.on_exit()


def test_drawing_shows_the_scrollback_footer(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    plugin.term.feed(b"".join(f"line {i}\n".encode() for i in range(50)))
    plugin.scroll = 12
    assert "scrollback (12 lines back)" in _render(plugin)
    plugin.on_exit()


def test_drawing_shows_the_exit_status(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    plugin._finish()
    assert "process exited" in _render(plugin)
    plugin.on_exit()


def test_drawing_a_pane_too_short_for_any_output(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))

    def draw(stdscr):
        plugin.draw(stdscr, 0, 0, 2, 40)     # h - 2 leaves no rows
        return True

    assert with_curses_screen(10, 42, draw) is True
    plugin.on_exit()


def test_drawing_resizes_the_remote_pty(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    _render(plugin, h=12, w=50)
    assert channel.resized == (50, 10)
    plugin.on_exit()


def test_a_resize_the_channel_refuses_is_ignored(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    channel.resize_pty = lambda width, height: 1 / 0
    _render(plugin, h=14, w=52)              # must not raise
    plugin.on_exit()


def test_drawing_resizes_a_local_pty(local_ctx, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        _render(plugin, h=12, w=50)          # exercises the ioctl path
    finally:
        plugin.on_exit()


def test_a_local_resize_that_fails_is_ignored(local_ctx, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        plugin.on_exit()
        plugin._fd = -1                      # a closed descriptor
        plugin._resize(10, 20)               # must not raise
    finally:
        plugin._fd = None


def test_the_cursor_cell_is_drawn_on_the_last_row(app):
    channel = _FakeChannel()
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    plugin.term.feed(b"prompt$ ")
    assert "prompt$" in _render(plugin)
    plugin.on_exit()


def test_a_remote_drain_feeds_what_was_still_buffered(app):
    channel = _FakeChannel(reads=[b"last words\n"], exit_ready=True)
    plugin = TerminalPlugin(_remote_ctx(app, channel))
    # Nothing ready on the first poll, then one buffered chunk during the
    # drain that follows the exit status.
    ready = iter([False, True, False])
    channel.recv_ready = lambda: next(ready, False)
    plugin.tick()
    assert plugin.done is True
    assert "last words" in plugin.term.lines
    plugin.on_exit()


def test_closing_a_descriptor_that_has_already_gone_is_ignored(local_ctx,
                                                               monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    plugin.on_exit()
    plugin._fd = -1                      # never a valid descriptor
    plugin.on_exit()                     # must not raise
    assert plugin._fd is None


def test_signalling_a_child_that_has_already_gone_is_ignored(local_ctx,
                                                             monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    plugin = TerminalPlugin(local_ctx)
    try:
        monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(
            ProcessLookupError("no such process")))
        plugin.on_exit()                 # must not raise
        assert plugin._pid is None
    finally:
        monkeypatch.undo()
