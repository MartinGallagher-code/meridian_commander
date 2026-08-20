"""The stdlib .docx reader and the document viewer.

Fixtures are real zip archives with the part layout Word writes, so what is
under test is the format rather than a stand-in for it.
"""

from __future__ import annotations

import curses

import pytest

from meridian_commander import docx, ooxml
from meridian_commander.browsers import viewer_for
from meridian_commander.docx import BULLET, HEADING, NUMBER, PARAGRAPH
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.viewer import Viewer

from support import (
    XLSX_PKG,
    docx_numbering,
    docx_p,
    docx_parts,
    docx_table,
    script_newwin,
    simple_docx,
    with_curses_screen,
    write,
    write_docx,
    xlsx_bytes,
)


def parse(body, numbering="", name="report.docx"):
    return docx.parse_document(simple_docx(body, numbering), name)


def blocks(body, numbering=""):
    return parse(body, numbering).blocks


def lines(body, numbering=""):
    return docx.to_lines(parse(body, numbering))


# -- names ---------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("report.docx", True), ("MACRO.DOCM", True), ("Report.DocX", True),
    ("legacy.doc", False), ("notes.txt", False), ("book.xlsx", False),
])
def test_is_document(name, expected):
    assert docx.is_document(name) is expected


# -- paragraph text ------------------------------------------------------------
def test_runs_are_glued_back_together():
    body = ('<w:p><w:r><w:t>Mixed </w:t></w:r><w:r><w:t>runs</w:t></w:r>'
            '<w:r><w:t> and café — dash.</w:t></w:r></w:p>')
    assert lines(body) == ["Mixed runs and café — dash."]


def test_tabs_and_breaks_become_spaces():
    body = ('<w:p><w:r><w:t>before</w:t><w:tab/><w:t>after</w:t>'
            '<w:br/><w:t>next</w:t></w:r></w:p>')
    # The viewer wraps for itself, so a break inside a paragraph is a gap in
    # the flow rather than a line of its own.
    assert lines(body) == ["before after next"]


def test_a_hyperlink_keeps_its_text():
    body = ('<w:p><w:r><w:t>see </w:t></w:r>'
            '<w:hyperlink r:id="rId9"><w:r><w:t>the docs</w:t></w:r>'
            '</w:hyperlink></w:p>')
    assert lines(body) == ["see the docs"]


def test_an_empty_paragraph_is_kept():
    assert lines("<w:p/>" + docx_p("after")) == ["", "after"]


# -- structure -----------------------------------------------------------------
@pytest.mark.parametrize("style, level", [
    ("Heading1", 1), ("Heading2", 2), ("heading3", 3), ("Heading9", 9),
])
def test_headings_are_recognised(style, level):
    block = blocks(docx_p("Title", style))[0]
    assert (block.kind, block.level) == (HEADING, level)


@pytest.mark.parametrize("style", ["Heading", "HeadingChar", "Normal", "Title"])
def test_things_that_are_not_headings(style):
    assert blocks(docx_p("text", style))[0].kind != HEADING


def test_a_heading_numbered_zero_is_still_a_heading():
    assert blocks(docx_p("t", "Heading0"))[0].level == 1


def test_a_paragraph_with_no_properties():
    assert blocks("<w:p><w:r><w:t>plain</w:t></w:r></w:p>")[0].kind == PARAGRAPH


@pytest.mark.parametrize("style, kind", [
    ("ListBullet", BULLET), ("ListNumber", NUMBER), ("Normal", PARAGRAPH),
])
def test_lists_recognised_by_style_alone(style, kind):
    assert blocks(docx_p("item", style))[0].kind == kind


def test_numbering_tells_a_bullet_from_a_numbered_item():
    # Both paragraphs are style "ListParagraph"; only numbering.xml separates
    # them, which is how Word actually writes a document.
    body = (docx_p("a bullet", "ListParagraph", num=("1", 0))
            + docx_p("an item", "ListParagraph", num=("2", 0)))
    numbering = docx_numbering({"1": ["bullet"], "2": ["decimal"]})
    assert [b.kind for b in blocks(body, numbering)] == [BULLET, NUMBER]


def test_a_list_level_becomes_an_indent():
    body = (docx_p("top", "ListParagraph", num=("1", 0))
            + docx_p("nested", "ListParagraph", num=("1", 1)))
    numbering = docx_numbering({"1": ["bullet", "bullet"]})
    assert lines(body, numbering) == ["  * top", "    * nested"]


def test_a_list_with_no_numbering_part_is_numbered():
    # Without a definition there is nothing to say "bullet", and a numbered
    # item at least keeps the items countable.
    assert blocks(docx_p("x", "ListParagraph", num=("1", 0)))[0].kind == NUMBER


