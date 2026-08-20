"""The stdlib .xlsx reader.

Every fixture is a real zip archive with the part layout a spreadsheet
producer writes, so what is under test is the format, not a stand-in for it.
"""

from __future__ import annotations

import zipfile

import pytest

from meridian_commander import ooxml, xlsx
from meridian_commander.filesystems import LocalFileSystem

from support import (
    XLSX_MAIN,
    XLSX_PKG,
    XLSX_REL,
    rows_xml,
    sheet_xml,
    simple_xlsx,
    write_xlsx,
    xlsx_bytes,
    xlsx_parts,
)


def parse(parts, name="book.xlsx", **kwargs):
    return xlsx.parse_workbook(xlsx_bytes(parts, **kwargs), name)


def only_sheet(parts, **kwargs):
    return parse(parts, **kwargs).sheets[0]


def cells(body: str, **kwargs):
    """Rows of the single sheet built from ``<row>`` XML."""
    return only_sheet(xlsx_parts({"S": sheet_xml(body)}, **kwargs)).rows


# -- names ---------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("book.xlsx", True), ("MACRO.XLSM", True), ("Book.XlsX", True),
    ("legacy.xls", False), ("notes.txt", False), ("xlsx", False),
])
def test_is_spreadsheet(name, expected):
    assert xlsx.is_spreadsheet(name) is expected


@pytest.mark.parametrize("ref, index", [
    ("A1", 0), ("B2", 1), ("Z9", 25), ("AA1", 26), ("AB1", 27),
    ("BA100", 52), ("aa1", 26), ("1", None), ("", None),
])
def test_column_index(ref, index):
    assert xlsx.column_index(ref) == index


@pytest.mark.parametrize("index, name", [
    (0, "A"), (25, "Z"), (26, "AA"), (27, "AB"), (51, "AZ"), (16383, "XFD"),
])
def test_column_name(index, name):
    assert xlsx.column_name(index) == name


def test_column_names_and_indices_are_inverses():
    for i in range(800):
        assert xlsx.column_index(xlsx.column_name(i) + "1") == i


# -- numbers and dates ---------------------------------------------------------
@pytest.mark.parametrize("raw, shown", [
    ("1234.5", "1234.5"), ("-98", "-98"), ("0", "0"), ("42.0", "42"),
    ("0.10000000000000001", "0.1"), ("0.333333333333333", "0.3333333333"),
    ("1e-9", "1e-09"), ("1e20", "1e+20"), ("not a number", "not a number"),
])
def test_format_number(raw, shown):
    assert xlsx.format_number(raw) == shown


@pytest.mark.parametrize("code, kind", [
    ("dd/mm/yyyy", "date"),
    ("yyyy-mm-dd hh:mm:ss", "datetime"),
    ("h:mm AM/PM", "time"),
    ("mmm-yy", "date"),
    ("mm", "date"),                       # a lone "m" is a month
    ("h:mm", "time"),                     # ... but beside an hour it is minutes
    ("0.00", None),
    ("General", None),
    ("#,##0.00", None),
    ('"days"0', None),                    # the letters are quoted text
    ("[$-409]d/m/yyyy", "date"),          # a bracketed locale id
    ("[Red]0.00", None),
    (r"0\d", None),                       # an escaped literal
    ("yyyy;@", "date"),                   # only the first section counts
])
def test_format_kind(code, kind):
    assert xlsx._format_kind(code) == kind


@pytest.mark.parametrize("serial, kind, shown", [
    (45000, "date", "2023-03-15"),
    (45000.5, "datetime", "2023-03-15 12:00:00"),
    (45000.5, "time", "12:00:00"),
    (5, "date", "1900-01-05"),            # below the phantom leap day
    (61, "date", "1900-03-01"),           # the first day it lines up again
    (60, "date", "1900-02-29"),           # the phantom day itself
    (60.5, "datetime", "1900-02-29 12:00:00"),
    (0.9999999, "time", "00:00:00"),      # rounds to midnight, not 24:00:00
])
def test_serial_to_text(serial, kind, shown):
    assert xlsx.serial_to_text(serial, kind) == shown


