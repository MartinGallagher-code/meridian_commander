# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Background commands (MC-027): the process on a pty of its own, read while
no pane shows it, and stopped with everything it started; the list of them;
and the pane that shows one.

The processes are real, on real ptys, as the terminal's own tests run a real
shell -- but every program is a stub from ``command_stubs`` on a PATH of the
test's own, never a real server, and each test stops what it starts (the
suite checks that it did).
"""

from __future__ import annotations

import curses
import errno
import os
import signal
import time

import pytest

from meridian_commander import background, theme
from meridian_commander.background import (
    Command,
    CommandList,
    duration,
    shell_argv,
    stop_all,
)
from meridian_commander.commandpane import CommandPane
from meridian_commander.plugin_api import PluginContext
from meridian_commander.plugins.terminal import MENU_KEY, TerminalPlugin

from command_stubs import process_gone, wait_until
from support import terminal_text, with_curses_screen


@pytest.fixture
def started(stubs, tmp_path):
    """``start(line)`` runs a command in ``tmp_path``; all stop at the end."""
    made = CommandList()

    def start(line, directory=None, name="cmd"):
        return made.start(name, line, str(directory or tmp_path))

    start.list = made
    yield start
    made.stop_all(0.5)


def _text(command) -> str:
    with command.lock:
        return terminal_text(command.screen)


def _shows(command, text, seconds=10.0) -> bool:
    return wait_until(lambda: text in _text(command), seconds)


def _ended(command, seconds=10.0) -> bool:
    return wait_until(command.poll, seconds)


def _why(command) -> str:
    """Everything worth knowing when an assertion about a command fails."""
    return (f"state={command.state_text()} status={command.exit_status} "
            f"eof={command._eof} reads={command.output} "
            f"reader_alive={command._thread.is_alive()}\n"
            f"--- screen ---\n{_text(command)}")


# -- small parts -----------------------------------------------------------------

@pytest.mark.parametrize("seconds, text", [
    (0, "0s"), (59.9, "59s"), (60, "1m00s"), (187, "3m07s"),
    (3600, "1h00m"), (7500, "2h05m"),
])
def test_duration(seconds, text):
    assert duration(seconds) == text


def test_the_line_is_run_by_the_users_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    assert shell_argv("a | b") == ["/usr/bin/zsh", "-c", "a | b"]
    monkeypatch.setenv("SHELL", "")
    assert shell_argv("x") == ["/bin/sh", "-c", "x"]
    monkeypatch.delenv("SHELL")
    assert shell_argv("x") == ["/bin/sh", "-c", "x"]


def _idle(pid=424242) -> Command:
    """A command never started: its state is set by hand."""
    command = Command(7, "web", "fake-server", "/srv")
    command.pid = pid
    return command


def test_the_state_as_the_list_shows_it():
    command = _idle()
    assert command.state_text() == "running" and command.running()
    command.ended = True
    assert command.state_text() == "exited"           # status not collected
    command.exit_status = 0
    assert command.state_text() == "exited (status 0)"
    command.exit_status = 3
    assert command.state_text() == "exited (status 3)"
    command.exit_status = -signal.SIGTERM
    assert command.state_text() == "killed (SIGTERM)"
    command.stopped = True
    assert command.state_text() == "stopped"
    assert not command.running()


def test_the_age_runs_until_the_end(monkeypatch):
    command = _idle()
    command.started = 100.0
    monkeypatch.setattr(background.time, "monotonic", lambda: 145.0)
    assert command.age() == "45s"
    command.finished = 130.0
    assert command.age() == "30s"


def test_a_status_collected_elsewhere_is_unknown(monkeypatch):
    command = _idle()

    def gone(pid, flags):
        raise ChildProcessError("no child processes")

    def empty(pgid, number):
        raise ProcessLookupError("no such process group")

    monkeypatch.setattr(os, "waitpid", gone)
    monkeypatch.setattr(os, "killpg", empty)
    assert command.poll() is True
    assert command.ended and command.exit_status is None
    assert command.poll() is True                      # once is enough
    assert command.settled()


def test_a_group_that_is_not_ours_to_signal(monkeypatch):
    command = _idle()
    sent = []

    def refused(pgid, number):
        sent.append(number)
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "killpg", refused)
    assert command._group_alive() is True
    command.terminate()
    assert sent == [0, signal.SIGTERM, signal.SIGCONT]
    assert command.stopped


def test_a_group_found_empty_is_never_signalled_again(monkeypatch):
    command = _idle()
    sent = []

    def empty(pgid, number):
        sent.append(number)
        raise ProcessLookupError("no such process group")

    monkeypatch.setattr(os, "killpg", empty)
    command.terminate()
    command._signal(signal.SIGKILL)
    assert command._group_alive() is False
    assert sent == [signal.SIGTERM]


def test_killing_waits_for_the_reaping(monkeypatch):
    command = _idle()
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pgid, number: sent.append(number))
    answers = [False, False, True]
    command.poll = lambda: answers.pop(0)
    command.kill()
    assert sent == [signal.SIGKILL] and answers == []


def test_killing_waits_no_longer_than_it_should(monkeypatch):
    """A process stuck in the kernel is not waited for for ever."""
    command = _idle()
    monkeypatch.setattr(os, "killpg", lambda pgid, number: None)
    monkeypatch.setattr(background, "KILL_WAIT", 0.05)
    command.poll = lambda: False
    began = time.monotonic()
    command.kill()
    assert time.monotonic() - began < 1


def test_starting_waits_for_the_childs_own_group(monkeypatch):
    """Stopped the moment it starts, a command must still be found."""
    command = _idle()
    answers = [1, 1, command.pid]
    monkeypatch.setattr(os, "getpgid", lambda pid: answers.pop(0))
    command._await_group()
    assert answers == []


@pytest.mark.parametrize("answer", ["gone", "late"])
def test_starting_does_not_wait_for_ever(monkeypatch, answer):
    command = _idle()
    if answer == "gone":
        def getpgid(pid):
            raise ProcessLookupError("no such process")
    else:
        def getpgid(pid):
            return 1                       # never its own
        monkeypatch.setattr(background, "GROUP_SECONDS", 0.05)
    monkeypatch.setattr(os, "getpgid", getpgid)
    began = time.monotonic()
    command._await_group()
    assert time.monotonic() - began < 1


def test_a_closing_reader_takes_the_last_words_and_lets_go(tmp_path):
    """Closed before it saw the end: what is still on its way is read, then
    the terminal is closed -- by the reader, the one place that closes it."""
    command = _idle()
    read_end, write_end = os.pipe()
    os.write(write_end, b"last words")
    os.close(write_end)
    command._fd = read_end
    command._closing.set()
    command._pump()                        # on this thread, to the end
    assert "last words" in _text(command)
    assert command._eof and command._fd is None
    with pytest.raises(OSError):
        os.fstat(read_end)


def test_a_closing_reader_waits_only_a_moment(monkeypatch):
    command = _idle()
    read_end, write_end = os.pipe()        # open, and silent
    monkeypatch.setattr(background, "DRAIN_SECONDS", 0.1)
    command._fd = read_end
    command._closing.set()
    began = time.monotonic()
    command._pump()
    assert time.monotonic() - began < 1 and not command._eof
    os.close(write_end)


def test_stopped_the_moment_it_starts(started):
    """The race the group wait is for, on a real fork."""
    for _ in range(5):
        command = started("fake-server")
        command.stop(grace=1.0)
        assert command.ended and process_gone(command.pid)


def test_a_read_that_would_block_is_not_the_end(monkeypatch):
    command = _idle()
    answers = [OSError(errno.EAGAIN, "again"), OSError(errno.EIO, "gone")]

    def read(fd, size):
        raise answers.pop(0)

    monkeypatch.setattr(os, "read", read)
    assert command._read(99) is False and not command._eof
    assert command._read(99) is False and command._eof


def test_an_empty_read_is_the_end(monkeypatch):
    command = _idle()
    monkeypatch.setattr(os, "read", lambda fd, size: b"")
    assert command._read(99) is False and command._eof


def test_writes_to_a_terminal_that_is_not_there_are_dropped(monkeypatch):
    command = _idle()
    command.send(b"x")                                 # no terminal yet
    command.set_size(10, 10)
    command._fd = 99

    def full(fd, data):
        raise BlockingIOError(errno.EAGAIN, "full")

    monkeypatch.setattr(os, "write", full)
    command.send(b"typed")                             # must not raise
    written = []

    def one_byte(fd, data):
        written.append(data[:1])
        return 1

    monkeypatch.setattr(os, "write", one_byte)
    command.send(b"\x1b[A")
    assert b"".join(written) == b"\x1b[A"

    def refused(fd, rows, cols):
        raise OSError(errno.EINVAL, "invalid argument")

    monkeypatch.setattr(background, "set_size", refused)
    command.set_size(10, 10)                           # must not raise


# -- the process, on a real pty -----------------------------------------------------

def test_a_command_starts_in_its_directory(started, tmp_path):
    (tmp_path / "site").mkdir()
    command = started("pwd; echo shown", tmp_path / "site")
    assert _ended(command)
    assert _shows(command, "shown")
    assert str(tmp_path / "site") in _text(command)
    assert command.state_text() == "exited (status 0)"
    assert command.finished is not None


def test_the_line_runs_as_at_a_prompt(started):
    command = started('X=piped; echo "a-$X" | tr - + && echo after')
    assert _ended(command)
    assert _shows(command, "after")
    assert "a+piped" in _text(command)


def test_the_programs_terminal_is_xterm_with_its_own_size(started):
    # Asked once the program is up: the size is set just after the fork,
    # and a program asking at once could be answered before it.
    command = started("fake-repl")
    assert _shows(command, "ready"), _why(command)
    command.send(b'echo "term=$TERM lines=${LINES-unset}"; stty size\r')
    assert _shows(command, "term=xterm-256color lines=unset"), _why(command)
    assert _shows(command, "24 80"), _why(command)


def test_the_last_words_before_the_end_are_kept(started):
    """Written after a while, just before the program ends: still read."""
    command = started("sleep 0.3; echo last-words-here")
    assert _ended(command), _why(command)
    assert _shows(command, "last-words-here"), _why(command)
    assert command.exit_status == 0, _why(command)


def test_the_exit_status_is_collected(started):
    command = started("exit 3")
    assert _ended(command)
    assert command.exit_status == 3
    assert command.state_text() == "exited (status 3)"


def test_a_command_that_fails_at_once_says_why_on_its_screen(started):
    command = started("fake-port-taken")
    assert _ended(command)
    assert _shows(command, "Address already in use")
    assert command.state_text() == "exited (status 1)"


def test_a_program_that_is_not_there_says_so(started):
    command = started("no-such-program-here")
    assert _ended(command)
    assert command.exit_status == 127
    assert _shows(command, "not found")


def test_a_shell_that_cannot_be_run_says_so(started, monkeypatch):
    monkeypatch.setenv("SHELL", "/no/such/shell")
    command = started("true")
    assert _ended(command)
    assert command.exit_status == 127
    assert _shows(command, "/no/such/shell: No such file or directory")


def test_output_is_read_while_no_pane_shows_it(started):
    """A pty holds a few kilobytes: a command nobody read would stop at its
    next write, and never get to the end of several megabytes."""
    command = started("fake-flood 4000000")
    assert command.viewer is None
    assert _ended(command, seconds=60)
    assert command.exit_status == 0
    assert _shows(command, "flood-done")
    assert len(command.screen.scrollback) <= background.vt.SCROLLBACK
    assert command.screen.history_count > background.vt.SCROLLBACK


def test_keys_reach_the_program(started):
    command = started("fake-repl")
    assert _shows(command, "ready")
    command.send(b"echo typed-$((6*7))\r")
    assert _shows(command, "typed-42")


def test_ctrl_c_reaches_the_program(started):
    command = started("fake-repl")
    assert _shows(command, "ready")
    command.send(b"\x03")
    assert _ended(command)
    # dash dies of the signal; bash, macOS's sh, may exit with 128 + 2.
    assert command.exit_status in (-signal.SIGINT, 128 + signal.SIGINT)


def test_the_programs_questions_are_answered(started):
    command = started("fake-repl")
    assert _shows(command, "ready")
    command.send(b"printf '\\033[H\\033[6n'; sleep 1\r")
    assert _shows(command, "^[[1;1R")


def test_a_new_size_reaches_the_program(started):
    command = started("fake-repl")
    assert _shows(command, "ready")
    command.set_size(10, 50)
    command.send(b"stty size\r")
    assert _shows(command, "10 50")


def test_a_program_that_lets_go_of_its_terminal_is_still_waited_for(started):
    command = started("exec </dev/null >/dev/null 2>&1; sleep 0.5; exit 4")
    # The pty has no reader left long before the program ends; the end is
    # still collected, with its status.
    assert _ended(command)
    assert command._eof and command.exit_status == 4


def test_stopping_ends_the_whole_process_group(started, tmp_path):
    pidfile = tmp_path / "worker.pid"
    command = started(f"fake-workers {pidfile}")
    assert _shows(command, "workers-up")
    worker = int(pidfile.read_text())
    assert not process_gone(worker)
    command.stop(grace=1.0)
    assert command.ended and command.stopped
    assert command.state_text() == "stopped"
    assert wait_until(lambda: process_gone(worker))
    assert process_gone(command.pid)
    assert command._fd is None                       # let go of the pty


def test_stopping_lets_a_program_have_its_last_word(started):
    command = started("fake-signals")
    assert _shows(command, "ready")
    command.stop()
    assert _shows(command, "SIGNALLED", seconds=1)
    assert command.state_text() == "stopped"


def test_a_program_that_ignores_sigterm_is_killed(started, tmp_path):
    """The whole group refuses SIGTERM, the shell that leads it included:
    only the SIGKILL after the grace ends it.  (With a leader that did die
    of SIGTERM, the kernel would hang the rest of the group up as the
    leader went, and the grace would never be reached.)"""
    pidfile = tmp_path / "stubborn.pid"
    command = started(f"trap '' TERM; fake-stubborn {pidfile}; :")
    assert _shows(command, "ready"), _why(command)
    stubborn = int(pidfile.read_text())
    began = time.monotonic()
    command.stop(grace=0.3)
    assert 0.3 <= time.monotonic() - began < 3, _why(command)
    assert command.exit_status == -signal.SIGKILL, _why(command)
    assert wait_until(lambda: process_gone(stubborn))
    assert command.ended and command.state_text() == "stopped"


def test_a_leader_that_dies_of_sigterm_takes_its_group_with_it(started,
                                                               tmp_path):
    """The shell leading the group ends on SIGTERM; the kernel hangs up the
    rest as it goes, so a child that ignores SIGTERM goes too, at once."""
    pidfile = tmp_path / "stubborn.pid"
    command = started(f"fake-stubborn {pidfile}; :")
    assert _shows(command, "ready"), _why(command)
    stubborn = int(pidfile.read_text())
    command.stop(grace=1.0)
    assert command.ended and command.exit_status == -signal.SIGTERM, \
        _why(command)
    assert wait_until(lambda: process_gone(stubborn))


def test_a_stopped_program_is_continued_to_hear_it(started):
    command = started("fake-server")
    assert _shows(command, "Serving HTTP")
    os.killpg(command.pid, signal.SIGSTOP)
    began = time.monotonic()
    command.stop(grace=5.0)
    assert time.monotonic() - began < 4               # SIGTERM, not SIGKILL
    assert command.exit_status == -signal.SIGTERM


def test_commands_are_stopped_together(started):
    first, second = started("fake-server"), started("fake-server 8001")
    assert _shows(first, "port 8000") and _shows(second, "port 8001")
    began = time.monotonic()
    stop_all([first, second], grace=5.0)
    assert time.monotonic() - began < 4
    assert first.ended and second.ended


def test_stopping_an_ended_command_signals_nothing(started, monkeypatch):
    command = started("true")
    assert _ended(command)
    assert command._group_gone                          # found empty
    monkeypatch.setattr(os, "killpg", lambda *a: 1 / 0)  # never again
    command.stop()
    assert not command.stopped
    assert command.state_text() == "exited (status 0)"


def test_the_safety_net_at_exit_stops_whatever_is_left(stubs, tmp_path):
    command = CommandList().start("left", "fake-server", str(tmp_path))
    assert command in background._LIVE
    background.stop_everything()
    assert command.ended and command not in background._LIVE
    assert process_gone(command.pid)


# -- the list of them ------------------------------------------------------------------

def test_the_list_numbers_reports_and_counts(started):
    commands = started.list
    assert commands.status() == "" and commands.tick() == []
    first = started("fake-server", name="web")
    second = started("exit 2", name="once")
    assert (first.number, second.number) == (1, 2)
    assert commands.find("web") is first and commands.find("once") is None \
        or _ended(second)
    assert _ended(second)
    assert commands.tick() == [second]
    assert commands.tick() == []                        # reported once
    assert commands.running() == [first]
    assert commands.find("once") is None
    assert commands.status() == "Commands: 1 running"


def test_starting_again_replaces_the_ended_run(started):
    commands = started.list
    old = started("exit 1", name="job")
    assert _ended(old)
    running = started("fake-server", name="job")
    again = started("fake-server", name="job")
    assert old not in commands.commands
    assert commands.commands == [running, again]       # both still running
    assert again.number == 3


def test_removing_lets_go_of_an_ended_command(started):
    commands = started.list
    command = started("true")
    assert _ended(command)
    commands.remove(command)
    assert commands.commands == []
    assert command not in background._LIVE


def test_a_command_that_cannot_start_is_not_listed(started, monkeypatch):
    def no_pty(directory, argv):
        raise OSError(errno.EAGAIN, "out of pseudo-terminals")

    monkeypatch.setattr(background, "spawn", no_pty)
    with pytest.raises(OSError):
        started("fake-server")
    assert started.list.commands == []
    monkeypatch.undo()


# -- the view: a pane showing a command ---------------------------------------------------

@pytest.fixture
def view(app):
    """A command never started, and a pane showing it.  Its screen is fed by
    hand, and what is typed to it is recorded."""
    command = _idle()
    command.sent = []
    command.send = command.sent.append
    ctx = PluginContext(app=app, own_panel=app.left, other_panel=app.right)
    return CommandPane(ctx, command), command


def test_the_view_attaches_and_detaches(view):
    pane, command = view
    assert command.viewer is pane and pane.screen is command.screen
    assert isinstance(pane, TerminalPlugin)
    assert (pane._fd, pane._pid, pane._chan) == (None, None, None)
    pane.on_exit()
    assert command.viewer is None
    command.viewer = other = object()
    pane.on_exit()                                     # not ours to undo
    assert command.viewer is other
    assert command.running()


def test_the_view_notices_output_and_asks_for_quick_polls(view, monkeypatch):
    pane, command = view
    pane._busy_at = 0.0
    pane.tick()
    assert pane.poll_ms is None                        # nothing new
    command.screen.feed(b"hello")
    command.output += 1
    pane.tick()
    assert pane.poll_ms is not None


def test_a_scrolled_back_view_stays_on_its_lines(view):
    pane, command = view
    command.screen.feed(b"line\r\n" * 60)
    pane.tick()
    pane.handle_key(MENU_KEY)
    pane.handle_key(curses.KEY_PPAGE)
    before = pane.scroll
    assert before > 0
    command.screen.feed(b"more\r\n" * 5)
    pane.tick()
    assert pane.scroll == before + 5


def test_keys_go_to_the_program(view):
    pane, command = view
    assert pane.handle_key(ord("x")) is True
    assert pane.handle_key(3) is True                  # Ctrl-C, sent as such
    assert command.sent == [b"x", b"\x03"]


def test_ctrl_bracket_q_hides_without_stopping(view, app, monkeypatch):
    pane, command = view
    stopped = []
    monkeypatch.setattr(app, "_stop_command", stopped.append)
    pane.handle_key(MENU_KEY)
    assert pane.handle_key(ord("q")) is False
    assert not pane.menu and stopped == [] and command.sent == []


def test_ctrl_bracket_k_stops_it_through_the_app(view, app, monkeypatch):
    pane, command = view
    stopped = []
    monkeypatch.setattr(app, "_stop_command", stopped.append)
    pane.handle_key(MENU_KEY)
    assert pane.handle_key(ord("K")) is True
    assert stopped == [command] and not pane.menu


def test_ctrl_bracket_tab_and_z_are_the_terminals(view, app):
    pane, _ = view
    pane.handle_key(MENU_KEY)
    pane.handle_key(ord("z"))
    assert app.single is True
    pane.handle_key(MENU_KEY)
    pane.handle_key(9)
    assert app.active is app.right


def test_an_ended_command_keeps_its_history_readable(view):
    pane, command = view
    command.screen.feed(b"old\r\n" * 60 + b"last words")
    command.ended, command.exit_status = True, 1
    pane.tick()
    assert pane.done
    assert pane.status == "[exited (status 1) -- PgUp history, any key closes]"
    assert pane.handle_key(curses.KEY_MOUSE) is None
    assert pane.handle_key(curses.KEY_PPAGE) is True and pane.scroll > 0
    assert pane.handle_key(curses.KEY_UP) is True
    assert pane.handle_key(27) is True and pane.scroll == 0   # back to live
    pane.wheel(3)
    assert pane.scroll == 3
    pane.wheel(-3)
    assert pane.handle_key(MENU_KEY) is True and pane.menu
    assert pane.handle_key(ord("z")) is True                  # the menu's
    pane.handle_key(MENU_KEY)
    assert pane.handle_key(ord("q")) is False
    assert pane.handle_key(ord("x")) is False                 # any key closes
    assert command.sent == []
    pane._finish()                                            # once only
    assert pane.status.startswith("[exited")


def test_a_command_already_ended_shows_so_at_once(app):
    command = _idle()
    command.ended, command.exit_status = True, 0
    ctx = PluginContext(app=app, own_panel=app.left, other_panel=app.right)
    pane = CommandPane(ctx, command)
    assert pane.done and "exited (status 0)" in pane.status


def test_the_caption_and_the_footers(view):
    pane, command = view
    assert pane._caption() == " [web] running in /srv "
    assert pane._footer() == " Ctrl-]: Tab pane  q hide  k stop "
    pane.menu = True
    assert pane._footer() == " Tab pane  z zoom  q hide  k stop  PgUp "
    pane.menu, pane.scroll = False, 4
    assert pane._footer() == " scrollback, 4 lines up  Esc: live "
    pane.scroll = 0
    command.ended, command.exit_status = True, 0
    pane.tick()
    assert pane._footer().startswith(" [exited (status 0) -- ")
    pane.menu = True
    assert pane._footer() == " Tab pane  z zoom  q close  PgUp scroll "
    for footer in (" Ctrl-]: Tab pane  q hide  k stop ",
                   " Tab pane  z zoom  q hide  k stop  PgUp ",
                   " Tab pane  z zoom  q close  PgUp scroll "):
        assert len(footer) <= 40                     # half an 80-column screen


def test_drawing_resizes_the_screen_and_tells_the_program(view, monkeypatch):
    pane, command = view
    sizes = []
    monkeypatch.setattr(command, "set_size",
                        lambda rows, cols: sizes.append((rows, cols)))
    command.screen.feed(b"\x1b[1;31mred\x1b[0m")

    def draw(stdscr):
        pane.draw(stdscr, 0, 0, 12, 50)
        pane.draw(stdscr, 0, 0, 12, 50)              # the same size: once
        return [stdscr.instr(row, 0, 50).decode() for row in range(12)]

    rows = with_curses_screen(24, 80, draw)
    assert sizes == [(10, 50)]
    assert (command.screen.rows, command.screen.cols) == (10, 50)
    assert rows[0].startswith(" [web] running in /srv ")
    assert rows[1].startswith("red")
    assert rows[11].startswith(" Ctrl-]: Tab pane  q hide  k stop ")


def test_the_view_draws_under_the_commands_lock(view):
    """The reader thread feeds the same screen; it must wait for the draw."""
    pane, command = view
    held = []

    class Watching:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            held.append("in")
            return self.lock.__enter__()

        def __exit__(self, *exc):
            held.append("out")
            return self.lock.__exit__(*exc)

    command.lock = Watching(command.lock)
    with_curses_screen(24, 80, lambda stdscr: pane.draw(stdscr, 0, 0, 10, 40))
    # Taken around the whole draw (and again, nested, for the resize).
    assert held[0] == "in" and held[-1] == "out"
    assert held.count("in") == held.count("out")
    assert theme.attr("keybar")                      # the screen was real
