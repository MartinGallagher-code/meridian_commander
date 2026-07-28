"""The stdlib .pptx reader and the slide browser.

Fixtures are real zip archives with the part layout PowerPoint writes, so what
is under test is the format rather than a stand-in for it.
"""

from __future__ import annotations

import curses

import pytest

from meridian_commander import ooxml, pptx
from meridian_commander.browsers import viewer_for
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.pptx import SlideView
from meridian_commander.viewer import Viewer

from support import (
    PPTX_A,
    PPTX_P,
    XLSX_PKG,
    notes_xml,
    pptx_p,
    pptx_parts,
    pptx_shape,
    pptx_table,
    script_newwin,
    simple_pptx,
    slide_xml,
    with_curses_screen,
    write,
    write_pptx,
    xlsx_bytes,
)

TITLE_SLIDE = slide_xml(
    pptx_shape(pptx_p("Meridian Commander"), "ctrTitle")
    + pptx_shape(pptx_p("A two-pane terminal file manager")))
AGENDA_SLIDE = slide_xml(
    pptx_shape(pptx_p("Agenda"), "title")
    + pptx_shape(pptx_p("Why panes") + pptx_p("Remote locations", 1)
                 + pptx_p("Plug-ins, café — dash", 2)))
TABLE_SLIDE = slide_xml(
    pptx_shape(pptx_p("Numbers"), "title")
    + pptx_table([["Region", "Revenue"], ["North", "1234.5"]]))


def parse(slides, notes=None, order=None, name="deck.pptx"):
    return pptx.parse_presentation(simple_pptx(slides, notes, order), name)


@pytest.fixture
def deck(fs, tmp_path):
    path = write_pptx(str(tmp_path / "deck.pptx"),
                      [TITLE_SLIDE, AGENDA_SLIDE, TABLE_SLIDE],
                      notes={1: notes_xml(pptx_p("Mention the grid."))})
    return SlideView(fs, path)


# -- names ---------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("deck.pptx", True), ("SHOW.PPTM", True), ("Deck.PptX", True),
    ("legacy.ppt", False), ("notes.txt", False), ("book.xlsx", False),
])
def test_is_presentation(name, expected):
    assert pptx.is_presentation(name) is expected


# -- paragraph text ------------------------------------------------------------
def test_runs_are_glued_back_together():
    slide = parse([slide_xml(pptx_shape(pptx_p("several words here")))]).slides[0]
    assert slide.lines == ["  * several words here"]


def test_a_line_break_becomes_a_space():
    body = ("<a:p><a:r><a:t>before</a:t></a:r><a:br/>"
            "<a:r><a:t>after</a:t></a:r></a:p>")
    assert parse([slide_xml(pptx_shape(body))]).slides[0].lines == \
        ["  * before after"]


def test_a_field_is_not_content():
    # A slide number is furniture the application fills in.
    body = ('<a:p><a:r><a:t>real text</a:t></a:r>'
            '<a:fld type="slidenum"><a:t>3</a:t></a:fld></a:p>')
    assert parse([slide_xml(pptx_shape(body))]).slides[0].lines == \
        ["  * real text"]


def test_an_empty_paragraph_is_dropped():
    body = "<a:p/>" + pptx_p("kept")
    assert parse([slide_xml(pptx_shape(body))]).slides[0].lines == ["  * kept"]


@pytest.mark.parametrize("level, indent", [(0, "  "), (1, "    "), (3, "        ")])
def test_outline_levels_become_indents(level, indent):
    slide = parse([slide_xml(pptx_shape(pptx_p("item", level)))]).slides[0]
    assert slide.lines == [indent + "* item"]


def test_a_level_that_is_not_a_number():
    body = '<a:p><a:pPr lvl="deep"/><a:r><a:t>item</a:t></a:r></a:p>'
    assert parse([slide_xml(pptx_shape(body))]).slides[0].lines == ["  * item"]


def test_a_negative_level_is_clamped():
    body = '<a:p><a:pPr lvl="-2"/><a:r><a:t>item</a:t></a:r></a:p>'
    assert parse([slide_xml(pptx_shape(body))]).slides[0].lines == ["  * item"]


# -- titles --------------------------------------------------------------------
@pytest.mark.parametrize("placeholder", ["title", "ctrTitle", "TITLE"])
def test_a_title_placeholder_becomes_the_title(placeholder):
    slide = parse([slide_xml(pptx_shape(pptx_p("The Title"), placeholder)
                             + pptx_shape(pptx_p("body")))]).slides[0]
    assert slide.title == "The Title"
    assert slide.lines == ["  * body"]


