"""Head and tail of a file at once, inside a pane: reading, folding, drawing."""

from __future__ import annotations

import curses

import pytest

from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.peek import PeekPane, fold, read_ends
from meridian_commander.plugin_api import PluginContext

from support import with_curses_screen, write


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
    head, _tail, total = read_ends(fs, path, 5)
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


# -- folding the middle away ---------------------------------------------------

def test_the_middle_is_folded_away_with_a_count():
    rows, omitted = fold(["a", "b"], ["y", "z"], 100)
    assert omitted == 96
    assert rows[0] == "a"
    assert "96 line(s) omitted" in rows[2]
    assert "lines 3-98" in rows[2]
    assert rows[-1] == "z"


def test_a_file_that_fits_is_shown_whole_without_a_rule():
    rows, omitted = fold(["a", "b"], ["a", "b"], 2)
    assert (rows, omitted) == (["a", "b"], 0)


def test_the_ends_meeting_exactly_shows_each_line_once():
    rows, omitted = fold(["a", "b"], ["c", "d"], 4)
    assert (rows, omitted) == (["a", "b", "c", "d"], 0)


def test_overlapping_ends_do_not_repeat_lines():
    rows, omitted = fold(["a", "b", "c"], ["b", "c", "d"], 4)
    assert (rows, omitted) == (["a", "b", "c", "d"], 0)


# -- the pane ------------------------------------------------------------------

@pytest.fixture
def peeking(app, tmp_path):
    """The left pane peeking at whatever the right pane's cursor is on."""
    def make(**files):
        for name, lines in files.items():
            write(str(tmp_path / "right" / name),
                  "".join(f"{name} line {i}\n" for i in range(lines)))
        app.right.refresh()
        ctx = PluginContext(app=app, own_panel=app.left,
                            other_panel=app.right)
        return PeekPane(ctx)

    return make


