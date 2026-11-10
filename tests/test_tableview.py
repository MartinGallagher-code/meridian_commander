# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The full-screen table browser: choosing it, drawing, keys, filters, records.

Fixtures stay neutral -- fruit, cities and numbers -- rather than copies of
real results files.
"""

from __future__ import annotations

import curses

import pytest

from meridian_commander import dialogs, theme
from meridian_commander import tableview as mod
from meridian_commander.archive import ArchiveFileSystem
from meridian_commander.browsers import has_own_browser, viewer_for
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.sheetview import WIDTH_CHOICES, SheetView
from meridian_commander.table import display_width
from meridian_commander.tableview import TableView, displayed
from meridian_commander.viewer import Viewer

from support import (
    _FakeRemoteBackend,
    _KeyScript,
    _NoLocalPath,
    script_newwin,
    with_curses_screen,
    write,
    write_xlsx,
    write_zip,
)

FRUIT = ("fruit\tprice\tcity\n"
         "apple\t3\tParis\n"
         "pear\t10.5\tOslo\n"
         "fig\t0.25\tParis\n"
         "plum\t\tLima\n")


def make(fs, tmp_path, text=FRUIT, name="fruit.tsv"):
    write(str(tmp_path / name), text)
    return TableView(fs, str(tmp_path / name))


@pytest.fixture
def fruit(fs, tmp_path):
    return make(fs, tmp_path)


def wide(fs, tmp_path, rows=40, cols=12):
    """A table bigger than any test screen, each cell naming its position."""
    lines = ["\t".join(f"h{c}" for c in range(cols))]
    lines += ["\t".join(f"r{r}c{c}" for c in range(cols)) for r in range(rows)]
    return make(fs, tmp_path, "\n".join(lines) + "\n", "wide.tsv")


def _run(monkeypatch, view, keys, rows=12, cols=60):
    captured = script_newwin(monkeypatch, keys)
    with_curses_screen(rows, cols, view.run)
    return captured["window"]


def _draw(view, rows=12, cols=60):
    """Draw once on a real curses window and return what was written."""
    def run(stdscr):
        window = _KeyScript(curses.newwin(rows, cols, 0, 0), [])
        view.draw(window)
        return window.drawn
    return with_curses_screen(rows, cols, run)


def _texts(drawn):
    return [a[2] for a in drawn if len(a) > 2 and isinstance(a[2], str)]


def _text(view, **size):
    return " ".join(_texts(_draw(view, **size)))


def _shown(view):
    return [view.records[index][0] for index in view.order]


# -- choosing the browser ------------------------------------------------------
@pytest.mark.parametrize("name", ["a.tsv", "a.csv", "a.tab", "a.psv",
                                  "A.TSV", "Data.Csv"])
def test_each_table_suffix_opens_in_the_table_browser(fs, tmp_path, name):
    write(str(tmp_path / name), "a\n1\n")
    assert isinstance(viewer_for(fs, str(tmp_path / name)), TableView)
    # So the configured pager no longer takes them, as it does not take a
    # spreadsheet: less shows a TSV as misaligned as the text viewer did.
    assert has_own_browser(name) is True


def test_everything_else_opens_as_it_did(fs, tmp_path):
    write(str(tmp_path / "notes.txt"), "a\tb\n")
    book = write_xlsx(str(tmp_path / "book.xlsx"), {"S": [["a"]]})
    assert isinstance(viewer_for(fs, str(tmp_path / "notes.txt")), Viewer)
    assert isinstance(viewer_for(fs, book), SheetView)
    assert has_own_browser("notes.txt") is False


def test_a_table_on_a_remote_pane_is_read_through_the_backend(tmp_path):
    write(str(tmp_path / "far.tsv"), FRUIT)
    for backend in (_FakeRemoteBackend(), _NoLocalPath()):
        view = viewer_for(backend, str(tmp_path / "far.tsv"))
        assert isinstance(view, TableView)
        assert view.error is None and view.records[1] == ["apple", "3", "Paris"]


def test_a_table_inside_an_archive_opens(tmp_path):
    archive = write_zip(str(tmp_path / "bundle.zip"), {"data/fruit.tsv": FRUIT})
    fs = ArchiveFileSystem(LocalFileSystem(), archive)
    view = viewer_for(fs, "/data/fruit.tsv")
    assert isinstance(view, TableView)
    assert view.error is None and view.labels() == ["fruit", "price", "city"]


# -- loading -------------------------------------------------------------------
def test_the_file_is_read_with_its_header(fruit):
    assert fruit.name == "fruit.tsv" and fruit.error is None
    assert fruit.labels() == ["fruit", "price", "city"]
    assert fruit.data_rows() == 4 and fruit.ncols == 3
    assert _shown(fruit) == ["apple", "pear", "fig", "plum"]
    assert fruit.current_cell() == "apple"


def test_a_file_that_is_not_there_reports_itself(fs, tmp_path):
    view = TableView(fs, str(tmp_path / "missing.tsv"))
    assert view.error and view.records == [] and view.order == []
    assert view.current_cell() == ""


def test_a_backend_that_fails_reports_itself(tmp_path):
    class Broken(LocalFileSystem):
        def open_read(self, path):
            raise OSError()            # no message at all

    view = TableView(Broken(), str(tmp_path / "x.tsv"))
    assert view.error == "OSError"


def test_a_header_cell_that_is_blank_is_named_by_its_number(fs, tmp_path):
    view = make(fs, tmp_path, "a\t\tc\n1\t2\t3\t4\n")
    assert view.labels() == ["a", "2", "c", "4"]


def test_rows_with_the_wrong_number_of_fields_are_known(fs, tmp_path):
    view = make(fs, tmp_path, "a\tb\n1\t2\n1\n1\t2\t3\n")
    assert [view.ragged(i) for i in range(4)] == [False, False, True, True]


# -- geometry ------------------------------------------------------------------
def test_column_widths_follow_the_content_in_display_columns(fs, tmp_path):
    view = make(fs, tmp_path, "name\tn\n漢字漢字漢字\t1\n")
    # Six wide characters take twelve columns, not six; the header has one
    # column more than its name, for the sort arrow.
    assert view._measure()[0] == [12, 3]


def test_a_very_wide_column_is_clamped_and_a_narrow_one_kept_readable(fs, tmp_path):
    view = make(fs, tmp_path, "a\tb\n" + "x" * 200 + "\ty\n")
    assert view._measure()[0] == [view.max_width, 3]


def test_widths_are_measured_over_the_top_of_the_file_only(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "WIDTH_SAMPLE_ROWS", 2)
    view = make(fs, tmp_path, "a\nab\ncd\na much longer one\n")
    assert view._measure()[0] == [3]


def test_measurements_are_cached_per_header_setting_and_width(fruit):
    first = fruit._measure()
    assert fruit._measure() is first
    fruit.cycle_width()
    assert fruit._measure() is not first


def test_numeric_columns_and_their_decimal_tails(fruit):
    _widths, numeric, tails = fruit._measure()
    assert numeric == [False, True, False]
    assert tails == [0, 3, 0]          # ".25" is the longest decimal part


def test_numbers_line_up_on_their_decimal_points():
    assert displayed("3", 3) == ("3   ", True)
    assert displayed("10.5", 3) == ("10.5 ", True)
    assert displayed(" 0.25 ", 3) == ("0.25", True)
    assert displayed("pear", 3) == ("pear", False)
    # A cell with more decimals than the sample saw is not padded negatively.
    assert displayed("1.12345", 3) == ("1.12345", True)


def test_a_control_character_in_a_cell_is_drawn_as_a_space():
    assert displayed("a\tb", 0) == ("a b", False)


def test_the_row_number_gutter_grows_with_the_row_count(fs, tmp_path):
    assert make(fs, tmp_path, "a\n1\n").gutter() == 4
    big = make(fs, tmp_path, "a\n" + "1\n" * 1000, "big.tsv")
    assert big.gutter() == 5


def test_the_layout_places_columns_side_by_side(fruit):
    placed = fruit.layout(60)
    assert [c for c, _x, _w in placed] == [0, 1, 2]
    assert [x for _c, x, _w in placed] == [0, 7, 14]   # widths 6, 6 and 5


def test_the_layout_stops_at_the_edge_and_never_leaves_a_blank_screen(fruit):
    assert [c for c, _x, _w in fruit.layout(12)] == [0]
    assert fruit.layout(4) == [(0, 0, 4)]
    assert fruit.layout(0) == []


# -- movement ------------------------------------------------------------------
def test_moving_around_with_the_arrows_and_the_letters(monkeypatch, fruit):
    _run(monkeypatch, fruit, [curses.KEY_DOWN, curses.KEY_RIGHT, ord("q")])
    assert (fruit.row, fruit.col) == (1, 1)
    _run(monkeypatch, fruit, [ord("j"), ord("l"), ord("q")])
    assert (fruit.row, fruit.col) == (2, 2)
    _run(monkeypatch, fruit, [curses.KEY_UP, curses.KEY_LEFT, ord("q")])
    assert (fruit.row, fruit.col) == (1, 1)
    _run(monkeypatch, fruit, [ord("k"), ord("h"), ord("q")])
    assert (fruit.row, fruit.col) == (0, 0)


def test_the_cursor_never_leaves_the_table(monkeypatch, fruit):
    _run(monkeypatch, fruit, [curses.KEY_UP, curses.KEY_LEFT, ord("q")])
    assert (fruit.row, fruit.col) == (0, 0)
    _run(monkeypatch, fruit, [ord("G"), curses.KEY_END, curses.KEY_DOWN,
                              curses.KEY_RIGHT, ord("q")])
    assert (fruit.row, fruit.col) == (3, 2)


def test_paging_and_jumping_to_the_ends(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    _run(monkeypatch, view, [curses.KEY_NPAGE, ord("q")], rows=12)
    assert view.row == 9
    _run(monkeypatch, view, [curses.KEY_PPAGE, ord("q")], rows=12)
    assert view.row == 0
    _run(monkeypatch, view, [ord("G"), ord("q")])
    assert view.row == 39
    _run(monkeypatch, view, [ord("g"), curses.KEY_END, ord("q")])
    assert (view.row, view.col) == (0, 11)
    _run(monkeypatch, view, [curses.KEY_HOME, ord("q")])
    assert view.col == 0


def test_the_header_stays_pinned_while_the_rows_scroll(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    window = _run(monkeypatch, view, [ord("G"), ord("q")], rows=12)
    assert view.top == 39 - 9 + 1
    last = [a for a in window.drawn if a[0] == 1]
    assert any("h0" in a[2] for a in last)
    assert any("r39c0" in a[2] for a in window.drawn if a[0] > 1)


def test_the_view_pans_to_keep_the_cursor_visible(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    _run(monkeypatch, view, [curses.KEY_END, ord("q")], cols=40)
    assert view.left > 0
    _run(monkeypatch, view, [curses.KEY_HOME, ord("q")], cols=40)
    assert view.left == 0


def test_movement_in_an_empty_file_stays_put(monkeypatch, fs, tmp_path):
    view = make(fs, tmp_path, "")
    _run(monkeypatch, view, [curses.KEY_DOWN, curses.KEY_RIGHT, ord("G"),
                             curses.KEY_END, 10, ord("s"), ord("q")])
    assert (view.row, view.col) == (0, 0) and view.record is False


# -- search ----------------------------------------------------------------------
def test_finding_a_value_moves_the_cursor(fruit):
    fruit.set_search("oslo")
    assert fruit.find_next(1) is True
    assert (fruit.row, fruit.col) == (1, 2)


def test_search_is_case_sensitive_when_the_pattern_is_not_lowercase(fruit):
    fruit.set_search("OSLO")
    assert fruit.find_next(1) is False
    fruit.set_search("Oslo")
    assert fruit.find_next(1) is True


def test_searching_wraps_around_and_goes_backwards(fruit):
    fruit.set_search("apple")
    fruit.row, fruit.col = 3, 2
    assert fruit.find_next(1) is True and (fruit.row, fruit.col) == (0, 0)
    fruit.set_search("lima")
    assert fruit.find_next(-1) is True and (fruit.row, fruit.col) == (3, 2)


def test_search_looks_only_at_the_rows_shown(fruit):
    fruit.add_filter("city = Paris")
    fruit.set_search("oslo")
    assert fruit.find_next(1) is False


def test_searching_for_nothing_or_in_nothing_finds_nothing(fs, tmp_path, fruit):
    assert fruit.find_next(1) is False
    view = make(fs, tmp_path, "", "e.tsv")
    view.set_search("x")
    assert view.find_next(1) is False


def test_the_search_prompt_drives_the_search(monkeypatch, fruit):
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "oslo")
    _run(monkeypatch, fruit, [ord("/"), ord("q")])
    assert (fruit.row, fruit.col) == (1, 2)


def test_a_search_that_finds_nothing_says_so(monkeypatch, fruit):
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "zebra")
    assert "not found" in _run(monkeypatch, fruit, [curses.KEY_F7, ord("q")]).text


def test_a_cancelled_or_empty_search(monkeypatch, fruit):
    fruit.set_search("oslo")
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: None)
    _run(monkeypatch, fruit, [ord("/"), ord("q")])
    assert fruit.search == "oslo"
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "")
    _run(monkeypatch, fruit, [ord("/"), ord("q")])
    assert fruit.search == ""


def test_the_next_and_previous_keys(monkeypatch, fruit):
    fruit.set_search("paris")
    _run(monkeypatch, fruit, [ord("n"), ord("q")])
    assert (fruit.row, fruit.col) == (0, 2)
    _run(monkeypatch, fruit, [ord("n"), ord("q")])
    assert (fruit.row, fruit.col) == (2, 2)
    _run(monkeypatch, fruit, [ord("N"), ord("q")])
    assert (fruit.row, fruit.col) == (0, 2)
    _run(monkeypatch, fruit, [ord("p"), ord("q")])
    assert (fruit.row, fruit.col) == (2, 2)


def test_next_and_previous_say_when_there_is_no_match(monkeypatch, fruit):
    fruit.set_search("zebra")
    assert "no match" in _run(monkeypatch, fruit, [ord("n"), ord("q")]).text
    assert "no match" in _run(monkeypatch, fruit, [ord("N"), ord("q")]).text


def test_next_without_a_pattern_does_nothing(monkeypatch, fruit):
    window = _run(monkeypatch, fruit, [ord("n"), ord("N"), ord("q")])
    assert "no match" not in window.text


# -- filters ---------------------------------------------------------------------
def test_a_filter_from_the_prompt(monkeypatch, fruit):
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "city = paris")
    window = _run(monkeypatch, fruit, [ord("f"), ord("q")])
    assert _shown(fruit) == ["apple", "fig"]
    assert "2 of 4 rows: city = paris" in window.text


def test_filters_stack_and_numbers_compare_as_numbers(fruit):
    assert fruit.add_filter("city = paris") is True
    assert fruit.add_filter("price < 1") is True
    assert _shown(fruit) == ["fig"]


def test_a_filter_on_a_missing_column_says_so_and_changes_nothing(monkeypatch, fruit):
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "colour = red")
    window = _run(monkeypatch, fruit, [ord("f"), ord("q")])
    assert "no column called 'colour'" in window.text
    assert fruit.view.filters == [] and len(fruit.order) == 4


@pytest.mark.parametrize("answer", [None, "", "   "])
def test_a_cancelled_or_blank_filter_adds_nothing(monkeypatch, fruit, answer):
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: answer)
    _run(monkeypatch, fruit, [ord("f"), ord("q")])
    assert fruit.view.filters == []


def test_the_quick_filter_keeps_or_hides_the_cursors_value(monkeypatch, fruit):
    fruit.col = 2                                  # city, on "Paris"
    _run(monkeypatch, fruit, [ord("="), ord("q")])
    assert _shown(fruit) == ["apple", "fig"]
    _run(monkeypatch, fruit, [curses.KEY_BACKSPACE, ord("!"), ord("q")])
    assert _shown(fruit) == ["pear", "plum"]


def test_a_quick_filter_with_no_rows_to_take_from_says_so(fruit):
    fruit.add_filter("nothing-matches-this")
    fruit.quick_filter(keep=True)
    assert "no row" in fruit.notice and len(fruit.view.filters) == 1


def test_dropping_and_clearing_filters(monkeypatch, fruit):
    fruit.add_filter("paris")
    fruit.add_filter("apple")
    _run(monkeypatch, fruit, [127, ord("q")])
    assert [f.text for f in fruit.view.filters] == ["paris"]
    fruit.add_filter("fig")
    _run(monkeypatch, fruit, [ord("0"), ord("q")])
    assert fruit.view.filters == [] and len(fruit.order) == 4
    # Clearing restores the file's own order, rows and all.
    assert _shown(fruit) == ["apple", "pear", "fig", "plum"]


def test_dropping_or_clearing_with_no_filters_says_so(monkeypatch, fruit):
    assert "no filters to drop" in _run(monkeypatch, fruit, [8, ord("q")]).text
    assert "no filters to clear" in _run(monkeypatch, fruit,
                                         [ord("0"), ord("q")]).text


def test_the_cursor_stays_on_its_row_through_a_filter(fruit):
    fruit.row = 2                                   # fig
    fruit.add_filter("city = paris")
    assert fruit.order[fruit.row] == 3              # still fig
    fruit.add_filter("apple")
    assert fruit.row == 0                           # fig is gone


def test_a_filter_that_matches_nothing_says_how_to_undo_it(fruit):
    fruit.add_filter("nothing-matches-this")
    text = _text(fruit)
    assert "0 of 4 rows" in text and "no row matches" in text


# -- sorting --------------------------------------------------------------------
def test_sorting_by_the_cursors_column(monkeypatch, fruit):
    fruit.col = 1
    window = _run(monkeypatch, fruit, [ord("s"), ord("q")])
    assert _shown(fruit) == ["fig", "apple", "pear", "plum"]
    assert "sorted by price" + theme.glyph("up") in window.text
    _run(monkeypatch, fruit, [ord("S"), ord("q")])
    assert _shown(fruit) == ["pear", "apple", "fig", "plum"]
    _run(monkeypatch, fruit, [ord("s"), ord("q")])
    assert _shown(fruit) == ["apple", "pear", "fig", "plum"]


def test_the_sorted_column_is_marked_in_the_header(fruit):
    fruit.col = 1
    fruit.sort_by_cursor()
    fruit.sort_by_cursor()
    headers = [a[2] for a in _draw(fruit) if a[0] == 1]
    assert any(text.endswith(theme.glyph("down")) and "price" in text
               for text in headers)


def test_sorting_a_file_with_no_columns_does_nothing(fs, tmp_path):
    view = make(fs, tmp_path, "")
    view.sort_by_cursor()
    assert view.view.keys == []


# -- the header line ------------------------------------------------------------
def test_the_first_line_can_be_read_as_data(monkeypatch, fruit):
    window = _run(monkeypatch, fruit, [ord("H"), ord("q")])
    assert fruit.has_header is False and "first line is data" in window.text
    assert fruit.labels() == ["1", "2", "3"] and fruit.data_rows() == 5
    assert _shown(fruit)[0] == "fruit"
    _run(monkeypatch, fruit, [ord("H"), ord("q")])
    assert fruit.has_header is True and fruit.labels()[0] == "fruit"


def test_filters_carry_across_the_header_setting(fruit):
    fruit.add_filter("city ~ a")
    fruit.toggle_header()
    # "city" is now column 3, and still the column the filter looks at.
    assert _shown(fruit) == ["apple", "fig", "plum"]


# -- comment lines ------------------------------------------------------------------
NOTED = "# from a tool\n# second line\nfruit\tprice\napple\t3\n"


def test_comment_lines_are_counted_in_the_title(fs, tmp_path):
    view = make(fs, tmp_path, NOTED)
    assert view.labels() == ["fruit", "price"]
    assert "2 comment lines above (#)" in _text(view, cols=80)
    assert "comment line" not in _text(make(fs, tmp_path, FRUIT, "plain.tsv"),
                                       cols=80)


def test_the_comment_key_shows_them(monkeypatch, fs, tmp_path):
    view = make(fs, tmp_path, NOTED)
    shown = []
    monkeypatch.setattr(dialogs, "message",
                        lambda stdscr, title, text, **k: shown.append(text))
    _run(monkeypatch, view, [ord("#"), ord("q")])
    assert shown == ["# from a tool\n# second line"]


def test_the_comment_key_says_when_there_are_none(monkeypatch, fruit):
    window = _run(monkeypatch, fruit, [ord("#"), ord("q")])
    assert "no comment lines" in window.text


def test_a_file_of_nothing_but_comments_says_so(fs, tmp_path):
    view = make(fs, tmp_path, "# only a note\n")
    assert "nothing but comment lines" in _text(view)


# -- colour -----------------------------------------------------------------------
def _cell_attrs(drawn, y):
    return [a[3] for a in drawn if a[0] == y and len(a) > 3]


def test_neighbouring_columns_are_drawn_in_different_colours(fruit):
    # Row 3 on screen is the second row of data, which the cursor is not on.
    on = _cell_attrs(_draw(fruit), 3)
    assert on[1:] == [theme.attr("edit"), theme.attr("tablecol1"),
                      theme.attr("tablecol2")]


def test_the_colour_key_turns_the_column_colours_off_and_on(monkeypatch, fruit):
    window = _run(monkeypatch, fruit, [ord("c"), ord("q")])
    assert fruit.colour is False and "colours off" in window.text
    assert set(_cell_attrs(_draw(fruit), 3)[1:]) == {theme.attr("edit")}
    window = _run(monkeypatch, fruit, [ord("C"), ord("q")])
    assert fruit.colour is True and "colours on" in window.text


def test_the_column_colours_cycle(fs, tmp_path):
    view = make(fs, tmp_path, "a\tb\tc\td\te\n1\t2\t3\t4\t5\n6\t7\t8\t9\t0\n")
    attrs = _cell_attrs(_draw(view, cols=60), 3)[1:]
    assert attrs[4] == attrs[0] and len(set(attrs)) == 4


def test_the_column_colours_are_theme_roles_with_a_midnight_version():
    assert all(role in theme.ROLES for role in mod.COLUMN_ROLES)
    # Midnight's ground is black, so the blue-ground colours are restated.
    assert all(role in theme.SCHEMES["midnight"]
               for role in mod.COLUMN_ROLES[1:])


# -- the width key ------------------------------------------------------------------
def test_the_width_key_cycles(monkeypatch, fs, tmp_path):
    view = make(fs, tmp_path, "a\n" + "x" * 100 + "\n")
    _run(monkeypatch, view, [ord("w"), ord("q")])
    assert view._measure()[0] == [WIDTH_CHOICES[2]]
    _run(monkeypatch, view, [ord("W"), ord("q")])
    assert view._measure()[0] == [WIDTH_CHOICES[0]]


# -- the record view --------------------------------------------------------------
def test_enter_shows_the_row_as_a_record(monkeypatch, fruit):
    fruit.row = 1
    window = _run(monkeypatch, fruit, [10, ord("j")])
    assert "Row 2 (2 of 4 shown)" in window.text
    assert "price : " in window.text and "10.5" in window.text


def test_the_record_steps_through_the_rows_shown(monkeypatch, fruit):
    _run(monkeypatch, fruit, [curses.KEY_ENTER, curses.KEY_DOWN, ord("j"),
                              ord("k"), ord("q")])
    assert fruit.row == 1 and fruit.record is False
    window = _run(monkeypatch, fruit, [13, ord("k"), ord("k"), 27])
    assert fruit.row == 0 and "first row shown" in window.text
    window = _run(monkeypatch, fruit, [10, ord("G")] + [ord("j")] * 4 + [10])
    assert fruit.row == 3 and "last row shown" in window.text


@pytest.mark.parametrize("key", [ord("q"), ord("Q"), 27, curses.KEY_F3,
                                 curses.KEY_F10, 10, 13, curses.KEY_ENTER])
def test_every_way_out_of_the_record_returns_to_the_table(monkeypatch, fruit, key):
    # The record closes, and the next key quits the table itself.
    _run(monkeypatch, fruit, [10, key, ord("q")])
    assert fruit.record is False


def test_a_long_value_is_wrapped_not_cut(fs, tmp_path):
    view = make(fs, tmp_path, "name\tnote\nfig\t" + "word " * 40 + "\n")
    _start, lines = view.record_lines(40)
    notes = [value for _name, value, _role in lines]
    assert "".join(notes[1:]) == "word " * 40
    assert all(display_width(n) <= 40 - 7 for n in notes)


def test_a_short_row_says_which_fields_it_is_missing(fs, tmp_path):
    view = make(fs, tmp_path, "a\tb\tc\n1\n")
    _start, lines = view.record_lines(60)
    assert [(n.strip(), v) for n, v, _r in lines] == [
        ("a :", "1"),
        ("b :", "(missing: this row is short of fields)"),
        ("c :", "(missing: this row is short of fields)"),
    ]
    assert "Row 1 (1 of 1 shown) -- 1 field; the first line has 3" \
        in _text_record(view)


def test_a_long_row_shows_its_extra_fields_by_number(fs, tmp_path):
    view = make(fs, tmp_path, "a\n1\t2\n")
    _start, lines = view.record_lines(60)
    assert [n.strip() for n, _v, _r in lines] == ["a :", "2 :"]


def test_a_line_break_in_a_csv_cell_shows_as_one(fs, tmp_path):
    view = make(fs, tmp_path, 'a,b\n1,"two\nlines"\n', "x.csv")
    _start, lines = view.record_lines(60)
    assert [v for _n, v, _r in lines] == ["1", "two", "lines"]


def test_the_record_values_follow_the_colour_setting(fruit):
    roles = [role for _n, _v, role in fruit.record_lines(60)[1]]
    assert roles == ["edit", "tablecol1", "tablecol2"]
    fruit.toggle_colour()
    assert {role for _n, _v, role in fruit.record_lines(60)[1]} == {"edit"}


def _text_record(view, rows=12, cols=60):
    view.record = True
    return _text(view, rows=rows, cols=cols)


def test_a_record_longer_than_the_screen_scrolls(monkeypatch, fs, tmp_path):
    header = "\t".join(f"field{i}" for i in range(30))
    values = "\t".join(str(i) for i in range(30))
    view = make(fs, tmp_path, f"{header}\n{values}\n{values}\n")
    _run(monkeypatch, view, [10, curses.KEY_NPAGE], rows=12)
    assert view.record_top == 10
    _run(monkeypatch, view, [10, ord(" "), ord(" "), ord(" ")], rows=12)
    assert view.record_top == 20             # 30 lines, 10 at a time
    _run(monkeypatch, view, [10, curses.KEY_END], rows=12)
    assert view.record_top == 20
    _run(monkeypatch, view, [10, curses.KEY_END, curses.KEY_PPAGE], rows=12)
    assert view.record_top == 10
    _run(monkeypatch, view, [10, curses.KEY_END, curses.KEY_HOME], rows=12)
    assert view.record_top == 0
    # Stepping to the next row starts it at its top.
    _run(monkeypatch, view, [10, curses.KEY_END, ord("j")], rows=12)
    assert view.record_top == 0 and view.row == 1


def test_a_key_the_record_does_not_know_is_ignored(monkeypatch, fruit):
    _run(monkeypatch, fruit, [10, ord("z"), curses.KEY_RESIZE, ord("q")])
    assert fruit.row == 0


def test_enter_with_no_rows_shown_stays_on_the_table(monkeypatch, fruit):
    fruit.add_filter("nothing-matches-this")
    _run(monkeypatch, fruit, [10, ord("q")])
    assert fruit.record is False


def test_the_record_view_survives_a_tiny_screen(fruit):
    for rows, cols in [(1, 20), (2, 20), (3, 4), (4, 1)]:
        _text_record(fruit, rows=rows, cols=cols)


# -- drawing, on a real screen ----------------------------------------------------
def test_the_title_names_the_file_its_delimiter_and_its_size(fruit):
    assert "fruit.tsv -- 4 rows -- 3 columns, tab" in _text(fruit)


def test_one_row_and_one_column_are_not_plural(fs, tmp_path):
    assert "1 row -- 1 column, tab " in _text(make(fs, tmp_path, "a\n1\n"))


def test_a_truncated_table_says_so(fs, tmp_path, monkeypatch):
    from meridian_commander import table
    monkeypatch.setattr(table, "MAX_ROWS", 2)
    view = make(fs, tmp_path, "a\n1\n2\n3\n")
    assert "[truncated]" in _text(view)


def test_the_header_row_numbers_and_values_are_drawn(fruit):
    text = _text(fruit)
    assert "fruit" in text and "city" in text
    assert "  1 " in text and "  4 " in text
    assert "Paris" in text and "Lima" in text


def test_numbers_are_right_aligned_and_their_points_line_up(fruit):
    drawn = _texts(_draw(fruit))
    # The price column is six wide.  "10.5" carries one space for the second
    # decimal of "0.25", and "3" three, for a point and two decimals; then
    # all three are right-aligned, which puts every point in one column.
    prices = ["  3   ", " 10.5 ", "  0.25"]
    assert all(price in drawn for price in prices)
    assert [p.index(".") for p in prices[1:]] == [3, 3]
    assert prices[0].index("3") == 2


def test_a_cjk_cell_takes_the_columns_it_needs(fs, tmp_path):
    view = make(fs, tmp_path, "name\tn\n漢字漢字\t1\nab\t2\n")
    drawn = _draw(view)
    first = [a[2] for a in drawn if a[0] in (2, 3) and a[1] == view.gutter()]
    # Both cells fill the column's eight terminal columns exactly, so the
    # next column starts in the same place on both rows -- which it did not
    # when widths were counted in characters.
    assert [display_width(text) for text in first] == [8, 8]
    second = {a[1] for a in drawn
              if a[0] in (2, 3) and a[1] > 0 and a[2].strip() in ("1", "2")}
    assert second == {view.gutter() + 9}


def test_a_ragged_row_is_marked_beside_its_number(fs, tmp_path):
    view = make(fs, tmp_path, "a\tb\n1\t2\n3\n")
    gutters = [a[2] for a in _draw(view) if a[1] == 0 and a[0] in (2, 3)]
    assert gutters == ["  1 ", "  2!"]


def test_the_footer_shows_the_cell_and_what_is_wrong_with_the_row(fs, tmp_path):
    view = make(fs, tmp_path, "a\tb\n1\n")
    view.col = 0
    text = _text(view)
    assert "a: 1" in text and "1 field; the first line has 2" in text


def test_the_footer_shows_the_search_and_any_notice(fruit):
    fruit.set_search("oslo")
    assert "/oslo" in _text(fruit)
    fruit.notice = " a passing remark "
    assert "a passing remark" in _text(fruit)


@pytest.mark.parametrize("text, message", [
    ("", "this file is empty"),
    ("a\tb\n", "nothing below the header"),
])
def test_an_empty_or_header_only_file_says_so(fs, tmp_path, text, message):
    assert message in _text(make(fs, tmp_path, text))


def test_the_error_screen_replaces_the_table(fs, tmp_path):
    view = TableView(fs, str(tmp_path / "missing.tsv"))
    text = _text(view)
    assert "Cannot read" in text and "[q]uit" in text and "rows" not in text


@pytest.mark.parametrize("rows, cols", [
    (1, 20), (2, 20), (3, 20), (4, 1), (4, 2), (4, 4), (4, 8),
])
def test_drawing_survives_a_screen_with_no_room_on_it(fruit, rows, cols):
    _draw(fruit, rows=rows, cols=cols)


@pytest.mark.parametrize("rows", [1, 2, 3])
def test_the_messages_survive_a_screen_with_no_room(fs, tmp_path, rows):
    _draw(make(fs, tmp_path, ""), rows=rows, cols=20)
    _draw(TableView(fs, str(tmp_path / "missing.tsv")), rows=rows, cols=20)


def test_a_cell_the_terminal_measures_wider_does_not_end_the_program(monkeypatch, fruit):
    # Every cell write is guarded: here every one of them is refused.
    def run(stdscr):
        window = _KeyScript(curses.newwin(12, 60, 0, 0), [], fail_draws=True)
        fruit.draw(window)
    with_curses_screen(12, 60, run)


def test_drawing_survives_a_cursor_past_the_visible_columns(fs, tmp_path):
    view = wide(fs, tmp_path)
    view.col = 11
    _draw(view, rows=12, cols=30)
    assert view.left > 0


def test_the_table_closes_on_every_quit_key(monkeypatch, fruit):
    for key in (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10):
        _run(monkeypatch, fruit, [key])


def test_an_unhandled_key_is_simply_ignored(monkeypatch, fruit):
    _run(monkeypatch, fruit, [ord("z"), curses.KEY_RESIZE, "ā", ord("q")])
    assert (fruit.row, fruit.col) == (0, 0)


# -- frozen columns ----------------------------------------------------------------
def _rules(drawn, x):
    """The rows on which the frozen columns' rule is drawn at ``x``."""
    return sorted(a[0] for a in drawn
                  if a[1] == x and a[2] == theme.glyph("v1"))


