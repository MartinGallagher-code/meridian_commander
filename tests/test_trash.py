# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""F8 to the trash on local panes (MC-018), and T to look in it (MC-024)."""

from __future__ import annotations

import curses
import os
import stat

import pytest

from meridian_commander import app as app_mod
from meridian_commander import config, trash
from meridian_commander.app import MENUS
from meridian_commander.trash import TrashUnavailable, move_to_trash

from support import (
    _FakeRemoteBackend,
    _ScriptedDialogs,
    read,
    write,
    write_zip,
)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(trash.sys, "platform", "linux")
    return tmp_path / "data" / "Trash"


def _info(trash_dir, name):
    return (trash_dir / "info" / f"{name}.trashinfo").read_text()


# -- the FreeDesktop trash ---------------------------------------------------------

def test_a_file_goes_to_the_home_trash_with_its_info(tmp_path, data):
    write(str(tmp_path / "my notes é.txt"), "x")
    where = move_to_trash(str(tmp_path / "my notes é.txt"))
    assert where == str(data / "files" / "my notes é.txt")
    assert not (tmp_path / "my notes é.txt").exists()
    info = _info(data, "my notes é.txt")
    assert info.startswith("[Trash Info]\nPath=")
    assert "my%20notes%20%C3%A9.txt" in info and str(tmp_path).replace(" ", "%20") in info
    assert "\nDeletionDate=" in info


def test_a_name_already_in_the_trash_is_never_overwritten(tmp_path, data):
    for content in ("first", "second", "third"):
        write(str(tmp_path / "a.txt"), content)
        move_to_trash(str(tmp_path / "a.txt"))
    assert read(str(data / "files" / "a.txt")) == "first"
    assert read(str(data / "files" / "a.2.txt")) == "second"
    assert read(str(data / "files" / "a.3.txt")) == "third"
    assert (data / "info" / "a.3.txt.trashinfo").exists()


def test_a_directory_and_a_link_go_in_themselves(tmp_path, data):
    write(str(tmp_path / "d" / "inner"), "x")
    write(str(tmp_path / "target"), "keep me")
    os.symlink("target", tmp_path / "link")
    move_to_trash(str(tmp_path / "d"))
    move_to_trash(str(tmp_path / "link"))
    assert (data / "files" / "d" / "inner").exists()
    assert os.path.islink(data / "files" / "link")
    assert read(str(tmp_path / "target")) == "keep me"


def test_a_move_that_fails_takes_its_info_back(tmp_path, data, monkeypatch):
    write(str(tmp_path / "f"), "x")

    def refuse(src, dst):
        raise PermissionError("busy")

    monkeypatch.setattr(trash.os, "rename", refuse)
    with pytest.raises(PermissionError):
        move_to_trash(str(tmp_path / "f"))
    assert list((data / "info").iterdir()) == []


def test_the_mount_point_is_an_ancestor_on_the_same_disk(tmp_path):
    top = trash._mount_point(str(tmp_path))
    assert str(tmp_path).startswith(top)
    assert os.lstat(top).st_dev == os.lstat(str(tmp_path)).st_dev


def _other_disk(monkeypatch, tmp_path, data):
    """Make the home trash look as if it were on another disk."""
    real_stat = os.stat
    home = str(data)

    def stat(path, *args, **kwargs):
        found = real_stat(path, *args, **kwargs)
        if str(path) == home:
            # The real answer on another device, so that whatever else is
            # asked of it -- is it a directory? -- is still answered.
            fields = list(found[:10])
            fields[2] = -1
            return os.stat_result(fields)
        return found

    monkeypatch.setattr(trash.os, "stat", stat)
    disk = tmp_path / "disk"
    disk.mkdir()
    monkeypatch.setattr(trash, "_mount_point", lambda path: str(disk))
    return disk


def test_another_disk_uses_its_own_trash_dash_uid(tmp_path, data, monkeypatch):
    disk = _other_disk(monkeypatch, tmp_path, data)
    write(str(disk / "sub" / "f"), "x")
    where = move_to_trash(str(disk / "sub" / "f"))
    own = disk / f".Trash-{os.getuid()}"
    assert where == str(own / "files" / "f")
    assert "Path=sub/f\n" in _info(own, "f")      # relative to the disk's top


