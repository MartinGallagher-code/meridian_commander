"""The Markdown renderer and its viewer.

Assertions are on the rendered *text and styles* together, because the styles
are half the rendering: a test that only checked the text would pass on a
version that had stopped emphasising anything.
"""

from __future__ import annotations

import curses

import pytest

from meridian_commander import markdown
from meridian_commander.browsers import viewer_for
from meridian_commander.markdown import (
    BOLD,
    CODE,
    DIM,
    ITALIC,
    LINK,
    MarkdownView,
    is_markdown,
    render,
)
from meridian_commander import theme
from meridian_commander.viewer import STYLE_ROLES, Viewer

from support import _KeyScript, script_newwin, with_curses_screen, write


def lines(text):
    return render(text).lines


def styled(text):
    """``[(line, style)]`` for the rendered source."""
    result = render(text)
    return list(zip(result.lines, result.styles))


def styles_of(text, needle):
    """The style codes under ``needle`` in the first line that contains it."""
    for line, style in styled(text):
        at = line.find(needle)
        if at >= 0:
            return style[at:at + len(needle)]
    raise AssertionError(f"{needle!r} not in {lines(text)}")


# -- names ---------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("README.md", True), ("NOTES.MARKDOWN", True), ("a.mdown", True),
    ("a.mkd", True), ("a.mdwn", True),
    ("notes.txt", False), ("report.docx", False), ("md", False),
])
def test_is_markdown(name, expected):
    assert is_markdown(name) is expected


# -- inline emphasis -----------------------------------------------------------
@pytest.mark.parametrize("source, text, code", [
    ("**bold**", "bold", BOLD),
    ("__bold__", "bold", BOLD),
    ("*italic*", "italic", ITALIC),
    ("_italic_", "italic", ITALIC),
    ("`code`", "code", CODE),
    ("``code with ` inside``", "code with ` inside", CODE),
    ("~~struck~~", "struck", DIM),
])
def test_a_span_loses_its_markers_and_gains_a_style(source, text, code):
    assert lines(source) == [text]
    assert styles_of(source, text) == code * len(text)


def test_emphasis_inside_emphasis():
    # The inner span keeps its own code; the rest of the outer span keeps the
    # outer one, which is what nesting has to mean.
    assert lines("**bold with *italic* inside**") == ["bold with italic inside"]
    assert styles_of("**bold with *italic* inside**", "italic") == ITALIC * 6
    assert styles_of("**bold with *italic* inside**", "bold") == BOLD * 4


def test_text_around_a_span_stays_plain():
    assert styled("plain **bold** plain") == \
        [("plain bold plain", "      BBBB      ")]


def test_an_escaped_marker_is_not_a_span():
    # The backslash stops the span forming, and then leaves the file's text.
    assert lines(r"\*not italic\* and \`not code\`") == \
        ["*not italic* and `not code`"]
    assert styled(r"\*not italic\*")[0][1].strip() == ""


def test_a_backslash_before_something_unescapable_is_kept():
    assert lines(r"C:\path\to\file") == [r"C:\path\to\file"]


def test_a_trailing_backslash_survives():
    assert lines("ends with a backslash \\") == ["ends with a backslash \\"]


def test_underscores_inside_a_word_are_not_emphasis():
    assert lines("some_variable_name") == ["some_variable_name"]


# -- links ---------------------------------------------------------------------
def test_a_link_is_numbered_and_listed_at_the_end():
    out = lines("see [the docs](https://example.com/docs) for more")
    assert out[0] == "see the docs[1] for more"
    assert out[-1] == "  [1] https://example.com/docs"
    assert "Links" in out


def test_link_text_is_underlined_and_the_number_dimmed():
    source = "see [the docs](https://x.io) now"
    assert styles_of(source, "the docs") == LINK * 8
    assert styles_of(source, "[1]") == DIM * 3


def test_a_reference_link_resolves_its_definition():
    out = lines("see [the docs][d] now\n\n[d]: https://example.com/d\n")
    assert out[0] == "see the docs[1] now"
    assert out[-1] == "  [1] https://example.com/d"


def test_a_shortcut_reference_uses_its_own_label():
    out = lines("see [docs][] now\n\n[docs]: https://example.com/d\n")
    assert out[0] == "see docs[1] now"


