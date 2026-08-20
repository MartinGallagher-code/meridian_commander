"""The application's drawing: panes, status line, function bar, main loop."""

from __future__ import annotations

import curses
import re

import pytest

from meridian_commander.app import App

from support import _StubScreen, with_curses_screen, write


@pytest.fixture
def panes(tmp_path):
    """Two directories with a mix of entries to render."""
    write(str(tmp_path / "left" / "readme.txt"), "hello", mtime=1000)
    write(str(tmp_path / "left" / "big.bin"), "x" * 5000, mtime=2000)
    (tmp_path / "left" / "subdir").mkdir()
    (tmp_path / "left" / "alias").symlink_to(tmp_path / "left" / "readme.txt")
    (tmp_path / "right").mkdir()
    return tmp_path


def _render(panes, rows=24, cols=80, prepare=None):
    """Draw an App on a real screen and return (text, app).

    Rows are read whole rather than by a byte count: the frames are drawn in
    box-drawing characters, and three bytes per cell means a count in bytes
    stops well short of the right-hand edge.
    """
    holder = {}

    def run(stdscr):
        app = App(stdscr, str(panes / "left"), str(panes / "right"))
        holder["app"] = app
        if prepare is not None:
            prepare(app)
        app.draw()
        return "\n".join(stdscr.instr(row, 0).decode()
                         for row in range(rows))

    return with_curses_screen(rows, cols, run), holder["app"]


def test_both_panes_are_drawn_in_framed_windows(panes):
    text, _ = _render(panes)
    assert "readme.txt" in text
    assert "subdir/" in text
    # Two windows side by side: the frames meet in the middle of every body
    # row, so the boundary is drawn rather than implied.  The left pane's own
    # edge carries its scrollbar, which is where a Turbo Vision window kept
    # one, and the right pane's frame is the column after it.
    body = text.splitlines()[3]
    assert body[39] in "\u2591\u2588\u25b2\u25bc"
    assert body[40] in "|\u2551\u2502"
    # The active pane is framed double, the other single.
    assert "\u2554" in text and "\u250c" in text


def test_the_header_shows_the_location(panes):
    text, _ = _render(panes, cols=200)
    assert "local:" in text
    assert "/left" in text


def test_a_long_path_keeps_its_tail_visible(panes):
    deep = panes / "left" / ("a" * 40) / ("b" * 40)
    deep.mkdir(parents=True)
    text, _ = _render(panes, prepare=lambda app: app.left.chdir(str(deep)))
    # Truncated from the left, marked with a tilde, so the deepest part shows.
    assert "~" in text
    assert "bbbb" in text


def test_sizes_and_times_are_rendered(panes):
    text, _ = _render(panes)
    assert "4.9K" in text          # big.bin
    assert "<DIR>" in text


def test_a_symlink_is_marked(panes):
    text, _ = _render(panes, cols=200)
    link_line = [line for line in text.splitlines() if "alias" in line][0]
    # The marker column, just inside the window frame, carries "@".
    assert link_line[1] == "@"


def test_tagged_entries_are_marked(panes):
    def tag(app):
        app.left.selected = {"readme.txt"}

    text, _ = _render(panes, prepare=tag)
    tagged = [line for line in text.splitlines() if "readme.txt" in line][0]
    assert tagged[1] == "*"


def test_the_footer_summarises_a_tagged_selection(panes):
    def tag(app):
        app.left.selected = {"readme.txt", "big.bin"}

    text, _ = _render(panes, prepare=tag)
    assert "2 tagged" in text


def test_the_footer_describes_the_entry_under_the_cursor(panes):
    def point_at_file(app):
        names = [e.name for e in app.left.entries]
        app.left.move_to(names.index("readme.txt"))

    text, _ = _render(panes, prepare=point_at_file)
    assert "readme.txt  5" in text


def test_the_footer_counts_items_on_a_directory(panes):
    text, _ = _render(panes)          # the cursor starts on ".."
    assert "items" in text


def test_the_footer_shows_a_listing_error(panes):
    def break_it(app):
        app.left.error = "Permission denied"

    text, _ = _render(panes, prepare=break_it)
    assert "! Permission denied" in text


