# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Joining two tables on columns picked in each, with no screen (MC-033).

Fixtures stay neutral -- people, orders and fruit -- rather than copies of
real results files.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from meridian_commander import table, tablejoin
from meridian_commander.table import read_table
from meridian_commander.tablejoin import Side, join, join_key, match_value


def side(rows, columns, header=True):
    """A Side over ``rows`` (lists of cells), joining on ``columns``, with
    every row after the header taken, in the file's order."""
    first = 1 if header else 0
    width = max((len(row) for row in rows), default=0)
    return Side(rows, list(range(first, len(rows))), columns, width)


PEOPLE = [["id", "name"], ["1", "ann"], ["2", "bob"], ["2", "bo"],
          ["3", "cy"]]
ORDERS = [["uid", "item"], ["2", "pen"], ["2", "ink"], ["2", "cap"],
          ["1", "cup"], ["9", "hat"]]


def cells(joined):
    return [" ".join(row) for row in joined.rows]


# -- what is joined --------------------------------------------------------------
def test_one_column_joins_every_matching_pair():
    joined = join(side(PEOPLE, [0]), side(ORDERS, [0]))
    assert cells(joined) == ["1 ann 1 cup",
                             "2 bob 2 pen", "2 bob 2 ink", "2 bob 2 cap",
                             "2 bo 2 pen", "2 bo 2 ink", "2 bo 2 cap"]
    assert joined.matched == 7 and not joined.truncated


def test_a_value_m_times_in_a_and_n_in_b_gives_m_times_n_rows():
    # 2 appears twice in A and three times in B: six rows, in A's order, and
    # within each A row in B's.  Keeping only the first match would give two.
    joined = join(side(PEOPLE, [0]), side(ORDERS, [0]))
    twos = [row for row in joined.rows if row[0] == "2"]
    assert len(twos) == 2 * 3
    assert [(row[1], row[3]) for row in twos] == [
        ("bob", "pen"), ("bob", "ink"), ("bob", "cap"),
        ("bo", "pen"), ("bo", "ink"), ("bo", "cap")]


def test_each_row_records_the_a_and_b_record_it_came_from():
    joined = join(side(PEOPLE, [0]), side(ORDERS, [0]))
    assert joined.sources[:2] == [(1, 4), (2, 1)]


def test_the_rows_given_choose_which_records_and_in_what_order():
    # A quarter's filters and sort, as the rows it shows.
    a = Side(PEOPLE, [3, 1], [0], 2)
    b = Side(ORDERS, [4, 2], [0], 2)
    assert cells(join(a, b)) == ["2 bo 2 ink", "1 ann 1 cup"]


def test_two_columns_pair_up_in_the_order_picked():
    a = [["city", "day", "rain"], ["Oslo", "mon", "3"], ["Oslo", "tue", "0"],
         ["Lima", "mon", "1"]]
    b = [["date", "town", "sun"], ["mon", "Oslo", "2"], ["tue", "Lima", "9"],
         ["tue", "Oslo", "5"]]
    # A's city with B's town, A's day with B's date: B's columns were picked
    # in the other order from where they sit.
    joined = join(side(a, [0, 1]), side(b, [1, 0]))
    assert cells(joined) == ["Oslo mon 3 mon Oslo 2", "Oslo tue 0 tue Oslo 5"]
    # Paired the other way, nothing matches: a city is never a day.
    assert join(side(a, [0, 1]), side(b, [0, 1])).rows == []


def test_a_row_matches_only_when_every_pair_does():
    a = [["k1", "k2"], ["x", "1"], ["x", "2"]]
    b = [["k1", "k2"], ["x", "2"]]
    assert cells(join(side(a, [0, 1]), side(b, [0, 1]))) == ["x 2 x 2"]


# -- which rows are kept -----------------------------------------------------------
def test_matching_rows_only_drops_the_unmatched():
    joined = join(side(PEOPLE, [0]), side(ORDERS, [0]), keep="inner")
    assert "3" not in [row[0] for row in joined.rows]
    assert "9" not in [row[2] for row in joined.rows]