def test_a_reference_with_no_definition_keeps_its_text():
    out = lines("see [the docs][nope] now")
    assert out == ["see the docs now"]        # styled, but with no number


def test_a_definition_line_is_not_shown_as_content():
    assert lines("[d]: https://example.com/d\n\ntext\n") == ["text"]


def test_an_autolink_shows_the_url():
    assert lines("mail <mailto:a@b.com> now") == ["mail mailto:a@b.com now"]
    assert styles_of("<https://x.io>", "https://x.io") == LINK * 12


def test_an_image_becomes_a_note():
    assert lines("![a badge](img.svg)") == ["[image: a badge]"]
    assert lines("![](img.svg)") == ["[image]"]


def test_a_badge_is_an_image_inside_a_link():
    # The commonest construct in a README, and the one neither the plain
    # image nor the plain link pattern can read on its own.
    out = lines("[![PyPI](https://img.shields.io/pypi/v/x)](https://pypi.org/p/x)")
    assert out[0] == "[image: PyPI][1]"
    assert out[-1] == "  [1] https://pypi.org/p/x"


def test_a_badge_with_no_alt_text():
    assert lines("[![](img.svg)](https://x.io)")[0] == "[image][1]"


def test_a_linked_image_with_no_destination():
    assert lines("[![alt](img.svg)]()") == ["[image: alt]"]


def test_a_file_with_no_links_has_no_link_list():
    assert "Links" not in lines("just text")


# -- headings ------------------------------------------------------------------
@pytest.mark.parametrize("source, text, rule", [
    ("# One", "One", "==="),
    ("## Two", "Two", "---"),
])
def test_a_heading_is_ruled_underneath(source, text, rule):
    assert lines(source) == [text, rule]


def test_a_deeper_heading_is_bold_with_no_rule():
    assert lines("### Three") == ["Three"]
    assert styles_of("### Three", "Three") == BOLD * 5


def test_closing_hashes_are_dropped():
    assert lines("## Two ##") == ["Two", "---"]


def test_a_heading_can_hold_a_span():
    assert lines("# A `code` heading") == ["A code heading", "=============="]
    assert styles_of("# A `code` heading", "code") == CODE * 4


def test_a_setext_heading():
    assert lines("Title\n=====\n") == ["Title", "====="]
    assert lines("Sub\n---\n") == ["Sub", "---"]


def test_a_setext_underline_with_no_paragraph_is_a_rule():
    assert lines("---\ntext\n")[0].startswith("─")


def test_a_heading_with_no_text_gets_no_rule():
    assert lines("#  ") == []


# -- lists ---------------------------------------------------------------------
@pytest.mark.parametrize("marker", ["-", "*", "+"])
def test_a_bullet_list(marker):
    assert lines(f"{marker} first\n{marker} second") == \
        ["  • first", "  • second"]


@pytest.mark.parametrize("marker", ["1.", "2)"])
def test_an_ordered_list_keeps_the_author_numbers(marker):
    # Unlike a Word document, the numbers are literally in the file.
    assert lines(f"{marker} item") == [f"  {marker} item"]


def test_nesting_indents_by_depth():
    assert lines("- top\n  - nested\n    - deeper") == \
        ["  • top", "    • nested", "      • deeper"]


def test_a_task_list():
    assert lines("- [x] done\n- [ ] todo") == ["  • ☑ done", "  • ☐ todo"]


def test_a_list_marker_is_dimmed_and_the_content_is_not():
    assert styles_of("- **bold** item", "•") == DIM
    assert styles_of("- **bold** item", "bold") == BOLD * 4


def test_a_wrapped_list_item_is_joined():
    assert lines("- one long item\n  continued here\n- next") == \
        ["  • one long item continued here", "  • next"]


def test_a_continuation_stops_at_the_next_block():
    # A heading opens with a blank line of its own, which is what stops the
    # item swallowing it.
    assert lines("- item\n# Heading") == ["  • item", "", "Heading", "======="]
    assert lines("- item\n\ntext") == ["  • item", "", "text"]


