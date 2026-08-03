"""PDF text extraction, page images and the browser.

The extractor was checked against reportlab and qpdf output alongside pypdf's
own extraction -- plain text, an embedded subset font, one draw call per word,
two columns and a scanned page.  It matched pypdf on the text and reconstructed
the spaces and columns that pypdf loses.  These are the behavioural statements;
none of those libraries is a dependency.
"""

from __future__ import annotations

import curses
import zlib

import pytest

from meridian_commander import pdf
from meridian_commander.browsers import viewer_for
from meridian_commander.image import Image
from meridian_commander.pdf import (
    Font,
    PdfView,
    Run,
    extract_runs,
    glyph_to_text,
    is_pdf,
    lines_from_runs,
    multiply,
    page_fonts,
    page_images,
    parse_cmap,
)
from meridian_commander.pdfobj import Document, PdfError

from support import (
    pdf_bytes,
    pdf_page_objects,
    pdf_stream,
    script_newwin,
    simple_pdf,
    text_content,
    with_curses_screen,
)


def lines_of(data: bytes, index: int = 0) -> list[str]:
    doc = Document(data)
    page = doc.pages()[index]
    return lines_from_runs(extract_runs(doc, page, page_fonts(doc, page)))


# -- names ---------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("book.pdf", True), ("BOOK.PDF", True), ("a.pdf", True),
    ("notes.txt", False), ("thing.pdfx", False), ("", False),
])
def test_is_pdf(name, expected):
    assert is_pdf(name) is expected


# -- text placement ------------------------------------------------------------

def test_a_page_of_plain_text():
    assert lines_of(simple_pdf(text_content("First line", "Second line"))) == \
        ["First line", "Second line"]


def test_parentheses_and_backslashes_survive():
    """The escapes that a naive lexer eats or chokes on."""
    content = (b"BT /F1 12 Tf 72 700 Td "
               rb"(with \(parens\) and a \\ backslash) Tj ET")
    assert lines_of(simple_pdf(content)) == \
        ["with (parens) and a \\ backslash"]


def test_a_string_that_spells_an_operator_is_still_text():
    """(Tj) is a perfectly ordinary piece of text, not the Tj operator."""
    content = b"BT /F1 12 Tf 72 700 Td (Tj ET BT) Tj ET"
    assert lines_of(simple_pdf(content)) == ["Tj ET BT"]


def test_runs_at_the_same_height_join_into_one_line():
    content = (b"BT /F1 12 Tf 72 700 Td (left) Tj "
               b"100 0 Td (right) Tj ET")
    assert lines_of(simple_pdf(content)) == ["left   right"]


def test_lines_come_out_top_down_whatever_order_they_were_drawn():
    content = (b"BT /F1 12 Tf 72 600 Td (lower) Tj ET "
               b"BT /F1 12 Tf 72 700 Td (upper) Tj ET")
    assert lines_of(simple_pdf(content)) == ["upper", "lower"]


def test_a_gap_wide_enough_becomes_a_space():
    """Producers routinely emit one run per word with no spaces in the file."""
    content = (b"BT /F1 12 Tf 72 700 Td (Spaces) Tj "
               b"45 0 Td (inferred) Tj ET")
    assert lines_of(simple_pdf(content)) == ["Spaces inferred"]


def test_a_step_that_only_just_clears_the_last_run_is_not_a_space():
    """"Spaces" measures 40pt here, so a 40pt step leaves nothing between."""
    content = (b"BT /F1 12 Tf 72 700 Td (Spaces) Tj "
               b"40 0 Td (inferred) Tj ET")
    assert lines_of(simple_pdf(content)) == ["Spacesinferred"]


def test_runs_that_touch_are_not_split():
    content = b"BT /F1 12 Tf 72 700 Td (Con) Tj (tiguous) Tj ET"
    assert lines_of(simple_pdf(content)) == ["Contiguous"]


def test_columns_stay_side_by_side():
    """A viewer shows the page as laid out; interleaving columns reads as noise."""
    content = (b"BT /F1 11 Tf 72 700 Td (Left one) Tj ET "
               b"BT /F1 11 Tf 330 700 Td (Right one) Tj ET")
    assert lines_of(simple_pdf(content)) == ["Left one   Right one"]


def test_a_superscript_stays_on_its_line():
    content = (b"BT /F1 12 Tf 72 700 Td (base) Tj "
               b"1 0 0 1 110 703 Tm (2) Tj ET")
    assert lines_of(simple_pdf(content)) == ["base 2"]


