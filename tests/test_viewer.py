# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The read-only file viewer: loading, search, scrolling and drawing."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import dialogs, highlight, theme, viewer as viewer_mod
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.viewer import Viewer

from support import (
    _FakeRemoteBackend, _KeyScript, _NoLocalPath, script_newwin,
    with_curses_screen, write,
)


@pytest.fixture
def sample(fs, tmp_path):
    write(str(tmp_path / "poem.txt"),
          "Alpha line\nbeta line\nGamma line\nbeta again\ndelta\n")
    return Viewer(fs, str(tmp_path / "poem.txt"))


# -- loading -------------------------------------------------------------------

def test_a_file_is_split_into_lines(sample):
    assert sample.lines[:3] == ["Alpha line", "beta line", "Gamma line"]
    assert sample.name == "poem.txt"
    assert sample.error is None
    assert sample.truncated is False


def test_newlines_are_normalised_and_tabs_expanded(fs, tmp_path):
    path = tmp_path / "mixed.txt"
    path.write_bytes(b"a\r\nb\rc\n\tindented\n")
    view = Viewer(fs, str(path))
    assert view.lines[:3] == ["a", "b", "c"]
    assert view.lines[3] == "    indented"


def test_undecodable_bytes_are_replaced_not_fatal(fs, tmp_path):
    path = tmp_path / "binary.dat"
    path.write_bytes(b"caf\xe9\n")
    view = Viewer(fs, str(path))
    assert view.error is None
    assert "�" in view.lines[0]


def test_a_file_that_will_not_open_is_reported(fs, tmp_path):
    view = Viewer(fs, str(tmp_path / "no-such-file.txt"))
    assert view.error is not None
    assert view.lines == []


def test_a_huge_file_is_capped_and_flagged(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_mod, "MAX_VIEW_BYTES", 20)
    path = tmp_path / "big.txt"
    path.write_text("x" * 100)
    view = Viewer(fs, str(path))
    assert view.truncated is True
    assert len("".join(view.lines)) == 20


def test_a_file_exactly_at_the_cap_is_not_flagged(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_mod, "MAX_VIEW_BYTES", 20)
    path = tmp_path / "exact.txt"
    path.write_text("y" * 20)
    assert Viewer(fs, str(path)).truncated is False


def test_a_reader_that_fails_to_close_still_reports_the_error(fs, tmp_path):
    class _BadClose(LocalFileSystem):
        def open_read(self, path):
            handle = super().open_read(path)

            class _Wrapper:
                def read(self, n):
                    return handle.read(n)

                def close(self):
                    raise OSError("close failed")

            return _Wrapper()

    path = tmp_path / "a.txt"
    path.write_text("content")
    view = Viewer(_BadClose(), str(path))
    # The close error surfaces rather than being mistaken for content.
    assert view.error == "close failed"


# -- search --------------------------------------------------------------------

def test_a_lowercase_pattern_searches_case_insensitively(sample):
    sample.set_search("alpha")
    assert sample.find_next(1) == 0
    assert "match on line 1" in sample.notice


def test_an_uppercase_letter_makes_the_search_case_sensitive(sample):
    sample.set_search("Beta")
    assert sample.find_next(1) is None
    assert "not found" in sample.notice
    sample.set_search("beta")
    assert sample.find_next(1) == 1


def test_search_moves_forwards_and_wraps(sample):
    sample.set_search("beta")
    assert sample.find_next(1) == 1
    assert sample.find_next(1) == 3
    assert sample.find_next(1) == 1
    assert "(wrapped)" in sample.notice


def test_search_moves_backwards_and_wraps(sample):
    sample.set_search("beta")
    assert sample.find_next(-1) == 3
    assert "(wrapped)" in sample.notice
    assert sample.find_next(-1) == 1


def test_searching_with_no_pattern_asks_for_one(sample):
    assert sample.find_next(1) is None
    assert "no search pattern" in sample.notice