def test_serial_to_text_in_the_1904_system():
    assert xlsx.serial_to_text(0, "date", date1904=True) == "1904-01-01"
    assert xlsx.serial_to_text(45000, "date", date1904=True) == "2027-03-16"


def test_a_serial_that_is_not_a_date_at_all():
    assert xlsx.serial_to_text(-1, "date") is None
    assert xlsx.serial_to_text(1e15, "date") is None       # past any calendar


# -- cell types ----------------------------------------------------------------
def test_inline_strings_and_sparse_cells():
    rows = cells('<row r="1"><c r="A1" t="inlineStr"><is><t>one</t></is></c>'
                 '<c r="C1" t="inlineStr"><is><t>three</t></is></c></row>')
    assert rows == [["one", "", "three"]]


def test_shared_strings_including_rich_text_runs():
    shared = (f'<?xml version="1.0"?><sst xmlns="{XLSX_MAIN}">'
              "<si><t>plain</t></si>"
              "<si><r><t>Rev</t></r><r><t>enue</t></r></si>"
              "<si><t>x</t><rPh><t>phonetic</t></rPh></si>"
              "<other/>"
              "</sst>")
    rows = cells('<row r="1"><c r="A1" t="s"><v>0</v></c>'
                 '<c r="B1" t="s"><v>1</v></c>'
                 '<c r="C1" t="s"><v>2</v></c></row>', shared=shared)
    assert rows == [["plain", "Revenue", "x"]]


def test_a_shared_string_index_that_points_nowhere():
    shared = f'<?xml version="1.0"?><sst xmlns="{XLSX_MAIN}"><si><t>a</t></si></sst>'
    rows = cells('<row r="1"><c r="A1" t="s"><v>9</v></c>'
                 '<c r="B1" t="s"><v>zz</v></c></row>', shared=shared)
    assert rows == [["", ""]]


def test_a_shared_strings_part_that_is_missing_from_the_archive():
    parts = xlsx_parts({"S": sheet_xml('<row r="1"><c r="A1" t="s"><v>0</v></c></row>')},
                       shared="<sst/>")
    del parts["xl/sharedStrings.xml"]
    assert only_sheet(parts).rows == [[""]]


@pytest.mark.parametrize("body, expected", [
    ('<c r="A1" t="b"><v>1</v></c>', "TRUE"),
    ('<c r="A1" t="b"><v>0</v></c>', "FALSE"),
    ('<c r="A1" t="b"><v></v></c>', ""),          # no text at all is not False
    ('<c r="A1" t="e"><v>#DIV/0!</v></c>', "#DIV/0!"),
    ('<c r="A1" t="str"><v>cached</v></c>', "cached"),
    ('<c r="A1" t="d"><v>2024-01-01</v></c>', "2024-01-01"),
    ('<c r="A1"><v>3.5</v></c>', "3.5"),
    ('<c r="A1"/>', ""),                          # a formula with no result
    ('<c r="A1"><f>B1+1</f></c>', ""),
])
def test_cell_types(body, expected):
    assert cells(f'<row r="1">{body}</row>') == [[expected]]


# -- styles and dates ----------------------------------------------------------
STYLES = (f'<?xml version="1.0"?><styleSheet xmlns="{XLSX_MAIN}">'
          '<numFmts><numFmt numFmtId="164" formatCode="dd/mm/yyyy hh:mm"/>'
          '<numFmt numFmtId="bad" formatCode="junk"/><other/></numFmts>'
          '<cellStyleXfs><xf numFmtId="14"/></cellStyleXfs>'
          '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/>'
          '<xf numFmtId="oops"/><other/></cellXfs>'
          "</styleSheet>")


def test_dates_are_rendered_through_the_number_format():
    rows = cells('<row r="1"><c r="A1" s="0"><v>45000</v></c>'
                 '<c r="B1" s="1"><v>45000</v></c>'
                 '<c r="C1" s="2"><v>45000.5</v></c>'
                 '<c r="D1" s="3"><v>45000</v></c>'
                 '<c r="E1" s="99"><v>45000</v></c></row>', styles=STYLES)
    assert rows == [["45000", "2023-03-15", "2023-03-15 12:00:00",
                     "45000", "45000"]]


