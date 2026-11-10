# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Copies and moves that ask before replacing, keep modes and keep links.

MC-011.  Before it, F5 onto a file that existed replaced it without a word,
every copy came out with the default mode -- a script copied to a server lost
its execute bit -- and a link was copied as the file it pointed at.
"""

from __future__ import annotations

import curses
import os
import stat as stat_mod
import time

import pytest

from meridian_commander import app as app_mod
from meridian_commander import dialogs
from meridian_commander.app import TransferConflicts
from meridian_commander.filesystems import (
    DirEntry,
    FileSystem,
    FileSystemError,
    LocalFileSystem,
)
from meridian_commander.operations import (
    OVERWRITE,
    SKIP,
    OperationCancelled,
    TransferReport,
    copy_path,
    move_path,
)

from support import _ScriptedDialogs, read, run_menu, scripted_menu, \
    with_curses_screen, write, write_zip


def _mode(path) -> int:
    return stat_mod.S_IMODE(os.lstat(path).st_mode)


def _exec_file(path, content="#!/bin/sh\necho hi\n", mode=0o755):
    write(str(path), content)
    os.chmod(path, mode)


class _Answers:
    """An on_conflict that answers from a script and records what it saw."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, target, source, existing):
        self.asked.append(os.path.basename(target))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _NoLinks(LocalFileSystem):
    """The local disk, pretending it cannot hold a link -- as FTP cannot."""

    can_symlink = False


class _NoModes(LocalFileSystem):
    """The local disk, pretending it cannot set a mode -- as a zip cannot."""

    can_chmod = False


class _ModesFail(LocalFileSystem):
    def chmod(self, path, mode):
        raise PermissionError("not yours")


class _Counting(LocalFileSystem):
    """The local disk, counting the stat calls an existence check costs."""

    def __init__(self):
        super().__init__()
        self.stats: list[str] = []

    def stat(self, path):
        self.stats.append(os.path.basename(path))
        return super().stat(path)


# -- the filesystem interface ------------------------------------------------

def test_the_default_backend_refuses_modes_and_links_by_name(fs):
    class Bare(FileSystem):
        def label(self): return "bare://x"
        def home(self): return "/"
        def listdir(self, path): return []
        def stat(self, path): raise FileNotFoundError(path)
        def open_read(self, path): raise OSError
        def open_write(self, path): raise OSError
        def mkdir(self, path): pass
        def remove(self, path): pass
        def rmdir(self, path): pass
        def rename(self, src, dst): pass

    bare = Bare()
    assert not bare.can_chmod and not bare.can_symlink
    for call in (lambda: bare.chmod("/a", 0o644),
                 lambda: bare.symlink("b", "/a"),
                 lambda: bare.readlink("/a")):
        with pytest.raises(FileSystemError, match="bare://x cannot"):
            call()


def test_local_modes_and_links(fs, tmp_path):
    target = tmp_path / "f"
    write(str(target), "x")
    fs.chmod(str(target), 0o104751)          # set-user-ID is dropped
    assert _mode(target) == 0o751
    fs.symlink("f", str(tmp_path / "l"))
    assert fs.readlink(str(tmp_path / "l")) == "f"


def test_local_stat_of_a_dangling_link_describes_the_link(fs, tmp_path):
    (tmp_path / "dangling").symlink_to(tmp_path / "never-existed")
    entry = fs.stat(str(tmp_path / "dangling"))
    assert entry.is_symlink and not entry.is_dir
    with pytest.raises(FileNotFoundError):
        fs.stat(str(tmp_path / "nothing-at-all"))


# -- asking before replacing ---------------------------------------------------

def test_an_existing_target_is_asked_about_and_kept(fs, tmp_path):
    write(str(tmp_path / "a" / "f"), "new")
    write(str(tmp_path / "b" / "f"), "old")
    answers, report = _Answers(SKIP), TransferReport()
    left = copy_path(fs, str(tmp_path / "a" / "f"), fs, str(tmp_path / "b" / "f"),
                     on_conflict=answers, report=report)
    assert read(str(tmp_path / "b" / "f")) == "old"
    assert left == [str(tmp_path / "a" / "f")]
    assert report.kept == [str(tmp_path / "b" / "f")]
    assert answers.asked == ["f"]