def test_F_freezes_the_columns_up_to_the_cursors(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    window = _run(monkeypatch, view, [ord("l"), ord("F"), ord("q")], cols=40)
    assert view.frozen == 2 and "frozen up to h1; F again unfreezes" \
        in window.text
    assert view.pinned() == [0, 1]


def test_panning_right_keeps_the_frozen_columns_at_the_left(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    window = _run(monkeypatch, view, [ord("F"), curses.KEY_END, ord("q")],
                  cols=40)
    placed = view.layout(36)
    assert placed[0] == (0, 0, 5) and view.left > 1
    assert [c for c, _x, _w in placed] == [0, 8, 9, 10, 11]
    headers = [a[2] for a in window.drawn if a[0] == 1]
    assert any("h0" in text for text in headers[-6:])
    assert any("h11" in text for text in headers[-6:])


def test_a_rule_marks_where_the_frozen_columns_end(fruit):
    fruit.toggle_freeze()                    # the cursor is on "fruit"
    gutter = fruit.gutter()
    # The fruit column is six wide; the rule takes the gap after it, on the
    # header and on every row drawn, and the columns after it keep their
    # places.
    assert _rules(_draw(fruit), gutter + 6) == [1, 2, 3, 4, 5]
    assert [x for _c, x, _w in fruit.layout(56)] == [0, 7, 14]
    fruit.toggle_freeze()
    assert _rules(_draw(fruit), gutter + 6) == []


def test_F_again_unfreezes(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    _run(monkeypatch, view, [ord("F"), ord("q")])
    window = _run(monkeypatch, view, [ord("F"), ord("q")])
    assert view.frozen == 0 and "columns unfrozen" in window.text
    assert view.pinned() == []


def test_moving_into_the_frozen_columns_leaves_the_rest_where_they_are(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    _run(monkeypatch, view, [ord("F"), curses.KEY_END, ord("q")], cols=40)
    left = view.left
    _run(monkeypatch, view, [curses.KEY_HOME, ord("q")], cols=40)
    assert view.col == 0 and view.left == left
    # One step right is the first column after the frozen ones, which pans
    # back into view.
    _run(monkeypatch, view, [ord("l"), ord("q")], cols=40)
    assert view.col == 1 and view.left == 1


def test_freezing_more_than_two_thirds_of_the_screen_is_refused(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    view.col = 5
    window = _run(monkeypatch, view, [ord("F"), ord("q")], cols=40)
    assert view.frozen == 0 and "too wide to freeze here" in window.text
    # Four columns of five, and their gaps, take 23 of the 24 allowed.
    view.col = 3
    _run(monkeypatch, view, [ord("F"), ord("q")], cols=40)
    assert view.frozen == 4


def test_a_table_not_yet_drawn_freezes_without_a_limit(fs, tmp_path):
    view = wide(fs, tmp_path)
    view.col = 11
    view.toggle_freeze()
    assert view.avail is None and view.frozen == 12


def test_frozen_columns_that_no_longer_fit_give_way(fs, tmp_path):
    # As after the screen shrinks: the frozen columns are left out once they
    # pass two-thirds of the room, and the first is cut rather than lost.
    view = wide(fs, tmp_path)
    view.frozen = 6
    assert [c for c, _x, _w in view.layout(36)] == [0, 1, 2, 3, 6, 7]
    long = make(fs, tmp_path, "name\tn\n" + "x" * 40 + "\t1\n", "long.tsv")
    long.frozen = 1
    assert long.layout(16) == [(0, 0, 11), (1, 12, 3)]
    # With no room for them at all, the table draws as if unfrozen.
    assert long.layout(4) == [(1, 0, 3)]


def test_freezing_an_empty_file_says_there_is_nothing_to_freeze(fs, tmp_path):
    view = make(fs, tmp_path, "")
    view.toggle_freeze()
    assert view.frozen == 0 and "no column to freeze" in view.notice


def test_a_hidden_column_is_not_drawn_among_the_frozen(fruit):
    fruit.col = 1
    fruit.toggle_freeze()
    fruit.hidden.add(0)
    assert fruit.pinned() == [1]
    assert [c for c, _x, _w in fruit.layout(56)] == [1, 2]


@pytest.mark.parametrize("rows, cols", [(1, 20), (3, 8), (4, 4), (12, 9)])
def test_frozen_columns_survive_a_screen_with_no_room(fs, tmp_path, rows, cols):
    view = wide(fs, tmp_path)
    view.col = 2
    view.toggle_freeze()
    view.col = 11
    _draw(view, rows=rows, cols=cols)


# -- hidden columns ----------------------------------------------------------------
def _headers(view, **size):
    return [a[2].strip() for a in _draw(view, **size)
            if a[0] == 1 and a[1] > 0 and a[2].strip()]


def test_minus_hides_the_cursors_column(monkeypatch, fruit):
    fruit.col = 1
    window = _run(monkeypatch, fruit, [ord("-"), ord("q")])
    assert fruit.hidden == {1} and fruit.col == 2
    assert "price hidden; + shows it again" in window.text
    assert _headers(fruit) == ["fruit", "city"]
    assert "3 columns (1 hidden), tab" in _text(fruit)


def test_hiding_the_last_column_moves_the_cursor_back(fruit):
    fruit.col = 2
    fruit.hide_column()
    assert fruit.hidden == {2} and fruit.col == 1


def test_the_last_column_shown_cannot_be_hidden(monkeypatch, fs, tmp_path, fruit):
    window = _run(monkeypatch, fruit, [ord("-"), ord("-"), ord("-"), ord("q")])
    assert fruit.hidden == {0, 1} and fruit.col == 2
    assert "the last column shown cannot be hidden" in window.text
    empty = make(fs, tmp_path, "", "empty.tsv")
    empty.hide_column()
    assert empty.hidden == set() and "cannot be hidden" in empty.notice


def test_movement_skips_hidden_columns(monkeypatch, fs, tmp_path):
    view = make(fs, tmp_path, "a\tb\tc\td\n1\t2\t3\t4\n")
    view.hidden = {0, 2, 3}
    view.col = 1
    _run(monkeypatch, view, [curses.KEY_RIGHT, curses.KEY_LEFT, ord("q")])
    assert view.col == 1                 # nowhere to go either way
    view.hidden = {1}
    view.col = 0
    _run(monkeypatch, view, [ord("l"), ord("q")])
    assert view.col == 2
    _run(monkeypatch, view, [ord("h"), ord("q")])
    assert view.col == 0
    view.hidden = {0, 3}
    _run(monkeypatch, view, [curses.KEY_END, ord("q")])
    assert view.col == 2
    _run(monkeypatch, view, [curses.KEY_HOME, ord("q")])
    assert view.col == 1


def test_search_skips_hidden_columns(fruit):
    fruit.hidden = {2}                    # city
    fruit.set_search("oslo")
    assert fruit.find_next(1) is False
    fruit.set_search("p")
    # "pear" on the second row, not "Paris" on the first: the city column
    # is hidden, so the next match in reading order is in the fruit column.
    assert fruit.find_next(1) is True and (fruit.row, fruit.col) == (1, 0)
    fruit.set_search("a")
    # Backwards from "pear", "Paris" and then (wrapping round) "Lima" would
    # be found first; with the city column hidden it is "apple", then "pear".
    assert fruit.find_next(-1) is True and (fruit.row, fruit.col) == (0, 0)
    assert fruit.find_next(-1) is True and (fruit.row, fruit.col) == (1, 0)


def test_the_record_leaves_hidden_columns_out_and_says_so(fruit):
    fruit.hidden = {1}
    _start, lines = fruit.record_lines(60)
    assert [(n.strip(), v) for n, v, _r in lines] == [
        ("fruit :", "apple"), ("city :", "Paris"),
        ("", "(hidden: price; + shows them)")]
    assert lines[-1][2] == "editdim"
    assert "Row 1 (1 of 4 shown) -- 1 column hidden -- fruit.tsv" \
        in _text_record(fruit)


def test_a_record_whose_fields_are_all_hidden_still_says_so(fs, tmp_path):
    # Row one has a single field, which is hidden; the other columns come
    # from a longer row further down, so this one has none to list.
    view = make(fs, tmp_path, "a\n1\n1\t2\t3\n")
    view.hidden = {0}
    view.col = 1
    _start, lines = view.record_lines(60)
    assert [v for _n, v, _r in lines] == ["(hidden: a; + shows them)"]


def test_plus_shows_every_hidden_column_again(monkeypatch, fruit):
    fruit.hidden = {0, 2}
    fruit.col = 1
    window = _run(monkeypatch, fruit, [ord("+"), ord("q")])
    assert fruit.hidden == set() and "2 hidden columns shown again" \
        in window.text
    window = _run(monkeypatch, fruit, [ord("+"), ord("q")])
    assert "no columns are hidden" in window.text


def test_filters_and_sorting_still_see_hidden_columns(fruit):
    fruit.add_filter("city = paris")
    fruit.col = 1
    fruit.sort_by_cursor()
    fruit.col = 2
    fruit.hide_column()
    assert _shown(fruit) == ["fig", "apple"]
    assert "2 of 4 rows: city = paris" in _text(fruit)


# -- the column summary --------------------------------------------------------------
def _summary(view, rows=30, cols=60):
    """What the summary screen draws, line by line from the top; a line
    with nothing drawn on it is blank."""
    drawn: dict[int, list[str]] = {}
    for a in _draw(view, rows=rows, cols=cols):
        if len(a) > 2 and isinstance(a[2], str):
            drawn.setdefault(a[0], []).append(a[2])
    return ["".join(drawn.get(y, [])).rstrip() for y in range(rows)]


def test_i_summarises_the_cursors_column(monkeypatch, fruit):
    fruit.col = 1                                       # price
    _run(monkeypatch, fruit, [ord("i")])                # ... then Escape
    assert fruit.summary is None                        # Escape closed it
    fruit.open_summary()
    lines = _summary(fruit, rows=30)
    assert lines[0] == " Column: price -- numbers -- 4 rows -- fruit.tsv"
    # Over 3, 10.5 and 0.25, with the plum's blank price counted as empty.
    assert lines[1:9] == [
        " Holds     numbers", " Rows      4", " Empty     1",
        " Distinct  3 values", " Minimum   0.25", " Maximum   10.5",
        " Mean      4.583333333", " Median    3"]
    assert lines[10].startswith(" Most frequent: Enter keeps")
    assert lines[11:14] == ["  1  3", "  1  10.5", "  1  0.25"]
    assert lines[15] == " Histogram"
    assert lines[29].startswith(" Up/Down value  Enter/= keep its rows")


def test_the_histogram_scales_its_bars_to_the_screen(fruit):
    fruit.col = 1
    fruit.open_summary()
    bars = [text for text, _role in fruit.summary_lines(fruit.summary, 60)
            if theme.glyph("block") in text or text.rstrip().endswith(" 0")]
    block = theme.glyph("block")
    # Ten equal ranges from 0.25 to 10.5: 0.25 in the first, 3 in the
    # third, 10.5 in the last; the fullest fills the room there is.
    assert [text.count(block) for text in bars] == [48, 0, 48, 0, 0, 0, 0,
                                                    0, 0, 48]
    assert bars[0].startswith("   0.25  ") and bars[0].rstrip().endswith(" 1")
    assert all(display_width(text) == 60 for text in bars)


def test_a_short_bar_is_never_drawn_as_none(fs, tmp_path):
    view = make(fs, tmp_path, "n\n" + "1\n" * 200 + "2\n")
    view.open_summary()
    lines = view.summary_lines(view.summary, 40)
    counts = [text.count(theme.glyph("block")) for text, _r in lines[-10:]]
    assert counts[0] > 1 and counts[-1] == 1
    # A screen too narrow for any bar still lists the ranges and counts.
    assert view.summary_lines(view.summary, 12)[-1][0].rstrip() == \
        "  1.9     1"


def test_a_text_column_has_no_statistics_or_histogram(fruit):
    fruit.col = 2
    fruit.open_summary()
    lines = _summary(fruit, rows=30)
    assert " Holds     text" in lines and " Distinct  3 values" in lines
    assert not any(line.startswith((" Mean", " Histogram")) for line in lines)
    assert lines[7:10] == ["  2  Paris", "  1  Oslo", "  1  Lima"]


def test_the_summary_is_over_the_rows_the_filters_keep(fruit):
    fruit.add_filter("city = paris")
    fruit.col = 1
    fruit.open_summary()
    lines = _summary(fruit, cols=80)
    assert lines[0] == (" Column: price -- numbers -- 2 of 4 rows, "
                        "filtered -- fruit.tsv")
    assert " Rows      2 of 4, the rows the filters keep" in lines
    assert " Mean      1.625" in lines


def test_the_summary_follows_the_file_order_whatever_the_sort(fruit):
    fruit.col = 2
    fruit.sort_by_cursor()
    fruit.sort_by_cursor()                  # Paris, Paris, Oslo, Lima
    fruit.col = 0
    fruit.open_summary()
    assert [value for value, _n in fruit.summary.top] == [
        "apple", "pear", "fig", "plum"]


def test_a_column_with_many_values_says_how_many_are_listed(fs, tmp_path):
    view = make(fs, tmp_path, "n\n" + "".join(f"v{i}\n" for i in range(25)))
    view.open_summary()
    assert any(line.startswith(" Most frequent (10 of 25): ")
               for line in _summary(view, rows=30))


def test_a_blank_column_says_there_is_nothing_to_pick(monkeypatch, fs, tmp_path):
    view = make(fs, tmp_path, "a\tb\n1\t\n2\t\n")
    view.col = 1
    view.open_summary()
    lines = _summary(view, rows=30)
    assert " Holds     nothing: every cell is blank" in lines
    assert "  (none: every cell in these rows is blank)" in lines
    view.summary = None
    window = _run(monkeypatch, view, [ord("i"), 10, ord("q"), ord("q")])
    assert "no value to filter on" in window.text and view.view.filters == []


def test_a_value_with_spaces_or_a_tab_is_shown_so(fs, tmp_path):
    view = make(fs, tmp_path, 'a\n" fig "\nx\ty\n', "x.csv")
    view.open_summary()
    texts = [text.rstrip() for text, _r in view.summary_lines(view.summary, 40)]
    assert '  1  " fig "' in texts


def test_enter_on_a_value_keeps_its_rows(monkeypatch, fruit):
    fruit.col = 2
    window = _run(monkeypatch, fruit, [ord("i"), curses.KEY_DOWN, 10, ord("q")],
                  rows=30)
    assert fruit.summary is None
    assert [f.text for f in fruit.view.filters] == ["city = Oslo"]
    assert _shown(fruit) == ["pear"]
    assert "1 of 4 rows: city = Oslo" in window.text


def test_equals_keeps_and_bang_hides_the_picked_values_rows(monkeypatch, fruit):
    fruit.col = 2
    _run(monkeypatch, fruit, [ord("i"), ord("="), ord("q")])
    assert _shown(fruit) == ["apple", "fig"]
    fruit.clear_filters()
    _run(monkeypatch, fruit, [ord("i"), ord("j"), ord("j"), ord("!"), ord("q")])
    assert [f.text for f in fruit.view.filters] == ["city != Lima"]
    assert _shown(fruit) == ["apple", "pear", "fig"]


def test_the_pick_stays_among_the_values(monkeypatch, fruit):
    fruit.col = 2
    _run(monkeypatch, fruit, [ord("i"), ord("k"), curses.KEY_UP, ord("q"),
                              ord("q")])
    assert fruit.summary_pick == 0
    _run(monkeypatch, fruit, [ord("i")] + [ord("j")] * 5 + [ord("q"), ord("q")])
    assert fruit.summary_pick == 2


def test_the_picked_value_is_kept_on_a_short_screen(monkeypatch, fruit):
    fruit.col = 1
    # Ten lines of room: the eight statistics, a blank and the heading fill
    # them, so stepping down scrolls the values into view; stepping back up
    # leaves them there.
    window = _run(monkeypatch, fruit, [ord("i"), ord("j"), ord("j"), ord("k"),
                                       ord("q"), ord("q")], rows=12)
    assert fruit.summary_pick == 1 and fruit.summary_top == 3
    # The last frame drawn before q: the picked value on screen, as the
    # cursor, on the row its line scrolled to.
    picked = [a for a in window.drawn
              if len(a) > 3 and a[3] == theme.attr("panelcursor")
              and "1  10.5" in a[2]]
    assert picked and picked[-1][0] == 12 - 3 + 1 - 1


def test_the_summary_scrolls(monkeypatch, fruit):
    fruit.col = 1                                    # 25 lines, 10 on screen
    _run(monkeypatch, fruit, [ord("i"), curses.KEY_NPAGE, ord("q"), ord("q")])
    assert fruit.summary_top == 10
    _run(monkeypatch, fruit, [ord("i"), ord(" "), ord(" "), ord("q"),
                              ord("q")])
    assert fruit.summary_top == 15                   # clamped at the end
    _run(monkeypatch, fruit, [ord("i"), curses.KEY_END, curses.KEY_PPAGE,
                              ord("q"), ord("q")])
    assert fruit.summary_top == 5
    _run(monkeypatch, fruit, [ord("i"), curses.KEY_END, curses.KEY_HOME,
                              ord("q"), ord("q")])
    assert fruit.summary_top == 0


def test_left_and_right_summarise_the_neighbouring_columns(monkeypatch, fs, tmp_path):
    view = make(fs, tmp_path, "a\tb\tc\n1\tx\t3\n")
    view.hidden = {1}
    _run(monkeypatch, view, [ord("i"), curses.KEY_RIGHT, ord("q"), ord("q")])
    assert view.col == 2
    window = _run(monkeypatch, view, [ord("i"), ord("l"), ord("q"), ord("q")])
    assert view.col == 2 and "this is the last column shown" in window.text
    _run(monkeypatch, view, [ord("i"), curses.KEY_LEFT, ord("q"), ord("q")])
    assert view.col == 0
    window = _run(monkeypatch, view, [ord("i"), ord("h"), ord("q"), ord("q")])
    assert "this is the first column shown" in window.text


@pytest.mark.parametrize("key", [ord("q"), ord("Q"), 27, curses.KEY_F3,
                                 curses.KEY_F10, ord("i")])
def test_every_way_out_of_the_summary_returns_to_the_table(monkeypatch, fruit, key):
    # The summary closes, and the next key quits the table itself.
    _run(monkeypatch, fruit, [ord("i"), key, ord("q")])
    assert fruit.summary is None and fruit.view.filters == []


def test_a_key_the_summary_does_not_know_is_ignored(monkeypatch, fruit):
    _run(monkeypatch, fruit, [ord("i"), ord("z"), curses.KEY_RESIZE, ord("q")])
    assert fruit.view.filters == []


def test_there_is_no_summary_of_no_rows(monkeypatch, fs, tmp_path, fruit):
    fruit.add_filter("nothing-matches-this")
    window = _run(monkeypatch, fruit, [ord("i"), ord("q")])
    assert fruit.summary is None and "no rows to summarise" in window.text
    empty = make(fs, tmp_path, "", "empty.tsv")
    empty.open_summary()
    assert empty.summary is None


@pytest.mark.parametrize("rows, cols", [(1, 20), (2, 20), (3, 4), (4, 1),
                                        (30, 10)])
def test_the_summary_survives_a_tiny_screen(fruit, rows, cols):
    fruit.col = 1
    fruit.open_summary()
    _draw(fruit, rows=rows, cols=cols)


def test_the_footer_names_the_new_keys(fruit):
    text = _text(fruit, cols=200)
    assert "[i] summary  [F]reeze  -/+ hide/show" in text
