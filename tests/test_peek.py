"""Head and tail of a file at once: the reading, the fold and the key loop."""

from __future__ import annotations

import curses

import pytest

from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.peek import PeekViewer, read_ends

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


def _log(tmp_path, lines: int, name: str = "app.log") -> str:
    path = str(tmp_path / name)
    write(path, "".join(f"line {i}\n" for i in range(lines)))
    return path


# -- reading both ends ---------------------------------------------------------

def test_read_ends_returns_both_ends_and_the_count(fs, tmp_path):
    head, tail, total = read_ends(fs, _log(tmp_path, 100), 3)
    assert head == ["line 0", "line 1", "line 2"]
    assert tail == ["line 97", "line 98", "line 99"]
    assert total == 100


def test_a_short_file_is_its_own_head_and_tail(fs, tmp_path):
    head, tail, total = read_ends(fs, _log(tmp_path, 2), 10)
    assert head == tail == ["line 0", "line 1"]
    assert total == 2


def test_a_last_line_without_a_newline_still_counts(fs, tmp_path):
    path = str(tmp_path / "a.txt")
    write(path, "one\ntwo")
    head, tail, total = read_ends(fs, path, 5)
    assert head == ["one", "two"]
    assert total == 2


def test_carriage_returns_and_tabs_are_normalised(fs, tmp_path):
    path = str(tmp_path / "a.txt")
    write(path, "one\r\n\tindented\n")
    head, _tail, _total = read_ends(fs, path, 5)
    assert head == ["one", "    indented"]


def test_an_empty_file_has_no_lines(fs, tmp_path):
    path = str(tmp_path / "empty.txt")
    write(path, "")
    assert read_ends(fs, path, 5) == ([], [], 0)


def test_a_line_longer_than_the_cap_is_cut_rather_than_buffered(fs, tmp_path,
                                                                monkeypatch):
    from meridian_commander import peek as peek_mod

    monkeypatch.setattr(peek_mod, "MAX_LINE_BYTES", 8)
    path = str(tmp_path / "one-line.txt")
    write(path, "x" * 40)
    head, _tail, total = read_ends(fs, path, 10)
    assert total == 5                      # 40 bytes cut into 8-byte pieces
    assert head[0] == "x" * 8


# -- the fold ------------------------------------------------------------------

def test_the_middle_is_folded_away_with_a_count(fs, tmp_path):
    view = PeekViewer(fs, _log(tmp_path, 100), count=3)
    assert view.omitted == 94
    assert view.source[:3] == ["line 0", "line 1", "line 2"]
    assert "94 line(s) omitted" in view.source[3]
    assert "lines 4-97" in view.source[3]
    assert view.source[-1] == "line 99"


def test_a_file_that_fits_is_shown_whole_without_a_rule(fs, tmp_path):
    view = PeekViewer(fs, _log(tmp_path, 5), count=10)
    assert view.source == [f"line {i}" for i in range(5)]
    assert view.omitted == 0


def test_the_ends_meeting_exactly_shows_each_line_once(fs, tmp_path):
    view = PeekViewer(fs, _log(tmp_path, 10), count=5)
    assert view.source == [f"line {i}" for i in range(10)]
    assert view.omitted == 0


def test_overlapping_ends_do_not_repeat_lines(fs, tmp_path):
    view = PeekViewer(fs, _log(tmp_path, 8), count=5)
    assert view.source == [f"line {i}" for i in range(8)]


def test_the_rule_is_styled_dim_and_the_lines_are_not(fs, tmp_path):
    view = PeekViewer(fs, _log(tmp_path, 100), count=2)
    assert set(view.styles[2]) == {"D"}
    assert view.styles[0] == ""


def test_a_stream_that_will_not_close_is_still_read(tmp_path):
    """A backend whose close() fails must not lose the lines already read."""

    class _Awkward(LocalFileSystem):
        def open_read(self, path):
            stream = super().open_read(path)

            class _Wrapper:
                def read(self, size):
                    return stream.read(size)

                def close(self):
                    stream.close()
                    raise OSError("the connection went away")

            return _Wrapper()

    head, _tail, total = read_ends(_Awkward(), _log(tmp_path, 4), 2)
    assert head == ["line 0", "line 1"]
    assert total == 4


def test_an_unreadable_file_becomes_an_error(fs, tmp_path):
    view = PeekViewer(fs, str(tmp_path / "nowhere.log"))
    assert view.error
    assert view.source == []


# -- the view ------------------------------------------------------------------