def test_an_existing_target_is_replaced_when_told_to(fs, tmp_path):
    write(str(tmp_path / "a" / "f"), "new")
    write(str(tmp_path / "b" / "f"), "old")
    assert copy_path(fs, str(tmp_path / "a" / "f"), fs, str(tmp_path / "b" / "f"),
                     on_conflict=_Answers(OVERWRITE)) == []
    assert read(str(tmp_path / "b" / "f")) == "new"


def test_a_new_target_is_not_asked_about(fs, tmp_path):
    write(str(tmp_path / "a" / "f"), "new")
    answers = _Answers()
    copy_path(fs, str(tmp_path / "a" / "f"), fs, str(tmp_path / "b" / "f"),
              on_conflict=answers)
    assert answers.asked == []


def test_cancel_stops_the_transfer_there(fs, tmp_path):
    write(str(tmp_path / "a" / "tree" / "1"), "one")
    write(str(tmp_path / "a" / "tree" / "2"), "two")
    write(str(tmp_path / "b" / "tree" / "1"), "old")
    with pytest.raises(OperationCancelled):
        copy_path(fs, str(tmp_path / "a" / "tree"), fs, str(tmp_path / "b" / "tree"),
                  on_conflict=_Answers(OperationCancelled()))
    assert read(str(tmp_path / "b" / "tree" / "1")) == "old"


def test_a_tree_merges_and_asks_only_where_something_is_there(tmp_path):
    """Nothing can be in the way under a directory the copy made itself, so
    no round trip is spent asking there -- on a remote pane that is one per
    file."""
    for rel in ("same", "fresh/inner/deep", "old/kept", "old/added"):
        write(str(tmp_path / "a" / "tree" / rel), "new " + rel)
    write(str(tmp_path / "b" / "tree" / "same"), "old")
    write(str(tmp_path / "b" / "tree" / "old" / "kept"), "old")
    dst = _Counting()
    answers = _Answers(SKIP, SKIP)
    copy_path(LocalFileSystem(), str(tmp_path / "a" / "tree"), dst,
              str(tmp_path / "b" / "tree"), on_conflict=answers)
    assert sorted(answers.asked) == ["kept", "same"]
    assert read(str(tmp_path / "b" / "tree" / "fresh" / "inner" / "deep")) \
        == "new fresh/inner/deep"
    assert read(str(tmp_path / "b" / "tree" / "old" / "added")) == "new old/added"
    # Every file in a directory that was already there is looked for; one
    # under a directory the copy made is not.
    assert "added" in dst.stats
    assert "deep" not in dst.stats


# -- keeping permissions -------------------------------------------------------

@pytest.mark.parametrize("mode", [0o755, 0o600])
def test_a_copy_keeps_the_mode_new_or_replacing(fs, tmp_path, mode):
    """MC-009's Appendix C: a 755 script came out 644 either way."""
    _exec_file(tmp_path / "a" / "run.sh", mode=mode)
    write(str(tmp_path / "b" / "run.sh"), "old")
    os.chmod(tmp_path / "b" / "run.sh", 0o644)
    copy_path(fs, str(tmp_path / "a" / "run.sh"), fs, str(tmp_path / "b" / "new.sh"),
              keep_mode=True)
    copy_path(fs, str(tmp_path / "a" / "run.sh"), fs, str(tmp_path / "b" / "run.sh"),
              on_conflict=_Answers(OVERWRITE), keep_mode=True)
    assert _mode(tmp_path / "b" / "new.sh") == mode
    assert _mode(tmp_path / "b" / "run.sh") == mode


def test_without_keep_mode_a_copy_gets_the_default_mode(fs, tmp_path):
    _exec_file(tmp_path / "a" / "run.sh")
    copy_path(fs, str(tmp_path / "a" / "run.sh"), fs, str(tmp_path / "b" / "run.sh"))
    umask = os.umask(0)
    os.umask(umask)
    assert _mode(tmp_path / "b" / "run.sh") == 0o666 & ~umask