def _point(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def test_the_pane_reads_the_other_pane_s_file(peeking, app):
    pane = peeking(**{"app.log": 100})
    _point(app.right, "app.log")
    pane.reload()
    assert pane.name_shown == "app.log"
    assert pane.total == 100
    assert pane.rows[0] == "app.log line 0"
    assert pane.rows[-1] == "app.log line 99"


def test_the_pane_follows_the_other_pane_s_cursor(peeking, app):
    pane = peeking(**{"one.log": 30, "two.log": 40})
    _point(app.right, "one.log")
    pane.follow()
    assert pane.name_shown == "one.log"
    _point(app.right, "two.log")
    pane.follow()
    assert pane.name_shown == "two.log"
    assert pane.total == 40


def test_following_the_same_file_does_not_read_it_again(peeking, app,
                                                        monkeypatch):
    pane = peeking(**{"one.log": 30})
    _point(app.right, "one.log")
    pane.follow()
    reloads = []
    monkeypatch.setattr(pane, "reload", lambda: reloads.append(1))
    pane.follow()
    pane.follow()
    assert reloads == []


def test_a_directory_under_the_other_cursor_is_not_a_file_to_peek_at(peeking,
                                                                     app):
    pane = peeking(**{"one.log": 5})
    _point(app.right, "sub")
    pane.reload()
    assert pane.target() is None
    assert pane.rows == []
    assert "no file" in pane.error


def test_a_file_that_cannot_be_read_is_reported_in_the_pane(peeking, app,
                                                            tmp_path):
    pane = peeking(**{"one.log": 5})
    _point(app.right, "one.log")
    (tmp_path / "right" / "one.log").unlink()
    pane.reload()
    assert pane.error
    assert pane.rows == []


def test_more_and_fewer_lines(peeking, app):
    pane = peeking(**{"app.log": 200})
    _point(app.right, "app.log")
    pane.reload()
    assert pane.count == 20
    assert pane.auto is True
    pane.handle_key(ord("+"))
    assert pane.count == 30
    assert pane.auto is False               # pinned: the pane stops resizing
    assert pane.rows[:30] == [f"app.log line {i}" for i in range(30)]
    pane.handle_key(ord("-"))
    assert pane.count == 20


def test_the_count_never_goes_below_one(peeking, app):
    pane = peeking(**{"app.log": 200})
    _point(app.right, "app.log")
    pane.reload()
    for _ in range(3):
        pane.handle_key(ord("-"))
    assert pane.count == 1
    assert pane.rows[0] == "app.log line 0"


def test_scrolling_stays_inside_the_rows(peeking, app):
    pane = peeking(**{"app.log": 500})
    _point(app.right, "app.log")
    pane.reload()
    pane._body = 5
    pane.handle_key(curses.KEY_DOWN)
    assert pane.top == 1
    pane.handle_key(curses.KEY_UP)
    assert pane.top == 0
    pane.handle_key(curses.KEY_UP)          # already at the top
    assert pane.top == 0
    pane.handle_key(curses.KEY_NPAGE)
    assert pane.top == 5
    pane.handle_key(curses.KEY_PPAGE)
    assert pane.top == 0
    pane.handle_key(curses.KEY_END)
    assert pane.top == len(pane.rows) - 5
    pane.handle_key(curses.KEY_HOME)
    assert pane.top == 0


def test_r_reads_the_file_again(peeking, app, tmp_path):
    """A log grows; the pane is asked to look again rather than polling it."""
    pane = peeking(**{"app.log": 10})
    _point(app.right, "app.log")
    pane.reload()
    assert pane.total == 10
    write(str(tmp_path / "right" / "app.log"),
          "".join(f"app.log line {i}\n" for i in range(60)))
    pane.handle_key(ord("r"))
    assert pane.total == 60


@pytest.mark.parametrize("key", [27, ord("q"), ord("h"), curses.KEY_F10])
def test_the_closing_keys_give_the_pane_back(peeking, app, key):
    pane = peeking(**{"app.log": 5})
    assert pane.handle_key(key) is False


def test_a_key_the_pane_does_not_want_goes_back_to_the_app(peeking, app):
    """Tab must still switch panes while this is open."""
    pane = peeking(**{"app.log": 5})
    assert pane.handle_key(9) is None


# -- drawing -------------------------------------------------------------------

def _render(pane, h=12, w=44):
    def draw(stdscr):
        pane.draw(stdscr, 0, 0, h, w)
        return "\n".join(stdscr.instr(row, 0, w).decode() for row in range(h))

    return with_curses_screen(h + 2, w + 2, draw)


def test_the_pane_draws_a_header_both_ends_and_a_footer(peeking, app):
    """A 12-row pane has 10 body rows: four lines each end, and the rule."""
    pane = peeking(**{"app.log": 100})
    _point(app.right, "app.log")
    screen = _render(pane)
    assert "[head+tail] app.log" in screen
    assert "app.log line 0" in screen
    assert "app.log line 99" in screen
    assert "line(s) omitted" in screen
    assert "first/last 4 (fitted) of 100" in screen


def test_the_pane_sizes_itself_so_both_ends_are_on_screen(peeking, app):
    """The reason to put this in a pane is to see both ends at once, so the
    count follows the pane's height until +/- pins it."""
    pane = peeking(**{"app.log": 500})
    _point(app.right, "app.log")
    _render(pane, h=12)
    assert pane.count == 4                   # (10 body rows - the rule) // 2
    assert len(pane.rows) <= 10
    _render(pane, h=24)
    assert pane.count == 10
    assert "app.log line 0" in _render(pane, h=24)
    assert "app.log line 499" in _render(pane, h=24)


def test_a_pinned_count_survives_a_redraw(peeking, app):
    pane = peeking(**{"app.log": 500})
    _point(app.right, "app.log")
    _render(pane, h=12)
    pane.handle_key(ord("+"))
    assert (pane.count, pane.auto) == (14, False)
    _render(pane, h=12)
    assert pane.count == 14                  # the pane no longer resizes it
    assert "(pinned)" in _render(pane, h=12)


def test_a_pane_too_short_for_two_ends_still_shows_one_line_each(peeking, app):
    pane = peeking(**{"app.log": 500})
    _point(app.right, "app.log")
    _render(pane, h=4)                       # two body rows
    assert pane.count == 1


def test_drawing_picks_up_a_moved_cursor(peeking, app):
    pane = peeking(**{"one.log": 30, "two.log": 30})
    _point(app.right, "one.log")
    assert "one.log line 0" in _render(pane)
    _point(app.right, "two.log")
    assert "two.log line 0" in _render(pane)


def test_the_pane_draws_its_error_rather_than_rows(peeking, app):
    pane = peeking(**{"one.log": 5})
    _point(app.right, "sub")
    screen = _render(pane)
    assert "no file" in screen
    assert "nothing selected" in screen


def test_a_pane_with_no_room_for_a_body_draws_nothing(peeking, app):
    pane = peeking(**{"app.log": 5})
    _render(pane, h=2, w=20)                 # must not raise


def test_a_narrow_pane_clips_rather_than_wrapping(peeking, app):
    pane = peeking(**{"app.log": 100})
    _point(app.right, "app.log")
    _render(pane, h=8, w=10)                 # must not raise


# -- from the application ------------------------------------------------------

def test_the_key_opens_the_pane_and_closes_it_again(app, tmp_path):
    write(str(tmp_path / "right" / "app.log"), "one\ntwo\n")
    app.right.refresh()
    _point(app.right, "app.log")
    app.handle_key(ord("h"))
    assert isinstance(app.left.plugin, PeekPane)
    assert app.left.plugin.name_shown == "app.log"
    app.handle_key(ord("h"))                 # the pane's own key closes it
    assert app.left.plugin is None


def test_the_action_toggles_the_pane_too(app, tmp_path):
    write(str(tmp_path / "right" / "app.log"), "one\n")
    app.right.refresh()
    _point(app.right, "app.log")
    app._dispatch("peek")
    assert isinstance(app.left.plugin, PeekPane)
    app._dispatch("peek")
    assert app.left.plugin is None
    assert "closed" in app.message


def test_it_opens_in_the_active_pane_reading_the_other_one(app, tmp_path):
    write(str(tmp_path / "left" / "notes.txt"), "hello\n")
    app.left.refresh()
    _point(app.left, "notes.txt")
    app.active = app.right                   # so the right pane peeks left
    app._peek()
    assert isinstance(app.right.plugin, PeekPane)
    assert app.right.plugin.name_shown == "notes.txt"
    assert app.left.plugin is None