def test_searching_an_empty_file_finds_nothing(fs, tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("")
    view = Viewer(fs, str(path))
    # Searching works over the file's own lines, so that is the list to empty.
    view.source = []
    view.set_search("anything")
    assert view.find_next(1) is None


def test_setting_a_search_clears_the_previous_match(sample):
    sample.set_search("beta")
    sample.find_next(1)
    assert sample.cur_match is not None
    sample.set_search("")
    assert sample.cur_match is None
    assert sample.search == ""
    assert sample.notice == ""


def test_match_positions_finds_every_occurrence(sample):
    sample.set_search("a")
    assert sample.match_positions("banana") == [1, 3, 5]
    sample.set_search("")
    assert sample.match_positions("banana") == []


def test_match_positions_respects_case_sensitivity(sample):
    sample.set_search("A")
    assert sample.match_positions("Alpha a A") == [0, 8]
    sample.set_search("a")
    assert sample.match_positions("Alpha a A") == [0, 4, 6, 8]


def test_a_jump_scrolls_the_match_into_view(fs, tmp_path):
    write(str(tmp_path / "long.txt"),
          "".join(f"line {i}\n" for i in range(100)) + "needle\n")
    view = Viewer(fs, str(tmp_path / "long.txt"))
    view.set_search("needle")
    index = view.find_next(1)
    assert view.top == index - 2


# -- the key loop, on a real screen --------------------------------------------

def _run(monkeypatch, view, keys, rows=10, cols=60):
    captured = script_newwin(monkeypatch, keys)
    with_curses_screen(rows, cols, view.run)
    return captured["window"]


@pytest.mark.parametrize("key", [ord("q"), ord("Q"), 27, curses.KEY_F3,
                                 curses.KEY_F10])
def test_the_viewer_closes(monkeypatch, sample, key):
    _run(monkeypatch, sample, [key])


def test_scrolling_by_line_and_page(monkeypatch, fs, tmp_path):
    write(str(tmp_path / "long.txt"), "".join(f"line {i}\n" for i in range(100)))
    view = Viewer(fs, str(tmp_path / "long.txt"))
    _run(monkeypatch, view, [curses.KEY_DOWN, ord("j"), ord("q")])
    assert view.top == 2
    _run(monkeypatch, view, [curses.KEY_UP, ord("k"), ord("q")])
    assert view.top == 0
    _run(monkeypatch, view, [curses.KEY_NPAGE, ord("q")])
    assert view.top == 8
    _run(monkeypatch, view, [curses.KEY_PPAGE, ord("q")])
    assert view.top == 0
    _run(monkeypatch, view, [ord(" "), ord("q")])
    assert view.top == 8


def test_home_and_end(monkeypatch, fs, tmp_path):
    write(str(tmp_path / "long.txt"), "".join(f"line {i}\n" for i in range(100)))
    view = Viewer(fs, str(tmp_path / "long.txt"))
    _run(monkeypatch, view, [curses.KEY_END, ord("q")])
    assert view.top == len(view.lines) - 8
    _run(monkeypatch, view, [curses.KEY_HOME, ord("q")])
    assert view.top == 0


def test_scrolling_never_leaves_the_file(monkeypatch, sample):
    _run(monkeypatch, sample, [curses.KEY_PPAGE, curses.KEY_UP, ord("q")])
    assert sample.top == 0
    for _ in range(5):
        _run(monkeypatch, sample, [curses.KEY_NPAGE, ord("q")])
    assert sample.top == len(sample.lines) - 1


def test_scrolling_sideways(monkeypatch, sample):
    _run(monkeypatch, sample, [curses.KEY_RIGHT, curses.KEY_RIGHT, ord("q")])
    assert sample.left == 16
    _run(monkeypatch, sample, [curses.KEY_LEFT, ord("q")])
    assert sample.left == 8
    _run(monkeypatch, sample, [curses.KEY_LEFT, curses.KEY_LEFT, ord("q")])
    assert sample.left == 0


def test_toggling_line_numbers_and_wrap(monkeypatch, sample):
    _run(monkeypatch, sample, [ord("l"), ord("w"), ord("q")])
    assert sample.show_line_numbers is False
    assert sample.wrap is True
    _run(monkeypatch, sample, [ord("L"), ord("W"), ord("q")])
    assert sample.show_line_numbers is True
    assert sample.wrap is False


def test_searching_from_the_key_loop(monkeypatch, sample):
    monkeypatch.setattr(dialogs, "prompt",
                        lambda *a, **k: "gamma")
    _run(monkeypatch, sample, [ord("/"), ord("q")])
    assert sample.search == "gamma"
    assert sample.cur_match == 2


def test_cancelling_the_search_prompt_keeps_the_old_pattern(monkeypatch, sample):
    sample.set_search("beta")
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: None)
    _run(monkeypatch, sample, [ord("/"), ord("q")])
    assert sample.search == "beta"


