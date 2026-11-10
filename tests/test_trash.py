# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""F8 to the trash on local panes (MC-018)."""

from __future__ import annotations

import os
import types

import pytest

from meridian_commander import app as app_mod
from meridian_commander import config, trash
from meridian_commander.app import MENUS
from meridian_commander.trash import TrashUnavailable, move_to_trash

from support import _FakeRemoteBackend, _ScriptedDialogs, read, write


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
        if str(path) == home:
            return types.SimpleNamespace(st_dev=-1)
        return real_stat(path, *args, **kwargs)

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
