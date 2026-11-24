# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The join screen (K): File A and File B above, the rows they make joined
below -- opening it, drawing it, its keys, and saving the join (MC-033).

Fixtures stay neutral -- people, orders and fruit -- rather than copies of
real results files.
"""

from __future__ import annotations

import curses
import os

import pytest

from meridian_commander import joinview, table, theme
from meridian_commander.archive import ArchiveFileSystem
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.joinview import (
    HELP,
    MIN_HEIGHT,
    MIN_WIDTH,
    JoinScreen,
)

from support import (
    TAKE_DEFAULT,
    _FakeRemoteBackend,
    _ScriptedDialogs,
    read,
    script_newwin,
    with_curses_screen,
    write,
    write_zip,
)

PEOPLE = "id\tname\n1\tann\n2\tbob\n2\tbo\n3\tcy\n"
ORDERS = "uid,item\n2,pen\n2,ink\n1,cup\n9,hat\n"
DOWN, TAB, BTAB = curses.KEY_DOWN, 9, curses.KEY_BTAB


def make(fs, tmp_path, a=PEOPLE, b=ORDERS, a_name="people.tsv",
         b_name="orders.csv", **kwargs) -> JoinScreen:
    write(str(tmp_path / a_name), a)
    write(str(tmp_path / b_name), b)
    return JoinScreen(fs, str(tmp_path / a_name), fs, str(tmp_path / b_name),
                      **kwargs)


def press(screen, *keys, stdscr=None, size=(24, 80)):
    """Hand keys to the screen as its loop would; the last answer."""
    result = True
    for key in keys:
        result = screen.handle_key(key, stdscr, *size)
    return result


def joined(fs, tmp_path, **kwargs) -> JoinScreen:
    """People joined to orders on their first columns."""
    screen = make(fs, tmp_path, **kwargs)
    press(screen, ord("m"), TAB, ord("m"))
    return screen


def out_rows(screen):
    out = screen.out
    return [" ".join(out.records[index]) for index in out.order]


def picture(screen, rows=24, cols=80):
    """Draw once on a real screen: each region drawn, as its place, its size
    and its lines of text; the whole screen's lines under ``"frame"``."""
    def run(stdscr):
        frame = curses.newwin(rows, cols, 0, 0)
        screen.draw(frame)
        shot = {"frame": [frame.instr(y, 0, cols).decode().rstrip()
                          for y in range(rows)]}
        for index, (geometry, win) in screen._windows.items():
            height, width = win.getmaxyx()
            if geometry != (height, width) + win.getbegyx():
                continue            # made for another layout, not drawn now
            shot[index] = (win.getbegyx(), (height, width),
                           [win.instr(y, 0, width).decode().rstrip()
                            for y in range(height)])
            shot[f"{index}.title"] = win.inch(0, 1) & ~curses.A_CHARTEXT
            shot[f"{index}.head"] = [win.inch(1, x) & ~curses.A_CHARTEXT
                                     for x in range(width)]
        return shot
    return with_curses_screen(rows, cols, run)


def regions(shot) -> set:
    """Which regions were drawn: 0 File A, 1 File B, 2 the output."""
    return {key for key in shot if isinstance(key, int)}


def text(shot, index) -> str:
    return "\n".join(shot[index][2])


# -- the screen ---------------------------------------------------------------------
def test_file_a_is_top_left_file_b_top_right_and_the_output_below(fs,
                                                                  tmp_path):
    shot = picture(make(fs, tmp_path))
    assert shot[0][:2] == ((0, 0), (12, 40))
    assert shot[1][:2] == ((0, 40), (12, 40))
    assert shot[2][:2] == ((12, 0), (12, 80))
    assert shot[0][2][0].startswith(" File A: people.tsv")
    assert shot[1][2][0].startswith(" File B: orders.csv")
    assert shot[2][2][0].startswith(" Joined: nothing yet")


def test_a_larger_screen_is_shared_the_same_way(fs, tmp_path):
    shot = picture(joined(fs, tmp_path), rows=31, cols=121)
    # The odd row goes to the output, the odd column to File B.
    assert shot[0][:2] == ((0, 0), (15, 60))
    assert shot[1][:2] == ((0, 60), (15, 61))
    assert shot[2][:2] == ((15, 0), (16, 121))
    assert "A.id = B.uid" in shot[2][2][0]


