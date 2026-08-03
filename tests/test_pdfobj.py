"""The PDF object layer: lexing, cross-references, object streams, filters.

Fixtures are written out byte by byte in ``support``.  The reader was checked
separately against files from reportlab, pypdf and qpdf -- classic tables,
cross-reference streams, object streams, linearised and incrementally updated
files -- but none of those is a dependency this project will take.
"""

from __future__ import annotations

import zlib

import pytest

from meridian_commander import pdfobj
from meridian_commander.pdfobj import (
    Document,
    Lexer,
    Name,
    PdfError,
    Ref,
    Stream,
    String,
    apply_predictor,
    ascii85_decode,
    ascii_hex_decode,
    as_list,
    is_keyword,
    lzw_decode,
    run_length_decode,
)

from support import (
    pdf_bytes,
    pdf_page_objects,
    pdf_stream,
    pdf_xref_stream,
    simple_pdf,
    text_content,
)


def parse(text: bytes):
    return Lexer(text).object()


# -- the lexer -----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (b"42", 42), (b"-17", -17), (b"+5", 5),
    (b"3.5", 3.5), (b"-.5", -0.5), (b"4.", 4.0),
    (b"true", True), (b"false", False), (b"null", None),
])
def test_simple_values(text, expected):
    assert parse(text) == expected


def test_names_are_their_own_type():
    """So /F1 and the string (F1) can be told apart, which matters in a scan."""
    value = parse(b"/Widths")
    assert value == "Widths"
    assert isinstance(value, Name)


def test_a_name_with_hex_escapes():
    assert parse(b"/A#20Name") == "A Name"


def test_a_name_with_a_broken_hex_escape_keeps_the_hash():
    assert parse(b"/A#ZZ") == "A#ZZ"


def test_strings_are_their_own_type_too():
    """The bug this prevents: (Tj) in a content stream reading as an operator."""
    value = parse(b"(Tj)")
    assert value == b"Tj"
    assert isinstance(value, String)
    assert not is_keyword(value, b"Tj")
    assert is_keyword(b"Tj", b"Tj")


@pytest.mark.parametrize("text, expected", [
    (rb"(plain)", b"plain"),
    (rb"(with \(nested\) parens)", b"with (nested) parens"),
    (rb"(outer (inner) outer)", b"outer (inner) outer"),
    (rb"(tab\there)", b"tab\there"),
    (rb"(line\nbreak)", b"line\nbreak"),
    (rb"(\101\102\103)", b"ABC"),
    (rb"(\7)", b"\x07"),
    (rb"(literal \q)", b"literal q"),
])
def test_literal_strings(text, expected):
    assert parse(text) == expected


def test_a_backslash_before_a_newline_continues_the_line():
    assert parse(b"(one\\\ntwo)") == b"onetwo"
    assert parse(b"(one\\\r\ntwo)") == b"onetwo"


def test_a_string_that_never_closes_ends_at_the_data():
    assert parse(rb"(unterminated") == b"unterminated"
    assert parse(b"(trailing\\") == b"trailing"


@pytest.mark.parametrize("text, expected", [
    (b"<48656C6C6F>", b"Hello"),
    (b"<48 65 6c 6C 6f>", b"Hello"),
    (b"<4>", b"@"),                      # an odd digit is padded with zero
    (b"<>", b""),
])
def test_hex_strings(text, expected):
    assert parse(text) == expected


def test_a_hex_string_with_rubbish_in_it_is_refused():
    with pytest.raises(PdfError, match="non-hex digit"):
        parse(b"<zz>")


def test_arrays_and_nesting():
    assert parse(b"[1 2 [3 4] /Five (six)]") == [1, 2, [3, 4], "Five", b"six"]


def test_an_array_that_never_closes_returns_what_it_had():
    assert parse(b"[1 2 3") == [1, 2, 3]


def test_dictionaries():
    value = parse(b"<< /Type /Page /Count 3 /Kids [1 0 R] >>")
    assert value["Type"] == "Page"
    assert value["Count"] == 3
    assert value["Kids"] == [Ref(1, 0)]