def test_an_empty_search_pattern_clears_the_search(monkeypatch, sample):
    sample.set_search("beta")
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "")
    _run(monkeypatch, sample, [ord("/"), ord("q")])
    assert sample.search == ""


def test_n_and_shift_n_step_through_matches(monkeypatch, sample):
    sample.set_search("beta")
    _run(monkeypatch, sample, [ord("n"), ord("q")])
    assert sample.cur_match == 1
    _run(monkeypatch, sample, [ord("n"), ord("q")])
    assert sample.cur_match == 3
    _run(monkeypatch, sample, [ord("N"), ord("q")])
    assert sample.cur_match == 1
    _run(monkeypatch, sample, [ord("p"), ord("q")])
    assert sample.cur_match == 3


def test_an_unhandled_key_is_ignored(monkeypatch, sample):
    _run(monkeypatch, sample, [ord("z"), ord("q")])
    assert sample.top == 0


# -- drawing -------------------------------------------------------------------

def _render(view, rows=10, cols=40):
    def draw(stdscr):
        win = curses.newwin(rows, cols, 0, 0)
        view.draw(win)
        return "\n".join(win.instr(row, 0, cols).decode() for row in range(rows))

    return with_curses_screen(rows + 2, cols + 2, draw)


def test_the_header_names_the_file(sample):
    assert "View: poem.txt" in _render(sample)


def test_the_header_flags_a_truncated_file(sample):
    sample.truncated = True
    assert "[truncated]" in _render(sample)


def test_line_numbers_can_be_turned_off(sample):
    with_numbers = _render(sample)
    assert "1 Alpha line" in with_numbers
    sample.show_line_numbers = False
    assert "1 Alpha line" not in _render(sample)
    assert "Alpha line" in _render(sample)


def test_a_file_that_would_not_open_shows_the_reason(fs, tmp_path):
    view = Viewer(fs, str(tmp_path / "missing.txt"))
    rendered = _render(view)
    assert "Cannot open file" in rendered
    assert "line 1/0" in rendered


def test_matches_are_highlighted_on_screen(sample):
    sample.set_search("beta")
    rendered = _render(sample)
    # The pattern is still drawn (in reverse video, which instr does not show).
    assert "beta line" in rendered


def test_the_footer_shows_the_pattern_and_notice(sample):
    sample.set_search("beta")
    sample.find_next(1)
    rendered = _render(sample)
    assert "/beta" in rendered
    assert "match on line 2" in rendered


def test_a_match_scrolled_off_the_left_edge_is_clipped(sample):
    sample.set_search("line")
    sample.left = 8
    # The match starts before the left edge; the visible part is still marked.
    assert "View: poem.txt" in _render(sample)


def test_a_match_beyond_the_right_edge_is_skipped(fs, tmp_path):
    write(str(tmp_path / "wide.txt"), "x" * 200 + "needle\n")
    view = Viewer(fs, str(tmp_path / "wide.txt"))
    view.set_search("needle")
    # Off the right of a narrow window: drawn without complaint.
    assert "View: wide.txt" in _render(view, cols=30)


def test_drawing_a_line_wider_than_the_window(fs, tmp_path):
    write(str(tmp_path / "wide.txt"), "y" * 500 + "\n")
    view = Viewer(fs, str(tmp_path / "wide.txt"))
    assert "View: wide.txt" in _render(view, cols=30)


def test_line_matches_needs_a_pattern(sample):
    # find_next guards this, but the predicate is safe to ask on its own.
    assert sample._line_matches(0) is False


class _RefusesIndentedWrites:
    """A window that refuses any write starting past the left margin.

    Enough to exercise the viewer's own guards around the text and highlight
    writes without disturbing the header and gutter, which start at column 0.
    """

    def __init__(self, win):
        self._win = win

    def addstr(self, *args):
        if args[1] > 0:
            raise curses.error("addwstr() returned ERR")
        return self._win.addstr(*args)

    def __getattr__(self, name):
        return getattr(self._win, name)


def test_drawing_survives_a_window_that_refuses_the_text(sample):
    """A refused write must not take the viewer down mid-draw."""
    sample.set_search("beta")

    def draw(stdscr):
        window = _RefusesIndentedWrites(curses.newwin(10, 40, 0, 0))
        sample.draw(window)                  # must not raise
        return True

    assert with_curses_screen(12, 42, draw) is True


# -- wrapping ------------------------------------------------------------------

