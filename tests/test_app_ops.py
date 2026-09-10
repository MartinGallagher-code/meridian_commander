"""The application's file operations: copy, move, delete, mkdir, sync, view."""

from __future__ import annotations

import curses
import os

import pytest

from meridian_commander import app as app_mod
from meridian_commander import dialogs
from meridian_commander import sync as sync_mod
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.operations import OperationCancelled

from support import _ScriptedDialogs, read, write


@pytest.fixture(autouse=True)
def _no_refresh(monkeypatch):
    """_sync draws mid-operation; the stub screen cannot paint a real one.

    ACS_VLINE only exists once curses has been initialised on a real
    terminal, so it is pinned here rather than depending on test order.
    """
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "ACS_VLINE",
                        getattr(curses, "ACS_VLINE", ord("|")), raising=False)
    monkeypatch.setattr(curses, "has_colors", lambda: False)


@pytest.fixture(autouse=True)
def _quiet_progress(monkeypatch):
    """Replace the progress window; it needs a real screen and adds nothing."""

    class _Progress:
        def __init__(self, stdscr, title):
            self.title = title
            self.updates: list = []
            self.overall: list[str] = []
            self.cancel_after = None
            self.closed = False

        def set_overall(self, text):
            self.overall.append(text)

        def update(self, cur, total, label):
            self.updates.append((cur, total, label))

        def cancelled(self):
            if self.cancel_after is None:
                return False
            return len(self.overall) > self.cancel_after

        def close(self):
            self.closed = True

    made = []

    def factory(stdscr, title):
        dlg = _Progress(stdscr, title)
        made.append(dlg)
        return dlg

    monkeypatch.setattr(dialogs, "ProgressDialog", factory)
    return made


def _files(app, tmp_path, **names):
    for name, content in names.items():
        write(str(tmp_path / "left" / name), content)
    app.left.refresh()


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


# -- copy and move -------------------------------------------------------------

def test_copy_the_entry_under_the_cursor(app, tmp_path, monkeypatch):
    _files(app, tmp_path, a="payload")
    _point_at(app.left, "a")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._copy()
    assert read(str(tmp_path / "right" / "a")) == "payload"
    assert "Copy complete: 1 item(s)" in app.message


def test_copy_several_tagged_entries(app, tmp_path, monkeypatch):
    _files(app, tmp_path, a="one", b="two")
    app.left.selected = {"a", "b"}
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._copy()
    assert "Copy 2 items to:" in scripted.prompts[0]
    assert read(str(tmp_path / "right" / "a")) == "one"
    assert read(str(tmp_path / "right" / "b")) == "two"
    # The tags are cleared once the work is done.
    assert app.left.selected == set()


def test_move_removes_the_source(app, tmp_path, monkeypatch):
    _files(app, tmp_path, a="payload")
    _point_at(app.left, "a")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._move()
    assert read(str(tmp_path / "right" / "a")) == "payload"
    assert not (tmp_path / "left" / "a").exists()
    assert "Move complete" in app.message


def test_a_copy_says_which_symlinked_directories_it_left(app, tmp_path,
                                                         monkeypatch):
    write(str(tmp_path / "left" / "tree" / "a.txt"), "A")
    (tmp_path / "left" / "tree" / "sub").mkdir()
    os.symlink(str(tmp_path / "left" / "tree" / "sub"),
               str(tmp_path / "left" / "tree" / "link"))
    app.left.refresh()
    _point_at(app.left, "tree")
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])

    app._copy()

    assert read(str(tmp_path / "right" / "tree" / "a.txt")) == "A"
    assert "1 symlinked directory skipped" in app.message
    assert "link" in scripted.last_message


def test_a_move_that_skipped_a_link_says_the_source_was_kept(app, tmp_path,
                                                             monkeypatch):
    write(str(tmp_path / "left" / "tree" / "a.txt"), "A")
    (tmp_path / "left" / "tree" / "sub").mkdir()
    for name in ("link", "link2"):
        os.symlink(str(tmp_path / "left" / "tree" / "sub"),
                   str(tmp_path / "left" / "tree" / name))
    app.left.refresh()
    _point_at(app.left, "tree")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])

    app._move()

    assert read(str(tmp_path / "right" / "tree" / "a.txt")) == "A"
    assert "2 symlinked directories skipped, source kept" in app.message
    assert (tmp_path / "left" / "tree" / "a.txt").exists()


