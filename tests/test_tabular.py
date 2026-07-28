"""The tabular helpers shared by the CSV/TSV data plug-ins."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.plugins import _tabular as tabular

from support import read, write


# -- reading and writing -------------------------------------------------------

def test_read_text_caps_and_reports_truncation(fs, tmp_path):
    path = tmp_path / "big.csv"
    path.write_text("x" * 5000)
    text, truncated = tabular.read_text(fs, str(path), max_bytes=1000)
    assert len(text) == 1000
    assert truncated is True

    whole, truncated = tabular.read_text(fs, str(path), max_bytes=1_000_000)
    assert len(whole) == 5000
    assert truncated is False


def test_read_text_at_exactly_the_cap_is_not_truncated(fs, tmp_path):
    path = tmp_path / "exact.csv"
    path.write_text("y" * 100)
    text, truncated = tabular.read_text(fs, str(path), max_bytes=100)
    assert text == "y" * 100
    assert truncated is False


def test_read_text_replaces_undecodable_bytes(fs, tmp_path):
    path = tmp_path / "latin.csv"
    path.write_bytes(b"caf\xe9\n")
    text, _ = tabular.read_text(fs, str(path))
    assert "caf" in text and "�" in text


def test_a_stream_that_fails_to_close_is_ignored(fs, tmp_path):
    class _BadClose(LocalFileSystem):
        def open_read(self, path):
            handle = super().open_read(path)

            class _Wrapper:
                def read(self, n):
                    return handle.read(n)

                def close(self):
                    raise OSError("close failed")

            return _Wrapper()

    path = tmp_path / "a.csv"
    path.write_text("a,b\n")
    text, _ = tabular.read_text(_BadClose(), str(path))
    assert text == "a,b\n"


def test_a_stream_with_no_close_at_all_is_fine(fs, tmp_path):
    class _NoClose(LocalFileSystem):
        def open_read(self, path):
            handle = super().open_read(path)
            return SimpleNamespace(read=handle.read)

    path = tmp_path / "a.csv"
    path.write_text("a,b\n")
    assert tabular.read_text(_NoClose(), str(path))[0] == "a,b\n"


# -- delimiters ----------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("tab", "\t"),
    ("\\t", "\t"),
    ("comma", ","),
    (",", ","),
    ("semicolon", ";"),
    (";", ";"),
    ("pipe", "|"),
    ("|", "|"),
    ("space", " "),
    ("TAB", "\t"),          # names are case-insensitive
    ("~", "~"),             # an unknown name is taken as the character
    ("abc", "a"),           # ...and only its first character
])
def test_resolve_delimiter(value, expected):
    assert tabular.resolve_delimiter(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", 0])
def test_resolve_delimiter_means_auto_when_unset(value):
    assert tabular.resolve_delimiter(value) is None


def test_sniff_delimiter_recognises_the_usual_separators():
    assert tabular.sniff_delimiter("a,b,c\n1,2,3\n") == ","
    assert tabular.sniff_delimiter("a\tb\tc\n1\t2\t3\n") == "\t"
    assert tabular.sniff_delimiter("a;b;c\n1;2;3\n") == ";"


def test_sniff_delimiter_falls_back_on_a_ragged_sample():
    # The sniffer gives up here; the fallback counts candidates on line one.
    ragged = "a|b|c\n1|2\n\n3\n"
    assert tabular.sniff_delimiter(ragged) == "|"


def test_sniff_delimiter_uses_the_default_when_there_is_nothing_to_go_on():
    assert tabular.sniff_delimiter("single-column\nvalues\n", default=";") == ";"
    assert tabular.sniff_delimiter("") == ","


# -- the table model -----------------------------------------------------------

def test_read_table_round_trip(fs, tmp_path):
    write(str(tmp_path / "t.csv"), "a,b\n1,x\n2,y\n")
    table = tabular.read_table(fs, str(tmp_path / "t.csv"))
    assert table.header == ["a", "b"]
    assert table.rows == [["1", "x"], ["2", "y"]]
    assert table.ncols == 2
    tabular.write_table(fs, str(tmp_path / "out.csv"), table.header, table.rows)
    assert read(str(tmp_path / "out.csv")) == "a,b\n1,x\n2,y\n"


def test_read_table_without_a_header_row_invents_names(fs, tmp_path):
    write(str(tmp_path / "t.csv"), "1,x\n2,y\n")
    table = tabular.read_table(fs, str(tmp_path / "t.csv"), has_header=False)
    assert table.header == ["col1", "col2"]
    assert len(table.rows) == 2


def test_read_an_empty_table(fs, tmp_path):
    write(str(tmp_path / "empty.csv"), "")
    table = tabular.read_table(fs, str(tmp_path / "empty.csv"))
    assert table.header == []
    assert table.rows == []
    # And with has_header off, still nothing to invent names from.
    headerless = tabular.read_table(fs, str(tmp_path / "empty.csv"),
                                    has_header=False)
    assert headerless.header == []


def test_write_table_with_no_header(fs, tmp_path):
    tabular.write_table(fs, str(tmp_path / "out.csv"), [], [["1", "2"]])
    assert read(str(tmp_path / "out.csv")) == "1,2\n"


def test_column_lookup_is_case_insensitive_and_explicit_when_missing():
    table = tabular.Table(header=["Name", "age"], rows=[["a", "1"]])
    assert table.index("Name") == 0          # exact match wins
    assert table.index("NAME") == 1 - 1      # case-insensitive fallback
    assert table.index("AGE") == 1
    with pytest.raises(ValueError, match="no such column: 'height'"):
        table.index("height")


def test_column_indices_from_a_comma_separated_spec():
    table = tabular.Table(header=["a", "b", "c"], rows=[])
    assert table.indices("a, c") == [0, 2]
    assert table.indices(" b ,, ") == [1]
    assert table.indices("") == []


def test_column_pads_short_rows():
    table = tabular.Table(header=["a", "b"], rows=[["1", "2"], ["3"]])
    assert table.column(1) == ["2", ""]


# -- output paths --------------------------------------------------------------

def test_derive_output_path_puts_the_tag_before_the_extension(fs, tmp_path):
    src = str(tmp_path / "sales.csv")
    assert tabular.derive_output_path(fs, src, "cleaned") == \
        str(tmp_path / "sales.cleaned.csv")


def test_derive_output_path_for_a_file_with_no_extension(fs, tmp_path):
    src = str(tmp_path / "sales")
    assert tabular.derive_output_path(fs, src, "cleaned") == \
        str(tmp_path / "sales.cleaned")


def test_derive_output_path_numbers_around_an_existing_file(fs, tmp_path):
    write(str(tmp_path / "sales.cleaned.csv"), "taken")
    write(str(tmp_path / "sales.cleaned2.csv"), "also taken")
    assert tabular.derive_output_path(fs, str(tmp_path / "sales.csv"),
                                      "cleaned") == \
        str(tmp_path / "sales.cleaned3.csv")


# -- value helpers -------------------------------------------------------------

@pytest.mark.parametrize("value", ["", "  ", "na", "NA", "null", "NULL", "none"])
def test_is_null_covers_the_usual_spellings(value):
    assert tabular.is_null(value) is True


@pytest.mark.parametrize("value", ["0", "false", "nan-ish", "text"])
def test_is_null_is_not_over_eager(value):
    assert tabular.is_null(value) is False


def test_numeric_values_skips_nulls_and_non_numbers():
    assert tabular.numeric_values(["1", "", "2.5", "abc", "NA", "-3"]) == \
        [1.0, 2.5, -3.0]


@pytest.mark.parametrize("value, expected", [
    (True, "True"),
    (3, "3"),
    (3.0, "3"),
    (3.5, "3.5"),
    (2.500000, "2.5"),
    (1.234567891, "1.234568"),
])
def test_format_number(value, expected):
    assert tabular.format_number(value) == expected


@pytest.mark.parametrize("values, expected", [
    ([], "empty"),
    (["", "NA"], "empty"),
    (["1", "2", "-3"], "int"),
    (["1.5", "2"], "float"),
    (["true", "no", "Y"], "bool"),
    (["2024-01-01", "2024-12-31 10:30"], "date"),
    (["2024-01-01T10:30:00"], "date"),
    (["alpha", "1"], "str"),
])
def test_infer_type(values, expected):
    assert tabular.infer_type(values) == expected


def test_infer_type_stops_once_nothing_can_match():
    # A long run after the deciding value must not change the answer.
    assert tabular.infer_type(["zzz"] + ["1"] * 1000) == "str"


# -- JSON lines ----------------------------------------------------------------

def test_to_jsonl_and_back():
    header = ["a", "b"]
    rows = [["1", "x"], ["2", "y"]]
    text = tabular.to_jsonl(header, rows)
    assert [json.loads(ln) for ln in text.splitlines()] == [
        {"a": "1", "b": "x"}, {"a": "2", "b": "y"}]
    assert tabular.from_jsonl(text) == (header, rows)


def test_to_jsonl_of_nothing_is_empty():
    assert tabular.to_jsonl(["a"], []) == ""


def test_to_jsonl_pads_short_rows():
    assert json.loads(tabular.to_jsonl(["a", "b"], [["1"]])) == {"a": "1", "b": ""}


def test_from_jsonl_takes_the_union_of_keys_in_first_seen_order():
    text = '{"a": 1, "b": 2}\n\n{"b": 3, "c": 4}\n'
    header, rows = tabular.from_jsonl(text)
    assert header == ["a", "b", "c"]
    assert rows == [["1", "2", ""], ["", "3", "4"]]


def test_from_jsonl_wraps_a_bare_scalar():
    header, rows = tabular.from_jsonl('"hello"\n42\n')
    assert header == ["value"]
    assert rows == [["hello"], ["42"]]


@pytest.mark.parametrize("value, expected", [
    (None, ""),
    (True, "true"),
    (False, "false"),
    (7, "7"),
    ({"k": 1}, '{"k": 1}'),
    ([1, 2], "[1, 2]"),
])
def test_scalar_rendering(value, expected):
    assert tabular._scalar(value) == expected


# -- selection -----------------------------------------------------------------

def _ctx_with(entries):
    return SimpleNamespace(other_selected=lambda: entries)


def test_selected_file_skips_directories():
    entries = [SimpleNamespace(name="d", is_dir=True),
               SimpleNamespace(name="a.csv", is_dir=False)]
    assert tabular.selected_file(_ctx_with(entries)).name == "a.csv"
    assert [e.name for e in tabular.selected_files(_ctx_with(entries))] == ["a.csv"]


def test_selected_file_with_only_directories_finds_nothing():
    entries = [SimpleNamespace(name="d", is_dir=True)]
    assert tabular.selected_file(_ctx_with(entries)) is None
    assert tabular.selected_files(_ctx_with(entries)) == []


def test_selection_survives_a_context_that_raises():
    def explode():
        raise RuntimeError("pane went away")

    ctx = SimpleNamespace(other_selected=explode)
    assert tabular.selected_file(ctx) is None
    assert tabular.selected_files(ctx) == []


# -- presentation --------------------------------------------------------------

def test_format_rows_aligns_columns():
    lines = tabular.format_rows(["name", "n"], [["alice", "1"], ["bo", "22"]])
    assert lines[0] == "name   n"
    assert set(lines[1]) <= {"-", " "}
    assert lines[2] == "alice  1"
    assert lines[3] == "bo     22"


def test_format_rows_truncates_wide_cells_and_pads_short_ones():
    lines = tabular.format_rows(["a", "b"], [["x" * 40], ["short", "extra"]],
                                max_col=10)
    assert lines[2].startswith("x" * 10)
    assert "extra" in lines[3]


def test_histogram_of_nothing_is_empty():
    assert tabular.histogram([]) == []


def test_histogram_of_a_single_value_is_one_bar():
    lines = tabular.histogram([5, 5, 5])
    assert len(lines) == 1
    assert lines[0].startswith("  5 |")
    assert lines[0].endswith("3")


def test_histogram_buckets_a_spread_of_values():
    lines = tabular.histogram(range(100), buckets=4, width=10)
    assert len(lines) == 4
    # Every value is accounted for across the buckets.
    assert sum(int(line.rsplit(" ", 1)[1]) for line in lines) == 100
    assert "#" in lines[0]


def test_histogram_puts_the_maximum_in_the_last_bucket():
    lines = tabular.histogram([0, 10], buckets=2)
    counts = [int(line.rsplit(" ", 1)[1]) for line in lines]
    assert counts == [1, 1]


def test_read_table_drops_a_trailing_blank_line(fs, tmp_path):
    write(str(tmp_path / "t.csv"), "a,b\n1,x\n\n")
    table = tabular.read_table(fs, str(tmp_path / "t.csv"))
    # The blank line at the end is not a row of data.
    assert table.rows == [["1", "x"]]