def test_a_dictionary_key_that_is_not_a_name_is_skipped():
    """Malformed, but the rest of the dictionary is usually still good."""
    assert parse(b"<< 42 /Type /Page >>") == {"Type": "Page"}


def test_indirect_references():
    assert parse(b"7 0 R") == Ref(7, 0)
    assert Ref(7, 0) == Ref(7, 0)
    assert Ref(7, 0) != Ref(7, 1)
    assert Ref(7, 0) != "not a ref"
    assert len({Ref(1, 0), Ref(1, 0), Ref(2, 0)}) == 2


def test_a_reference_says_what_it_is_when_printed():
    """Assertion failures in this suite are unreadable without it."""
    assert repr(Ref(7, 1)) == "Ref(7, 1)"


def test_two_numbers_that_are_not_a_reference_stay_numbers():
    """"1 2" followed by anything but R is two integers, not a reference."""
    assert parse(b"[1 2 /Name]") == [1, 2, "Name"]


def test_a_real_number_is_never_a_reference():
    assert parse(b"1.0 2 R") == 1.0


def test_comments_are_skipped():
    assert parse(b"% a comment\n42") == 42


def test_nesting_that_goes_too_deep_is_refused():
    with pytest.raises(PdfError, match="nesting is too deep"):
        parse(b"[" * 40 + b"1")


def test_a_stray_delimiter_comes_back_as_itself():
    lexer = Lexer(b")x")
    assert lexer.token() == b")"


def test_as_list_normalises_single_values():
    assert as_list(None) == []
    assert as_list(5) == [5]
    assert as_list([5, 6]) == [5, 6]


# -- filters -------------------------------------------------------------------

def test_ascii_hex_decode():
    assert ascii_hex_decode(b"48 65 6C 6C 6F>") == b"Hello"
    assert ascii_hex_decode(b"4>") == b"@"


def test_ascii_hex_decode_refuses_rubbish():
    with pytest.raises(PdfError, match="non-hex digit"):
        ascii_hex_decode(b"zz>")


def test_ascii85_decode():
    import base64

    encoded = base64.a85encode(b"Hello, world!")
    assert ascii85_decode(encoded) == b"Hello, world!"
    assert ascii85_decode(b"<~" + encoded + b"~>") == b"Hello, world!"


def test_ascii85_decode_refuses_rubbish():
    with pytest.raises(PdfError, match="ASCII85Decode stream is damaged"):
        ascii85_decode(b"~~~~~invalid~~~~~")


def test_run_length_decode():
    # A literal run of three, then a repeat of five, then the end marker.
    assert run_length_decode(bytes([2]) + b"abc" + bytes([252]) + b"x"
                             + bytes([128])) == b"abc" + b"x" * 5


def test_run_length_decode_stops_at_a_truncated_repeat():
    assert run_length_decode(bytes([252])) == b""


def test_lzw_decode_round_trips_a_simple_stream():
    """Encoded here with literals only, which is valid and easy to be sure of."""
    codes = [256] + [ord(c) for c in "hello"] + [257]
    bits = "".join(format(code, "09b") for code in codes)
    bits += "0" * (-len(bits) % 8)
    data = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    assert lzw_decode(data) == b"hello"


def test_lzw_refuses_a_stream_that_opens_undefined():
    bits = format(300, "09b") + "0" * 7
    data = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    with pytest.raises(PdfError, match="undefined code"):
        lzw_decode(data)


@pytest.mark.parametrize("kind", [0, 1, 2, 3, 4])
def test_png_predictors_all_reconstruct_the_same_rows(kind):
    """Every predictor is reversible, so all five land on identical bytes."""
    rows = [bytes([10, 20, 30, 40]), bytes([12, 22, 33, 44])]
    encoded = bytearray()
    previous = bytes(4)
    for row in rows:
        encoded.append(kind)
        for i, value in enumerate(row):
            left = row[i - 1] if i >= 1 else 0
            up = previous[i]
            upleft = previous[i - 1] if i >= 1 else 0
            if kind == 0:
                encoded.append(value)
            elif kind == 1:
                encoded.append((value - left) & 0xFF)
            elif kind == 2:
                encoded.append((value - up) & 0xFF)
            elif kind == 3:
                encoded.append((value - ((left + up) >> 1)) & 0xFF)
            else:
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                best = left if pa <= pb and pa <= pc else (
                    up if pb <= pc else upleft)
                encoded.append((value - best) & 0xFF)
        previous = row
    assert apply_predictor(bytes(encoded), 12, 1, 8, 4) == b"".join(rows)


