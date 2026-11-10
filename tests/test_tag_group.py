# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""+, \\ and *: tag and untag by pattern, and invert the tags (MC-013)."""

from __future__ import annotations

import pytest

from meridian_commander.app import MENUS, App
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.panel import Panel

from support import _ScriptedDialogs, _StubScreen, write, write_zip


@pytest.fixture
def results(tmp_path):
    for name in ("run_1.tsv", "run_2.tsv", "RUN_9.TSV", "notes.md"):
        write(str(tmp_path / "d" / name), name)
    (tmp_path / "d" / "old.tsv").mkdir()        # a directory with a file's name
    return tmp_path / "d"


@pytest.fixture
def app(results, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "other").mkdir()
    return App(_StubScreen(), str(results), str(tmp_path / "other"))


# -- the panel -----------------------------------------------------------------

def test_a_glob_tags_the_files_that_match(results):
    panel = Panel(LocalFileSystem(), str(results))
    assert panel.select_matching("*.tsv") == 3
    assert panel.selected == {"run_1.tsv", "run_2.tsv", "RUN_9.TSV"}


def test_files_only_off_takes_a_matching_directory_too(results):
    panel = Panel(LocalFileSystem(), str(results))
    assert panel.select_matching("*.tsv", files_only=False) == 4
    assert "old.tsv" in panel.selected


def test_a_capital_means_it(results):
    panel = Panel(LocalFileSystem(), str(results))
    assert panel.select_matching("RUN*") == 1
    assert panel.selected == {"RUN_9.TSV"}


def test_untag_by_pattern_leaves_the_rest(results):
    panel = Panel(LocalFileSystem(), str(results))
    panel.select_matching("*")
    assert panel.select_matching("run_?.tsv", tag=False) == 3
    assert panel.selected == {"notes.md"}


def test_the_way_up_is_never_tagged(results):
    panel = Panel(LocalFileSystem(), str(results))
    panel.select_matching("*", files_only=False)
    panel.invert_selection()
    panel.invert_selection()
    assert ".." not in panel.selected
    assert len(panel.selected) == 5


@pytest.mark.parametrize("before, after", [
    (set(), {"run_1.tsv", "run_2.tsv", "RUN_9.TSV", "notes.md", "old.tsv"}),
    ({"notes.md", "old.tsv"}, {"run_1.tsv", "run_2.tsv", "RUN_9.TSV"}),
    ({"run_1.tsv", "run_2.tsv", "RUN_9.TSV", "notes.md", "old.tsv"}, set()),
])
def test_invert(results, before, after):
    panel = Panel(LocalFileSystem(), str(results))
    panel.selected = set(before)
    panel.invert_selection()
    assert panel.selected == after


def test_a_filter_keeps_its_hidden_names_out_of_it(results):
    """Only what is shown is tagged, untagged or inverted."""
    panel = Panel(LocalFileSystem(), str(results))
    panel.selected = {"notes.md"}
    panel.set_filter("run")
    panel.select_matching("*")
    assert panel.selected == {"notes.md", "run_1.tsv", "run_2.tsv", "RUN_9.TSV"}
    panel.invert_selection()
    assert panel.selected == {"notes.md"}


# -- the application -----------------------------------------------------------

def test_plus_asks_with_star_and_tags_every_file(app, monkeypatch):
    seen = {}

    def prompt(stdscr, title, label, default="", is_password=False,
               toggles=None):
        seen.update(title=title, default=default,
                    toggles=[(t.label, t.on) for t in toggles])
        return default

    monkeypatch.setattr("meridian_commander.dialogs.prompt", prompt)
    app.handle_key(ord("+"))
    assert seen == {"title": "Tag group", "default": "*",
                    "toggles": [("Files only", True)]}
    assert app.left.selected == {"run_1.tsv", "run_2.tsv", "RUN_9.TSV",
                                 "notes.md"}
    assert app.message == "Tagged 4: 4 tagged"


def test_files_only_switched_off_at_the_prompt(app, monkeypatch):
    _ScriptedDialogs(monkeypatch, prompt=["*.tsv"],
                     toggles=[{"Files only": False}])
    app.handle_key(ord("+"))
    assert "old.tsv" in app.left.selected


def test_backslash_untags_by_pattern(app, monkeypatch):
    app.left.selected = {"run_1.tsv", "notes.md"}
    scripted = _ScriptedDialogs(monkeypatch, prompt=["*.md"])
    app.handle_key(ord("\\"))
    assert app.left.selected == {"run_1.tsv"}
    assert "Untag the names matching" in scripted.prompts[0]
    assert app.message == "Untagged 1: 1 tagged"


def test_minus_untags_everything_without_asking(app, monkeypatch):
    app.left.selected = {"run_1.tsv"}
    scripted = _ScriptedDialogs(monkeypatch)
    app.handle_key(ord("-"))
    assert app.left.selected == set()
    assert scripted.prompts == []


def test_star_inverts(app):
    app.left.selected = {"notes.md"}
    app.handle_key(ord("*"))
    assert app.left.selected == {"run_1.tsv", "run_2.tsv", "RUN_9.TSV",
                                 "old.tsv"}
    assert app.message == "Tags inverted: 4 tagged"


@pytest.mark.parametrize("answer", [None, ""])
def test_escape_or_nothing_typed_changes_nothing(app, monkeypatch, answer):
    app.left.selected = {"notes.md"}
    _ScriptedDialogs(monkeypatch, prompt=[answer])
    app.handle_key(ord("+"))
    assert app.left.selected == {"notes.md"}


def test_no_match_says_so_and_changes_nothing(app, monkeypatch):
    app.left.selected = {"notes.md"}
    _ScriptedDialogs(monkeypatch, prompt=["*.xyz"])
    app.handle_key(ord("+"))
    assert app.left.selected == {"notes.md"}
    assert app.message == "Nothing matches *.xyz"


@pytest.mark.parametrize("action", ["_tag_group", "_untag_group",
                                    "_invert_tags"])
def test_a_plug_in_pane_has_no_listing_to_tag(app, monkeypatch, action):
    app.left.plugin = object()
    _ScriptedDialogs(monkeypatch)
    getattr(app, action)()
    assert app.message == "Tagging works on a directory listing"


def test_inside_an_archive_too(app, results, monkeypatch):
    write_zip(str(results / "z.zip"), {"a.tsv": "1", "b.txt": "2"})
    app.left.refresh()
    app.left.move_to([e.name for e in app.left.entries].index("z.zip"))
    app._activate_entry()
    _ScriptedDialogs(monkeypatch, prompt=["*.tsv"])
    app.handle_key(ord("+"))
    assert app.left.selected == {"a.tsv"}


def test_the_file_menu_offers_all_three(app, monkeypatch):
    file_menu = next(m for m in MENUS if m["name"] == "file")
    keys = {i.get("name"): i.get("key") for i in file_menu["items"]}
    assert (keys["tag_group"], keys["untag_group"], keys["invert"]) == \
        ("+", "\\", "*")
    for name in ("tag_group", "untag_group", "invert"):
        assert name in app._actions()
    _ScriptedDialogs(monkeypatch, prompt=["*.md"])
    app._dispatch("tag_group")
    assert app.left.selected == {"notes.md"}