def test_a_transfer_with_nothing_selected_does_nothing(app, tmp_path,
                                                       monkeypatch):
    app.left.move_to(0)                      # the ".." entry
    _ScriptedDialogs(monkeypatch)
    opening = app.message
    app._copy()
    assert app.message == opening             # unchanged


def test_a_transfer_can_be_cancelled_at_the_prompt(app, tmp_path, monkeypatch):
    _files(app, tmp_path, a="payload")
    _point_at(app.left, "a")
    _ScriptedDialogs(monkeypatch, prompt=[None])
    app._copy()
    assert not (tmp_path / "right" / "a").exists()


def test_copying_onto_itself_is_refused(app, tmp_path, monkeypatch):
    _files(app, tmp_path, a="payload")
    _point_at(app.left, "a")
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "left")])
    app.right.set_location(app.left.fs, str(tmp_path / "left"))
    app._copy()
    assert "source and target are the same" in scripted.last_message
    assert read(str(tmp_path / "left" / "a")) == "payload"


def test_copying_onto_itself_is_refused_between_the_panes_too(app, tmp_path,
                                                              monkeypatch):
    """The panes hold two LocalFileSystem objects for the one disk.

    The guard used to ask ``same_fs``, which compares identity, so it never
    fired here -- and the copy emptied the file it was asked to copy.
    """
    _files(app, tmp_path, a="payload")
    _point_at(app.left, "a")
    assert app.left.fs is not app.right.fs
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "left")])
    app._copy()
    assert "source and target are the same" in scripted.last_message
    assert read(str(tmp_path / "left" / "a")) == "payload"


def test_copying_a_directory_into_its_own_subtree_is_refused(app, tmp_path,
                                                             monkeypatch):
    write(str(tmp_path / "left" / "tree" / "notes.txt"), "N")
    (tmp_path / "left" / "tree" / "backups").mkdir()
    app.left.refresh()
    _point_at(app.left, "tree")
    scripted = _ScriptedDialogs(
        monkeypatch, prompt=[str(tmp_path / "left" / "tree" / "backups")])
    app._copy()
    assert "into itself" in scripted.last_message
    assert not (tmp_path / "left" / "tree" / "backups" / "tree").exists()


def test_a_failing_copy_is_reported(app, tmp_path, monkeypatch):
    _files(app, tmp_path, a="payload")
    _point_at(app.left, "a")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(app_mod, "copy_path", explode)
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._copy()
    assert "disk full" in scripted.last_message


def test_a_copy_cancelled_mid_flight_says_so(app, tmp_path, monkeypatch,
                                             _quiet_progress):
    _files(app, tmp_path, a="one", b="two")
    app.left.selected = {"a", "b"}

    def cancel_immediately(*args, **kwargs):
        raise app_mod.OperationCancelled()

    monkeypatch.setattr(app_mod, "copy_path", cancel_immediately)
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._copy()
    assert "Copy cancelled" in app.message


def test_a_copy_cancelled_between_items(app, tmp_path, monkeypatch,
                                        _quiet_progress):
    """Cancelling between two items stops before the second is started."""
    _files(app, tmp_path, a="one", b="two")
    app.left.selected = {"a", "b"}
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])

    copied = []
    # A copy that ignores the cancel callback, so the only place the run can
    # stop is the check at the top of the loop.  It skips nothing, hence the
    # empty list every copy answers with.
    def fake_copy(*a, **k):
        copied.append(a[1])
        return []

    monkeypatch.setattr(app_mod, "copy_path", fake_copy)

    real_factory = dialogs.ProgressDialog

    def cancelling(stdscr, title):
        dlg = real_factory(stdscr, title)
        dlg.cancel_after = 0
        return dlg

    monkeypatch.setattr(dialogs, "ProgressDialog", cancelling)
    app._copy()
    assert len(copied) == 1
    assert "Copy cancelled" in app.message


def test_a_transfer_survives_a_source_it_cannot_measure(app, tmp_path,
                                                        monkeypatch):
    _files(app, tmp_path, a="payload")
    _point_at(app.left, "a")

    def explode(*args, **kwargs):
        raise OSError("cannot walk")

    monkeypatch.setattr(app_mod, "count_tree", explode)
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._copy()
    assert read(str(tmp_path / "right" / "a")) == "payload"


def test_the_progress_window_reports_bytes(app, tmp_path, monkeypatch,
                                           _quiet_progress):
    _files(app, tmp_path, a="payload")
    _point_at(app.left, "a")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._copy()
    assert _quiet_progress[0].updates
    assert "Copy 1/1: a" in _quiet_progress[0].overall
    assert _quiet_progress[0].closed is True