def test_td_moves_and_sets_the_line_origin():
    content = b"BT /F1 12 Tf 72 700 Td (one) Tj 0 -20 Td (two) Tj ET"
    assert lines_of(simple_pdf(content)) == ["one", "two"]


def test_td_capital_also_sets_the_leading():
    content = b"BT /F1 12 Tf 72 700 Td (one) Tj 0 -20 TD (two) Tj T* (three) Tj ET"
    assert lines_of(simple_pdf(content)) == ["one", "two", "three"]


def test_the_quote_operators_start_a_new_line():
    content = (b"BT /F1 12 Tf 72 700 Td 16 TL (one) Tj (two) ' "
               b"0 0 (three) \" ET")
    assert lines_of(simple_pdf(content)) == ["one", "two", "three"]


def test_tm_replaces_the_matrix_outright():
    content = (b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (one) Tj "
               b"1 0 0 1 72 680 Tm (two) Tj ET")
    assert lines_of(simple_pdf(content)) == ["one", "two"]


def test_a_tj_array_places_its_pieces_with_the_kerning_applied():
    content = b"BT /F1 12 Tf 72 700 Td [(A) -500 (B)] TJ ET"
    # A negative number moves the pen on: 500 thousandths at 12pt is 6pt,
    # which is a space. At 2000 it would be 24pt, and read as a column break.
    assert lines_of(simple_pdf(content)) == ["A B"]
    wide = b"BT /F1 12 Tf 72 700 Td [(A) -2000 (B)] TJ ET"
    assert lines_of(simple_pdf(wide)) == ["A   B"]


def test_the_transform_matrix_moves_the_text():
    content = b"q 1 0 0 1 0 -300 cm BT /F1 12 Tf 72 700 Td (moved) Tj ET Q"
    doc = Document(simple_pdf(content))
    page = doc.pages()[0]
    runs = extract_runs(doc, page, page_fonts(doc, page))
    assert runs[0].y == pytest.approx(400)


def test_the_graphics_stack_restores_the_transform():
    content = (b"q 1 0 0 1 0 -300 cm Q "
               b"BT /F1 12 Tf 72 700 Td (unmoved) Tj ET")
    doc = Document(simple_pdf(content))
    page = doc.pages()[0]
    runs = extract_runs(doc, page, page_fonts(doc, page))
    assert runs[0].y == pytest.approx(700)


def test_an_unbalanced_restore_does_not_raise():
    content = b"Q Q BT /F1 12 Tf 72 700 Td (fine) Tj ET"
    assert lines_of(simple_pdf(content)) == ["fine"]


def test_text_before_any_font_is_dropped_rather_than_guessed_at():
    content = b"BT 72 700 Td (no font set) Tj ET"
    assert lines_of(simple_pdf(content)) == []


def test_horizontal_scaling_widens_the_advance():
    content = (b"BT /F1 12 Tf 200 Tz 72 700 Td (AAAA) Tj 30 0 Td (B) Tj ET")
    # At 200% the first run is twice as wide, so the 30pt step no longer
    # clears it and the two runs stay joined.
    assert lines_of(simple_pdf(content)) == ["AAAAB"]


def test_character_and_word_spacing_widen_the_advance():
    plain = b"BT /F1 12 Tf 72 700 Td (a b) Tj 22 0 Td (c) Tj ET"
    spaced = b"BT /F1 12 Tf 8 Tc 4 Tw 72 700 Td (a b) Tj 22 0 Td (c) Tj ET"
    assert lines_of(simple_pdf(plain)) == ["a b c"]
    assert lines_of(simple_pdf(spaced)) == ["a bc"]


def test_a_content_stream_split_across_several_objects():
    objects = pdf_page_objects(b"")
    objects[4] = ("<< /Type /Page /Parent 2 0 R /Resources"
                  " << /Font << /F1 3 0 R >> >> /Contents [5 0 R 8 0 R] >>")
    objects[5] = pdf_stream("<< >>", b"BT /F1 12 Tf 72 700 Td (first) Tj ET")
    objects[8] = pdf_stream("<< >>", b"BT /F1 12 Tf 72 680 Td (second) Tj ET")
    assert lines_of(pdf_bytes(objects)) == ["first", "second"]


def test_a_content_stream_that_cannot_be_decoded_is_skipped():
    objects = pdf_page_objects(b"")
    objects[5] = pdf_stream("<< /Filter /FlateDecode >>", b"not compressed")
    assert lines_of(pdf_bytes(objects)) == []