def test_a_read_only_directory_gets_its_mode_once_it_is_filled(fs, tmp_path):
    """Given its mode first, it would refuse its own contents."""
    write(str(tmp_path / "a" / "ro" / "inside"), "x")
    os.chmod(tmp_path / "a" / "ro", 0o555)
    try:
        copy_path(fs, str(tmp_path / "a" / "ro"), fs, str(tmp_path / "b" / "ro"),
                  keep_mode=True)
        assert read(str(tmp_path / "b" / "ro" / "inside")) == "x"
        assert _mode(tmp_path / "b" / "ro") == 0o555
    finally:
        for side in ("a", "b"):
            if (tmp_path / side / "ro").exists():
                os.chmod(tmp_path / side / "ro", 0o755)


def test_a_directory_that_was_already_there_keeps_its_own_mode(fs, tmp_path):
    write(str(tmp_path / "a" / "d" / "f"), "x")
    os.chmod(tmp_path / "a" / "d", 0o700)
    (tmp_path / "b" / "d").mkdir(parents=True)
    os.chmod(tmp_path / "b" / "d", 0o755)
    copy_path(fs, str(tmp_path / "a" / "d"), fs, str(tmp_path / "b" / "d"),
              keep_mode=True)
    assert _mode(tmp_path / "b" / "d") == 0o755


def test_a_target_that_cannot_take_a_mode_is_reported(tmp_path):
    _exec_file(tmp_path / "a" / "run.sh")
    report = TransferReport()
    copy_path(LocalFileSystem(), str(tmp_path / "a" / "run.sh"), _NoModes(),
              str(tmp_path / "b" / "run.sh"), keep_mode=True, report=report)
    assert report.modes_lost == [str(tmp_path / "b" / "run.sh")]


def test_a_mode_that_will_not_set_is_reported(tmp_path):
    _exec_file(tmp_path / "a" / "run.sh")
    report = TransferReport()
    copy_path(LocalFileSystem(), str(tmp_path / "a" / "run.sh"), _ModesFail(),
              str(tmp_path / "b" / "run.sh"), keep_mode=True, report=report)
    assert report.modes_lost == [str(tmp_path / "b" / "run.sh")]


def test_a_source_whose_mode_is_unknown_has_nothing_to_keep(tmp_path):
    class Unknown(LocalFileSystem):
        def stat(self, path):
            entry = super().stat(path)
            entry.mode = 0
            return entry

    write(str(tmp_path / "a" / "f"), "x")
    report = TransferReport()
    copy_path(Unknown(), str(tmp_path / "a" / "f"), _NoModes(),
              str(tmp_path / "b" / "f"), keep_mode=True, report=report)
    assert report.modes_lost == []


# -- keeping links -------------------------------------------------------------

def test_links_are_copied_as_links_with_their_targets(fs, tmp_path):
    write(str(tmp_path / "a" / "tree" / "real"), "x")
    (tmp_path / "a" / "tree" / "sub").mkdir()
    os.symlink("real", tmp_path / "a" / "tree" / "relative")
    os.symlink(str(tmp_path / "a" / "tree" / "real"), tmp_path / "a" / "tree" / "absolute")
    os.symlink("nowhere", tmp_path / "a" / "tree" / "dangling")
    os.symlink("sub", tmp_path / "a" / "tree" / "dirlink")
    assert copy_path(fs, str(tmp_path / "a" / "tree"), fs,
                     str(tmp_path / "b" / "tree"), links=True) == []
    out = tmp_path / "b" / "tree"
    assert os.readlink(out / "relative") == "real"
    assert os.readlink(out / "absolute") == str(tmp_path / "a" / "tree" / "real")
    assert os.readlink(out / "dangling") == "nowhere"
    assert os.readlink(out / "dirlink") == "sub"


def test_a_single_link_is_copied_as_a_link(fs, tmp_path):
    """MC-009's Appendix C: a link to a file came out as a plain file."""
    write(str(tmp_path / "a" / "run.sh"), "x")
    os.symlink("run.sh", tmp_path / "a" / "link.sh")
    copy_path(fs, str(tmp_path / "a" / "link.sh"), fs, str(tmp_path / "b" / "link.sh"),
              links=True)
    assert os.readlink(tmp_path / "b" / "link.sh") == "run.sh"


def test_without_links_a_link_to_a_file_is_copied_as_the_file(fs, tmp_path):
    write(str(tmp_path / "a" / "run.sh"), "x")
    os.symlink("run.sh", tmp_path / "a" / "link.sh")
    copy_path(fs, str(tmp_path / "a" / "link.sh"), fs, str(tmp_path / "b" / "link.sh"))
    assert not os.path.islink(tmp_path / "b" / "link.sh")


