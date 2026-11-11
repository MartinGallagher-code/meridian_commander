# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""F: show only the names in a pane that match a pattern (MC-012)."""

from __future__ import annotations

import pytest

from meridian_commander import dialogs
from meridian_commander.app import App
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.panel import Panel, name_matches

from support import _ScriptedDialogs, with_curses_screen, write


@pytest.fixture
def results(tmp_path):
    """A results directory: a few runs among other files."""
    for name in ("run_1.tsv", "run_2.tsv", "run_10.tsv", "RUN_99.TSV",
                 "notes.md", "plot.png"):
        write(str(tmp_path / "d" / name), name)
    (tmp_path / "d" / "runs").mkdir()
    return tmp_path / "d"


def _names(panel):
    return [e.name for e in panel.entries]


class _Counting(LocalFileSystem):
    """A backend that counts its listings, as a remote one pays for them."""

    def __init__(self):
        super().__init__()
        self.listings = 0

    def listdir(self, path):
        self.listings += 1
        return super().listdir(path)


# -- matching ------------------------------------------------------------------

@pytest.mark.parametrize("pattern, name, expected", [
    ("run", "run_1.tsv", True),           # plain text, anywhere
    ("un_1", "run_1.tsv", True),
    ("run", "RUN_99.TSV", True),          # lowercase ignores case
    ("RUN", "run_1.tsv", False),          # a capital means it
    ("RUN", "RUN_99.TSV", True),
    ("*.tsv", "run_1.tsv", True),         # a glob is the whole name
    ("*.tsv", "RUN_99.TSV", True),
    ("*.TSV", "run_1.tsv", False),
    ("run_?.tsv", "run_1.tsv", True),
    ("run_?.tsv", "run_10.tsv", False),
    ("run_[12].tsv", "run_2.tsv", True),
    ("run_[12].tsv", "run_3.tsv", False),
    ("tsv*", "run_1.tsv", False),
    ("xyz", "run_1.tsv", False),
])
def test_name_matches(pattern, name, expected):
    assert name_matches(pattern, name) is expected


# -- the panel -----------------------------------------------------------------

def test_a_filter_shows_only_matches_and_the_way_up(results):
    panel = Panel(LocalFileSystem(), str(results))
    panel.move_to(_names(panel).index("plot.png"))
    panel.set_filter("*.tsv")
    assert _names(panel) == ["..", "run_1.tsv", "run_10.tsv", "run_2.tsv",
                             "RUN_99.TSV"]
    assert panel.hidden_count == 3
    # The cursor lands on the first match.
    assert panel.current_name() == "run_1.tsv"


def test_directories_are_filtered_too(results):
    panel = Panel(LocalFileSystem(), str(results))
    panel.set_filter("runs")
    assert _names(panel) == ["..", "runs"]


def test_no_match_leaves_only_the_way_up(results):
    panel = Panel(LocalFileSystem(), str(results))
    panel.set_filter("nothing-like-it")
    assert _names(panel) == [".."]
    assert panel.current_name() == ".."


def test_no_match_at_the_root_leaves_nothing(tmp_path):
    panel = Panel(LocalFileSystem(), "/")
    panel.set_filter("nothing-is-called-this-at-the-root")
    assert panel.entries == []
    assert panel.current() is None


def test_an_empty_pattern_shows_everything_and_keeps_the_cursor(results):
    panel = Panel(LocalFileSystem(), str(results))
    panel.set_filter("run_2")
    assert panel.current_name() == "run_2.tsv"
    panel.set_filter("")
    assert panel.filter == ""
    assert panel.hidden_count == 0
    assert panel.current_name() == "run_2.tsv"
    assert len(panel.entries) == len(panel.listed) == 8


def test_filtering_asks_the_backend_nothing(results):
    """The listing is in memory: a remote pane filters with no round trip."""
    fs = _Counting()
    panel = Panel(fs, str(results))
    before = fs.listings
    panel.set_filter("run")
    panel.set_filter("")
    assert fs.listings == before


def test_reload_sort_and_dotfiles_keep_the_filter(results):
    write(str(results / ".hidden.tsv"), "x")
    panel = Panel(LocalFileSystem(), str(results))
    panel.set_filter("*.tsv")
    panel.refresh()
    panel.set_sort("size")
    panel.toggle_hidden()
    assert panel.filter == "*.tsv"
    assert all(name == ".." or name.lower().endswith(".tsv")
               for name in _names(panel))


@pytest.mark.parametrize("leave", ["chdir", "go_parent", "set_location"])
def test_leaving_the_directory_clears_the_filter(results, leave):
    panel = Panel(LocalFileSystem(), str(results))
    panel.set_filter("run")
    if leave == "chdir":
        panel.chdir(str(results / "runs"))
    elif leave == "go_parent":
        panel.go_parent()
    else:
        panel.set_location(LocalFileSystem(), str(results / "runs"))
    assert panel.filter == ""
    assert panel.hidden_count == 0


