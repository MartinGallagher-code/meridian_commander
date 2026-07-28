"""The full-screen spreadsheet grid: layout, movement, search and drawing."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import dialogs
from meridian_commander.sheetview import (
    WIDTH_CHOICES,
    SheetView,
    looks_numeric,
)
from meridian_commander.browsers import viewer_for
from meridian_commander.viewer import Viewer

from support import (
    _KeyScript,
    script_newwin,
    with_curses_screen,
    write,
    write_xlsx,
)

SAMPLE = {
    "Sales": [
        ["Region", "Revenue", "Opened"],
        ["North", "1234.5", "2023-03-15"],
        ["South", "-98", "2024-12-31"],
        ["East", "0", "2020-01-01"],
    ],
    "Notes": [["only", "a note"]],
}


@pytest.fixture
def book(fs, tmp_path):
    return SheetView(fs, write_xlsx(str(tmp_path / "book.xlsx"), SAMPLE))


def wide(fs, tmp_path, rows=40, cols=12):
    """A sheet bigger than any test screen, each cell naming its position."""
    grid = [[f"r{r}c{c}" for c in range(cols)] for r in range(rows)]
    return SheetView(fs, write_xlsx(str(tmp_path / "wide.xlsx"), {"S": grid}))


def _run(monkeypatch, view, keys, rows=12, cols=60):
    captured = script_newwin(monkeypatch, keys)
    with_curses_screen(rows, cols, view.run)
    return captured["window"]


# -- choosing the browser ------------------------------------------------------
def test_a_workbook_opens_in_the_grid_and_anything_else_in_the_viewer(fs, tmp_path):
    book_path = write_xlsx(str(tmp_path / "book.xlsx"), {"S": [["a"]]})
    write(str(tmp_path / "notes.txt"), "plain text")
    assert isinstance(viewer_for(fs, book_path), SheetView)
    assert isinstance(viewer_for(fs, str(tmp_path / "notes.txt")), Viewer)


def test_the_choice_is_made_on_the_name_not_the_content(fs, tmp_path):
    # A workbook named .txt stays in the text viewer: the pane knows the name
    # before the file, and a remote pane would have to download it to know more.
    path = write_xlsx(str(tmp_path / "book.txt"), {"S": [["a"]]})
    assert isinstance(viewer_for(fs, path), Viewer)


# -- loading -------------------------------------------------------------------
def test_the_workbook_is_read_and_the_first_sheet_shown(book):
    assert book.name == "book.xlsx"
    assert book.error is None
    assert [s.name for s in book.workbook.sheets] == ["Sales", "Notes"]
    assert book.sheet.name == "Sales"
    assert book.current_cell() == "Region"
    assert book.reference() == "A1"


def test_a_file_that_cannot_be_read_reports_itself(fs, tmp_path):
    write(str(tmp_path / "fake.xlsx"), "not a spreadsheet at all")
    view = SheetView(fs, str(tmp_path / "fake.xlsx"))
    assert "not a readable" in view.error
    # An empty stand-in sheet keeps every other path working on the error
    # screen, rather than needing a second set of guards.
    assert view.sheet.rows == [] and view.current_cell() == ""


def test_a_file_that_is_not_there_reports_itself(fs, tmp_path):
    view = SheetView(fs, str(tmp_path / "missing.xlsx"))
    assert view.error and view.sheet.rows == []


# -- cells ---------------------------------------------------------------------
def test_reading_cells_inside_and_outside_the_sheet(book):
    assert book.cell(1, 0) == "North"
    assert book.cell(0, 2) == "Opened"
    assert book.cell(99, 0) == ""
    assert book.cell(0, 99) == ""
    assert book.cell(-1, 0) == ""


@pytest.mark.parametrize("row, col, ref", [
    (0, 0, "A1"), (3, 2, "C4"), (0, 26, "AA1"),
])
def test_the_cursor_reference(book, row, col, ref):
    book.row, book.col = row, col
    assert book.reference() == ref


@pytest.mark.parametrize("text, numeric", [
    ("1", True), ("-98", True), ("1234.5", True), (" 42 ", True),
    ("+3", True), (".5", True), ("1e6", True),
    ("North", False), ("", False), ("   ", False),
    ("nan", False), ("inf", False), ("-", False), ("12a", False),
])
def test_what_counts_as_a_number_for_alignment(text, numeric):
    assert looks_numeric(text) is numeric


# -- layout --------------------------------------------------------------------
def test_column_widths_follow_the_content(book):
    widths = book._widths()
    assert widths[0] == len("Region")
    assert widths[2] == len("2023-03-15")


def test_a_column_of_short_values_keeps_a_readable_minimum(fs, tmp_path):
    view = SheetView(fs, write_xlsx(str(tmp_path / "b.xlsx"), {"S": [["a", "b"]]}))
    assert view._widths() == [3, 3]


def test_a_very_wide_column_is_clamped(fs, tmp_path):
    view = SheetView(fs, write_xlsx(str(tmp_path / "b.xlsx"),
                                    {"S": [["x" * 200]]}))
    assert view._widths() == [view.max_width]


def test_widths_are_measured_over_the_top_of_the_sheet_only(fs, tmp_path, monkeypatch):
    import meridian_commander.sheetview as mod
    monkeypatch.setattr(mod, "WIDTH_SAMPLE_ROWS", 2)
    view = SheetView(fs, write_xlsx(str(tmp_path / "b.xlsx"),
                                    {"S": [["ab"], ["cd"], ["a much longer one"]]}))
    # The long value is past the sample, so it does not widen the column and
    # the layout cannot shift under the reader while scrolling.
    assert view._widths() == [3]


def test_widths_are_cached_per_sheet_and_per_width_setting(book):
    first = book._widths()
    assert book._widths() is first
    book.cycle_width()
    assert book._widths() is not first


def test_the_row_number_gutter_grows_with_the_row_count(fs, tmp_path):
    small = SheetView(fs, write_xlsx(str(tmp_path / "s.xlsx"), {"S": [["a"]]}))
    assert small.gutter() == 4
    big = SheetView(fs, write_xlsx(str(tmp_path / "b.xlsx"),
                                   {"S": [["a"]] * 1000}))
    assert big.gutter() == 5


def test_an_empty_sheet_still_has_a_gutter(fs, tmp_path):
    view = SheetView(fs, write_xlsx(str(tmp_path / "e.xlsx"), {"S": []}))
    assert view.gutter() == 4
    assert view.layout(40) == []


def test_the_layout_places_columns_side_by_side(book):
    placed = book.layout(60)
    assert [c for c, _x, _w in placed] == [0, 1, 2]
    xs = [x for _c, x, _w in placed]
    assert xs == [0, 7, 15]        # widths 6 and 7, one space between


def test_the_layout_stops_at_the_edge_of_the_screen(book):
    assert [c for c, _x, _w in book.layout(14)] == [0, 1]
    assert [c for c, _x, _w in book.layout(6)] == [0]


def test_a_column_too_wide_for_the_screen_is_shown_anyway(book):
    # Nothing fits whole, so the first column is drawn cut off rather than
    # leaving a blank screen.
    assert book.layout(4) == [(0, 0, 4)]


def test_no_room_at_all_places_nothing(book):
    assert book.layout(0) == []


def test_the_width_key_cycles_through_the_choices(book):
    assert book.max_width == WIDTH_CHOICES[1]
    book.cycle_width()
    assert book.max_width == WIDTH_CHOICES[2]
    book.cycle_width()
    assert book.max_width == WIDTH_CHOICES[0]


# -- movement ------------------------------------------------------------------
def test_moving_around_with_the_arrows(monkeypatch, book):
    _run(monkeypatch, book, [curses.KEY_DOWN, curses.KEY_RIGHT, ord("q")])
    assert (book.row, book.col) == (1, 1)
    _run(monkeypatch, book, [ord("j"), ord("l"), ord("q")])
    assert (book.row, book.col) == (2, 2)
    _run(monkeypatch, book, [curses.KEY_UP, curses.KEY_LEFT, ord("q")])
    assert (book.row, book.col) == (1, 1)
    _run(monkeypatch, book, [ord("k"), ord("h"), ord("q")])
    assert (book.row, book.col) == (0, 0)


def test_the_cursor_never_leaves_the_sheet(monkeypatch, book):
    _run(monkeypatch, book, [curses.KEY_UP, curses.KEY_LEFT, ord("q")])
    assert (book.row, book.col) == (0, 0)
    _run(monkeypatch, book, [ord("G"), curses.KEY_END, ord("q")])
    assert (book.row, book.col) == (3, 2)
    _run(monkeypatch, book, [curses.KEY_DOWN, curses.KEY_RIGHT, ord("q")])
    assert (book.row, book.col) == (3, 2)


def test_paging_and_jumping_to_the_ends(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    _run(monkeypatch, view, [curses.KEY_NPAGE, ord("q")], rows=12)
    assert view.row == 9                       # rows - 3 for title, header, hint
    _run(monkeypatch, view, [curses.KEY_PPAGE, ord("q")], rows=12)
    assert view.row == 0
    _run(monkeypatch, view, [ord("G"), ord("q")])
    assert view.row == 39
    _run(monkeypatch, view, [ord("g"), ord("q")])
    assert view.row == 0
    _run(monkeypatch, view, [curses.KEY_END, ord("q")])
    assert view.col == 11
    _run(monkeypatch, view, [curses.KEY_HOME, ord("q")])
    assert view.col == 0


def test_the_view_scrolls_to_keep_the_cursor_visible(monkeypatch, fs, tmp_path):
    view = wide(fs, tmp_path)
    _run(monkeypatch, view, [ord("G"), ord("q")], rows=12)
    assert view.top == 39 - 9 + 1
    assert view.left > 0 or view.col == 0
    _run(monkeypatch, view, [ord("g"), ord("q")], rows=12)
    assert view.top == 0
    _run(monkeypatch, view, [curses.KEY_END, ord("q")], rows=12, cols=40)
    assert view.left > 0
    _run(monkeypatch, view, [curses.KEY_HOME, ord("q")], rows=12, cols=40)
    assert view.left == 0


def test_movement_on_an_empty_sheet_stays_put(monkeypatch, fs, tmp_path):
    view = SheetView(fs, write_xlsx(str(tmp_path / "e.xlsx"), {"S": []}))
    _run(monkeypatch, view, [curses.KEY_DOWN, curses.KEY_RIGHT, ord("G"),
                             curses.KEY_END, ord("q")])
    assert (view.row, view.col) == (0, 0)


# -- sheets --------------------------------------------------------------------
def test_switching_sheets_with_tab(monkeypatch, book):
    _run(monkeypatch, book, [9, ord("q")])
    assert book.sheet.name == "Notes"
    _run(monkeypatch, book, [curses.KEY_BTAB, ord("q")])
    assert book.sheet.name == "Sales"
    _run(monkeypatch, book, [ord("]"), ord("q")])
    assert book.sheet.name == "Notes"
    _run(monkeypatch, book, [ord("["), ord("q")])
    assert book.sheet.name == "Sales"


def test_switching_sheets_wraps_around(book):
    book.switch_sheet(-1)
    assert book.sheet.name == "Notes"
    book.switch_sheet(1)
    assert book.sheet.name == "Sales"


def test_switching_sheets_starts_the_new_one_at_the_top(book):
    book.row, book.col, book.top, book.left = 3, 2, 1, 1
    book.switch_sheet(1)
    assert (book.row, book.col, book.top, book.left) == (0, 0, 0, 0)


def test_a_workbook_with_one_sheet_says_so(fs, tmp_path):
    view = SheetView(fs, write_xlsx(str(tmp_path / "one.xlsx"), {"S": [["a"]]}))
    view.switch_sheet(1)
    assert "only one sheet" in view.notice
    assert view.sheet_index == 0


# -- search --------------------------------------------------------------------
def test_finding_a_value_moves_the_cursor(book):
    book.set_search("south")
    assert book.find_next(1) is True
    assert (book.row, book.col) == (2, 0)


def test_search_is_case_sensitive_when_the_pattern_is_not_lowercase(book):
    book.set_search("SOUTH")
    assert book.find_next(1) is False
    book.set_search("South")
    assert book.find_next(1) is True


def test_searching_wraps_around_and_goes_backwards(book):
    book.set_search("region")
    book.row, book.col = 3, 2
    assert book.find_next(1) is True
    assert (book.row, book.col) == (0, 0)
    book.set_search("east")
    assert book.find_next(-1) is True
    assert (book.row, book.col) == (3, 0)


def test_repeating_a_search_moves_on_rather_than_staying_put(book):
    book.set_search("0")
    first = (book.find_next(1), book.row, book.col)
    second = (book.find_next(1), book.row, book.col)
    assert first[0] and second[0] and first[1:] != second[1:]


def test_searching_for_nothing_finds_nothing(book):
    assert book.find_next(1) is False
    book.set_search("nowhere")
    assert book.find_next(1) is False


def test_searching_an_empty_sheet_finds_nothing(fs, tmp_path):
    view = SheetView(fs, write_xlsx(str(tmp_path / "e.xlsx"), {"S": []}))
    view.set_search("anything")
    assert view.find_next(1) is False


def test_searching_a_sheet_of_only_blank_rows(fs, tmp_path):
    # Row gaps leave rows with no cells at all, so the column count is zero
    # while the row count is not.
    from support import sheet_xml, xlsx_bytes, xlsx_parts
    path = str(tmp_path / "gaps.xlsx")
    with open(path, "wb") as f:
        f.write(xlsx_bytes(xlsx_parts({"S": sheet_xml('<row r="3"/>')})))
    view = SheetView(fs, path)
    view.set_search("x")
    assert view.find_next(1) is False


def test_the_search_prompt_drives_the_search(monkeypatch, book):
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "south")
    _run(monkeypatch, book, [ord("/"), ord("q")])
    assert (book.row, book.col) == (2, 0)


def test_a_search_prompt_that_finds_nothing_says_so(monkeypatch, book):
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "zebra")
    # The notice is cleared by the next keystroke, so what matters is that it
    # reached the screen in between.
    window = _run(monkeypatch, book, [ord("/"), ord("q")])
    assert "not found" in window.text


def test_a_cancelled_search_prompt_changes_nothing(monkeypatch, book):
    book.set_search("north")
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: None)
    _run(monkeypatch, book, [curses.KEY_F7, ord("q")])
    assert book.search == "north" and (book.row, book.col) == (0, 0)


def test_an_empty_search_pattern_clears_the_search(monkeypatch, book):
    book.set_search("north")
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "")
    _run(monkeypatch, book, [ord("/"), ord("q")])
    assert book.search == ""


def test_the_next_and_previous_keys(monkeypatch, book):
    book.set_search("north")
    _run(monkeypatch, book, [ord("n"), ord("q")])
    assert (book.row, book.col) == (1, 0)
    book.set_search("region")
    _run(monkeypatch, book, [ord("N"), ord("q")])
    assert (book.row, book.col) == (0, 0)
    book.set_search("east")
    _run(monkeypatch, book, [ord("p"), ord("q")])
    assert (book.row, book.col) == (3, 0)


def test_next_and_previous_report_when_there_is_no_match(monkeypatch, book):
    book.set_search("zebra")
    assert "no match" in _run(monkeypatch, book, [ord("n"), ord("q")]).text
    assert "no match" in _run(monkeypatch, book, [ord("N"), ord("q")]).text


def test_next_without_a_pattern_does_nothing(monkeypatch, book):
    window = _run(monkeypatch, book, [ord("n"), ord("N"), ord("q")])
    assert "no match" not in window.text
    assert (book.row, book.col) == (0, 0)


# -- drawing, on a real screen -------------------------------------------------
def _draw(view, rows=12, cols=60):
    """Draw once on a real curses window and return what was written.

    The window is genuine, so curses bounds-checks every write and a drawing
    guard that is missing shows up as an error rather than as a silent gap.
    """
    def run(stdscr):
        window = _KeyScript(curses.newwin(rows, cols, 0, 0), [])
        view.draw(window)
        return window.drawn
    return with_curses_screen(rows, cols, run)


def _texts(drawn):
    return [a[2] for a in drawn if len(a) > 2 and isinstance(a[2], str)]


def test_the_title_names_the_file_and_the_sheet(book):
    text = " ".join(_texts(_draw(book)))
    assert "book.xlsx" in text and "Sales (1/2)" in text


def test_the_column_headers_and_row_numbers_are_drawn(book):
    text = " ".join(_texts(_draw(book)))
    assert "A  " in text and "B" in text
    assert "  1 " in text and "  4 " in text


def test_the_values_are_drawn(book):
    text = " ".join(_texts(_draw(book)))
    assert "Region" in text and "North" in text and "2023-03-15" in text


def test_numbers_are_right_aligned_and_text_is_not(book):
    drawn = _texts(_draw(book))
    assert " 1234.5" in drawn        # padded on the left
    assert "North " in drawn         # padded on the right


def test_the_footer_shows_the_cursor_cell_in_full(fs, tmp_path):
    view = SheetView(fs, write_xlsx(str(tmp_path / "b.xlsx"),
                                    {"S": [["a value far too wide to fit"]]}))
    text = " ".join(_texts(_draw(view, cols=60)))
    assert "A1" in text and "a value far too wide to fit" in text


def test_the_footer_shows_the_search_and_any_notice(book):
    book.set_search("north")
    assert "/north" in " ".join(_texts(_draw(book)))
    book.notice = " a passing remark "
    assert "a passing remark" in " ".join(_texts(_draw(book)))


def test_a_truncated_sheet_says_so_in_the_title(fs, tmp_path, monkeypatch):
    from meridian_commander import xlsx
    monkeypatch.setattr(xlsx, "MAX_ROWS", 2)
    view = SheetView(fs, write_xlsx(str(tmp_path / "big.xlsx"),
                                    {"S": [["a"], ["b"], ["c"]]}))
    assert "[truncated]" in " ".join(_texts(_draw(view)))


def test_an_empty_sheet_says_so(fs, tmp_path):
    view = SheetView(fs, write_xlsx(str(tmp_path / "e.xlsx"), {"S": []}))
    assert "empty" in " ".join(_texts(_draw(view)))


def test_the_error_screen_replaces_the_grid(fs, tmp_path):
    write(str(tmp_path / "fake.xlsx"), "not a spreadsheet")
    view = SheetView(fs, str(tmp_path / "fake.xlsx"))
    text = " ".join(_texts(_draw(view)))
    assert "Cannot read" in text and "[q]uit" in text
    assert "A" not in text.split("Cannot read")[0].replace("Sheet:", "")


@pytest.mark.parametrize("rows, cols", [
    (1, 20),    # no room for the column headers, and the title fills the
                # only row -- which includes the cell curses always refuses
    (2, 20),    # room for headers but not for a body
    (3, 20),    # room for a body row but not much
    (4, 1), (4, 2), (4, 4), (4, 8),   # narrower than the row-number gutter
])
def test_drawing_survives_a_screen_with_no_room_on_it(book, rows, cols):
    # The window is real, so curses refuses these writes for real; what is
    # under test is that every refusal is caught rather than reaching the user.
    _draw(book, rows=rows, cols=cols)


@pytest.mark.parametrize("rows", [1, 2, 3])
def test_the_empty_sheet_message_survives_a_screen_with_no_room(fs, tmp_path, rows):
    view = SheetView(fs, write_xlsx(str(tmp_path / "e.xlsx"), {"S": []}))
    _draw(view, rows=rows, cols=20)


@pytest.mark.parametrize("rows", [1, 2, 3])
def test_the_error_message_survives_a_screen_with_no_room(fs, tmp_path, rows):
    write(str(tmp_path / "fake.xlsx"), "not a spreadsheet")
    _draw(SheetView(fs, str(tmp_path / "fake.xlsx")), rows=rows, cols=20)


def test_drawing_survives_a_cursor_past_the_visible_columns(fs, tmp_path):
    view = wide(fs, tmp_path)
    view.col = 11
    _draw(view, rows=12, cols=30)
    assert view.left > 0


def test_the_grid_closes_on_every_quit_key(monkeypatch, book):
    for key in (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10):
        _run(monkeypatch, book, [key])


def test_an_unhandled_key_is_simply_ignored(monkeypatch, book):
    _run(monkeypatch, book, [ord("z"), curses.KEY_RESIZE, ord("q")])
    assert (book.row, book.col) == (0, 0)


def test_the_width_key_redraws_at_the_new_width(monkeypatch, fs, tmp_path):
    view = SheetView(fs, write_xlsx(str(tmp_path / "b.xlsx"),
                                    {"S": [["x" * 100]]}))
    _run(monkeypatch, view, [ord("w"), ord("q")])
    assert view._widths() == [WIDTH_CHOICES[2]]
    _run(monkeypatch, view, [ord("W"), ord("q")])
    assert view._widths() == [WIDTH_CHOICES[0]]
