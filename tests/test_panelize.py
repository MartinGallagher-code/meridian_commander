# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Find's hits sent to a pane, and everything a pane does to them (MC-026).

The tree under the left pane is a run tree: every run has the same file
names, and a decoy of the same name sits at the top.  An action that reached
the wrong path -- the directory searched joined to the bare file name, say --
would land on the decoy, and the tests look at the decoy as well as at the
file they meant.
"""

from __future__ import annotations

import curses
import os
import stat
import time

import pytest

from meridian_commander import app as app_mod
from meridian_commander import dialogs, findfiles, presets, trash
from meridian_commander.archive import ArchiveFileSystem
from meridian_commander.filesystems import (
    FileSystemError,
    LocalFileSystem,
    SFTPFileSystem,
)
from meridian_commander.findfiles import collect_matches
from meridian_commander.jobs import JobQueue
from meridian_commander.panel import Panel, is_hidden
from meridian_commander.peek import PeekPane
from meridian_commander.quickview import QuickViewPane

from support import _ScriptedDialogs, read, write, write_zip
from test_app_draw import _render
from test_app_keys import _FakeBrowser, _FtpLike, _SftpLike, _SshLike
from test_filesystems_sftp import DIR_MODE, FILE_MODE, _Attr, _FakeClient, \
    _FakeSftp


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No real screen: the progress window and the redraws go nowhere."""

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
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)


@pytest.fixture
def runs(app, tmp_path):
    """The left pane's tree: two runs, each with out/results.tsv, and a decoy."""
    left = tmp_path / "left"
    write(str(left / "results.tsv"), "decoy")
    write(str(left / "run_07" / "out" / "results.tsv"), "seven\n")
    write(str(left / "run_08" / "out" / "results.tsv"), "eight!\n")
    write(str(left / "run_08" / "notes.txt"), "notes")
    app.left.refresh()
    return left


SEVEN = "run_07/out/results.tsv"
EIGHT = "run_08/out/results.tsv"


def _names(panel):
    return [e.name for e in panel.entries]


def _point_at(panel, name):
    panel.move_to(_names(panel).index(name))


def _panelize(app, monkeypatch, pattern="results.tsv"):
    """Find ``pattern`` from the active pane and list the hits there."""
    _ScriptedDialogs(monkeypatch, prompt=[pattern])
    app._find_to_pane()


def _prompt_default(monkeypatch, answer=lambda default: default):
    """Answer every prompt from its default; record the defaults offered."""
    offered: list = []

    def prompt(stdscr, title, label, default="", is_password=False,
               toggles=None):
        offered.append(default)
        return answer(default)

    monkeypatch.setattr(dialogs, "prompt", prompt)
    return offered


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# -- sending the hits to the pane -------------------------------------------------

def test_p_in_the_results_lists_every_hit_in_the_pane(app, runs, monkeypatch):
    seen = {}

    def browser(fs, root, pattern, matches, truncated):
        seen.update(root=root, count=len(matches))
        return _FakeBrowser(None, to_pane=True)

    monkeypatch.setattr(findfiles, "FindBrowser", browser)
    history = list(app.left.history)
    _ScriptedDialogs(monkeypatch, prompt=["results.tsv"])
    app.handle_key(ord("f"))
    panel = app.left
    assert _names(panel) == ["..", "results.tsv", SEVEN, EIGHT]
    assert panel.results_pattern == "results.tsv"
    assert panel.results_truncated is False
    # Still the directory searched, and not a move: nothing in the history.
    assert panel.path == str(runs) == seen["root"]
    assert panel.history == history
    assert app.message.startswith("3 found -- tag, copy, move or delete")


def test_the_menu_entry_lists_them_without_the_browser(app, runs, monkeypatch):
    monkeypatch.setattr(findfiles, "FindBrowser", None)    # never opened
    _ScriptedDialogs(monkeypatch, prompt=["results.tsv"])
    app._dispatch("find_to_pane")
    assert _names(app.left)[1:] == ["results.tsv", SEVEN, EIGHT]
    command = next(m for m in app_mod.MENUS if m["name"] == "command")
    assert any(item.get("name") == "find_to_pane"
               for item in command["items"])


def test_closing_the_results_leaves_the_directory(app, runs, monkeypatch):
    monkeypatch.setattr(findfiles, "FindBrowser",
                        lambda *a: _FakeBrowser(None))
    _ScriptedDialogs(monkeypatch, prompt=["results.tsv"])
    app._find_files()
    assert app.left.results is None
    assert "run_07" in _names(app.left)


def test_the_cap_says_truncated(app, runs, monkeypatch):
    real = findfiles.collect_matches
    monkeypatch.setattr(findfiles, "collect_matches",
                        lambda *a, **k: real(*a, max_results=2, **k))
    _panelize(app, monkeypatch)
    assert len(app.left.results) == 2 and app.left.results_truncated
    assert "(truncated)" in app.message
    caption = app._results_caption(app.left, 200)
    assert caption == (f"Find 'results.tsv': 2 found (truncated) under "
                       f"local:{runs}")


