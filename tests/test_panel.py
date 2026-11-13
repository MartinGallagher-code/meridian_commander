# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Panel navigation, sorting, tagging and scrolling."""

from __future__ import annotations

import pytest

from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.panel import Panel, Search

from support import write


@pytest.fixture
def tree(tmp_path):
    """A directory with a predictable mix of names, sizes, times and types."""
    write(str(tmp_path / "root" / "b.txt"), "22", mtime=2000)
    write(str(tmp_path / "root" / "a.log"), "4444", mtime=3000)
    write(str(tmp_path / "root" / "c.md"), "1", mtime=1000)
    (tmp_path / "root" / "zdir").mkdir()
    (tmp_path / "root" / "adir").mkdir()
    return tmp_path / "root"


def names(panel):
    return [e.name for e in panel.entries]


def test_sort_by_name_puts_directories_first(fs, tree):
    panel = Panel(fs, str(tree))
    assert names(panel) == ["..", "adir", "zdir", "a.log", "b.txt", "c.md"]


def test_sort_by_size(fs, tree):
    panel = Panel(fs, str(tree))
    panel.set_sort("size")
    assert names(panel)[3:] == ["c.md", "b.txt", "a.log"]


def test_sort_by_mtime(fs, tree):
    panel = Panel(fs, str(tree))
    panel.set_sort("mtime")
    assert names(panel)[3:] == ["c.md", "b.txt", "a.log"]


def test_sort_by_extension(fs, tree):
    panel = Panel(fs, str(tree))
    panel.set_sort("ext")
    assert names(panel)[3:] == ["a.log", "c.md", "b.txt"]


def test_sort_by_extension_treats_a_dotfile_as_having_none(fs, tmp_path):
    write(str(tmp_path / "d" / ".hidden"), "x")
    write(str(tmp_path / "d" / "z.txt"), "x")
    panel = Panel(fs, str(tmp_path / "d"))
    panel.set_sort("ext")
    # A leading dot is part of the name, not an extension, so it sorts first.
    assert names(panel)[1:] == [".hidden", "z.txt"]


def test_choosing_the_same_sort_key_again_reverses_it(fs, tree):
    panel = Panel(fs, str(tree))
    panel.set_sort("name")
    assert panel.sort_reverse is True
    assert names(panel)[1:3] == ["zdir", "adir"]
    panel.set_sort("name")
    assert panel.sort_reverse is False
    panel.set_sort("size")
    # Switching key starts forwards again rather than keeping the reversal.
    assert panel.sort_reverse is False


def test_at_the_filesystem_root_there_is_no_parent_entry(fs):
    panel = Panel(fs, "/")
    assert ".." not in names(panel)