def test_a_page_with_no_contents_at_all():
    objects = pdf_page_objects(b"")
    objects[4] = ("<< /Type /Page /Parent 2 0 R /Resources"
                  " << /Font << /F1 3 0 R >> >> >>")
    assert lines_of(pdf_bytes(objects)) == []


def test_a_malformed_stream_does_not_run_the_operand_stack_away():
    content = b"BT " + b"1 " * 200 + b"/F1 12 Tf 72 700 Td (survived) Tj ET"
    assert lines_of(simple_pdf(content)) == ["survived"]


def test_matrix_multiplication():
    assert multiply((1, 0, 0, 1, 5, 7), (1, 0, 0, 1, 2, 3)) == \
        (1, 0, 0, 1, 7, 10)
    assert multiply((2, 0, 0, 2, 0, 0), (1, 0, 0, 1, 1, 1)) == \
        (2, 0, 0, 2, 1, 1)


def test_a_run_knows_where_it_ends():
    assert Run(10.0, 20.0, "hi", 12.0, 5.0).end == 15.0


def test_no_runs_means_no_lines():
    assert lines_from_runs([]) == []


# -- encodings -----------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("space", " "), ("emdash", "—"), ("quotedblleft", "“"),
    ("A", "A"), ("uni00E9", "é"), ("u1F600", "\U0001F600"),
    ("g23", ""), ("cid7", ""), ("uniFFFFFFFF", ""),
])
def test_glyph_names(name, expected):
    assert glyph_to_text(name) == expected


def test_winansi_high_characters():
    """The 0x80-0x9F block, where WinAnsi and Latin-1 disagree."""
    content = b"BT /F1 12 Tf 72 700 Td (\x93quoted\x94 \x97 dash) Tj ET"
    assert lines_of(simple_pdf(content)) == ["“quoted” — dash"]


