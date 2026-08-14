"""The process browser/killer plug-in, local and against a fake SSH client."""

from __future__ import annotations

import curses
import signal
import subprocess
import types

import pytest

from meridian_commander.plugin_api import PluginContext
from meridian_commander.plugins import processes as processes_mod
from meridian_commander.plugins.processes import (
    Process, ProcessesPlugin, _etime_seconds, parse_ps, PS_COLUMNS,
)

from support import with_curses_screen


RICH = PS_COLUMNS[0]
PLAIN = PS_COLUMNS[1]

PS_OUTPUT = """\
      1 root       0.0   0.1   1234    01:02:03 /sbin/init splash
    314 alice     12.5   4.0 204800  1-02:03:04 python3 train.py --epochs 5
    999 bob        3.2   1.0  51200       05:06 nginx: worker process
"""


# -- parsing -------------------------------------------------------------------

def test_parse_rich_output():
    procs = parse_ps(PS_OUTPUT, RICH)
    assert [p.pid for p in procs] == [1, 314, 999]
    p = procs[1]
    assert p.user == "alice"
    assert p.cpu == 12.5
    assert p.mem == 4.0
    assert p.rss == 204800
    assert p.etime == "1-02:03:04"
    assert p.command == "python3 train.py --epochs 5"


def test_parse_plain_output():
    procs = parse_ps("   7 root /usr/bin/daemon --flag\n", PLAIN)
    assert procs[0].pid == 7
    assert procs[0].command == "/usr/bin/daemon --flag"
    assert procs[0].cpu is None


def test_parse_skips_garbled_lines():
    text = "PID USER CPU MEM RSS TIME COMMAND\nnot-a-pid x 0 0 0 0 x\n" \
           "  5 root 0.0 0.0 100 00:01 ok\n"
    procs = parse_ps(text, RICH)
    assert [p.pid for p in procs] == [5]


def test_parse_skips_lines_with_too_few_fields():
    assert parse_ps("  12 root\n\n", RICH) == []


def test_parse_accepts_a_decimal_comma():
    procs = parse_ps("  5 root 0,5 1,5 100 00:01 x\n", RICH)
    assert procs[0].cpu == 0.5


def test_etime_parsing():
    assert _etime_seconds("05:06") == 306
    assert _etime_seconds("01:02:03") == 3723
    assert _etime_seconds("1-02:03:04") == 86400 + 7384
    assert _etime_seconds("") == 0
    assert _etime_seconds("bad") == 0
    assert _etime_seconds("x-00:01") == 0


# -- fakes ---------------------------------------------------------------------

class _FakeExecChannel:
    def __init__(self, status):
        self.status = status
        self.channel = self

    def recv_exit_status(self):
        return self.status


class _FakeStream(_FakeExecChannel):
    def __init__(self, data=b"", status=0):
        super().__init__(status)
        self._data = data

    def read(self):
        return self._data


class _FakeSSHClient:
    """Answers exec_command for ``ps`` and ``kill`` from a canned table."""

    def __init__(self, ps_out=PS_OUTPUT, ps_status=0, kill_status=0,
                 active=True):
        self.commands = []
        self.ps_out = ps_out
        self.ps_status = ps_status
        self.kill_status = kill_status
        self._active = active

    def get_transport(self):
        client = self

        class _Transport:
            def is_active(self):
                return client._active

        return _Transport()

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        if command.startswith("kill"):
            return None, _FakeStream(b"", self.kill_status), \
                _FakeStream(b"no such process", self.kill_status)
        return None, _FakeStream(self.ps_out.encode(), self.ps_status), \
            _FakeStream(b"ps: bad column", self.ps_status)


def _panel(scheme, client=None):
    fs = types.SimpleNamespace(scheme=scheme, _client=client,
                               label=lambda: f"{scheme}://host")
    return types.SimpleNamespace(fs=fs, path="/")


def _ctx(scheme="local", client=None):
    return PluginContext(app=None, own_panel=_panel(scheme, client),
                         other_panel=_panel("local"))


