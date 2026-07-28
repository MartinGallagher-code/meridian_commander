"""Panel navigation, sorting, tagging and scrolling."""

from __future__ import annotations

import pytest

from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.panel import Panel

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


def test_restoring_the_cursor_falls_back_to_the_top(fs, tree):
    panel = Panel(fs, str(tree))
    panel.move_to(4)
    # The remembered name is gone from the listing, so the cursor resets.
    panel.refresh(keep_name="no-such-entry")
    assert panel.cursor == 0


def test_set_location_onto_a_second_backend(fs, tmp_path):
    write(str(tmp_path / "here" / "a.txt"), "a")
    write(str(tmp_path / "there" / "b.txt"), "b")
    panel = Panel(fs, str(tmp_path / "here"))
    panel.top = 4
    other = LocalFileSystem()
    assert panel.set_location(other, str(tmp_path / "there")) is True
    assert panel.fs is other
    assert panel.top == 0