# -- mkdir ---------------------------------------------------------------------

def test_mkdir(app, tmp_path, monkeypatch):
    _ScriptedDialogs(monkeypatch, prompt=["newdir"])
    app._mkdir()
    assert (tmp_path / "left" / "newdir").is_dir()
    assert "Created newdir" in app.message
    assert app.left.current_name() == "newdir"


def test_mkdir_creates_intermediate_directories(app, tmp_path, monkeypatch):
    _ScriptedDialogs(monkeypatch, prompt=["a/b/c"])
    app._mkdir()
    assert (tmp_path / "left" / "a" / "b" / "c").is_dir()


def test_mkdir_can_be_cancelled(app, tmp_path, monkeypatch):
    for answer in (None, ""):
        _ScriptedDialogs(monkeypatch, prompt=[answer])
        app._mkdir()
    assert list((tmp_path / "left").iterdir())


def test_a_failing_mkdir_is_reported(app, tmp_path, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch, prompt=["file.txt"])
    app._mkdir()          # a file of that name already exists
    assert "Mkdir error" == scripted.messages[-1][0]


# -- touch ---------------------------------------------------------------------

def test_touch_creates_an_empty_file(app, tmp_path, monkeypatch):
    _ScriptedDialogs(monkeypatch, prompt=["notes.txt"])
    app._touch()
    assert read(str(tmp_path / "left" / "notes.txt")) == ""
    assert "Created notes.txt" in app.message
    assert app.left.current_name() == "notes.txt"


def test_touch_restamps_an_existing_file_without_emptying_it(app, tmp_path,
                                                             monkeypatch):
    path = tmp_path / "left" / "file.txt"
    os.utime(path, (123456.0, 123456.0))
    _ScriptedDialogs(monkeypatch, prompt=["file.txt"])
    app._touch()
    assert read(str(path)) == "left"
    assert path.stat().st_mtime > 123456.0
    assert "Touched file.txt" in app.message


def test_touch_can_be_cancelled(app, tmp_path, monkeypatch):
    before = sorted(p.name for p in (tmp_path / "left").iterdir())
    for answer in (None, ""):
        _ScriptedDialogs(monkeypatch, prompt=[answer])
        app._touch()
    assert sorted(p.name for p in (tmp_path / "left").iterdir()) == before


def test_touch_does_not_invent_the_directories_on_the_way(app, tmp_path,
                                                          monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch, prompt=["a/b/c.txt"])
    app._touch()
    assert not (tmp_path / "left" / "a").exists()
    assert scripted.messages[-1][0] == "New file error"
    assert scripted.messages[-1][2] is True


def test_touch_on_a_directory_restamps_it_the_way_the_shell_does(app, tmp_path,
                                                                 monkeypatch):
    (tmp_path / "left" / "adir").mkdir()
    os.utime(tmp_path / "left" / "adir", (123456.0, 123456.0))
    app.left.refresh()
    _ScriptedDialogs(monkeypatch, prompt=["adir"])
    app._touch()
    assert (tmp_path / "left" / "adir").is_dir()
    assert (tmp_path / "left" / "adir").stat().st_mtime > 123456.0
    assert "Touched adir" in app.message


# -- delete --------------------------------------------------------------------

def test_delete_a_file_after_confirmation(app, tmp_path, monkeypatch):
    _files(app, tmp_path, doomed="x")
    _point_at(app.left, "doomed")
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app._delete()
    assert not (tmp_path / "left" / "doomed").exists()
    assert "Deleted 1 item(s)" in app.message


def test_delete_can_be_declined(app, tmp_path, monkeypatch):
    _files(app, tmp_path, keep="x")
    _point_at(app.left, "keep")
    _ScriptedDialogs(monkeypatch, confirm=[False])
    app._delete()
    assert (tmp_path / "left" / "keep").exists()


def test_delete_with_nothing_selected_does_nothing(app, monkeypatch):
    app.left.move_to(0)
    _ScriptedDialogs(monkeypatch)
    app._delete()


def test_deleting_several_items_warns_about_directories(app, tmp_path,
                                                        monkeypatch):
    _files(app, tmp_path, a="x")
    app.left.selected = {"a", "sub"}
    scripted = _ScriptedDialogs(monkeypatch, confirm=[True])
    app._delete()
    assert "Delete 2 selected items?" in scripted.messages[0][1]
    assert "removed recursively" in scripted.messages[0][1]
    assert not (tmp_path / "left" / "sub").exists()