def test_every_row_of_a_keeps_an_unmatched_a_row_with_b_blank():
    joined = join(side(PEOPLE, [0]), side(ORDERS, [0]), keep="left")
    assert joined.rows[-1] == ["3", "cy", "", ""]
    assert joined.sources[-1] == (4, None)
    assert joined.matched == 7 and len(joined.rows) == 8
    assert "9" not in [row[2] for row in joined.rows]


def test_every_row_of_both_adds_the_unmatched_b_rows_after():
    joined = join(side(PEOPLE, [0]), side(ORDERS, [0]), keep="full")
    assert joined.rows[-2:] == [["3", "cy", "", ""], ["", "", "9", "hat"]]
    assert joined.sources[-1] == (None, 5)
    assert len(joined.rows) == 9


def test_unmatched_b_rows_come_in_bs_order():
    b = [["uid", "item"], ["8", "a"], ["1", "b"], ["7", "c"]]
    joined = join(side(PEOPLE, [0]), side(b, [0]), keep="full")
    assert [row[3] for row in joined.rows if not row[0]] == ["a", "c"]


# -- blanks and short rows -------------------------------------------------------
def test_a_blank_key_matches_nothing_and_is_counted():
    a = [["id", "v"], ["", "a1"], ["  ", "a2"], ["1", "a3"]]
    b = [["id", "w"], ["", "b1"], ["1", "b2"], [" ", "b3"]]
    joined = join(side(a, [0]), side(b, [0]))
    # Blank with blank would have made four rows nobody asked for.
    assert cells(joined) == ["1 a3 1 b2"]
    assert (joined.blank_a, joined.blank_b) == (2, 2)


def test_blank_keys_are_kept_where_unmatched_rows_are():
    a = [["id", "v"], ["", "a1"]]
    b = [["id", "w"], ["", "b1"]]
    joined = join(side(a, [0]), side(b, [0]), keep="full")
    assert joined.rows == [["", "a1", "", ""], ["", "", "", "b1"]]


def test_one_blank_of_two_join_cells_is_still_a_key():
    a = [["k1", "k2"], ["x", ""]]
    b = [["k1", "k2"], ["x", ""]]
    assert cells(join(side(a, [0, 1]), side(b, [0, 1]))) == ["x  x "]
    assert join_key(["x", ""], [0, 1], "exact") == ("x", "")
    assert join_key(["", " "], [0, 1], "exact") is None


def test_a_short_row_is_padded_never_shifted():
    a = [["id", "x", "y"], ["1"], ["2", "p", "q"]]
    b = [["id", "z"], ["1", "r"], ["2"]]
    joined = join(side(a, [0]), side(b, [0]))
    assert joined.rows == [["1", "", "", "1", "r"], ["2", "p", "q", "2", ""]]


def test_a_join_column_a_short_row_lacks_is_blank():
    a = [["v", "id"], ["a"], ["b", "1"]]
    b = [["id"], ["1"]]
    joined = join(side(a, [1]), side(b, [0]))
    assert cells(joined) == ["b 1 1"] and joined.blank_a == 1


def test_with_no_header_every_line_is_a_row():
    a = [["1", "ann"], ["2", "bob"]]
    b = [["2", "pen"]]
    joined = join(side(a, [0], header=False), side(b, [0], header=False))
    assert cells(joined) == ["2 bob 2 pen"]


# -- the row limit -------------------------------------------------------------------
def test_the_join_stops_at_the_limit_and_says_so():
    a = [["k"]] + [["x"]] * 30
    b = [["k"]] + [["x"]] * 30            # 900 combinations
    joined = join(side(a, [0]), side(b, [0]), limit=50)
    assert len(joined.rows) == 50 and joined.truncated


