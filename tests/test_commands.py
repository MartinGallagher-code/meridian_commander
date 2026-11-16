# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The saved background commands: ``commands.ini``, kept like the presets."""

from __future__ import annotations

import os
import stat

import pytest

from meridian_commander import commands
from meridian_commander.commands import SavedCommand

from support import read, write


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config" / "meridian-commander" / "commands.ini"


def test_the_file_is_in_the_config_directory(config):
    assert commands.commands_path() == str(config)


def test_a_round_trip(config):
    items = [SavedCommand("http", "python3 -m http.server 8000"),
             SavedCommand("docs", "make -C docs livehtml", "/srv/docs")]
    assert commands.save(items) == str(config)
    assert commands.load() == [items[1], items[0]]


def test_a_missing_file_is_an_empty_list(config):
    assert commands.load() == []
    assert commands.load(str(config.parent / "nowhere.ini")) == []


@pytest.mark.parametrize("content", ["[unclosed\ncommand = x\n",
                                     "command = before any section\n"])
def test_a_broken_file_is_an_empty_list(config, content):
    write(str(config), content)
    assert commands.load() == []


def test_an_unreadable_file_is_an_empty_list(config, monkeypatch):
    write(str(config), "[http]\ncommand = x\n")

    def refused(self, filenames, encoding=None):
        raise OSError("permission denied")

    monkeypatch.setattr(commands.configparser.ConfigParser, "read", refused)
    assert commands.load() == []


def test_alphabetical_on_the_way_in_and_out(config):
    write(str(config), "[zeta]\ncommand = z\n[Alpha]\ncommand = a\n"
                       "[beta]\ncommand = b\n[alpha]\ncommand = lower\n")
    names = [item.name for item in commands.load()]
    assert names == ["Alpha", "alpha", "beta", "zeta"]
    commands.save(commands.load())
    text = read(str(config))
    assert (text.index("[Alpha]") < text.index("[alpha]")
            < text.index("[beta]") < text.index("[zeta]"))


def test_a_hand_edited_file_is_re_sorted_on_the_next_write(config):
    write(str(config), "; mine\n[web]\ncommand = serve\n\n[api]\n"
                       "command = run-api\ndirectory = ~/api\n")
    items = commands.add(SavedCommand("logs", "tail -f x.log"),
                         commands.load())
    commands.save(items)
    text = read(str(config))
    assert text.index("[api]") < text.index("[logs]") < text.index("[web]")
    assert "directory = ~/api" in text


def test_a_percent_is_taken_literally(config):
    item = SavedCommand("pct", "printf '%s\\n' 100%done", "/srv/100%complete")
    commands.save([item])
    assert commands.load() == [item]


def test_missing_keys_read_as_blank(config):
    write(str(config), "[bare]\n")
    assert commands.load() == [SavedCommand("bare", "", "")]


@pytest.mark.parametrize("name, ok", [
    ("http", True), ("  web  ", True), ("", False), ("   ", False),
    ("a[b", False), ("a]b", False), ("DEFAULT", False), ("default", False),
    ("two\nlines", False),
])
def test_names_the_file_cannot_hold_are_refused(name, ok):
    assert commands.valid_name(name) is ok


def test_adding_a_name_already_there_replaces_it():
    items = [SavedCommand("b", "old"), SavedCommand("a", "x")]
    result = commands.add(SavedCommand("b", "new"), items)
    assert result == [SavedCommand("a", "x"), SavedCommand("b", "new")]


def test_editing_in_place_with_a_rename():
    items = [SavedCommand("alpha", "a"), SavedCommand("web", "serve")]
    result = commands.replace("web", SavedCommand("site", "serve --port 1",
                                                  "/srv"), items)
    assert result == [SavedCommand("alpha", "a"),
                      SavedCommand("site", "serve --port 1", "/srv")]


def test_editing_without_a_rename_keeps_the_place():
    items = [SavedCommand("a", "1"), SavedCommand("b", "2")]
    assert commands.replace("a", SavedCommand("a", "one"), items) == [
        SavedCommand("a", "one"), SavedCommand("b", "2")]


def test_a_rename_onto_another_name_replaces_that_one():
    items = [SavedCommand("a", "1"), SavedCommand("b", "2"),
             SavedCommand("c", "3")]
    assert commands.replace("a", SavedCommand("b", "renamed"), items) == [
        SavedCommand("b", "renamed"), SavedCommand("c", "3")]


def test_deleting():
    items = [SavedCommand("a", "1"), SavedCommand("b", "2")]
    assert commands.remove("a", items) == [SavedCommand("b", "2")]
    assert commands.remove("missing", items) == items


def test_a_blank_directory_means_the_panes():
    """Shown without a directory in the menu: it runs wherever the active
    pane is when it is started."""
    blank = SavedCommand("http", "python3 -m http.server 8000")
    assert blank.directory == ""
    assert blank.label() == "python3 -m http.server 8000"
    assert SavedCommand("d", "make", "/srv").label() == "make  (in /srv)"


def test_the_file_starts_with_an_example_and_is_private(config):
    commands.save([])
    text = read(str(config))
    assert "python3 -m http.server 8000" in text
    assert text.startswith("; Meridian Commander saved commands")
    assert commands.load() == []                 # the example is a comment
    assert stat.S_IMODE(os.stat(config).st_mode) == 0o600


def test_saving_to_a_bare_file_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert commands.save([SavedCommand("a", "b")], "here.ini") == "here.ini"
    assert commands.load("here.ini") == [SavedCommand("a", "b")]