@pytest.mark.parametrize("text, width, pieces", [
    ("short", 10, ["short"]),
    ("", 10, [""]),
    ("exactly-10", 10, ["exactly-10"]),
    ("one two three four", 8, ["one two", "three", "four"]),
    ("supercalifragilistic", 8, ["supercal", "ifragili", "stic"]),
    ("a verylongunbrokenword x", 6, ["a", "verylo", "ngunbr", "okenwo", "rd x"]),
    ("  leading spaces kept", 10, ["  leading", "spaces", "kept"]),
])
def test_wrap_line(text, width, pieces):
    assert viewer_mod.wrap_line(text, width) == pieces


def test_wrapping_never_loses_characters():
    text = "the quick brown fox jumps over the lazy dog"
    for width in range(1, 20):
        assert "".join(viewer_mod.wrap_line(text, width)).replace(" ", "") == \
            text.replace(" ", "")


@pytest.fixture
def prose(fs, tmp_path):
    write(str(tmp_path / "prose.txt"),
          "one two three four five six seven eight\nshort\n")
    return Viewer(fs, str(tmp_path / "prose.txt"))


def test_wrapping_off_leaves_the_lines_alone(prose):
    assert prose.wrap is False
    assert prose.lines is prose.source


def test_wrapping_on_breaks_a_long_line_into_rows(prose):
    prose.wrap = True
    prose._reflow(12)
    assert prose.lines[:4] == ["one two", "three four", "five six", "seven eight"]
    # The file still has three lines however many rows they occupy.
    assert len(prose.source) == 3


def test_a_wrapped_line_keeps_one_number(monkeypatch, prose):
    prose.wrap = True
    prose._reflow(12)
    assert prose.origin[:4] == [0, 0, 0, 0]
    assert prose.first_row == [0, 4, 5]


def test_reflowing_is_skipped_when_nothing_changed(prose):
    prose.wrap = True
    prose._reflow(12)
    before = prose.lines
    prose._reflow(12)
    assert prose.lines is before


def test_a_search_match_split_across_a_wrap_is_still_found(prose):
    # "three four" straddles a row boundary at this width; searching the
    # file's own lines is what makes it findable at all.
    prose.wrap = True
    prose._reflow(12)
    prose.set_search("three four")
    assert prose.find_next(1) == 0


def test_toggling_wrap_keeps_the_reader_on_the_same_line(prose):
    prose.wrap = True
    prose._reflow(12)
    prose.top = 4                      # the second file line, "short"
    prose.toggle_wrap()
    assert prose.wrap is False
    assert prose.origin[prose.top] <= 1
    prose.toggle_wrap()
    assert prose.wrap is True


def test_toggling_wrap_on_a_file_that_would_not_open(fs, tmp_path):
    view = Viewer(fs, str(tmp_path / "missing.txt"))
    view.toggle_wrap()
    assert view.top == 0 and view.lines == []


def test_the_wrap_key_reflows_what_is_drawn(monkeypatch, fs, tmp_path):
    write(str(tmp_path / "long.txt"), "word " * 40 + "\n")
    view = Viewer(fs, str(tmp_path / "long.txt"))
    window = _run(monkeypatch, view, [ord("w"), ord("q")], rows=10, cols=40)
    assert view.wrap is True
    # Every drawn row fits the screen, which is the whole point.
    assert all(len(a[2]) <= 40 for a in window.drawn if len(a) > 2)


# -- syntax colour -------------------------------------------------------------

SCRIPT = "import os\n# note\ndef main():\n    return 'done'\n"


@pytest.fixture
def script(fs, tmp_path):
    write(str(tmp_path / "tool.py"), SCRIPT)
    return Viewer(fs, str(tmp_path / "tool.py"))


@pytest.fixture
def restore_theme():
    """Put the colours back the way the rest of the suite expects them."""
    scheme = theme.current
    yield
    theme.init(scheme)


def _writes(view, rows=10, cols=60):
    """Every write the viewer makes to the screen: (y, x, text, attr)."""
    def run(stdscr):
        window = _KeyScript(curses.newwin(rows, cols, 0, 0), [])
        view.draw(window)
        return window.drawn
    return with_curses_screen(rows + 2, cols + 2, run)


def test_a_code_file_is_coloured(script):
    assert script.language == highlight.PYTHON
    assert script.styles == ["KKKKKK   ", "MMMMMM", "KKK FFFF   ",
                             "    KKKKKK SSSSSS", ""]
    assert script.row_styles == script.styles