def test_a_shared_sticky_trash_is_preferred(tmp_path, data, monkeypatch):
    disk = _other_disk(monkeypatch, tmp_path, data)
    (disk / ".Trash").mkdir()
    os.chmod(disk / ".Trash", 0o1777)
    write(str(disk / "f"), "x")
    where = move_to_trash(str(disk / "f"))
    assert where == str(disk / ".Trash" / str(os.getuid()) / "files" / "f")


def test_a_shared_trash_without_the_sticky_bit_is_passed_over(tmp_path, data,
                                                             monkeypatch):
    disk = _other_disk(monkeypatch, tmp_path, data)
    (disk / ".Trash").mkdir()
    os.chmod(disk / ".Trash", 0o777)
    write(str(disk / "f"), "x")
    assert f".Trash-{os.getuid()}" in move_to_trash(str(disk / "f"))


def test_a_disk_with_no_usable_trash_says_so(tmp_path, data, monkeypatch):
    disk = _other_disk(monkeypatch, tmp_path, data)
    (disk / ".Trash").mkdir()
    os.chmod(disk / ".Trash", 0o1777)
    monkeypatch.setattr(trash, "_usable", lambda directory: False)
    write(str(disk / "f"), "x")
    with pytest.raises(TrashUnavailable):
        move_to_trash(str(disk / "f"))


def test_a_trash_that_cannot_be_made_is_not_usable(tmp_path):
    write(str(tmp_path / "a-file"), "x")
    assert trash._usable(str(tmp_path / "a-file" / "Trash")) is False
    assert trash._usable(str(tmp_path / "fine")) is True


# -- macOS -----------------------------------------------------------------------