def test_a_failing_delete_is_reported(app, tmp_path, monkeypatch):
    _files(app, tmp_path, stubborn="x")
    _point_at(app.left, "stubborn")

    def explode(path):
        raise OSError("permission denied")

    monkeypatch.setattr(app.left.fs, "delete_tree", explode)
    scripted = _ScriptedDialogs(monkeypatch, confirm=[True])
    app._delete()
    assert "permission denied" in scripted.last_message


def test_a_delete_cancelled_from_the_progress_window(app, tmp_path, monkeypatch):
    _files(app, tmp_path, a="x", b="y")
    app.left.selected = {"a", "b"}
    _ScriptedDialogs(monkeypatch, confirm=[True])

    real_factory = dialogs.ProgressDialog

    def cancelling(stdscr, title):
        dlg = real_factory(stdscr, title)
        dlg.cancel_after = 0
        return dlg

    monkeypatch.setattr(dialogs, "ProgressDialog", cancelling)
    app._delete()
    # One of the two survived because the run was stopped.
    remaining = {p.name for p in (tmp_path / "left").iterdir()}
    assert remaining & {"a", "b"}


# -- where the cursor is left after a delete -----------------------------------

def _listing(app, tmp_path, *names):
    for name in names:
        write(str(tmp_path / "left" / name), "x")
    app.left.refresh()
    return [e.name for e in app.left.entries]


def test_the_cursor_lands_on_the_next_file_down(app, tmp_path, monkeypatch):
    _listing(app, tmp_path, "a.txt", "b.txt", "c.txt")
    _point_at(app.left, "b.txt")
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app._delete()
    assert app.left.current_name() == "c.txt"


def test_deleting_the_last_file_walks_the_cursor_back_up(app, tmp_path,
                                                         monkeypatch):
    """Nothing below to land on, so the cursor takes the entry above."""
    listing = _listing(app, tmp_path, "a.txt", "b.txt", "c.txt")
    last, second_last = listing[-1], listing[-2]
    _point_at(app.left, last)
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app._delete()
    assert app.left.current_name() == second_last


def test_tagged_files_are_stepped_over_together(app, tmp_path, monkeypatch):
    """The cursor clears the whole tagged block, not just the entry under it."""
    _listing(app, tmp_path, "a.txt", "b.txt", "c.txt", "d.txt")
    _point_at(app.left, "a.txt")
    app.left.selected = {"a.txt", "b.txt", "c.txt"}
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app._delete()
    assert app.left.current_name() == "d.txt"


def test_a_delete_that_fails_leaves_the_cursor_on_something_real(app, tmp_path,
                                                                 monkeypatch):
    """The kept name was never deleted, so it is still there to land on."""
    _listing(app, tmp_path, "a.txt", "b.txt", "c.txt")
    _point_at(app.left, "b.txt")

    def explode(path):
        raise OSError("permission denied")

    monkeypatch.setattr(app.left.fs, "delete_tree", explode)
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app._delete()
    assert app.left.current_name() == "c.txt"
    assert (tmp_path / "left" / "b.txt").exists()


def test_emptying_the_directory_leaves_the_cursor_somewhere_valid(app, tmp_path,
                                                                  monkeypatch):
    import shutil

    for stale in (tmp_path / "left").iterdir():
        if stale.is_file():
            stale.unlink()
        else:
            shutil.rmtree(stale)
    _listing(app, tmp_path, "only.txt")
    app.left.selected = {"only.txt"}
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app._delete()
    # All that is left is the ".." entry, and the cursor is on it.
    assert app.left.current_name() == ".."


# -- sync ----------------------------------------------------------------------

def test_sync_copies_the_missing_files(app, tmp_path, monkeypatch):
    _files(app, tmp_path, only_left="payload")
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app._sync()
    assert read(str(tmp_path / "right" / "only_left")) == "payload"
    assert "Synchronized" in app.message


def test_sync_reports_when_there_is_nothing_to_do(app, tmp_path, monkeypatch):
    for side in ("left", "right"):
        for stale in (tmp_path / side).iterdir():
            if stale.is_file():
                stale.unlink()
            else:
                import shutil

                shutil.rmtree(stale)
    app.left.refresh()
    app.right.refresh()
    scripted = _ScriptedDialogs(monkeypatch)
    app._sync()
    assert "already in sync" in scripted.last_message
    assert app.message == "Already in sync"