def test_the_limit_defaults_to_the_table_browsers(monkeypatch):
    monkeypatch.setattr(table, "MAX_ROWS", 4)
    joined = join(side(PEOPLE, [0]), side(ORDERS, [0]))
    assert len(joined.rows) == 4 and joined.truncated


def test_a_join_exactly_at_the_limit_is_whole():
    joined = join(side(PEOPLE, [0]), side(ORDERS, [0]), limit=7)
    assert len(joined.rows) == 7 and not joined.truncated


@pytest.mark.parametrize("keep, limit, last", [
    ("left", 7, ["2", "bo", "2", "cap"]),       # stopped before A's "3"
    ("full", 8, ["3", "cy", "", ""]),           # stopped before B's "9"
])
def test_unmatched_rows_stop_at_the_limit_too(keep, limit, last):
    joined = join(side(PEOPLE, [0]), side(ORDERS, [0]), keep=keep,
                  limit=limit)
    assert joined.truncated and len(joined.rows) == limit
    assert joined.rows[-1] == last


def test_a_huge_join_does_only_the_work_the_limit_allows():
    # Two thousand equal keys each side would be four million rows.
    a = [["k"]] + [["x"]] * 2000
    b = [["k"]] + [["x"]] * 2000
    joined = join(side(a, [0]), side(b, [0]), limit=1000)
    assert len(joined.rows) == 1000 and joined.truncated


# -- how strict a match is ---------------------------------------------------------
def test_exact_matches_the_text_as_it_is():
    a = [["k"], ["Ann"], [" 7"], ["7"]]
    b = [["k"], ["ann"], ["7"], ["7.0"]]
    assert cells(join(side(a, [0]), side(b, [0]), rule="exact")) == ["7 7"]


def test_loose_ignores_case_and_spaces_at_either_end():
    a = [["k"], ["Ann "], [" 7"]]
    b = [["k"], ["aNN"], ["7"], ["7.0"]]
    assert cells(join(side(a, [0]), side(b, [0]), rule="loose")) == [
        "Ann  aNN", " 7 7"]


def test_loose_is_not_by_number():
    a = [["k"], ["7"]]
    b = [["k"], ["007"], ["7.0"]]
    assert join(side(a, [0]), side(b, [0]), rule="loose").rows == []


def test_numbers_match_by_value():
    a = [["k"], ["7"], ["Ann"]]
    b = [["k"], ["007"], ["7.0"], [" +7 "], ["7e0"], ["ANN"], ["8"]]
    joined = join(side(a, [0]), side(b, [0]), rule="numbers")
    assert [row[1] for row in joined.rows] == ["007", "7.0", " +7 ", "7e0",
                                               "ANN"]


def test_numbers_keep_long_identifiers_apart():
    # A float would round these twenty digits into one value.
    a = [["k"], ["12345678901234567890"]]
    b = [["k"], ["12345678901234567891"], ["12345678901234567890.0"]]
    joined = join(side(a, [0]), side(b, [0]), rule="numbers")
    assert [row[1] for row in joined.rows] == ["12345678901234567890.0"]


def test_numbers_never_match_text_that_is_not_a_number():
    assert match_value("7", "numbers") == Decimal(7)
    assert match_value("seven", "numbers") == "seven"
    a = [["k"], ["7"]]
    b = [["k"], ["seven"], ["inf"]]
    assert join(side(a, [0]), side(b, [0]), rule="numbers").rows == []


def test_not_a_number_is_compared_as_text():
    # The one "number" not equal to itself would otherwise never match.
    assert match_value("-NaN", "numbers") == "-nan"
    a = [["k"], ["-nan"]]
    b = [["k"], ["-NaN"]]
    assert len(join(side(a, [0]), side(b, [0]), rule="numbers").rows) == 1


# -- asking for something it cannot do --------------------------------------------
@pytest.mark.parametrize("a_columns, b_columns", [([], []), ([0], []),
                                                  ([0], [0, 1])])