def test_a_far_side_without_links_gets_files_and_names_what_it_left(tmp_path):
    write(str(tmp_path / "a" / "tree" / "real"), "x")
    (tmp_path / "a" / "tree" / "sub").mkdir()
    os.symlink("real", tmp_path / "a" / "tree" / "filelink")
    os.symlink("sub", tmp_path / "a" / "tree" / "dirlink")
    report = TransferReport()
    left = copy_path(LocalFileSystem(), str(tmp_path / "a" / "tree"), _NoLinks(),
                     str(tmp_path / "b" / "tree"), links=True, report=report)
    out = tmp_path / "b" / "tree"
    assert read(str(out / "filelink")) == "x" and not os.path.islink(out / "filelink")
    assert not (out / "dirlink").exists()
    assert report.links_as_files == [str(tmp_path / "a" / "tree" / "filelink")]
    assert report.links_left == left == [str(tmp_path / "a" / "tree" / "dirlink")]


def test_a_link_the_source_cannot_read_is_copied_as_its_file(tmp_path):
    class Unreadable(LocalFileSystem):
        def readlink(self, path):
            raise OSError("no")

    write(str(tmp_path / "a" / "real"), "x")
    os.symlink("real", tmp_path / "a" / "link")
    report = TransferReport()
    copy_path(Unreadable(), str(tmp_path / "a" / "link"), LocalFileSystem(),
              str(tmp_path / "b" / "link"), links=True, report=report)
    assert read(str(tmp_path / "b" / "link")) == "x"
    assert report.links_as_files == [str(tmp_path / "a" / "link")]


def test_a_link_replaces_a_file_only_when_told_to(fs, tmp_path):
    os.makedirs(tmp_path / "a")
    os.symlink("elsewhere", tmp_path / "a" / "l")
    write(str(tmp_path / "b" / "l"), "a file")
    report = TransferReport()
    assert copy_path(fs, str(tmp_path / "a" / "l"), fs, str(tmp_path / "b" / "l"),
                     links=True, on_conflict=_Answers(SKIP), report=report) \
        == [str(tmp_path / "a" / "l")]
    assert read(str(tmp_path / "b" / "l")) == "a file"
    copy_path(fs, str(tmp_path / "a" / "l"), fs, str(tmp_path / "b" / "l"),
              links=True, on_conflict=_Answers(OVERWRITE))
    assert os.readlink(tmp_path / "b" / "l") == "elsewhere"


def test_a_link_will_not_replace_a_directory(fs, tmp_path):
    os.makedirs(tmp_path / "a")
    os.symlink("elsewhere", tmp_path / "a" / "l")
    (tmp_path / "b" / "l").mkdir(parents=True)
    with pytest.raises(FileSystemError, match="a directory is in the way"):
        copy_path(fs, str(tmp_path / "a" / "l"), fs, str(tmp_path / "b" / "l"),
                  links=True, on_conflict=_Answers(OVERWRITE))


# -- moves ---------------------------------------------------------------------

def test_a_rename_asks_before_replacing_and_keeps_the_source_on_skip(fs, tmp_path):
    """Within one filesystem a move is a rename, which replaces silently."""
    write(str(tmp_path / "f"), "mine")
    write(str(tmp_path / "d" / "f"), "theirs")
    report = TransferReport()
    assert move_path(fs, str(tmp_path / "f"), fs, str(tmp_path / "d" / "f"),
                     on_conflict=_Answers(SKIP), report=report) == [str(tmp_path / "f")]
    assert read(str(tmp_path / "f")) == "mine"
    assert report.kept == [str(tmp_path / "d" / "f")]
    assert move_path(fs, str(tmp_path / "f"), fs, str(tmp_path / "d" / "f"),
                     on_conflict=_Answers(OVERWRITE)) == []
    assert read(str(tmp_path / "d" / "f")) == "mine"
    assert not (tmp_path / "f").exists()


def test_a_rename_onto_a_directory_is_not_asked_about(fs, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "dst").mkdir()
    answers = _Answers()
    move_path(fs, str(tmp_path / "src"), fs, str(tmp_path / "dst"),
              on_conflict=answers)
    assert answers.asked == []


