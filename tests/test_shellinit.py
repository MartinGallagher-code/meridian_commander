"""Leaving the shell where you browsed to: --printwd and --shell-init."""

from __future__ import annotations

import curses
import os

import pytest

from meridian_commander import app as app_mod
from meridian_commander import shellinit
from meridian_commander.app import main

from support import _StubScreen, write


# -- the snippets --------------------------------------------------------------

@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_every_shell_gets_a_snippet(shell):
    text = shellinit.snippet(shell)
    assert "meridian_cd" in text
    assert "--printwd" in text


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_a_snippet_only_cds_when_there_is_somewhere_to_go(shell):
    """The empty-file guard is the whole contract with ``--printwd``.

    Without it a non-local pane would send the shell to whatever the last
    run happened to leave behind, or to the root of nowhere.
    """
    assert "-s " in shellinit.snippet(shell)


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_a_snippet_cleans_up_its_temporary_file(shell):
    assert "rm -f" in shellinit.snippet(shell)


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_no_snippet_declares_a_variable_its_shell_reserves(shell):
    """``status`` is read-only in zsh -- it is a name for ``$?`` -- so
    ``local wd status`` aborted the function on its fourth line and Ctrl-O
    did nothing at all. fish reserves it too. Nothing may use that name.
    """
    text = shellinit.snippet(shell)
    assert " status\n" not in text
    assert "status=" not in text


def test_shell_init_prints_the_snippet_and_does_not_start_curses(monkeypatch,
                                                                 capsys):
    """It has to work in a pipe: `eval "$(meridian --shell-init bash)"`."""
    def refuse(*args, **kwargs):
        raise AssertionError("curses must not be started")

    monkeypatch.setattr(curses, "wrapper", refuse)
    assert main(["--shell-init", "bash"]) == 0
    assert capsys.readouterr().out == shellinit.BASH


def test_an_unknown_shell_is_refused_by_the_parser(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--shell-init", "csh"])
    assert info.value.code == 2
    assert "csh" in capsys.readouterr().err


# -- which directory the shell should follow to --------------------------------

def _app(tmp_path):
    write(str(tmp_path / "left" / "file.txt"), "hello")
    (tmp_path / "right").mkdir()
    return app_mod.App(_StubScreen(), str(tmp_path / "left"),
                       str(tmp_path / "right"))


def test_a_local_pane_names_the_directory_to_follow_to(tmp_path, monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    app = _app(tmp_path)
    assert app.printwd_path() == str(tmp_path / "left")


def test_the_active_pane_decides(tmp_path, monkeypatch):
    """Whichever pane you were standing in when you quit is the one meant."""
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    app = _app(tmp_path)
    app.active = app.right
    assert app.printwd_path() == str(tmp_path / "right")


def test_a_pane_that_is_not_local_names_nowhere(tmp_path, monkeypatch):
    """An SFTP pane is a connection and an archive pane is a file inside one;
    no shell can cd to either, so the shell stays where it was."""
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    app = _app(tmp_path)

    class _NotLocal:      # as SFTP, FTP and archive backends all are
        path = "/srv"

    app.active.fs = _NotLocal()
    assert app.printwd_path() is None


# -- writing the file ----------------------------------------------------------

def test_the_directory_is_written_without_a_trailing_newline(tmp_path):
    """`$(cat ...)` would strip one anyway, and leaving it off keeps a
    directory whose name really ends in a newline unambiguous."""
    target = tmp_path / "wd"
    app_mod._write_printwd(str(target), "/home/someone/projects")
    assert target.read_text() == "/home/someone/projects"


def test_nothing_is_written_when_there_is_nowhere_to_go(tmp_path):
    """The file stays empty, which is what the snippet's -s test looks for."""
    target = tmp_path / "wd"
    target.write_text("")
    app_mod._write_printwd(str(target), None)
    assert target.read_text() == ""


def test_a_file_that_cannot_be_written_is_reported_not_raised(tmp_path,
                                                              capsys):
    """The browsing is done and the terminal is back; failing to exit over a
    temporary file would be worse than a shell that did not move."""
    app_mod._write_printwd(str(tmp_path / "no-such-dir" / "wd"), "/tmp")
    assert "could not write" in capsys.readouterr().err


# -- the whole route through main() --------------------------------------------

def _curses_free(monkeypatch, final_dir):
    """Run main() without a terminal, with _main answering ``final_dir``."""
    monkeypatch.setattr(curses, "wrapper", lambda fn, args: final_dir)


def test_printwd_writes_where_the_app_finished(monkeypatch, tmp_path):
    target = tmp_path / "wd"
    _curses_free(monkeypatch, "/home/someone/notes")
    assert main(["--printwd", str(target)]) == 0
    assert target.read_text() == "/home/someone/notes"


def test_printwd_writes_nothing_for_a_remote_pane(monkeypatch, tmp_path):
    target = tmp_path / "wd"
    target.write_text("")
    _curses_free(monkeypatch, None)
    assert main(["--printwd", str(target)]) == 0
    assert target.read_text() == ""


def test_an_interrupted_session_leaves_the_shell_where_it_was(monkeypatch,
                                                              tmp_path):
    """Ctrl-C is not a choice of directory."""
    target = tmp_path / "wd"
    target.write_text("")

    def interrupted(fn, args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(curses, "wrapper", interrupted)
    assert main(["--printwd", str(target)]) == 0
    assert target.read_text() == ""


def test_without_printwd_no_file_is_touched(monkeypatch, tmp_path):
    """The flag is opt-in: the default run writes nothing anywhere."""
    _curses_free(monkeypatch, str(tmp_path))
    before = sorted(os.listdir(tmp_path))
    assert main([]) == 0
    assert sorted(os.listdir(tmp_path)) == before
