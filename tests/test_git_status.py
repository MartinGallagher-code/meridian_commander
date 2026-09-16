# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The Git built-in plug-in."""

from __future__ import annotations

import os
import subprocess

import pytest

from meridian_commander.plugin_api import Command
from meridian_commander.plugins import git_status
from meridian_commander.plugins.git_status import GitStatus


def _run(ctx, command=""):
    result = GitStatus(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


def _git(path, *args):
    return subprocess.run(["git", *args], cwd=str(path), check=True,
                          capture_output=True, text=True)


def _init_repo(path):
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Tester"],
                 ["config", "commit.gpgsign", "false"]):
        _git(path, *args)


def _commit(path, name="a.txt", text="hello\n", message="a commit"):
    (path / name).write_text(text)
    _git(path, "add", name)
    _git(path, "commit", "-qm", message)


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


def test_a_hanging_git_is_killed_and_reported(tmp_path, monkeypatch):
    """The backstop: whatever else happens, the pane comes back.

    The prompt-disabling environment is meant to stop git waiting for an
    answer in the first place, but it cannot be proved to cover every case;
    the timeout is the one that can.
    """
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\nsleep 30\n")
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")

    rc, out, err = git_status._git(["push"], str(tmp_path), timeout=0.5)
    assert rc is None
    assert "gave up after 0.5s" in err


# -- the environment git runs in ----------------------------------------------