def test_the_join_columns_must_pair_up(a_columns, b_columns):
    with pytest.raises(ValueError, match="same number"):
        join(side(PEOPLE, a_columns), side(ORDERS, b_columns))


def test_an_unknown_choice_is_refused():
    with pytest.raises(ValueError, match="rows"):
        join(side(PEOPLE, [0]), side(ORDERS, [0]), keep="sideways")
    with pytest.raises(ValueError, match="match rule"):
        join(side(PEOPLE, [0]), side(ORDERS, [0]), rule="roughly")


def test_every_choice_has_a_name_on_the_screen():
    assert set(tablejoin.KEEP_NAMES) == set(tablejoin.KEEPS)
    assert set(tablejoin.RULE_NAMES) == set(tablejoin.RULES)


# -- the output's names --------------------------------------------------------------
def test_the_output_names_each_column_for_its_file():
    assert tablejoin.labels(["id", "name"], ["id", "item"]) == [
        "A.id", "A.name", "B.id", "B.item"]


def test_the_pairing_says_which_a_column_goes_with_which_b_column():
    assert tablejoin.pairing(["city", "day"], [1, 0], ["date", "town"],
                             [0, 1]) == "A.day = B.date, A.city = B.town"


# -- reading: the files as the table browser reads them ---------------------------
def _read(fs, tmp_path, name, text):
    path = tmp_path / name
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    return read_table(fs, str(path))


def _side_of(t, columns):
    return Side(t.records, list(range(1, len(t.records))), columns, t.ncols)


def test_a_tsv_joins_a_csv(fs, tmp_path):
    a = _read(fs, tmp_path, "a.tsv", "id\tname\n1\tann\n2\tbob\n")
    b = _read(fs, tmp_path, "b.csv", "uid,item\n2,pen\n")
    assert cells(join(_side_of(a, [0]), _side_of(b, [0]))) == ["2 bob 2 pen"]


def test_a_semicolon_csv_joins(fs, tmp_path):
    a = _read(fs, tmp_path, "a.tsv", "id\tname\n1\tann\n")
    b = _read(fs, tmp_path, "b.csv", "uid;item\n1;cup\n")
    assert cells(join(_side_of(a, [0]), _side_of(b, [0]))) == ["1 ann 1 cup"]


def test_a_byte_order_mark_does_not_stick_to_the_first_key(fs, tmp_path):
    a = _read(fs, tmp_path, "a.csv", b"\xef\xbb\xbfid,name\n1,ann\n")
    b = _read(fs, tmp_path, "b.csv", "id,item\n1,cup\n")
    assert a.records[0][0] == "id"
    assert cells(join(_side_of(a, [0]), _side_of(b, [0]))) == ["1 ann 1 cup"]


def test_quoted_commas_and_line_breaks_stay_in_their_cells(fs, tmp_path):
    a = _read(fs, tmp_path, "a.csv",
              'id,note\n1,"a, b"\n2,"two\nlines"\n')
    b = _read(fs, tmp_path, "b.csv", 'id,item\n"2","x,y"\n')
    joined = join(_side_of(a, [0]), _side_of(b, [0]))
    assert joined.rows == [["2", "two\nlines", "2", "x,y"]]


def test_a_file_cut_short_at_the_read_limit_joins_the_rows_read(
        fs, tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_ROWS", 3)
    rows = "".join(f"{n}\tv{n}\n" for n in range(1, 11))
    a = _read(fs, tmp_path, "a.tsv", "id\tv\n" + rows)
    b = _read(fs, tmp_path, "b.tsv", "id\tw\n2\tb2\n9\tb9\n")
    assert a.truncated
    joined = join(_side_of(a, [0]), _side_of(b, [0]))
    assert cells(joined) == ["2 v2 2 b2"]       # 9 was never read


def test_a_side_fills_a_short_row_to_its_width():
    assert Side([["1"]], [0], [0], 3).cells(0) == ["1", "", ""]