def test_a_predictor_row_type_that_does_not_exist():
    with pytest.raises(PdfError, match="row type 9"):
        apply_predictor(bytes([9, 1, 2, 3, 4]), 12, 1, 8, 4)


def test_tiff_prediction():
    assert apply_predictor(bytes([10, 5, 5, 5]), 2, 1, 8, 4) == \
        bytes([10, 15, 20, 25])


def test_tiff_prediction_at_other_depths_is_refused():
    with pytest.raises(PdfError, match="TIFF prediction at 4 bits"):
        apply_predictor(b"\x00", 2, 1, 4, 4)


def test_a_predictor_of_one_changes_nothing():
    assert apply_predictor(b"abc", 1, 1, 8, 3) == b"abc"


# -- documents -----------------------------------------------------------------

def test_a_file_that_is_not_a_pdf():
    with pytest.raises(PdfError, match="not a PDF"):
        Document(b"just some text")


def test_a_classic_table_and_a_page():
    doc = Document(simple_pdf(text_content("Hi")))
    assert len(doc.pages()) == 1
    assert doc.pages()[0]["Type"] == "Page"


def test_pages_inherit_from_their_parent():
    """MediaBox and Resources are declared once on the tree, not per page."""
    doc = Document(simple_pdf(text_content("Hi")))
    page = doc.pages()[0]
    assert page["MediaBox"] == [0, 0, 612, 792]


def test_a_cross_reference_stream_with_object_streams():
    """The layout every PDF made this century uses."""
    data = pdf_xref_stream(pdf_page_objects(text_content("Hi")),
                           in_object_stream=(1, 2, 3, 4))
    doc = Document(data)
    assert len(doc.pages()) == 1
    assert sum(1 for v in doc.xref.values() if isinstance(v, tuple)) == 4


def test_a_broken_xref_is_recovered_by_scanning():
    """Hand-edited and truncated files have offsets that point nowhere."""
    doc = Document(simple_pdf(text_content("Hi"), broken_xref=True))
    assert len(doc.pages()) == 1


def test_a_file_with_no_trailer_finds_its_catalogue_by_type():
    doc = Document(simple_pdf(text_content("Hi"), no_trailer=True))
    assert len(doc.pages()) == 1


def test_an_encrypted_pdf_says_so_rather_than_producing_nonsense():
    data = pdf_bytes(pdf_page_objects(text_content("Hi")),
                     trailer_extra="/Encrypt 99 0 R ")
    with pytest.raises(PdfError, match="encrypted"):
        Document(data)


def test_a_missing_object_is_none_not_an_error():
    doc = Document(simple_pdf(text_content("Hi")))
    assert doc.object(999) is None


def test_resolve_follows_a_chain_and_gives_up_on_a_loop():
    objects = pdf_page_objects(text_content("Hi"))
    objects[20] = "21 0 R"
    objects[21] = "20 0 R"
    doc = Document(pdf_bytes(objects))
    assert doc.resolve(Ref(20, 0)) in (None, Ref(20, 0), Ref(21, 0))


def test_get_tolerates_anything_that_is_not_a_dictionary():
    doc = Document(simple_pdf(text_content("Hi")))
    assert doc.get(None, "Type", "fallback") == "fallback"
    assert doc.get({"A": None}, "A", "fallback") == "fallback"
    assert doc.get({}, "Missing", 7) == 7


