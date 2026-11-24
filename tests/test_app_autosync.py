# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Auto-sync in the application: starting, showing, stopping (MC-034).

The runner's thread is not started here -- ``spawn`` is replaced -- and its
checks are made one at a time with ``step()``, so a test says exactly when a
check happens.  The panes, the directories and the dialogs' words are real.
"""

from __future__ import annotations

import curses
import os
import time

import pytest

from meridian_commander import autosync as autosync_mod
from meridian_commander import dialogs, theme, trash
from meridian_commander.app import MENUS, App
from meridian_commander.autosync import AutoSyncRunner
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.jobspane import JobsPane
from meridian_commander.sync import LARGE_FILE_COUNT

from support import (
    _FakeRemoteBackend,
    _ScriptedDialogs,
    _StubScreen,
    read,
    with_curses_screen,
    write,
    write_zip,
)

#: The real thread starter, for the one test that runs auto-sync on one.
_REAL_THREAD = autosync_mod._thread


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No real progress window and no real thread: the tests step it.

    The FreeDesktop trash, in the app fixture's data directory, on every
    platform: on macOS the desktop's trash is the real ``~/.Trash``.
    """
    monkeypatch.setattr(trash.sys, "platform", "linux")
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
    spawned = []
    monkeypatch.setattr(autosync_mod, "_thread", spawned.append)
    return spawned


def _start(app, monkeypatch, confirm=(True,), key=curses.KEY_F21):
    scripted = _ScriptedDialogs(monkeypatch, confirm=list(confirm))
    app.handle_key(key)
    return scripted


def _same(tmp_path):
    """Make the conftest panes' file.txt the same on both sides."""
    for side in ("left", "right"):
        write(str(tmp_path / side / "file.txt"), "same", mtime=1000)


# -- starting ------------------------------------------------------------------

def test_shift_f9_starts_it_after_an_ordinary_sync(app, tmp_path, monkeypatch,
                                                   _quiet):
    scripted = _start(app, monkeypatch)
    runner = app.autosync
    assert isinstance(runner, AutoSyncRunner)
    assert _quiet == [runner._run]                      # the thread started
    # The F9 pass came first: the plan, confirmed, and carried out.
    assert read(str(tmp_path / "right" / "file.txt")) == "left"
    assert [title for title, _t, _e in scripted.messages] == \
        ["Auto-sync panes", "Auto-sync"]
    assert "to copy:" in scripted.messages[0][1]
    notice = scripted.last_message
    assert notice.startswith("Auto-sync is on. These two are kept in step:")
    assert f"local:{tmp_path / 'left'}" in notice
    assert "Every 2 seconds" in notice and "F9 or Shift-F9 stops it" in notice
    assert app.message == ("Auto-sync on: left ⇄ right -- F9 or Shift-F9 "
                           "stops it")
    assert runner.interval == 2.0
    assert runner.panels == (app.left, app.right)
    assert [place.name for place in runner.places] == ["left", "right"]
    # Connections of its own, even for the local disk.
    assert runner.places[0].fs is not app.left.fs
    assert runner.closing == []


def test_s_and_the_menu_start_it_too(app, tmp_path, monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=(), key=ord("S"))
    assert app.autosync is not None
    app._stop_auto_sync("test")
    _ScriptedDialogs(monkeypatch)
    app._dispatch("auto_sync")
    assert app.autosync is not None


def test_already_in_step_goes_straight_to_the_notice(app, tmp_path,
                                                     monkeypatch):
    _same(tmp_path)
    scripted = _start(app, monkeypatch, confirm=())
    assert [title for title, _t, _e in scripted.messages] == ["Auto-sync"]
    assert app.autosync is not None


def test_the_menu_offers_it_next_to_synchronize():
    command = next(m for m in MENUS if m["name"] == "command")
    names = [item.get("name") for item in command["items"]]
    assert names[names.index("sync") + 1] == "auto_sync"
    entry = command["items"][names.index("auto_sync")]
    assert entry["label"] == "A~u~to-sync panes"
    assert entry["key"] == "Shift+F9"