def test_until_something_is_marked_the_output_says_what_to_do(fs, tmp_path):
    screen = make(fs, tmp_path)
    assert "Mark the column to join on in File A and in File B" \
        in text(picture(screen), 2)
    press(screen, ord("m"))
    assert "Now mark the column to join on in File B: Tab to it, then m" \
        in text(picture(screen), 2)
    press(screen, ord("m"), TAB, ord("m"))      # unmark A, mark B
    assert "Now mark the column to join on in File A" in text(picture(screen),
                                                             2)


def test_different_numbers_of_join_columns_say_so(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, BTAB, curses.KEY_RIGHT, ord("m"))     # File A's name too
    shot = picture(screen)
    assert "File A has 2 join columns marked and File B 1" in text(shot, 2)
    assert screen.joined is None and screen.out.records == []


def test_the_join_columns_are_marked_in_both_headers(fs, tmp_path):
    screen = joined(fs, tmp_path)
    shot = picture(screen)
    assert shot[0][2][1].split()[0] == "1:id"
    assert shot[1][2][1].split()[0] == "1:uid"
    # Marked like a tagged file: under the cursor in File B, which has the
    # keyboard, and plain tagged in File A after the cursor moves on.
    press(screen, BTAB, curses.KEY_RIGHT)
    shot = picture(screen)
    gutter = screen.a.gutter()
    assert shot["0.head"][gutter] == theme.attr("paneltag")
    assert shot["1.head"][screen.b.gutter()] == theme.attr("panelcursortag")


def test_the_mark_makes_room_for_itself(fs, tmp_path):
    # "uid" is three letters; marked "1:uid" is not cut to "1:u~".
    screen = make(fs, tmp_path)
    widths = screen.b._measure()[0]
    press(screen, TAB, ord("m"))
    assert screen.b._measure()[0][0] == widths[0] + 2


def test_the_output_says_which_column_is_matched_with_which(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, BTAB, curses.KEY_RIGHT, ord("m"), TAB, curses.KEY_RIGHT,
          ord("m"))
    title = picture(screen, cols=160)[2][2][0]
    assert title.startswith(" Joined on A.id = B.uid, A.name = B.item -- ")


def test_the_region_with_the_keyboard_is_plain_to_see(fs, tmp_path):
    screen = make(fs, tmp_path)
    shot = picture(screen)
    assert shot["0.title"] == theme.attr("keybar")
    assert shot["1.title"] == shot["2.title"] == theme.attr("titlenc")
    press(screen, TAB, TAB)
    shot = picture(screen)
    assert shot["2.title"] == theme.attr("keybar")
    assert shot["0.title"] == theme.attr("titlenc")


def test_a_screen_too_small_for_three_says_so(fs, tmp_path):
    screen = make(fs, tmp_path)
    shot = picture(screen, rows=MIN_HEIGHT - 1, cols=80)
    assert shot["frame"][0].startswith("Too small for File A, File B and the "
                                       "output: it needs 40x10.")
    assert "z shows the region with the keyboard on its own" \
        in shot["frame"][1]
    assert regions(shot) == set()
    # z still shows one region, on all of what there is.
    press(screen, ord("z"))
    shot = picture(screen, rows=8, cols=MIN_WIDTH - 1)
    assert shot[0][:2] == ((0, 0), (8, 39))


def test_a_screen_of_one_line_says_as_much_as_fits(fs, tmp_path):
    shot = picture(make(fs, tmp_path), rows=1, cols=30)
    assert shot["frame"][0] == "Too small for File A, File B ~"


def test_three_regions_fit_at_the_smallest_size(fs, tmp_path):
    shot = picture(joined(fs, tmp_path), rows=MIN_HEIGHT, cols=MIN_WIDTH)
    assert [shot[index][1] for index in (0, 1, 2)] == [(5, 20), (5, 20),
                                                       (5, 40)]


def test_z_zooms_the_active_region_and_back(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, ord("z"))
    shot = picture(screen)
    assert regions(shot) == {1}
    assert shot[1][:2] == ((0, 0), (24, 80))
    assert "File B on the whole screen; z again shows all three" \
        in shot[1][2][-1]
    press(screen, TAB)                       # Tab shows the next one zoomed
    assert picture(screen)[2][:2] == ((0, 0), (24, 80))
    press(screen, ord("z"))
    shot = picture(screen)
    assert regions(shot) == {0, 1, 2}
    assert "whole screen" not in shot[2][2][-1]