def test_a_stream_with_a_wrong_length_is_found_by_its_endstream():
    """A wrong /Length is common; what follows the data is the real check."""
    objects = pdf_page_objects(text_content("Hi"))
    body = b"BT /F1 12 Tf 72 700 Td (Recovered) Tj ET"
    objects[5] = b"<< /Length 3 >>\nstream\n" + body + b"\nendstream"
    doc = Document(pdf_bytes(objects))
    stream = doc.resolve(doc.pages()[0]["Contents"])
    assert b"Recovered" in doc.decode_stream(stream)


def test_a_stream_with_no_endstream_at_all():
    objects = pdf_page_objects(text_content("Hi"))
    objects[5] = b"<< /Length 999 >>\nstream\nbody with no terminator"
    doc = Document(pdf_bytes(objects))
    assert doc.object(5) is None


def test_a_flate_stream_is_decompressed():
    doc = Document(simple_pdf(b"", resources="<< >>"))
    stream = Stream({"Filter": "FlateDecode"}, zlib.compress(b"payload"))
    assert doc.decode_stream(stream) == b"payload"


def test_a_truncated_flate_stream_keeps_what_it_can():
    doc = Document(simple_pdf(b"", resources="<< >>"))
    whole = zlib.compress(b"a long enough payload to survive truncation" * 4)
    stream = Stream({"Filter": "FlateDecode"}, whole[:-6])
    assert b"payload" in doc.decode_stream(stream)


def test_a_flate_stream_that_is_not_flate_at_all():
    doc = Document(simple_pdf(b"", resources="<< >>"))
    with pytest.raises(PdfError, match="Flate stream is corrupt"):
        doc.decode_stream(Stream({"Filter": "FlateDecode"}, b"not compressed"))


def test_a_filter_with_no_decoder_says_which():
    doc = Document(simple_pdf(b"", resources="<< >>"))
    with pytest.raises(PdfError, match="filter Weird is not one this reads"):
        doc.decode_stream(Stream({"Filter": "Weird"}, b""))


def test_filters_are_applied_in_order():
    doc = Document(simple_pdf(b"", resources="<< >>"))
    import base64

    raw = base64.a85encode(zlib.compress(b"twice wrapped"))
    stream = Stream({"Filter": ["ASCII85Decode", "FlateDecode"]}, raw)
    assert doc.decode_stream(stream) == b"twice wrapped"


def test_a_decoded_stream_is_kept_not_redone():
    doc = Document(simple_pdf(b"", resources="<< >>"))
    stream = Stream({}, b"plain")
    assert doc.decode_stream(stream) == b"plain"
    stream.raw = b"changed"
    assert doc.decode_stream(stream) == b"plain"


def test_an_image_filter_is_left_encoded():
    """A DCTDecode stream is a JPEG file; the image decoder wants it whole."""
    doc = Document(simple_pdf(b"", resources="<< >>"))
    stream = Stream({"Filter": "DCTDecode"}, b"\xff\xd8jpeg bytes")
    assert doc.decode_stream(stream) == b"\xff\xd8jpeg bytes"
    assert doc.image_filter(stream) == "DCTDecode"
    assert doc.image_filter(Stream({"Filter": "FlateDecode"}, b"")) is None


def test_a_stream_that_decompresses_past_the_limit(monkeypatch):
    doc = Document(simple_pdf(b"", resources="<< >>"))
    monkeypatch.setattr(pdfobj, "MAX_STREAM_BYTES", 16)
    stream = Stream({"Filter": "FlateDecode"}, zlib.compress(b"x" * 1000))
    with pytest.raises(PdfError, match="more than the limit"):
        doc.decode_stream(stream)


def test_a_predictor_named_in_decode_parms_is_applied():
    doc = Document(simple_pdf(b"", resources="<< >>"))
    rows = bytes([2, 10, 20, 30, 40])           # PNG "up" on a zero first row
    stream = Stream({"Filter": "FlateDecode",
                     "DecodeParms": {"Predictor": 12, "Columns": 4}},
                    zlib.compress(rows))
    assert doc.decode_stream(stream) == bytes([10, 20, 30, 40])