# -- quotes, rules, code -------------------------------------------------------
def test_a_blockquote_gets_a_gutter():
    assert lines("> quoted") == ["│ quoted"]
    assert styles_of("> quoted", "│ ") == DIM * 2


def test_a_nested_blockquote_gets_two():
    assert lines(">> deeper") == ["│ │ deeper"]


def test_a_quote_can_hold_a_span():
    assert styles_of("> a **bold** quote", "bold") == BOLD * 4


@pytest.mark.parametrize("source", ["---", "***", "___", "- - -", "* * *"])
def test_a_thematic_break_becomes_a_rule(source):
    out = lines(f"text\n\n{source}\n")
    assert out[-1] == "─" * markdown.RULE_WIDTH or "─" * 10 in "".join(out)


def test_a_fenced_code_block_is_indented_and_left_alone():
    assert lines("```bash\npip install x\n```") == ["    pip install x"]
    assert styles_of("```\ncode\n```", "code") == DIM * 4


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_both_fence_styles(fence):
    assert lines(f"{fence}\nbody\n{fence}") == ["    body"]


def test_markers_inside_a_fence_are_not_spans():
    assert lines("```\n**not bold**\n```") == ["    **not bold**"]


def test_an_unclosed_fence_runs_to_the_end():
    assert lines("```\none\ntwo") == ["    one", "    two"]


def test_code_and_tables_are_marked_unwrappable():
    result = render("```\na line of code\n```")
    assert result.nowrap == {0}


# -- tables --------------------------------------------------------------------
TABLE = ("| Format | Reader    |\n"
         "|:-------|----------:|\n"
         "| xlsx   | xlsx.py   |\n")


def test_a_table_is_aligned_with_a_rule_under_the_header():
    # "Reader" is right-aligned by the delimiter row, so it sits at the end
    # of a column as wide as "xlsx.py".
    assert lines(TABLE) == [
        "  Format   Reader",
        "  ──────  ───────",
        "  xlsx    xlsx.py",
    ]


def test_table_alignment_is_honoured():
    out = lines("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |\n")
    assert out[-1] == "  1  2  3"
    # left in a 3-wide column, centred in the next, right in the last.
    wide = lines("| aaa | bbb | ccc |\n|:----|:---:|----:|\n| 1 | 2 | 3 |\n")
    assert wide[-1] == "  1     2     3"


def test_a_table_without_outer_pipes():
    assert lines("a | b\n--|--\n1 | 2\n")[-1] == "  1  2"


def test_a_body_row_wider_than_the_header():
    # The delimiter row has to match the header, but a body row may still run
    # over it; the extra columns default to left-aligned.
    assert lines("| a |\n|---|\n| 1 | 2 |\n")[-1] == "  1  2"


def test_a_ragged_table_pads_the_short_rows():
    assert lines("| a | b |\n|---|---|\n| 1 |\n")[-1] == "  1"


def test_a_table_cell_can_hold_a_span():
    assert styles_of(TABLE, "xlsx.py") == " " * 7      # plain in this table
    assert styles_of("| a |\n|---|\n| `x` |\n", "x") == CODE


def test_a_runaway_cell_is_clamped(monkeypatch):
    monkeypatch.setattr(markdown, "MAX_CELL_WIDTH", 5)
    assert lines("| a |\n|---|\n| " + "x" * 40 + " |\n")[-1] == "  xxxxx"


def test_a_table_row_is_capped(monkeypatch):
    monkeypatch.setattr(markdown, "MAX_TABLE_COLS", 2)
    assert lines("| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |\n")[-1] == "  1  2"


@pytest.mark.parametrize("source", [
    "no pipes here\nnor here",
    "| a | b |",                       # nothing under it
    "| a | b |\nnot a delimiter\n",
    "| a | b |\n|---|\n",              # the counts must match
])
def test_things_that_are_not_tables(source):
    assert "─" not in "".join(lines(source))


# -- paragraphs ----------------------------------------------------------------
def test_soft_wrapped_lines_are_joined():
    # This is why blocks are parsed before inline: the span opens on one
    # source line and closes on the next.
    assert lines("a *span over\ntwo lines* here") == ["a span over two lines here"]
    assert styles_of("a *span over\ntwo lines* here", "span over two lines") == \
        ITALIC * 19