@pytest.fixture
def local_ps(monkeypatch):
    """Route the local backend's subprocess.run to canned ps output."""
    calls = []

    def install(out=PS_OUTPUT, returncode=0, stderr=""):
        def fake_run(argv, capture_output=None, text=None):
            calls.append(argv)
            return types.SimpleNamespace(stdout=out, stderr=stderr,
                                         returncode=returncode)

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    return install


# -- opening -------------------------------------------------------------------

def test_local_pane_lists_local_processes(local_ps):
    calls = local_ps()
    plugin = ProcessesPlugin(_ctx("local"))
    assert [p.pid for p in plugin.view][:1] == [314]   # sorted by CPU desc
    assert calls[0][:2] == ["ps", "-A"]
    assert "pid=" in calls[0]


def test_remote_pane_runs_ps_over_the_connection():
    client = _FakeSSHClient()
    plugin = ProcessesPlugin(_ctx("ssh", client))
    assert len(plugin.view) == 3
    assert client.commands[0].startswith("ps -A -o pid=")


def test_an_ftp_pane_is_refused():
    with pytest.raises(RuntimeError, match="FTP"):
        ProcessesPlugin(_ctx("ftp"))


def test_a_dead_ssh_connection_is_refused():
    client = _FakeSSHClient(active=False)
    with pytest.raises(RuntimeError, match="no longer active"):
        ProcessesPlugin(_ctx("sftp", client))


def test_a_failing_ps_shows_an_error_instead_of_crashing(local_ps):
    local_ps(out="", returncode=1, stderr="ps: not found")
    plugin = ProcessesPlugin(_ctx("local"))
    assert plugin.view == []
    assert "ps: not found" in plugin.error


def test_a_failing_remote_ps_shows_its_stderr():
    client = _FakeSSHClient(ps_status=1)
    plugin = ProcessesPlugin(_ctx("ssh", client))
    assert plugin.view == []
    assert "ps: bad column" in plugin.error