def test_the_status_line_and_function_bar(panes):
    def message(app):
        app.message = "Copied 3 files"

    text, _ = _render(panes, prepare=message)
    assert "Copied 3 files" in text
    assert "Help" in text and "Quit" in text


def test_the_menu_bar_carries_the_menus_and_a_clock(panes):
    text, _ = _render(panes)
    bar = text.splitlines()[0]
    for caption in ("File", "Command", "Options", "Help"):
        assert caption in bar
    assert re.search(r"\d\d:\d\d", bar)


def test_the_desktop_is_shaded_behind_the_panes(panes):
    """The hint line sits on the desktop, so its row shows the texture."""
    text, _ = _render(panes)
    assert "\u2591" in text.splitlines()[-2]


def test_a_tiny_terminal_says_so(panes):
    text, _ = _render(panes, rows=5, cols=40)
    assert "Terminal too small" in text


def test_a_narrow_terminal_says_so(panes):
    text, _ = _render(panes, rows=24, cols=19, prepare=None)
    assert "Terminal too small" in text


def test_a_pane_running_a_plugin_is_drawn_by_the_plugin(panes):
    class _Plugin:
        def draw(self, stdscr, y, x, h, w):
            stdscr.addstr(y + 1, x + 1, "PLUGIN OUTPUT")

    text, _ = _render(panes, prepare=lambda app: setattr(app.left, "plugin",
                                                         _Plugin()))
    assert "PLUGIN OUTPUT" in text


def test_a_plugin_that_fails_to_draw_does_not_take_the_ui_down(panes):
    class _Broken:
        def draw(self, stdscr, y, x, h, w):
            raise RuntimeError("bad plugin")

    text, _ = _render(panes, cols=200,
                      prepare=lambda app: setattr(app.left, "plugin", _Broken()))
    assert "plugin draw error" in text
    # The other pane still renders normally.
    assert "local:" in text


def test_a_plugin_that_fails_in_a_pane_too_narrow_to_report_it(panes):
    class _Broken:
        def draw(self, stdscr, y, x, h, w):
            raise RuntimeError("x" * 200)

    # Drawing the error itself may not fit; it must still not raise.
    _render(panes, cols=22,
            prepare=lambda app: setattr(app.left, "plugin", _Broken()))


def test_a_listing_longer_than_the_pane_scrolls(panes):
    for i in range(60):
        write(str(panes / "left" / f"f{i:02d}.txt"), "x")

    def to_the_end(app):
        app.left.move_to(len(app.left.entries) - 1)

    text, _ = _render(panes, prepare=to_the_end)
    assert "f59.txt" in text
    assert "f00.txt" not in text


def test_an_empty_pane_draws_blank_rows(panes):
    (panes / "empty").mkdir()
    text, _ = _render(panes, prepare=lambda app: app.left.chdir(str(panes / "empty")))
    assert "items" in text


# -- the main loop -------------------------------------------------------------

class _LoopScreen(_StubScreen):
    """A screen that replays a key script and records the timeouts asked for."""

    def __init__(self, keys, height=24, width=80):
        super().__init__(height, width)
        self.keys = list(keys)
        self.timeouts: list[int] = []
        self.drawn = False

    def getch(self):
        if not self.keys:
            raise AssertionError("the loop asked for more keys than scripted")
        key = self.keys.pop(0)
        if isinstance(key, BaseException):
            raise key
        return key

    def timeout(self, value):
        self.timeouts.append(value)

    def erase(self):
        self.drawn = True

    def addstr(self, *args, **kwargs):
        pass

    def addch(self, *args, **kwargs):
        pass

    def noutrefresh(self):
        pass

    def attrset(self, *args):
        pass


@pytest.fixture
def loop_app(panes, monkeypatch):
    def make(keys, **kwargs):
        screen = _LoopScreen(keys, **kwargs)
        app = App(screen, str(panes / "left"), str(panes / "right"))
        return app, screen

    return make