def test_a_move_between_filesystems_keeps_a_source_it_skipped(tmp_path):
    write(str(tmp_path / "a" / "tree" / "1"), "one")
    write(str(tmp_path / "a" / "tree" / "2"), "two")
    write(str(tmp_path / "b" / "tree" / "1"), "old")
    left, right = LocalFileSystem(), LocalFileSystem()
    skipped = move_path(left, str(tmp_path / "a" / "tree"), right,
                        str(tmp_path / "b" / "tree"), on_conflict=_Answers(SKIP))
    assert skipped == [str(tmp_path / "a" / "tree" / "1")]
    assert read(str(tmp_path / "a" / "tree" / "1")) == "one"
    assert read(str(tmp_path / "b" / "tree" / "2")) == "two"


# -- the conflict policy -------------------------------------------------------

def _entry(mtime):
    return DirEntry(name="f", is_dir=False, size=10, mtime=mtime)


@pytest.mark.parametrize("existing, source, older", [
    (1000.0, 2000.0, True),
    (2000.0, 1000.0, False),
    (1000.0, 1001.0, False),        # within sync's two seconds
    (None, 1000.0, False),          # unknown is never older
    (1000.0, None, False),
])
def test_older_means_older_by_more_than_slack(existing, source, older):
    assert TransferConflicts.older(_entry(existing), _entry(source)) is older


@pytest.mark.parametrize("label, first, second", [
    ("Overwrite", OVERWRITE, "asks again"),
    ("Overwrite all", OVERWRITE, OVERWRITE),
    ("Skip", SKIP, "asks again"),
    ("Skip all", SKIP, SKIP),
])
def test_an_all_answer_holds_for_the_rest(monkeypatch, label, first, second):
    scripted = _ScriptedDialogs(monkeypatch, menu=[label, "Skip"])
    conflicts = TransferConflicts(None, "Copy")
    assert conflicts("/d/f", _entry(2000.0), _entry(1000.0)) == first
    expected = SKIP if second == "asks again" else second
    assert conflicts("/d/g", _entry(2000.0), _entry(1000.0)) == expected
    assert len(scripted.menus) == (2 if second == "asks again" else 1)


def test_if_older_compares_each_time(monkeypatch):
    _ScriptedDialogs(monkeypatch, menu=["Overwrite all older"])
    conflicts = TransferConflicts(None, "Copy")
    assert conflicts("/d/f", _entry(2000.0), _entry(1000.0)) == OVERWRITE
    assert conflicts("/d/g", _entry(1000.0), _entry(2000.0)) == SKIP


def test_if_older_for_one_file_asks_again_next_time(monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch,
                                menu=["Overwrite if older", "Overwrite if older"])
    conflicts = TransferConflicts(None, "Copy")
    assert conflicts("/d/f", _entry(1000.0), _entry(2000.0)) == SKIP
    assert conflicts("/d/g", _entry(2000.0), _entry(1000.0)) == OVERWRITE
    assert len(scripted.menus) == 2


@pytest.mark.parametrize("answer", ["Cancel", None])
def test_cancel_or_escape_stops_the_transfer(monkeypatch, answer):
    _ScriptedDialogs(monkeypatch, menu=[answer])
    with pytest.raises(OperationCancelled):
        TransferConflicts(None, "Copy")("/d/f", _entry(2.0), _entry(1.0))


@pytest.mark.parametrize("existing, source, word", [
    (1000.0, 2000.0, "older"),
    (2000.0, 1000.0, "newer"),
    (1000.0, 1000.0, "same age"),
    (None, 1000.0, ""),
])
def test_the_question_says_which_is_newer(monkeypatch, existing, source, word):
    scripted = _ScriptedDialogs(monkeypatch, menu=["Skip"])
    TransferConflicts(None, "Move")("/d/report.tsv", _entry(source), _entry(existing))
    title, options = scripted.menus[0]
    text = scripted.menu_texts[0]
    assert title == "Move: already there"
    assert "report.tsv is already there." in text
    assert "Existing:" in text and "Replace with:" in text
    last = text.splitlines()[-1]
    if word:
        assert last.endswith(word)
    else:
        assert not last.rstrip().endswith(("older", "newer", "same age"))
    assert scripted.menu_keys[0] == ["o", "a", "i", "l", "s", "n", "c"]
    assert options[-1] == "Cancel"


