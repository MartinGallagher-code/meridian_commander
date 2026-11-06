# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The terminal emulator's screen: bytes in, cells out.

Every sequence the emulator implements has a test here that feeds it and
looks at the grid, the attributes and the cursor afterwards.  Where xterm,
VTE and tmux agree, these are their answers; where they differ, the test says
which one is followed and why.  The program recordings at the end are checked
against what tmux showed for the same bytes.
"""

from __future__ import annotations

import os
import random
import time

import pytest

from meridian_commander import vt
from meridian_commander.vt import (
    BOLD, DEFAULT, DIM, INVISIBLE, ITALIC, PLAIN, REVERSE, RGB, STRIKE,
    UNDERLINE, BLINK, Line, Screen, char_width,
)

from vt_recordings import RECORDINGS

ESC = b"\x1b"


def screen(data=b"", rows=5, cols=10, **kwargs) -> Screen:
    term = Screen(rows, cols, **kwargs)
    term.feed(data if isinstance(data, bytes) else data.encode())
    return term


def cursor(term: Screen) -> tuple[int, int]:
    """(row, column), the way the tests talk about positions."""
    return term.y, term.x


FILLED = b"line1\r\nline2\r\nline3\r\nline4\r\nline5"


# -- printing ------------------------------------------------------------------

def test_text_lands_at_the_cursor_and_moves_it():
    term = screen(b"hello")
    assert term.display()[0] == "hello"
    assert cursor(term) == (0, 5)


def test_a_new_screen_is_blank_and_plain():
    term = Screen(3, 4)
    assert term.display() == ["", "", ""]
    assert all(attr == PLAIN for line in term.lines for attr in line.attrs)


def test_a_screen_is_never_smaller_than_one_cell():
    term = Screen(0, -3)
    assert (term.rows, term.cols) == (1, 1)


def test_carriage_return_and_line_feed():
    term = screen(b"one\r\ntwo\rT")
    assert term.display()[:2] == ["one", "Two"]
    assert cursor(term) == (1, 1)


@pytest.mark.parametrize("control", [b"\n", b"\x0b", b"\x0c"])
def test_lf_vt_and_ff_all_move_down_without_returning(control):
    term = screen(b"ab" + control + b"c")
    assert term.display()[:2] == ["ab", "  c"]


def test_backspace_moves_back_but_not_past_the_margin():
    term = screen(b"ab\b\b\b\bZ")
    assert term.display()[0] == "Zb"


def test_the_bell_and_other_controls_print_nothing():
    term = screen(b"a\x07\x00\x01\x7fb")
    assert term.display()[0] == "ab"


def test_c1_controls_print_nothing():
    term = screen("a\u0085\u009bb".encode())
    assert term.display()[0] == "ab"


def test_undecodable_bytes_are_replaced():
    # The decoder holds an incomplete sequence until the next byte proves it
    # bad, then puts the replacement character in its place.
    term = screen(b"caf\xe9e")
    assert "�" in term.display()[0]


def test_a_character_split_across_two_feeds_is_reassembled():
    term = Screen(2, 10)
    term.feed("é".encode()[:1])
    term.feed("é".encode()[1:])
    assert term.display()[0] == "é"


def test_a_sequence_split_across_feeds_is_reassembled():
    term = Screen(2, 10)
    for byte in b"\x1b[1;31mred":
        term.feed(bytes([byte]))
    assert term.display()[0] == "red"
    assert term.lines[0].attrs[0] == (1, DEFAULT, BOLD)


def test_feed_text_takes_decoded_text():
    term = Screen(2, 10)
    term.feed_text("\x1b[2Ghi")
    assert term.display()[0] == " hi"


# -- wrapping ------------------------------------------------------------------

def test_the_last_column_holds_the_cursor_with_a_wrap_pending():
    term = screen(b"abcdefghij")
    assert cursor(term) == (0, 9)
    assert term.pending_wrap is True
    assert term.display()[:2] == ["abcdefghij", ""]


def test_the_next_character_wraps_and_marks_the_line():
    term = screen(b"abcdefghijK")
    assert term.display()[:2] == ["abcdefghij", "K"]
    assert term.lines[0].wrapped is True
    assert cursor(term) == (1, 1)


def test_a_newline_after_a_full_line_does_not_add_a_blank_one():
    term = screen(b"abcdefghij\r\nX")
    assert term.display()[:2] == ["abcdefghij", "X"]


def test_a_long_run_wraps_over_several_lines_and_scrolls():
    term = screen(b"x" * 45, rows=4, cols=10)
    assert term.display() == ["x" * 10, "x" * 10, "x" * 10, "x" * 5]
    assert len(term.scrollback) == 1


def test_without_autowrap_the_last_column_is_overwritten():
    term = screen(b"\x1b[?7labcdefghijklmn")
    assert term.display()[:2] == ["abcdefghin", ""]
    assert term.pending_wrap is False


def test_turning_autowrap_off_cancels_a_pending_wrap():
    term = screen(b"abcdefghij\x1b[?7l")
    assert term.pending_wrap is False


@pytest.mark.parametrize("sequence, expected", [
    (b"\x1b[K", "abcdefghij"),     # EL 0: nothing is right of the cursor
    (b"\x1b[J", "abcdefghij"),     # ED 0
    (b"\x1b[X", "abcdefghij"),     # ECH
    (b"\x1b[@", "abcdefghij"),     # ICH
    (b"\x1b[P", "abcdefghij"),     # DCH
    (b"\x1b[1K", ""),              # EL 1 takes the whole line
])
def test_with_a_wrap_pending_the_cursor_is_past_the_last_column(sequence,
                                                                 expected):
    """top writes lines that fill the width and then erases to the end of
    each; erasing from the last column took its last character away.  The
    wrap stays pending, so the next character still starts the next line --
    what VTE and tmux do."""
    term = screen(b"abcdefghij" + sequence + b"X")
    assert term.display()[:2] == [expected, "X"]


@pytest.mark.parametrize("sequence", [b"\b", b"\x1b[D"])
def test_stepping_back_from_a_pending_wrap_lands_on_the_last_column(sequence):
    term = screen(b"abcdefghij" + sequence + b"X")
    assert term.display()[0] == "abcdefghiX"


def test_a_move_cancels_the_pending_wrap():
    term = screen(b"abcdefghij\x1b[1mX")
    assert term.display()[:2] == ["abcdefghij", "X"]        # SGR is no move
    term = screen(b"abcdefghij\x1b[10GX")
    assert term.display()[:2] == ["abcdefghiX", ""]


# -- cursor movement -----------------------------------------------------------

def test_cursor_position_is_one_based_and_clamped():
    term = screen(b"\x1b[3;5HX\x1b[HY\x1b[99;99HZ", rows=6, cols=12)
    assert term.display() == ["Y", "", "    X", "", "", "           Z"]


def test_hvp_is_cursor_position_too():
    assert cursor(screen(b"\x1b[2;3f")) == (1, 2)


def test_a_cursor_position_with_only_a_row():
    assert cursor(screen(b"\x1b[3H")) == (2, 0)


@pytest.mark.parametrize("sequence, expected", [
    (b"\x1b[3;3H\x1b[2A", (0, 2)),
    (b"\x1b[3;3H\x1b[A", (1, 2)),
    (b"\x1b[3;3H\x1b[9A", (0, 2)),
    (b"\x1b[3;3H\x1b[B", (3, 2)),
    (b"\x1b[3;3H\x1b[9B", (4, 2)),
    (b"\x1b[3;3H\x1b[4C", (2, 6)),
    (b"\x1b[3;3H\x1b[99C", (2, 9)),
    (b"\x1b[3;3H\x1b[2D", (2, 0)),
    (b"\x1b[3;3H\x1b[0D", (2, 1)),      # 0 means 1
    (b"\x1b[3;3H\x1b[E", (3, 0)),
    (b"\x1b[3;3H\x1b[2F", (0, 0)),
    (b"\x1b[3;3H\x1b[7G", (2, 6)),
    (b"\x1b[3;3H\x1b[99G", (2, 9)),
    (b"\x1b[3;3H\x1b[4`", (2, 3)),      # HPA
    (b"\x1b[3;3H\x1b[3a", (2, 5)),      # HPR
    (b"\x1b[3;3H\x1b[5d", (4, 2)),      # VPA
    (b"\x1b[3;3H\x1b[e", (3, 2)),       # VPR
])
def test_relative_and_absolute_moves(sequence, expected):
    assert cursor(screen(sequence)) == expected


def test_cursor_up_and_down_stop_at_the_scroll_region():
    term = screen(b"\x1b[2;4r\x1b[3;1H\x1b[9A")
    assert cursor(term) == (1, 0)
    term.feed(b"\x1b[9B")
    assert cursor(term) == (3, 0)


def test_outside_the_region_up_and_down_reach_the_screen_edges():
    term = screen(b"\x1b[2;3r\x1b[5;1H\x1b[9A")
    assert cursor(term) == (1, 0)           # stopped by the region's top
    term = screen(b"\x1b[2;3r\x1b[5;1H\x1b[9B")
    assert cursor(term) == (4, 0)
    term = screen(b"\x1b[3;4r\x1b[1;1H\x1b[9A")
    assert cursor(term) == (0, 0)


def test_huge_moves_are_clamped_to_the_screen():
    term = screen(b"\x1b[99999;99999HX\x1b[99999A\x1b[99999D\x1b[99999BY")
    assert cursor(term) == (4, 1)


def test_a_parameter_beyond_the_largest_number_is_clamped():
    term = screen(b"\x1b[" + b"9" * 60 + b"C")
    assert cursor(term) == (0, 9)


def test_a_malformed_parameter_reads_as_the_default():
    assert cursor(screen(b"abcdef\x1b[2?D")) == (0, 5)
    assert cursor(screen(b"abcdef\x1b[;D")) == (0, 5)


def test_a_sequence_with_unknown_intermediates_is_ignored():
    # "-" is an intermediate byte, not a parameter: CSI - - D is no CUB.
    assert cursor(screen(b"abcdef\x1b[--D")) == (0, 6)


# -- erasing -------------------------------------------------------------------

@pytest.mark.parametrize("mode, expected", [
    (b"", ["line1", "line2", "li", "", ""]),
    (b"0", ["line1", "line2", "li", "", ""]),
    (b"1", ["", "", "   e3", "line4", "line5"]),
    (b"2", ["", "", "", "", ""]),
])
def test_erase_in_display(mode, expected):
    term = screen(FILLED + b"\x1b[3;3H\x1b[" + mode + b"J")
    assert term.display() == expected
    assert cursor(term) == (2, 2)


def test_erase_display_3_clears_the_scrollback_only():
    term = screen(FILLED + b"\r\nline6\r\nline7")
    assert len(term.scrollback) == 2
    term.feed(b"\x1b[3J")
    assert len(term.scrollback) == 0
    assert term.display()[-1] == "line7"


def test_an_unknown_erase_mode_does_nothing():
    term = screen(FILLED + b"\x1b[3;3H\x1b[7J\x1b[7K")
    assert term.display()[2] == "line3"


@pytest.mark.parametrize("mode, expected", [
    (b"", "abc"), (b"0", "abc"), (b"1", "    efgh"), (b"2", ""),
])
def test_erase_in_line(mode, expected):
    term = screen(b"abcdefgh\x1b[1;4H\x1b[" + mode + b"K")
    assert term.display()[0] == expected


def test_selective_erase_is_treated_as_an_ordinary_erase():
    assert screen(b"abcdefgh\x1b[1;4H\x1b[?K").display()[0] == "abc"
    assert screen(FILLED + b"\x1b[2;1H\x1b[?J").display()[1:] == [""] * 4


def test_erase_characters():
    term = screen(b"abcdefgh\x1b[1;3H\x1b[3X")
    assert term.display()[0] == "ab   fgh"
    assert cursor(term) == (0, 2)
    assert screen(b"abcdefgh\x1b[1;3H\x1b[X").display()[0] == "ab defgh"


def test_erasing_uses_the_current_background():
    """xterm-256color promises bce: a program paints its background by
    setting a colour and clearing."""
    term = screen(b"\x1b[1;44m\x1b[2J")
    assert term.lines[3].attrs[5] == (DEFAULT, 4, 0)


def test_an_erase_reaching_the_end_unmarks_the_wrap():
    term = screen(b"abcdefghijK\x1b[1;5H\x1b[K")
    assert term.lines[0].wrapped is False


# -- inserting and deleting ----------------------------------------------------

def test_insert_characters():
    term = screen(b"abcdefgh\x1b[1;3H\x1b[2@")
    assert term.display()[0] == "ab  cdefgh"
    term.feed(b"\x1b[@")
    assert term.display()[0] == "ab   cdefg"


def test_delete_characters():
    term = screen(b"abcdefgh\x1b[1;3H\x1b[2P")
    assert term.display()[0] == "abefgh"
    term.feed(b"\x1b[P")
    assert term.display()[0] == "abfgh"


def test_insert_lines_pushes_the_rest_of_the_region_down():
    term = screen(FILLED + b"\x1b[2;3H\x1b[2L")
    assert term.display() == ["line1", "", "", "line2", "line3"]
    assert cursor(term) == (1, 0)           # IL returns to the left margin


def test_delete_lines_pulls_the_rest_of_the_region_up():
    term = screen(FILLED + b"\x1b[2;1H\x1b[2M")
    assert term.display() == ["line1", "line4", "line5", "", ""]
    term = screen(FILLED + b"\x1b[2;1H\x1b[M")
    assert term.display() == ["line1", "line3", "line4", "line5", ""]


def test_insert_and_delete_lines_stay_inside_the_region():
    term = screen(FILLED + b"\x1b[2;4r\x1b[3;1H\x1b[5L")
    assert term.display() == ["line1", "line2", "", "", "line5"]
    term = screen(FILLED + b"\x1b[2;4r\x1b[2;1H\x1b[M")
    assert term.display() == ["line1", "line3", "line4", "", "line5"]


def test_insert_and_delete_lines_outside_the_region_do_nothing():
    term = screen(FILLED + b"\x1b[2;4r\x1b[5;1H\x1b[L\x1b[M")
    assert term.display() == ["line1", "line2", "line3", "line4", "line5"]


def test_insert_mode_pushes_text_right():
    term = screen(b"abcdef\r\x1b[4hXY\x1b[4l")
    assert term.display()[0] == "XYabcdef"
    assert term.insert is False


def test_insert_mode_drops_what_is_pushed_off_the_end():
    term = screen(b"abcdefghij\r\x1b[4hXY")
    assert term.display()[0] == "XYabcdefgh"


# -- scrolling -----------------------------------------------------------------

def test_scroll_up_and_down():
    term = screen(FILLED + b"\x1b[2S")
    assert term.display() == ["line3", "line4", "line5", "", ""]
    term.feed(b"\x1b[T")
    assert term.display() == ["", "line3", "line4", "line5", ""]


def test_scroll_down_with_five_parameters_is_mouse_tracking_and_ignored():
    term = screen(FILLED + b"\x1b[1;2;3;4;5T")
    assert term.display()[0] == "line1"


def test_scrolling_the_whole_screen_keeps_the_history():
    term = screen(FILLED + b"\r\nline6\r\nline7")
    assert [line.text() for line in term.scrollback] == ["line1", "line2"]
    assert term.history_count == 2


def test_the_scrollback_is_capped():
    term = screen(b"".join(b"line %d\r\n" % i for i in range(20)),
                  scrollback=5)
    assert len(term.scrollback) == 5
    assert term.scrollback[-1].text() == "line 15"
    assert term.history_count == 16


def test_a_region_scrolls_on_its_own_and_keeps_no_history():
    term = screen(FILLED + b"\x1b[2;4r\x1b[4;1H\nX")
    assert term.display() == ["line1", "line3", "line4", "X", "line5"]
    assert len(term.scrollback) == 0


def test_a_region_starting_at_the_top_does_keep_history():
    term = screen(FILLED + b"\x1b[1;3r\x1b[3;1H\n")
    assert [line.text() for line in term.scrollback] == ["line1"]
    assert term.display() == ["line2", "line3", "", "line4", "line5"]


def test_reverse_index_at_the_top_of_the_region_scrolls_it_down():
    term = screen(FILLED + b"\x1b[2;4r\x1b[2;1H\x1bMX")
    assert term.display() == ["line1", "X", "line2", "line3", "line5"]


def test_reverse_index_elsewhere_just_moves_up():
    term = screen(FILLED + b"\x1b[3;1H\x1bMX")
    assert term.display()[1] == "Xine2"
    term = screen(b"\x1b[3;5r\x1b[1;1H\x1bMX")       # above the region
    assert cursor(term) == (0, 1)


def test_index_and_next_line():
    term = screen(b"ab\x1bEcd\x1bDef")
    assert term.display()[:3] == ["ab", "cd", "  ef"]


def test_index_below_the_region_stops_at_the_bottom_of_the_screen():
    term = screen(b"\x1b[1;2r\x1b[5;1H\n\nX")
    assert cursor(term) == (4, 1)


def test_scroll_counts_beyond_the_region_clear_it():
    term = screen(FILLED + b"\x1b[99999S")
    assert term.display() == [""] * 5
    term = screen(FILLED + b"\x1b[99999T")
    assert term.display() == [""] * 5


def test_new_lines_take_the_current_background():
    term = screen(FILLED + b"\x1b[42m\x1b[S")
    assert term.lines[4].attrs[0] == (DEFAULT, 2, 0)


def test_set_region_homes_the_cursor():
    assert cursor(screen(b"\x1b[3;3H\x1b[2;4r")) == (0, 0)


@pytest.mark.parametrize("region", [b"4;2", b"3;3", b"9;12"])
def test_a_region_that_is_not_one_is_ignored(region):
    """xterm requires at least two lines; tmux takes a single one."""
    term = screen(FILLED + b"\x1b[3;3H\x1b[" + region + b"r")
    assert (term.top, term.bottom) == (0, 4)
    assert cursor(term) == (2, 2)


def test_a_region_without_parameters_is_the_whole_screen():
    term = screen(b"\x1b[2;3r\x1b[r")
    assert (term.top, term.bottom) == (0, 4)


def test_a_region_bottom_past_the_screen_is_clamped():
    term = screen(b"\x1b[2;99r")
    assert (term.top, term.bottom) == (1, 4)


# -- origin mode ---------------------------------------------------------------

def test_origin_mode_positions_from_the_region_and_keeps_inside_it():
    term = screen(b"\x1b[2;4r\x1b[?6h")
    assert cursor(term) == (1, 0)
    term.feed(b"\x1b[1;1HO\x1b[9;1HB")
    assert term.display()[1] == "O"
    assert term.display()[3] == "B"


def test_origin_mode_reports_positions_from_the_region():
    term = screen(b"\x1b[2;4r\x1b[?6h\x1b[2;3H\x1b[6n")
    assert term.take_replies() == b"\x1b[2;3R"


def test_leaving_origin_mode_homes_to_the_screen():
    term = screen(b"\x1b[2;4r\x1b[?6h\x1b[?6l")
    assert cursor(term) == (0, 0)


# -- tabs ----------------------------------------------------------------------

def test_tabs_stop_every_eight_columns():
    term = screen(b"a\tb\tc", cols=30)
    assert term.display()[0] == "a       b       c"


def test_a_tab_past_the_last_stop_goes_to_the_last_column():
    term = screen(b"\t\t\t\tX", cols=20)
    assert term.display()[0] == " " * 19 + "X"


def test_setting_and_clearing_tab_stops():
    term = screen(b"\x1b[3g\x1b[5G\x1bH\r\tX", cols=30)
    assert term.display()[0] == "    X"
    term.feed(b"\x1b[5G\x1b[0g\r\tY")
    assert cursor(term) == (0, 29)


def test_forward_and_back_tab_counts():
    term = screen(b"\x1b[2I", cols=30)
    assert cursor(term) == (0, 16)
    term.feed(b"\x1b[Z")
    assert cursor(term) == (0, 8)
    term.feed(b"\x1b[5Z")
    assert cursor(term) == (0, 0)


def test_tab_clear_with_an_unknown_mode_does_nothing():
    term = screen(b"\x1b[5g\t", cols=30)
    assert cursor(term) == (0, 8)


# -- saving and restoring the cursor --------------------------------------------

@pytest.mark.parametrize("save, restore", [
    (b"\x1b7", b"\x1b8"), (b"\x1b[s", b"\x1b[u"), (b"\x1b[?1048h", b"\x1b[?1048l"),
])
def test_save_and_restore_the_cursor(save, restore):
    term = screen(b"\x1b[2;3H\x1b[1;31m" + save + b"\x1b[0m\x1b[4;5H" + restore + b"X")
    assert term.display()[1] == "  X"
    assert term.lines[1].attrs[2] == (1, DEFAULT, BOLD)


def test_save_and_restore_keep_the_character_sets():
    term = screen(b"\x1b(0\x1b7\x1b(B\x1b8q")
    assert term.display()[0] == "─"


def test_restoring_with_nothing_saved_goes_home_with_a_plain_pen():
    term = screen(b"\x1b[3;3H\x1b[1m\x1b(0\x1b8X")
    assert term.display()[0] == "X"
    assert term.lines[0].attrs[0] == PLAIN


def test_a_restored_position_is_clamped_to_a_smaller_screen():
    term = screen(b"\x1b[5;10H\x1b7")
    term.resize(3, 4)
    term.feed(b"\x1b8")
    assert cursor(term) == (2, 3)


def test_a_saved_pending_wrap_comes_back_only_in_the_last_column():
    term = screen(b"abcdefghij\x1b7\x1b8K")
    assert term.display()[:2] == ["abcdefghij", "K"]


# -- the alternate screen ------------------------------------------------------

def test_1049_saves_the_main_screen_and_cursor_and_brings_them_back():
    term = screen(b"main\r\nprompt$ \x1b[?1049h")
    assert term.alt_active is True
    assert term.display() == [""] * 5
    term.feed(b"\x1b[2J\x1b[Halt\x1b[5;5Hzz")
    assert term.display()[0] == "alt"
    term.feed(b"\x1b[?1049l")
    assert term.alt_active is False
    assert term.display()[:2] == ["main", "prompt$"]
    assert cursor(term) == (1, 8)


def test_1049_clears_the_alternate_screen_on_the_way_in():
    term = screen(b"\x1b[?1049hold\x1b[?1049l\x1b[?1049h")
    assert term.display()[0] == ""


def test_1049_set_twice_does_not_clear_or_save_again():
    term = screen(b"\x1b[2;2H\x1b[?1049hdrawn\x1b[?1049h")
    assert term.display()[1] == " drawn"
    term.feed(b"\x1b[?1049l")
    assert cursor(term) == (1, 1)


def test_1049_reset_on_the_main_screen_does_not_move_the_cursor():
    term = screen(b"\x1b[3;3H\x1b[?1049l")
    assert cursor(term) == (2, 2)


def test_47_switches_without_clearing_or_saving():
    term = screen(b"main\x1b[?47hALT\x1b[?47l")
    assert term.display()[0] == "main"
    assert cursor(term) == (0, 7)
    term.feed(b"\x1b[?47h")
    assert term.display()[0] == "    ALT"


def test_1047_clears_the_alternate_screen_on_the_way_out():
    term = screen(b"main\x1b[?1047hALT\x1b[?1047l")
    assert term.display()[0] == "main"
    term.feed(b"\x1b[?47h")
    assert term.display()[0] == ""


def test_1047_reset_on_the_main_screen_clears_nothing():
    term = screen(b"main\x1b[?1047l")
    assert term.display()[0] == "main"


def test_the_alternate_screen_keeps_no_history():
    term = screen(b"\x1b[?1049h" + b"x\r\n" * 20)
    assert len(term.scrollback) == 0


def test_save_and_restore_are_kept_per_screen():
    term = screen(b"\x1b[2;2H\x1b7\x1b[?47h\x1b[4;4H\x1b7\x1b[?47l\x1b8X")
    assert term.display()[1] == " X"


# -- SGR -----------------------------------------------------------------------

def _pen(sequence: bytes) -> tuple:
    return screen(sequence).pen


@pytest.mark.parametrize("sequence, expected", [
    (b"\x1b[1m", (DEFAULT, DEFAULT, BOLD)),
    (b"\x1b[2m", (DEFAULT, DEFAULT, DIM)),
    (b"\x1b[3m", (DEFAULT, DEFAULT, ITALIC)),
    (b"\x1b[4m", (DEFAULT, DEFAULT, UNDERLINE)),
    (b"\x1b[21m", (DEFAULT, DEFAULT, UNDERLINE)),
    (b"\x1b[5m", (DEFAULT, DEFAULT, BLINK)),
    (b"\x1b[6m", (DEFAULT, DEFAULT, BLINK)),
    (b"\x1b[7m", (DEFAULT, DEFAULT, REVERSE)),
    (b"\x1b[8m", (DEFAULT, DEFAULT, INVISIBLE)),
    (b"\x1b[9m", (DEFAULT, DEFAULT, STRIKE)),
    (b"\x1b[31m", (1, DEFAULT, 0)),
    (b"\x1b[47m", (DEFAULT, 7, 0)),
    (b"\x1b[91m", (9, DEFAULT, 0)),
    (b"\x1b[104m", (DEFAULT, 12, 0)),
    (b"\x1b[38;5;196m", (196, DEFAULT, 0)),
    (b"\x1b[48;5;21m", (DEFAULT, 21, 0)),
    (b"\x1b[38;2;10;20;30m", (RGB | 0x0A141E, DEFAULT, 0)),
    (b"\x1b[48;2;255;0;1m", (DEFAULT, RGB | 0xFF0001, 0)),
    (b"\x1b[38:5:196m", (196, DEFAULT, 0)),
    (b"\x1b[48:5:3m", (DEFAULT, 3, 0)),
    (b"\x1b[38:2:10:20:30m", (RGB | 0x0A141E, DEFAULT, 0)),
    (b"\x1b[38:2::10:20:30m", (RGB | 0x0A141E, DEFAULT, 0)),
    (b"\x1b[48:2:0:10:20:30m", (DEFAULT, RGB | 0x0A141E, 0)),
    (b"\x1b[4:3m", (DEFAULT, DEFAULT, UNDERLINE)),
    (b"\x1b[1;31;42m", (1, 2, BOLD)),
])
def test_sgr(sequence, expected):
    assert _pen(sequence) == expected


@pytest.mark.parametrize("sequence", [
    b"\x1b[0m", b"\x1b[m", b"\x1b[;m",
])
def test_sgr_reset(sequence):
    assert _pen(b"\x1b[1;4;31;42m" + sequence) == PLAIN


@pytest.mark.parametrize("off, flag", [
    (22, BOLD | DIM), (23, ITALIC), (24, UNDERLINE), (25, BLINK),
    (27, REVERSE), (28, INVISIBLE), (29, STRIKE),
])
def test_sgr_turning_attributes_off(off, flag):
    everything = BOLD | DIM | ITALIC | UNDERLINE | BLINK | REVERSE | \
        INVISIBLE | STRIKE
    pen = _pen(b"\x1b[1;2;3;4;5;7;8;9m\x1b[%dm" % off)
    assert pen[2] == everything & ~flag


def test_underline_off_in_the_colon_spelling():
    assert _pen(b"\x1b[4m\x1b[4:0m") == PLAIN


def test_default_colours_come_back_with_39_and_49():
    assert _pen(b"\x1b[31;42;1m\x1b[39;49m") == (DEFAULT, DEFAULT, BOLD)


@pytest.mark.parametrize("sequence", [
    b"\x1b[38;5m",              # no index
    b"\x1b[38;5;256m",          # out of the palette
    b"\x1b[38;2;1;2m",          # a channel short
    b"\x1b[38;2;1;2;300m",      # a channel out of range
    b"\x1b[38m",                # nothing at all
    b"\x1b[38;7m",              # an unknown kind
    b"\x1b[38:5m",
    b"\x1b[38:5:999m",
    b"\x1b[38:2:1:2m",
    b"\x1b[38:m",
    b"\x1b[38:9:1m",
    b"\x1b[58;5;3m",            # underline colour: consumed, not drawn
    b"\x1b[58;2;1;2;3m",
    b"\x1b[58:5:3m",
    b"\x1b[999m",
    b"\x1b[1:2m",               # sub-parameters on something that has none
])
def test_malformed_and_ignored_colours_change_nothing(sequence):
    assert _pen(sequence) == PLAIN


def test_the_colour_numbers_after_58_are_not_read_as_attributes():
    # Read as attributes, "58;5;1" would have turned on blink and bold.
    assert _pen(b"\x1b[58;5;1m") == PLAIN


def test_text_takes_the_pen_it_was_written_with():
    term = screen(b"\x1b[31mA\x1b[0mB")
    assert term.lines[0].attrs[:2] == [(1, DEFAULT, 0), PLAIN]


# -- character sets ------------------------------------------------------------

def test_dec_line_drawing_through_g0():
    term = screen(b"\x1b(0lqqk\r\nx  x\r\nmqqj\x1b(B ok", cols=12)
    assert term.display()[:3] == ["┌──┐", "│  │", "└──┘ ok"]


def test_shift_out_and_in_switch_to_g1_and_back():
    term = screen(b"\x1b)0\x0elqk\x0f abc", cols=12)
    assert term.display()[0] == "┌─┐ abc"


def test_g1_is_ascii_until_designated():
    assert screen(b"\x0elqk\x0f").display()[0] == "lqk"


def test_locking_shifts_to_g2_and_g3():
    term = screen(b"\x1b*0\x1b+A\x1bnq\x1bo#\x0fq")
    assert term.display()[0] == "─£q"


def test_single_shifts_take_one_character():
    term = screen(b"\x1b*0\x1bNqq\x1b+A\x1bO##")
    assert term.display()[0] == "─q£#"


def test_a_single_shift_before_a_sequence_waits_for_the_character():
    term = screen(b"\x1b*0\x1bN\x1b[1mq")
    assert term.display()[0] == "─"


def test_the_uk_set_has_a_pound_sign():
    assert screen(b"\x1b(A#1\x1b(B#").display()[0] == "£1#"


def test_every_dec_graphic_is_one_column():
    for ch in "_`abcdefghijklmnopqrstuvwxyz{|}~":
        assert char_width(ch.translate(vt.DEC_GRAPHICS)) == 1


# -- wide and combining characters ---------------------------------------------

@pytest.mark.parametrize("ch, width", [
    ("a", 1), ("é", 1), ("́", 0), ("‍", 0), ("⃝", 0),
    ("日", 2), ("Ａ", 2), ("─", 1), ("Ж", 1),
])
def test_character_widths(ch, width):
    assert char_width(ch) == width


def test_a_wide_character_takes_two_columns():
    term = screen("日本x".encode())
    assert term.lines[0].chars[:5] == ["日", "", "本", "", "x"]
    assert term.display()[0] == "日本x"
    assert cursor(term) == (0, 5)


def test_a_wide_character_that_does_not_fit_moves_to_the_next_line():
    term = screen("abcde日".encode(), cols=6)
    assert term.display()[:2] == ["abcde", "日"]
    assert term.lines[0].wrapped is True


def test_without_autowrap_a_wide_character_moves_back_to_fit():
    term = screen("\x1b[?7labcde日".encode(), cols=6)
    assert term.display()[0] == "abcd日"


def test_a_wide_character_filling_the_line_leaves_a_wrap_pending():
    term = screen("abcd日".encode(), cols=6)
    assert term.pending_wrap is True
    term.feed(b"x")
    assert term.display()[:2] == ["abcd日", "x"]


def test_a_one_column_screen_shows_a_wide_character_as_a_question_mark():
    assert screen("日".encode(), rows=2, cols=1).display()[0] == "?"


def test_overwriting_half_a_wide_character_blanks_the_other_half():
    term = screen("日本\x1b[1;2Hx".encode(), cols=6)
    assert term.display()[0] == " x本"
    term = screen("日本\x1b[1;1Hx".encode(), cols=6)
    assert term.display()[0] == "x 本"


def test_a_narrow_run_over_a_wide_character_blanks_its_other_half():
    term = screen("ab日cd\x1b[1;2Hxy".encode(), cols=8)
    assert term.display()[0] == "axy cd"


def test_erasing_half_a_wide_character_erases_all_of_it():
    term = screen("日本語\x1b[1;4H\x1b[K".encode(), cols=8)
    assert term.display()[0] == "日"


def test_inserting_inside_a_wide_character_blanks_it():
    term = screen("ab日cd\x1b[1;4H\x1b[@".encode(), cols=8)
    assert term.display()[0] == "ab   cd"


def test_a_wide_character_pushed_half_off_the_end_is_blanked():
    term = screen("abcd日\x1b[1;1H\x1b[@".encode(), cols=6)
    assert term.display()[0] == " abcd"


def test_deleting_inside_a_wide_character_blanks_it():
    term = screen("ab日cd\x1b[1;4H\x1b[P".encode(), cols=8)
    assert term.display()[0] == "ab cd"


def test_insert_mode_with_a_wide_character():
    term = screen("abc\r\x1b[4h日".encode(), cols=8)
    assert term.display()[0] == "日abc"


def test_combining_marks_join_the_character_before():
    term = screen("éäx".encode())
    assert term.lines[0].chars[:3] == ["é", "ä", "x"]
    assert cursor(term) == (0, 3)


def test_a_combining_mark_joins_a_wide_character():
    term = screen("日́".encode())
    assert term.lines[0].chars[0] == "日́"


def test_a_combining_mark_with_a_wrap_pending_joins_the_last_column():
    term = screen("abcdefghij́".encode())
    assert term.lines[0].chars[9] == "j́"
    assert term.pending_wrap is True


def test_a_combining_mark_at_the_start_of_a_line_is_dropped():
    term = screen("́x".encode())
    assert term.display()[0] == "x"


def test_a_cell_collects_only_so_many_combining_marks():
    term = screen(("e" + "́" * 10000).encode())
    assert len(term.lines[0].chars[0]) == vt.MAX_MARKS + 1


# -- repeat --------------------------------------------------------------------

def test_repeat_the_last_character():
    term = screen(b"ab\x1b[3b")
    assert term.display()[0] == "abbbb"


def test_repeat_after_a_wide_character():
    term = screen("日\x1b[b".encode())
    assert term.display()[0] == "日日"


def test_repeat_with_nothing_printed_repeats_a_space():
    term = screen(b"\x1b[3bx")
    assert term.display()[0] == "   x"


def test_a_huge_repeat_is_capped_at_a_screenful():
    started = time.monotonic()
    term = screen(b"a\x1b[65535b", rows=3, cols=5)
    assert time.monotonic() - started < 1
    # The "a" and fifteen more: one screenful, then one line scrolled off.
    assert term.display() == ["aaaaa", "aaaaa", "a"]


# -- replies -------------------------------------------------------------------

@pytest.mark.parametrize("sequence, reply", [
    (b"\x1b[5n", b"\x1b[0n"),
    (b"\x1b[3;4H\x1b[6n", b"\x1b[3;4R"),
    (b"\x1b[3;4H\x1b[?6n", b"\x1b[?3;4R"),
    (b"\x1b[c", b"\x1b[?62;22c"),
    (b"\x1b[0c", b"\x1b[?62;22c"),
    (b"\x1b[>c", b"\x1b[>1;10;0c"),
    (b"\x1b[18t", b"\x1b[8;5;10t"),
    (b"\x1b[1c", b""),
    (b"\x1b[9n", b""),
    (b"\x1b[?5n", b""),
    (b"\x1b[>q", b""),
    (b"\x1b[=c", b""),
    (b"\x1b[22;0;0t", b""),
])
def test_replies(sequence, reply):
    assert screen(sequence).take_replies() == reply


def test_taking_the_replies_empties_them():
    term = screen(b"\x1b[5n")
    term.take_replies()
    assert term.take_replies() == b""


def test_the_cursor_report_counts_a_pending_wrap_as_the_last_column():
    assert screen(b"abcdefghij\x1b[6n").take_replies() == b"\x1b[1;10R"


def test_a_flood_of_queries_cannot_grow_the_replies_without_bound():
    term = screen(b"\x1b[6n" * 100000)
    assert len(term.take_replies()) <= vt.MAX_REPLY_BYTES


# -- modes ---------------------------------------------------------------------

@pytest.mark.parametrize("sequence, name, value", [
    (b"\x1b[?1h", "app_cursor", True),
    (b"\x1b[?1h\x1b[?1l", "app_cursor", False),
    (b"\x1b=", "app_keypad", True),
    (b"\x1b=\x1b>", "app_keypad", False),
    (b"\x1b[?66h", "app_keypad", True),
    (b"\x1b[?25l", "cursor_visible", False),
    (b"\x1b[?25l\x1b[?25h", "cursor_visible", True),
    (b"\x1b[?5h", "reverse_video", True),
    (b"\x1b[?2004h", "bracketed_paste", True),
    (b"\x1b[20h", "newline_mode", True),
    (b"\x1b[?1000;1006h", "app_cursor", False),     # mouse: accepted, ignored
    (b"\x1b[?3h", "autowrap", True),                # 132 columns: ignored
])
def test_modes(sequence, name, value):
    assert getattr(screen(sequence), name) is value


def test_newline_mode_returns_to_the_margin_on_line_feed():
    term = screen(b"\x1b[20hab\ncd")
    assert term.display()[:2] == ["ab", "cd"]


def test_an_unknown_ansi_mode_is_ignored():
    term = screen(b"\x1b[7habc")
    assert term.display()[0] == "abc"


def test_soft_reset_puts_the_modes_back_and_keeps_the_screen():
    term = screen(b"text\x1b[?25l\x1b[4h\x1b[?6h\x1b[?7l\x1b[?1h\x1b=\x1b[2;3r"
                  b"\x1b[1;31m\x1b(0\x1b7\x1b[!p")
    assert term.display()[0] == "text"
    assert term.cursor_visible and term.autowrap
    assert not (term.insert or term.origin or term.app_cursor or term.app_keypad)
    assert (term.top, term.bottom) == (0, 4)
    assert term.pen == PLAIN
    assert term.charsets == ["B"] * 4


def test_full_reset_clears_everything_but_the_history():
    term = screen(FILLED + b"\r\nmore\x1b[?1049h\x1b[1m\x1bc")
    assert term.display() == [""] * 5
    assert term.alt_active is False
    assert term.pen == PLAIN
    assert cursor(term) == (0, 0)
    assert len(term.scrollback) == 1


def test_the_alignment_pattern_fills_the_screen_with_e():
    term = screen(b"\x1b[2;3r\x1b[?6h\x1b[3;3H\x1b#8", rows=3, cols=4)
    assert term.display() == ["EEEE"] * 3
    assert cursor(term) == (0, 0)
    assert (term.top, term.bottom, term.origin) == (0, 2, False)


# -- the parser ----------------------------------------------------------------

def test_a_control_character_inside_a_sequence_is_acted_on():
    term = screen(b"ab\x1b[\r2CX")
    assert term.display()[0] == "abX"


def test_a_control_character_after_escape_is_acted_on():
    term = screen(b"ab\x1b\r=X")
    assert term.display()[0] == "Xb"
    assert term.app_keypad is True


@pytest.mark.parametrize("cancel", [b"\x18", b"\x1a"])
def test_can_and_sub_abandon_a_sequence(cancel):
    assert screen(b"ab\x1b[2" + cancel + b"CX").display()[0] == "abCX"
    assert screen(b"ab\x1b" + cancel + b"X").display()[0] == "abX"


def test_an_escape_inside_a_sequence_starts_a_new_one():
    term = screen(b"ab\x1b[2\x1b[1;31mX")
    assert term.display()[0] == "abX"
    assert term.lines[0].attrs[2] == (1, DEFAULT, BOLD)


def test_escape_escape_starts_again():
    assert screen(b"\x1b\x1b[2Cx").display()[0] == "  x"


def test_unknown_escapes_are_dropped():
    assert screen(b"a\x1bZb\x1b%Gc\x1b#3d\x1b\\e").display()[0] == "abcde"


def test_too_many_intermediates_are_dropped():
    assert screen(b"a\x1b !\"#0b").display()[0] == "ab"


def test_del_after_escape_abandons_it():
    assert screen(b"a\x1b\x7fb").display()[0] == "ab"


def test_text_after_escape_abandons_it_and_is_printed():
    assert screen("a\x1bé".encode()).display()[0] == "aé"


def test_a_c1_control_after_escape_is_dropped():
    assert screen("a\x1b\u0085b".encode()).display()[0] == "ab"


def test_intermediates_inside_a_csi_sequence():
    term = screen(b"\x1b[2 qX\x1b[1$pY\x1b[?1$pZ\x1b[1 !p")
    assert term.display()[0] == "XYZ"


def test_a_long_csi_sequence_falls_back_to_the_slow_path_and_is_bounded():
    # The sequence is still swallowed to its final byte, so what follows it
    # is printed rather than the stream losing its place.
    term = screen(b"\x1b[" + b"1" * 100000 + b"m")
    term.feed(b"after")
    assert term.display()[0] == "after"
    term = screen(b"\x1b[" + b"1;" * 100000 + b"mafter")
    assert term.display()[0] == "after"


def test_text_inside_a_csi_sequence_is_swallowed_to_the_final_byte():
    assert screen("\x1b[1é\x7f;2Hx".encode()).display()[0] == " x"


def test_too_many_csi_intermediates_are_dropped():
    assert screen(b"\x1b[1 !\"#Cx").display()[0] == "x"


def test_an_osc_ends_at_bel_or_at_the_string_terminator():
    term = screen(b"a\x1b]0;title\x07b\x1b]2;t\x1b\\c")
    assert term.display()[0] == "abc"


def test_an_escape_inside_an_osc_that_is_not_a_terminator_stays_in_it():
    assert screen(b"\x1b]0;ti\x1bXtle\x07after").display()[0] == "after"


def test_an_unterminated_osc_is_abandoned_at_a_control_character():
    """One stray "ESC ]" in binary output used to swallow the rest of the
    session (PR #69); an OSC string is text, so a newline means it never
    ended.  tmux and xterm swallow on; this keeps the protection."""
    term = screen(b"a\x1b]0;never terminated\r\nback to normal", cols=20)
    assert term.display()[:2] == ["a", "back to normal"]


def test_an_unterminated_osc_holds_no_memory():
    term = Screen(3, 10)
    term.feed(b"\x1b]0;" + b"A" * 2_000_000)
    assert len(term._params) == 0 and len(term._inter) == 0
    term.feed(b"\x07ok")
    assert term.display()[0] == "ok"


@pytest.mark.parametrize("introducer", [b"P", b"X", b"^", b"_"])
def test_dcs_sos_pm_and_apc_strings_never_reach_the_screen(introducer):
    term = screen(b"a\x1b" + introducer + b"+q544e\x1b\\b")
    assert term.display()[0] == "ab"


def test_an_unterminated_dcs_is_abandoned_at_a_control_character():
    term = screen(b"a\x1bP1$qm\r\nb")
    assert term.display()[:2] == ["a", "b"]


def test_a_bel_ends_a_dcs_too():
    assert screen(b"a\x1bPq\x07b").display()[0] == "ab"


def test_del_inside_a_string_is_ignored():
    assert screen(b"a\x1b]0;t\x7fx\x07b").display()[0] == "ab"


# -- hostile input -------------------------------------------------------------

@pytest.mark.parametrize("sequence", [
    b"\x1b[99999L", b"\x1b[99999M", b"\x1b[99999@", b"\x1b[99999P",
    b"\x1b[99999X", b"\x1b[99999S", b"\x1b[99999T", b"\x1b[99999I",
    b"\x1b[99999Z", b"\x1b[99999E", b"\x1b[99999F",
])
def test_huge_counts_are_quick_and_stay_on_the_screen(sequence):
    started = time.monotonic()
    term = screen(FILLED + b"\x1b[3;3H" + sequence + b"X")
    assert time.monotonic() - started < 1
    assert len(term.lines) == 5
    assert all(len(line.chars) == 10 for line in term.lines)


def test_binary_output_cannot_hang_or_grow_the_screen():
    rng = random.Random(1)
    data = bytes(rng.randrange(256) for _ in range(300_000))
    term = Screen(24, 80)
    started = time.monotonic()
    term.feed(data)
    assert time.monotonic() - started < 20
    assert len(term.lines) == 24
    assert all(len(line.chars) == 80 for line in term.lines)
    assert len(term.scrollback) <= vt.SCROLLBACK
    assert all(len(cell) <= vt.MAX_MARKS + 1
               for line in term.lines for cell in line.chars)
    assert len(term.take_replies()) <= vt.MAX_REPLY_BYTES


def test_os_random_bytes_in_small_reads_do_not_raise():
    term = Screen(5, 7)
    for _ in range(200):
        term.feed(os.urandom(64))
    term.resize(1, 1)
    term.feed(os.urandom(512))
    assert (term.rows, term.cols) == (1, 1)


# -- resizing ------------------------------------------------------------------

def test_shrinking_keeps_the_cursor_line_and_moves_the_top_to_history():
    term = screen(FILLED)
    term.resize(3, 10)
    assert term.display() == ["line3", "line4", "line5"]
    assert [line.text() for line in term.scrollback] == ["line1", "line2"]
    assert cursor(term) == (2, 5)
    assert term.history_count == 2


def test_shrinking_drops_lines_below_the_cursor_first():
    term = screen(FILLED + b"\x1b[2;1H")
    term.resize(3, 10)
    assert term.display() == ["line1", "line2", "line3"]
    assert len(term.scrollback) == 0


def test_growing_adds_blank_lines_below():
    term = screen(b"top")
    term.resize(7, 10)
    assert term.display() == ["top"] + [""] * 6


def test_narrowing_cuts_lines_and_blanks_a_split_wide_character():
    term = screen("abc日x".encode())
    term.resize(5, 4)
    assert term.display()[0] == "abc"
    assert all(len(line.chars) == 4 for line in term.lines + term.alt)


def test_widening_pads_lines_and_extends_the_tab_stops():
    term = Screen(3, 10)
    term.resize(3, 30)
    assert all(len(line.chars) == 30 for line in term.lines)
    term.feed(b"\t\t\tX")
    assert term.display()[0] == " " * 24 + "X"


def test_resizing_to_the_same_size_does_nothing():
    term = screen(FILLED)
    term.resize(5, 10)
    assert term.display()[0] == "line1"


def test_resizing_to_one_cell_and_back():
    term = screen(FILLED)
    term.resize(1, 1)
    assert (term.rows, term.cols) == (1, 1)
    term.feed(b"xyz\x1b[5;5H\x1b[2J\x1b[L\x1b[P")
    term.resize(0, 0)
    assert (term.rows, term.cols) == (1, 1)
    term.resize(5, 10)
    term.feed(b"\x1b[5;10Hok")
    assert len(term.lines) == 5


def test_resizing_resets_the_scroll_region_and_clamps_the_cursor():
    term = screen(b"\x1b[2;4r\x1b[4;9H")
    term.resize(3, 5)
    assert (term.top, term.bottom) == (0, 2)
    assert cursor(term) == (2, 4)
    assert term.pending_wrap is False


def test_resizing_on_the_alternate_screen_keeps_the_shell_screen_right():
    """vim open while the pane shrinks: on exit the prompt is still there,
    with the cursor on it."""
    term = screen(FILLED + b"\x1b[?1049h\x1b[5;1Halt")
    term.resize(3, 10)
    assert term.alt_active is True
    assert cursor(term) == (2, 3)
    term.feed(b"\x1b[?1049l")
    assert term.display() == ["line3", "line4", "line5"]
    assert cursor(term) == (2, 5)


def test_resizing_adjusts_a_cursor_saved_on_the_alternate_screen():
    term = screen(b"\x1b[?47h\x1b[5;3H\x1b7")
    term.resize(2, 10)
    term.feed(b"\x1b8")
    assert cursor(term) == (1, 2)


def test_a_line_knows_its_text_and_how_to_resize():
    line = Line(4)
    line.chars[:2] = ["a", "b"]
    assert line.text() == "ab"
    line.resize(6)
    assert line.chars == ["a", "b", " ", " ", " ", " "]
    line.resize(1)
    assert line.chars == ["a"]


# -- the programs --------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(RECORDINGS))
def test_a_program_draws_its_screen(name):
    """vim, less and top, recorded on a real pty, render as tmux showed them."""
    recording = RECORDINGS[name]
    data = recording["data"]
    term = Screen(recording["rows"], recording["cols"])
    term.feed(data[:recording["running"]])
    assert term.display() == recording["running_screen"]
    assert (term.x, term.y) == recording["running_cursor"]


@pytest.mark.parametrize("name", sorted(RECORDINGS))
def test_a_program_leaves_the_screen_as_it_found_it(name):
    """After vim and less exit the shell's screen is back, prompt and
    cursor included; top, which uses no alternate screen, leaves its last
    picture, as it does in a real terminal."""
    recording = RECORDINGS[name]
    term = Screen(recording["rows"], recording["cols"])
    term.feed(b"before\r\n$ ")
    term.feed(recording["data"])
    if name == "top":
        assert term.display() == recording["exit_screen"]
        return
    assert term.display()[:2] == ["before", "$"]
    assert term.display()[2:] == [""] * (recording["rows"] - 2)
    assert cursor(term) == (1, 2)


def test_vim_draws_in_colour():
    recording = RECORDINGS["vim"]
    term = Screen(recording["rows"], recording["cols"])
    term.feed(recording["data"][:recording["running"]])
    # Line numbers in colour 130; the comment in blue (34).
    assert term.lines[0].attrs[2][0] == 130
    assert term.lines[0].attrs[6][0] == 4


def test_less_shows_colours_and_overstrike_as_attributes():
    recording = RECORDINGS["less"]
    term = Screen(recording["rows"], recording["cols"])
    data = recording["data"]
    # Up to the first page, before the space bar moved it on.
    first_page = data[:data.index(b"\x1b[7mpaged.txt")]
    term.feed(first_page)
    assert term.display()[0].startswith("red bold plain blue256 truecolour")
    assert term.lines[0].attrs[0] == (1, DEFAULT, BOLD)
    assert term.lines[0].attrs[15][0] == 33
    assert term.lines[0].attrs[23][0] == RGB | (10 << 16) | (200 << 8) | 10
    assert term.display()[1] == "Bold and under"
    assert term.lines[1].attrs[0][2] == BOLD
    assert term.lines[1].attrs[9][2] == UNDERLINE