def test_a_numbering_reference_with_no_id_is_not_a_list():
    body = ('<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr>'
            "<w:r><w:t>x</w:t></w:r></w:p>")
    assert blocks(body)[0].kind == PARAGRAPH


def test_a_numbering_level_that_is_not_a_number():
    body = ('<w:p><w:pPr><w:numPr><w:ilvl w:val="deep"/>'
            '<w:numId w:val="1"/></w:numPr></w:pPr>'
            "<w:r><w:t>x</w:t></w:r></w:p>")
    assert blocks(body)[0].level == 0


def test_a_numbering_part_that_is_missing_from_the_archive():
    parts = docx_parts(docx_p("x", num=("1", 0)), docx_numbering({"1": ["bullet"]}))
    del parts["word/numbering.xml"]
    doc = docx.parse_document(xlsx_bytes(parts), "d.docx")
    assert doc.blocks[0].kind == NUMBER


def test_unreadable_numbering_entries_are_skipped():
    numbering = (f'<?xml version="1.0"?><w:numbering xmlns:w="{docx.__name__}">'
                 '<w:abstractNum w:abstractNumId="A">'
                 '<w:lvl w:ilvl="oops"><w:numFmt w:val="bullet"/></w:lvl>'
                 "<w:other/></w:abstractNum>"
                 '<w:num w:numId="1"><w:abstractNumId w:val="A"/></w:num>'
                 "<w:other/></w:numbering>")
    # The bad level falls back to 0, which is the level the paragraph uses.
    assert blocks(docx_p("x", num=("1", 0)), numbering)[0].kind == BULLET


def test_a_numbering_definition_pointing_at_nothing():
    numbering = docx_numbering({"1": ["bullet"]}).replace('w:val="A1"', 'w:val="Z"')
    assert blocks(docx_p("x", num=("1", 0)), numbering)[0].kind == NUMBER


# -- tables --------------------------------------------------------------------
def test_a_table_becomes_aligned_columns():
    body = docx_table([["Region", "Revenue"], ["North", "1234.5"]])
    assert lines(body) == [
        "  Region  Revenue",
        "  ------  -------",
        "  North   1234.5",
        "",
    ]


def test_a_ragged_table_pads_the_short_rows():
    # The padding is there for alignment but trailing blanks are trimmed, so
    # a short row does not leave the cursor sitting in empty space.
    assert lines(docx_table([["a", "b"], ["c"]])) == [
        "  a  b", "  -  -", "  c", ""]


def test_non_table_children_are_ignored():
    body = ("<w:tbl><w:tblPr/><w:tr><w:tblPrEx/>"
            "<w:tc><w:p><w:r><w:t>only</w:t></w:r></w:p></w:tc>"
            "</w:tr></w:tbl>")
    assert lines(body) == ["  only", "  ----", ""]


def test_an_empty_table_draws_nothing():
    assert docx._table_lines([]) == []
    assert lines("<w:tbl/>") == [""]


def test_a_table_row_is_capped(monkeypatch):
    monkeypatch.setattr(docx, "MAX_TABLE_COLS", 2)
    assert blocks(docx_table([["a", "b", "c", "d"]]))[0].rows == [["a", "b"]]


def test_a_runaway_cell_cannot_push_the_table_off_the_screen(monkeypatch):
    monkeypatch.setattr(docx, "MAX_CELL_WIDTH", 5)
    assert lines(docx_table([["x" * 40, "b"]])) == ["  xxxxx  b", "  -----  -", ""]


# -- rendering -----------------------------------------------------------------
def test_a_heading_is_ruled_underneath():
    assert lines(docx_p("Title", "Heading1")) == ["Title", "====="]
    assert lines(docx_p("Sub", "Heading2")) == ["Sub", "---"]


def test_an_empty_heading_still_gets_a_rule():
    assert lines(docx_p("", "Heading1")) == ["", "="]


def test_a_document_never_opens_with_a_blank_line():
    assert lines(docx_p("Title", "Heading1"))[0] == "Title"


def test_blank_lines_are_not_stacked():
    body = docx_table([["a"]]) + docx_p("Head", "Heading1")
    # The table already ends with a blank line, so the heading adds none.
    assert lines(body) == ["  a", "  -", "", "Head", "===="]


def test_numbering_counts_up_and_restarts_after_other_content():
    body = (docx_p("one", "ListNumber") + docx_p("two", "ListNumber")
            + docx_p("interrupted") + docx_p("one again", "ListNumber"))
    assert lines(body) == ["  1. one", "  2. two", "interrupted", "  1. one again"]