# -- the dialogs ---------------------------------------------------------------

def test_a_menu_shows_its_text_above_the_options(monkeypatch):
    captured = scripted_menu(monkeypatch, [ord("s")])
    choice = with_curses_screen(
        24, 70, lambda s: dialogs.menu(s, "Copy: already there",
                                       ["Overwrite", "Skip"], ["o", "s"],
                                       text="f is already there.\nExisting: 1K"))
    drawn = "\n".join(str(d) for d in captured["window"].drawn)
    assert choice == 1
    assert "f is already there." in drawn and "Existing: 1K" in drawn


def test_a_short_screen_gives_the_options_the_room(monkeypatch):
    choice, drawn = run_menu(monkeypatch, 6, ["one", "two", "three"],
                             [curses.KEY_END, 10])
    assert choice == 2
    captured = scripted_menu(monkeypatch, [10])
    with_curses_screen(
        6, 60, lambda s: dialogs.menu(s, "T", ["only"], None,
                                      text="a\nb\nc\nd\ne\nf"))
    text = "\n".join(str(d) for d in captured["window"].drawn)
    assert "only" in text and "f" not in text.split("only")[0][-5:]


def test_a_prompt_switch_flips_with_ctrl_and_its_key(monkeypatch):
    keep = dialogs.Toggle("Keep permissions", "p")
    captured = scripted_menu(monkeypatch, [keep.code, ord("x"), 10])
    answer = with_curses_screen(
        24, 70, lambda s: dialogs.prompt(s, "Copy", "Copy to:", toggles=[keep]))
    drawn = "\n".join(str(d) for d in captured["window"].drawn)
    assert answer == "x"
    assert keep.on is False
    assert "[x] Keep permissions   Ctrl-P" in drawn
    assert "[ ] Keep permissions   Ctrl-P" in drawn
    assert keep.code == 16


# -- the application -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)

    class _Progress:
        def __init__(self, stdscr, title):
            pass

        def set_overall(self, text):
            pass

        def update(self, cur, total, label):
            pass

        def cancelled(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(dialogs, "ProgressDialog", _Progress)


def _left(app, tmp_path, **files):
    for name, content in files.items():
        write(str(tmp_path / "left" / name), content)
    app.left.refresh()


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def test_appendix_c_on_the_branch(app, tmp_path, monkeypatch):
    """MC-009's copy check, through F5: asked first, modes kept, link kept."""
    _exec_file(tmp_path / "left" / "run.sh")
    os.symlink("run.sh", tmp_path / "left" / "link.sh")
    write(str(tmp_path / "right" / "run.sh"), "old")
    app.left.refresh()
    app.left.selected = {"run.sh", "link.sh"}
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")],
                                menu=["Overwrite"])
    app._copy()
    assert scripted.menus[0][0] == "Copy: already there"
    assert _mode(tmp_path / "right" / "run.sh") == 0o755
    assert read(str(tmp_path / "right" / "run.sh")).startswith("#!/bin/sh")
    assert os.readlink(tmp_path / "right" / "link.sh") == "run.sh"
    assert scripted.prompt_toggles[0] == [("Keep permissions", True)]
    assert app.message == "Copy complete: 2 item(s)"


def test_skip_keeps_the_target_and_says_so(app, tmp_path, monkeypatch):
    _left(app, tmp_path, **{"file.txt": "mine"})
    _point_at(app.left, "file.txt")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")], menu=["Skip"])
    app._copy()
    assert read(str(tmp_path / "right" / "file.txt")) == "right"
    assert "1 existing kept" in app.message


def test_cancel_at_the_question_cancels_the_copy(app, tmp_path, monkeypatch):
    _left(app, tmp_path, **{"file.txt": "mine", "z": "later"})
    app.left.selected = {"file.txt", "z"}
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")], menu=["Cancel"])
    app._copy()
    assert app.message == "Copy cancelled"
    assert read(str(tmp_path / "right" / "file.txt")) == "right"