# -- keys ---------------------------------------------------------------------------
def test_tab_and_shift_tab_move_between_the_three(fs, tmp_path):
    screen = make(fs, tmp_path)
    seen = []
    for key in (TAB, TAB, TAB, BTAB, BTAB):
        press(screen, key)
        seen.append(screen.focus)
    assert seen == [1, 2, 0, 2, 1]


def test_marking_joins_and_unmarking_takes_the_join_away(fs, tmp_path):
    screen = joined(fs, tmp_path)
    assert out_rows(screen) == ["1 ann 1 cup", "2 bob 2 pen", "2 bob 2 ink",
                                "2 bo 2 pen", "2 bo 2 ink"]
    assert screen.b.notice == " uid marked: join column 1 "
    press(screen, ord("m"))
    assert screen.b.notice == " uid unmarked "
    assert screen.joined is None and "Now mark" in screen.out.instruction


def test_m_in_the_output_says_where_columns_are_marked(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, TAB, ord("m"))
    assert screen.out.notice == (" the join columns are marked in File A and "
                                 "File B: Tab to one, then m ")


def test_a_file_that_cannot_be_read_has_no_column_to_mark(fs, tmp_path):
    write(str(tmp_path / "b.tsv"), "id\n1\n")
    screen = JoinScreen(fs, str(tmp_path / "gone.tsv"), fs,
                        str(tmp_path / "b.tsv"))
    press(screen, ord("m"))
    assert screen.a.notice == " no column to mark " and screen.a.marks == []
    assert screen.out.instruction == ("File A cannot be read, so there is "
                                      "nothing to join.")
    assert "Cannot read" in text(picture(screen), 0)


def test_o_steps_through_which_rows_are_kept(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, ord("o"))
    assert screen.b.notice == " rows kept: matching rows, and every row of A "
    assert out_rows(screen)[-1] == "3 cy  "
    press(screen, ord("o"))
    assert out_rows(screen)[-1] == "  9 hat"
    assert "matching rows, and every row of A and B" \
        in picture(screen, cols=200)[2][2][0]
    press(screen, ord("o"))
    assert screen.keep == "inner" and len(out_rows(screen)) == 5


def test_e_steps_through_how_strict_a_match_is(fs, tmp_path):
    screen = make(fs, tmp_path, a="id\tname\n7\tann\n",
                  b="uid,item\n007,pen\n")
    press(screen, ord("m"), TAB, ord("m"))
    assert out_rows(screen) == []
    assert "(no rows match: no row of A has the same values" \
        in text(picture(screen), 2)
    press(screen, ord("e"))
    assert screen.b.notice == " match: any case, spaces trimmed "
    press(screen, ord("e"))
    assert out_rows(screen) == ["7 ann 007 pen"]
    press(screen, ord("e"))
    assert screen.rule == "exact"


def test_f1_shows_the_screens_keys(fs, tmp_path, monkeypatch):
    seen = _ScriptedDialogs(monkeypatch)
    monkeypatch.setattr(curses, "curs_set", lambda visible: None)
    screen = make(fs, tmp_path)
    for key in (curses.KEY_F1, ord("?")):
        assert press(screen, key) is True
    assert seen.messages == [("Join files", HELP, False)] * 2
    # It fits a screen 80 columns wide, frame and all.
    assert max(len(line) for line in HELP.split("\n")) + 6 <= 80


def test_a_resize_redraws_and_changes_nothing_else(fs, tmp_path):
    screen = joined(fs, tmp_path)
    rows = out_rows(screen)
    assert press(screen, curses.KEY_RESIZE) is True
    assert out_rows(screen) == rows and screen.focus == 1


def test_q_closes_the_screen_but_first_closes_a_record(fs, tmp_path):
    screen = make(fs, tmp_path)
    assert press(screen, 10) is True and screen.a.record     # Enter: record
    assert press(screen, ord("q")) is True and not screen.a.record
    assert press(screen, ord("q")) is False