def test_sync_can_be_declined_at_the_preview(app, tmp_path, monkeypatch):
    _files(app, tmp_path, only_left="payload")
    _ScriptedDialogs(monkeypatch, confirm=[False])
    app._sync()
    assert not (tmp_path / "right" / "only_left").exists()
    assert "Sync cancelled" in app.message


def test_the_sync_preview_lists_the_actions_and_truncates(app, tmp_path,
                                                          monkeypatch):
    for i in range(15):
        write(str(tmp_path / "left" / f"f{i:02d}.txt"), "x")
    app.left.refresh()
    scripted = _ScriptedDialogs(monkeypatch, confirm=[False])
    app._sync()
    preview = scripted.messages[0][1]
    assert "file(s)" in preview
    assert "and " in preview and "more" in preview
    assert "'->' copy left->right" in preview


def _crowd(app, tmp_path, files=0, dirs=0):
    """Fill the left pane with more than anyone should want to sync."""
    for i in range(files):
        write(str(tmp_path / "left" / f"f{i:04d}.txt"), "x")
    for i in range(dirs):
        (tmp_path / "left" / f"d{i:04d}").mkdir()
    app.left.refresh()


def test_a_crowded_directory_is_queried_before_the_scan(app, tmp_path,
                                                        monkeypatch,
                                                        _quiet_progress):
    _crowd(app, tmp_path, files=sync_mod.LARGE_FILE_COUNT)
    scripted = _ScriptedDialogs(monkeypatch, confirm=[False])
    app._sync()

    title, text, _err = scripted.messages[0]
    assert title == "Synchronize panes"
    # The conftest pane already holds a file of its own.
    assert f"{sync_mod.LARGE_FILE_COUNT + 1:,} files" in text
    assert "Scan these two directories anyway?" in text
    assert app.message == "Sync cancelled"
    # Declining costs nothing: the expensive half never ran.
    assert _quiet_progress == []


def test_a_pane_full_of_subdirectories_is_queried_too(app, tmp_path,
                                                      monkeypatch):
    _crowd(app, tmp_path, dirs=sync_mod.LARGE_DIR_COUNT)
    scripted = _ScriptedDialogs(monkeypatch, confirm=[False])
    app._sync()
    # The conftest pane already holds one subdirectory of its own.
    assert f"{sync_mod.LARGE_DIR_COUNT + 1:,} subdirectories" in scripted.all_text
    assert app.message == "Sync cancelled"


def test_the_warning_names_both_panes_and_flags_the_big_one(app, tmp_path,
                                                            monkeypatch):
    _crowd(app, tmp_path, files=sync_mod.LARGE_FILE_COUNT)
    scripted = _ScriptedDialogs(monkeypatch, confirm=[False])
    app._sync()

    text = scripted.messages[0][1]
    assert str(tmp_path / "left") in text and str(tmp_path / "right") in text
    # Only the offending side is marked, so the user can see which one it is.
    flagged = [line for line in text.splitlines() if "<-- large" in line]
    assert len(flagged) == 1
    assert "nothing is ever deleted" in text.lower()


def test_the_sync_goes_ahead_when_the_warning_is_accepted(app, tmp_path,
                                                          monkeypatch):
    _crowd(app, tmp_path, files=sync_mod.LARGE_FILE_COUNT)
    # Once for the warning, once for the plan preview.
    _ScriptedDialogs(monkeypatch, confirm=[True, True])
    app._sync()
    assert (tmp_path / "right" / "f0000.txt").exists()
    assert "Synchronized" in app.message


def test_an_ordinary_pair_of_directories_is_not_queried(app, tmp_path,
                                                        monkeypatch):
    """The warning stays out of the way of the syncs people actually do."""
    _files(app, tmp_path, only_left="payload")
    scripted = _ScriptedDialogs(monkeypatch, confirm=[True])
    app._sync()
    # The first thing the user saw was the plan, not a question about size.
    assert "to copy:" in scripted.messages[0][1]
    assert "anyway?" not in scripted.all_text


def test_the_scan_is_shown_and_can_be_cancelled(app, tmp_path, monkeypatch,
                                                _quiet_progress):
    """The slow half reports in, and Esc during it stops the whole sync."""
    _files(app, tmp_path, only_left="payload")
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app._sync()

    scan = _quiet_progress[0]
    assert scan.title == "Scanning for differences"
    assert scan.closed
    # It counted out loud while walking, with no denominator to divide by.
    assert scan.updates and all(total == 0 for _cur, total, _label in scan.updates)
    assert any("files" in label for _c, _t, label in scan.updates)


