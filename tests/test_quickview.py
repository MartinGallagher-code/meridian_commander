# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Ctrl-Q: the other pane shows the file under the cursor (MC-015)."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import quickview
from meridian_commander.app import MENUS, App
from meridian_commander.imageview import ImageView
from meridian_commander.markdown import MarkdownView
from meridian_commander.quickview import QuickViewPane
from meridian_commander.tableview import TableView

from support import (
    _FakeRemoteBackend,
    _StubScreen,
    rgb_png,
    with_curses_screen,
    write,
)


@pytest.fixture
def files(tmp_path):
    left = tmp_path / "left"
    write(str(left / "notes.txt"), "first line\nsecond line\n")
    write(str(left / "readme.md"), "# Title\n\nSome *text*.\n")
    write(str(left / "runs.tsv"), "run\tms\n1\t20\n2\t30\n")
    (left / "pic.png").write_bytes(rgb_png(2, 2, [(255, 0, 0)] * 4))
    write(str(left / "sub" / "inner.txt"), "x")
    (tmp_path / "right").mkdir()
    return tmp_path


@pytest.fixture
def app(files, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))
    return App(_StubScreen(), str(files / "left"), str(files / "right"))


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def _quick(app) -> QuickViewPane:
    plugin = app.right.plugin
    assert isinstance(plugin, QuickViewPane)
    return plugin


# -- on and off ----------------------------------------------------------------

def test_ctrl_q_turns_the_other_pane_into_a_quick_view_and_back(app, files):
    app.handle_key(17)
    _quick(app)
    assert "Quick view" in app.message
    app.handle_key(17)
    assert app.right.plugin is None
    assert app.right.path == str(files / "right")
    assert app.message == "Quick view closed"


def test_a_busy_other_pane_is_not_taken_over(app):
    app.right.plugin = object()
    app.handle_key(17)
    assert app.message == "The other pane is busy -- close it first"


def test_single_mode_shows_both_while_it_is_on(app):
    app.single = True
    assert len(app._panes_on_screen()) == 1
    app._quick_view()
    assert len(app._panes_on_screen()) == 2
    app._quick_view()
    assert len(app._panes_on_screen()) == 1


def test_the_command_menu_offers_it(app):
    command = next(m for m in MENUS if m["name"] == "command")
    assert any(i.get("name") == "quick_view" and i.get("key") == "Ctrl+Q"
               for i in command["items"])
    app._dispatch("quick_view")
    _quick(app)


# -- what it shows ---------------------------------------------------------------

@pytest.mark.parametrize("name, kind", [
    ("notes.txt", quickview._Glance),
    ("readme.md", MarkdownView),
    ("runs.tsv", TableView),
    ("pic.png", ImageView),
])
def test_each_file_in_the_viewer_f3_would_use(app, name, kind):
    _point_at(app.left, name)
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    assert isinstance(pane.view, kind)


def test_an_image_is_drawn_in_half_blocks(app):
    _point_at(app.left, "pic.png")
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    assert pane.view.using_graphics() is False


def test_text_is_read_only_as_far_as_a_pane_needs():
    assert quickview._Glance.LIMIT == quickview.TEXT_LIMIT


def test_moving_the_cursor_follows(app):
    _point_at(app.left, "notes.txt")
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    first = pane.view
    pane.follow()
    assert pane.view is first                  # nothing moved, nothing reread
    _point_at(app.left, "readme.md")
    pane.follow()
    assert isinstance(pane.view, MarkdownView)


def test_a_directory_is_summarised(app):
    _point_at(app.left, "sub")
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    assert pane.view is None
    assert pane.lines[0] == "1 entries: 0 directories, 1 file"


def test_the_way_up_shows_nothing(app):
    app.left.move_to(0)
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    assert pane.lines == ["Nothing under the cursor."]
    pane.load()                                # nothing to load
    assert pane.view is None


def test_a_file_that_will_not_open_says_why(app, monkeypatch):
    def broken(fs, path):
        raise ValueError("damaged")

    monkeypatch.setattr(quickview, "viewer_for", broken)
    _point_at(app.left, "runs.tsv")
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    assert pane.lines == ["Cannot show it: damaged"]


# -- remote and large files ------------------------------------------------------

class _Counted(_FakeRemoteBackend):
    def __init__(self):
        super().__init__()
        self.reads: list[str] = []

    def open_read(self, path):
        self.reads.append(self.basename(path))
        return super().open_read(path)