def _render(view, rows=12, cols=60):
    def draw(stdscr):
        win = curses.newwin(rows, cols, 0, 0)
        view.draw(win)
        return "\n".join(win.instr(row, 0, cols).decode() for row in range(rows))

    return with_curses_screen(rows + 2, cols + 2, draw)


def test_the_header_names_the_file_and_the_counts(fs, tmp_path):
    header = _render(PeekViewer(fs, _log(tmp_path, 100), count=3)).splitlines()[0]
    assert "Peek: app.log" in header
    assert "first/last 3 of 100 lines" in header


def test_both_ends_are_on_the_one_screen(fs, tmp_path):
    screen = _render(PeekViewer(fs, _log(tmp_path, 100), count=3))
    assert "line 0" in screen and "line 99" in screen
    assert "line 50" not in screen


def test_the_footer_offers_the_size_keys(fs, tmp_path):
    assert "+/- more/less" in _render(PeekViewer(fs, _log(tmp_path, 100)))


def _run(monkeypatch, view, keys, rows=12, cols=60):
    captured = script_newwin(monkeypatch, keys)
    with_curses_screen(rows, cols, view.run)
    return captured["window"]


def test_plus_and_minus_show_more_and_fewer_lines(monkeypatch, fs, tmp_path):
    view = PeekViewer(fs, _log(tmp_path, 200), count=20)
    _run(monkeypatch, view, [ord("+"), ord("q")])
    assert view.count == 30
    assert view.source[:30] == [f"line {i}" for i in range(30)]
    _run(monkeypatch, view, [ord("-"), ord("q")])
    assert view.count == 20


def test_the_count_never_goes_below_one(monkeypatch, fs, tmp_path):
    view = PeekViewer(fs, _log(tmp_path, 200), count=5)
    _run(monkeypatch, view, [ord("-"), ord("-"), ord("q")])
    assert view.count == 1
    assert view.source[0] == "line 0"


def test_scrolling_and_searching_still_work(monkeypatch, fs, tmp_path):
    view = PeekViewer(fs, _log(tmp_path, 200), count=20)
    _run(monkeypatch, view, [curses.KEY_DOWN, ord("q")])
    assert view.top == 1
    view.set_search("line 199")
    assert view.find_next(1) is not None


# -- from the application ------------------------------------------------------

def _opened(monkeypatch):
    """Capture the PeekViewer the app builds instead of running it."""
    from meridian_commander import app as app_mod

    seen: list = []

    class _Fake:
        def __init__(self, *args):
            seen.append(args)

        def run(self, stdscr):
            pass

    monkeypatch.setattr(app_mod, "PeekViewer", _Fake)
    return seen


def _point(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def test_peek_reads_the_file_in_the_other_pane(app, tmp_path, monkeypatch):
    _point(app.right, "file.txt")
    seen = _opened(monkeypatch)
    app._peek()                            # the left pane is active
    assert seen[0][1] == str(tmp_path / "right" / "file.txt")


def test_peek_follows_the_active_pane(app, tmp_path, monkeypatch):
    _point(app.left, "file.txt")
    app.active = app.right
    seen = _opened(monkeypatch)
    app._peek()
    assert seen[0][1] == str(tmp_path / "left" / "file.txt")


def test_peek_uses_the_tagged_file_over_the_cursor(app, tmp_path, monkeypatch):
    write(str(tmp_path / "right" / "other.txt"), "x")
    app.right.refresh()
    app.right.selected = {"other.txt"}
    seen = _opened(monkeypatch)
    app._peek()
    assert seen[0][1] == str(tmp_path / "right" / "other.txt")


def test_peek_refuses_a_directory(app, monkeypatch):
    _point(app.right, "sub")
    seen = _opened(monkeypatch)
    app._peek()
    assert not seen
    assert "other pane" in app.message and "directory" in app.message


def test_a_failing_peek_is_reported(app, monkeypatch):
    from meridian_commander import app as app_mod

    def explode(*args):
        raise OSError("no")

    _point(app.right, "file.txt")
    monkeypatch.setattr(app_mod, "PeekViewer", explode)
    scripted = _ScriptedDialogs(monkeypatch)
    app._peek()
    assert scripted.messages[-1][0] == "Head + tail error"


def test_the_menu_and_the_key_both_reach_it(app, monkeypatch):
    _point(app.right, "file.txt")
    seen = _opened(monkeypatch)
    app._dispatch("peek")
    app.handle_key(ord("h"))
    assert len(seen) == 2