def test_the_environment_refuses_every_prompt():
    env = git_status._env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["SSH_ASKPASS_REQUIRE"] == "never"
    assert env["GIT_EDITOR"] == "true"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_a_users_own_ssh_command_is_left_alone(monkeypatch):
    """Someone who set GIT_SSH_COMMAND has said how ssh should run."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /my/key")
    assert git_status._env()["GIT_SSH_COMMAND"] == "ssh -i /my/key"


@pytest.mark.parametrize("stderr, expected", [
    ("fatal: could not read Username for 'https://x'", "credential"),
    ("terminal prompts disabled", "credential"),
    ("git@x: Permission denied (publickey).", "ssh-agent"),
    ("Host key verification failed.", "known_hosts"),
    ("some other failure", ""),
])
def test_network_failures_get_a_useful_hint(stderr, expected):
    assert expected in git_status._network_hint(stderr)


# -- non-local panes -----------------------------------------------------------

def test_greeting_and_process_decline_a_remote_pane(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1"})
    monkeypatch.setattr(ctx.other_fs, "scheme", "sftp")
    assert "local pane only" in GitStatus(ctx).greeting
    assert "local pane only" in GitStatus(ctx).process("status")


def test_greeting_shows_the_summary(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1"})
    _init_repo(tmp_path / "data")
    greeting = GitStatus(ctx).greeting
    assert "Repository" in greeting
    assert "F2" in greeting


# -- argument validation (no git needed) --------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("log lots", "usage: log"),
    ("add", "usage: add"),
    ("commit", "usage: commit"),
    ("switch", "usage: switch"),
])
def test_usage_errors(data_ctx, line, expected):
    ctx = data_ctx({"a.txt": "1"})
    assert expected in GitStatus(ctx).process(line)


def test_unknown_command(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "unknown command" in GitStatus(ctx).process("wobble")


def test_a_failure_to_run_git_is_reported(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1"})
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (None, "", "boom"))
    assert "Could not run git" in _run(ctx, "status")
    assert "Could not run git" in _run(ctx, "log")


def test_a_directory_that_is_not_a_repository(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "not a git repository" in _run(ctx, "status")


def test_a_status_failure_with_no_message(data_ctx, monkeypatch):
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (3, "", ""))
    assert "git exited 3" in _run(data_ctx({"a.txt": "1"}), "status")


# -- reading a repository ------------------------------------------------------

def test_the_summary_of_a_fresh_repository(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "hello\n"})
    _init_repo(tmp_path / "data")
    out = _run(ctx, "status")
    assert "no commits yet" in out
    assert "?? a.txt" in out


def test_the_summary_counts_and_lists_what_changed(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "one\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "one\n", "first commit")

    (data / "a.txt").write_text("two\n")
    _git(data, "add", "a.txt")
    (data / "b.txt").write_text("new\n")

    out = _run(ctx, "status")
    assert "1 staged, 1 untracked" in out
    assert "first commit" in out          # the last-commit row
    assert "M  a.txt" in out              # git's own short format
    assert "?? b.txt" in out


def test_the_summary_says_when_the_tree_is_clean(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "one\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "one\n")
    assert "clean" in _run(ctx, "status")


def test_a_long_list_of_changes_is_capped(data_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(git_status, "MAX_FILES", 2)
    ctx = data_ctx({f"f{i}.txt": "x" for i in range(5)})
    _init_repo(tmp_path / "data")
    out = _run(ctx, "status")
    assert "and 3 more" in out


def test_a_merge_in_progress_is_announced(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "base\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "f.txt", "base\n", "base")
    _git(data, "checkout", "-q", "-b", "side")
    _commit(data, "f.txt", "side\n", "side")
    _git(data, "checkout", "-q", "main")
    _commit(data, "f.txt", "main\n", "main")
    subprocess.run(["git", "merge", "side"], cwd=str(data),
                   capture_output=True)

    out = _run(ctx, "status")
    assert "In progress" in out and "merge" in out
    assert "1 conflicted" in out


def test_a_detached_head_is_named(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "1\n")
    _git(data, "checkout", "-q", "--detach", "HEAD")
    assert "detached HEAD" in _run(ctx, "status")


# -- the pieces of the summary, in isolation -----------------------------------

@pytest.mark.parametrize("info, expected", [
    ({"detached": True}, "detached HEAD"),
    ({"head": "main", "initial": True}, "no commits yet"),
    ({"head": "main", "upstream": ""}, "no upstream"),
    ({"head": "m", "upstream": "o/m", "ahead": 0, "behind": 0}, "up to date"),
    ({"head": "m", "upstream": "o/m", "ahead": 2, "behind": 0}, "2 ahead"),
    ({"head": "m", "upstream": "o/m", "ahead": 0, "behind": 3}, "3 behind"),
    ({"head": "m", "upstream": "o/m", "ahead": 2, "behind": 3}, "diverged"),
    ({"head": "", "upstream": ""}, "(unknown)"),
])
def test_the_branch_line(info, expected):
    base = {"detached": False, "initial": False, "head": "", "upstream": "",
            "ahead": 0, "behind": 0}
    base.update(info)
    assert expected in git_status._branch_text(base)


def test_the_working_tree_line():
    counts = {"conflicts": 1, "staged": 2, "modified": 3, "untracked": 4}
    text = git_status._tree_text(counts)
    assert text == "1 conflicted, 2 staged, 3 modified, 4 untracked"
    assert git_status._tree_text(dict.fromkeys(counts, 0)) == "clean"


def test_parsing_a_rename_keeps_the_new_name():
    """A "2" line carries both names, tab-separated; the first is current."""
    text = ("# branch.oid abc123\n"
            "# branch.head main\n"
            "# branch.upstream origin/main\n"
            "# branch.ab +1 -2\n"
            "2 R. N... 100644 100644 100644 aaa bbb R100 new.txt\told.txt\n")
    info = git_status._parse_status(text)
    assert info["files"] == [("R.", "new.txt")]
    assert info["ahead"] == 1 and info["behind"] == 2
    assert info["staged"] == 1 and info["modified"] == 0


def test_a_real_rename_shows_the_name_the_file_has_now(data_ctx, tmp_path):
    """Against git itself, not against a string this file made up.

    The synthetic case above encodes my reading of the format; this one
    encodes git's.  They disagreed once already.
    """
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "old.txt", "content\n")
    _git(data, "mv", "old.txt", "new.txt")

    out = _run(ctx, "status")
    assert "R  new.txt" in out
    assert "R100" not in out


def test_the_last_commit_row_copes_with_odd_output(monkeypatch, tmp_path):
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (1, "", ""))
    assert git_status._last_commit(str(tmp_path)) == ""
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (0, "only-one\n", ""))
    assert git_status._last_commit(str(tmp_path)) == ""


def test_the_stash_row(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "one\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "one\n")
    assert "Stash" not in _run(ctx, "status")

    (data / "a.txt").write_text("two\n")
    _run(ctx, "stash")
    out = _run(ctx, "status")
    assert "1 entry" in out

    (data / "a.txt").write_text("three\n")
    _run(ctx, "stash")
    assert "2 entries" in _run(ctx, "status")

    # Popping brings the change back, so the tree is dirty again -- and the
    # summary printed after it says so.
    assert "1 modified" in _run(ctx, "pop")


def test_the_stash_count_survives_a_git_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (1, "", ""))
    assert git_status._stash_count(str(tmp_path)) == 0


def test_the_remote_row(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "1\n")
    assert "Remote" not in _run(ctx, "status")

    _git(data, "remote", "add", "upstream", "/somewhere/else")
    out = _run(ctx, "status")
    assert "upstream" in out and "/somewhere/else" in out

    # With an origin among them, origin is the one reported.
    _git(data, "remote", "add", "origin", "/the/origin")
    assert "/the/origin" in _run(ctx, "status")


def test_the_remote_row_survives_git_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (1, "", ""))
    assert git_status._remote_url(str(tmp_path)) == ""
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (0, "", ""))
    assert git_status._remote_url(str(tmp_path)) == ""

    calls = []

    def one_remote_no_url(argv, cwd, timeout=None):
        calls.append(argv)
        return (0, "origin\n", "") if argv == ["remote"] else (0, "  \n", "")

    monkeypatch.setattr(git_status, "_git", one_remote_no_url)
    assert git_status._remote_url(str(tmp_path)) == ""


@pytest.mark.parametrize("marker, label", [
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("BISECT_LOG", "bisect"),
])
def test_every_half_finished_operation_is_recognised(tmp_path, marker, label):
    _init_repo(tmp_path)
    (tmp_path / ".git" / marker).write_text("")
    assert git_status._in_progress(str(tmp_path)) == label


def test_nothing_in_progress_in_a_quiet_repository(tmp_path):
    _init_repo(tmp_path)
    assert git_status._in_progress(str(tmp_path)) == ""


def test_in_progress_copes_with_an_absolute_git_dir(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    (tmp_path / ".git" / "MERGE_HEAD").write_text("")
    absolute = str(tmp_path / ".git")
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (0, absolute, ""))
    assert git_status._in_progress(str(tmp_path)) == "merge"


def test_in_progress_gives_up_quietly_when_git_does(tmp_path, monkeypatch):
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (1, "", ""))
    assert git_status._in_progress(str(tmp_path)) == ""
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (0, "  \n", ""))
    assert git_status._in_progress(str(tmp_path)) == ""


# -- the branch picker ---------------------------------------------------------

def test_switch_offers_the_branches(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "1\n")
    _git(data, "branch", "other")
    plugin = GitStatus(ctx)
    assert sorted(plugin.command_options(Command("switch", arg="options"))) \
        == ["main", "other"]


def test_the_picker_declines_when_it_has_nothing_to_offer(data_ctx,
                                                          monkeypatch):
    ctx = data_ctx({"a.txt": "1\n"})
    plugin = GitStatus(ctx)
    # Another command's menu is not the branch picker's business.
    assert plugin.command_options(Command("log")) is None
    # Not a repository: git fails, and there is nothing to list.
    assert plugin.command_options(Command("switch", arg="options")) is None

    monkeypatch.setattr(ctx.other_fs, "scheme", "sftp")
    assert plugin.command_options(Command("switch", arg="options")) is None


def test_the_picker_offers_nothing_for_a_repository_with_no_branches(
        data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1\n"})
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (0, "\n", ""))
    assert GitStatus(ctx).command_options(
        Command("switch", arg="options")) is None


# -- push ----------------------------------------------------------------------

def test_push_passes_an_explicit_destination_through(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    argv, note = GitStatus(ctx)._push_argv(str(tmp_path), ["origin", "main"])
    assert argv == ["push", "origin", "main"]
    assert note is None


def test_push_sets_the_upstream_the_first_time(data_ctx, tmp_path):
    """The longer command was the only thing that could happen next."""
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "1\n")
    _git(data, "remote", "add", "origin", str(tmp_path / "bare"))

    argv, note = GitStatus(ctx)._push_argv(str(data), [])
    assert argv == ["push", "--set-upstream", "origin", "main"]
    assert "no upstream yet" in note


def test_push_is_plain_once_an_upstream_exists(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "main")
    _init_repo(data)
    _commit(data, "a.txt", "1\n")
    _git(data, "remote", "add", "origin", str(bare))
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(data),
                   capture_output=True)

    argv, note = GitStatus(ctx)._push_argv(str(data), [])
    assert argv == ["push"]
    assert note is None


def test_push_refuses_what_it_cannot_work_out(data_ctx, tmp_path,
                                              monkeypatch):
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "1\n")
    plugin = GitStatus(ctx)

    # No remote at all.
    argv, why = plugin._push_argv(str(data), [])
    assert argv is None and "No remote" in why

    # Detached: there is no branch to push.
    _git(data, "checkout", "-q", "--detach", "HEAD")
    argv, why = plugin._push_argv(str(data), [])
    assert argv is None and "detached" in why
    _git(data, "checkout", "-q", "main")

    # Several remotes and no obvious one.
    _git(data, "remote", "add", "alpha", "/a")
    _git(data, "remote", "add", "beta", "/b")
    argv, why = plugin._push_argv(str(data), [])
    assert argv is None and "say which" in why

    # And when the repository cannot be read at all.
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (None, "", "boom"))
    argv, why = plugin._push_argv(str(data), [])
    assert argv is None and "Could not run git" in why


def test_push_declines_rather_than_running_a_doomed_command(data_ctx,
                                                            tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "1\n")
    assert "No remote" in _run(ctx, "push")


def test_push_reports_a_failure_with_a_hint(data_ctx, tmp_path, monkeypatch):
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "1\n")
    _git(data, "remote", "add", "origin", "https://example.invalid/x.git")

    def refuse(argv, cwd, timeout=None):
        if argv[0] == "push":
            return 128, "", "fatal: could not read Username for 'https://x'"
        return git_status._git.__wrapped__(argv, cwd, timeout) \
            if hasattr(git_status._git, "__wrapped__") else real(argv, cwd,
                                                                 timeout)

    real = git_status._git
    monkeypatch.setattr(git_status, "_git", refuse)
    out = _run(ctx, "push")
    assert "git exited 128" in out
    assert "credential" in out


def test_a_network_failure_without_a_known_cause_adds_no_hint(data_ctx,
                                                              tmp_path,
                                                              monkeypatch):
    ctx = data_ctx({"a.txt": "1\n"})
    monkeypatch.setattr(git_status, "_git",
                        lambda argv, cwd, timeout=None: (1, "", "odd"))
    out = _run(ctx, "fetch")
    assert "git exited 1" in out


# -- end to end against a real repository -------------------------------------

def test_status_add_commit_log_end_to_end(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "hello\n"})
    _init_repo(tmp_path / "data")
    plugin = GitStatus(ctx)

    assert "a.txt" in "\n".join(plugin.process("status"))

    plugin.process("add a.txt")
    commit_out = "\n".join(plugin.process("commit first commit"))
    assert "first commit" in commit_out
    # A commit leaves the summary behind it, not just git's own line.
    assert "Working tree" in commit_out

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
    plugin.process("unstage")
    status = "\n".join(plugin.process("status"))
    assert "?? b.txt" in status


def test_switching_branches_reloads_the_file_pane(data_ctx, tmp_path):
    """The listing next door is stale the moment HEAD moves."""
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    _init_repo(data)
    _commit(data, "a.txt", "1\n")
    _git(data, "branch", "other")

    reloaded = []
    ctx.other_panel.refresh = lambda *a, **k: reloaded.append(True)

    out = "\n".join(GitStatus(ctx).process("switch other"))
    assert reloaded, "the other pane was never reloaded"
    assert "other" in out


def test_a_git_error_shows_the_exit_code(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    _init_repo(tmp_path / "data")
    plugin = GitStatus(ctx)
    out = "\n".join(plugin.process("commit nothing here"))
    assert "git exited" in out


def test_fetch_and_pull_against_a_real_remote(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1\n"})
    data = tmp_path / "data"
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "main")
    _init_repo(data)
    _commit(data, "a.txt", "1\n", "c1")
    _git(data, "remote", "add", "origin", str(bare))
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(data),
                   capture_output=True)

    plugin = GitStatus(ctx)
    assert "git exited" not in "\n".join(plugin.process("fetch"))
    # Nothing new upstream, so the pull is a no-op that still redraws.
    assert "Working tree" in "\n".join(plugin.process("pull"))