def test_cellstylexfs_is_not_mistaken_for_cellxfs():
    # The first <xf> in cellStyleXfs is a date format; cells index into
    # cellXfs, whose first entry is General, so style 0 must stay a number.
    assert cells('<row r="1"><c r="A1" s="0"><v>45000</v></c></row>',
                 styles=STYLES) == [["45000"]]


def test_a_style_index_that_is_not_a_number():
    assert cells('<row r="1"><c r="A1" s="x"><v>45000</v></c></row>',
                 styles=STYLES) == [["45000"]]


def test_a_date_format_over_a_value_that_is_not_a_serial():
    rows = cells('<row r="1"><c r="A1" s="1"><v>text</v></c>'
                 '<c r="B1" s="1"><v>-5</v></c></row>', styles=STYLES)
    assert rows == [["text", "-5"]]


def test_the_1904_date_system():
    rows = cells('<row r="1"><c r="A1" s="1"><v>45000</v></c></row>',
                 styles=STYLES, workbook_pr='<workbookPr date1904="true"/>')
    assert rows == [["2027-03-16"]]


def test_a_styles_part_that_is_missing_from_the_archive():
    parts = xlsx_parts({"S": sheet_xml('<row r="1"><c r="A1"><v>1</v></c></row>')},
                       styles="<styleSheet/>")
    del parts["xl/styles.xml"]
    assert only_sheet(parts).rows == [["1"]]


# -- rows and columns ----------------------------------------------------------
def test_row_gaps_are_filled_so_numbering_matches_the_file():
    sheet = only_sheet(xlsx_parts({"S": sheet_xml(
        '<row r="1"><c r="A1" t="inlineStr"><is><t>top</t></is></c></row>'
        '<row r="4"><c r="A4" t="inlineStr"><is><t>fourth</t></is></c></row>'
    )}))
    assert sheet.rows == [["top"], [], [], ["fourth"]]


def test_rows_and_cells_without_references_fall_back_to_their_order():
    rows = cells("<row><c t=\"inlineStr\"><is><t>a</t></is></c>"
                 "<c t=\"inlineStr\"><is><t>b</t></is></c></row>"
                 '<row r="zz"><c t="inlineStr"><is><t>c</t></is></c></row>')
    assert rows == [["a", "b"], ["c"]]


def test_non_cell_children_of_a_row_are_ignored():
    assert cells('<row r="1"><extLst/><c r="A1"><v>1</v></c></row>') == [["1"]]


def test_the_row_cap_stops_reading_and_is_reported(monkeypatch):
    monkeypatch.setattr(xlsx, "MAX_ROWS", 3)
    sheet = only_sheet(xlsx_parts({"S": sheet_xml(
        rows_xml([[str(i)] for i in range(10)]))}))
    assert [r[0] for r in sheet.rows] == ["0", "1", "2"]
    assert sheet.row_limit is True and sheet.truncated is True


def test_the_column_cap_stops_reading_a_row_and_is_reported(monkeypatch):
    monkeypatch.setattr(xlsx, "MAX_COLS", 2)
    sheet = only_sheet(xlsx_parts({"S": sheet_xml(
        rows_xml([["a", "b", "c", "d"]]))}))
    assert sheet.rows == [["a", "b"]]
    assert sheet.col_limit is True and sheet.truncated is True


def test_an_enormous_cell_is_cut_to_something_drawable(monkeypatch):
    monkeypatch.setattr(xlsx, "MAX_CELL_CHARS", 5)
    assert cells(rows_xml([["abcdefghij"]])) == [["abcde"]]


def test_ncols_is_the_widest_row():
    sheet = only_sheet(xlsx_parts({"S": sheet_xml(
        rows_xml([["a"], ["a", "b", "c"], ["a", "b"]]))}))
    assert sheet.ncols == 3


def test_an_empty_sheet():
    sheet = only_sheet(xlsx_parts({"S": sheet_xml("")}))
    assert sheet.rows == [] and sheet.ncols == 0 and sheet.truncated is False