def test_a_shape_that_is_not_a_placeholder_is_body_text():
    slide = parse([slide_xml(pptx_shape(pptx_p("just a box")))]).slides[0]
    assert slide.title == ""
    assert slide.lines == ["  * just a box"]


def test_a_placeholder_with_no_type_at_all():
    # PowerPoint writes bare <p:ph/> for an unnamed placeholder.
    shape = ("<p:sp><p:nvSpPr><p:nvPr><p:ph/></p:nvPr></p:nvSpPr>"
             f"<p:txBody>{pptx_p('unnamed')}</p:txBody></p:sp>")
    slide = parse([slide_xml(shape)]).slides[0]
    assert slide.title == "" and slide.lines == ["  * unnamed"]


def test_paragraph_properties_without_an_outline_level():
    body = "<a:p><a:pPr/><a:r><a:t>item</a:t></a:r></a:p>"
    assert parse([slide_xml(pptx_shape(body))]).slides[0].lines == ["  * item"]


def test_a_body_placeholder_is_not_a_title():
    slide = parse([slide_xml(pptx_shape(pptx_p("bullet"), "body"))]).slides[0]
    assert slide.title == "" and slide.lines == ["  * bullet"]


def test_only_the_first_title_shape_wins():
    slide = parse([slide_xml(pptx_shape(pptx_p("First"), "title")
                             + pptx_shape(pptx_p("Second"), "title"))]).slides[0]
    assert slide.title == "First"
    assert slide.lines == ["  * Second"]


def test_a_slide_is_labelled_by_its_title_or_its_number():
    deck = parse([AGENDA_SLIDE, slide_xml("")])
    assert deck.slides[0].label == "Agenda"
    assert deck.slides[1].label == "(slide 2)"


# -- tables --------------------------------------------------------------------
def test_a_table_becomes_aligned_columns():
    assert parse([TABLE_SLIDE]).slides[0].lines == [
        "  Region  Revenue",
        "  ------  -------",
        "  North   1234.5",
    ]


def test_a_ragged_table_pads_the_short_rows():
    slide = parse([slide_xml(pptx_table([["a", "b"], ["c"]]))]).slides[0]
    assert slide.lines == ["  a  b", "  -  -", "  c"]


def test_non_table_children_are_ignored():
    frame = ("<p:graphicFrame><a:graphic><a:tbl><a:tblPr/>"
             "<a:tr><a:gridCol/><a:tc><a:txBody><a:p><a:r><a:t>only</a:t>"
             "</a:r></a:p></a:txBody></a:tc></a:tr></a:tbl>"
             "</a:graphic></p:graphicFrame>")
    assert parse([slide_xml(frame)]).slides[0].lines == ["  only", "  ----"]


def test_an_empty_table_draws_nothing():
    frame = "<p:graphicFrame><a:graphic><a:tbl/></a:graphic></p:graphicFrame>"
    assert parse([slide_xml(frame)]).slides[0].lines == []


def test_a_table_row_is_capped(monkeypatch):
    monkeypatch.setattr(pptx, "MAX_TABLE_COLS", 2)
    slide = parse([slide_xml(pptx_table([["a", "b", "c", "d"]]))]).slides[0]
    assert slide.lines == ["  a  b", "  -  -"]


def test_a_runaway_cell_is_clamped(monkeypatch):
    monkeypatch.setattr(pptx, "MAX_CELL_WIDTH", 4)
    slide = parse([slide_xml(pptx_table([["x" * 30, "b"]]))]).slides[0]
    assert slide.lines == ["  xxxx  b", "  ----  -"]


# -- slide order ---------------------------------------------------------------
def test_slides_come_back_in_declaration_order():
    deck = parse([TITLE_SLIDE, AGENDA_SLIDE, TABLE_SLIDE])
    assert [s.title for s in deck.slides] == \
        ["Meridian Commander", "Agenda", "Numbers"]


def test_the_order_is_the_declared_one_not_the_file_names():
    # The parts keep their names; only sldIdLst is reordered, which is exactly
    # what happens when slides are dragged about in PowerPoint.
    deck = parse([TITLE_SLIDE, AGENDA_SLIDE, TABLE_SLIDE], order=[1, 2, 0])
    assert [s.title for s in deck.slides] == \
        ["Agenda", "Numbers", "Meridian Commander"]
    assert [s.number for s in deck.slides] == [1, 2, 3]


