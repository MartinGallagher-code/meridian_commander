"""The Git built-in plug-in."""

from __future__ import annotations

import subprocess

import pytest

from meridian_commander.plugins import git_status
from meridian_commander.plugins.git_status import GitStatus


def _run(ctx, command=""):
    result = GitStatus(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


def _init_repo(path):
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Tester"],
                 ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git"] + args, cwd=str(path), check=True,
                       capture_output=True)


# -- the git runner, in isolation ---------------------------------------------

def test_git_runner_runs_a_command(tmp_path):
    rc, out, err = git_status._git(["--version"], str(tmp_path))
    assert rc == 0
    assert "git version" in out


def test_git_runner_reports_a_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", boom)
    rc, out, err = git_status._git(["status"], ".")
    assert rc is None
    assert "not found" in err


def test_git_runner_reports_an_os_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr(subprocess, "run", boom)
    rc, out, err = git_status._git(["status"], ".")
    assert rc is None
    assert "no exec" in err


# -- non-local panes -----------------------------------------------------------

def test_greeting_and_process_decline_a_remote_pane(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1"})
    monkeypatch.setattr(ctx.other_fs, "scheme", "sftp")
    assert "local pane only" in GitStatus(ctx).greeting
    assert "local pane only" in GitStatus(ctx).process("status")


def test_greeting_names_the_repository(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "Repository:" in GitStatus(ctx).greeting


# -- argument validation (no git needed) --------------------------------------

def test_log_rejects_a_bad_count(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "usage: log" in GitStatus(ctx).process("log lots")


def test_add_needs_a_path(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "usage: add" in GitStatus(ctx).process("add")


def test_commit_needs_a_message(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "usage: commit" in GitStatus(ctx).process("commit")


def test_unknown_command(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "unknown command" in GitStatus(ctx).process("wobble")


def test_a_failure_to_run_git_is_reported(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1"})
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd: (None, "", "git not found"))
    assert "Could not run git" in GitStatus(ctx).process("status")


# -- end to end against a real repository -------------------------------------

def test_status_add_commit_log_end_to_end(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "hello\n"})
    _init_repo(tmp_path / "data")
    plugin = GitStatus(ctx)

    # A brand-new file shows as untracked.
    assert "a.txt" in "\n".join(plugin.process("status"))

    plugin.process("add a.txt")
    commit_out = "\n".join(plugin.process("commit first commit"))
    assert "first commit" in commit_out

    log_out = "\n".join(plugin.process("log"))
    assert "first commit" in log_out


def test_log_takes_a_count(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    _init_repo(tmp_path / "data")
    plugin = GitStatus(ctx)
    plugin.process("add a.txt")
    plugin.process("commit c1")
    out = "\n".join(plugin.process("log 5"))
    assert "c1" in out


def test_diff_shows_changes_and_is_empty_when_clean(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "one\n"})
    _init_repo(tmp_path / "data")
    plugin = GitStatus(ctx)
    plugin.process("add a.txt")
    plugin.process("commit c1")

    # Clean tree: diff has nothing to show.
    assert "nothing to show" in "\n".join(plugin.process("diff"))

    (tmp_path / "data" / "a.txt").write_text("two\n")
    diff_out = "\n".join(plugin.process("diff a.txt"))
    assert "-one" in diff_out and "+two" in diff_out


def test_branch_and_unstage(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    _init_repo(tmp_path / "data")
    plugin = GitStatus(ctx)
    plugin.process("add a.txt")
    plugin.process("commit c1")

    assert "main" in "\n".join(plugin.process("branch"))

    (tmp_path / "data" / "b.txt").write_text("new\n")
    plugin.process("add b.txt")
    # After unstaging, b.txt is back to untracked in the short status.
    plugin.process("unstage")
    status = "\n".join(plugin.process("status"))
    assert "?? b.txt" in status


def test_a_git_error_shows_the_exit_code(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    _init_repo(tmp_path / "data")
    plugin = GitStatus(ctx)
    # Nothing staged: git commit exits non-zero and says so.
    out = "\n".join(plugin.process("commit nothing here"))
    assert "git exited" in out
