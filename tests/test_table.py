# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The table model: reading TSV and CSV, display widths, filters, sorting
and column summaries.

Fixtures stay neutral -- fruit, cities and numbers -- rather than copies of
real results files.
"""

from __future__ import annotations

import io

import pytest

from meridian_commander import table
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.table import (
    Filter,
    SortKey,
    Statistics,
    Table,
    View,
    bucket_counts,
    clean,
    column_kind,
    decimals,
    display_width,
    find_column,
    fit,
    format_number,
    is_numeric,
    is_table,
    number,
    parse,
    parse_filter,
    quick_filter,
    read_bytes,
    read_table,
    shown_value,
    sniff_delimiter,
    sort_rows,
    split_preamble,
    summarise,
    wrap,
)

from support import write


def write_bytes(path, data: bytes) -> str:
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


class CountingFS(LocalFileSystem):
    """A local backend that records every read the table makes."""

    def __init__(self, close_fails: bool = False, closable: bool = True):
        super().__init__()
        self.reads: list[int] = []
        self.close_fails = close_fails
        self.closable = closable

    def open_read(self, path):
        outer = self
        real = open(path, "rb")

        class Stream:
            def read(self, n):
                data = real.read(n)
                outer.reads.append(len(data))
                return data

        stream = Stream()
        if self.closable:
            def close():
                real.close()
                if outer.close_fails:
                    raise OSError("the connection dropped on close")
            stream.close = close
        return stream


# -- which names ----------------------------------------------------------------
@pytest.mark.parametrize("name, claimed", [
    ("a.tsv", True), ("a.csv", True), ("a.tab", True), ("a.psv", True),
    ("A.TSV", True), ("Results.Csv", True),
    ("a.txt", False), ("a.tsv.gz", False), ("tsv", False), ("a.xlsx", False),
])
def test_which_names_are_tables(name, claimed):
    assert is_table(name) is claimed


# -- reading: bounded -----------------------------------------------------------
def test_a_small_file_is_read_whole(tmp_path):
    path = write_bytes(tmp_path / "a.tsv", b"a\tb\n1\t2\n")
    assert read_bytes(LocalFileSystem(), path) == (b"a\tb\n1\t2\n", False)


def test_the_byte_limit_stops_at_a_line_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_BYTES", 10)
    path = write_bytes(tmp_path / "a.tsv", b"aaaa\nbbbb\ncccc\n")
    data, more = read_bytes(LocalFileSystem(), path)
    # Ten bytes end just after "bbbb\n": nothing whole is dropped, and the
    # file is known to go on.
    assert data == b"aaaa\nbbbb\n" and more is True


def test_a_line_cut_by_the_byte_limit_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_BYTES", 12)
    path = write_bytes(tmp_path / "a.tsv", b"aaaa\nbbbb\ncccc\n")
    assert read_bytes(LocalFileSystem(), path) == (b"aaaa\nbbbb\n", True)


def test_a_file_exactly_at_the_byte_limit_is_not_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_BYTES", 10)
    path = write_bytes(tmp_path / "a.tsv", b"aaaa\nbbbb\n")
    assert read_bytes(LocalFileSystem(), path) == (b"aaaa\nbbbb\n", False)


def test_reading_stops_once_there_are_more_lines_than_rows_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_ROWS", 2)
    monkeypatch.setattr(table, "PREAMBLE_LINES", 0)
    monkeypatch.setattr(table, "CHUNK", 4)
    path = write_bytes(tmp_path / "a.tsv", b"h\n1\n2\n3\n4\n5\n6\n7\n8\n9\n")
    fs = CountingFS()
    data, more = read_bytes(fs, path)
    # The header, two rows and one more to know there is more: four lines,
    # read in four-byte chunks, then one byte to see whether the file ends.
    assert data.count(b"\n") >= 4 and more is True
    assert sum(fs.reads) <= 4 * 4 + 1


def test_opening_a_huge_file_reads_no_more_than_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_BYTES", 1000)
    path = write_bytes(tmp_path / "big.tsv", b"x" * 20 + b"\n" + b"y" * 100_000)
    fs = CountingFS()
    read_table(fs, path)
    assert sum(fs.reads) <= 1001


def test_a_backend_that_fails_on_close_still_gives_the_data(tmp_path):
    path = write_bytes(tmp_path / "a.tsv", b"a\n")
    assert read_bytes(CountingFS(close_fails=True), path) == (b"a\n", False)


def test_a_stream_with_no_close_is_read(tmp_path):
    path = write_bytes(tmp_path / "a.tsv", b"a\n")
    assert read_bytes(CountingFS(closable=False), path) == (b"a\n", False)


# -- reading: the two parsers ---------------------------------------------------
def test_tsv_is_split_on_tabs_and_a_quote_is_just_a_character():
    # Through the csv module the quote would swallow the tab and both line
    # breaks, and three rows would come out as one.
    text = 'name\tnote\npear\t"ripe\nfig\tsweet\n'
    assert parse(text, "\t", quoted=False) == [
        ["name", "note"], ["pear", '"ripe'], ["fig", "sweet"]]


def test_csv_keeps_a_quoted_delimiter_and_line_break_in_one_cell():
    text = 'city,note\nParis,"big, old"\nOslo,"two\nlines"\n'
    assert parse(text, ",", quoted=True) == [
        ["city", "note"], ["Paris", "big, old"], ["Oslo", "two\nlines"]]


@pytest.mark.parametrize("quoted", [False, True])
def test_line_endings_blank_lines_and_the_last_newline(quoted):
    text = "a,b\r\n\r\n1,2\r\n" if quoted else "a\tb\r\n\r\n1\t2\r\n"
    delimiter = "," if quoted else "\t"
    # A blank line is one empty field; the final line break adds no row.
    assert parse(text, delimiter, quoted) == [["a", "b"], [""], ["1", "2"]]


@pytest.mark.parametrize("quoted", [False, True])
def test_no_text_is_no_records(quoted):
    assert parse("", ",", quoted) == []


def test_a_file_without_a_final_newline_keeps_its_last_line():
    assert parse("a\tb\n1\t2", "\t", quoted=False) == [["a", "b"], ["1", "2"]]


def test_a_nul_in_a_csv_is_replaced_rather_than_fatal():
    assert parse("a,b\x00c\n", ",", quoted=True) == [["a", "b�c"]]


@pytest.mark.parametrize("name, delimiter, quoted", [
    ("a.tsv", "\t", False), ("a.TAB", "\t", False), ("a.psv", "|", False),
    ("a.csv", ",", True),
])
def test_the_name_chooses_the_reading(fs, tmp_path, name, delimiter, quoted):
    write(str(tmp_path / name), 'x\ty|z,w\n')
    read = read_table(fs, str(tmp_path / name))
    assert (read.delimiter, read.quoted) == (delimiter, quoted)


def test_a_psv_is_split_on_pipes_without_quoting(fs, tmp_path):
    write(str(tmp_path / "a.psv"), 'a|b\n"x|y"|z\n')
    assert read_table(fs, str(tmp_path / "a.psv")).records == [
        ["a", "b"], ['"x', 'y"', "z"]]


@pytest.mark.parametrize("first, expected", [
    ("a,b,c", ","), ("a;b;c", ";"), ("a\tb\tc", "\t"), ("a|b|c", "|"),
    ("a;b,c", ","),          # a tie goes to the comma
    ("abc", ","),            # nothing to go on
])
def test_the_delimiter_of_a_csv_comes_from_its_first_line(first, expected):
    # The second line would say otherwise; the first line decides.
    assert sniff_delimiter(first + "\n1;2;3;4;5;6\n") == expected


def test_an_empty_csv_is_taken_to_be_comma_separated():
    assert sniff_delimiter("") == ","


def test_sniffing_skips_blank_leading_lines():
    assert sniff_delimiter("\n  \nfruit;price\napple;3\n") == ";"


def test_sniffing_passes_over_comment_lines():
    assert sniff_delimiter("# made by a tool, on a day\nfruit;price\n") == ";"
    # A file that is all comment is judged by its first line.
    assert sniff_delimiter("# a;b;c\n") == ";"


# -- comment lines at the top ---------------------------------------------------
def test_comment_lines_at_the_top_are_set_aside(fs, tmp_path):
    write(str(tmp_path / "run.tsv"),
          "# made by a tool\n\n# two fruits\nfruit\tprice\napple\t3\n")
    read = read_table(fs, str(tmp_path / "run.tsv"))
    assert read.comments == ["# made by a tool", "# two fruits"]
    assert read.records == [["fruit", "price"], ["apple", "3"]]


def test_a_header_written_as_a_comment_stays_the_header():
    records = [["##format 2"], ["#city", "pop"], ["Oslo", "709037"]]
    assert split_preamble(records) == ([["##format 2"]],
                                       [["#city", "pop"], ["Oslo", "709037"]])


def test_a_one_field_comment_over_one_field_rows_is_still_a_comment():
    assert split_preamble([["# note"], ["1"]]) == ([["# note"]], [["1"]])


def test_a_comment_line_further_down_is_a_row():
    records = [["a"], ["# not at the top"], ["1"]]
    assert split_preamble(records) == ([], records)


def test_a_file_of_nothing_but_comments(fs, tmp_path):
    write(str(tmp_path / "c.csv"), "# only, notes\n\n# here\n")
    read = read_table(fs, str(tmp_path / "c.csv"))
    assert read.records == [] and len(read.comments) == 2


def test_comments_are_looked_for_only_near_the_top(monkeypatch):
    monkeypatch.setattr(table, "PREAMBLE_LINES", 2)
    records = [["# a"], ["# b"], ["# c"], ["x"]]
    assert split_preamble(records) == (records[:2], records[2:])


def test_comment_lines_do_not_count_against_the_row_limit(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_ROWS", 2)
    write(str(tmp_path / "a.tsv"), "# one\n# two\n# three\nh\n1\n2\n")
    read = read_table(fs, str(tmp_path / "a.tsv"))
    assert read.records == [["h"], ["1"], ["2"]] and read.truncated is False


def test_a_semicolon_csv_is_read_as_one(fs, tmp_path):
    write(str(tmp_path / "eu.csv"), "fruit;price\napple;1,5\n")
    read = read_table(fs, str(tmp_path / "eu.csv"))
    assert read.records == [["fruit", "price"], ["apple", "1,5"]]
    assert read.delimiter_name == "semicolon"


def test_a_byte_order_mark_is_dropped(fs, tmp_path):
    path = write_bytes(tmp_path / "a.csv", b"\xef\xbb\xbfid,name\n1,fig\n")
    assert read_table(fs, path).records[0] == ["id", "name"]


def test_invalid_utf8_is_replaced_not_raised(fs, tmp_path):
    path = write_bytes(tmp_path / "a.tsv", b"name\ncaf\xe9\n")
    assert read_table(fs, path).records[1] == ["caf�"]


def test_rows_past_the_limit_are_left_and_the_table_says_so(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_ROWS", 2)
    write(str(tmp_path / "a.tsv"), "h\n1\n2\n3\n")
    read = read_table(fs, str(tmp_path / "a.tsv"))
    assert read.records == [["h"], ["1"], ["2"]] and read.truncated is True


def test_exactly_the_limit_is_not_truncated(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(table, "MAX_ROWS", 2)
    write(str(tmp_path / "a.tsv"), "h\n1\n2\n")
    assert read_table(fs, str(tmp_path / "a.tsv")).truncated is False


def test_a_csv_cut_by_the_byte_limit_drops_its_last_record(fs, tmp_path, monkeypatch):
    # The cut lands at a line break inside a quoted cell; the record it
    # splits must not be shown as if it were whole.
    text = b'a,b\n1,x\n2,"one\ntwo"\n3,y\n'
    monkeypatch.setattr(table, "MAX_BYTES", text.index(b"two") + 2)
    read = read_table(fs, write_bytes(tmp_path / "a.csv", text))
    assert read.records == [["a", "b"], ["1", "x"]] and read.truncated


def test_a_cell_past_the_csv_field_limit_is_a_readable_error(fs, tmp_path):
    write(str(tmp_path / "a.csv"), '"' + "x" * 200_000 + '"\n')
    with pytest.raises(ValueError, match="not readable as CSV"):
        read_table(fs, str(tmp_path / "a.csv"))


def test_empty_and_header_only_files(fs, tmp_path):
    write(str(tmp_path / "e.tsv"), "")
    write(str(tmp_path / "h.tsv"), "a\tb\n")
    empty = read_table(fs, str(tmp_path / "e.tsv"))
    assert empty.records == [] and empty.ncols == 0
    assert read_table(fs, str(tmp_path / "h.tsv")).records == [["a", "b"]]


def test_the_column_count_is_the_widest_record():
    assert Table([["a"], ["1", "2", "3"], ["x", "y"]], "\t").ncols == 3


def test_an_unusual_delimiter_is_named_as_itself():
    assert Table([], ":").delimiter_name == "':'"


# -- display widths -------------------------------------------------------------
def test_control_characters_are_shown_as_spaces():
    assert clean("a\tb\nc\x1bd\x85e") == "a b c d e"


@pytest.mark.parametrize("text, width", [
    ("abc", 3), ("", 0), ("漢字", 4), ("é", 1), ("😀", 2), ("a漢", 3),
])
def test_display_width_counts_terminal_columns(text, width):
    assert display_width(text) == width


def test_display_width_can_stop_counting_early():
    assert display_width("x" * 1_000_000, limit=5) == 6


@pytest.mark.parametrize("text, width, right, expected", [
    ("ab", 4, False, "ab  "),
    ("ab", 4, True, "  ab"),
    ("abcdef", 4, False, "abc~"),
    ("abcd", 4, False, "abcd"),
    ("漢字", 4, False, "漢字"),
    ("漢字", 3, False, "漢~"),
    ("漢字x", 4, False, "漢~ "),     # 字 would straddle the edge: padded
    ("abc", 1, False, "~"),
    ("abc", 0, False, ""),
    ("abc", -2, False, ""),
])
def test_fitting_text_to_a_width(text, width, right, expected):
    assert fit(text, width, right) == expected
    if width > 0:
        assert display_width(expected) == width


def test_wrapping_breaks_at_the_width_and_at_line_breaks():
    assert wrap("abcdefg", 3) == ["abc", "def", "g"]
    assert wrap("ab\ncd\r\nef", 5) == ["ab", "cd", "ef"]
    assert wrap("漢字漢字", 5) == ["漢字", "漢字"]
    assert wrap("", 4) == [""]
    assert wrap("a\tb", 4) == ["a b"]
    # A character wider than the whole width still gets a line of its own.
    assert wrap("漢", 1) == ["漢"]


@pytest.mark.parametrize("text, tail", [
    ("10.25", 3), ("7", 0), (".5", 2), ("1e5", 0), ("1.5E3", 0), (" 2.5 ", 2),
])
def test_the_decimal_tail_of_a_number(text, tail):
    assert decimals(text) == tail


def test_numbers_and_numeric_columns():
    assert number("2.5") == 2.5 and number("x") is None and number("nan") is None
    assert is_numeric(["1", "", " 2.5 ", "-3"]) is True
    assert is_numeric(["1", "two"]) is False
    assert is_numeric(["", "  "]) is False


# -- filters ---------------------------------------------------------------------
LABELS = ["fruit", "price", "Origin"]
ROWS = [
    ["apple", "3", "Spain"],
    ["Pear", "10", "france"],
    ["fig", "", "Turkey"],
    ["plum", "2.50", "Spain "],
    ["kiwi"],
]


def kept(expression):
    f = parse_filter(expression, LABELS)
    return [row[0] for row in ROWS if f.keeps(row)]


def test_free_text_matches_any_cell_with_smart_case():
    assert kept("spain") == ["apple", "plum"]
    assert kept("Spain") == ["apple", "plum"]
    assert kept("france") == ["Pear"]
    assert kept("France") == []
    assert kept("pea") == ["Pear"]


@pytest.mark.parametrize("expression, fruits", [
    ("origin ~ an", ["Pear"]),
    ("origin !~ an", ["apple", "fig", "plum", "kiwi"]),
    ("price = 10", ["Pear"]),
    ("price = 2.5", ["plum"]),                   # numerically equal to 2.50
    ("price != 10", ["apple", "fig", "plum", "kiwi"]),
    ("origin = spain", ["apple", "plum"]),      # smart case, spaces ignored
    ("origin = Spain", ["apple", "plum"]),
    ("origin = SPAIN", []),
    ("price < 10", ["apple", "plum"]),           # 3 < 10, as numbers
    ("price > 2.5", ["apple", "Pear"]),
    ("price <= 3", ["apple", "plum"]),
    ("price >= 10", ["Pear"]),
    ("price =", ["fig", "kiwi"]),                # empty, or no field at all
    ("price != ''", ["apple", "Pear", "plum"]),
    ("fruit < kiwi", ["apple", "fig"]),          # text order, ignoring case
    ("fruit >= p", ["Pear", "plum"]),
    ("fruit < Q", ["Pear"]),                     # with a capital: exact, so
                                                 # capitals sort first
    ("FRUIT = fig", ["fig"]),                    # the column, ignoring case
])
def test_column_filters(expression, fruits):
    assert kept(expression) == fruits


def test_a_number_compares_as_a_number_not_as_text():
    # As text "9" sorts after "10"; as numbers it does not.
    f = parse_filter("n < 10", ["n"])
    assert f.keeps(["9"]) and not f.keeps(["10"])
    assert parse_filter("n < x10", ["n"]).keeps(["10"]) is True


def test_a_blank_never_meets_a_numeric_comparison():
    assert parse_filter("n < 10", ["n"]).keeps([""]) is False


def test_operators_are_read_longest_first():
    assert parse_filter("n<=3", ["n"]).op == "<="
    assert parse_filter("n!~x", ["n"]).op == "!~"
    assert parse_filter("n >= 3", ["n"]).text == "n >= 3"


def test_a_quoted_value_keeps_its_spaces():
    f = parse_filter('name = " fig "', ["name"])
    assert f.value == " fig " and f.text == 'name = " fig "'
    assert parse_filter("name = 'x'", ["name"]).value == "x"


def test_a_filter_on_a_column_that_does_not_exist_says_so():
    with pytest.raises(ValueError, match="no column called 'colour'"):
        parse_filter("colour = red", LABELS)


def test_an_operator_with_nothing_before_it_is_free_text():
    f = parse_filter("=x", LABELS)
    assert f.column is None and f.value == "=x"


def test_the_quick_filter_takes_the_value_exactly():
    keep = quick_filter("origin", 2, "Spain", keep=True)
    hide = quick_filter("origin", 2, "Spain", keep=False)
    assert [row[0] for row in ROWS if keep.keeps(row)] == ["apple"]
    assert [row[0] for row in ROWS if hide.keeps(row)] == [
        "Pear", "fig", "plum", "kiwi"]
    assert keep.text == "origin = Spain" and hide.text == "origin != Spain"
    assert quick_filter("price", 1, "", keep=True).text == 'price = ""'


def test_a_quick_filter_on_a_number_is_still_exact():
    assert quick_filter("n", 0, "2.5", keep=True).keeps(["2.50"]) is False


def test_a_filter_describes_itself():
    assert repr(Filter("a = b", 0, "=", "b")) == "Filter('a = b')"


@pytest.mark.parametrize("name, index", [
    ("fruit", 0), ("Origin", 2), ("origin", 2), ("ORIGIN", 2), ("colour", None),
])
def test_finding_a_column_by_name(name, index):
    assert find_column(name, LABELS) == index


def test_an_exact_name_wins_over_one_that_differs_in_case():
    assert find_column("a", ["A", "a"]) == 1


# -- sorting --------------------------------------------------------------------
RECORDS = [
    ["name", "size", "kind"],
    ["fig", "10", "b"],
    ["Apple", "9", "a"],
    ["date", "", "a"],
    ["cherry", "100", "b"],
    ["banana", "9", "b"],
]


def names(order):
    return [RECORDS[index][0] for index in order]


def test_a_numeric_column_sorts_as_numbers():
    assert names(sort_rows([1, 2, 3, 4, 5], RECORDS, [SortKey(1)])) == [
        "Apple", "banana", "fig", "cherry", "date"]


def test_descending_keeps_the_blanks_last_and_the_ties_in_file_order():
    assert names(sort_rows([1, 2, 3, 4, 5], RECORDS, [SortKey(1, True)])) == [
        "cherry", "fig", "Apple", "banana", "date"]


def test_a_text_column_sorts_ignoring_case():
    assert names(sort_rows([1, 2, 3, 4, 5], RECORDS, [SortKey(0)])) == [
        "Apple", "banana", "cherry", "date", "fig"]


def test_the_sort_is_stable():
    assert names(sort_rows([1, 2, 3, 4, 5], RECORDS, [SortKey(2)])) == [
        "Apple", "date", "fig", "cherry", "banana"]


def test_a_second_key_breaks_the_first_ones_ties():
    order = sort_rows([1, 2, 3, 4, 5], RECORDS, [SortKey(2), SortKey(1, True)])
    assert names(order) == ["Apple", "date", "cherry", "fig", "banana"]


def test_a_column_with_text_in_it_sorts_as_text():
    records = [["n"], ["10"], ["9"], ["x"]]
    assert sort_rows([1, 2, 3], records, [SortKey(0)]) == [1, 2, 3]


def test_a_short_record_sorts_as_blank():
    records = [["a", "b"], ["1"], ["2", "x"]]
    assert sort_rows([1, 2], records, [SortKey(1)]) == [2, 1]


# -- the view -------------------------------------------------------------------
def test_the_view_applies_every_filter_then_the_sort():
    view = View()
    view.filters.append(parse_filter("kind = b", ["name", "size", "kind"]))
    view.filters.append(parse_filter("size > 9", ["name", "size", "kind"]))
    assert names(view.rows(RECORDS, 1)) == ["fig", "cherry"]
    view.keys.append(SortKey(1, True))
    assert names(view.rows(RECORDS, 1)) == ["cherry", "fig"]


def test_the_view_starts_at_the_first_row_it_is_given():
    assert View().rows(RECORDS, 0)[0] == 0
    assert View().rows(RECORDS, 1)[0] == 1


def test_sorting_by_a_column_cycles_up_down_and_off():
    view = View()
    view.sort_by(1)
    assert view.direction(1) is False
    view.sort_by(1)
    assert view.direction(1) is True
    view.sort_by(1)
    assert view.direction(1) is None and view.keys == []


def test_sorting_by_another_column_keeps_the_first_as_the_tie_break():
    view = View()
    view.sort_by(1)
    view.sort_by(2)
    assert [(k.column, k.descending) for k in view.keys] == [(2, False), (1, False)]
    # Coming back to the older one makes it the first again, ascending.
    view.sort_by(1)
    assert [(k.column, k.descending) for k in view.keys] == [(1, False), (2, False)]


def test_reading_and_parsing_agree_on_a_whole_file(fs, tmp_path):
    # The two halves of reading together, through a real file, so a change
    # to one that the other does not expect shows up here.
    text = io.StringIO()
    text.write("city\tpop\n")
    for index in range(50):
        text.write(f"town{index}\t{index * 10}\n")
    write(str(tmp_path / "towns.tsv"), text.getvalue())
    read = read_table(fs, str(tmp_path / "towns.tsv"))
    assert len(read.records) == 51 and read.records[50] == ["town49", "490"]


# -- column summaries -------------------------------------------------------------
@pytest.mark.parametrize("values, kind", [
    (["3", " 10.5 ", "-0.25", "1e3", ""], "number"),
    (["2026-11-10", "2026-11-10T18:40:25Z", "2026-11-10 18:40",
      "2026-11-10T18:40:25.5+01:00", ""], "date"),
    (["apple", "3"], "text"),
    (["10/11/2026"], "text"),          # day or month first: not guessed
    (["nan", "inf"], "text"),          # words, as the grid treats them
    (["", "  "], "empty"),
    ([], "empty"),
])
def test_what_a_column_holds(values, kind):
    assert column_kind(values) == kind


def test_a_number_column_is_what_the_sort_calls_numeric():
    # The summary and the sort must never disagree about a column.
    for values in (["1", "2"], ["1", "x"], ["", "4"], ["-nan"], ["+inf"]):
        assert (column_kind(values) == "number") == is_numeric(values)


@pytest.mark.parametrize("value, text", [
    (3.0, "3"), (-0.0, "0"), (10092000.0, "10092000"), (0.25, "0.25"),
    (4.1875, "4.1875"), (1 / 3, "0.3333333333"), (1e20, "1e+20"),
    (123456789012345.0, "123456789012345"), (float("inf"), "inf"),
    (float("nan"), "nan"),
])
def test_statistics_are_written_as_a_reader_expects(value, text):
    assert format_number(value) == text


def test_buckets_divide_the_range_into_equal_parts():
    assert bucket_counts([0, 1, 2, 3, 9, 10], buckets=5) == [
        (0.0, 2), (2.0, 2), (4.0, 0), (6.0, 0), (8.0, 2)]


def test_the_largest_number_goes_in_the_last_bucket():
    assert bucket_counts([0, 10], buckets=2) == [(0.0, 1), (5.0, 1)]


def test_one_value_many_times_is_one_bucket():
    assert bucket_counts([7, 7, 7]) == [(7, 3)]


def test_no_numbers_or_a_spread_too_wide_for_a_float_make_no_buckets():
    assert bucket_counts([]) == []
    assert bucket_counts([-1e308, 1e308]) == []


# The summary fixture: four prices, a repeat, and a blank.
PRICES = ["3", "10.5", "0.25", "", "3"]


def test_a_summary_of_a_number_column():
    summary = summarise(PRICES)
    assert summary.kind == "number"
    assert (summary.count, summary.empty, summary.distinct) == (5, 1, 3)
    assert summary.top == [("3", 2), ("10.5", 1), ("0.25", 1)]
    # Over the four numbers: (3 + 10.5 + 0.25 + 3) / 4, and the middle two
    # of 0.25, 3, 3, 10.5.
    assert summary.numbers == Statistics(0.25, 10.5, 4.1875, 3.0)
    assert summary.buckets == [
        (0.25, 1), (1.275, 0), (2.3, 2), (3.325, 0), (4.35, 0),
        (5.375, 0), (6.4, 0), (7.425, 0), (8.45, 0), (9.475, 1)]
    assert sum(count for _edge, count in summary.buckets) == 4


def test_an_odd_count_has_a_middle_value_for_its_median():
    assert summarise(["9", "1", "5"]).numbers.median == 5


def test_a_summary_of_a_text_column_has_no_statistics():
    summary = summarise(["Paris", "Oslo", "Paris", "paris", ""])
    assert summary.kind == "text" and summary.numbers is None
    assert summary.buckets == []
    # Counted exactly as written: picking one keeps exactly those rows.
    assert summary.top == [("Paris", 2), ("Oslo", 1), ("paris", 1)]
    assert summary.distinct == 3 and summary.empty == 1


def test_a_summary_lists_the_most_frequent_values_most_first():
    values = [str(n) for n in range(20) for _ in range(n % 4 + 1)]
    summary = summarise(values, top=3)
    # 3, 7, 11, ... each appear four times; ties keep the rows' order.
    assert summary.top == [("3", 4), ("7", 4), ("11", 4)]
    assert summary.distinct == 20
    assert len(summarise(values).top) == table.TOP_VALUES


def test_a_summary_of_blanks_or_of_nothing():
    for values in (["", " "], []):
        summary = summarise(values)
        assert summary.kind == "empty" and summary.top == []
        assert summary.numbers is None and summary.distinct == 0
    assert summarise(["", " "]).empty == 2


def test_a_cell_that_says_nan_is_no_number_to_average():
    summary = summarise(["-nan", "2", "4"])
    assert summary.kind == "number"
    assert summary.numbers == Statistics(2.0, 4.0, 3.0, 3.0)
    assert summarise(["-nan"]).numbers is None


def test_infinities_give_honest_statistics_and_no_histogram_of_them():
    summary = summarise(["-inf", "+inf", "1"])
    assert summary.numbers.minimum == float("-inf")
    assert summary.numbers.maximum == float("inf")
    assert format_number(summary.numbers.mean) == "nan"
    assert summary.buckets == [(1.0, 1)]


def test_a_value_with_spaces_around_it_is_shown_quoted():
    assert shown_value("Paris") == "Paris"
    assert shown_value(" fig ") == '" fig "'
    assert shown_value("") == '""'