def test_a_slide_with_no_relationship_keeps_its_place():
    parts = pptx_parts([TITLE_SLIDE, AGENDA_SLIDE])
    parts["ppt/presentation.xml"] = \
        parts["ppt/presentation.xml"].replace('r:id="rIdS0"', "")
    deck = pptx.parse_presentation(xlsx_bytes(parts), "d.pptx")
    assert [s.title for s in deck.slides] == ["", "Agenda"]


def test_a_slide_whose_relationship_points_nowhere():
    parts = pptx_parts([TITLE_SLIDE])
    parts["ppt/presentation.xml"] = \
        parts["ppt/presentation.xml"].replace('r:id="rIdS0"', 'r:id="rIdGone"')
    deck = pptx.parse_presentation(xlsx_bytes(parts), "d.pptx")
    assert deck.slides[0].title == "" and deck.slides[0].lines == []


def test_a_slide_part_missing_from_the_archive():
    parts = pptx_parts([TITLE_SLIDE])
    del parts["ppt/slides/slide1.xml"]
    deck = pptx.parse_presentation(xlsx_bytes(parts), "d.pptx")
    assert deck.slides[0].lines == []


def test_unexpected_children_are_ignored():
    parts = pptx_parts([TITLE_SLIDE])
    parts["ppt/presentation.xml"] = parts["ppt/presentation.xml"].replace(
        "<p:sldIdLst>", "<p:sldMasterIdLst/><p:sldIdLst><p:other/>")
    deck = pptx.parse_presentation(xlsx_bytes(parts), "d.pptx")
    assert [s.title for s in deck.slides] == ["Meridian Commander"]


def test_the_slide_cap_is_reported(monkeypatch):
    monkeypatch.setattr(pptx, "MAX_SLIDES", 2)
    deck = parse([TITLE_SLIDE, AGENDA_SLIDE, TABLE_SLIDE])
    assert len(deck.slides) == 2 and deck.truncated is True


def test_a_slide_with_too_much_on_it(monkeypatch):
    monkeypatch.setattr(pptx, "MAX_LINES_PER_SLIDE", 3)
    body = "".join(pptx_p(f"line {i}") for i in range(10))
    slide = parse([slide_xml(pptx_shape(body))]).slides[0]
    assert slide.lines == ["  * line 0", "  * line 1", "  * line 2",
                           "(slide truncated)"]


# -- notes ---------------------------------------------------------------------
def test_speaker_notes_are_read():
    deck = parse([TITLE_SLIDE], notes={0: notes_xml(pptx_p("Say this bit."))})
    assert deck.slides[0].notes == ["Say this bit."]


def test_a_slide_with_no_notes():
    assert parse([TITLE_SLIDE]).slides[0].notes == []


def test_a_notes_part_missing_from_the_archive():
    parts = pptx_parts([TITLE_SLIDE], notes={0: notes_xml(pptx_p("x"))})
    del parts["ppt/notesSlides/notesSlide1.xml"]
    deck = pptx.parse_presentation(xlsx_bytes(parts), "d.pptx")
    assert deck.slides[0].notes == []


def test_the_slide_number_field_is_left_out_of_the_notes():
    notes = notes_xml(pptx_p("A real note")
                      + '<a:p><a:fld type="slidenum"><a:t>2</a:t></a:fld></a:p>')
    deck = parse([TITLE_SLIDE], notes={0: notes})
    assert deck.slides[0].notes == ["A real note"]


# -- refusals ------------------------------------------------------------------
def test_a_file_that_is_not_a_zip_archive():
    with pytest.raises(ooxml.OoxmlError, match="not a readable"):
        pptx.parse_presentation(b"just some bytes", "x.pptx")


def test_an_archive_with_no_presentation_part():
    parts = pptx_parts([TITLE_SLIDE])
    del parts["ppt/presentation.xml"]
    with pytest.raises(ooxml.OoxmlError, match="no presentation part"):
        pptx.parse_presentation(xlsx_bytes(parts), "x.pptx")


def test_a_presentation_that_declares_no_slides():
    with pytest.raises(ooxml.OoxmlError, match="declares no slides"):
        parse([])