def test_falls_back_to_the_minimal_column_set(monkeypatch):
    """A ps that rejects the rich columns still yields a listing."""
    calls = []

    def fake_run(argv, capture_output=None, text=None):
        calls.append(argv)
        if "pcpu=" in argv:
            return types.SimpleNamespace(stdout="", stderr="bad -o",
                                         returncode=1)
        return types.SimpleNamespace(stdout="  9 root sh\n", stderr="",
                                     returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    plugin = ProcessesPlugin(_ctx("local"))
    assert [p.pid for p in plugin.view] == [9]
    assert plugin.columns == PLAIN
    assert len(calls) == 2


# -- sorting and filtering -----------------------------------------------------

@pytest.fixture
def plugin(local_ps):
    local_ps()
    return ProcessesPlugin(_ctx("local"))


def test_sorted_by_cpu_descending_by_default(plugin):
    assert [p.pid for p in plugin.view] == [314, 999, 1]


def test_sort_keys_change_the_order(plugin):
    plugin.handle_key(ord("p"))
    assert [p.pid for p in plugin.view] == [1, 314, 999]
    plugin.handle_key(ord("p"))            # same key flips the direction
    assert [p.pid for p in plugin.view] == [999, 314, 1]
    plugin.handle_key(ord("n"))
    assert plugin.view[0].command.startswith("/sbin/init")
    plugin.handle_key(ord("u"))
    assert plugin.view[0].user == "alice"
    plugin.handle_key(ord("t"))
    assert plugin.view[0].pid == 314       # longest running first
    plugin.handle_key(ord("m"))
    assert plugin.view[0].pid == 314


def test_the_cursor_follows_its_process_across_a_resort(plugin):
    plugin.handle_key(curses.KEY_DOWN)     # onto nginx (pid 999)
    plugin.handle_key(ord("p"))            # resort by pid
    assert plugin.view[plugin.cursor].pid == 999


def test_filtering_narrows_the_view(plugin):
    plugin.handle_key(ord("/"))
    assert plugin.mode == "filter"
    for ch in "nginx":
        plugin.handle_key(ord(ch))
    assert [p.pid for p in plugin.view] == [999]
    plugin.handle_key(10)                  # Enter keeps the filter
    assert plugin.mode == "list"
    assert plugin.filter == "nginx"


def test_filter_matches_the_user_too(plugin):
    plugin.handle_key(ord("/"))
    for ch in "alice":
        plugin.handle_key(ord(ch))
    assert [p.pid for p in plugin.view] == [314]


def test_escape_clears_the_filter(plugin):
    plugin.handle_key(ord("/"))
    plugin.handle_key(ord("z"))
    assert plugin.view == []
    plugin.handle_key(27)
    assert plugin.filter == ""
    assert len(plugin.view) == 3


def test_backspace_and_ctrl_u_edit_the_filter(plugin):
    plugin.handle_key(ord("/"))
    for ch in "ab":
        plugin.handle_key(ord(ch))
    plugin.handle_key(curses.KEY_BACKSPACE)
    assert plugin.filter == "a"
    plugin.handle_key(21)
    assert plugin.filter == ""


def test_tab_still_switches_panes_while_filtering(plugin):
    plugin.handle_key(ord("/"))
    assert plugin.handle_key(9) is None
    assert plugin.mode == "filter"


def test_the_filter_prompt_and_status_show_in_the_footer(plugin):
    plugin.handle_key(ord("/"))
    plugin.handle_key(ord("a"))
    assert plugin._footer() == " filter> a "
    plugin.handle_key(10)
    plugin.status = "sent SIGTERM to 5"
    assert "SIGTERM" in plugin._footer()


def test_the_refresh_key_reruns_ps(plugin, local_ps):
    calls = local_ps()
    before = len(calls)
    plugin.handle_key(ord("r"))
    assert len(calls) == before + 1


# -- navigation and closing ----------------------------------------------------

def test_navigation_keys_move_and_clamp(plugin):
    plugin.handle_key(curses.KEY_DOWN)
    assert plugin.cursor == 1
    plugin.handle_key(curses.KEY_NPAGE)
    assert plugin.cursor == 2              # clamped to the last row
    plugin.handle_key(curses.KEY_HOME)
    assert plugin.cursor == 0
    plugin.handle_key(curses.KEY_UP)
    assert plugin.cursor == 0
    plugin.handle_key(curses.KEY_END)
    assert plugin.cursor == 2
    plugin.handle_key(curses.KEY_PPAGE)
    assert plugin.cursor == 0


def test_tab_is_passed_to_the_application(plugin):
    assert plugin.handle_key(9) is None


def test_escape_closes_the_plugin(plugin):
    assert plugin.handle_key(27) is False


def test_f10_closes_the_plugin(plugin):
    assert plugin.handle_key(curses.KEY_F10) is False


# -- killing -------------------------------------------------------------------

def test_kill_asks_then_sends_sigterm_locally(plugin, monkeypatch):
    sent = []
    monkeypatch.setattr("os.kill", lambda pid, sig: sent.append((pid, sig)))
    plugin.handle_key(curses.KEY_F8)
    assert plugin.mode == "confirm"
    assert plugin.pending.pid == 314
    assert "314" in plugin._footer()
    plugin.handle_key(10)                  # Enter confirms with SIGTERM
    assert sent == [(314, signal.SIGTERM)]
    assert plugin.mode == "list"
    assert "SIGTERM" in plugin.status


@pytest.mark.parametrize("key,sig", [
    (ord("9"), signal.SIGKILL),
    (ord("1"), signal.SIGHUP),
    (ord("2"), signal.SIGINT),
    (ord("y"), signal.SIGTERM),
])
def test_the_signal_menu(plugin, monkeypatch, key, sig):
    sent = []
    monkeypatch.setattr("os.kill", lambda pid, s: sent.append(s))
    plugin.handle_key(ord("k"))
    plugin.handle_key(key)
    assert sent == [sig]


def test_a_confirm_with_nothing_pending_is_a_no_op(plugin):
    plugin.mode = "confirm"
    plugin.pending = None
    assert plugin.handle_key(10) is True
    assert plugin.mode == "list"


def test_escape_cancels_the_kill(plugin, monkeypatch):
    monkeypatch.setattr("os.kill",
                        lambda *a: pytest.fail("should not have killed"))
    plugin.handle_key(curses.KEY_DC)
    plugin.handle_key(27)
    assert plugin.mode == "list"
    assert plugin.status == ""


def test_a_denied_kill_is_reported_not_raised(plugin, monkeypatch):
    def deny(pid, sig):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr("os.kill", deny)
    plugin.handle_key(ord("k"))
    plugin.handle_key(10)
    assert "Operation not permitted" in plugin.status


def test_remote_kill_runs_kill_over_the_connection():
    client = _FakeSSHClient()
    plugin = ProcessesPlugin(_ctx("ssh", client))
    plugin.handle_key(ord("k"))
    plugin.handle_key(ord("9"))
    assert f"kill -{int(signal.SIGKILL)} 314" in client.commands
    assert "SIGKILL" in plugin.status


def test_a_failed_remote_kill_is_reported():
    client = _FakeSSHClient(kill_status=1)
    plugin = ProcessesPlugin(_ctx("ssh", client))
    plugin.handle_key(ord("k"))
    plugin.handle_key(10)
    assert "no such process" in plugin.status


# -- refreshing ----------------------------------------------------------------

def test_tick_refreshes_on_the_configured_cadence(plugin, monkeypatch):
    times = iter([100.0, 100.5, 103.0, 103.0])
    monkeypatch.setattr(processes_mod.time, "monotonic", lambda: next(times))
    plugin.refresh()                       # stamps 100.0
    before = plugin._last_refresh
    plugin.tick()                          # 0.5s later: too soon
    assert plugin._last_refresh == before
    plugin.tick()                          # 3s later: refreshes
    assert plugin._last_refresh == 103.0


def test_tick_is_paused_while_a_prompt_is_open(plugin, monkeypatch):
    monkeypatch.setattr(processes_mod.time, "monotonic",
                        lambda: plugin._last_refresh + 999)
    plugin.handle_key(ord("k"))
    before = plugin._last_refresh
    plugin.tick()
    assert plugin._last_refresh == before


def test_a_refresh_failure_keeps_the_old_listing(plugin, monkeypatch):
    def boom(argv, capture_output=None, text=None):
        raise OSError("connection lost")

    monkeypatch.setattr(subprocess, "run", boom)
    plugin.refresh()
    assert len(plugin.procs) == 3
    assert "connection lost" in plugin.error


# -- drawing -------------------------------------------------------------------

def test_draw_renders_header_rows_and_footer(plugin):
    def paint(stdscr):
        plugin.draw(stdscr, 1, 0, 20, 60)
        return [stdscr.instr(r, 0, 60).decode(errors="replace")
                for r in range(1, 21)]

    lines = with_curses_screen(24, 62, paint)
    assert "[processes]" in lines[0]
    assert "PID" in lines[1] and "COMMAND" in lines[1]
    assert any("nginx" in line for line in lines)
    assert "F8 kill" in lines[19]


def test_draw_scrolls_to_keep_the_cursor_visible(local_ps):
    rows = "\n".join(f"  {pid} u 0.0 0.0 10 00:01 cmd{pid}"
                     for pid in range(100, 140))
    local_ps(out=rows + "\n")
    plugin = ProcessesPlugin(_ctx("local"))

    def paint(stdscr):
        plugin.draw(stdscr, 0, 0, 10, 60)
        return [stdscr.instr(r, 0, 60).decode(errors="replace")
                for r in range(10)]

    def render():
        return with_curses_screen(24, 62, paint)

    plugin.handle_key(curses.KEY_END)
    assert any(f"cmd{plugin.view[-1].pid}" in line for line in render())
    plugin.handle_key(curses.KEY_HOME)     # scrolls back up to the top
    assert any(f"cmd{plugin.view[0].pid}" in line for line in render())
    assert plugin.scroll == 0


def test_draw_shows_the_error_when_there_is_nothing_to_list(local_ps):
    local_ps(out="", returncode=1, stderr="broken")
    plugin = ProcessesPlugin(_ctx("local"))

    def paint(stdscr):
        plugin.draw(stdscr, 0, 0, 10, 60)
        return stdscr.instr(2, 0, 60).decode(errors="replace")

    assert "broken" in with_curses_screen(24, 62, paint)


def test_draw_survives_a_tiny_pane(plugin):
    with_curses_screen(24, 62, lambda s: plugin.draw(s, 0, 0, 2, 10))