def test_without_winansi_the_bytes_are_latin_1():
    objects = pdf_page_objects(
        b"BT /F1 12 Tf 72 700 Td (caf\xe9) Tj ET",
        resources="<< /Font << /F1 3 0 R >> >>")
    objects[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    assert lines_of(pdf_bytes(objects)) == ["café"]


def test_an_encoding_differences_array_is_applied():
    objects = pdf_page_objects(
        b"BT /F1 12 Tf 72 700 Td (\x01\x02) Tj ET",
        resources="<< /Font << /F1 3 0 R >> >>")
    objects[3] = ("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
                  " /Encoding << /BaseEncoding /WinAnsiEncoding"
                  " /Differences [1 /emdash /bullet] >> >>")
    assert lines_of(pdf_bytes(objects)) == ["—•"]


def test_a_differences_array_that_is_not_one_is_ignored():
    font = Font.__new__(Font)
    font.differences = {}
    font._read_differences("not a list")
    assert font.differences == {}


def test_a_tounicode_cmap_is_what_makes_a_subset_font_readable():
    """Subset codes are glyph numbers; the CMap is the only way back."""
    cmap = (b"/CIDInit /ProcSet findresource begin\n"
            b"2 beginbfchar\n<0003> <0048>\n<0004> <0069>\nendbfchar\n"
            b"endcmap\n")
    objects = pdf_page_objects(
        b"BT /F1 12 Tf 72 700 Td <00030004> Tj ET",
        resources="<< /Font << /F1 3 0 R >> >>")
    objects[3] = ("<< /Type /Font /Subtype /Type0 /BaseFont /AAAAAA+Sub"
                  " /Encoding /Identity-H /ToUnicode 8 0 R"
                  " /DescendantFonts [9 0 R] >>")
    objects[8] = pdf_stream("<< >>", cmap)
    objects[9] = "<< /Type /Font /Subtype /CIDFontType2 /DW 600 >>"
    assert lines_of(pdf_bytes(objects)) == ["Hi"]


def test_a_composite_font_without_a_cmap_says_nothing_rather_than_rubbish():
    objects = pdf_page_objects(
        b"BT /F1 12 Tf 72 700 Td <00030004> Tj ET",
        resources="<< /Font << /F1 3 0 R >> >>")
    objects[3] = ("<< /Type /Font /Subtype /Type0 /BaseFont /AAAAAA+Sub"
                  " /Encoding /Identity-H /DescendantFonts [9 0 R] >>")
    objects[9] = "<< /Type /Font /Subtype /CIDFontType2 >>"
    assert lines_of(pdf_bytes(objects)) == []


@pytest.mark.parametrize("body, expected", [
    (b"1 beginbfchar <41> <0042> endbfchar", {0x41: "B"}),
    (b"1 beginbfrange <41> <43> <0061> endbfrange",
     {0x41: "a", 0x42: "b", 0x43: "c"}),
    (b"1 beginbfrange <41> <42> [<0078> <0079>] endbfrange",
     {0x41: "x", 0x42: "y"}),
    (b"1 beginbfchar <41> <00480049> endbfchar", {0x41: "HI"}),
])
def test_cmap_forms(body, expected):
    assert parse_cmap(body) == expected


@pytest.mark.parametrize("body", [
    b"1 beginbfrange <43> <41> <0061> endbfrange",     # backwards
    b"1 beginbfrange <0000> <FFFFFF> <0061> endbfrange",  # absurdly wide
    b"1 beginbfchar 42 43 endbfchar",                  # not strings
    b"1 beginbfrange 1 2 3 endbfrange",
    b"nothing here at all",
])
def test_cmap_nonsense_is_skipped(body):
    assert parse_cmap(body) == {}


def test_a_cmap_range_with_an_empty_target():
    assert parse_cmap(b"1 beginbfrange <41> <42> <> endbfrange") == \
        {0x41: "", 0x42: ""}


def test_a_broken_tounicode_stream_leaves_the_font_usable():
    objects = pdf_page_objects(
        b"BT /F1 12 Tf 72 700 Td (Hi) Tj ET",
        resources="<< /Font << /F1 3 0 R >> >>")
    objects[3] = ("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
                  " /ToUnicode 8 0 R >>")
    objects[8] = pdf_stream("<< /Filter /FlateDecode >>", b"not compressed")
    assert lines_of(pdf_bytes(objects)) == ["Hi"]


# -- widths --------------------------------------------------------------------

def test_a_declared_widths_array_is_used():
    objects = pdf_page_objects(
        b"BT /F1 12 Tf 72 700 Td (AA) Tj 30 0 Td (B) Tj ET",
        resources="<< /Font << /F1 3 0 R >> >>")
    objects[3] = ("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
                  " /FirstChar 65 /Widths [1000 1000] >>")
    # Two 1000-unit glyphs at 12pt is 24pt, so the 30pt step leaves 6pt --
    # half the font size, wide enough for one space but not for a gap.
    assert lines_of(pdf_bytes(objects)) == ["AA B"]


def test_a_missing_width_comes_from_the_descriptor():
    font = _font("<< /Type /Font /Subtype /Type1 /FontDescriptor 9 0 R >>",
                 {9: "<< /MissingWidth 750 >>"})
    assert font.default_width == 750


def test_cid_widths_in_both_forms():
    font = _font("<< /Type /Font /Subtype /Type0 /DescendantFonts [9 0 R] >>",
                 {9: "<< /DW 900 /W [3 [111 222] 10 12 333] >>"})
    assert font.widths[3] == 111
    assert font.widths[4] == 222
    assert font.widths[10] == font.widths[12] == 333
    assert font.default_width == 900


@pytest.mark.parametrize("table", [
    "[3]",                       # a code with nothing after it
    "[3 4]",                     # a range with no width
    "[/Name [1]]",               # a code that is not a number
    "[3 999999999 5]",           # a range too wide to be real
])
def test_cid_width_tables_that_make_no_sense(table):
    font = _font("<< /Type /Font /Subtype /Type0 /DescendantFonts [9 0 R] >>",
                 {9: "<< /W %s >>" % table})
    assert font.widths == {} or all(isinstance(v, float)
                                    for v in font.widths.values())


def test_a_composite_font_with_no_descendant():
    font = _font("<< /Type /Font /Subtype /Type0 >>", {})
    assert font.two_byte is True
    assert font.default_width == Font.FALLBACK_WIDTH


def test_widths_are_estimated_when_a_font_declares_none():
    """The standard fourteen ship no /Widths, and a flat 500 eats spaces."""
    font = _font("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", {})
    assert font._glyph_width(ord("i")) < font._glyph_width(ord("n"))
    assert font._glyph_width(ord("m")) > font._glyph_width(ord("n"))
    assert font._glyph_width(ord("A")) == Font.ESTIMATE_UPPER
    assert font._glyph_width(ord("5")) == Font.ESTIMATE_DEFAULT


def _font(spec: str, extra: dict) -> Font:
    objects = pdf_page_objects(b"", resources="<< /Font << /F1 3 0 R >> >>")
    objects[3] = spec
    objects.update(extra)
    doc = Document(pdf_bytes(objects))
    return page_fonts(doc, doc.pages()[0])["F1"]


def test_a_font_resource_that_is_not_a_dictionary_is_skipped():
    objects = pdf_page_objects(b"", resources="<< /Font << /F1 99 0 R >> >>")
    doc = Document(pdf_bytes(objects))
    assert page_fonts(doc, doc.pages()[0]) == {}


def test_a_page_with_no_font_resources():
    objects = pdf_page_objects(b"", resources="<< >>")
    doc = Document(pdf_bytes(objects))
    assert page_fonts(doc, doc.pages()[0]) == {}


# -- page images ---------------------------------------------------------------

def _image_pdf(dictionary: str, body: bytes, deflate: bool = False) -> bytes:
    objects = pdf_page_objects(
        b"q 300 0 0 200 72 500 cm /Im1 Do Q",
        resources="<< /XObject << /Im1 8 0 R >> >>")
    objects[8] = pdf_stream(dictionary, body, deflate=deflate)
    return pdf_bytes(objects)


def _images(data: bytes):
    doc = Document(data)
    return page_images(doc, doc.pages()[0])


def test_a_raw_rgb_image_is_decoded():
    """A scanned page is one big image and no text at all."""
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9])
    data = _image_pdf("<< /Type /XObject /Subtype /Image /Width 2 /Height 2"
                      " /BitsPerComponent 8 /ColorSpace /DeviceRGB >>",
                      pixels, deflate=True)
    (name, image), = _images(data)
    assert name == "Im1"
    assert isinstance(image, Image)
    assert (image.width, image.height) == (2, 2)
    assert tuple(image.frames[0].pixels[:3]) == (255, 0, 0)


