"""Running files from the panels: Enter, the Run menu entry, and runner.py."""

from __future__ import annotations

import curses
import os
import subprocess
import types

import pytest

from meridian_commander import runner
from meridian_commander.runner import parse_shebang, read_head, run_argv

from support import _ScriptedDialogs, write
from test_app_keys import _FtpLike, _SftpLike


# -- parse_shebang ---------------------------------------------------------------

@pytest.mark.parametrize("head, argv", [
    (b"#!/bin/sh\necho hi\n", ["/bin/sh"]),
    (b"#!/usr/bin/env python3\nprint()\n", ["/usr/bin/env", "python3"]),
    (b"#! /bin/sh\n", ["/bin/sh"]),
    (b"#!/bin/sh -eu\n", ["/bin/sh", "-eu"]),
    (b"#!/bin/sh\r\necho hi\r\n", ["/bin/sh"]),
    (b"#!/bin/sh", ["/bin/sh"]),           # no newline in the head at all
    (b"echo hi\n", None),
    (b"#!\n", None),
    (b"#!   \n", None),
    (b"", None),
    (b"\x7fELF\x02\x01", None),
])
def test_parse_shebang(head, argv):
    assert parse_shebang(head) == argv


# -- read_head / run_argv ---------------------------------------------------------

class _Stream:
    def __init__(self, data=b"", fail_close=False):
        self._data = data
        self._fail_close = fail_close

    def read(self, n):
        return self._data[:n]

    def close(self):
        if self._fail_close:
            raise OSError("already closed")


class _FakeFS:
    def __init__(self, data=b"", fail_open=False, fail_close=False):
        self.data = data
        self.fail_open = fail_open
        self.fail_close = fail_close
        self.opened = 0

    def open_read(self, path):
        self.opened += 1
        if self.fail_open:
            raise OSError("unreadable")
        return _Stream(self.data, self.fail_close)


def test_read_head_is_bounded():
    fs = _FakeFS(b"x" * 4096)
    assert len(read_head(fs, "/f")) == runner.SHEBANG_BYTES


def test_read_head_survives_a_failing_close_and_empty_read():
    assert read_head(_FakeFS(b"", fail_close=True), "/f") == b""


def test_run_argv_runs_an_executable_without_reading_it():
    fs = _FakeFS(fail_open=True)      # a read would blow up -- none happens
    assert run_argv(fs, "/bin/tool", 0o755) == ["/bin/tool"]
    assert fs.opened == 0


def test_run_argv_uses_the_shebang_for_a_plain_file():
    fs = _FakeFS(b"#!/usr/bin/env python3\n")
    assert run_argv(fs, "/f.py", 0o644) == ["/usr/bin/env", "python3", "/f.py"]


def test_run_argv_declines_a_file_with_neither():
    assert run_argv(_FakeFS(b"just text\n"), "/notes.txt", 0o644) is None


def test_run_argv_declines_an_unreadable_file():
    assert run_argv(_FakeFS(fail_open=True), "/f", 0o644) is None


# -- running through the interface -------------------------------------------------

def _cursor_to(panel, name: str) -> None:
    panel.cursor = [e.name for e in panel.entries].index(name)


@pytest.fixture
def run_env(app, monkeypatch):
    """Route the suspend-and-run machinery through fakes.

    Returns a namespace with the recorded subprocess calls, everything written
    to the raw terminal, the number of return-prompts shown, and knobs for the
    exit status / a startup failure.
    """
    env = types.SimpleNamespace(calls=[], written=[], pauses=0,
                                status=0, boom=None)

    def fake_call(cmd, cwd=None):
        env.calls.append((cmd, cwd))
        if env.boom is not None:
            raise env.boom
        return env.status

    monkeypatch.setattr(subprocess, "call", fake_call)
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    monkeypatch.setattr(os, "write",
                        lambda fd, data: env.written.append(data) or len(data))
    monkeypatch.setattr("builtins.input",
                        lambda prompt="": env.__setattr__(
                            "pauses", env.pauses + 1) or "")
    monkeypatch.setattr(app.stdscr, "clearok", lambda flag: None,
                        raising=False)
    monkeypatch.setattr(app.stdscr, "refresh", lambda: None, raising=False)
    return env


def _script(tmp_path, name="job.sh", body="#!/bin/sh\necho hi\n",
            mode=0o755) -> str:
    path = tmp_path / "left" / name
    write(str(path), body)
    os.chmod(path, mode)
    return str(path)


def test_enter_runs_an_executable_script(app, tmp_path, run_env, monkeypatch):
    path = _script(tmp_path)
    scripted = _ScriptedDialogs(monkeypatch, prompt=[""])
    app.left.refresh()
    _cursor_to(app.left, "job.sh")

    app.handle_key(10)
    assert run_env.calls == [([path], str(tmp_path / "left"))]
    assert "job.sh" in scripted.prompts[0]
    assert run_env.pauses == 1
    assert "job.sh exited 0" in app.message


def test_enter_uses_the_shebang_when_not_executable(app, tmp_path, run_env,
                                                    monkeypatch):
    path = _script(tmp_path, name="tool.py",
                   body="#!/usr/bin/env python3\nprint('hi')\n", mode=0o644)
    _ScriptedDialogs(monkeypatch, prompt=[""])
    app.left.refresh()
    _cursor_to(app.left, "tool.py")

    app.handle_key(10)
    assert run_env.calls[0][0] == ["/usr/bin/env", "python3", path]