@pytest.mark.parametrize("ending", ["  ", "\\"])
def test_a_hard_break_splits_the_paragraph(ending):
    assert lines(f"first{ending}\nsecond") == ["first", "second"]


def test_blank_lines_are_not_stacked():
    assert lines("a\n\n\n\nb") == ["a", "", "b"]


def test_a_document_never_opens_with_a_blank_line():
    assert lines("\n\n# Title") == ["Title", "====="]


# -- front matter --------------------------------------------------------------
def test_front_matter_is_shown_dimmed_not_mistaken_for_a_heading():
    out = lines("---\ntitle: x\n---\n\n# Real\n")
    assert out == ["title: x", "", "Real", "===="]
    assert styles_of("---\ntitle: x\n---\n", "title: x") == DIM * 8


def test_front_matter_can_close_with_dots():
    assert lines("---\na: 1\n...\n\ntext")[0] == "a: 1"


def test_an_unterminated_front_matter_is_just_a_rule():
    assert lines("---\nnot really front matter")[0].startswith("─")


def test_a_file_that_does_not_start_with_a_marker():
    assert lines("text\n---\n") == ["text", "----"]     # a setext heading


# -- caps and edges ------------------------------------------------------------
def test_the_line_cap_is_reported(monkeypatch):
    monkeypatch.setattr(markdown, "MAX_LINES", 3)
    result = render("\n\n".join(f"para {i}" for i in range(10)))
    assert len(result.lines) == 3 and result.truncated is True


def test_an_empty_document():
    assert render("").lines == []


def test_a_style_shorter_than_its_line_is_padded():
    out = markdown.Rendered()
    out.add("abcdef", "BB")
    assert out.styles == ["BB    "]
    out.add("ab", "DDDDDD")
    assert out.styles[1] == "DD"


def test_collect_refs():
    assert markdown.collect_refs(["[a]: http://x", "[B]: <http://y> \"t\"",
                                  "not a ref"]) == \
        {"a": "http://x", "b": "http://y"}


# -- the viewer ----------------------------------------------------------------
SAMPLE = """# Title

Some **bold** prose with `code` and [a link](https://example.com).

- a bullet
- another

```python
print("hi")
```
"""


@pytest.fixture
def doc(fs, tmp_path):
    write(str(tmp_path / "README.md"), SAMPLE)
    return MarkdownView(fs, str(tmp_path / "README.md"))


def test_markdown_opens_in_the_markdown_viewer(fs, tmp_path):
    write(str(tmp_path / "README.md"), "# x")
    write(str(tmp_path / "notes.txt"), "plain")
    assert isinstance(viewer_for(fs, str(tmp_path / "README.md")), MarkdownView)
    assert isinstance(viewer_for(fs, str(tmp_path / "notes.txt")), Viewer)


def test_the_viewer_renders_and_styles(doc):
    assert doc.error is None
    assert doc.source[:2] == ["Title", "====="]
    assert doc.styles[0] == BOLD * 5
    assert doc.wrap is True


def test_a_file_that_will_not_open_is_reported(fs, tmp_path):
    view = MarkdownView(fs, str(tmp_path / "missing.md"))
    assert view.error and view.source == [] and view.styles == []


def test_the_source_toggle_shows_the_file_as_written(doc):
    doc.toggle_source()
    assert doc.show_source is True
    assert doc.source[0] == "# Title"
    assert doc.styles == [] and doc.nowrap == set()
    doc.toggle_source()
    assert doc.show_source is False
    assert doc.source[0] == "Title" and doc.styles[0] == BOLD * 5


def test_the_title_says_which_view_it_is(doc):
    assert "Markdown: README.md" in doc._title()
    doc.toggle_source()
    assert "Source: README.md" in doc._title()


def test_the_hints_mention_the_raw_key(doc):
    assert "[r]aw" in doc._hints()