def test_a_greyscale_image_expands_to_rgb():
    data = _image_pdf("<< /Subtype /Image /Width 2 /Height 1"
                      " /BitsPerComponent 8 /ColorSpace /DeviceGray >>",
                      bytes([0, 255]))
    (_name, image), = _images(data)
    assert tuple(image.frames[0].pixels[:6]) == (0, 0, 0, 255, 255, 255)


def test_a_one_bit_image_mask():
    data = _image_pdf("<< /Subtype /Image /Width 2 /Height 1"
                      " /ImageMask true >>", bytes([0b01000000]))
    (_name, image), = _images(data)
    assert tuple(image.frames[0].pixels[:6]) == (0, 0, 0, 255, 255, 255)


def test_a_jpeg_image_goes_through_the_jpeg_decoder():
    from support import jpeg_bytes

    data = _image_pdf("<< /Subtype /Image /Width 8 /Height 8"
                      " /BitsPerComponent 8 /ColorSpace /DeviceRGB"
                      " /Filter /DCTDecode >>", jpeg_bytes(8, 8, [[80]]))
    (_name, image), = _images(data)
    assert image.kind == "JPEG"


def test_a_colour_space_given_as_an_array():
    data = _image_pdf("<< /Subtype /Image /Width 1 /Height 1"
                      " /BitsPerComponent 8 /ColorSpace [/DeviceGray] >>",
                      bytes([128]))
    (_name, image), = _images(data)
    assert tuple(image.frames[0].pixels[:3]) == (128, 128, 128)


@pytest.mark.parametrize("dictionary, body, message", [
    ("<< /Subtype /Image /Width 1 /Height 1 /Filter /JPXDecode >>", b"",
     "JPXDecode"),
    ("<< /Subtype /Image /Width 1 /Height 1 /Filter /CCITTFaxDecode >>", b"",
     "CCITTFaxDecode"),
    ("<< /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceCMYK >>",
     bytes(4), "colour space DeviceCMYK"),
    ("<< /Subtype /Image /Width 1 /Height 1 >>", bytes(1),
     "colour space unknown"),
    ("<< /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray"
     " /BitsPerComponent 16 >>", bytes(2), "16 bits per component"),
    ("<< /Subtype /Image /Width 0 /Height 0 /ColorSpace /DeviceGray >>",
     b"", "no size"),
    ("<< /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceGray >>",
     b"short", "short of sample data"),
])
def test_an_image_this_cannot_decode_says_why(dictionary, body, message):
    (_name, reason), = _images(_image_pdf(dictionary, body))
    assert isinstance(reason, str)
    assert message in reason


def test_things_in_the_xobject_table_that_are_not_images_are_skipped():
    objects = pdf_page_objects(b"", resources="<< /XObject << /F1 8 0 R"
                                              " /F2 9 0 R >> >>")
    objects[8] = pdf_stream("<< /Subtype /Form >>", b"")
    objects[9] = "<< /Not /AStream >>"
    assert _images(pdf_bytes(objects)) == []