def test_a_plain_file_has_no_styles_at_all(sample):
    assert sample.language is None and sample.plain_reason == ""
    assert sample.styles == [] and sample.row_styles == []


def test_a_script_with_no_suffix_is_known_by_its_first_line(fs, tmp_path):
    write(str(tmp_path / "deploy"), "#!/bin/sh\necho hi\n")
    view = Viewer(fs, str(tmp_path / "deploy"))
    assert view.language == highlight.SHELL
    assert view.styles[1] == "KKKK   "


def test_a_code_file_that_will_not_open_still_says_why(fs, tmp_path):
    view = Viewer(fs, str(tmp_path / "missing.py"))
    assert view.error is not None
    assert view.language is None and view.styles == []
    assert "Cannot open file" in _render(view)


def test_a_code_file_on_a_remote_pane_is_coloured_the_same(tmp_path):
    write(str(tmp_path / "far.py"), SCRIPT)
    for backend in (_FakeRemoteBackend(), _NoLocalPath()):
        view = Viewer(backend, str(tmp_path / "far.py"))
        assert view.language == highlight.PYTHON
        assert view.styles[2] == "KKK FFFF   "


def test_a_binary_file_with_a_code_name_shows_every_character(fs, tmp_path):
    path = tmp_path / "blob.py"
    path.write_bytes(bytes(range(256)) * 64 + b'"""\xff\xfe never closed')
    view = Viewer(fs, str(path))
    assert view.error is None and view.language == highlight.PYTHON
    assert [len(s) for s in view.styles] == [len(line) for line in view.source]


def test_a_code_file_cut_at_the_cap_is_still_coloured(fs, tmp_path,
                                                      monkeypatch):
    monkeypatch.setattr(viewer_mod, "MAX_VIEW_BYTES", 30)
    write(str(tmp_path / "cut.py"), 'x = """a string the cap cuts in two"""\n')
    view = Viewer(fs, str(tmp_path / "cut.py"))
    assert view.truncated is True
    assert view.styles == ["    " + "S" * 26]


def test_wrapping_cuts_the_colour_with_the_text(fs, tmp_path):
    line = "x = 'a long string that has to wrap' + 12345  # and a comment"
    write(str(tmp_path / "long.py"), line + "\n")
    view = Viewer(fs, str(tmp_path / "long.py"))
    style = view.styles[0]
    view.toggle_wrap()
    view._flow = None
    view._reflow(16)
    spans = viewer_mod.wrap_spans(line, 16)
    assert len(spans) > 2
    assert view.lines[:len(spans)] == [line[a:b] for a, b in spans]
    assert view.row_styles[:len(spans)] == [style[a:b] for a, b in spans]


def test_c_turns_the_colour_off_and_back_on(monkeypatch, script):
    coloured = list(script.styles)
    _run(monkeypatch, script, [ord("c"), ord("q")])
    assert script.colour is False
    assert script.styles == [] and script.row_styles == []
    _run(monkeypatch, script, [ord("C"), ord("q")])
    assert script.colour is True
    assert script.styles == coloured and script.row_styles == coloured


def test_turning_the_colour_off_keeps_the_place_and_the_wrap(script):
    script.wrap = True
    script._flow = None
    script._reflow(6)
    rows, script.top = list(script.lines), 3
    script.toggle_colour()
    assert script.lines == rows and script.top == 3 and script.wrap
    assert script.row_styles == []
    script.toggle_colour()
    assert len(script.row_styles) == len(rows)


def test_c_on_a_plain_file_is_left_for_a_viewer_that_wants_it(monkeypatch,
                                                             sample):
    seen = []
    monkeypatch.setattr(sample, "_other_key", seen.append)
    _run(monkeypatch, sample, [ord("c"), ord("q")])
    assert seen == [ord("c")]
    assert sample.colour is True and sample.styles == []


def test_the_footer_offers_the_colour_key_only_where_there_is_colour(script,
                                                                    sample):
    assert "[c]olour" in _render(script, cols=78)
    assert "[c]olour" not in _render(sample, cols=78)