# -- archive structure ---------------------------------------------------------
def test_several_sheets_keep_their_names_and_order():
    book = xlsx.parse_workbook(
        simple_xlsx({"Sales": [["a"]], "Notes": [["b"]]}), "book.xlsx")
    assert [s.name for s in book.sheets] == ["Sales", "Notes"]
    assert book.name == "book.xlsx" and book.truncated is False


def test_the_workbook_part_is_found_through_the_package_relationships():
    parts = xlsx_parts({"S": sheet_xml(rows_xml([["found"]]))})
    parts["xl/book2.xml"] = parts.pop("xl/workbook.xml")
    parts["xl/_rels/book2.xml.rels"] = parts.pop("xl/_rels/workbook.xml.rels")
    parts["_rels/.rels"] = (
        f'<?xml version="1.0"?><Relationships xmlns="{XLSX_PKG}">'
        f'<Relationship Id="r1" Type="{XLSX_REL}/officeDocument" '
        f'Target="/xl/book2.xml"/></Relationships>')       # an absolute target
    assert only_sheet(parts).rows == [["found"]]


def test_without_package_relationships_the_conventional_path_is_used():
    parts = xlsx_parts({"S": sheet_xml(rows_xml([["found"]]))})
    del parts["_rels/.rels"]
    assert only_sheet(parts).rows == [["found"]]


def test_relationship_entries_that_cannot_be_followed_are_skipped():
    parts = xlsx_parts({"S": sheet_xml(rows_xml([["ok"]]))})
    parts["_rels/.rels"] = (
        f'<?xml version="1.0"?><Relationships xmlns="{XLSX_PKG}">'
        "<NotARelationship/>"
        f'<Relationship Id="r0" Type="{XLSX_REL}/officeDocument"/>'
        f'<Relationship Type="{XLSX_REL}/officeDocument" Target="x.xml"/>'
        f'<Relationship Id="r1" Type="{XLSX_REL}/officeDocument" '
        f'Target="http://elsewhere/book.xlsx" TargetMode="External"/>'
        "</Relationships>")
    # Nothing usable is left, so the conventional workbook path is used.
    assert only_sheet(parts).rows == [["ok"]]


def test_a_sheet_identified_without_the_relationship_namespace():
    parts = xlsx_parts({"S": sheet_xml(rows_xml([["found"]]))})
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
        'r:id="rId1"', 'id="rId1"')
    assert only_sheet(parts).rows == [["found"]]


def test_a_sheet_with_no_relationship_still_holds_its_place():
    parts = xlsx_parts({"Gone": sheet_xml(""), "Here": sheet_xml(rows_xml([["x"]]))})
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
        'r:id="rId1"', "")
    book = parse(parts)
    assert [s.name for s in book.sheets] == ["Gone", "Here"]
    assert book.sheets[0].rows == [] and book.sheets[1].rows == [["x"]]


def test_a_sheet_whose_part_is_missing_from_the_archive():
    parts = xlsx_parts({"S": sheet_xml(rows_xml([["x"]]))})
    del parts["xl/worksheets/sheet1.xml"]
    assert only_sheet(parts).rows == []


def test_unexpected_children_of_the_workbook_are_ignored():
    parts = xlsx_parts({"S": sheet_xml(rows_xml([["x"]]))})
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
        "<sheets>", "<fileVersion/><sheets><notASheet/>")
    assert only_sheet(parts).rows == [["x"]]


# -- refusals ------------------------------------------------------------------
def test_a_file_that_is_not_a_zip_archive():
    with pytest.raises(ooxml.OoxmlError, match="not a readable"):
        xlsx.parse_workbook(b"plain text, not a spreadsheet", "x.xlsx")


def test_an_archive_with_no_workbook_part():
    parts = xlsx_parts({"S": sheet_xml("")})
    del parts["xl/workbook.xml"]
    with pytest.raises(ooxml.OoxmlError, match="no workbook part"):
        parse(parts)


def test_a_workbook_that_declares_no_sheets():
    with pytest.raises(ooxml.OoxmlError, match="declares no sheets"):
        parse(xlsx_parts({}))