def test_a_presentation_part_that_is_not_valid_xml():
    parts = pptx_parts([TITLE_SLIDE])
    parts["ppt/presentation.xml"] = "<p:presentation><p:sldIdLst>"
    with pytest.raises(ooxml.OoxmlError, match="not valid XML"):
        pptx.parse_presentation(xlsx_bytes(parts), "x.pptx")


def test_a_file_too_large_to_hold_in_memory(tmp_path, monkeypatch):
    path = write_pptx(str(tmp_path / "d.pptx"), [TITLE_SLIDE])
    monkeypatch.setattr(ooxml, "MAX_BYTES", 10)
    with pytest.raises(ooxml.OoxmlError, match="cannot be read in part"):
        pptx.read_presentation(LocalFileSystem(), path)


def test_read_presentation_through_a_backend(tmp_path):
    path = write_pptx(str(tmp_path / "sub" / "deck.pptx"), [AGENDA_SLIDE])
    deck = pptx.read_presentation(LocalFileSystem(), path)
    assert deck.name == "deck.pptx" and deck.slides[0].title == "Agenda"


# -- the browser ---------------------------------------------------------------
def _run(monkeypatch, view, keys, rows=14, cols=60):
    captured = script_newwin(monkeypatch, keys)
    with_curses_screen(rows, cols, view.run)
    return captured["window"]


def test_a_deck_opens_in_the_slide_browser(fs, tmp_path):
    path = write_pptx(str(tmp_path / "deck.pptx"), [TITLE_SLIDE])
    write(str(tmp_path / "notes.txt"), "plain")
    assert isinstance(viewer_for(fs, path), SlideView)
    assert isinstance(viewer_for(fs, str(tmp_path / "notes.txt")), Viewer)


def test_the_first_slide_is_shown(deck):
    assert deck.error is None
    assert deck.index == 0 and deck.slide.title == "Meridian Commander"


def test_a_deck_that_cannot_be_read_reports_itself(fs, tmp_path):
    write(str(tmp_path / "fake.pptx"), "not a deck at all")
    view = SlideView(fs, str(tmp_path / "fake.pptx"))
    assert "not a readable" in view.error
    # A stand-in slide keeps every other path working on the error screen.
    assert view.slide.lines == []


def test_a_deck_that_is_not_there_reports_itself(fs, tmp_path):
    view = SlideView(fs, str(tmp_path / "missing.pptx"))
    assert view.error and view.slide.lines == []


def test_a_slide_renders_its_title_ruled(deck):
    deck.index = 1
    assert deck.slide_lines(deck.slide)[:2] == ["Agenda", "======"]


def test_a_slide_with_nothing_on_it_says_so(fs, tmp_path):
    path = write_pptx(str(tmp_path / "d.pptx"), [slide_xml("")])
    view = SlideView(fs, path)
    assert view.slide_lines(view.slide) == ["(this slide has no text on it)"]


def test_notes_are_shown_only_when_asked_for(deck):
    deck.index = 1
    assert "Notes" not in deck.slide_lines(deck.slide)
    deck.toggle_notes()
    rendered = deck.slide_lines(deck.slide)
    assert "Notes" in rendered and "  Mention the grid." in rendered


def test_a_slide_with_notes_turned_on_but_none_of_its_own(deck):
    deck.index = 0
    deck.toggle_notes()
    assert "  (none)" in deck.slide_lines(deck.slide)


def test_long_lines_are_wrapped_to_the_screen(fs, tmp_path):
    path = write_pptx(str(tmp_path / "d.pptx"),
                      [slide_xml(pptx_shape(pptx_p("word " * 40)))])
    view = SlideView(fs, path)
    assert all(len(row) <= 30 for row in view.display(30))


def test_the_rendering_is_cached_per_slide_and_width(deck):
    first = deck.display(40)
    assert deck.display(40) is first
    assert deck.display(20) is not first


# -- movement ------------------------------------------------------------------
def test_moving_between_slides_wraps_around(deck):
    deck.step(1)
    assert deck.index == 1
    deck.step(-1)
    assert deck.index == 0
    deck.step(-1)
    assert deck.index == 2
    deck.step(1)
    assert deck.index == 0


def test_a_deck_with_one_slide_says_so(fs, tmp_path):
    path = write_pptx(str(tmp_path / "one.pptx"), [TITLE_SLIDE])
    view = SlideView(fs, path)
    view.step(1)
    assert view.index == 0 and "only one slide" in view.notice