def test_the_caption_keeps_the_count_as_it_narrows(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    found = "Find 'results.tsv': 3 found"
    where = f"local:{runs}"
    assert app._results_caption(app.left, 200) == f"{found} under {where}"
    # The place gives way from its start, as a directory's caption does.
    narrow = app._results_caption(app.left, len(found) + 20)
    assert narrow == f"{found} under ~{where[-12:]}"
    assert len(narrow) == len(found) + 20
    # Too narrow even for that: the end of the count goes too.
    assert app._results_caption(app.left, 12) == "Find 'resul~"


# -- every action reaches the real file --------------------------------------------

def test_f3_views_the_hit_not_the_decoy(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    seen = []

    class _View:
        def __init__(self, fs, path):
            seen.append(path)

        def run(self, stdscr):
            pass

    monkeypatch.setattr(app_mod, "viewer_for", _View)
    app.handle_key(curses.KEY_F3)
    assert seen == [str(runs / "run_07" / "out" / "results.tsv")]


def test_f4_edits_the_hit_and_the_list_shows_its_new_size(app, runs,
                                                          monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    seen = []

    class _Editor:
        def __init__(self, fs, path):
            self.path = path
            seen.append(path)

        def run(self, stdscr):
            write(self.path, "edited at some length\n")

    monkeypatch.setattr(app_mod, "Editor", _Editor)
    app.handle_key(curses.KEY_F4)
    assert seen == [str(runs / "run_07" / "out" / "results.tsv")]
    assert read(str(runs / "results.tsv")) == "decoy"
    assert app.left.current().name == SEVEN
    assert app.left.current().size == len("edited at some length\n")


def test_a_changes_the_hit_and_the_list_shows_its_mode(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    decoy = _mode(runs / "results.tsv")
    seen = {}

    def attributes(stdscr, title, subject, on, off, owner="", group="",
                   can_chown=False, has_dirs=False):
        seen["subject"] = subject
        return dialogs.AttributeChoice(0o700, 0o077, "", "", False)

    monkeypatch.setattr(dialogs, "attributes", attributes)
    app.handle_key(ord("a"))
    assert _mode(runs / "run_07" / "out" / "results.tsv") == 0o700
    assert _mode(runs / "results.tsv") == decoy
    assert seen["subject"] == SEVEN
    assert stat.S_IMODE(app.left.current().mode) == 0o700


def test_l_links_the_hit_under_its_own_path(app, runs, tmp_path, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    offered = _prompt_default(monkeypatch)
    app.handle_key(ord("l"))
    link = tmp_path / "right" / "run_07" / "out" / "results.tsv"
    assert offered == [str(link)]
    assert os.readlink(link) == "../../../left/run_07/out/results.tsv"
    assert read(str(link)) == "seven\n"


def test_l_on_tagged_hits_keeps_each_ones_path(app, runs, tmp_path,
                                               monkeypatch):
    _panelize(app, monkeypatch)
    app.left.selected = {SEVEN, EIGHT}
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._link()
    right = tmp_path / "right"
    assert read(str(right / "run_07" / "out" / "results.tsv")) == "seven\n"
    assert read(str(right / "run_08" / "out" / "results.tsv")) == "eight!\n"
    assert not (right / "results.tsv").exists()
    assert app.message == "Made 2 link(s)"


def test_l_on_a_link_hit_changes_that_link(app, runs, monkeypatch):
    os.symlink("results.tsv", runs / "run_07" / "out" / "latest")
    os.symlink("results.tsv", runs / "latest")
    _panelize(app, monkeypatch, "latest")
    _point_at(app.left, "run_07/out/latest")
    _ScriptedDialogs(monkeypatch, prompt=["older.tsv"])
    app.handle_key(ord("l"))
    assert os.readlink(runs / "run_07" / "out" / "latest") == "older.tsv"
    assert os.readlink(runs / "latest") == "results.tsv"
    # Dangling now, and still a hit: it is there, as a link.
    assert app.left.current_name() == "run_07/out/latest"
    assert app.message == "run_07/out/latest -> older.tsv"


def test_w_opens_the_hit(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, EIGHT)
    seen = []
    monkeypatch.setattr(app.opener, "open",
                        lambda fs, path, is_dir: seen.append(path) or "Opened")
    app.handle_key(ord("w"))
    assert seen == [str(runs / "run_08" / "out" / "results.tsv")]


def test_r_renames_the_hit_where_it_is(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    offered = _prompt_default(monkeypatch, lambda default: "final.tsv")
    app.handle_key(ord("r"))
    assert offered == ["results.tsv"]           # its own name, not its path
    out = runs / "run_07" / "out"
    assert read(str(out / "final.tsv")) == "seven\n"
    assert not (out / "results.tsv").exists()
    assert read(str(runs / "results.tsv")) == "decoy"
    assert app.left.current_name() == "run_07/out/final.tsv"
    assert SEVEN not in _names(app.left)
    assert app.message == "Renamed to final.tsv"


def test_r_will_not_move_a_hit_to_another_directory(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    _ScriptedDialogs(monkeypatch, prompt=["../elsewhere.tsv"])
    app._rename()
    assert (runs / "run_07" / "out" / "results.tsv").exists()
    assert "renamed where it is" in app.message


def test_r_onto_another_hit_replaces_it_in_the_list(app, runs, monkeypatch):
    write(str(runs / "run_07" / "out" / "results.old"), "old")
    _panelize(app, monkeypatch, "results")
    _point_at(app.left, "run_07/out/results.old")
    _ScriptedDialogs(monkeypatch, prompt=["results.tsv"], confirm=[True])
    app._rename()
    assert read(str(runs / "run_07" / "out" / "results.tsv")) == "old"
    assert _names(app.left).count(SEVEN) == 1
    assert "run_07/out/results.old" not in _names(app.left)


def test_r_on_a_directory_hit_takes_what_was_found_in_it(app, runs,
                                                         monkeypatch):
    _panelize(app, monkeypatch, "out")
    write(str(runs / "run_07" / "out" / "out.log"), "x")
    _panelize(app, monkeypatch, "out")
    assert "run_07/out/out.log" in _names(app.left)
    _point_at(app.left, "run_07/out")
    _ScriptedDialogs(monkeypatch, prompt=["output"])
    app._rename()
    assert (runs / "run_07" / "output" / "out.log").exists()
    assert "run_07/output" in _names(app.left)
    assert "run_07/output/out.log" in _names(app.left)
    assert not any(name.startswith("run_07/out/") for name in _names(app.left))


def test_ctrl_q_shows_the_hit_in_the_other_pane(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    app.handle_key(17)
    quick = app.right.plugin
    assert isinstance(quick, QuickViewPane)
    entry, path = quick.target()
    assert entry.name == SEVEN
    assert path == str(runs / "run_07" / "out" / "results.tsv")


def test_h_reads_the_hit_the_other_pane_is_on(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, EIGHT)
    app.active = app.right
    app.handle_key(ord("h"))
    peek = app.right.plugin
    assert isinstance(peek, PeekPane)
    assert peek.target() == str(runs / "run_08" / "out" / "results.tsv")
    assert any("eight!" in row for row in peek.rows)


def test_enter_runs_a_hit_in_its_own_directory(app, runs, monkeypatch):
    script = runs / "run_07" / "go.sh"
    write(str(script), "#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    _panelize(app, monkeypatch, "go.sh")
    _point_at(app.left, "run_07/go.sh")
    ran = []
    monkeypatch.setattr(app, "_suspend_and_run",
                        lambda cmd, cwd, banner, **kw: ran.append(
                            (cmd, cwd, banner)) or 0)
    _ScriptedDialogs(monkeypatch, prompt=[""])
    app.handle_key(10)
    cmd, cwd, banner = ran[0]
    assert cmd == [str(script)]
    assert cwd == str(runs / "run_07")
    assert f"local:{runs / 'run_07'}" in banner


def test_enter_on_a_directory_hit_goes_into_it(app, runs, monkeypatch):
    _panelize(app, monkeypatch, "out")
    assert _names(app.left) == ["..", "run_07/out", "run_08/out"]
    _point_at(app.left, "run_08/out")
    app.handle_key(10)
    assert app.left.path == str(runs / "run_08" / "out")
    assert app.left.results is None
    # The list is not a place of its own: back is the directory searched,
    # with the cursor on the way to where it went.
    app.handle_key(ord("<"))
    assert app.left.path == str(runs) and app.left.results is None
    assert app.left.current_name() == "run_08"
    app.handle_key(ord(">"))
    assert app.left.path == str(runs / "run_08" / "out")


def test_enter_on_an_archive_hit_opens_it(app, runs, monkeypatch):
    write_zip(str(runs / "run_07" / "bundle.zip"), {"inside.txt": "in"})
    _panelize(app, monkeypatch, "bundle")
    _point_at(app.left, "run_07/bundle.zip")
    app.handle_key(10)
    assert isinstance(app.left.fs, ArchiveFileSystem)
    assert "inside.txt" in _names(app.left)
    app.handle_key(curses.KEY_BACKSPACE)       # out of it, where it is
    assert app.left.path == str(runs / "run_07")
    assert app.left.current_name() == "bundle.zip"


def test_x_extracts_the_hit(app, runs, tmp_path, monkeypatch):
    write_zip(str(runs / "run_07" / "bundle.zip"), {"inside.txt": "in"})
    _panelize(app, monkeypatch, "bundle")
    _point_at(app.left, "run_07/bundle.zip")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app.handle_key(ord("x"))
    assert read(str(tmp_path / "right" / "bundle" / "inside.txt")) == "in"


def test_n_makes_a_file_beside_the_cursors_hit(app, runs, tmp_path,
                                               monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    offered = _prompt_default(monkeypatch, lambda default: default + "DONE")
    app.handle_key(ord("n"))
    assert offered == ["run_07/out/"]
    assert (runs / "run_07" / "out" / "DONE").exists()
    assert app.left.current_name() == "run_07/out/DONE"
    # Made outside the directory searched: made, but the list has no name
    # for it, and the cursor stays where it was.
    outside = str(tmp_path / "right" / "outside.txt")
    _ScriptedDialogs(monkeypatch, prompt=[outside])
    app._touch()
    assert os.path.exists(outside)
    assert app.left.current_name() == "run_07/out/DONE"
    assert all("outside" not in name for name in _names(app.left))
    # On ".." there is no hit to be beside.
    app.left.move_to(0)
    offered = _prompt_default(monkeypatch, lambda default: None)
    app._touch()
    assert offered == [""]


def test_f7_makes_a_directory_beside_the_cursors_hit(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, EIGHT)
    _prompt_default(monkeypatch, lambda default: default + "plots")
    app.handle_key(curses.KEY_F7)
    assert (runs / "run_08" / "out" / "plots").is_dir()
    assert app.left.current().is_dir
    assert app.left.current_name() == "run_08/out/plots"


# -- what refuses a list of find results -----------------------------------------

def test_d_refuses_a_list_of_find_results(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    ran = []
    monkeypatch.setattr(app_mod, "Comparison",
                        lambda *a: ran.append(a))
    app.handle_key(ord("D"))
    assert ran == []
    assert app.message == ("Compare: left pane -- it lists find results; "
                           "< goes back to the directory")


def test_f9_refuses_a_list_of_find_results_on_either_side(app, runs,
                                                          monkeypatch):
    _panelize(app, monkeypatch)
    monkeypatch.setattr(app_mod, "build_sync_plan",
                        lambda *a, **k: pytest.fail("no sync"))
    app.handle_key(curses.KEY_F9)
    assert app.message == ("Sync: the left pane lists find results "
                           "-- < goes back to the directory")
    app.left, app.right = app.right, app.left
    app._sync()
    assert app.message.startswith("Sync: the right pane lists find results")


def test_u_refuses_a_list_of_find_results(app, runs, monkeypatch):
    _panelize(app, monkeypatch, "out")
    app.handle_key(ord("u"))
    assert app.left.show_sizes is False
    assert "lists find results" in app.message


def test_sizes_on_before_wait_for_the_directory(app, runs, monkeypatch):
    app.left.toggle_sizes()
    _panelize(app, monkeypatch, "out")
    panel = app.left
    assert panel.sizer is None
    assert panel.size_of(panel.entries[1]) is None     # "<DIR>", unmeasured
    text, _ = _render(runs.parent, prepare=lambda a: _sized_list(a, runs))
    assert "Modify time" in text and "Share" not in text
    panel.leave_results()
    assert panel.sizer is not None                     # measuring again


def _sized_list(app, runs):
    app.left.toggle_sizes()
    app.left.show_results("out", *collect_matches(app.left.fs, str(runs),
                                                  "out"))


class _Works:
    name = "Works on the other pane"
    description = "x"

    def __init__(self, ctx):
        self.ctx = ctx


class _Alone(_Works):
    name = "Keeps to itself"
    uses_other_pane = False

    def draw(self, stdscr, y, x, h, w):
        pass


def test_a_plug_in_that_works_on_a_list_of_results_is_refused(app, runs,
                                                              monkeypatch):
    from meridian_commander import plugins

    _panelize(app, monkeypatch)
    app.active = app.right
    monkeypatch.setattr(plugins, "discover", lambda: ([_Works, _Alone], []))
    _ScriptedDialogs(monkeypatch, menu=[0])
    app._plugin_mode()
    assert app.right.plugin is None
    assert app.message == ("Works on the other pane works on the other "
                           "pane's directory, and it lists find results")
    _ScriptedDialogs(monkeypatch, menu=[1])
    app._plugin_mode()
    assert isinstance(app.right.plugin, _Alone)


# -- copy and move ----------------------------------------------------------------

def test_f5_keeps_each_hits_path_under_the_target(app, runs, tmp_path,
                                                  monkeypatch):
    _panelize(app, monkeypatch)
    app.left.selected = {"results.tsv", SEVEN, EIGHT}
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app.handle_key(curses.KEY_F5)
    right = tmp_path / "right"
    assert read(str(right / "results.tsv")) == "decoy"
    assert read(str(right / "run_07" / "out" / "results.tsv")) == "seven\n"
    assert read(str(right / "run_08" / "out" / "results.tsv")) == "eight!\n"
    assert app.message == "Copy complete: 3 item(s)"
    # A copy leaves the hits where they were, untagged.
    assert _names(app.left)[1:] == ["results.tsv", SEVEN, EIGHT]
    assert app.left.selected == set()


def test_f5_asks_before_replacing_a_target(app, runs, tmp_path, monkeypatch):
    write(str(tmp_path / "right" / "run_07" / "out" / "results.tsv"), "old")
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")],
                                menu=["Skip"])
    app._copy()
    assert scripted.menus[0][0] == "Copy: already there"
    assert "results.tsv is already there" in scripted.menu_texts[0]
    assert read(str(tmp_path / "right" / "run_07" / "out" /
                    "results.tsv")) == "old"
    assert app.message.endswith("1 existing kept")


def test_f5_keeps_the_execute_bit_and_copies_a_link_as_a_link(
        app, runs, tmp_path, monkeypatch):
    os.chmod(runs / "run_07" / "out" / "results.tsv", 0o755)
    os.symlink("results.tsv", runs / "run_08" / "out" / "results.link")
    _panelize(app, monkeypatch, "results.*")
    app.left.selected = {SEVEN, "run_08/out/results.link"}
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._copy()
    right = tmp_path / "right"
    assert _mode(right / "run_07" / "out" / "results.tsv") == 0o755
    link = right / "run_08" / "out" / "results.link"
    assert os.path.islink(link) and os.readlink(link) == "results.tsv"


def test_f6_takes_the_hits_out_of_the_list_and_the_cursor_stays(
        app, runs, tmp_path, monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)                 # index 2
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app.handle_key(curses.KEY_F6)
    assert read(str(tmp_path / "right" / "run_07" / "out" /
                    "results.tsv")) == "seven\n"
    assert not (runs / "run_07" / "out" / "results.tsv").exists()
    assert _names(app.left) == ["..", "results.tsv", EIGHT]
    assert app.left.cursor == 2 and app.left.current_name() == EIGHT
    assert app._results_caption(app.left, 30) == "Find 'results.tsv': 2 found"


def test_a_filter_hidden_tag_is_still_copied(app, runs, tmp_path, monkeypatch):
    """MC-012's rule holds in a list: a filter hides, it does not untag."""
    _panelize(app, monkeypatch)
    app.left.selected = {SEVEN, EIGHT}
    app.left.set_filter("run_08")
    assert app.left.hidden_tagged == 1
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._copy()
    assert "(1 not shown)" in scripted.prompts[0]
    assert (tmp_path / "right" / "run_07" / "out" / "results.tsv").exists()
    assert (tmp_path / "right" / "run_08" / "out" / "results.tsv").exists()


def test_what_is_inside_a_tagged_directory_goes_with_it(app, runs, tmp_path,
                                                        monkeypatch):
    write(str(runs / "run_07" / "run_07.log"), "log")
    _panelize(app, monkeypatch, "run_07")
    assert _names(app.left)[1:] == ["run_07", "run_07/run_07.log"]
    app.left.selected = {"run_07", "run_07/run_07.log"}
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._copy()
    assert scripted.menus == []            # not copied a second time
    assert read(str(tmp_path / "right" / "run_07" / "run_07.log")) == "log"
    assert app.message == "Copy complete: 1 item(s)"
    app.left.selected = {"run_07", "run_07/run_07.log"}
    scripted = _ScriptedDialogs(monkeypatch, confirm=[True])
    app._delete()
    assert scripted.messages[0][1].startswith("Delete 'run_07'?")
    assert not (runs / "run_07").exists()
    assert _names(app.left) == [".."]
    assert app.message == "Deleted 1 item(s)"


@pytest.fixture
def now(app):
    app.jobs = JobQueue(spawn=lambda work: work())


@pytest.mark.parametrize("move", [False, True])
def test_in_the_background_too(app, runs, tmp_path, monkeypatch, now, move):
    _panelize(app, monkeypatch)
    app.left.selected = {SEVEN, EIGHT}
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")],
                     toggles=[{"Background": True}], menu=["Overwrite"])
    app.handle_key(curses.KEY_F6 if move else curses.KEY_F5)
    job = app.jobs.jobs[0]
    assert sorted(job.names) == [SEVEN, EIGHT]
    right = tmp_path / "right"
    assert read(str(right / "run_07" / "out" / "results.tsv")) == "seven\n"
    assert read(str(right / "run_08" / "out" / "results.tsv")) == "eight!\n"
    app._tick_plugins()                    # the main loop notices it ended
    assert job.state == "done"
    left = ["..", "results.tsv"] + ([] if move else [SEVEN, EIGHT])
    assert _names(app.left) == left


# -- delete -------------------------------------------------------------------------

def test_f8_for_good_takes_the_hits_out_and_the_cursor_stays(app, runs,
                                                             monkeypatch):
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app.handle_key(curses.KEY_F8)
    assert not (runs / "run_07" / "out" / "results.tsv").exists()
    assert (runs / "results.tsv").exists()
    assert _names(app.left) == ["..", "results.tsv", EIGHT]
    assert app.left.current_name() == EIGHT and app.left.cursor == 2


def test_f8_to_the_trash(app, runs, tmp_path, monkeypatch):
    monkeypatch.setattr(trash.sys, "platform", "linux")
    app.use_trash = True
    _panelize(app, monkeypatch)
    _point_at(app.left, SEVEN)
    _ScriptedDialogs(monkeypatch, menu=["Move to trash"])
    app.handle_key(curses.KEY_F8)
    files = tmp_path / "data" / "Trash" / "files"
    assert read(str(files / "results.tsv")) == "seven\n"
    assert _names(app.left) == ["..", "results.tsv", EIGHT]
    assert app.left.current_name() == EIGHT and app.left.cursor == 2
    # Tagged, two at once: each to the trash, each out of the list.
    app.left.selected = {"results.tsv", EIGHT}
    _ScriptedDialogs(monkeypatch, menu=["Move to trash"])
    app._delete()
    assert sorted(os.listdir(files)) == ["results.2.tsv", "results.3.tsv",
                                         "results.tsv"]
    assert _names(app.left) == [".."]
    assert app.message == "Moved 2 item(s) to the trash"


def test_f8_on_a_hit_already_in_the_trash_is_for_good(app, runs, tmp_path,
                                                      monkeypatch):
    """Found from above the trash, a hit in it is still in it."""
    monkeypatch.setattr(trash.sys, "platform", "linux")
    app.use_trash = True
    write(str(runs / "old.tmp"), "x")
    trash.move_to_trash(str(runs / "old.tmp"))
    app.left.chdir(str(tmp_path / "data"))
    _panelize(app, monkeypatch, "*.tmp")
    assert _names(app.left) == ["..", "Trash/files/old.tmp"]
    _point_at(app.left, "Trash/files/old.tmp")
    scripted = _ScriptedDialogs(monkeypatch, menu=[0])
    app._delete()
    assert scripted.menus[0] == ("Delete from the trash",
                                 ["Delete permanently", "Cancel"])
    assert not (tmp_path / "data" / "Trash" / "files" / "old.tmp").exists()
    assert not (tmp_path / "data" / "Trash" / "info" /
                "old.tmp.trashinfo").exists()
    assert _names(app.left) == [".."]


# -- tags, the filter, sorting and hidden files ---------------------------------------

def test_tags_and_the_filter_match_the_hits_paths(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    _ScriptedDialogs(monkeypatch, prompt=["run_0*"])
    app.handle_key(ord("+"))
    assert app.left.selected == {SEVEN, EIGHT}
    _ScriptedDialogs(monkeypatch, prompt=["run_08"])
    app.handle_key(ord("\\"))
    assert app.left.selected == {SEVEN}
    app.handle_key(ord("*"))
    assert app.left.selected == {"results.tsv", EIGHT}
    app.handle_key(ord("-"))
    assert app.left.selected == set()
    _point_at(app.left, EIGHT)
    app.handle_key(ord(" "))
    assert app.left.selected == {EIGHT}
    _ScriptedDialogs(monkeypatch, prompt=["out/"])
    app.handle_key(ord("F"))
    assert _names(app.left) == ["..", SEVEN, EIGHT]
    assert app.message == "Filter out/: 2 shown, 1 hidden"


def test_hidden_files_hide_the_hits_inside_a_dot_directory(app, runs,
                                                          monkeypatch):
    write(str(runs / ".cache" / "results.tsv"), "cached")
    _panelize(app, monkeypatch)
    assert ".cache/results.tsv" in _names(app.left)
    app.handle_key(ord("."))
    assert ".cache/results.tsv" not in _names(app.left)
    assert len(app.left.results) == 4              # hidden, not dropped
    app.handle_key(ord("."))
    assert ".cache/results.tsv" in _names(app.left)


def test_is_hidden_looks_at_every_part():
    assert is_hidden(".bashrc") and is_hidden("a/.git/config")
    assert not is_hidden("a/b.c/d") and not is_hidden("plain")


def test_sorting_by_extension_reads_the_hits_own_name(app, runs, monkeypatch):
    write(str(runs / "v1.z" / "readme"), "x")
    write(str(runs / "a.log"), "x")
    _panelize(app, monkeypatch, "*")
    app.left.set_sort("ext")
    files = [e.name for e in app.left.entries if not e.is_dir]
    # "readme" has no extension of its own, whatever its directory is called.
    assert files.index("v1.z/readme") < files.index("a.log")


# -- Ctrl-R ---------------------------------------------------------------------------

def test_ctrl_r_drops_a_hit_gone_behind_the_panes_back(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    os.remove(runs / "run_08" / "out" / "results.tsv")
    write(str(runs / "run_09" / "out" / "results.tsv"), "nine")
    app.handle_key(18)
    # Checked, not searched again: run_09 was not there to be found.
    assert _names(app.left) == ["..", "results.tsv", SEVEN]
    assert app.message == "Reloaded -- 1 find result(s) no longer there"
    app.handle_key(18)
    assert app.message == "Reloaded"


def test_ctrl_r_lists_each_directory_once_not_each_hit(app, runs, monkeypatch):
    for i in range(10):
        write(str(runs / "run_07" / "out" / f"results.{i}"), "x")
    _panelize(app, monkeypatch, "results")
    assert len(app.left.results) == 13
    asked = []
    real = app.left.fs.listdir
    monkeypatch.setattr(app.left.fs, "listdir",
                        lambda path: asked.append(path) or real(path))
    app._reload()
    assert sorted(asked) == sorted([str(runs), str(runs / "run_07" / "out"),
                                    str(runs / "run_08" / "out")])
    assert len(app.left.results) == 13


def test_a_directory_that_cannot_be_listed_drops_its_hits(tmp_path):
    class _Refusing(LocalFileSystem):
        refuse = ""

        def listdir(self, path):
            if path == self.refuse:
                raise PermissionError("permission denied")
            return super().listdir(path)

    fs = _Refusing()
    write(str(tmp_path / "a" / "x.tsv"), "x")
    write(str(tmp_path / "b" / "x.tsv"), "x")
    panel = Panel(fs, str(tmp_path))
    panel.show_results("x", *collect_matches(fs, str(tmp_path), "x.tsv"))
    fs.refuse = str(tmp_path / "a")
    assert panel.reload() == 1
    assert [e.name for e in panel.results] == ["b/x.tsv"]


class _TreeSftp(_FakeSftp):
    """An SFTP server holding a run tree: each directory's listing by path."""

    def __init__(self, tree):
        super().__init__()
        self.tree = tree

    def listdir_attr(self, path):
        self.calls.append(("listdir_attr", path))
        if path not in self.tree:
            raise FileNotFoundError(path)
        return self.tree[path]


def test_ctrl_r_over_sftp_costs_a_listing_per_directory(monkeypatch):
    """The cap's worth of hits, 2,000 in 200 directories: 200 round trips."""
    from meridian_commander import filesystems as fsmod

    runs = [f"run_{i:03d}" for i in range(200)]
    tree = {"/data": [_Attr(run, DIR_MODE) for run in runs]}
    for run in runs:
        tree[f"/data/{run}"] = [_Attr(f"part_{j}.tsv", FILE_MODE)
                                for j in range(10)]
    sftp = _TreeSftp(tree)
    monkeypatch.setattr(fsmod, "_open_ssh_client",
                        lambda *a, **k: _FakeClient(sftp=sftp))
    fs = SFTPFileSystem("web1", username="deploy")
    panel = Panel(fs, "/data")
    matches, truncated = collect_matches(fs, "/data", "*.tsv")
    assert len(matches) == 2000 and truncated is True
    panel.show_results("*.tsv", matches, truncated)
    del tree["/data/run_199"]                       # gone on the server
    sftp.calls.clear()
    started = time.perf_counter()
    assert panel.reload() == 10
    elapsed = time.perf_counter() - started
    assert len(sftp.calls) == 200
    assert len(panel.results) == 1990
    assert elapsed < 5                  # about 0.02 s here; a loose bound


# -- leaving the list ---------------------------------------------------------------

@pytest.mark.parametrize("key", [ord("<"), curses.KEY_BACKSPACE, "dotdot"])
def test_back_returns_to_the_directory_searched(app, runs, monkeypatch, key):
    app.left.chdir(str(runs / "run_08"))
    app.left.go_parent()
    history = list(app.left.history)
    _panelize(app, monkeypatch)
    _point_at(app.left, EIGHT)
    if key == "dotdot":
        app.left.move_to(0)
        app.handle_key(10)
    else:
        app.handle_key(key)
    assert app.left.results is None and app.left.path == str(runs)
    # On the way to the hit the cursor was on, or on ".." from "..".
    assert app.left.current_name() == (".." if key == "dotdot" else "run_08")
    assert app.left.history == history     # the list was never a place


def test_home_a_preset_and_go_to_leave_the_list(app, runs, tmp_path,
                                                monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "right"))
    _panelize(app, monkeypatch)
    app.handle_key(ord("~"))
    assert app.left.results is None
    assert app.left.path == str(tmp_path / "right")
    # Back from there is the directory searched, not the list.
    app.handle_key(ord("<"))
    assert app.left.path == str(runs) and app.left.results is None

    _panelize(app, monkeypatch)
    app._open_preset(presets.Preset("runs", path=str(runs / "run_07")))
    assert app.left.results is None
    assert app.left.path == str(runs / "run_07")

    app.left.chdir(str(runs))
    _panelize(app, monkeypatch)
    _ScriptedDialogs(monkeypatch, prompt=[str(runs)])
    app.handle_key(7)                          # Ctrl-G, to the same place
    assert app.left.results is None and app.left.path == str(runs)

    _panelize(app, monkeypatch)                # F2: a connection's home
    monkeypatch.setattr(dialogs, "connect_dialog",
                        lambda stdscr: {"scheme": "local"})
    app.handle_key(curses.KEY_F2)
    assert app.left.results is None
    assert app.left.path == str(tmp_path / "right")

    app.left.chdir(str(runs))
    _panelize(app, monkeypatch)                # T: the trash
    monkeypatch.setattr(trash.sys, "platform", "linux")
    app.handle_key(ord("T"))
    assert app.left.results is None
    assert app.left.path == str(tmp_path / "data" / "Trash" / "files")


def test_equals_from_the_other_pane_replaces_the_list(app, runs, monkeypatch):
    _panelize(app, monkeypatch)
    app.right.chdir(str(runs))
    app.active = app.right
    app.handle_key(ord("="))
    assert app.left.results is None and app.left.path == str(runs)
    assert app.message.startswith("Other pane:")


def test_equals_in_the_list_shows_its_directory_opposite(app, runs,
                                                         monkeypatch):
    _panelize(app, monkeypatch)
    app.handle_key(ord("="))
    assert app.right.path == str(runs) and app.right.results is None
    assert app.left.results is not None


def test_a_move_that_fails_keeps_the_list(app, runs, tmp_path, monkeypatch):
    _panelize(app, monkeypatch)
    scripted = _ScriptedDialogs(monkeypatch,
                                prompt=[str(tmp_path / "no-such-place")])
    app._go_to_path()
    assert "Cannot open" in scripted.last_message
    assert app.left.results is not None
    assert _names(app.left) == ["..", "results.tsv", SEVEN, EIGHT]
    assert app.left.set_location(app.left.fs, str(tmp_path / "gone")) is False
    assert app.left.results is not None


def test_back_and_forward_step_over_the_list(app, runs, tmp_path, monkeypatch):
    app.left.chdir(str(runs / "run_07"))
    app.left.back()                            # at runs, with a way forward
    _panelize(app, monkeypatch)
    app.handle_key(ord(">"))
    assert app.left.path == str(runs / "run_07") and app.left.results is None
    app.handle_key(ord("<"))
    assert app.left.path == str(runs) and app.left.results is None


# -- the Panel's own bookkeeping -------------------------------------------------------

def test_a_typed_name_is_listed_by_its_path_below_the_directory(fs, tmp_path):
    panel = Panel(fs, str(tmp_path))
    panel.show_results("x", [])
    assert panel.result_name("a/b/../c.txt") == "a/c.txt"
    assert panel.result_name(str(tmp_path / "d" / "e")) == "d/e"
    assert panel.result_name(str(tmp_path.parent / "elsewhere")) is None
    assert panel.result_name("../elsewhere") is None


def test_outermost_leaves_out_what_a_directory_takes_with_it():
    from meridian_commander.filesystems import DirEntry

    entries = [DirEntry("a", True), DirEntry("a/b.txt", False),
               DirEntry("a/c", True), DirEntry("a/c/d", False),
               DirEntry("link", True, is_symlink=True),
               DirEntry("link/e", False), DirEntry("ab", False)]
    assert [e.name for e in Panel.outermost(entries)] == ["a", "link",
                                                           "link/e", "ab"]


def test_a_directory_is_never_rechecked(fs, tmp_path):
    panel = Panel(fs, str(tmp_path))
    assert panel.recheck() == 0 and panel.reload(["x"]) == 0


def test_the_list_has_a_way_back_even_at_the_root(fs, tmp_path):
    panel = Panel(fs, "/")
    assert ".." not in [e.name for e in panel.entries]
    panel.show_results("x", [])
    assert [e.name for e in panel.entries] == [".."]


# -- every backend find walks --------------------------------------------------------

BACKENDS = [_SftpLike, _SshLike, _FtpLike, "archive"]


def _on(app, runs, backend):
    """Point the left pane at the run tree through ``backend``, or a zip of it."""
    if backend == "archive":
        write_zip(str(runs / "bundle.zip"),
                  {"results.tsv": "top", SEVEN: "seven\n", EIGHT: "eight!\n"})
        app.left.refresh()
        _point_at(app.left, "bundle.zip")
        app._activate_entry()
        assert isinstance(app.left.fs, ArchiveFileSystem)
    else:
        assert app.left.set_location(backend(), str(runs))


class _Nothing:
    def run(self, stdscr):
        pass


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_path_taking_keys_on_every_backend(app, runs, monkeypatch,
                                               backend):
    _on(app, runs, backend)
    _panelize(app, monkeypatch)
    fs = app.left.fs
    hit = fs.join(app.left.path, "run_07", "out", "results.tsv")
    _point_at(app.left, SEVEN)
    seen = []
    monkeypatch.setattr(app_mod, "viewer_for", lambda fs, path: seen.append(
        ("view", path)) or _Nothing())
    monkeypatch.setattr(app_mod, "Editor", lambda fs, path: seen.append(
        ("edit", path)) or _Nothing())
    monkeypatch.setattr(app.opener, "open", lambda fs, path, is_dir: seen.append(
        ("open", path)) or "Opened")
    for key in (curses.KEY_F3, curses.KEY_F4, ord("w")):
        app.handle_key(key)
    assert seen == [("view", hit), ("edit", hit), ("open", hit)]
    app.handle_key(17)
    assert app.right.plugin.target()[1] == hit
    app.handle_key(17)
    assert app.right.plugin is None
    # Tags and the filter, on the paths.
    _ScriptedDialogs(monkeypatch, prompt=["run_07*"])
    app.handle_key(ord("+"))
    assert app.left.selected == {SEVEN}
    app.left.set_filter("run_08")
    assert _names(app.left) == ["..", EIGHT]
    app.left.set_filter("")
    app.handle_key(ord("-"))
    # a: where the backend can change permissions, on the hit alone.
    monkeypatch.setattr(dialogs, "attributes", lambda *a, **k:
                        dialogs.AttributeChoice(0o700, 0o077, "", "", False))
    _point_at(app.left, SEVEN)
    scripted = _ScriptedDialogs(monkeypatch)
    app.handle_key(ord("a"))
    if fs.can_chmod:
        assert _mode(runs / "run_07" / "out" / "results.tsv") == 0o700
        assert _mode(runs / "results.tsv") != 0o700
    else:
        assert "cannot change permissions" in scripted.last_message
    # r: where the hit is.
    _ScriptedDialogs(monkeypatch, prompt=["final.tsv"])
    app.handle_key(ord("r"))
    # Through stat, which every stand-in answers from the tree it holds.
    assert fs.stat(fs.join(app.left.path, "run_07", "out", "final.tsv"))
    with pytest.raises((OSError, FileSystemError)):
        fs.stat(hit)
    assert app.left.current_name() == "run_07/out/final.tsv"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_round_trip_on_every_backend(app, runs, tmp_path, monkeypatch,
                                       backend):
    _on(app, runs, backend)
    _panelize(app, monkeypatch)
    assert _names(app.left) == ["..", "results.tsv", SEVEN, EIGHT]
    app.handle_key(ord("*"))                   # tag every hit
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app.handle_key(curses.KEY_F5)
    right = tmp_path / "right"
    assert read(str(right / "run_07" / "out" / "results.tsv")) == "seven\n"
    assert read(str(right / "run_08" / "out" / "results.tsv")) == "eight!\n"
    assert app.message == "Copy complete: 3 item(s)"
    # A hit that goes leaves the list: from the archive by F8, which
    # rewrites it; elsewhere behind the pane's back, seen by Ctrl-R.
    if backend == "archive":
        _point_at(app.left, EIGHT)
        _ScriptedDialogs(monkeypatch, confirm=[True])
        app._delete()
    else:
        os.remove(runs / "run_08" / "out" / "results.tsv")
        app.handle_key(18)
    assert _names(app.left) == ["..", "results.tsv", SEVEN]


# -- on a real screen ------------------------------------------------------------------

def _listed(app, runs):
    os.makedirs(runs / ("a-run-with-a-long-name-" * 2) / "out")
    write(str(runs / ("a-run-with-a-long-name-" * 2) / "out" /
              "results.tsv"), "x")
    app.left.show_results("results.tsv", *collect_matches(
        app.left.fs, str(runs), "results.tsv"))
    _point_at(app.left, SEVEN)


def test_a_list_drawn_at_80_by_24(app, runs):
    text, drawn = _render(runs.parent, rows=24, cols=80,
                          prepare=lambda a: _listed(a, runs))
    rows = text.splitlines()
    assert "Find 'results.tsv': 4 found" in rows[1]
    left = [row[:40] for row in rows]
    # A long path keeps its first part and its file name; a short one whole.
    assert any("║ a-run-wi~sults.tsv " in row for row in left)
    assert any("║ run_07/o~sults.tsv " in row for row in left)
    assert any("║ results.tsv " in row for row in left)
    # The cursor's hit, whole, along the bottom.
    assert "╡ run_07/out/results.tsv  6 ╞" in rows[-3]
    # The frame is whole: both corners of the left pane's bottom edge.
    assert rows[-3][0] in "╚└+" and rows[-3][39] in "╝┘+"


@pytest.mark.parametrize("single", [False, True])
def test_a_list_drawn_at_the_smallest_screen(app, runs, single):
    def prepare(a):
        _listed(a, runs)
        a.single = single

    text, drawn = _render(runs.parent, rows=8, cols=24, prepare=prepare)
    assert "Terminal too small" not in text
    rows = text.splitlines()
    width = 24 if single else 12
    # The frame is whole, whatever the caption and the footer had to lose.
    assert rows[1].startswith("╔") and rows[1][width - 1] == "╗"
    assert rows[-3].startswith("╚") and rows[-3][width - 1] == "╝"
    assert "Find" in rows[1]
    assert "~sv" in rows[3] or "~sv" in rows[4]
