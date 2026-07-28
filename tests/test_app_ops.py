"""The application's file operations: copy, move, delete, mkdir, sync, view."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import app as app_mod
from meridian_commander import dialogs
from meridian_commander.filesystems import LocalFileSystem

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


def test_a_transfer_with_nothing_selected_does_nothing(app, tmp_path,
                                                       monkeypatch):
    app.left.move_to(0)                      # the ".." entry
    _ScriptedDialogs(monkeypatch)
    app._copy()
    assert app.message.startswith("F1/?")     # unchanged


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
    # stop is the check at the top of the loop.
    monkeypatch.setattr(app_mod, "copy_path",
                        lambda *a, **k: copied.append(a[1]))

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