def test_a_regions_keys_page_by_its_own_height(fs, tmp_path):
    rows = "".join(f"{n}\tp{n}\n" for n in range(40))
    screen = make(fs, tmp_path, a="id\tname\n" + rows)
    press(screen, curses.KEY_NPAGE)             # 12 rows less three lines
    assert screen.a.row == 9
    press(screen, ord("z"), curses.KEY_NPAGE)    # zoomed: 24 less three
    assert screen.a.row == 30
    press(screen, ord("z"), ord("g"))
    # Too small for three and not zoomed: paged by the whole screen.
    press(screen, curses.KEY_NPAGE, size=(8, 80))
    assert screen.a.row == 5


# -- the output follows what the files show --------------------------------------------
def test_a_filter_in_file_a_narrows_the_join(fs, tmp_path):
    screen = joined(fs, tmp_path)
    # File A: down to bob, across to the name, keep the rows holding it.
    press(screen, BTAB, DOWN, curses.KEY_RIGHT, ord("="))
    assert out_rows(screen) == ["2 bob 2 pen", "2 bob 2 ink"]
    press(screen, curses.KEY_BACKSPACE)
    assert len(out_rows(screen)) == 5


def test_the_output_follows_file_as_order(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, BTAB, curses.KEY_RIGHT, ord("s"))     # sort A by name
    assert [row.split()[1] for row in out_rows(screen)] == [
        "ann", "bo", "bo", "bob", "bob"]


def test_the_header_off_in_a_file_names_its_columns_by_number(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, BTAB, ord("H"))
    assert screen.out.labels() == ["A.1", "A.2", "B.uid", "B.item"]
    # The header line is a row now, and "id" matches no uid.
    assert len(out_rows(screen)) == 5


def test_a_key_that_changes_nothing_does_not_join_again(fs, tmp_path,
                                                        monkeypatch):
    screen = joined(fs, tmp_path)
    calls = []
    real = joinview.tablejoin.join
    monkeypatch.setattr(joinview.tablejoin, "join",
                        lambda *args: calls.append(1) or real(*args))
    press(screen, DOWN, ord("w"), ord("c"), curses.KEY_RIGHT)
    assert calls == []
    press(screen, ord("o"))
    assert calls == [1]


def test_the_outputs_own_filter_and_place_survive_a_new_join(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, TAB, curses.KEY_RIGHT, ord("!"), DOWN)    # hide A.name ann
    assert out_rows(screen) == ["2 bob 2 pen", "2 bob 2 ink", "2 bo 2 pen",
                                "2 bo 2 ink"]
    press(screen, ord("o"))                         # the same columns
    assert out_rows(screen)[-1] == "3 cy  "
    assert [f.text for f in screen.out.view.filters] == ["A.name != ann"]
    assert (screen.out.row, screen.out.col) == (1, 1)
    # Other columns: the filter no longer means what it did, so it goes.
    press(screen, TAB, ord("H"))
    assert screen.out.view.filters == []


def test_a_kept_place_past_the_end_comes_back_inside(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, TAB, ord("G"))                    # the last of five rows
    # File A's cursor followed the output's to bo; from its top, keep bob.
    press(screen, BTAB, BTAB, ord("g"), DOWN, curses.KEY_RIGHT, ord("="))
    press(screen, TAB, TAB)
    assert screen.out.row == 1 and len(screen.out.order) == 2


def test_the_files_cursors_follow_the_outputs(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, TAB)                              # into the output
    assert (screen.a.row, screen.b.row) == (0, 2)   # ann, cup
    press(screen, DOWN, DOWN)                       # bob, ink
    assert (screen.a.row, screen.b.row) == (1, 1)
    # Moved by hand, File A stays put until the output's cursor moves.
    press(screen, BTAB, BTAB, ord("G"), TAB, TAB, ord("w"))
    assert screen.a.row == 3
    press(screen, DOWN)                             # bo, pen
    assert (screen.a.row, screen.b.row) == (2, 0)


def test_an_unmatched_row_moves_only_the_file_it_came_from(fs, tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, ord("o"), ord("o"), TAB, ord("G"))    # B's hat, alone
    assert screen.b.row == 3 and screen.a.row == 0
    press(screen, curses.KEY_UP)                        # A's cy, alone
    assert screen.a.row == 3 and screen.b.row == 3


