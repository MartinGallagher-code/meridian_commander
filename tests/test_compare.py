"""Two files side by side: the alignment, the drawing and the key loop."""

from __future__ import annotations

import curses

import pytest

from meridian_commander.compare import (
    Comparison,
    align,
    hunk_starts,
    load_lines,
)
from meridian_commander.filesystems import LocalFileSystem

from support import (
    _ScriptedDialogs,
    script_newwin,
    with_curses_screen,
    write,
)


@pytest.fixture(autouse=True)
def _quiet_screen(monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)


def _pair(tmp_path, left: str, right: str) -> Comparison:
    write(str(tmp_path / "a.txt"), left)
    write(str(tmp_path / "b.txt"), right)
    fs = LocalFileSystem()
    return Comparison(fs, str(tmp_path / "a.txt"), fs, str(tmp_path / "b.txt"))


# -- reading -------------------------------------------------------------------

def test_a_trailing_newline_does_not_add_an_empty_line(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "one\ntwo\n")
    lines, truncated = load_lines(fs, str(tmp_path / "a.txt"))
    assert lines == ["one", "two"]
    assert truncated is False


def test_a_file_over_the_cap_is_cut_and_says_so(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "x" * 100)
    lines, truncated = load_lines(fs, str(tmp_path / "a.txt"), cap=10)
    assert lines == ["x" * 10]
    assert truncated is True


# -- alignment -----------------------------------------------------------------

def test_identical_files_align_row_for_row():
    rows = align(["a", "b"], ["a", "b"])
    assert [(r.left, r.right, r.kind) for r in rows] == [
        (0, 0, "same"), (1, 1, "same")]


def test_a_rewritten_line_pairs_with_the_one_it_replaced():
    rows = align(["a", "b"], ["a", "B"])
    assert rows[1].kind == "change"
    assert (rows[1].left, rows[1].right) == (1, 1)


def test_an_extra_line_on_one_side_faces_a_gap():
    rows = align(["a"], ["a", "b"])
    assert (rows[1].left, rows[1].right, rows[1].kind) == (None, 1, "add")
    rows = align(["a", "b"], ["a"])
    assert (rows[1].left, rows[1].right, rows[1].kind) == (1, None, "del")


def test_a_replaced_block_of_unequal_length_hangs_the_surplus_below():
    rows = align(["a", "b"], ["A", "B", "C"])
    kinds = [r.kind for r in rows]
    # Two rewritten lines face each other; the third has nothing opposite it.
    assert kinds == ["change", "change", "add"]
    assert rows[2].left is None


def test_every_line_of_both_files_appears_exactly_once():
    left = ["one", "two", "three", "four"]
    right = ["one", "2", "three", "four", "five"]
    rows = align(left, right)
    assert [r.left for r in rows if r.left is not None] == list(range(4))
    assert [r.right for r in rows if r.right is not None] == list(range(5))


def test_hunks_are_blocks_not_rows():
    rows = align(["a", "b", "c", "d"], ["a", "B", "C", "d"])
    # Two rewritten lines in a row are one difference to jump to.
    assert hunk_starts(rows) == [1]


# -- the comparison ------------------------------------------------------------

def test_identical_files_report_no_differences(tmp_path):
    view = _pair(tmp_path, "one\ntwo\n", "one\ntwo\n")
    assert view.identical is True
    assert view.hunks == []


def test_the_rows_carry_the_text_of_each_side(tmp_path):
    view = _pair(tmp_path, "one\ntwo\n", "one\nTWO\n")
    assert view.text_of(view.rows[1], "left") == "two"
    assert view.text_of(view.rows[1], "right") == "TWO"


def test_a_gap_has_no_text_on_that_side(tmp_path):
    view = _pair(tmp_path, "one\n", "one\ntwo\n")
    assert view.text_of(view.rows[1], "left") == ""
    assert view.text_of(view.rows[1], "right") == "two"


def test_an_unreadable_file_becomes_an_error_not_an_exception(tmp_path):
    fs = LocalFileSystem()
    write(str(tmp_path / "a.txt"), "x")
    view = Comparison(fs, str(tmp_path / "a.txt"), fs, str(tmp_path / "gone"))
    assert view.error
    assert view.identical is False


def test_jumping_between_differences_wraps_around(tmp_path):
    body = "".join(f"line {i}\n" for i in range(40))
    other = body.replace("line 5", "LINE 5").replace("line 30", "LINE 30")
    view = _pair(tmp_path, body, other)
    assert len(view.hunks) == 2
    assert view.next_hunk(1) == view.hunks[0]
    assert view.next_hunk(1) == view.hunks[1]
    # Past the last one, round to the first.
    assert view.next_hunk(1) == view.hunks[0]
    assert "wrapped" in view.notice
    assert view.next_hunk(-1) == view.hunks[1]