def test_the_menu_entry_is_ticked_while_it_runs(app, tmp_path, monkeypatch):
    index = next(i for i, m in enumerate(MENUS) if m["name"] == "command")

    def ticked():
        return next(item for item in app._menu_items(index)
                    if item.get("name") == "auto_sync")["checked"]

    assert ticked() is False
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    assert ticked() is True


def test_a_remote_side_gets_its_own_connection_and_the_longer_interval(
        app, tmp_path, monkeypatch):
    pane_fs = _FakeRemoteBackend(host="web1", username="deploy")
    app.right.set_location(pane_fs, str(tmp_path / "right"))
    dialled = []

    def connect(info):
        fs = _FakeRemoteBackend(host=info["host"], username=info["username"])
        dialled.append((info, fs))
        return fs

    monkeypatch.setattr(app, "_connect", connect)
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    runner = app.autosync
    own = dialled[0][1]
    assert runner.places[1].fs is own and own is not pane_fs
    assert runner.closing == [own]
    assert runner.interval == 10.0
    assert runner.places[1].name == "web1:right"
    # The details are kept, to dial again after a drop without asking.
    again = runner.reopen[1]()
    assert again is dialled[1][1] and dialled[1][0]["host"] == "web1"
    assert runner.reopen[0] is None


def test_no_connection_of_its_own_means_no_auto_sync(app, tmp_path,
                                                     monkeypatch):
    app.left.set_location(_FakeRemoteBackend(host="a"), str(tmp_path / "left"))
    app.right.set_location(_FakeRemoteBackend(host="b"),
                           str(tmp_path / "right"))
    opened = []

    class _Closing(_FakeRemoteBackend):
        closed = False

        def close(self):
            self.closed = True

    def connect(info):
        if info["host"] == "b":
            raise OSError("refused")
        fs = _Closing(host="a")
        opened.append(fs)
        return fs

    monkeypatch.setattr(app, "_connect", connect)
    _same(tmp_path)
    # No password to the prompt the failed dial asks: it gives up.
    _ScriptedDialogs(monkeypatch, prompt=[""])
    app.handle_key(curses.KEY_F21)
    assert app.autosync is None
    assert app.message == ("Auto-sync not started -- it needs a connection "
                           "of its own")
    assert opened[0].closed


def test_the_intervals_come_from_the_config(app, tmp_path, monkeypatch):
    write(str(tmp_path / "config" / "meridian-commander" / "config.ini"),
          "[autosync]\ninterval = 7\n")
    _same(tmp_path)
    scripted = _start(app, monkeypatch, confirm=())
    assert app.autosync.interval == 7.0
    assert "Every 7 seconds" in scripted.last_message


# -- refusing, and being declined ---------------------------------------------------

def test_a_list_of_find_results_is_refused(app, monkeypatch):
    app.right.results = []
    scripted = _start(app, monkeypatch, confirm=())
    assert app.autosync is None and scripted.messages == []
    assert app.message == ("Auto-sync: the right pane lists find results "
                           "-- < goes back to the directory")


def test_an_archive_is_refused(app, tmp_path, monkeypatch):
    write_zip(str(tmp_path / "left" / "bundle.zip"), {"a.txt": "a"})
    app.left.refresh()
    app.left.cursor = next(i for i, e in enumerate(app.left.entries)
                           if e.name == "bundle.zip")
    app._activate_entry()
    scripted = _start(app, monkeypatch, confirm=())
    assert app.autosync is None
    title, text, error = scripted.messages[0]
    assert (title, error) == ("Auto-sync", True)
    assert text.startswith("The left pane is inside an archive")


def test_one_directory_inside_the_other_is_refused(app, tmp_path,
                                                   monkeypatch):
    app.right.chdir(str(tmp_path / "left" / "sub"))
    scripted = _start(app, monkeypatch, confirm=())
    assert app.autosync is None
    assert "one is inside the other" in scripted.messages[0][1]
    # And the other way round.
    app.right.chdir(str(tmp_path / "right"))
    app.left.chdir(str(tmp_path / "right" / "sub"))
    scripted = _start(app, monkeypatch, confirm=())
    assert app.autosync is None
    assert "one is inside the other" in scripted.messages[0][1]