def test_the_loop_quits_on_the_quit_key(loop_app, monkeypatch):
    from meridian_commander import dialogs

    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    app, screen = loop_app([ord("q")])
    app.run()
    assert app.running is False
    # With no ticking plug-in the loop blocks rather than polling.
    assert screen.timeouts[0] == -1


def test_the_loop_polls_while_a_plugin_wants_a_timer(loop_app, monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)

    ticks = []

    class _Ticker:
        wants_timer = True

        def tick(self):
            ticks.append(1)

        def draw(self, *a):
            pass

        def handle_key(self, key):
            return False

    app, screen = loop_app([-1, -1, curses.KEY_F10, ord("q")])
    app.left.plugin = _Ticker()
    from meridian_commander import dialogs

    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: True)
    app.run()
    assert screen.timeouts[0] == 120
    assert len(ticks) >= 2          # the timeouts pumped the plug-in


def test_a_plugin_whose_tick_fails_is_reported_not_fatal(loop_app):
    class _Broken:
        wants_timer = True

        def tick(self):
            raise RuntimeError("tick failed")

    app, _ = loop_app([])
    app.left.plugin = _Broken()
    assert app._tick_plugins() is True
    assert "Plugin error: tick failed" in app.message


def test_a_plugin_with_no_tick_still_asks_for_polling(loop_app):
    class _NoTick:
        wants_timer = True

    app, _ = loop_app([])
    app.left.plugin = _NoTick()
    assert app._tick_plugins() is True


def test_ctrl_c_offers_to_quit(loop_app, monkeypatch):
    from meridian_commander import dialogs

    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: True)
    app, _ = loop_app([KeyboardInterrupt()])
    app.run()


def test_ctrl_c_can_be_declined(loop_app, monkeypatch):
    from meridian_commander import dialogs

    monkeypatch.setattr(curses, "doupdate", lambda: None)
    answers = iter([False, True])
    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: next(answers))
    app, _ = loop_app([KeyboardInterrupt(), ord("q")])
    app.run()
    assert app.running is False


class _RefusingScreen(_StubScreen):
    """A screen that refuses every write, as a full terminal does at its edge."""

    def erase(self):
        pass

    def addstr(self, *args, **kwargs):
        raise curses.error("addwstr() returned ERR")

    def addch(self, *args, **kwargs):
        raise curses.error("addch() returned ERR")

    def attrset(self, *args):
        pass

    def noutrefresh(self):
        pass


def test_drawing_survives_a_screen_that_refuses_every_write(panes, monkeypatch):
    """Every write in the main draw is guarded; none may escape."""
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "ACS_VLINE",
                        getattr(curses, "ACS_VLINE", ord("|")), raising=False)
    monkeypatch.setattr(curses, "has_colors", lambda: False)

    class _Plugin:
        def draw(self, stdscr, y, x, h, w):
            raise RuntimeError("and the error report cannot be drawn either")

    app = App(_RefusingScreen(), str(panes / "left"), str(panes / "right"))
    app.left.plugin = _Plugin()
    app.left.error = "unreadable"
    app.right.selected = {"readme.txt"}
    app.draw()                                # must not raise


def test_the_function_bar_stops_at_the_screen_edge(panes, monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "ACS_VLINE",
                        getattr(curses, "ACS_VLINE", ord("|")), raising=False)
    monkeypatch.setattr(curses, "has_colors", lambda: False)

    drawn = []

    class _Recording(_StubScreen):
        def addstr(self, y, x, text, *args):
            drawn.append((y, x, text))

    screen = _Recording(24, 80)
    app = App(screen, str(panes / "left"), str(panes / "right"))
    # Narrower than the ten function keys: the loop stops instead of wrapping.
    app._draw_function_bar(23, 5)
    assert drawn
    # The grey background of the bar, then the five key numbers that fit --
    # at one column each there is no room for their labels, and the loop
    # stops rather than wrapping around.
    assert len(drawn) == 1 + 5
    assert [d[2] for d in drawn[1:]] == ["1", "2", "3", "4", "5"]


def test_activating_nothing_does_nothing(panes, monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    app = App(_StubScreen(), str(panes / "left"), str(panes / "right"))
    app.left.entries = []
    app._activate_entry()                     # must not raise
