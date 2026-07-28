"""The built-in text editor: loading, editing, saving and drawing."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import editor as editor_mod
from meridian_commander.editor import Editor
from meridian_commander.filesystems import LocalFileSystem

from support import read, script_newwin, with_curses_screen, write


@pytest.fixture
def sample(fs, tmp_path):
    write(str(tmp_path / "notes.txt"), "first line\nsecond line\nthird\n")
    return Editor(fs, str(tmp_path / "notes.txt"))


# -- loading -------------------------------------------------------------------

def test_an_existing_file_is_loaded(sample):
    assert sample.lines[:3] == ["first line", "second line", "third"]
    assert sample.name == "notes.txt"
    assert sample.readonly is False
    assert sample.dirty is False


def test_a_missing_file_starts_a_new_one(fs, tmp_path):
    editor = Editor(fs, str(tmp_path / "brand-new.txt"))
    assert editor.lines == [""]
    assert editor.message == "New file"
    assert editor.readonly is False


def test_newlines_are_normalised(fs, tmp_path):
    path = tmp_path / "mixed.txt"
    path.write_bytes(b"a\r\nb\rc")
    editor = Editor(fs, str(path))
    assert editor.lines == ["a", "b", "c"]


def test_an_empty_file_still_has_one_line(fs, tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("")
    assert Editor(fs, str(path)).lines == [""]


def test_a_file_that_will_not_open_is_read_only(fs, tmp_path, monkeypatch):
    class _Unreadable(LocalFileSystem):
        def open_read(self, path):
            raise PermissionError("permission denied")

    path = tmp_path / "locked.txt"
    path.write_text("secret")
    editor = Editor(_Unreadable(), str(path))
    assert editor.readonly is True
    assert editor.error == "permission denied"
    assert "Cannot open" in editor.lines[0]


def test_a_file_over_the_cap_is_read_only(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(editor_mod, "MAX_EDIT_BYTES", 20)
    path = tmp_path / "big.txt"
    path.write_text("x" * 100)
    editor = Editor(fs, str(path))
    assert editor.readonly is True
    assert "too large" in editor.message


# -- saving --------------------------------------------------------------------

def test_saving_writes_the_buffer(sample, tmp_path):
    sample.insert_char("!")
    assert sample.dirty is True
    assert sample.save() is True
    assert read(str(tmp_path / "notes.txt")) == "!first line\nsecond line\nthird\n"
    assert sample.dirty is False
    assert "Saved" in sample.message


def test_a_read_only_buffer_refuses_to_save(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(editor_mod, "MAX_EDIT_BYTES", 5)
    path = tmp_path / "big.txt"
    path.write_text("x" * 50)
    editor = Editor(fs, str(path))
    assert editor.save() is False
    assert "Read-only" in editor.message


def test_a_failed_write_is_reported_not_raised(fs, tmp_path):
    class _Unwritable(LocalFileSystem):
        def open_write(self, path):
            raise OSError("read-only file system")

    write(str(tmp_path / "a.txt"), "content")
    editor = Editor(_Unwritable(), str(tmp_path / "a.txt"))
    assert editor.save() is False
    assert "Save failed: read-only file system" in editor.message


# -- editing -------------------------------------------------------------------

def test_inserting_characters(sample):
    sample.cy, sample.cx = 0, 5
    sample.insert_char("X")
    assert sample.lines[0] == "firstX line"
    assert sample.cx == 6


def test_enter_splits_a_line_and_keeps_the_indentation(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "    indented text\n")
    editor = Editor(fs, str(tmp_path / "a.txt"))
    editor.cy, editor.cx = 0, 12
    editor.newline()
    assert editor.lines[:2] == ["    indented", "     text"]
    assert editor.cy == 1
    assert editor.cx == 4               # the cursor lands after the indent


def test_backspace_within_a_line(sample):
    sample.cy, sample.cx = 0, 5
    sample.backspace()
    assert sample.lines[0] == "firs line"
    assert sample.cx == 4


def test_backspace_at_the_start_joins_the_previous_line(sample):
    sample.cy, sample.cx = 1, 0
    sample.backspace()
    assert sample.lines[0] == "first linesecond line"
    assert sample.cy == 0
    assert sample.cx == 10


def test_backspace_at_the_very_start_does_nothing(sample):
    sample.cy, sample.cx = 0, 0
    sample.backspace()
    assert sample.lines[0] == "first line"
    assert sample.dirty is False


def test_delete_within_a_line(sample):
    sample.cy, sample.cx = 0, 0
    sample.delete()
    assert sample.lines[0] == "irst line"


def test_delete_at_the_end_joins_the_next_line(sample):
    sample.cy, sample.cx = 0, len(sample.lines[0])
    sample.delete()
    assert sample.lines[0] == "first linesecond line"
    assert len(sample.lines) == 3


def test_delete_at_the_very_end_does_nothing(sample):
    sample.cy = len(sample.lines) - 1
    sample.cx = len(sample.lines[sample.cy])
    sample.delete()
    assert sample.dirty is False


def test_delete_line_and_the_last_remaining_line(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "one\ntwo\n")
    editor = Editor(fs, str(tmp_path / "a.txt"))
    editor.cy = 0
    editor._delete_line()
    assert editor.lines[0] == "two"
    while len(editor.lines) > 1:
        editor._delete_line()
    editor._delete_line()
    # The buffer keeps one (empty) line rather than becoming empty.
    assert editor.lines == [""]


def test_a_read_only_buffer_refuses_every_edit(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(editor_mod, "MAX_EDIT_BYTES", 5)
    path = tmp_path / "big.txt"
    path.write_text("hello world")
    editor = Editor(fs, str(path))
    before = list(editor.lines)
    editor.insert_char("X")
    editor.newline()
    editor.backspace()
    editor.delete()
    editor._delete_line()
    assert editor.lines == before
    assert editor.dirty is False


# -- cursor movement -----------------------------------------------------------

def test_moving_between_lines_clamps_the_column(sample):
    sample.cy, sample.cx = 1, 11
    sample.move(1, 0)
    # "third" is shorter than "second line", so the column comes back.
    assert (sample.cy, sample.cx) == (2, 5)


def test_moving_left_off_a_line_goes_to_the_end_of_the_previous(sample):
    sample.cy, sample.cx = 1, 0
    sample.move(0, -1)
    assert (sample.cy, sample.cx) == (0, 10)


def test_moving_right_off_a_line_goes_to_the_start_of_the_next(sample):
    sample.cy, sample.cx = 0, 10
    sample.move(0, 1)
    assert (sample.cy, sample.cx) == (1, 0)


def test_moving_left_at_the_very_start_stays_put(sample):
    sample.cy, sample.cx = 0, 0
    sample.move(0, -1)
    assert (sample.cy, sample.cx) == (0, 0)


def test_moving_right_at_the_very_end_stays_put(sample):
    sample.cy = len(sample.lines) - 1
    sample.cx = len(sample.lines[sample.cy])
    sample.move(0, 1)
    assert sample.cx == len(sample.lines[sample.cy])


def test_moving_beyond_the_buffer_is_clamped(sample):
    sample.move(100, 0)
    assert sample.cy == len(sample.lines) - 1
    sample.move(-100, 0)
    assert sample.cy == 0


def test_home_and_end(sample):
    sample.cy, sample.cx = 0, 4
    sample.end()
    assert sample.cx == 10
    sample.home()
    assert sample.cx == 0


def test_cur_line_reads_the_line_under_the_cursor(sample):
    sample.cy = 1
    assert sample._cur_line() == "second line"


# -- key handling --------------------------------------------------------------

@pytest.mark.parametrize("key", [17, curses.KEY_F10])
def test_quit_keys(sample, key):
    assert sample.handle_key(key) == "quit"


@pytest.mark.parametrize("key", [19, 15, curses.KEY_F2])
def test_save_keys(sample, key, tmp_path):
    sample.insert_char("Z")
    assert sample.handle_key(key) is None
    assert read(str(tmp_path / "notes.txt")).startswith("Z")


@pytest.mark.parametrize("key", [11, 25])
def test_delete_line_keys(sample, key):
    sample.handle_key(key)
    assert sample.lines[0] == "second line"


def test_toggling_line_numbers(sample):
    sample.handle_key(12)
    assert sample.show_line_numbers is False
    sample.handle_key(12)
    assert sample.show_line_numbers is True


def test_arrow_and_paging_keys(sample):
    sample.handle_key(curses.KEY_DOWN)
    assert sample.cy == 1
    sample.handle_key(curses.KEY_UP)
    assert sample.cy == 0
    sample.handle_key(curses.KEY_RIGHT)
    assert sample.cx == 1
    sample.handle_key(curses.KEY_LEFT)
    assert sample.cx == 0
    sample.handle_key(curses.KEY_END)
    assert sample.cx == 10
    sample.handle_key(curses.KEY_HOME)
    assert sample.cx == 0
    sample.handle_key(curses.KEY_NPAGE, page=2)
    assert sample.cy == 2
    sample.handle_key(curses.KEY_PPAGE, page=2)
    assert sample.cy == 0


@pytest.mark.parametrize("key", [curses.KEY_BACKSPACE, 127, 8])
def test_backspace_keys(sample, key):
    sample.cx = 1
    sample.handle_key(key)
    assert sample.lines[0] == "irst line"


def test_delete_and_enter_keys(sample):
    sample.handle_key(curses.KEY_DC)
    assert sample.lines[0] == "irst line"
    sample.handle_key(10)
    assert sample.lines[0] == ""


def test_tab_inserts_spaces(sample):
    sample.handle_key(9)
    assert sample.lines[0].startswith("    first")


def test_printable_and_wide_characters(sample):
    sample.handle_key(ord("Z"))
    assert sample.lines[0].startswith("Z")
    sample.handle_key(ord("é"))
    assert sample.lines[0].startswith("Zé")


def test_a_key_outside_the_character_range_is_ignored(sample):
    before = list(sample.lines)
    sample.handle_key(0x110000)
    assert sample.lines == before


def test_an_unrecognised_control_key_is_ignored(sample):
    before = list(sample.lines)
    assert sample.handle_key(1) is None
    assert sample.lines == before


# -- the main loop, on a real screen -------------------------------------------

def _run(monkeypatch, editor, keys, rows=10, cols=50):
    captured = script_newwin(monkeypatch, keys)
    with_curses_screen(rows, cols, editor.run)
    return captured["window"]


def test_the_editor_opens_and_quits(monkeypatch, sample):
    _run(monkeypatch, sample, [curses.KEY_F10])
    assert sample.dirty is False


def test_typing_then_saving_then_quitting(monkeypatch, sample, tmp_path):
    _run(monkeypatch, sample, [ord("h"), ord("i"), 19, 17])
    assert read(str(tmp_path / "notes.txt")).startswith("hifirst line")


def test_quitting_with_unsaved_changes_asks_first(monkeypatch, sample, tmp_path):
    # Type, quit, then decline to discard, then save and quit for real.
    _run(monkeypatch, sample, [ord("!"), 17, ord("n"), 19, 17])
    assert read(str(tmp_path / "notes.txt")).startswith("!first")


def test_discarding_unsaved_changes(monkeypatch, sample, tmp_path):
    _run(monkeypatch, sample, [ord("!"), 17, ord("y")])
    # Nothing was written: the file on disk is untouched.
    assert read(str(tmp_path / "notes.txt")) == "first line\nsecond line\nthird\n"


@pytest.mark.parametrize("answer", [ord("N"), 27])
def test_declining_to_discard_by_other_keys(monkeypatch, sample, answer):
    _run(monkeypatch, sample, [ord("!"), 17, answer, 19, 17])
    assert sample.dirty is False


def test_an_unrecognised_answer_to_the_discard_prompt_is_ignored(monkeypatch,
                                                                 sample):
    # A stray key at the prompt is not taken as either answer.
    _run(monkeypatch, sample, [ord("!"), 17, ord("z"), ord("y")])
    assert sample.dirty is True


# -- drawing -------------------------------------------------------------------

def _render(editor, rows=8, cols=40):
    def draw(stdscr):
        win = curses.newwin(rows, cols, 0, 0)
        editor.draw(win)
        return "\n".join(win.instr(row, 0, cols).decode() for row in range(rows))

    return with_curses_screen(rows + 2, cols + 2, draw)


def test_the_header_names_the_file_and_flags_changes(sample):
    assert "Edit: notes.txt" in _render(sample)
    sample.insert_char("x")
    assert "*" in _render(sample).splitlines()[0]


def test_the_header_flags_a_read_only_buffer(fs, tmp_path, monkeypatch):
    monkeypatch.setattr(editor_mod, "MAX_EDIT_BYTES", 5)
    path = tmp_path / "big.txt"
    path.write_text("hello world")
    assert "[RO]" in _render(Editor(fs, str(path)))


def test_line_numbers_can_be_turned_off(sample):
    assert "1 first line" in _render(sample)
    sample.show_line_numbers = False
    rendered = _render(sample)
    assert "1 first line" not in rendered
    assert "first line" in rendered


def test_the_status_line_shows_the_message_or_the_hint(sample):
    assert "F2/^S save" in _render(sample)
    sample.message = "Saved 30 bytes"
    assert "Saved 30 bytes" in _render(sample)


def test_drawing_scrolls_down_to_follow_the_cursor(fs, tmp_path):
    write(str(tmp_path / "long.txt"), "".join(f"line {i}\n" for i in range(50)))
    editor = Editor(fs, str(tmp_path / "long.txt"))
    editor.cy = 40
    _render(editor)
    assert editor.top == 40 - 6 + 1
    assert "line 40" in _render(editor)


def test_drawing_scrolls_back_up_to_follow_the_cursor(fs, tmp_path):
    write(str(tmp_path / "long.txt"), "".join(f"line {i}\n" for i in range(50)))
    editor = Editor(fs, str(tmp_path / "long.txt"))
    editor.top = 30
    editor.cy = 5
    _render(editor)
    assert editor.top == 5


def test_drawing_scrolls_sideways_to_follow_the_cursor(fs, tmp_path):
    write(str(tmp_path / "wide.txt"), "z" * 200 + "\n")
    editor = Editor(fs, str(tmp_path / "wide.txt"))
    editor.cx = 150
    _render(editor)
    assert editor.left > 0
    editor.cx = 0
    _render(editor)
    assert editor.left == 0


def test_tabs_are_expanded_when_drawn(fs, tmp_path):
    path = tmp_path / "tabs.txt"
    path.write_bytes(b"\tindented\n")
    assert "    indented" in _render(Editor(fs, str(path)))


class _RefusesIndentedWrites:
    """A window that refuses any write starting past the left margin."""

    def __init__(self, win):
        self._win = win

    def addstr(self, *args):
        if args[1] > 0:
            raise curses.error("addwstr() returned ERR")
        return self._win.addstr(*args)

    def __getattr__(self, name):
        return getattr(self._win, name)


def test_drawing_survives_a_window_that_refuses_the_text(sample):
    def draw(stdscr):
        sample.draw(_RefusesIndentedWrites(curses.newwin(8, 40, 0, 0)))
        return True

    assert with_curses_screen(10, 42, draw) is True


def test_the_cursor_is_not_placed_outside_the_window(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "short\n")
    editor = Editor(fs, str(tmp_path / "a.txt"))
    # A cursor row below the status line must not be positioned there.
    editor.top = 0
    editor.cy = 0
    editor.show_line_numbers = False

    def draw(stdscr):
        win = curses.newwin(3, 20, 0, 0)
        editor.draw(win)
        return True

    assert with_curses_screen(6, 22, draw) is True