def test_going_to_a_slide_clamps_and_resets_the_scroll(deck):
    deck.top = 5
    deck.go_to(99)
    assert deck.index == 2 and deck.top == 0
    deck.go_to(-3)
    assert deck.index == 0


@pytest.mark.parametrize("keys, index", [
    ([curses.KEY_RIGHT], 1), ([9], 1), ([ord("]")], 1), ([ord(" ")], 1),
    ([curses.KEY_LEFT], 2), ([curses.KEY_BTAB], 2), ([ord("[")], 2),
])
def test_the_slide_keys(monkeypatch, deck, keys, index):
    _run(monkeypatch, deck, keys + [ord("q")])
    assert deck.index == index


def test_scrolling_within_a_slide(monkeypatch, fs, tmp_path):
    body = "".join(pptx_p(f"line {i}") for i in range(60))
    path = write_pptx(str(tmp_path / "d.pptx"), [slide_xml(pptx_shape(body))])
    view = SlideView(fs, path)
    _run(monkeypatch, view, [curses.KEY_DOWN, ord("j"), ord("q")])
    assert view.top == 2
    _run(monkeypatch, view, [curses.KEY_UP, ord("k"), ord("q")])
    assert view.top == 0
    _run(monkeypatch, view, [curses.KEY_NPAGE, ord("q")], rows=14)
    assert view.top == 12
    _run(monkeypatch, view, [curses.KEY_PPAGE, ord("q")], rows=14)
    assert view.top == 0
    _run(monkeypatch, view, [curses.KEY_UP, ord("q")])
    assert view.top == 0


def test_end_jumps_to_the_last_slide(monkeypatch, deck):
    _run(monkeypatch, deck, [curses.KEY_END, ord("q")])
    assert deck.index == 2
    _run(monkeypatch, deck, [curses.KEY_HOME, ord("q")])
    assert deck.top == 0


def test_the_notes_key(monkeypatch, deck):
    _run(monkeypatch, deck, [ord("t"), ord("q")])
    assert deck.show_notes is True
    _run(monkeypatch, deck, [ord("T"), ord("q")])
    assert deck.show_notes is False


# -- search --------------------------------------------------------------------
def test_finding_a_word_moves_to_its_slide(deck):
    deck.set_search("agenda")
    assert deck.find_next(1) is True and deck.index == 1


def test_search_looks_in_titles_body_and_notes(deck):
    for pattern, index in [("meridian", 0), ("remote locations", 1),
                           ("mention the grid", 1), ("revenue", 2)]:
        deck.go_to(2 if index != 2 else 0)
        deck.set_search(pattern)
        assert deck.find_next(1) is True
        assert deck.index == index, pattern


def test_search_is_case_sensitive_when_the_pattern_is_not_lowercase(deck):
    deck.set_search("AGENDA")
    assert deck.find_next(1) is False
    deck.set_search("Agenda")
    assert deck.find_next(1) is True


def test_searching_backwards_and_for_nothing(deck):
    deck.set_search("numbers")
    assert deck.find_next(-1) is True and deck.index == 2
    deck.set_search("zebra")
    assert deck.find_next(1) is False
    deck.set_search("")
    assert deck.find_next(1) is False


def test_the_search_prompt_drives_the_search(monkeypatch, deck):
    from meridian_commander import dialogs
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "agenda")
    _run(monkeypatch, deck, [ord("/"), ord("q")])
    assert deck.index == 1


def test_a_search_prompt_that_finds_nothing_says_so(monkeypatch, deck):
    from meridian_commander import dialogs
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "zebra")
    assert "not found" in _run(monkeypatch, deck, [ord("/"), ord("q")]).text


def test_a_cancelled_search_prompt_changes_nothing(monkeypatch, deck):
    from meridian_commander import dialogs
    deck.set_search("agenda")
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: None)
    _run(monkeypatch, deck, [curses.KEY_F7, ord("q")])
    assert deck.search == "agenda" and deck.index == 0


def test_an_empty_search_pattern_clears_the_search(monkeypatch, deck):
    from meridian_commander import dialogs
    deck.set_search("agenda")
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "")
    _run(monkeypatch, deck, [ord("/"), ord("q")])
    assert deck.search == ""