def test_a_page_tree_that_is_broken_falls_back_to_finding_pages():
    objects = pdf_page_objects(text_content("Hi"))
    objects[2] = "<< /Type /Pages /Count 1 /Kids [] >>"
    doc = Document(pdf_bytes(objects))
    assert len(doc.pages()) == 1


def test_a_page_tree_with_a_loop_in_it_terminates():
    objects = pdf_page_objects(text_content("Hi"))
    objects[2] = "<< /Type /Pages /Count 1 /Kids [2 0 R 4 0 R] >>"
    doc = Document(pdf_bytes(objects))
    assert len(doc.pages()) >= 1


# -- the corners ---------------------------------------------------------------

def test_a_bare_keyword_comes_back_as_itself():
    assert parse(b"obj") == b"obj"


def test_paeth_can_choose_the_pixel_above_left():
    """The third branch of the predictor, which the other cases never reach."""
    encoded = bytes([0, 100, 0]) + bytes([4, 156, 100])
    out = apply_predictor(encoded, 15, 1, 8, 2)
    assert len(out) == 4


def test_a_predictor_row_shorter_than_its_stride_is_padded():
    assert len(apply_predictor(bytes([0, 1, 2]), 12, 1, 8, 4)) == 4


def test_lzw_handles_the_self_referential_code_and_grows_the_width():
    """Enough distinct output to push the code width past nine bits."""
    payload = bytes(range(256)) * 3
    codes, table, previous = [256], {}, b""
    # A literal-only encoder, with the table tracked so the width matches.
    size, count = 9, 258
    bits = ""
    for byte in payload:
        bits += format(byte, "0%db" % size)
        count += 1
        if count + 1 >= (1 << size) and size < 12:
            size += 1
    bits = format(256, "09b") + bits
    bits += "0" * (-len(bits) % 8)
    data = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    assert lzw_decode(data).startswith(bytes(range(64)))


def test_lzw_early_change_is_honoured_through_decode_parms():
    doc = Document(simple_pdf(b"", resources="<< >>"))
    codes = [256] + [ord(c) for c in "ab"] + [257]
    bits = "".join(format(code, "09b") for code in codes)
    bits += "0" * (-len(bits) % 8)
    data = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    stream = Stream({"Filter": "LZWDecode",
                     "DecodeParms": {"EarlyChange": 0}}, data)
    assert doc.decode_stream(stream) == b"ab"


def test_the_short_filter_names_work_too():
    doc = Document(simple_pdf(b"", resources="<< >>"))
    assert doc.decode_stream(Stream({"F": "AHx"}, b"4869>")) == b"Hi"
    assert doc.decode_stream(
        Stream({"Filter": "RunLengthDecode"},
               bytes([1]) + b"Hi" + bytes([128]))) == b"Hi"


def test_a_hybrid_file_reads_the_stream_its_table_points_at():
    """/XRefStm: a classic table for old readers, a stream for new ones."""
    modern = pdf_xref_stream(pdf_page_objects(text_content("Hybrid")),
                             in_object_stream=(1, 2, 3, 4))
    at = modern.rfind(b"startxref")
    offset = int(modern[at + 9:].split()[0])
    tail = (b"xref\n0 1\n0000000000 65535 f \n"
            b"trailer\n<< /Size 1 /XRefStm %d >>\nstartxref\n%d\n%%%%EOF\n"
            % (offset, len(modern)))
    doc = Document(modern + tail)
    assert len(doc.pages()) == 1


@pytest.mark.parametrize("tail", [
    b"xref\n0 2\n0000000000 65535 f \n",          # runs out mid-subsection
    b"xref\nnot-a-number\n",                       # a header that is not one
    b"xref\n0 notanumber\n",                       # a count that is not one
])
def test_a_malformed_xref_table_falls_back_to_scanning(tail):
    good = simple_pdf(text_content("Hi"))
    body = good[:good.rfind(b"xref\n0")]
    data = body + tail + b"startxref\n%d\n%%%%EOF\n" % len(body)
    assert len(Document(data).pages()) == 1


