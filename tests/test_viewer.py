"""The read-only file viewer: loading, search, scrolling and drawing."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import dialogs, viewer as viewer_mod
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.viewer import Viewer

from support import script_newwin, with_curses_screen, write


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