def test_a_page_with_no_xobjects_at_all():
    objects = pdf_page_objects(b"", resources="<< /XObject 42 >>")
    assert _images(pdf_bytes(objects)) == []


# -- the browser ---------------------------------------------------------------

def write_pdf(path, data: bytes) -> str:
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


@pytest.fixture
def book(tmp_path, fs):
    data = pdf_bytes(pdf_page_objects(
        text_content("Chapter one", "The opening line."), pages=2))
    return PdfView(fs, write_pdf(tmp_path / "book.pdf", data))


def test_the_browser_reads_the_pages(book):
    assert book.error is None
    assert len(book.pages) == 2
    assert book.lines() == ["Chapter one", "The opening line."]


def test_the_dispatch_opens_pdfs_in_the_pdf_browser(tmp_path, fs):
    path = write_pdf(tmp_path / "a.pdf", simple_pdf(text_content("Hi")))
    assert isinstance(viewer_for(fs, path), PdfView)


def test_a_file_that_is_not_a_pdf_says_so(tmp_path, fs):
    path = write_pdf(tmp_path / "a.pdf", b"not a pdf at all")
    assert "not a PDF" in PdfView(fs, path).error


def test_a_file_that_cannot_be_read_says_so(tmp_path, fs):
    assert PdfView(fs, str(tmp_path / "absent.pdf")).error


def test_a_pdf_larger_than_the_cap_is_refused(tmp_path, fs, monkeypatch):
    from meridian_commander import pdfobj

    monkeypatch.setattr(pdfobj, "MAX_BYTES", 8)
    path = write_pdf(tmp_path / "a.pdf", simple_pdf(text_content("Hi")))
    assert "larger than" in PdfView(fs, path).error


def test_a_pdf_with_no_pages(tmp_path, fs):
    data = pdf_bytes({1: "<< /Type /Catalog >>"})
    assert "no pages" in PdfView(fs, write_pdf(tmp_path / "a.pdf", data)).error