def test_a_remote_file_waits_for_the_cursor_to_settle(app, files, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(quickview.time, "monotonic", lambda: clock[0])
    remote = _Counted()
    app.left.set_location(remote, str(files / "left"))
    app._quick_view()
    pane = _quick(app)
    for name in ("notes.txt", "readme.md", "runs.tsv"):  # arrowing past
        _point_at(app.left, name)
        pane.follow()
        clock[0] += 0.1
        pane.tick()
    assert remote.reads == []
    assert pane.lines == ["Waiting for the cursor to settle..."]
    clock[0] += quickview.SETTLE_SECONDS
    pane.tick()
    assert remote.reads == ["runs.tsv"]
    assert isinstance(pane.view, TableView)


def test_a_large_file_waits_to_be_asked(app, files, monkeypatch):
    monkeypatch.setattr(quickview, "LOCAL_LIMIT", 10)
    _point_at(app.left, "runs.tsv")
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    assert pane.view is None
    assert pane.lines[0].startswith("Large file:")
    assert pane.handle_key(10) is True         # Enter loads it after all
    assert isinstance(pane.view, TableView)


# -- keys in the quick view --------------------------------------------------------

def test_scrolling_the_view(app):
    write(str(app.left.path + "/long.txt"), "\n".join(str(i) for i in range(50)))
    app.left.refresh()
    _point_at(app.left, "long.txt")
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    for key, top in ((curses.KEY_DOWN, 1), (curses.KEY_NPAGE, 11),
                     (curses.KEY_UP, 10), (curses.KEY_PPAGE, 0),
                     (curses.KEY_END, 49), (curses.KEY_HOME, 0)):
        assert pane.handle_key(key) is True
        assert pane.view.top == top
    assert pane.handle_key(9) is None           # Tab is the app's


def test_keys_a_view_without_lines_leaves_to_the_app(app):
    _point_at(app.left, "pic.png")
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    assert pane.handle_key(curses.KEY_DOWN) is None


def test_enter_opens_the_full_viewer(app, monkeypatch):
    opened = []

    class Full:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            opened.append("ran")

    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    _point_at(app.left, "notes.txt")
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    monkeypatch.setattr(quickview, "viewer_for", Full)
    assert pane.handle_key(10) is True
    assert opened[-1] == "ran" and opened[0].endswith("notes.txt")


def test_enter_on_nothing_and_escape(app):
    app.left.move_to(0)
    app._quick_view()
    pane = _quick(app)
    pane.follow()
    assert pane.handle_key(10) is True          # nothing to load or open
    assert pane.handle_key(27) is False         # closes


def test_tab_moves_into_it_and_ctrl_q_from_there_closes(app):
    app._quick_view()
    app.handle_key(9)
    assert app.active is app.right
    app.handle_key(17)
    assert app.right.plugin is None


# -- drawing ---------------------------------------------------------------------

def _draw(files, prepare, rows=24, cols=100):
    def run(stdscr):
        app = App(stdscr, str(files / "left"), str(files / "right"))
        prepare(app)
        app.draw()
        app.draw()                              # the window is reused
        return "\n".join(stdscr.instr(row, 0).decode(errors="replace")
                         for row in range(rows))

    return with_curses_screen(rows, cols, run)


def test_the_pane_draws_the_file(files, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))

    def prepare(app):
        _point_at(app.left, "notes.txt")
        app._quick_view()

    text = _draw(files, prepare)
    assert "first line" in text and "second line" in text
    # The footer fits a pane of 40 columns: two panes on an 80-column screen.
    assert "Tab: in  Enter: full view  Ctrl-Q: off" in text


def test_the_pane_draws_a_summary(files, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))

    def prepare(app):
        _point_at(app.left, "sub")
        app._quick_view()

    text = _draw(files, prepare)
    assert "Quick view: sub" in text and "1 entries" in text


def test_a_pane_too_small_draws_nothing(app):
    app._quick_view()
    pane = _quick(app)
    pane.draw(None, 0, 0, 2, 40)                # no screen touched
    pane.draw(None, 0, 0, 20, 7)


def test_a_new_size_gets_a_new_window(app):
    made = []

    class Screen:
        def subwin(self, h, w, y, x):
            made.append((h, w, y, x))
            return object()

    app._quick_view()
    pane = _quick(app)
    screen = Screen()
    first = pane._window(screen, 1, 40, 20, 40)
    assert pane._window(screen, 1, 40, 20, 40) is first
    pane._window(screen, 1, 40, 22, 40)
    assert made == [(20, 40, 1, 40), (22, 40, 1, 40)]