def test_arguments_are_split_like_a_shell(app, tmp_path, run_env, monkeypatch):
    path = _script(tmp_path)
    _ScriptedDialogs(monkeypatch, prompt=['--fast "two words"'])
    app.left.refresh()
    _cursor_to(app.left, "job.sh")

    app.handle_key(10)
    assert run_env.calls[0][0] == [path, "--fast", "two words"]


def test_cancelling_the_arguments_dialog_runs_nothing(app, tmp_path, run_env,
                                                      monkeypatch):
    _script(tmp_path)
    _ScriptedDialogs(monkeypatch, prompt=[None])
    app.left.refresh()
    _cursor_to(app.left, "job.sh")

    app.handle_key(10)
    assert run_env.calls == []


def test_unbalanced_quotes_are_refused(app, tmp_path, run_env, monkeypatch):
    _script(tmp_path)
    scripted = _ScriptedDialogs(monkeypatch, prompt=['"oops'])
    app.left.refresh()
    _cursor_to(app.left, "job.sh")

    app.handle_key(10)
    assert run_env.calls == []
    assert "Bad arguments" in scripted.last_message


def test_a_plain_file_still_opens_the_viewer(app, tmp_path, run_env,
                                             monkeypatch):
    opened = []

    def fake_viewer(fs, path):
        opened.append(path)
        return types.SimpleNamespace(run=lambda stdscr: None)

    monkeypatch.setattr("meridian_commander.app.viewer_for", fake_viewer)
    app.left.refresh()
    _cursor_to(app.left, "file.txt")

    app.handle_key(10)
    assert run_env.calls == []
    assert opened and opened[0].endswith("file.txt")


def test_a_nonzero_exit_status_is_reported(app, tmp_path, run_env,
                                           monkeypatch):
    _script(tmp_path)
    _ScriptedDialogs(monkeypatch, prompt=[""])
    run_env.status = 3
    app.left.refresh()
    _cursor_to(app.left, "job.sh")

    app.handle_key(10)
    assert any(b"exit status 3" in w for w in run_env.written)
    assert "job.sh exited 3" in app.message


def test_a_program_that_cannot_start_is_reported(app, tmp_path, run_env,
                                                 monkeypatch):
    _script(tmp_path)
    _ScriptedDialogs(monkeypatch, prompt=[""])
    run_env.boom = OSError("no interpreter")
    app.left.refresh()
    _cursor_to(app.left, "job.sh")

    app.handle_key(10)
    assert any(b"Could not run job.sh" in w for w in run_env.written)
    assert "could not start" in app.message


def test_a_remote_file_runs_over_ssh(app, tmp_path, run_env, monkeypatch):
    _script(tmp_path)
    _ScriptedDialogs(monkeypatch, prompt=["--check"])
    backend = _SftpLike(host="web1", username="deploy", port=2222)
    app.left.set_location(backend, str(tmp_path / "left"))
    _cursor_to(app.left, "job.sh")

    app.handle_key(10)
    cmd = run_env.calls[0][0]
    assert cmd[:2] == ["ssh", "-t"]
    assert "-p" in cmd and "2222" in cmd
    assert "deploy@web1" in cmd
    assert run_env.calls[0][1] is None      # the cd happens remotely
    remote = cmd[-1]
    assert remote.startswith("cd ")
    assert "job.sh" in remote and "--check" in remote


def test_an_executable_on_an_ftp_pane_views_instead(app, tmp_path, run_env,
                                                    monkeypatch):
    _script(tmp_path)
    opened = []

    def fake_viewer(fs, path):
        opened.append(path)
        return types.SimpleNamespace(run=lambda stdscr: None)

    monkeypatch.setattr("meridian_commander.app.viewer_for", fake_viewer)
    app.left.set_location(_FtpLike(), str(tmp_path / "left"))
    _cursor_to(app.left, "job.sh")

    app.handle_key(10)
    assert run_env.calls == []
    assert opened                          # fell back to the viewer


# -- the Run menu entry ---------------------------------------------------------

def test_menu_run_on_a_directory_asks_for_a_file(app, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch)
    _cursor_to(app.left, "sub")
    app._dispatch("run")
    assert "Point the cursor at a file" in scripted.last_message


def test_menu_run_on_the_parent_entry_asks_for_a_file(app, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch)
    _cursor_to(app.left, "..")
    app._dispatch("run")
    assert "Point the cursor at a file" in scripted.last_message


def test_menu_run_on_a_plain_file_says_why_not(app, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch)
    _cursor_to(app.left, "file.txt")
    app._dispatch("run")
    assert "not runnable" in scripted.last_message


def test_menu_run_runs_the_cursor_file(app, tmp_path, run_env, monkeypatch):
    path = _script(tmp_path)
    _ScriptedDialogs(monkeypatch, prompt=[""])
    app.left.refresh()
    _cursor_to(app.left, "job.sh")
    app._dispatch("run")
    assert run_env.calls[0][0] == [path]


def test_context_menu_offers_run(app, tmp_path, run_env, monkeypatch):
    path = _script(tmp_path)
    _ScriptedDialogs(monkeypatch, menu=["Run"], prompt=[""])
    app.left.refresh()
    _cursor_to(app.left, "job.sh")
    app._context_menu()
    assert run_env.calls[0][0] == [path]