def test_jumping_when_there_is_nothing_to_jump_to_says_so(tmp_path):
    view = _pair(tmp_path, "same\n", "same\n")
    assert view.next_hunk(1) is None
    assert view.notice == "no differences"


# -- drawing -------------------------------------------------------------------

def _render(view, rows=10, cols=60):
    def draw(stdscr):
        win = curses.newwin(rows, cols, 0, 0)
        view.draw(win)
        return "\n".join(win.instr(row, 0, cols).decode() for row in range(rows))

    return with_curses_screen(rows + 2, cols + 2, draw)


def test_both_names_are_in_the_header(tmp_path):
    view = _pair(tmp_path, "one\n", "one\n")
    header = _render(view).splitlines()[0]
    assert "a.txt" in header and "b.txt" in header


def test_both_sides_are_drawn_on_the_same_row(tmp_path):
    view = _pair(tmp_path, "hello\n", "HELLO\n")
    row = _render(view).splitlines()[1]
    assert "hello" in row and "HELLO" in row


def test_a_changed_row_is_marked(tmp_path):
    view = _pair(tmp_path, "one\ntwo\n", "one\nTWO\n")
    assert "!TWO" in _render(view)


def test_an_added_line_is_marked_and_faces_a_gap(tmp_path):
    view = _pair(tmp_path, "one\n", "one\ntwo\n")
    screen = _render(view)
    assert "+two" in screen
    # The left half of that row holds no text of its own.
    row = screen.splitlines()[2]
    assert row[:28].strip() == ""


def test_the_footer_counts_the_differences(tmp_path):
    view = _pair(tmp_path, "one\ntwo\n", "one\nTWO\n")
    assert "1 difference(s)" in _render(view)


def test_the_footer_says_when_the_files_match(tmp_path):
    view = _pair(tmp_path, "one\n", "one\n")
    assert "identical" in _render(view)


def test_an_error_is_shown_instead_of_the_body(tmp_path):
    fs = LocalFileSystem()
    write(str(tmp_path / "a.txt"), "x")
    view = Comparison(fs, str(tmp_path / "a.txt"), fs, str(tmp_path / "gone"))
    assert "Cannot compare" in _render(view)


def test_line_numbers_can_be_turned_off(tmp_path):
    view = _pair(tmp_path, "one\n", "one\n")
    assert "1  one" in _render(view)
    view.show_line_numbers = False
    assert "1  one" not in _render(view)


def test_a_truncated_side_is_flagged_in_the_header(tmp_path):
    view = _pair(tmp_path, "one\n", "one\n")
    view.truncated = True
    assert "[truncated]" in _render(view).splitlines()[0]


def test_a_narrow_screen_still_draws(tmp_path):
    view = _pair(tmp_path, "a long line of text\n", "another long line\n")
    _render(view, rows=6, cols=12)          # must not raise


def test_a_screen_too_narrow_for_two_columns_draws_nothing_in_them(tmp_path):
    """One column wide leaves no room for either side; it must not raise."""
    view = _pair(tmp_path, "left\n", "right\n")
    _render(view, rows=6, cols=1)


# -- the key loop --------------------------------------------------------------

def _run(monkeypatch, view, keys, rows=10, cols=60):
    captured = script_newwin(monkeypatch, keys)
    with_curses_screen(rows, cols, view.run)
    return captured["window"]


@pytest.mark.parametrize("key", [ord("q"), ord("Q"), 27, curses.KEY_F3,
                                 curses.KEY_F10])
def test_the_comparison_closes(monkeypatch, tmp_path, key):
    _run(monkeypatch, _pair(tmp_path, "a\n", "b\n"), [key])


def test_one_scroll_moves_both_sides(monkeypatch, tmp_path):
    body = "".join(f"line {i}\n" for i in range(50))
    view = _pair(tmp_path, body, body.replace("line 9", "LINE 9"))
    _run(monkeypatch, view, [curses.KEY_DOWN, ord("j"), ord("q")])
    # One position for the pair: there is nothing to fall out of step.
    assert view.top == 2
    _run(monkeypatch, view, [curses.KEY_UP, ord("k"), ord("q")])
    assert view.top == 0
    _run(monkeypatch, view, [curses.KEY_DOWN, curses.KEY_DOWN, ord("q")])
    assert view.top == 2
    _run(monkeypatch, view, [curses.KEY_NPAGE, ord("q")])
    assert view.top == 10
    _run(monkeypatch, view, [curses.KEY_PPAGE, ord("q")])
    assert view.top == 2
    _run(monkeypatch, view, [curses.KEY_HOME, ord("q")])
    assert view.top == 0
    _run(monkeypatch, view, [curses.KEY_END, ord("q")])
    assert view.top == len(view.rows) - 8