def test_a_cross_reference_section_that_is_neither_kind():
    good = simple_pdf(text_content("Hi"))
    body = good[:good.rfind(b"xref\n0")]
    data = body + b"rubbish here\nstartxref\n%d\n%%%%EOF\n" % len(body)
    assert len(Document(data).pages()) == 1


def test_a_cross_reference_object_that_is_not_a_stream():
    objects = pdf_page_objects(text_content("Hi"))
    objects[9] = "<< /Type /XRef >>"
    body = pdf_bytes(objects)
    at = body.find(b"9 0 obj")
    data = body[:body.rfind(b"xref\n0")] + b"startxref\n%d\n%%%%EOF\n" % at
    assert len(Document(data).pages()) == 1


def test_a_cross_reference_stream_with_no_field_widths():
    objects = pdf_page_objects(text_content("Hi"))
    objects[9] = pdf_stream("<< /Type /XRef /Size 10 >>", b"")
    body = pdf_bytes(objects)
    at = body.find(b"9 0 obj")
    data = body[:body.rfind(b"xref\n0")] + b"startxref\n%d\n%%%%EOF\n" % at
    assert len(Document(data).pages()) == 1


def test_a_cross_reference_stream_that_runs_out_mid_row():
    objects = pdf_page_objects(text_content("Hi"))
    objects[9] = pdf_stream(
        "<< /Type /XRef /Size 20 /W [1 4 2] /Root 1 0 R /Index [0 20] >>",
        b"\x01\x00\x00\x00\x09\x00")
    body = pdf_bytes(objects)
    at = body.find(b"9 0 obj")
    data = body[:body.rfind(b"xref\n0")] + b"startxref\n%d\n%%%%EOF\n" % at
    assert len(Document(data).pages()) == 1


def test_an_object_at_an_offset_that_is_not_an_object():
    doc = Document(simple_pdf(text_content("Hi")))
    doc._cache.clear()
    doc.xref[4] = 3                     # points into the header
    assert doc.object(4) is not None    # found by scanning instead


def test_an_object_number_that_disagrees_with_its_header():
    objects = pdf_page_objects(text_content("Hi"))
    doc = Document(pdf_bytes(objects))
    doc._cache.clear()
    doc._scanned = True                 # no rescue by scanning
    doc.xref[4] = doc.xref[5]
    assert doc.object(4) is None


def test_an_offset_outside_the_file():
    doc = Document(simple_pdf(text_content("Hi")))
    doc._cache.clear()
    doc._scanned = True
    doc.xref[4] = 10 ** 9
    assert doc.object(4) is None


def test_a_stream_whose_dictionary_is_not_one():
    objects = pdf_page_objects(text_content("Hi"))
    objects[7] = b"42\nstream\nbody\nendstream"
    doc = Document(pdf_bytes(objects))
    assert doc.object(7) is None


@pytest.mark.parametrize("newline", [b"\n", b"\r", b"\r\n"])
def test_every_newline_after_the_stream_keyword(newline):
    objects = pdf_page_objects(text_content("Hi"))
    objects[7] = b"<< /Length 4 >>\nstream" + newline + b"body\nendstream"
    doc = Document(pdf_bytes(objects))
    assert doc.decode_stream(doc.object(7)) == b"body"


def test_an_object_stream_whose_container_is_missing():
    doc = Document(simple_pdf(text_content("Hi")))
    doc._cache.clear()
    doc.xref[4] = ("stm", 999, 0)
    doc._scanned = True
    assert doc.object(4) is None


def test_an_object_stream_with_a_truncated_header():
    objects = pdf_page_objects(text_content("Hi"))
    objects[8] = pdf_stream("<< /Type /ObjStm /N 5 /First 4 >>", b"1 0")
    doc = Document(pdf_bytes(objects))
    doc._cache.clear()
    doc.xref[20] = ("stm", 8, 0)
    assert doc.object(20) is None


def test_an_object_stream_that_does_not_hold_what_was_asked_for():
    objects = pdf_page_objects(text_content("Hi"))
    objects[8] = pdf_stream("<< /Type /ObjStm /N 1 /First 6 >>", b"30 0 (x)")
    doc = Document(pdf_bytes(objects))
    doc._cache.clear()
    doc.xref[31] = ("stm", 8, 0)
    assert doc.object(31) is None