def test_the_outputs_header_read_as_data_comes_from_neither_file(fs,
                                                                 tmp_path):
    screen = joined(fs, tmp_path)
    press(screen, TAB, DOWN, DOWN)
    before = (screen.a.row, screen.b.row)
    press(screen, ord("H"), ord("g"))         # the header line is row one
    assert screen.out.records[screen.out.order[0]][0] == "A.id"
    assert (screen.a.row, screen.b.row) == before


# -- cut short, partly read, blank keys ------------------------------------------------
def test_a_join_past_the_limit_stops_and_says_so(fs, tmp_path, monkeypatch):
    screen = joined(fs, tmp_path)
    monkeypatch.setattr(table, "MAX_ROWS", 3)
    press(screen, ord("o"))
    assert len(out_rows(screen)) == 3 and screen.joined.truncated
    assert screen.b.notice == " the join stopped at 3 rows: there are more "
    title = picture(screen, cols=200)[2][2][0]
    assert "3 rows [cut short at 3 rows]" in title
    assert screen.out.cut_short() == (
        "Only the first 3 rows of the join can be saved: it stopped there.",
        ", the first 3 rows only: the join stopped there")


def test_a_file_only_partly_read_says_so_in_its_quarter_and_the_output(
        fs, tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_ROWS", 3)
    rows = "".join(f"{n}\tp{n}\n" for n in range(10))
    screen = joined(fs, tmp_path, a="id\tname\n" + rows,
                    b="uid,item\n2,pen\n")
    shot = picture(screen, cols=240)
    assert "[truncated]" in shot[0][2][0]
    assert "[File A only partly read]" in shot[2][2][0]
    assert not screen.joined.truncated
    assert screen.out.cut_short() == (
        "The join is of the rows read: File A goes on past them.",
        ", from the rows read only: File A goes on")


def test_both_files_partly_read_are_both_named(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_ROWS", 2)
    rows = "".join(f"{n}\tp{n}\n" for n in range(9, 0, -1))
    screen = joined(fs, tmp_path, a="id\tname\n" + rows,
                    b="uid\titem\n" + rows, b_name="orders.tsv")
    assert screen.out.partial == ["File A", "File B"]
    assert "File A and File B goes on" in screen.out.cut_short()[1]


def test_blank_keys_are_counted_in_the_title(fs, tmp_path):
    screen = joined(fs, tmp_path, a="id\tname\n\tann\n1\tbob\n",
                    b="uid,item\n1,pen\n")
    assert "blank keys match nothing (1 in A, 0 in B)" \
        in picture(screen, cols=240)[2][2][0]


# -- saving -------------------------------------------------------------------------
def _save(monkeypatch, screen, answers=(TAKE_DEFAULT,)):
    seen = _ScriptedDialogs(monkeypatch, prompt=list(answers))
    press(screen, curses.KEY_F2)
    return seen


def test_f2_in_the_output_saves_the_join_beside_file_a(fs, tmp_path,
                                                       monkeypatch):
    screen = joined(fs, tmp_path)
    press(screen, TAB)
    seen = _save(monkeypatch, screen)
    target = tmp_path / "people-orders.joined.tsv"
    assert seen.prompt_defaults == ["people-orders.joined.tsv"]
    assert read(str(target)) == ("A.id\tA.name\tB.uid\tB.item\n"
                                 "1\tann\t1\tcup\n2\tbob\t2\tpen\n"
                                 "2\tbob\t2\tink\n2\tbo\t2\tpen\n"
                                 "2\tbo\t2\tink\n")
    assert screen.saved == [(fs, str(target))]
    assert screen.out.notice.startswith(" wrote 5 rows")


def test_a_saved_join_cut_short_says_so(fs, tmp_path, monkeypatch):
    screen = joined(fs, tmp_path)
    monkeypatch.setattr(table, "MAX_ROWS", 3)
    press(screen, ord("o"), TAB)
    seen = _save(monkeypatch, screen, ["cut.csv"])
    assert seen.prompt_notes == [["Only the first 3 rows of the join can be "
                                  "saved: it stopped there."]]
    assert read(str(tmp_path / "cut.csv")).count("\n") == 4
    assert screen.out.notice.endswith(", the first 3 rows only: the join "
                                      "stopped there ")


def test_what_is_saved_survives_the_join_being_made_again(fs, tmp_path,
                                                          monkeypatch):
    screen = joined(fs, tmp_path)
    press(screen, TAB)
    _save(monkeypatch, screen)
    press(screen, ord("o"))
    assert len(screen.saved) == 1


def test_f2_in_a_file_saves_that_files_view(fs, tmp_path, monkeypatch):
    screen = joined(fs, tmp_path)
    _save(monkeypatch, screen)                      # File B has the keyboard
    assert os.path.exists(tmp_path / "orders.view.csv")
    assert screen.saved == [(fs, str(tmp_path / "orders.view.csv"))]


def test_a_file_with_no_extension_is_named_whole(fs, tmp_path):
    out = joinview.JoinedView(fs, str(tmp_path / "x"), table.Table([], ","))
    out.files = ("people", "orders.csv")
    assert out.save_base() == ("people-orders", "joined")


# -- different formats and backends ----------------------------------------------------
def test_a_table_in_an_archive_joins_one_on_a_remote_pane(fs, tmp_path):
    archive = write_zip(str(tmp_path / "bundle.zip"),
                        {"data/people.psv": "id|name\n1|ann\n2|bob\n"})
    write(str(tmp_path / "orders.tab"), "uid\titem\n2\tpen\n")
    screen = JoinScreen(ArchiveFileSystem(LocalFileSystem(), archive),
                        "/data/people.psv", _FakeRemoteBackend(),
                        str(tmp_path / "orders.tab"))
    press(screen, ord("m"), TAB, ord("m"))
    assert out_rows(screen) == ["2 bob 2 pen"]


def test_saving_the_join_from_an_archive_goes_to_the_other_pane(
        fs, tmp_path, monkeypatch):
    archive = write_zip(str(tmp_path / "bundle.zip"),
                        {"data/people.tsv": PEOPLE})
    write(str(tmp_path / "orders.csv"), ORDERS)
    (tmp_path / "other").mkdir()
    pane = type("Pane", (), {"fs": fs, "path": str(tmp_path / "other")})
    screen = JoinScreen(ArchiveFileSystem(LocalFileSystem(), archive),
                        "/data/people.tsv", fs, str(tmp_path / "orders.csv"),
                        a_elsewhere=pane)
    press(screen, ord("m"), TAB, ord("m"), TAB)
    seen = _save(monkeypatch, screen)
    target = tmp_path / "other" / "people-orders.joined.tsv"
    assert seen.prompt_defaults == [str(target)]
    assert read(str(target)).startswith("A.id\tA.name\tB.uid\tB.item\n")


# -- the loop -----------------------------------------------------------------------
def test_the_loop_draws_reads_keys_and_ends_on_q(fs, tmp_path, monkeypatch):
    screen = make(fs, tmp_path)
    captured = script_newwin(monkeypatch, [ord("m"), TAB, ord("m"), BTAB,
                                           ord("q")])
    with_curses_screen(24, 80, screen.run)
    assert len(out_rows(screen)) == 5
    # One window for the keys, then one per region.
    frame, *regions = captured["windows"]
    assert [w.getbegyx() for w in regions] == [(0, 0), (0, 40), (12, 0)]
    assert any("Joined on A.id = B.uid" in str(args)
               for args in regions[2].drawn)


class _Shrinking:
    """A screen that reports a smaller size after its first look."""

    def __init__(self, real, sizes):
        self._real = real
        self._sizes = list(sizes)

    def getmaxyx(self):
        return self._sizes.pop(0) if len(self._sizes) > 1 else self._sizes[0]

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_the_screen_follows_a_resized_terminal(fs, tmp_path, monkeypatch):
    screen = make(fs, tmp_path)
    captured = script_newwin(monkeypatch, [curses.KEY_RESIZE, ord("q")])

    def run(stdscr):
        screen.run(_Shrinking(stdscr, [(24, 80), (20, 70)]))
        return captured["windows"][0].getmaxyx()

    assert with_curses_screen(24, 80, run) == (20, 70)
    # The regions were made again for the new size.
    assert screen._windows[2][0] == (10, 70, 10, 0)


# -- opening it from the panes -----------------------------------------------------
def _point(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def _tables(app, tmp_path):
    write(str(tmp_path / "left" / "people.tsv"), PEOPLE)
    write(str(tmp_path / "right" / "orders.csv"), ORDERS)
    app.left.refresh()
    app.right.refresh()
    _point(app.left, "people.tsv")
    _point(app.right, "orders.csv")


def _opened(monkeypatch, saves=None):
    """Capture the JoinScreen the app builds instead of running it.

    With no screen run there is no curses screen either, and the app hides
    the cursor through curses afterwards: so that call is stood in for, as
    Compare's tests do.  Without it these pass on Linux only because some
    earlier test in the run started curses; on macOS none does.
    """
    from meridian_commander import app as app_mod

    monkeypatch.setattr(curses, "curs_set", lambda visible: None)

    seen: list = []

    class _Fake:
        def __init__(self, *args, **kwargs):
            seen.append((args, kwargs))
            self.saved = []

        def run(self, stdscr):
            if saves:
                write(saves, "x")
                self.saved.append((LocalFileSystem(), saves))

    monkeypatch.setattr(app_mod, "JoinScreen", _Fake)
    return seen


def test_k_joins_the_left_panes_table_with_the_right_panes(app, tmp_path,
                                                           monkeypatch):
    _tables(app, tmp_path)
    seen = _opened(monkeypatch)
    app.handle_key(ord("K"))
    (args, kwargs), = seen
    assert args[1] == str(tmp_path / "left" / "people.tsv")
    assert args[3] == str(tmp_path / "right" / "orders.csv")
    # Each file's view saves in the pane opposite the one it came from.
    assert kwargs == {"a_elsewhere": app.right, "b_elsewhere": app.left}


def test_the_left_panes_file_is_file_a_whichever_pane_is_active(
        app, tmp_path, monkeypatch):
    _tables(app, tmp_path)
    app.active = app.right
    seen = _opened(monkeypatch)
    app._join_files()
    assert seen[0][0][1] == str(tmp_path / "left" / "people.tsv")


def test_the_command_menu_has_it_beside_compare(app, tmp_path, monkeypatch):
    from meridian_commander.app import MENUS

    command = next(menu for menu in MENUS if menu["name"] == "command")
    names = [item.get("name") for item in command["items"]]
    assert names[names.index("compare") + 1] == "join"
    assert command["items"][names.index("join")]["key"] == "K"
    _tables(app, tmp_path)
    seen = _opened(monkeypatch)
    app._dispatch("join")
    assert len(seen) == 1


@pytest.mark.parametrize("setup, message", [
    (lambda app, tmp: _point(app.left, "sub"),
     "Join: left pane -- sub is a directory"),
    (lambda app, tmp: _point(app.left, "file.txt"),
     "Join: left pane -- file.txt is not a table (.tsv .tab .psv .csv)"),
    (lambda app, tmp: setattr(app.right, "selected",
                              {"orders.csv", "file.txt"}),
     "Join: right pane -- 2 files are tagged; tag one"),
    (lambda app, tmp: app.right.show_results("x", []),
     "Join: right pane -- it lists find results; < goes back to the "
     "directory"),
], ids=["directory", "not-a-table", "two-tagged", "find-results"])
def test_a_pane_with_no_single_table_says_why(app, tmp_path, monkeypatch,
                                              setup, message):
    _tables(app, tmp_path)
    setup(app, tmp_path)
    seen = _opened(monkeypatch)
    app._join_files()
    assert not seen and app.message == message


def test_a_hidden_pane_is_named_hidden(app, tmp_path, monkeypatch):
    _tables(app, tmp_path)
    _point(app.right, "sub")
    app.single = True
    seen = _opened(monkeypatch)
    app._join_files()
    assert not seen
    assert app.message == "Join: right pane (hidden) -- sub is a directory"


def test_a_file_saved_on_the_screen_shows_in_its_pane(app, tmp_path,
                                                      monkeypatch):
    _tables(app, tmp_path)
    _opened(monkeypatch, saves=str(tmp_path / "right" / "new.tsv"))
    app._join_files()
    assert "new.tsv" in [entry.name for entry in app.right.entries]


def test_a_failing_join_screen_is_reported(app, tmp_path, monkeypatch):
    from meridian_commander import app as app_mod

    _tables(app, tmp_path)

    def explode(*args, **kwargs):
        raise OSError("no")

    monkeypatch.setattr(app_mod, "JoinScreen", explode)
    monkeypatch.setattr(curses, "curs_set", lambda visible: None)
    seen = _ScriptedDialogs(monkeypatch)
    app._join_files()
    assert seen.messages == [("Join error", "no", True)]