def test_switching_keep_permissions_off(app, tmp_path, monkeypatch):
    _exec_file(tmp_path / "left" / "run.sh")
    app.left.refresh()
    _point_at(app.left, "run.sh")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")],
                     toggles=[{"Keep permissions": False}])
    app._copy()
    assert _mode(tmp_path / "right" / "run.sh") != 0o755


def test_a_move_that_kept_a_target_keeps_its_source(app, tmp_path, monkeypatch):
    _left(app, tmp_path, **{"file.txt": "mine"})
    _point_at(app.left, "file.txt")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")], menu=["Skip"])
    app._move()
    assert read(str(tmp_path / "left" / "file.txt")) == "mine"
    assert "1 existing kept, source kept" in app.message


def test_copying_into_a_zip_says_what_it_could_not_keep(app, tmp_path, monkeypatch):
    write_zip(str(tmp_path / "right" / "bundle.zip"), {"old.txt": "old"})
    app.right.refresh()
    app.active = app.right
    _point_at(app.right, "bundle.zip")
    app._activate_entry()
    app.active = app.left
    _exec_file(tmp_path / "left" / "run.sh")
    os.symlink("run.sh", tmp_path / "left" / "link.sh")
    app.left.refresh()
    app.left.selected = {"run.sh", "link.sh"}
    scripted = _ScriptedDialogs(monkeypatch, prompt=["/"])
    app._copy()
    assert "1 link(s) copied as files" in app.message
    assert "permissions not kept on 2" in app.message
    assert scripted.messages[-1][0] == "Copy: links copied as files"
    assert str(tmp_path / "left" / "link.sh") in scripted.last_message


def test_rename_asks_before_replacing(app, tmp_path, monkeypatch):
    _left(app, tmp_path, a="one", b="two")
    _point_at(app.left, "a")
    scripted = _ScriptedDialogs(monkeypatch, prompt=["b"], confirm=[False])
    app._rename()
    assert read(str(tmp_path / "left" / "b")) == "two"
    assert "'b' already exists" in scripted.messages[0][1]
    _ScriptedDialogs(monkeypatch, prompt=["b"], confirm=[True])
    _point_at(app.left, "a")
    app._rename()
    assert read(str(tmp_path / "left" / "b")) == "one"


def test_rename_onto_a_directory_or_the_same_file_does_not_ask(app, tmp_path):
    _left(app, tmp_path, a="one")
    os.link(tmp_path / "left" / "a", tmp_path / "left" / "hard")
    fs = app.left.fs
    left = str(tmp_path / "left")
    assert app._rename_would_replace(fs, f"{left}/a", f"{left}/sub") is False
    assert app._rename_would_replace(fs, f"{left}/a", f"{left}/hard") is False
    assert app._rename_would_replace(fs, f"{left}/a", f"{left}/nothing") is False


def test_rename_onto_a_dangling_link_asks(app, tmp_path):
    _left(app, tmp_path, a="one")
    os.symlink("nowhere", tmp_path / "left" / "ghost")
    left = str(tmp_path / "left")
    assert app._rename_would_replace(app.left.fs, f"{left}/a", f"{left}/ghost")


def test_rename_inside_an_archive_asks_too(app, tmp_path):
    """Away from the local disk there is no samefile to ask."""
    write_zip(str(tmp_path / "left" / "z.zip"), {"a": "1", "b": "2"})
    app.left.refresh()
    _point_at(app.left, "z.zip")
    app._activate_entry()
    assert app._rename_would_replace(app.left.fs, "/a", "/b")


def test_sync_never_asks(app, tmp_path, monkeypatch):
    """F9 decides by age and never deletes; the question is not its to ask."""
    old = time.time() - 3600
    write(str(tmp_path / "left" / "file.txt"), "newer")
    write(str(tmp_path / "right" / "file.txt"), "older", mtime=old)
    app.left.refresh()
    app.right.refresh()

    def no_questions(*args, **kwargs):
        raise AssertionError("sync asked about a conflict")

    monkeypatch.setattr(app_mod.TransferConflicts, "__call__", no_questions)
    plan = app_mod.build_sync_plan(app.left.fs, str(tmp_path / "left"),
                                   app.right.fs, str(tmp_path / "right"))
    app_mod.execute_sync_plan(plan, app.left.fs, app.right.fs)
    assert read(str(tmp_path / "right" / "file.txt")) == "newer"