def test_a_hidden_tag_is_kept_counted_and_copied(results):
    panel = Panel(LocalFileSystem(), str(results))
    panel.selected = {"notes.md", "run_1.tsv"}
    panel.set_filter("*.tsv")
    panel.refresh()
    assert panel.selected == {"notes.md", "run_1.tsv"}
    assert panel.hidden_tagged == 1
    assert sorted(e.name for e in panel.selected_entries()) == \
        ["notes.md", "run_1.tsv"]


def test_a_tag_on_something_that_has_gone_is_still_dropped(results):
    panel = Panel(LocalFileSystem(), str(results))
    panel.selected = {"notes.md"}
    panel.set_filter("*.tsv")
    (results / "notes.md").unlink()
    panel.refresh()
    assert panel.selected == set()


# -- the application -----------------------------------------------------------

@pytest.fixture
def app(results, tmp_path, monkeypatch):
    from support import _StubScreen

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "other").mkdir()
    app = App(_StubScreen(), str(results), str(tmp_path / "other"))
    app.use_trash = False
    return app


def test_f_asks_for_a_pattern_and_filters(app, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch, prompt=["*.tsv"])
    app.handle_key(ord("F"))
    assert "Show only names matching" in scripted.prompts[0]
    assert app.left.filter == "*.tsv"
    assert app.message == "Filter *.tsv: 4 shown, 3 hidden"


def test_the_prompt_starts_from_the_filter_and_empty_turns_it_off(app,
                                                                monkeypatch):
    app.left.set_filter("run")
    seen = {}

    def prompt(stdscr, title, label, default="", is_password=False,
               toggles=None):
        seen["default"] = default
        return ""

    monkeypatch.setattr(dialogs, "prompt", prompt)
    app._filter()
    assert seen["default"] == "run"
    assert app.left.filter == ""
    assert app.message == "Filter off"


def test_escape_leaves_the_filter_as_it_was(app, monkeypatch):
    app.left.set_filter("run")
    _ScriptedDialogs(monkeypatch, prompt=[None])
    app._filter()
    assert app.left.filter == "run"


def test_a_plug_in_pane_has_nothing_to_filter(app, monkeypatch):
    app.left.plugin = object()
    _ScriptedDialogs(monkeypatch)
    app._filter()
    assert app.message == "Filter works on a directory listing"


def test_the_command_menu_offers_it(app, monkeypatch):
    from meridian_commander.app import MENUS

    command = next(m for m in MENUS if m["name"] == "command")
    entry = next(i for i in command["items"] if i.get("name") == "filter")
    assert entry["key"] == "F"
    assert "filter" in app._actions()
    _ScriptedDialogs(monkeypatch, prompt=["md"])
    app._dispatch("filter")
    assert app.left.filter == "md"


def test_a_copy_and_a_delete_say_what_is_not_shown(app, monkeypatch, tmp_path):
    app.left.selected = {"notes.md", "plot.png", "run_1.tsv"}
    app.left.set_filter("*.tsv")
    scripted = _ScriptedDialogs(monkeypatch, prompt=[None], confirm=[False])
    app._copy()
    app._delete()
    assert "Copy 3 items (2 not shown) to" in scripted.prompts[0]
    assert scripted.messages[0][1].startswith("Delete 3 selected items (2 not shown)?")


def test_a_single_hidden_item_is_named_with_its_count(app, monkeypatch):
    app.left.selected = {"notes.md"}
    app.left.set_filter("*.tsv")
    scripted = _ScriptedDialogs(monkeypatch, prompt=[None], confirm=[False])
    app._copy()
    app._delete()
    assert "Copy 'notes.md' (1 not shown) to" in scripted.prompts[0]
    assert scripted.messages[0][1].startswith("Delete 'notes.md' (1 not shown)?")


def test_the_footer_totals_hidden_tags_too(app):
    app.left.selected = {"notes.md", "run_1.tsv"}
    app.left.set_filter("*.tsv")
    footer = app._panel_footer(app.left)
    assert footer.startswith("2 tagged, ")
    assert footer == f"2 tagged, {len('notes.md') + len('run_1.tsv')}"


def test_the_caption_names_the_filter(results, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "other").mkdir()

    def run(stdscr):
        app = App(stdscr, str(results), str(tmp_path / "other"))
        app.left.selected = {"notes.md"}
        app.left.set_filter("*.tsv")
        app.draw()
        return "\n".join(stdscr.instr(row, 0).decode(errors="replace")
                         for row in range(24))

    text = with_curses_screen(24, 100, run)
    assert "[*.tsv: 3 hidden, 1 tagged]" in text


def test_the_caption_note_without_hidden_tags(app):
    app.left.set_filter("*.tsv")
    assert app._filter_note(app.left) == " [*.tsv: 3 hidden]"
    app.left.set_filter("")
    assert app._filter_note(app.left) == ""


def test_sync_counts_what_a_filter_hides(app, results, monkeypatch):
    """Sync walks the whole directory, so its size warning must count it."""
    for i in range(250):
        write(str(results / f"bulk_{i:03}.dat"), "x")
    app.left.refresh()
    app.left.set_filter("run_1")
    assert app._sync_scope_warning() is not None


def test_f_typed_in_the_search_is_text(app):
    app.handle_key(ord("/"))
    app.handle_key(ord("F"))
    assert app.left.filter == ""
    assert app.search is not None and app.search.pattern == "F"