def test_the_next_and_previous_keys(monkeypatch, deck):
    deck.set_search("numbers")
    _run(monkeypatch, deck, [ord("n"), ord("q")])
    assert deck.index == 2
    deck.go_to(0)
    _run(monkeypatch, deck, [ord("N"), ord("q")])
    assert deck.index == 2
    deck.go_to(0)
    _run(monkeypatch, deck, [ord("p"), ord("q")])
    assert deck.index == 2


def test_next_and_previous_report_when_there_is_no_match(monkeypatch, deck):
    deck.set_search("zebra")
    assert "no match" in _run(monkeypatch, deck, [ord("n"), ord("q")]).text
    assert "no match" in _run(monkeypatch, deck, [ord("N"), ord("q")]).text


def test_next_without_a_pattern_does_nothing(monkeypatch, deck):
    window = _run(monkeypatch, deck, [ord("n"), ord("N"), ord("q")])
    assert "no match" not in window.text and deck.index == 0


# -- drawing, on a real screen -------------------------------------------------
def _draw(view, rows=14, cols=60):
    from support import _KeyScript

    def run(stdscr):
        window = _KeyScript(curses.newwin(rows, cols, 0, 0), [])
        view.draw(window)
        return window.drawn
    return with_curses_screen(rows, cols, run)


def _texts(drawn):
    return " ".join(a[2] for a in drawn if len(a) > 2 and isinstance(a[2], str))


def test_the_title_bar_counts_the_slides(deck):
    assert "deck.pptx -- 1/3" in _texts(_draw(deck))


def test_the_slide_is_drawn(deck):
    deck.index = 1
    text = _texts(_draw(deck))
    assert "Agenda" in text and "Why panes" in text and "café" in text


def test_the_footer_names_the_slide_and_the_search(deck):
    deck.index = 1
    deck.set_search("panes")
    text = _texts(_draw(deck))
    assert "Agenda" in text and "/panes" in text


def test_a_notice_replaces_the_footer(deck):
    deck.notice = " a passing remark "
    assert "a passing remark" in _texts(_draw(deck))


def test_a_truncated_deck_says_so(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(pptx, "MAX_SLIDES", 1)
    path = write_pptx(str(tmp_path / "big.pptx"), [TITLE_SLIDE, AGENDA_SLIDE])
    assert "[truncated]" in _texts(_draw(SlideView(fs, path)))


def test_the_error_screen_replaces_the_slide(fs, tmp_path):
    write(str(tmp_path / "fake.pptx"), "not a deck")
    text = _texts(_draw(SlideView(fs, str(tmp_path / "fake.pptx"))))
    assert "Cannot read" in text and "[q]uit" in text


def test_a_heading_is_the_row_above_its_rule(deck):
    deck.index = 1
    rows = deck.display(60)
    assert deck._is_heading(0, rows) is True     # "Agenda" above "======"
    assert deck._is_heading(1, rows) is False    # the rule itself
    assert deck._is_heading(len(rows) - 1, rows) is False


@pytest.mark.parametrize("rows, cols", [
    (1, 20), (2, 20), (3, 20), (4, 1), (4, 2), (4, 8),
])
def test_drawing_survives_a_screen_with_no_room_on_it(deck, rows, cols):
    # The window is real, so curses refuses these writes for real; what is
    # under test is that every refusal is caught rather than reaching the user.
    _draw(deck, rows=rows, cols=cols)


@pytest.mark.parametrize("rows", [1, 2, 3])
def test_the_error_screen_survives_a_screen_with_no_room(fs, tmp_path, rows):
    write(str(tmp_path / "fake.pptx"), "not a deck")
    _draw(SlideView(fs, str(tmp_path / "fake.pptx")), rows=rows, cols=20)


def test_scrolling_past_the_end_of_a_slide_is_clamped(deck):
    deck.top = 999
    _draw(deck)
    assert deck.top < 999


def test_the_browser_closes_on_every_quit_key(monkeypatch, deck):
    for key in (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10):
        _run(monkeypatch, deck, [key])


def test_an_unhandled_key_is_ignored(monkeypatch, deck):
    _run(monkeypatch, deck, [ord("z"), curses.KEY_RESIZE, ord("q")])
    assert deck.index == 0


def test_the_fixture_builder_writes_the_namespaces_a_producer_does():
    parts = pptx_parts([TITLE_SLIDE])
    assert PPTX_P in parts["ppt/presentation.xml"]
    assert PPTX_A in parts["ppt/slides/slide1.xml"]
    assert XLSX_PKG in parts["_rels/.rels"]