def test_too_many_pages_are_truncated(tmp_path, fs, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PAGES", 1)
    data = pdf_bytes(pdf_page_objects(text_content("Hi"), pages=2))
    view = PdfView(fs, write_pdf(tmp_path / "a.pdf", data))
    assert len(view.pages) == 1
    assert view.truncated is True


def test_page_text_is_extracted_once_and_kept(book):
    first = book.lines()
    assert book.lines() is first


def test_a_page_that_cannot_be_laid_out_says_so(book, monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("nope")

    monkeypatch.setattr(pdf, "extract_runs", explode)
    book._lines.clear()
    assert "could not be laid out" in book.lines()[0]


def test_moving_between_pages_clamps_at_both_ends(book):
    book.go_to(5)
    assert book.index == 1
    book.go_to(-3)
    assert book.index == 0


def test_scrolling_clamps_to_the_page(book):
    book.scroll(100, 4)
    assert book.top == 0            # two lines in a four-row body: no scroll
    book.scroll(-100, 4)
    assert book.top == 0


def test_searching_finds_the_next_page(book):
    book.search = "opening"
    assert book.find_next(1) is True
    assert "opening" in book.lines()[book.top]


def test_searching_for_something_absent(book):
    book.search = "not in this document"
    assert book.find_next(1) is False


def test_searching_with_nothing_to_search_for(book):
    assert book.find_next(1) is False


def test_search_is_smart_case(book):
    book.search = "chapter"
    assert book.find_next(1) is True
    book.search = "CHAPTER"
    assert book.find_next(1) is False


@pytest.fixture
def scan(tmp_path, fs):
    pixels = bytes([255, 0, 0] * 4)
    objects = pdf_page_objects(b"q 1 0 0 1 0 0 cm /Im1 Do Q",
                               resources="<< /XObject << /Im1 8 0 R >> >>")
    objects[8] = pdf_stream("<< /Subtype /Image /Width 2 /Height 2"
                            " /BitsPerComponent 8 /ColorSpace /DeviceRGB >>",
                            pixels)
    return PdfView(fs, write_pdf(tmp_path / "scan.pdf", pdf_bytes(objects)))


def test_a_scanned_page_offers_its_picture(scan):
    body = scan.body()
    assert "it is a picture" in body[0]
    assert "press i to view" in "".join(body)
    assert scan.viewable()[0] == "Im1"


def test_an_empty_page_says_so(tmp_path, fs):
    view = PdfView(fs, write_pdf(tmp_path / "a.pdf", simple_pdf(b"")))
    assert view.body() == ["(this page is empty)"]


def test_a_page_whose_picture_cannot_be_decoded_still_names_it(tmp_path, fs):
    objects = pdf_page_objects(b"", resources="<< /XObject << /Im1 8 0 R >> >>")
    objects[8] = pdf_stream("<< /Subtype /Image /Width 1 /Height 1"
                            " /Filter /JPXDecode >>", b"")
    view = PdfView(fs, write_pdf(tmp_path / "a.pdf", pdf_bytes(objects)))
    assert "JPXDecode" in "".join(view.body())
    assert view.viewable() is None


def test_images_are_extracted_once_and_kept(scan):
    assert scan.images() is scan.images()


def test_images_of_a_page_that_does_not_exist(book):
    assert book.images(99) == []
    assert book.lines(99) == []


# -- drawing -------------------------------------------------------------------

def draw(view, rows=10, cols=60):
    def run(stdscr):
        win = curses.newwin(rows, cols, 0, 0)
        view.draw(win)
        return [win.instr(y, 0, cols).decode("utf-8", "replace")
                for y in range(rows)]

    return with_curses_screen(rows + 2, cols + 2, run)


def test_the_title_names_the_file_and_the_page(book):
    lines = draw(book)
    assert "PDF: book.pdf" in lines[0]
    assert "page 1/2" in lines[0]


def test_the_body_shows_the_text(book):
    assert "Chapter one" in "".join(draw(book))


def test_the_footer_shows_the_keys(book):
    assert "[q]uit" in draw(book)[-1]
    assert "[i]mage" not in draw(book)[-1]


def test_a_scanned_page_offers_the_image_key(scan):
    assert "[i]mage" in draw(scan)[-1]


def test_a_search_is_shown_in_the_footer(book):
    book.search = "opening"
    assert "/opening" in draw(book)[-1]


def test_a_notice_replaces_the_footer(book):
    book.notice = " nothing found "
    assert "nothing found" in draw(book)[-1]


def test_a_truncated_document_says_so(book):
    book.truncated = True
    assert "[truncated]" in draw(book)[0]


def test_an_unreadable_file_draws_its_reason(tmp_path, fs):
    view = PdfView(fs, write_pdf(tmp_path / "a.pdf", b"not a pdf"))
    assert "Cannot read" in "".join(draw(view))
    assert "[q]uit" in draw(view)[-1]


def test_drawing_into_a_window_with_no_room(book):
    for rows, cols in ((1, 20), (2, 20), (3, 4)):
        draw(book, rows=rows, cols=cols)


# -- the corners ---------------------------------------------------------------

def test_a_glyph_name_naming_a_code_point_that_does_not_exist():
    assert glyph_to_text("uni110000") == ""


def test_a_cmap_target_with_an_odd_number_of_bytes():
    """A hex target is always even; a literal-string target need not be."""
    assert parse_cmap(b"1 beginbfchar <41> <004> endbfchar") == {0x41: "@"}
    assert parse_cmap(b"1 beginbfchar <41> (A) endbfchar") == {0x41: "\u4100"}


def test_a_two_byte_string_with_an_odd_number_of_bytes():
    """A malformed run: the last byte is padded rather than dropped."""
    font = _font("<< /Type /Font /Subtype /Type0 /DescendantFonts [9 0 R] >>",
                 {9: "<< /DW 500 >>"})
    assert font.codes(b"\x00\x41\x00") == [0x41, 0x0000]


def test_the_text_rise_operator_is_accepted():
    content = b"BT /F1 12 Tf 72 700 Td 4 Ts (raised) Tj ET"
    assert lines_of(simple_pdf(content)) == ["raised"]


def test_a_tj_array_item_that_is_neither_a_string_nor_a_number():
    content = b"BT /F1 12 Tf 72 700 Td [(A) /Name (B)] TJ ET"
    assert lines_of(simple_pdf(content)) == ["AB"]


def test_the_browser_survives_a_window_that_refuses_every_write(book,
                                                                monkeypatch):
    def run(stdscr):
        script_newwin(monkeypatch, [ord("q")], fail_draws=True)
        book.run(stdscr)

    with_curses_screen(12, 62, run)


def test_the_error_screen_survives_a_refusing_window(tmp_path, fs, monkeypatch):
    view = PdfView(fs, write_pdf(tmp_path / "a.pdf", b"not a pdf"))

    def run(stdscr):
        script_newwin(monkeypatch, [ord("q")], fail_draws=True)
        view.run(stdscr)

    with_curses_screen(12, 62, run)


# -- the key loop --------------------------------------------------------------

def run_keys(view, keys, monkeypatch, rows=12, cols=60):
    """Drive the key loop, returning everything it drew.

    The drawn text is what gets asserted on rather than the browser's state
    afterwards: a notice is cleared by the *next* keystroke, and there is
    always a next keystroke -- the "q" that ends the loop.
    """

    def run(stdscr):
        captured = script_newwin(monkeypatch, list(keys) + [ord("q")])
        view.run(stdscr)
        return "\n".join(str(call[2]) for call in captured["window"].drawn
                          if len(call) > 2)

    return with_curses_screen(rows + 2, cols + 2, run)


@pytest.mark.parametrize("key", [27, ord("Q"), curses.KEY_F3, curses.KEY_F10])
def test_every_way_out_works(book, monkeypatch, key):
    def run(stdscr):
        script_newwin(monkeypatch, [key])
        book.run(stdscr)

    with_curses_screen(14, 62, run)


@pytest.mark.parametrize("key", [9, ord("]"), curses.KEY_RIGHT])
def test_the_page_forward_keys(book, monkeypatch, key):
    run_keys(book, [key], monkeypatch)
    assert book.index == 1


@pytest.mark.parametrize("key", [curses.KEY_BTAB, ord("["), curses.KEY_LEFT])
def test_the_page_back_keys(book, monkeypatch, key):
    book.go_to(1)
    run_keys(book, [key], monkeypatch)
    assert book.index == 0


@pytest.mark.parametrize("keys, expected", [
    ([curses.KEY_DOWN], 1), ([ord("j")], 1),
    ([curses.KEY_NPAGE], 1), ([curses.KEY_END], 1),
])
def test_scrolling_down_a_long_page(book, monkeypatch, keys, expected):
    book._lines[0] = ["line %d" % i for i in range(50)]
    run_keys(book, keys, monkeypatch)
    assert book.top >= expected


@pytest.mark.parametrize("keys", [[curses.KEY_UP], [ord("k")],
                                  [curses.KEY_PPAGE], [curses.KEY_HOME]])
def test_scrolling_back_to_the_top(book, monkeypatch, keys):
    book._lines[0] = ["line %d" % i for i in range(50)]
    book.top = 10
    run_keys(book, keys, monkeypatch)
    assert book.top < 10


def test_the_search_prompt_finds_a_match(book, monkeypatch):
    from meridian_commander import dialogs

    monkeypatch.setattr(dialogs, "prompt",
                        lambda *a, **k: "opening")
    drawn = run_keys(book, [ord("/")], monkeypatch)
    assert book.search == "opening"
    assert "not found" not in drawn


def test_the_search_prompt_reports_nothing_found(book, monkeypatch):
    from meridian_commander import dialogs

    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "absent")
    assert "not found" in run_keys(book, [ord("/")], monkeypatch)


def test_a_cancelled_search_prompt_changes_nothing(book, monkeypatch):
    from meridian_commander import dialogs

    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: None)
    run_keys(book, [ord("/")], monkeypatch)
    assert book.search == ""


@pytest.mark.parametrize("key", [ord("n"), ord("N"), ord("p")])
def test_the_repeat_search_keys_report_no_match(book, monkeypatch, key):
    book.search = "absent"
    assert "no match" in run_keys(book, [key], monkeypatch)


@pytest.mark.parametrize("key", [ord("n"), ord("N")])
def test_the_repeat_search_keys_with_nothing_searched_for(book, monkeypatch,
                                                          key):
    assert "no match" not in run_keys(book, [key], monkeypatch)


def test_the_image_key_opens_the_picture(scan, monkeypatch):
    from meridian_commander import imageview

    opened = []

    class _Fake(imageview.ImageView):
        def run(self, stdscr):
            opened.append((self.name, self.image))

    monkeypatch.setattr(imageview, "ImageView", _Fake)
    run_keys(scan, [ord("i")], monkeypatch)
    assert opened and opened[0][1].kind == "PDF image"
    assert "page 1" in opened[0][0]


def test_the_image_key_on_a_page_with_no_picture(book, monkeypatch):
    assert "no picture" in run_keys(book, [ord("I")], monkeypatch)


def test_an_unknown_key_is_ignored(book, monkeypatch):
    run_keys(book, [ord("z")], monkeypatch)
    assert book.index == 0