def test_a_scan_cancelled_by_the_user_copies_nothing(app, tmp_path, monkeypatch):
    def give_up(*args, **kwargs):
        raise OperationCancelled()

    _files(app, tmp_path, only_left="payload")
    monkeypatch.setattr(app_mod, "build_sync_plan", give_up)
    scripted = _ScriptedDialogs(monkeypatch, confirm=[True])
    app._sync()
    # Cancelling is not an error: no dialog, just the status line.
    assert app.message == "Sync cancelled"
    assert scripted.messages == []
    assert not (tmp_path / "right" / "only_left").exists()


def test_a_sync_that_cannot_scan_is_reported(app, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("cannot list")

    monkeypatch.setattr(app_mod, "build_sync_plan", explode)
    scripted = _ScriptedDialogs(monkeypatch)
    app._sync()
    assert "cannot list" in scripted.last_message


def test_a_sync_that_fails_mid_run_is_reported(app, tmp_path, monkeypatch):
    _files(app, tmp_path, only_left="payload")

    def explode(*args, **kwargs):
        raise OSError("connection lost")

    monkeypatch.setattr(app_mod, "execute_sync_plan", explode)
    scripted = _ScriptedDialogs(monkeypatch, confirm=[True])
    app._sync()
    assert "connection lost" in scripted.last_message


# -- view and edit -------------------------------------------------------------

def test_view_opens_the_file_under_the_cursor(app, tmp_path, monkeypatch):
    _files(app, tmp_path, readme="hello")
    _point_at(app.left, "readme")
    opened = []

    class _Viewer:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            return None

    monkeypatch.setattr(app_mod, "viewer_for", _Viewer)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    app._view()
    assert opened == [str(tmp_path / "left" / "readme")]


def test_view_on_a_directory_enters_it(app, tmp_path, monkeypatch):
    _point_at(app.left, "sub")
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    app._view()
    assert app.left.path == str(tmp_path / "left" / "sub")


def test_view_with_nothing_selected_does_nothing(app, monkeypatch):
    app.left.move_to(0)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    app._view()


def test_a_viewer_that_fails_is_reported(app, tmp_path, monkeypatch):
    _files(app, tmp_path, readme="hello")
    _point_at(app.left, "readme")

    class _Broken:
        def __init__(self, fs, path):
            raise OSError("cannot read")

    monkeypatch.setattr(app_mod, "viewer_for", _Broken)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    scripted = _ScriptedDialogs(monkeypatch)
    app._view()
    assert "cannot read" in scripted.last_message


# -- view with an outside pager ------------------------------------------------

def _paging(monkeypatch, app, command="less"):
    """Point [ui] viewer at ``command`` and capture what would be run."""
    from meridian_commander import config as config_mod

    monkeypatch.setattr(config_mod, "external_viewer", lambda: command)
    ran = []
    monkeypatch.setattr(app, "_suspend_and_run",
                        lambda cmd, cwd, banner, **kw: ran.append(cmd) or 0)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    return ran


def test_a_text_file_goes_to_the_configured_pager(app, tmp_path, monkeypatch):
    _files(app, tmp_path, **{"notes.txt": "hello"})
    _point_at(app.left, "notes.txt")
    opened = []
    monkeypatch.setattr(app_mod, "viewer_for",
                        lambda fs, path: opened.append(path))
    ran = _paging(monkeypatch, app)
    app._view()
    assert ran == [["less", str(tmp_path / "left" / "notes.txt")]]
    assert opened == []                     # the built-in viewer stayed out
    assert "Viewed notes.txt" in app.message


def test_a_file_with_a_browser_of_its_own_keeps_it(app, tmp_path, monkeypatch):
    """less on a .xlsx is a screen of zip bytes; the grid wins."""
    _files(app, tmp_path, **{"book.xlsx": "not really a workbook"})
    _point_at(app.left, "book.xlsx")
    opened = []

    class _Viewer:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            return None

    monkeypatch.setattr(app_mod, "viewer_for", _Viewer)
    ran = _paging(monkeypatch, app)
    app._view()
    assert opened == [str(tmp_path / "left" / "book.xlsx")]
    assert ran == []


def test_an_unusable_viewer_setting_falls_back_and_says_so(app, tmp_path,
                                                           monkeypatch):
    _files(app, tmp_path, **{"notes.txt": "hello"})
    _point_at(app.left, "notes.txt")
    opened = []

    class _Viewer:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            return None

    monkeypatch.setattr(app_mod, "viewer_for", _Viewer)
    ran = _paging(monkeypatch, app, command="less 'unclosed")
    scripted = _ScriptedDialogs(monkeypatch)
    app._view()
    assert "[ui] viewer" in scripted.messages[-1][1]
    assert ran == []
    assert opened == [str(tmp_path / "left" / "notes.txt")]


def test_edit_opens_the_file_under_the_cursor(app, tmp_path, monkeypatch):
    _files(app, tmp_path, notes="hello")
    _point_at(app.left, "notes")
    opened = []

    class _Editor:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            return None

    monkeypatch.setattr(app_mod, "Editor", _Editor)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    app._edit()
    assert opened == [str(tmp_path / "left" / "notes")]


def test_edit_refuses_a_directory(app, monkeypatch):
    _point_at(app.left, "sub")
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    app._edit()
    assert "Cannot edit a directory" in app.message


def test_edit_offers_to_create_a_new_file(app, tmp_path, monkeypatch):
    app.left.entries = []                    # nothing to edit: offer a new file
    opened = []

    class _Editor:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            return None

    monkeypatch.setattr(app_mod, "Editor", _Editor)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    _ScriptedDialogs(monkeypatch, prompt=["brand-new.txt"])
    app._edit()
    assert opened == [str(tmp_path / "left" / "brand-new.txt")]


def test_declining_to_name_a_new_file(app, monkeypatch):
    app.left.entries = []
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    _ScriptedDialogs(monkeypatch, prompt=[None])
    app._edit()


def test_an_editor_that_fails_is_reported(app, tmp_path, monkeypatch):
    _files(app, tmp_path, notes="hello")
    _point_at(app.left, "notes")

    class _Broken:
        def __init__(self, fs, path):
            raise OSError("read-only")

    monkeypatch.setattr(app_mod, "Editor", _Broken)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    scripted = _ScriptedDialogs(monkeypatch)
    app._edit()
    assert "read-only" in scripted.last_message


# -- editing with an outside editor --------------------------------------------

def _external(monkeypatch, command):
    """Point [ui] editor at ``command`` without touching a real config file."""
    from meridian_commander import config as config_mod
    monkeypatch.setattr(config_mod, "external_editor", lambda: command)


def _no_built_in_editor(monkeypatch):
    """Fail loudly if the built-in editor is opened after all."""
    class _Unexpected:
        def __init__(self, fs, path):
            raise AssertionError("the built-in editor should not have opened")

    monkeypatch.setattr(app_mod, "Editor", _Unexpected)


def test_edit_hands_the_file_to_the_configured_editor(app, tmp_path,
                                                      monkeypatch):
    _files(app, tmp_path, notes="hello")
    _point_at(app.left, "notes")
    _external(monkeypatch, "vim")
    _no_built_in_editor(monkeypatch)
    calls = []
    monkeypatch.setattr(app, "_suspend_and_run",
                        lambda cmd, cwd, banner, **kw: calls.append((cmd, cwd)) or 0)

    app._edit()
    assert calls == [(["vim", str(tmp_path / "left" / "notes")],
                      str(tmp_path / "left"))]
    assert app.message == "Edited notes"


def test_the_pane_is_refreshed_after_an_outside_editor_ran(app, tmp_path,
                                                           monkeypatch):
    """It may have created the file, or changed its size."""
    app.left.entries = []
    _external(monkeypatch, "vim")
    _no_built_in_editor(monkeypatch)
    _ScriptedDialogs(monkeypatch, prompt=["brand-new.txt"])

    def run(cmd, cwd, banner, **kw):
        write(cmd[-1], "made by the editor")
        return 0

    monkeypatch.setattr(app, "_suspend_and_run", run)
    app._edit()
    assert "brand-new.txt" in [entry.name for entry in app.left.entries]


def test_a_setting_that_cannot_work_is_reported_and_the_built_in_used(
        app, tmp_path, monkeypatch):
    _files(app, tmp_path, notes="hello")
    _point_at(app.left, "notes")
    monkeypatch.delenv("EDITOR", raising=False)
    _external(monkeypatch, "$EDITOR")
    opened = []

    class _Editor:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            return None

    monkeypatch.setattr(app_mod, "Editor", _Editor)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    scripted = _ScriptedDialogs(monkeypatch)

    app._edit()
    assert "does not set $EDITOR" in scripted.messages[0][1]
    assert opened == [str(tmp_path / "left" / "notes")]


def test_an_outside_editor_really_changes_the_file(app, tmp_path, monkeypatch):
    """End to end: the real suspend/resume path, and a real program.

    Everything above stubs the subprocess out, which would not notice the
    file never reaching the command's argv.  Here a shell writes it.
    """
    _files(app, tmp_path, notes="before")
    _point_at(app.left, "notes")
    _external(monkeypatch, """sh -c 'printf after > "$1"' --""")
    _no_built_in_editor(monkeypatch)
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(app.stdscr, "clearok", lambda flag: None,
                        raising=False)
    monkeypatch.setattr(app.stdscr, "refresh", lambda: None, raising=False)

    app._edit()
    assert read(str(tmp_path / "left" / "notes")) == "after"
    assert app.message == "Edited notes"


def test_a_plugin_file_is_edited_with_the_configured_editor_too(app, tmp_path,
                                                                monkeypatch):
    """The configuration menu's editing goes through the same choice."""
    _external(monkeypatch, "vim")
    _no_built_in_editor(monkeypatch)
    calls = []
    monkeypatch.setattr(app, "_suspend_and_run",
                        lambda cmd, cwd, banner, **kw: calls.append(cmd) or 0)

    path = str(tmp_path / "plug.py")
    write(path, "x = 1")
    app._edit_local_file(LocalFileSystem(), path)
    assert calls == [["vim", path]]


# -- misc ----------------------------------------------------------------------

def test_help_is_shown(app, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch)
    app._help()
    assert scripted.messages[0][0] == "Help"
    assert "two-pane terminal file manager" in scripted.last_message
    assert "~" in scripted.last_message and "presets" in scripted.last_message


def test_the_sort_menu_sets_the_order(app, monkeypatch):
    _ScriptedDialogs(monkeypatch, menu=["Size"])
    app._sort_menu()
    assert app.active.sort_key == "size"
    assert "Sorted by size" in app.message


def test_the_sort_menu_can_be_cancelled(app, monkeypatch):
    before = app.active.sort_key
    _ScriptedDialogs(monkeypatch, menu=[None])
    app._sort_menu()
    assert app.active.sort_key == before


def test_go_to_path(app, tmp_path, monkeypatch):
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "left" / "sub")])
    app._go_to_path()
    assert app.left.path == str(tmp_path / "left" / "sub")