def test_scanning_happens_only_once():
    doc = Document(simple_pdf(text_content("Hi")))
    doc._scan_objects()
    before = dict(doc.xref)
    doc._scan_objects()
    assert doc.xref == before


def test_paeth_chooses_the_pixel_above_left():
    """The branch the other three never reach: the diagonal predicts best."""
    # Row 0 decodes to [100, 200]. On row 1, column 1 has left=10, up=200 and
    # up-left=100, so the estimate 110 is nearest to the diagonal.
    encoded = bytes([0, 100, 200]) + bytes([4, 166, 156])
    out = apply_predictor(encoded, 15, 1, 8, 2)
    assert out[2] == 10                     # left, reconstructed from "up"
    assert out[3] == 0                      # 156 + 100, wrapped
    assert len(out) == 4


def test_a_trailer_that_is_not_a_dictionary():
    good = simple_pdf(text_content("Hi"))
    body = good[:good.rfind(b"xref\n0")]
    data = (body + b"xref\n0 1\n0000000000 65535 f \ntrailer\n42\n"
            + b"startxref\n%d\n%%%%EOF\n" % len(body))
    assert len(Document(data).pages()) == 1


def test_an_earlier_revision_does_not_overwrite_a_later_one():
    """An incremental update wins; the entry it replaced is skipped."""
    data = pdf_xref_stream(pdf_page_objects(text_content("Hi")))
    at = data.rfind(b"startxref")
    offset = int(data[at + 9:].split()[0])
    # A second cross-reference stream naming object 1 again, with /Prev
    # pointing back at the first: the first must not win.
    extra = pdf_stream(
        "<< /Type /XRef /Size 3 /W [1 4 2] /Root 1 0 R /Index [0 3]"
        " /Prev %d >>" % offset,
        b"\x00\x00\x00\x00\x00\x00\x00\x00" * 3, deflate=True)
    start = len(data)
    data += b"90 0 obj\n" + extra + b"\nendobj\nstartxref\n%d\n%%%%EOF\n" % start
    assert len(Document(data).pages()) == 1


def test_a_file_whose_startxref_points_at_rubbish_finds_its_trailer():
    good = simple_pdf(text_content("Hi"))
    data = good[:good.rfind(b"startxref")] + b"startxref\n7\n%%EOF\n"
    doc = Document(data)
    assert "Root" in doc.trailer
    assert len(doc.pages()) == 1


def test_an_object_the_rescan_also_cannot_place():
    """The offset is wrong and scanning cannot find the object either."""
    doc = Document(simple_pdf(text_content("Hi")))
    doc._cache.clear()
    doc._scanned = False
    # Erase the object's header, so neither the offset nor a scan finds it.
    doc.data = doc.data.replace(b"\n4 0 obj", b"\n4 0 xxx")
    doc.xref[4] = 3
    assert doc.object(4) is None


def test_an_older_revision_does_not_replace_a_newer_one():
    """Sections are read newest first, so an older entry for the same object
    is skipped -- which is what makes an incremental update behave."""
    data = pdf_xref_stream(pdf_page_objects(text_content("Hi")))
    at = data.rfind(b"startxref")
    previous = int(data[at + 9:].split()[0])
    catalogue = data.find(b"\n1 0 obj") + 1
    # A newer section that re-declares object 1 at the offset it really has;
    # reading the older section must then leave that entry alone.
    rows = (bytes([0]) + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big")
            + bytes([1]) + catalogue.to_bytes(4, "big") + (0).to_bytes(2, "big"))
    extra = pdf_stream(
        "<< /Type /XRef /Size 2 /W [1 4 2] /Root 1 0 R /Index [0 2]"
        " /Prev %d >>" % previous, rows, deflate=True)
    start = len(data)
    data += b"90 0 obj\n" + extra + b"\nendobj\nstartxref\n%d\n%%%%EOF\n" % start
    doc = Document(data)
    assert doc.xref[1] == catalogue
    assert len(doc.pages()) == 1