def test_the_block_cap_is_reported(monkeypatch):
    monkeypatch.setattr(docx, "MAX_BLOCKS", 2)
    doc = parse(docx_p("a") + docx_p("b") + docx_p("c"))
    assert [b.text for b in doc.blocks] == ["a", "b"]
    assert doc.truncated is True
    assert docx.to_lines(doc)[-1] == "(document truncated)"


# -- refusals ------------------------------------------------------------------
def test_a_file_that_is_not_a_zip_archive():
    with pytest.raises(ooxml.OoxmlError, match="not a readable"):
        docx.parse_document(b"just some text", "x.docx")


def test_an_archive_with_no_document_part():
    parts = docx_parts(docx_p("x"))
    del parts["word/document.xml"]
    with pytest.raises(ooxml.OoxmlError, match="no document part"):
        docx.parse_document(xlsx_bytes(parts), "x.docx")


def test_a_document_with_no_body():
    parts = docx_parts(docx_p("x"))
    parts["word/document.xml"] = (
        f'<?xml version="1.0"?><w:document xmlns:w="{XLSX_PKG}"/>')
    with pytest.raises(ooxml.OoxmlError, match="no body"):
        docx.parse_document(xlsx_bytes(parts), "x.docx")


def test_a_document_part_that_is_not_valid_xml():
    parts = docx_parts(docx_p("x"))
    parts["word/document.xml"] = "<w:document><w:body>"
    with pytest.raises(ooxml.OoxmlError, match="not valid XML"):
        docx.parse_document(xlsx_bytes(parts), "x.docx")


def test_a_file_too_large_to_hold_in_memory(tmp_path, monkeypatch):
    path = write_docx(str(tmp_path / "d.docx"), docx_p("x"))
    monkeypatch.setattr(ooxml, "MAX_BYTES", 10)
    with pytest.raises(ooxml.OoxmlError, match="cannot be read in part"):
        docx.read_document(LocalFileSystem(), path)


# -- reading through a filesystem ----------------------------------------------
def test_read_document_through_a_backend(tmp_path):
    path = write_docx(str(tmp_path / "sub" / "report.docx"),
                      docx_p("Title", "Heading1") + docx_p("Body text."))
    doc = docx.read_document(LocalFileSystem(), path)
    assert doc.name == "report.docx"
    assert [b.text for b in doc.blocks] == ["Title", "Body text."]


# -- the viewer ----------------------------------------------------------------
@pytest.fixture
def report(fs, tmp_path):
    body = (docx_p("Quarterly Report", "Heading1")
            + docx_p("Revenue grew across every region this year, with the "
                     "North leading and the South trailing behind.")
            + docx_p("Detail", "Heading2")
            + docx_p("a bullet", "ListBullet")
            + docx_table([["Region", "Revenue"], ["North", "1234.5"]]))
    return docx.DocxView(fs, write_docx(str(tmp_path / "report.docx"), body))


def test_a_document_opens_in_the_document_viewer(fs, tmp_path):
    path = write_docx(str(tmp_path / "report.docx"), docx_p("x"))
    write(str(tmp_path / "notes.txt"), "plain")
    assert isinstance(viewer_for(fs, path), docx.DocxView)
    assert isinstance(viewer_for(fs, str(tmp_path / "notes.txt")), Viewer)


def test_the_document_viewer_renders_its_structure(report):
    assert report.error is None
    assert report.source[:2] == ["Quarterly Report", "================"]
    assert "  * a bullet" in report.source
    assert "  Region  Revenue" in report.source


def test_wrapping_starts_on_for_a_document(report):
    assert report.wrap is True


def test_a_document_that_cannot_be_read_reports_itself(fs, tmp_path):
    write(str(tmp_path / "fake.docx"), "not a document at all")
    view = docx.DocxView(fs, str(tmp_path / "fake.docx"))
    assert "not a readable" in view.error
    assert view.source == []


def test_a_document_that_is_not_there_reports_itself(fs, tmp_path):
    view = docx.DocxView(fs, str(tmp_path / "missing.docx"))
    assert view.error and view.source == []


def test_the_viewer_reports_a_truncated_document(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(docx, "MAX_BLOCKS", 1)
    view = docx.DocxView(fs, write_docx(str(tmp_path / "d.docx"),
                                        docx_p("a") + docx_p("b")))
    assert view.truncated is True


def test_the_document_viewer_runs_on_a_real_screen(monkeypatch, report):
    captured = script_newwin(monkeypatch, [curses.KEY_DOWN, ord("w"), ord("q")])
    with_curses_screen(12, 50, report.run)
    assert "Quarterly Report" in captured["window"].text