def test_go_to_path_can_be_cancelled(app, tmp_path, monkeypatch):
    before = app.left.path
    for answer in (None, ""):
        _ScriptedDialogs(monkeypatch, prompt=[answer])
        app._go_to_path()
    assert app.left.path == before


def test_go_to_a_path_that_will_not_open(app, tmp_path, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch,
                                prompt=[str(tmp_path / "nowhere")])
    app._go_to_path()
    assert "Cannot open" in scripted.last_message


def test_swapping_the_panes(app, tmp_path):
    left_path, right_path = app.left.path, app.right.path
    app._swap_panes()
    assert app.left.path == right_path
    assert app.right.path == left_path


def test_activating_a_directory_enters_it(app, tmp_path, monkeypatch):
    _point_at(app.left, "sub")
    app._activate_entry()
    assert app.left.path == str(tmp_path / "left" / "sub")


def test_activating_a_file_views_it(app, tmp_path, monkeypatch):
    _files(app, tmp_path, readme="hello")
    _point_at(app.left, "readme")
    opened = []

    class _Viewer:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            return None

    monkeypatch.setattr(app_mod, "viewer_for", _Viewer)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    app._activate_entry()
    assert opened


def test_closing_the_backends(app, monkeypatch):
    closed = []

    class _Backend(LocalFileSystem):
        def close(self):
            closed.append(True)

    class _Stubborn(LocalFileSystem):
        def close(self):
            raise OSError("already gone")

    class _Plugin:
        def on_exit(self):
            closed.append("plugin")

    class _BrokenPlugin:
        def on_exit(self):
            raise RuntimeError("exit failed")

    app._backends = [_Backend(), _Stubborn()]
    app.left.plugin = _Plugin()
    app.right.plugin = _BrokenPlugin()
    app._close_backends()
    assert "plugin" in closed
    assert app.left.plugin is None and app.right.plugin is None