def test_current_helpers_at_the_parent_entry(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(0)
    assert panel.current().name == ".."
    assert panel.current_name() == ".."
    # ".." is not a real path to act on.
    assert panel.current_path() is None


def test_current_helpers_on_an_empty_listing(fs, tmp_path):
    (tmp_path / "empty").mkdir()
    panel = Panel(fs, "/")
    panel.entries = []
    assert panel.current() is None
    assert panel.current_name() is None
    assert panel.current_path() is None
    assert panel.selected_entries() == []
    panel.toggle_select()               # nothing to tag; must not raise
    assert panel.selected == set()


def test_current_path_of_a_real_entry(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(names(panel).index("b.txt"))
    assert panel.current_path() == str(tree / "b.txt")


def test_selected_entries_prefers_tags_over_the_cursor(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(names(panel).index("b.txt"))
    assert [e.name for e in panel.selected_entries()] == ["b.txt"]

    panel.selected = {"a.log", "c.md", ".."}
    tagged = [e.name for e in panel.selected_entries()]
    # ".." can never be operated on, even if it somehow got tagged.
    assert tagged == ["a.log", "c.md"]


def test_selected_entries_ignores_the_cursor_on_the_parent(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(0)
    assert panel.selected_entries() == []


def test_toggle_select_adds_then_removes(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(names(panel).index("b.txt"))
    panel.toggle_select()
    assert panel.selected == {"b.txt"}
    panel.toggle_select()
    assert panel.selected == set()


def test_toggle_select_refuses_the_parent_entry(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(0)
    panel.toggle_select()
    assert panel.selected == set()


def test_select_all_and_clear(fs, tree):
    panel = Panel(fs, str(tree))
    panel.select_all()
    assert ".." not in panel.selected
    assert panel.selected == {"adir", "zdir", "a.log", "b.txt", "c.md"}
    panel.clear_selection()
    assert panel.selected == set()


def test_enter_descends_into_a_directory(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(names(panel).index("adir"))
    assert panel.enter() is True
    assert panel.path == str(tree / "adir")


def test_enter_on_a_file_does_nothing(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(names(panel).index("b.txt"))
    assert panel.enter() is False
    assert panel.path == str(tree)


def test_enter_on_the_parent_entry_goes_up(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(0)
    assert panel.enter() is True
    assert panel.path == str(tree.parent)


def test_enter_with_nothing_under_the_cursor(fs, tree):
    panel = Panel(fs, str(tree))
    panel.entries = []
    assert panel.enter() is False


def test_go_parent_keeps_the_cursor_on_the_directory_just_left(fs, tree):
    panel = Panel(fs, str(tree / "zdir"))
    assert panel.go_parent() is True
    assert panel.path == str(tree)
    assert panel.current_name() == "zdir"


def test_go_parent_at_the_root_does_nothing(fs):
    panel = Panel(fs, "/")
    assert panel.go_parent() is False
    assert panel.path == "/"


def test_go_parent_drops_any_tags(fs, tree):
    panel = Panel(fs, str(tree / "zdir"))
    panel.selected = {"whatever"}
    panel.go_parent()
    assert panel.selected == set()


def test_move_and_move_to_stay_inside_the_listing(fs, tree):
    panel = Panel(fs, str(tree))
    last = len(panel.entries) - 1
    panel.move(1000)
    assert panel.cursor == last
    panel.move(-1000)
    assert panel.cursor == 0
    panel.move_to(3)
    assert panel.cursor == 3
    panel.move_to(-5)
    assert panel.cursor == 0
    panel.move_to(1000)
    assert panel.cursor == last


def test_the_cursor_resets_on_an_empty_listing(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(3)
    panel.top = 2
    panel.entries = []
    panel.move(0)
    assert panel.cursor == 0
    assert panel.top == 0


def test_ensure_visible_scrolls_the_window_to_the_cursor(fs, tmp_path):
    for i in range(20):
        write(str(tmp_path / "many" / f"f{i:02d}.txt"), "x")
    panel = Panel(fs, str(tmp_path / "many"))

    panel.move_to(15)
    panel.ensure_visible(5)
    # The cursor sits on the last visible row.
    assert panel.top == 11

    panel.move_to(2)
    panel.ensure_visible(5)
    # Scrolling back up puts the cursor on the first visible row.
    assert panel.top == 2

    # The window never scrolls past the end of the listing.
    panel.move_to(len(panel.entries) - 1)
    panel.ensure_visible(5)
    assert panel.top == len(panel.entries) - 5


def test_ensure_visible_ignores_a_zero_height_window(fs, tree):
    panel = Panel(fs, str(tree))
    panel.top = 3
    panel.ensure_visible(0)
    assert panel.top == 3


def test_toggle_hidden_keeps_the_cursor_on_the_same_entry(fs, tmp_path):
    write(str(tmp_path / "d" / ".dotfile"), "x")
    write(str(tmp_path / "d" / "visible.txt"), "x")
    panel = Panel(fs, str(tmp_path / "d"))
    assert ".dotfile" in names(panel)          # dotfiles are shown by default
    panel.move_to(names(panel).index("visible.txt"))

    panel.toggle_hidden()
    assert panel.show_hidden is False
    assert ".dotfile" not in names(panel)
    assert panel.current_name() == "visible.txt"

    panel.toggle_hidden()
    assert panel.show_hidden is True
    assert ".dotfile" in names(panel)
    assert panel.current_name() == "visible.txt"


def test_a_vanished_entry_leaves_the_cursor_where_it_was(fs, tree):
    """Losing the entry under the bar must not throw you back to the top."""
    panel = Panel(fs, str(tree))
    panel.move_to(4)
    panel.refresh(keep_name="no-such-entry")
    assert panel.cursor == 4


def test_the_held_position_is_clamped_to_a_shorter_listing(fs, tree):
    """Hold the position, but not past the end of what is left."""
    panel = Panel(fs, str(tree))
    panel.move_to(len(panel.entries) - 1)
    for name in ("b.txt", "c.md"):
        (tree / name).unlink()
    panel.refresh(keep_name="no-such-entry")
    assert panel.cursor == len(panel.entries) - 1
    assert panel.current_name() is not None


def test_going_up_lands_on_the_directory_just_left(fs, tmp_path):
    """The happy path still wins: the name is found, wherever the cursor was."""
    write(str(tmp_path / "root" / "zdir" / "deep.txt"), "x")
    (tmp_path / "root" / "adir").mkdir(parents=True, exist_ok=True)
    panel = Panel(fs, str(tmp_path / "root" / "zdir"))
    panel.move_to(len(panel.entries) - 1)
    assert panel.go_parent() is True
    assert panel.current_name() == "zdir"


# -- picking where the cursor goes when entries are removed --------------------

def test_the_next_entry_down_is_chosen(fs, tree):
    panel = Panel(fs, str(tree))
    # [".."   "adir"  "zdir"  "a.log"  "b.txt"  "c.md"]
    panel.move_to(names(panel).index("a.log"))
    assert panel.name_after_removing({"a.log"}) == "b.txt"


def test_a_block_of_tagged_entries_is_stepped_over(fs, tree):
    """Tagged items sit above and below the bar; the cursor clears them all."""
    panel = Panel(fs, str(tree))
    panel.move_to(names(panel).index("zdir"))
    assert panel.name_after_removing({"zdir", "a.log", "b.txt"}) == "c.md"


def test_the_search_turns_back_at_the_end_of_the_listing(fs, tree):
    """Delete the tail and there is nothing below, so look up instead."""
    panel = Panel(fs, str(tree))
    panel.move_to(names(panel).index("b.txt"))
    assert panel.name_after_removing({"b.txt", "c.md"}) == "a.log"


def test_removing_everything_leaves_no_name_to_hold(fs, tree):
    panel = Panel(fs, str(tree))
    doomed = {e.name for e in panel.entries}
    assert panel.name_after_removing(doomed) is None


def test_set_location_onto_a_second_backend(fs, tmp_path):
    write(str(tmp_path / "here" / "a.txt"), "a")
    write(str(tmp_path / "there" / "b.txt"), "b")
    panel = Panel(fs, str(tmp_path / "here"))
    panel.top = 4
    other = LocalFileSystem()
    assert panel.set_location(other, str(tmp_path / "there")) is True
    assert panel.fs is other
    assert panel.top == 0


# -- history ------------------------------------------------------------------

def test_back_returns_to_the_directory_left_with_the_cursor_on_it(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(names(panel).index("zdir"))
    assert panel.enter()
    assert panel.back() is True
    assert panel.path == str(tree)
    assert panel.current_name() == "zdir"
    # And forward goes in again, to where the cursor was in there.
    assert panel.forward() is True
    assert panel.path == str(tree / "zdir")
    assert panel.current_name() == ".."


def test_going_up_is_a_step_in_the_history_too(fs, tree):
    panel = Panel(fs, str(tree / "adir"))
    assert panel.go_parent()
    assert panel.back()
    assert panel.path == str(tree / "adir")


def test_a_move_that_fails_or_goes_nowhere_is_not_recorded(fs, tree):
    panel = Panel(fs, str(tree))
    assert panel.chdir(str(tree / "missing")) is False
    assert panel.chdir(str(tree)) is True          # already there
    assert panel.set_location(fs, str(tree)) is True
    assert panel.history == []


def test_a_new_backend_is_part_of_the_place(fs, tree, tmp_path):
    other = LocalFileSystem()
    panel = Panel(fs, str(tree))
    assert panel.set_location(other, str(tmp_path))
    assert panel.back()
    assert panel.fs is fs and panel.path == str(tree)
    assert panel.forward()
    assert panel.fs is other and panel.path == str(tmp_path)


def test_going_somewhere_new_gives_up_the_way_forward(fs, tree):
    panel = Panel(fs, str(tree))
    panel.chdir(str(tree / "adir"))
    panel.back()
    assert panel.future
    panel.chdir(str(tree / "zdir"))
    assert panel.future == []
    assert panel.forward() is False
    assert panel.path == str(tree / "zdir")


def test_the_history_is_bounded(fs, tree, monkeypatch):
    from meridian_commander import panel as panel_mod

    monkeypatch.setattr(panel_mod, "HISTORY_LIMIT", 3)
    panel = Panel(fs, str(tree))
    for _ in range(5):
        panel.chdir(str(tree / "adir"))
        panel.go_parent()
    assert len(panel.history) == 3


def test_a_place_that_has_gone_is_passed_over(fs, tree):
    panel = Panel(fs, str(tree))
    panel.chdir(str(tree / "adir"))
    panel.chdir(str(tree / "zdir"))
    (tree / "adir").rmdir()
    assert panel.back() is True                    # adir is gone: skipped
    assert panel.path == str(tree)
    assert panel.back() is False                   # nothing earlier
    assert panel.path == str(tree)


def test_a_backend_that_cannot_be_revived_is_passed_over(fs, tree):
    panel = Panel(fs, str(tree))
    panel.chdir(str(tree / "adir"))
    assert panel.back(lambda backend: None) is False
    assert panel.path == str(tree / "adir")
    assert panel.history == []


# -- searching by name ---------------------------------------------------------

@pytest.fixture
def named(tmp_path):
    """Names chosen to tell the matching rules apart.

    Listed as: .., archive/, Reports/, notes.txt, old-report.txt, README.md,
    report.txt, résumé.pdf, Été.txt, журнал.log -- directories first, then
    the files by name without regard to case.
    """
    root = tmp_path / "named"
    (root / "archive").mkdir(parents=True)
    (root / "Reports").mkdir()
    for name in ("notes.txt", "old-report.txt", "README.md", "report.txt",
                 "r\u00e9sum\u00e9.pdf", "\u00c9t\u00e9.txt",
                 "\u0436\u0443\u0440\u043d\u0430\u043b.log"):
        write(str(root / name), "x")
    return root


def test_the_listing_the_search_tests_rely_on(fs, named):
    assert names(Panel(fs, str(named))) == [
        "..", "archive", "Reports", "notes.txt", "old-report.txt",
        "README.md", "report.txt", "r\u00e9sum\u00e9.pdf",
        "\u00c9t\u00e9.txt", "\u0436\u0443\u0440\u043d\u0430\u043b.log"]


def test_a_lowercase_pattern_matches_anywhere_and_ignores_case(fs, named):
    panel = Panel(fs, str(named))
    # Reports/ (a directory, so first), old-report.txt (in the middle of the
    # name) and report.txt, in the order they are listed.
    assert panel.matches("rep") == [2, 4, 6]


def test_a_capital_in_the_pattern_means_it(fs, named):
    panel = Panel(fs, str(named))
    assert panel.matches("Rep") == [2]
    assert panel.matches("REP") == []
    assert panel.matches("READ") == [5]


def test_the_parent_entry_is_never_a_match(fs, named):
    panel = Panel(fs, str(named))
    # Every file has a dot in it; ".." is all dots and is still not one.
    assert panel.matches(".") == [3, 4, 5, 6, 7, 8, 9]
    assert 0 not in panel.matches("..") and panel.matches("..") == []


def test_an_empty_pattern_and_a_missing_one_match_nothing(fs, named):
    panel = Panel(fs, str(named))
    assert panel.matches("") == []
    assert panel.matches("zzz") == []


def test_names_beyond_ascii_match_by_the_same_rules(fs, named):
    panel = Panel(fs, str(named))
    assert panel.matches("\u00e9t\u00e9") == [8]          # été finds Été
    assert panel.matches("\u00c9t\u00e9") == [8]          # Été means it
    assert panel.matches("\u00c9T") == []
    assert panel.matches("\u0436\u0443\u0440") == [9]


def test_typing_goes_to_the_first_match_from_the_top(fs, named):
    panel = Panel(fs, str(named))
    panel.move_to(8)                     # wherever the cursor happened to be
    search = Search(panel)
    search.type("r")
    assert panel.cursor == 1             # "archive" has an r in it
    search.type("e")
    assert panel.cursor == 2             # Reports/, ahead of every file
    search.type("p")
    assert search.pattern == "rep"
    assert panel.cursor == 2
    assert search.position() == (1, 3)


def test_next_and_previous_wrap_round_at_both_ends(fs, named):
    panel = Panel(fs, str(named))
    search = Search(panel)
    for char in "rep":
        search.type(char)
    search.step(1)
    assert panel.cursor == 4 and search.position() == (2, 3)
    search.step(1)
    assert panel.cursor == 6 and search.position() == (3, 3)
    search.step(1)                       # past the last: back to the first
    assert panel.cursor == 2 and search.position() == (1, 3)
    search.step(-1)                      # before the first: on to the last
    assert panel.cursor == 6
    search.step(-1)
    assert panel.cursor == 4


def test_from_between_matches_each_way_finds_the_nearest(fs, named):
    panel = Panel(fs, str(named))
    search = Search(panel)
    search.type("rep")
    panel.move_to(5)                     # README.md, between two matches
    search.step(1)
    assert panel.cursor == 6
    panel.move_to(5)
    search.step(-1)
    assert panel.cursor == 4


def test_a_single_match_steps_onto_itself(fs, named):
    panel = Panel(fs, str(named))
    search = Search(panel)
    search.type("Rep")
    search.step(1)
    assert panel.cursor == 2
    search.step(-1)
    assert panel.cursor == 2


def test_a_character_too_many_leaves_the_cursor_on_the_last_match(fs, named):
    panel = Panel(fs, str(named))
    search = Search(panel)
    search.type("rep")
    search.step(1)                       # on old-report.txt
    search.type("x")
    assert search.pattern == "repx"
    assert panel.cursor == 4
    assert search.position() == (0, 0)
    search.step(1)                       # nothing to step to
    assert panel.cursor == 4


def test_backspace_goes_back_to_the_first_match_of_the_shorter_pattern(
        fs, named):
    panel = Panel(fs, str(named))
    search = Search(panel)
    search.type("rep")
    search.step(1)
    search.step(1)                       # on report.txt
    search.backspace()
    assert search.pattern == "re"
    assert panel.cursor == 2
    search.type("x")
    search.type("y")                     # two characters too many ...
    search.backspace()
    search.backspace()                   # ... and back again
    assert search.pattern == "re" and panel.cursor == 2


def test_backspace_with_nothing_typed_leaves_everything_alone(fs, named):
    panel = Panel(fs, str(named))
    panel.move_to(7)
    search = Search(panel)
    search.backspace()
    assert search.pattern == ""
    assert panel.cursor == 7
    assert search.position() == (0, 0)


def test_the_matches_are_found_once_per_pattern_not_once_per_frame(
        fs, named, monkeypatch):
    panel = Panel(fs, str(named))
    search = Search(panel)
    search.type("rep")
    passes = []
    real = panel.matches
    monkeypatch.setattr(panel, "matches",
                        lambda pattern: passes.append(pattern) or real(pattern))
    for _ in range(5):                   # five frames' worth of footers
        search.position()
    search.step(1)
    assert passes == []
    search.type(".")
    assert passes == ["rep."]


def test_a_listing_reloaded_under_the_search_is_searched_again(fs, named):
    panel = Panel(fs, str(named))
    search = Search(panel)
    search.type("rep")
    assert search.position()[1] == 3
    write(str(named / "zz-report.txt"), "x")
    panel.refresh()
    assert search.position() == (1, 4)
    panel.toggle_hidden()                # a new listing, the same names
    assert search.hits() == panel.matches("rep")


def test_a_move_that_stays_put_keeps_its_reason(tmp_path):
    (tmp_path / "sub").mkdir()
    fs = LocalFileSystem()
    panel = Panel(fs, str(tmp_path))
    assert not panel.chdir(str(tmp_path / "missing"))
    # The rollback listed the old place again, which cleared the error.
    assert panel.error is None and "missing" in panel.failure
    assert panel.chdir("sub") and panel.failure is None
    assert not panel.set_location(fs, str(tmp_path / "gone"))
    assert panel.path == str(tmp_path / "sub") and "gone" in panel.failure