def test_the_arrows_scroll_both_sides_sideways(monkeypatch, tmp_path):
    view = _pair(tmp_path, "a" * 200 + "\n", "b" * 200 + "\n")
    _run(monkeypatch, view, [curses.KEY_RIGHT, curses.KEY_RIGHT, ord("q")])
    assert view.col == 16
    _run(monkeypatch, view, [curses.KEY_LEFT, ord("q")])
    assert view.col == 8


def test_n_and_N_walk_the_differences(monkeypatch, tmp_path):
    body = "".join(f"line {i}\n" for i in range(40))
    view = _pair(tmp_path, body, body.replace("line 5", "LINE 5")
                 .replace("line 30", "LINE 30"))
    _run(monkeypatch, view, [ord("n"), ord("q")])
    assert view.top == view.hunks[0]
    _run(monkeypatch, view, [ord("n"), ord("q")])
    assert view.top == view.hunks[1]
    _run(monkeypatch, view, [ord("N"), ord("q")])
    assert view.top == view.hunks[0]


def test_the_number_gutter_toggles(monkeypatch, tmp_path):
    view = _pair(tmp_path, "a\n", "b\n")
    _run(monkeypatch, view, [ord("l"), ord("q")])
    assert view.show_line_numbers is False


def test_an_unhandled_key_is_ignored(monkeypatch, tmp_path):
    view = _pair(tmp_path, "a\n", "b\n")
    _run(monkeypatch, view, [ord("z"), ord("q")])
    assert view.top == 0


# -- from the application ------------------------------------------------------

def _point(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def _opened(monkeypatch):
    """Capture the Comparison the app builds instead of running it."""
    from meridian_commander import app as app_mod

    seen: list = []

    class _Fake:
        def __init__(self, *args):
            seen.append(args)

        def run(self, stdscr):
            pass

    monkeypatch.setattr(app_mod, "Comparison", _Fake)
    return seen


def test_compare_takes_one_file_from_each_pane(app, tmp_path, monkeypatch):
    _point(app.left, "file.txt")
    _point(app.right, "file.txt")
    seen = _opened(monkeypatch)
    app._compare()
    assert seen[0][1] == str(tmp_path / "left" / "file.txt")
    assert seen[0][3] == str(tmp_path / "right" / "file.txt")


def test_compare_uses_the_tagged_file_over_the_cursor(app, tmp_path,
                                                      monkeypatch):
    write(str(tmp_path / "left" / "other.txt"), "x")
    app.left.refresh()
    app.left.selected = {"other.txt"}
    _point(app.right, "file.txt")
    seen = _opened(monkeypatch)
    app._compare()
    assert seen[0][1] == str(tmp_path / "left" / "other.txt")


def test_compare_keeps_the_left_pane_on_the_left(app, tmp_path, monkeypatch):
    app.active = app.right                 # the active pane must not decide
    _point(app.left, "file.txt")
    _point(app.right, "file.txt")
    seen = _opened(monkeypatch)
    app._compare()
    assert seen[0][1] == str(tmp_path / "left" / "file.txt")


def test_compare_refuses_a_directory(app, tmp_path, monkeypatch):
    seen = _opened(monkeypatch)
    _point(app.left, "sub")
    app._compare()
    assert not seen
    assert "left pane" in app.message and "directory" in app.message


def test_compare_refuses_an_ambiguous_pane(app, tmp_path, monkeypatch):
    write(str(tmp_path / "right" / "other.txt"), "x")
    app.right.refresh()
    app.right.selected = {"file.txt", "other.txt"}
    _point(app.left, "file.txt")
    seen = _opened(monkeypatch)
    app._compare()
    assert not seen
    assert "right pane" in app.message and "tag one" in app.message


def test_compare_refuses_a_pane_with_nothing_to_offer(app, tmp_path,
                                                      monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    app.right.chdir(str(empty))            # only ".." to point at
    _point(app.left, "file.txt")
    seen = _opened(monkeypatch)
    app._compare()
    assert not seen
    assert "right pane -- nothing is selected" in app.message


def test_a_failing_comparison_is_reported(app, monkeypatch):
    _point(app.left, "file.txt")
    _point(app.right, "file.txt")
    from meridian_commander import app as app_mod

    def explode(*args):
        raise OSError("no")

    monkeypatch.setattr(app_mod, "Comparison", explode)
    scripted = _ScriptedDialogs(monkeypatch)
    app._compare()
    assert scripted.messages[-1][0] == "Compare error"


def test_the_menu_and_the_key_both_reach_it(app, monkeypatch):
    _point(app.left, "file.txt")
    _point(app.right, "file.txt")
    seen = _opened(monkeypatch)
    app._dispatch("compare")
    app.handle_key(ord("D"))
    assert len(seen) == 2