def test_a_large_directory_is_asked_about_first(app, tmp_path, monkeypatch):
    for i in range(LARGE_FILE_COUNT):
        write(str(tmp_path / "left" / f"f{i:04d}.txt"), "x")
    app.left.refresh()
    scripted = _start(app, monkeypatch, confirm=(False,))
    assert app.autosync is None
    title, text, _ = scripted.messages[0]
    assert title == "Auto-sync panes"
    assert "every few seconds for as long as it is on" in text
    assert text.endswith("Keep these two directories in step anyway?")
    assert "Nothing is ever deleted" not in text
    assert app.message == "Auto-sync not started"
    assert not (tmp_path / "right" / "f0000.txt").exists()


def test_a_declined_plan_starts_nothing(app, monkeypatch):
    _start(app, monkeypatch, confirm=(False,))
    assert app.autosync is None
    assert app.message == "Auto-sync not started -- Sync cancelled"


# -- stopping ------------------------------------------------------------------

def test_shift_f9_again_turns_it_off(app, tmp_path, monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    runner = app.autosync
    app.handle_key(curses.KEY_F21)
    assert app.autosync is None
    assert runner._stop.is_set()
    assert app.message == "Auto-sync stopped: turned off"


def test_f9_reads_stop_and_stops_it(app, tmp_path, monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    scripted = _ScriptedDialogs(monkeypatch)
    app.handle_key(curses.KEY_F9)
    assert app.autosync is None
    assert scripted.messages == []                 # no one-off sync after it
    assert app.message == "Auto-sync stopped: stopped with F9"
    app._stop_auto_sync("again")                   # nothing left to stop
    assert app.message == "Auto-sync stopped: stopped with F9"


@pytest.mark.parametrize("move, said", [
    (lambda app, tmp: app.left.chdir(str(tmp / "left" / "sub")),
     "the left pane changed directory"),
    (lambda app, tmp: app.right.go_parent(),
     "the right pane changed directory"),
    (lambda app, tmp: app.right.set_location(LocalFileSystem(),
                                             str(tmp / "right")),
     "the right pane changed connection"),
    (lambda app, tmp: setattr(app.left, "results", []),
     "the left pane changed directory"),
])
def test_it_stops_when_a_pane_moves(app, tmp_path, monkeypatch, move, said):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    runner = app.autosync
    move(app, tmp_path)
    assert app._tick_plugins() is None
    assert app.autosync is None and runner._stop.is_set()
    assert app.message == f"Auto-sync stopped: {said}"


def test_swapping_the_panes_keeps_it_running(app, tmp_path, monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    app.handle_key(21)                              # Ctrl-U
    assert app._tick_plugins() == App.AUTOSYNC_POLL_MS
    assert app.autosync is not None


def test_quitting_lets_the_file_being_copied_finish(app, tmp_path,
                                                   monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    runner = app.autosync
    runner.done.set()                    # the worker ends inside the grace
    app._close_backends()
    assert app.autosync is None
    assert runner._stop.is_set() and not runner._abort


def test_quitting_cancels_a_copy_that_outlasts_the_grace(app, tmp_path,
                                                        monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    runner = app.autosync
    monkeypatch.setattr(app, "JOBS_GRACE", 0.01)
    app._close_backends()
    assert runner._abort
    app._end_auto_sync()                 # nothing left to end


def test_quitting_mid_copy_leaves_no_half_file(app, tmp_path, monkeypatch):
    """A real thread, stopped by quitting in the middle of a large file."""
    monkeypatch.setattr(autosync_mod, "_thread", _REAL_THREAD)
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    runner = app.autosync
    writing = []

    class _Slow(LocalFileSystem):
        def open_write(self, path):
            writer = super().open_write(path)
            writing.append(path)

            class _Writer:
                def write(self, data):
                    time.sleep(0.05)
                    return writer.write(data)

                def close(self):
                    writer.close()

            return _Writer()

    runner.places[1].fs = _Slow()
    write(str(tmp_path / "left" / "big.bin"), "x" * (64 * 1024 * 40),
          mtime=1000)
    deadline = time.monotonic() + 20
    while not writing:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    monkeypatch.setattr(app, "JOBS_GRACE", 0.1)
    app._close_backends()
    assert runner._abort and runner.wait(5)
    assert not (tmp_path / "right" / "big.bin").exists()


# -- what it does, as the panes show it -----------------------------------------------

def test_the_panes_show_what_a_check_did(app, tmp_path, monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    runner = app.autosync
    app.right.cursor = next(i for i, e in enumerate(app.right.entries)
                            if e.name == "sub")
    runner.step()
    write(str(tmp_path / "left" / "new.txt"), "new", mtime=1000)
    runner.step()
    runner.step()
    assert "new.txt" not in [e.name for e in app.right.entries]
    assert app._tick_plugins() == App.AUTOSYNC_POLL_MS
    assert "new.txt" in [e.name for e in app.right.entries]
    assert app.right.current_name() == "sub"       # the cursor stayed put
    assert app.message == "Auto-sync: 1 copied"


def test_the_news_on_the_status_line():
    from meridian_commander.autosync import PassReport

    reports = [PassReport(copied=[1, 2], trashed=[0, 1],
                          conflicts=["a.txt", "b.txt"], failures=["x"])]
    assert App._autosync_news(reports, ["cannot reach web1, reconnecting"]) \
        == ("cannot reach web1, reconnecting; a.txt (and 1 more) changed on "
            "both sides -- the newer was kept, the other is in the trash; "
            "3 copied; 1 to the trash; 1 failed -- J to read")
    one = [PassReport(conflicts=["a.txt"])]
    assert App._autosync_news(one, []).startswith("a.txt changed on both")
    assert App._autosync_news([], []) == ""


def test_the_names_tell_the_two_apart(tmp_path):
    class _P:
        def __init__(self, fs, path):
            self.fs, self.path = fs, path

    local = LocalFileSystem()
    remote = _FakeRemoteBackend(host="web1")
    assert App._place_names(_P(local, "/srv/a/docs"), _P(local, "/b")) == \
        ["docs", "b"]
    assert App._place_names(_P(local, "/x/a/docs"), _P(local, "/x/b/docs")) \
        == ["a/docs", "b/docs"]
    assert App._place_names(_P(local, "/p/a/docs"), _P(local, "/q/a/docs")) \
        == ["/p/a/docs", "/q/a/docs"]
    assert App._place_names(_P(local, "/docs"), _P(remote, "/")) == \
        ["docs", "web1:/"]

    class _NoHost(LocalFileSystem):
        scheme = "ftp"

        def label(self):
            return "ftp://anon@h"

    assert App._place_names(_P(local, "/docs"), _P(_NoHost(), "/docs")) == \
        ["docs", "ftp://anon@h:docs"]


def test_the_bar_gives_way_on_a_narrow_line(app, tmp_path, monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    assert app._autosync_bar(100) == \
        " AUTO-SYNC  left ⇄ right  watching  F9 stops "
    assert app._autosync_bar(60) == " AUTO-SYNC  watching  F9 stops "
    assert app._autosync_bar(40) == " AUTO-SYNC  F9 stops "
    assert app._autosync_bar(10) == " AUTO-SYNC "


def _drawn(tmp_path, monkeypatch, prepare, rows=24, cols=100):
    """Start auto-sync, draw on a real screen; the rows and the app."""
    holder = {}

    def run(stdscr):
        app = App(stdscr, str(tmp_path / "left"), str(tmp_path / "right"))
        app.use_trash = False
        holder["app"] = app
        _same(tmp_path)
        _ScriptedDialogs(monkeypatch)
        app._auto_sync()
        prepare(app)
        app.draw()
        rows_text = [stdscr.instr(row, 0).decode() for row in range(rows)]
        attrs = {
            "frame": stdscr.inch(1, 0) & curses.A_ATTRIBUTES,
            "bar": stdscr.inch(rows - 2, 2) & curses.A_ATTRIBUTES,
            "syncframe": theme.attr("syncframe") & curses.A_ATTRIBUTES,
            "syncbar": theme.attr("syncbar") & curses.A_ATTRIBUTES,
        }
        return rows_text, attrs

    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "doupdate", curses.doupdate)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for side in ("left", "right"):
        (tmp_path / side).mkdir(exist_ok=True)
    result = with_curses_screen(rows, cols, run)
    return result, holder["app"]


def test_it_cannot_be_missed_while_it_runs(tmp_path, monkeypatch):
    (text, attrs), app = _drawn(tmp_path, monkeypatch, lambda app: None)
    assert "AUTO-SYNC  left" in text[-2] and "F9 stops" in text[-2]
    assert "9Stop" in text[-1]
    assert attrs["frame"] == attrs["syncframe"]
    assert attrs["bar"] == attrs["syncbar"]
    assert "Auto-sync on" in text[-2]              # the message beside it


def test_it_cannot_be_missed_in_single_pane_mode_either(tmp_path,
                                                        monkeypatch):
    def single(app):
        app.handle_key(ord("z"))

    (text, attrs), app = _drawn(tmp_path, monkeypatch, single)
    assert app.single
    assert "AUTO-SYNC" in text[-2] and "9Stop" in text[-1]
    assert attrs["frame"] == attrs["syncframe"]


def test_it_is_gone_once_it_stops(tmp_path, monkeypatch):
    def stop(app):
        app.handle_key(curses.KEY_F21)

    (text, attrs), app = _drawn(tmp_path, monkeypatch, stop)
    assert "AUTO-SYNC" not in text[-2] and "9Sync" in text[-1]
    assert attrs["frame"] != attrs["syncframe"]
    assert "Auto-sync stopped: turned off" in text[-2]


# -- the jobs list ---------------------------------------------------------------

def test_the_jobs_list_shows_it_first(app, tmp_path, monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    runner = app.autosync
    app._jobs_pane()
    pane = app.left.plugin
    assert isinstance(pane, JobsPane)
    assert pane.entries()[0] is runner
    assert JobsPane.lines_for_autosync(runner) == (
        " A. running   Auto-sync left ⇄ right",
        "    watching, every 2s  Enter: all of it")


def test_enter_on_it_reads_every_failure_in_full(app, tmp_path, monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    runner = app.autosync
    runner.failures = ["12:00:00  " + "a-long-name/" * 12 + "f.txt: denied"]
    app._jobs_pane()
    scripted = _ScriptedDialogs(monkeypatch)
    app.left.plugin.handle_key(10)
    title, text, error = scripted.messages[0]
    assert (title, error) == ("Auto-sync", True)
    assert "Failures:" in text
    assert all(len(line) <= 80 - 10 for line in text.split("\n"))
    assert "f.txt: denied" in text.replace("\n    ", "")


def test_c_on_it_stops_it_after_asking(app, tmp_path, monkeypatch):
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    app._jobs_pane()
    pane = app.left.plugin
    _ScriptedDialogs(monkeypatch, confirm=[False])
    pane.handle_key(ord("c"))
    assert app.autosync is not None
    pane.handle_key(ord("p"))                      # no pause for auto-sync
    assert app.autosync is not None
    _ScriptedDialogs(monkeypatch, confirm=[True])
    pane.handle_key(curses.KEY_DC)
    assert app.autosync is None
    assert app.message == "Auto-sync stopped: stopped from the jobs list"


def test_the_jobs_list_draws_it(tmp_path, monkeypatch):
    def jobs(app):
        app._jobs_pane()

    (text, _attrs), app = _drawn(tmp_path, monkeypatch, jobs)
    screen = "\n".join(text)
    assert "A. running   Auto-sync left" in screen
    assert "Enter: report  c: stop  J: close" in screen


def test_the_stub_screen_still_draws(app, tmp_path, monkeypatch):
    """The stand-in screen used across the suite draws a running auto-sync
    without complaint."""
    _same(tmp_path)
    _start(app, monkeypatch, confirm=())
    assert isinstance(app.stdscr, _StubScreen)
    app.draw()
    assert os.path.isdir(tmp_path / "left")