def test_macos_uses_its_trash_with_unique_names(tmp_path, monkeypatch):
    monkeypatch.setattr(trash.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for content in ("one", "two"):
        write(str(tmp_path / "a.txt"), content)
        move_to_trash(str(tmp_path / "a.txt"))
    assert read(str(tmp_path / "home" / ".Trash" / "a.txt")) == "one"
    assert read(str(tmp_path / "home" / ".Trash" / "a 2.txt")) == "two"


def test_macos_says_when_it_cannot(tmp_path, monkeypatch):
    monkeypatch.setattr(trash.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write(str(tmp_path / "f"), "x")

    def refuse(src, dst):
        raise OSError("cross-device")

    monkeypatch.setattr(trash.os, "rename", refuse)
    with pytest.raises(TrashUnavailable, match="cross-device"):
        move_to_trash(str(tmp_path / "f"))


# -- F8 ------------------------------------------------------------------------------

@pytest.fixture
def trashing(app, data, monkeypatch):
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

    monkeypatch.setattr("meridian_commander.dialogs.ProgressDialog", _Progress)
    app.use_trash = True
    return app


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def test_f8_offers_the_trash_first(trashing, tmp_path, data, monkeypatch):
    _point_at(trashing.left, "file.txt")
    scripted = _ScriptedDialogs(monkeypatch, menu=["Move to trash"])
    trashing._delete()
    title, options = scripted.menus[0]
    assert options == ["Move to trash", "Delete permanently", "Cancel"]
    assert (data / "files" / "file.txt").exists()
    assert trashing.message == "Moved 1 item(s) to the trash"


def test_f8_can_still_delete_for_good(trashing, tmp_path, data, monkeypatch):
    _point_at(trashing.left, "file.txt")
    _ScriptedDialogs(monkeypatch, menu=["Delete permanently"])
    trashing._delete()
    assert not (tmp_path / "left" / "file.txt").exists()
    assert not (data / "files" / "file.txt").exists()
    assert trashing.message == "Deleted 1 item(s)"


@pytest.mark.parametrize("answer", ["Cancel", None])
def test_cancel_deletes_nothing(trashing, tmp_path, monkeypatch, answer):
    _point_at(trashing.left, "file.txt")
    _ScriptedDialogs(monkeypatch, menu=[answer])
    trashing._delete()
    assert (tmp_path / "left" / "file.txt").exists()


def test_a_remote_pane_says_it_cannot_be_undone(trashing, tmp_path, monkeypatch):
    trashing.left.set_location(_FakeRemoteBackend(), str(tmp_path / "left"))
    _point_at(trashing.left, "file.txt")
    scripted = _ScriptedDialogs(monkeypatch, confirm=[False])
    trashing._delete()
    assert scripted.messages[0][1].endswith("This cannot be undone.")


def _no_trash(monkeypatch):
    def refuse(path):
        raise TrashUnavailable("none here")

    monkeypatch.setattr(app_mod, "move_to_trash", refuse)


def test_items_with_no_trash_can_be_deleted_for_good(trashing, tmp_path,
                                                    monkeypatch):
    _no_trash(monkeypatch)
    _point_at(trashing.left, "file.txt")
    scripted = _ScriptedDialogs(monkeypatch,
                                menu=["Move to trash", "Delete permanently"])
    trashing._delete()
    assert "have no trash on their disk" in scripted.menu_texts[1]
    assert not (tmp_path / "left" / "file.txt").exists()
    assert trashing.message == "Moved 0 item(s) to the trash, deleted 1"


def test_items_with_no_trash_can_be_skipped(trashing, tmp_path, monkeypatch):
    _no_trash(monkeypatch)
    _point_at(trashing.left, "file.txt")
    _ScriptedDialogs(monkeypatch, menu=["Move to trash", "Skip them"])
    trashing._delete()
    assert (tmp_path / "left" / "file.txt").exists()


def test_a_homeless_item_that_will_not_go_is_reported(trashing, monkeypatch):
    _no_trash(monkeypatch)

    def busy(path):
        raise OSError("device busy")

    monkeypatch.setattr(trashing.left.fs, "delete_tree", busy)
    _point_at(trashing.left, "file.txt")
    scripted = _ScriptedDialogs(monkeypatch,
                                menu=["Move to trash", "Delete permanently"])
    trashing._delete()
    assert "device busy" in scripted.last_message


def test_a_trash_that_fails_is_an_error(trashing, monkeypatch):
    def refuse(path):
        raise PermissionError("read-only")

    monkeypatch.setattr(app_mod, "move_to_trash", refuse)
    _point_at(trashing.left, "file.txt")
    scripted = _ScriptedDialogs(monkeypatch, menu=["Move to trash"])
    trashing._delete()
    assert "read-only" in scripted.last_message


# -- the setting -------------------------------------------------------------------

def test_the_setting_is_remembered_and_ticked(app):
    assert config.trash_enabled() is True        # on unless it says no
    app.use_trash = True
    app._toggle_trash()
    assert app.use_trash is False and config.trash_enabled() is False
    assert app.message == "F8 deletes permanently"
    options = next(i for i, m in enumerate(MENUS) if m["name"] == "options")
    entry = next(e for e in app._menu_items(options) if e.get("name") == "trash")
    assert entry["checked"] is False
    long = next(e for e in app._menu_items(options) if e.get("name") == "long")
    assert long["checked"] is False
    app._toggle_trash()
    assert app.message == "F8 offers the trash" and config.save_trash(True)


def test_an_unsaved_setting_says_so(app, monkeypatch):
    monkeypatch.setattr(config, "_save_ui", lambda key, value: False)
    app.use_trash = True
    app._toggle_trash()
    assert app.message.endswith("(could not be saved)")


def test_a_home_trash_that_cannot_be_written_says_so(tmp_path, data,
                                                    monkeypatch):
    monkeypatch.setattr(trash, "_usable", lambda directory: False)
    write(str(tmp_path / "f"), "x")
    with pytest.raises(TrashUnavailable, match="cannot write to the trash"):
        move_to_trash(str(tmp_path / "f"))


# -- where the trash is, looking only (MC-024) ---------------------------------

def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_location_is_the_home_trash_from_xdg_data_home(data):
    assert not data.exists()
    assert trash.trash_location() == str(data / "files")
    # Made as F8 would make it, so the pane opens on an empty trash.
    for made in (data, data / "files", data / "info"):
        assert made.is_dir() and _mode(made) == 0o700


def test_the_location_defaults_to_local_share(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(trash.sys, "platform", "linux")
    assert trash.trash_location() == str(
        tmp_path / "home" / ".local" / "share" / "Trash" / "files")


def test_a_home_trash_that_cannot_be_made_is_still_named(tmp_path,
                                                        monkeypatch):
    write(str(tmp_path / "not-a-dir"), "x")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "not-a-dir"))
    monkeypatch.setattr(trash.sys, "platform", "linux")
    # The listing that follows reports what is wrong with it.
    assert trash.trash_location() == str(
        tmp_path / "not-a-dir" / "Trash" / "files")


def test_the_location_on_macos_is_dot_trash(tmp_path, monkeypatch):
    monkeypatch.setattr(trash.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert trash.trash_location(str(tmp_path)) == str(
        tmp_path / "home" / ".Trash")
    assert (tmp_path / "home" / ".Trash").is_dir()


def test_a_macos_trash_that_cannot_be_made_is_still_named(tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(trash.sys, "platform", "darwin")
    write(str(tmp_path / "home"), "a file where a home should be")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert trash.trash_location() == str(tmp_path / "home" / ".Trash")


def test_a_place_on_the_home_disk_shows_the_home_trash(tmp_path, data):
    assert trash.trash_location(str(tmp_path)) == str(data / "files")


def test_a_place_that_has_gone_shows_the_home_trash(tmp_path, data):
    assert trash.trash_location(str(tmp_path / "gone")) == str(data / "files")


def test_looking_from_another_disk_creates_nothing_there(tmp_path, data,
                                                        monkeypatch):
    disk = _other_disk(monkeypatch, tmp_path, data)
    (disk / ".Trash").mkdir()
    os.chmod(disk / ".Trash", 0o1777)
    assert trash.trash_location(str(disk)) == str(data / "files")
    assert sorted(os.listdir(disk)) == [".Trash"]
    assert os.listdir(disk / ".Trash") == []


def test_another_disk_with_a_trash_dash_uid_shows_it(tmp_path, data,
                                                    monkeypatch):
    disk = _other_disk(monkeypatch, tmp_path, data)
    write(str(disk / "f"), "x")
    move_to_trash(str(disk / "f"))              # F8 makes the disk's trash
    own = disk / f".Trash-{os.getuid()}" / "files"
    assert trash.trash_location(str(disk)) == str(own)


def test_another_disks_shared_trash_is_shown_only_if_sticky(tmp_path, data,
                                                           monkeypatch):
    disk = _other_disk(monkeypatch, tmp_path, data)
    mine = disk / ".Trash" / str(os.getuid()) / "files"
    mine.mkdir(parents=True)
    os.chmod(disk / ".Trash", 0o1777)
    assert trash.trash_location(str(disk)) == str(mine)
    os.chmod(disk / ".Trash", 0o777)            # anyone could have made it
    assert trash.trash_location(str(disk)) == str(data / "files")


# -- telling a pane it is in the trash ----------------------------------------

def test_the_trash_root_is_found_from_files_and_below(tmp_path, data):
    files = trash.trash_location()
    os.makedirs(os.path.join(files, "d", "inner"))
    assert trash.trash_root(files) == files
    assert trash.trash_root(os.path.join(files, "d", "inner")) == files
    assert trash.trash_root(str(data)) is None           # Trash, not files/
    assert trash.trash_root(str(tmp_path)) is None


def test_the_disk_trashes_are_recognised_by_name(tmp_path, data):
    uid = os.getuid()
    own = tmp_path / "disk" / f".Trash-{uid}" / "files"
    shared = tmp_path / "disk" / ".Trash" / str(uid) / "files"
    for root in (own, shared):
        assert trash.trash_root(str(root / "sub")) == str(root)
    # A directory that happens to be called files is not a trash.
    assert trash.trash_root(str(tmp_path / "project" / "files")) is None
    assert trash.trash_root(str(tmp_path / ".Trash-0" / "x")) is None


def test_the_trash_root_on_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(trash.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    where = trash.trash_location()
    os.makedirs(os.path.join(where, "d"))
    assert trash.trash_root(where) == where
    assert trash.trash_root(os.path.join(where, "d")) == where
    assert trash.trash_root(str(tmp_path / "home")) is None


def test_only_an_item_at_the_top_has_a_trashinfo(tmp_path, data,
                                                 monkeypatch):
    files = data / "files"
    assert trash.info_file(str(files / "a.txt")) == str(
        data / "info" / "a.txt.trashinfo")
    assert trash.info_file(str(files / "d" / "inner")) is None
    assert trash.info_file(str(tmp_path / "files" / "a.txt")) is None
    monkeypatch.setattr(trash.sys, "platform", "darwin")
    assert trash.info_file(str(files / "a.txt")) is None


def test_forget_removes_the_record_and_minds_no_missing_one(tmp_path, data):
    write(str(tmp_path / "a.txt"), "x")
    where = move_to_trash(str(tmp_path / "a.txt"))
    os.remove(where)
    trash.forget(where)
    assert list((data / "info").iterdir()) == []
    trash.forget(where)                                  # already gone
    trash.forget(str(tmp_path / "elsewhere.txt"))        # never had one


# -- T: the pane goes to the trash ---------------------------------------------

def _names(panel):
    return [e.name for e in panel.entries]


def test_t_points_the_pane_at_the_trash_and_back_returns(app, tmp_path,
                                                        data):
    write(str(tmp_path / "left" / "gone.txt"), "x")
    move_to_trash(str(tmp_path / "left" / "gone.txt"))
    app.handle_key(ord("T"))
    assert app.left.path == str(data / "files")
    assert "gone.txt" in _names(app.left)
    assert app.message == f"Trash: {data / 'files'} -- '<' goes back"
    app.handle_key(ord("<"))
    assert app.left.path == str(tmp_path / "left")


def test_the_command_menu_and_the_right_click_menu_open_it(app, data,
                                                          monkeypatch):
    command = next(m for m in MENUS if m["name"] == "command")
    entry = next(i for i in command["items"] if i.get("name") == "open_trash")
    assert entry["label"] == "Trash fol~d~er" and entry["key"] == "T"
    app._dispatch("open_trash")
    assert app.left.path == str(data / "files")

    app.active = app.right
    _ScriptedDialogs(monkeypatch, menu=["Trash folder"])
    app._context_menu()
    assert app.right.path == str(data / "files")


def test_a_missing_home_trash_opens_empty(app, data):
    assert not data.exists()
    app._open_trash()
    assert _names(app.left) == [".."]
    assert _mode(data / "files") == 0o700 and _mode(data / "info") == 0o700


def test_a_pane_on_another_disk_opens_that_disks_trash(app, tmp_path, data,
                                                      monkeypatch):
    disk = _other_disk(monkeypatch, tmp_path, data)
    own = disk / f".Trash-{os.getuid()}" / "files"
    own.mkdir(parents=True)
    app.left.chdir(str(disk))
    app._open_trash()
    assert app.left.path == str(own)


def test_a_remote_pane_switches_to_this_computers_trash(app, tmp_path, data):
    remote = _FakeRemoteBackend()
    app.left.set_location(remote, str(tmp_path / "left"))
    app._open_trash()
    # The other pane's local backend, not a new one, and the home trash: a
    # directory on the server is never the trash.
    assert app.left.fs is app.right.fs
    assert app.left.path == str(data / "files")
    app._back()
    assert app.left.fs is remote and app.left.path == str(tmp_path / "left")


def test_with_no_local_backend_open_a_new_one_is_kept(app, tmp_path, data):
    for panel in (app.left, app.right):
        panel.set_location(_FakeRemoteBackend(), str(tmp_path / "left"))
    app._backends.clear()
    app._open_trash()
    assert app.left.fs.scheme == "local"
    assert app._backends == [app.left.fs]


def test_an_archive_pane_switches_to_the_trash_and_back(app, tmp_path,
                                                       data):
    write_zip(str(tmp_path / "left" / "bundle.zip"), {"in.txt": "x"})
    app.left.refresh()
    _point_at(app.left, "bundle.zip")
    app._activate_entry()
    assert app.left.fs.scheme == "zip"
    app._open_trash()
    assert app.left.fs.scheme == "local"
    assert app.left.path == str(data / "files")
    app._back()
    assert app.left.fs.scheme == "zip" and "in.txt" in _names(app.left)


def _refuse_listing(monkeypatch, panel, where, error):
    real = panel.fs.listdir

    def listdir(path):
        if path == where:
            raise error
        return real(path)

    monkeypatch.setattr(panel.fs, "listdir", listdir)


def test_a_trash_that_cannot_be_listed_leaves_the_pane_and_says_why(
        app, tmp_path, data, monkeypatch):
    where = str(data / "files")
    _refuse_listing(monkeypatch, app.left, where,
                    PermissionError(13, "Permission denied", where))
    scripted = _ScriptedDialogs(monkeypatch)
    app._open_trash()
    assert app.left.path == str(tmp_path / "left")
    title, text, error = scripted.messages[0]
    assert title == "Trash" and error is True
    assert text.startswith(f"Cannot list the trash:\n{where}\n")
    assert "Permission denied" in text and "Full Disk Access" not in text
    assert app.left.history == []                # nothing to go back from


def test_macos_says_how_to_allow_the_listing(app, tmp_path, monkeypatch):
    monkeypatch.setattr(trash.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    where = str(tmp_path / "home" / ".Trash")
    _refuse_listing(monkeypatch, app.left, where,
                    PermissionError(1, "Operation not permitted", where))
    scripted = _ScriptedDialogs(monkeypatch)
    app._open_trash()
    assert app.left.path == str(tmp_path / "left")
    assert "Operation not permitted" in scripted.last_message
    assert "Full Disk Access" in scripted.last_message


# -- F8 inside the trash --------------------------------------------------------

def _in_the_trash(app, tmp_path, *names):
    for name in names:
        write(str(tmp_path / "left" / name), name)
        move_to_trash(str(tmp_path / "left" / name))
    app._open_trash()


@pytest.mark.parametrize("use_trash", [True, False])
def test_f8_in_the_trash_deletes_for_good_with_its_record(
        trashing, tmp_path, data, monkeypatch, use_trash):
    trashing.use_trash = use_trash
    _in_the_trash(trashing, tmp_path, "a.txt", "b.txt")
    _point_at(trashing.left, "a.txt")
    scripted = _ScriptedDialogs(monkeypatch, menu=["Delete permanently"])
    trashing.handle_key(curses.KEY_F8)
    title, options = scripted.menus[0]
    assert title == "Delete from the trash"
    assert options == ["Delete permanently", "Cancel"]
    assert "Already in the trash: this cannot be undone." in \
        scripted.menu_texts[0]
    assert sorted(os.listdir(data / "files")) == ["b.txt"]
    assert sorted(os.listdir(data / "info")) == ["b.txt.trashinfo"]
    assert trashing.message == "Deleted 1 item(s) from the trash"


@pytest.mark.parametrize("answer", ["Cancel", None])
def test_f8_in_the_trash_can_be_cancelled(trashing, tmp_path, data,
                                          monkeypatch, answer):
    _in_the_trash(trashing, tmp_path, "a.txt")
    _point_at(trashing.left, "a.txt")
    _ScriptedDialogs(monkeypatch, menu=[answer])
    trashing._delete()
    assert (data / "files" / "a.txt").exists()
    assert (data / "info" / "a.txt.trashinfo").exists()


def test_f8_inside_a_trashed_directory_keeps_the_directorys_record(
        trashing, tmp_path, data, monkeypatch):
    write(str(tmp_path / "left" / "d" / "inner"), "x")
    move_to_trash(str(tmp_path / "left" / "d"))
    trashing._open_trash()
    trashing.left.chdir("d")
    _point_at(trashing.left, "inner")
    _ScriptedDialogs(monkeypatch, menu=["Delete permanently"])
    trashing._delete()
    assert os.listdir(data / "files" / "d") == []
    assert (data / "info" / "d.trashinfo").exists()


def test_a_record_that_will_not_go_is_reported(trashing, tmp_path, data,
                                               monkeypatch):
    _in_the_trash(trashing, tmp_path, "a.txt")
    _point_at(trashing.left, "a.txt")

    real_remove = os.remove

    def stuck(path):
        if path.endswith(".trashinfo"):
            raise PermissionError("read-only")
        real_remove(path)

    monkeypatch.setattr(trash.os, "remove", stuck)
    scripted = _ScriptedDialogs(monkeypatch, menu=["Delete permanently"])
    trashing._delete()
    assert not (data / "files" / "a.txt").exists()
    assert "a.txt: deleted, but not its .trashinfo: read-only" in \
        scripted.last_message