def test_a_truncated_render_is_flagged(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(markdown, "MAX_LINES", 2)
    write(str(tmp_path / "big.md"), "\n\n".join(f"para {i}" for i in range(10)))
    assert MarkdownView(fs, str(tmp_path / "big.md")).truncated is True


# -- the key loop and drawing, on a real screen --------------------------------
def _run(monkeypatch, view, keys, rows=14, cols=60):
    captured = script_newwin(monkeypatch, keys)
    with_curses_screen(rows, cols, view.run)
    return captured["window"]


def test_the_raw_key_toggles_the_view(monkeypatch, doc):
    _run(monkeypatch, doc, [ord("r"), ord("q")])
    assert doc.show_source is True
    _run(monkeypatch, doc, [ord("R"), ord("q")])
    assert doc.show_source is False


def test_a_key_the_viewer_does_not_know_is_ignored(monkeypatch, doc):
    _run(monkeypatch, doc, [ord("z"), curses.KEY_RESIZE, ord("q")])
    assert doc.show_source is False


def test_the_plain_viewer_ignores_the_same_keys(monkeypatch, fs, tmp_path):
    write(str(tmp_path / "a.txt"), "plain text\n")
    view = Viewer(fs, str(tmp_path / "a.txt"))
    _run(monkeypatch, view, [ord("r"), ord("z"), ord("q")])
    assert view.source == ["plain text", ""]


def _draw(view, rows=14, cols=60):
    def run(stdscr):
        window = _KeyScript(curses.newwin(rows, cols, 0, 0), [])
        view.draw(window)
        return window.drawn
    return with_curses_screen(rows, cols, run)


def test_styled_text_is_drawn_one_run_at_a_time(doc):
    drawn = _draw(doc)
    # "Some bold prose ..." is drawn as several writes, the bold one carrying
    # the strong-text attribute, rather than as one flat write of the line.
    bold = [a for a in drawn if len(a) > 3 and a[3] == theme.attr("editbold")]
    assert any(a[2] == "bold" for a in bold)
    code = [a for a in drawn if len(a) > 3 and a[3] == theme.attr("editcode")]
    assert any(a[2] == "code" for a in code)


def test_an_unstyled_run_is_drawn_in_the_plain_text_colour(doc):
    drawn = _draw(doc)
    plain = [a for a in drawn if len(a) > 3 and a[3] == theme.attr("edit")]
    assert any(a[2] == "a bullet" for a in plain)


def test_the_source_view_draws_unstyled(monkeypatch, doc):
    doc.toggle_source()
    drawn = _draw(doc)
    assert any("# Title" in a[2] for a in drawn if len(a) > 2)


def test_every_style_code_has_a_role():
    for code in (BOLD, ITALIC, CODE, DIM, LINK):
        assert code in STYLE_ROLES
        assert STYLE_ROLES[code] in theme.ROLES


@pytest.mark.parametrize("rows, cols", [
    (1, 20), (2, 20), (3, 20), (4, 2), (4, 8),
])
def test_drawing_survives_a_screen_with_no_room(doc, rows, cols):
    _draw(doc, rows=rows, cols=cols)


@pytest.mark.parametrize("rows", [1, 2, 3])
def test_the_error_screen_survives_a_screen_with_no_room(fs, tmp_path, rows):
    # The title and the error line were both unguarded before markdown was
    # drawn at these sizes; a short terminal raised rather than degrading.
    _draw(Viewer(fs, str(tmp_path / "missing.txt")), rows=rows, cols=20)


def test_an_unwrappable_line_keeps_its_shape(fs, tmp_path):
    write(str(tmp_path / "t.md"),
          "| a | b |\n|---|---|\n| a long cell here | another long one |\n")
    view = MarkdownView(fs, str(tmp_path / "t.md"))
    view.wrap = True
    view._flow = None
    view._reflow(12)
    # The table rows are wider than 12 and stay whole; horizontal scrolling
    # reaches the rest, because reflowing a table destroys it.
    assert any(len(line) > 12 for line in view.lines)


def test_ordinary_lines_still_wrap_around_an_unwrappable_one(fs, tmp_path):
    write(str(tmp_path / "t.md"), "one two three four five six seven eight\n\n"
          "```\nlong line of code that will not be wrapped at all\n```\n")
    view = MarkdownView(fs, str(tmp_path / "t.md"))
    view._flow = None
    view._reflow(12)
    assert any(len(line) <= 12 for line in view.lines)
    assert any(len(line) > 12 for line in view.lines)