def test_a_file_over_the_colour_limit_opens_plain_and_says_why(monkeypatch,
                                                              fs, tmp_path):
    monkeypatch.setattr(highlight, "MAX_COLOUR_BYTES", 1024 * 1024)
    lexed = []
    monkeypatch.setattr(highlight, "highlight",
                        lambda *args: lexed.append(args) or [])
    write(str(tmp_path / "big.py"), "x = 1\n" * 200_000)       # 1.1 MiB
    view = Viewer(fs, str(tmp_path / "big.py"))
    assert lexed == []                      # not even started
    assert view.language == highlight.PYTHON and view.styles == []
    assert view.plain_reason == "plain: over 1 MiB"
    assert "View: big.py [plain: over 1 MiB]" in _render(view, cols=60)
    assert "[c]olour" not in view._hints()
    view.toggle_colour()
    assert view.notice == "plain: over 1 MiB" and view.styles == []


def test_a_file_right_at_the_colour_limit_is_coloured(monkeypatch, fs,
                                                      tmp_path):
    monkeypatch.setattr(highlight, "MAX_COLOUR_BYTES", 6 * 1000)
    write(str(tmp_path / "edge.py"), "x = 1\n" * 1000)
    view = Viewer(fs, str(tmp_path / "edge.py"))
    assert view.plain_reason == "" and view.styles[0] == "    N"


def test_a_lexer_that_trips_leaves_the_file_plain(monkeypatch, fs, tmp_path):
    def trip(language, lines):
        raise ValueError("a lexer bug")

    monkeypatch.setattr(highlight, "highlight", trip)
    write(str(tmp_path / "tool.py"), SCRIPT)
    view = Viewer(fs, str(tmp_path / "tool.py"))
    assert view.error is None and view.source[0] == "import os"
    assert view.styles == [] and view.plain_reason == "plain: could not colour"


def test_each_run_is_drawn_in_its_own_colour(script):
    drawn = {w[2]: w[3] for w in _writes(script) if len(w) > 3}
    assert drawn["import"] == theme.attr("synkeyword")
    assert drawn["# note"] == theme.attr("syncomment")
    assert drawn["main"] == theme.attr("synname")
    assert drawn["'done'"] == theme.attr("synstring")
    assert drawn[" os"] == theme.attr("edit")


def test_search_matches_are_drawn_over_the_colour(script):
    script.set_search("return")
    writes = [w for w in _writes(script) if len(w) > 3 and w[2] == "return"]
    assert writes[0][3] == theme.attr("synkeyword")
    assert writes[-1][3] == theme.attr("editmatch")
    assert writes[-1][:2] == writes[0][:2]   # the same cells, overdrawn


def test_scrolling_sideways_keeps_each_colour_on_its_characters(script):
    script.left = 4
    script.show_line_numbers = False
    drawn = [w for w in _writes(script) if len(w) > 3]
    assert (4, 0, "return", theme.attr("synkeyword")) in drawn
    assert (3, 0, " main():", theme.attr("edit")) not in drawn
    assert (3, 0, "main", theme.attr("synname")) in drawn


@pytest.mark.parametrize("scheme", ["turbo", "midnight", "mono"])
def test_a_coloured_file_is_drawn_in_every_scheme(script, restore_theme,
                                                  scheme):
    roles = ("edit", "synkeyword", "syncomment", "synname", "synstring")

    def run(stdscr):
        theme.init(scheme)
        window = _KeyScript(curses.newwin(10, 60, 0, 0), [])
        script.draw(window)
        return window.drawn, {role: theme.attr(role) for role in roles}

    drawn, attrs = with_curses_screen(12, 62, run)
    by_text = {w[2]: w[3] for w in drawn if len(w) > 3}
    assert by_text["def"] == attrs["synkeyword"]
    assert by_text["# note"] == attrs["syncomment"]
    if scheme == "mono":
        assert attrs["synkeyword"] == curses.A_BOLD
        assert attrs["syncomment"] == curses.A_DIM
        assert attrs["synstring"] == attrs["edit"] == curses.A_NORMAL
    else:
        assert len(set(attrs.values())) == len(roles)


def test_a_terminal_out_of_colour_pairs_still_draws_the_code(monkeypatch,
                                                             script,
                                                             restore_theme):
    def run(stdscr):
        monkeypatch.setattr(curses, "COLOR_PAIRS", 8)
        theme.init("turbo")
        window = _KeyScript(curses.newwin(10, 60, 0, 0), [])
        script.draw(window)
        return window.drawn, theme.attr("synkeyword")

    drawn, keyword = with_curses_screen(12, 62, run)
    assert keyword == curses.A_BOLD          # its monochrome fallback
    assert ("def" in [w[2] for w in drawn if len(w) > 2])
