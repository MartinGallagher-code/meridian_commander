# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""l: make symbolic links in the other pane, or change a link's target (MC-017)."""

from __future__ import annotations

import os

import pytest

from meridian_commander.app import MENUS, App

from support import _FakeRemoteBackend, _ScriptedDialogs, _StubScreen, write, \
    write_zip


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    write(str(tmp_path / "left" / "release-42" / "app"), "x")
    write(str(tmp_path / "left" / "notes.txt"), "n")
    (tmp_path / "right").mkdir()
    return App(_StubScreen(), str(tmp_path / "left"), str(tmp_path / "right"))


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def test_a_relative_link_that_resolves(app, tmp_path, monkeypatch):
    _point_at(app.left, "release-42")
    scripted = _ScriptedDialogs(monkeypatch,
                                prompt=[str(tmp_path / "right" / "current")])
    app.handle_key(ord("l"))
    link = tmp_path / "right" / "current"
    assert os.readlink(link) == "../left/release-42"
    assert os.path.realpath(link) == str(tmp_path / "left" / "release-42")
    assert scripted.prompt_toggles[0] == [("Relative", True)]
    assert app.message == "Made 1 link(s)"


def test_an_absolute_link(app, tmp_path, monkeypatch):
    _point_at(app.left, "notes.txt")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right" / "n")],
                     toggles=[{"Relative": False}])
    app._link()
    assert os.readlink(tmp_path / "right" / "n") == str(tmp_path / "left" / "notes.txt")


def test_tagged_entries_each_get_one(app, tmp_path, monkeypatch):
    app.left.selected = {"release-42", "notes.txt"}
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app._link()
    assert sorted(os.listdir(tmp_path / "right")) == ["notes.txt", "release-42"]
    assert os.path.islink(tmp_path / "right" / "notes.txt")


def test_an_existing_name_is_asked_about(app, tmp_path, monkeypatch):
    write(str(tmp_path / "right" / "notes.txt"), "mine")
    _point_at(app.left, "notes.txt")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right" / "notes.txt")],
                     confirm=[False])
    app._link()
    assert not os.path.islink(tmp_path / "right" / "notes.txt")
    assert app.message == "Made 0 link(s)"
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right" / "notes.txt")],
                     confirm=[True])
    app._link()
    assert os.path.islink(tmp_path / "right" / "notes.txt")


def test_a_directory_in_the_way_is_refused(app, tmp_path, monkeypatch):
    (tmp_path / "right" / "notes.txt").mkdir()
    _point_at(app.left, "notes.txt")
    scripted = _ScriptedDialogs(
        monkeypatch, prompt=[str(tmp_path / "right" / "notes.txt")])
    app._link()
    assert "a directory of that name is in the way" in scripted.last_message


def test_escape_makes_nothing(app, tmp_path, monkeypatch):
    _point_at(app.left, "notes.txt")
    _ScriptedDialogs(monkeypatch, prompt=[None])
    app._link()
    assert os.listdir(tmp_path / "right") == []


def test_changing_a_links_target(app, tmp_path, monkeypatch):
    write(str(tmp_path / "left" / "release-43" / "app"), "y")
    os.symlink("release-42", tmp_path / "left" / "current")
    app.left.refresh()
    _point_at(app.left, "current")
    seen = {}

    def prompt(stdscr, title, label, default="", is_password=False, toggles=None):
        seen["default"] = default
        return "release-43"

    monkeypatch.setattr("meridian_commander.dialogs.prompt", prompt)
    app._link()
    assert seen["default"] == "release-42"
    assert os.readlink(tmp_path / "left" / "current") == "release-43"
    assert app.message == "current -> release-43"


@pytest.mark.parametrize("answer", [None, "release-42"])
def test_an_unchanged_target_is_left(app, tmp_path, monkeypatch, answer):
    os.symlink("release-42", tmp_path / "left" / "current")
    app.left.refresh()
    _point_at(app.left, "current")
    _ScriptedDialogs(monkeypatch, prompt=[answer])
    app._link()
    assert os.readlink(tmp_path / "left" / "current") == "release-42"


def test_a_link_that_cannot_be_replaced_says_why(app, tmp_path, monkeypatch):
    os.symlink("release-42", tmp_path / "left" / "current")
    app.left.refresh()
    _point_at(app.left, "current")

    def refuse(path):
        raise PermissionError("read-only")

    monkeypatch.setattr(app.left.fs, "remove", refuse)
    scripted = _ScriptedDialogs(monkeypatch, prompt=["elsewhere"])
    app._link()
    assert scripted.last_message == "read-only"


def test_a_link_that_cannot_be_read_says_why(app, tmp_path, monkeypatch):
    os.symlink("release-42", tmp_path / "left" / "current")
    app.left.refresh()
    _point_at(app.left, "current")

    def broken(path):
        raise OSError("gone")

    monkeypatch.setattr(app.left.fs, "readlink", broken)
    scripted = _ScriptedDialogs(monkeypatch)
    app._link()
    assert scripted.last_message == "gone"


def test_panes_on_different_machines_are_refused(app, tmp_path, monkeypatch):
    app.right.set_location(_FakeRemoteBackend(), str(tmp_path / "right"))
    _point_at(app.left, "notes.txt")
    scripted = _ScriptedDialogs(monkeypatch)
    app._link()
    assert "different machines" in scripted.last_message


def test_an_archive_cannot_hold_one(app, tmp_path, monkeypatch):
    write_zip(str(tmp_path / "left" / "z.zip"), {"a": "1"})
    app.left.refresh()
    _point_at(app.left, "z.zip")
    app._activate_entry()
    app.left.move_to(0)
    app.left.selected = {"a"}
    app.right.set_location(app.left.fs, "/")
    scripted = _ScriptedDialogs(monkeypatch)
    app._link()
    assert "links are not supported here" in scripted.last_message


def test_editing_a_link_inside_an_archive_is_refused(app, tmp_path, monkeypatch):
    write_zip(str(tmp_path / "left" / "z.zip"), {"a": "1"})
    app.left.refresh()
    _point_at(app.left, "z.zip")
    app._activate_entry()
    scripted = _ScriptedDialogs(monkeypatch)
    app._edit_link(app.left, "a")
    assert "links are not supported here" in scripted.last_message


def test_nothing_to_link_and_a_plug_in_pane(app, monkeypatch):
    app.left.move_to(0)
    _ScriptedDialogs(monkeypatch)
    app._link()
    app.left.plugin = object()
    app._link()
    assert app.message == "Links work on a directory listing"


def test_the_file_menu_offers_it(app):
    file_menu = next(m for m in MENUS if m["name"] == "file")
    assert any(i.get("name") == "link" and i.get("key") == "l"
               for i in file_menu["items"])
    assert "link" in app._actions()