def test_a_workbook_part_that_is_not_valid_xml():
    parts = xlsx_parts({"S": sheet_xml("")})
    parts["xl/workbook.xml"] = "<workbook><sheets>"
    with pytest.raises(ooxml.OoxmlError, match="not valid XML"):
        parse(parts)


def test_a_sheet_that_is_not_valid_xml():
    parts = xlsx_parts({"S": "<worksheet><sheetData><row>"})
    with pytest.raises(ooxml.OoxmlError, match="sheet 'S' is not valid XML"):
        parse(parts)


def test_a_part_too_large_to_read(monkeypatch):
    monkeypatch.setattr(ooxml, "MAX_PART_BYTES", 1)
    with pytest.raises(ooxml.OoxmlError, match="too large to read"):
        parse(xlsx_parts({"S": sheet_xml("")}))


def test_a_sheet_too_large_to_read(monkeypatch):
    parts = xlsx_parts({"S": sheet_xml(rows_xml([["x" * 80]] * 400))})
    monkeypatch.setattr(ooxml, "MAX_PART_BYTES", 20_000)
    with pytest.raises(ooxml.OoxmlError, match="sheet 'S' is too large"):
        parse(parts)


def _damaged_parts(where: str):
    """Parts holding the word "marker" in a worksheet, or in shared strings.

    The two land in different readers: a worksheet is decompressed while it is
    parsed, everything else is read whole.
    """
    if where == "sheet":
        return xlsx_parts({"S": sheet_xml(rows_xml([["marker"]]))})
    shared = (f'<?xml version="1.0"?><sst xmlns="{XLSX_MAIN}">'
              "<si><t>marker</t></si></sst>")
    return xlsx_parts(
        {"S": sheet_xml('<row r="1"><c r="A1" t="s"><v>0</v></c></row>')},
        shared=shared)


@pytest.mark.parametrize("where, message", [
    ("sheet", "cannot read sheet 'S'"),
    ("shared", "cannot read xl/sharedStrings.xml"),
])
def test_a_part_whose_stored_bytes_are_damaged(where, message):
    # Same length, different bytes: the archive still opens and its directory
    # still lists the part, but reading it fails the checksum.
    data = bytearray(xlsx_bytes(_damaged_parts(where), compress=False))
    at = data.index(b"marker")
    data[at:at + 6] = b"BROKEN"
    with pytest.raises(ooxml.OoxmlError, match=message):
        xlsx.parse_workbook(bytes(data), "book.xlsx")


@pytest.mark.parametrize("where", ["sheet", "shared"])
def test_the_damaged_archive_fixtures_are_otherwise_readable(where):
    # Guards the test above: without the substitution the same archives parse,
    # so the failure there is the damage and not the storage mode.
    book = xlsx.parse_workbook(
        xlsx_bytes(_damaged_parts(where), compress=False), "book.xlsx")
    assert book.sheets[0].rows == [["marker"]]


# -- reading through a filesystem ----------------------------------------------
def test_read_workbook_through_a_backend(tmp_path):
    path = write_xlsx(str(tmp_path / "data" / "book.xlsx"),
                      {"Sales": [["Region", "Revenue"], ["North", "10"]]})
    book = xlsx.read_workbook(LocalFileSystem(), path)
    assert book.name == "book.xlsx"
    assert book.sheets[0].rows == [["Region", "Revenue"], ["North", "10"]]


def test_a_file_too_large_to_hold_in_memory(tmp_path, monkeypatch):
    path = write_xlsx(str(tmp_path / "book.xlsx"), {"S": [["x"]]})
    monkeypatch.setattr(ooxml, "MAX_BYTES", 10)
    with pytest.raises(ooxml.OoxmlError, match="cannot be read in part"):
        xlsx.read_workbook(LocalFileSystem(), path)


def test_a_file_that_is_not_there(tmp_path):
    with pytest.raises(OSError):
        xlsx.read_workbook(LocalFileSystem(), str(tmp_path / "nope.xlsx"))


def test_the_fixture_builder_writes_a_readable_archive(tmp_path):
    path = write_xlsx(str(tmp_path / "book.xlsx"), {"S": [["a"]]})
    with zipfile.ZipFile(path) as zf:
        assert "xl/workbook.xml" in zf.namelist()
