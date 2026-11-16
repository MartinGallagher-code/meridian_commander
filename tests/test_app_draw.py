# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The application's drawing: panes, status line, function bar, main loop."""

from __future__ import annotations

import curses
import re

import pytest

from meridian_commander import theme
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


def test_a_terminal_too_small_even_for_the_message(panes):
    """The sentence is eighteen characters and the terminal had four.

    Writing it straight into the window raised, so shrinking a terminal far
    enough ended the application with a traceback -- while it was explaining
    that the terminal was too small.
    """
    text, _ = _render(panes, rows=4, cols=4, prepare=None)
    assert text.splitlines()[0].startswith("Term")


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


# -- the terminal's cursor: the plug-in's screen_cursor() (MC-030) ------------
#
# Each frame ends by asking the active pane's plug-in where the terminal's
# cursor goes.  The hook was looked up as ``cursor``, which the process and
# container lists keep as their selected row, so ``0()`` ended the program
# the moment either opened.  A plug-in is someone else's code, as its draw()
# is: whatever its hook is or does, the frame is drawn and the program goes
# on, with no cursor.

class _CursorScreen(_StubScreen):
    """A stub screen that records where the cursor is put, and refuses a
    place off the screen as a real window does."""

    def __init__(self) -> None:
        super().__init__()
        self.moves: list[tuple] = []

    def move(self, y, x):
        height, width = self.getmaxyx()
        if not (0 <= y < height and 0 <= x < width):
            raise curses.error("wmove() returned ERR")
        self.moves.append((y, x))


class _Quiet:
    """A plug-in as a third party might write it: no PanePlugin, only the
    two methods the application has always called, and so no hook."""

    def draw(self, stdscr, y, x, h, w):
        pass

    def handle_key(self, key):
        return True


class _Rows(_Quiet):
    """A list browser keeping its selected row as ``cursor``, as Processes
    and Containers do."""

    def __init__(self, row=0):
        self.cursor = row


class _Placed(_Quiet):
    """A plug-in whose hook answers ``answer``, or raises it."""

    def __init__(self, answer):
        self.answer = answer

    def screen_cursor(self):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture
def cursor_app(panes, monkeypatch):
    """An app on a cursor-keeping stub screen, and the curs_set() calls.

    curs_set() and doupdate() are the library's, not the screen's, and
    refuse without an initialised screen (a2a2b53), so both are absorbed;
    the visibility asked for is what is recorded.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(panes / "config"))
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    shown: list[int] = []
    monkeypatch.setattr(curses, "curs_set", shown.append)

    def make(plugin):
        app = App(_CursorScreen(), str(panes / "left"), str(panes / "right"))
        app.left.plugin = plugin
        app.active = app.left
        return app, app.stdscr.moves, shown

    return make


@pytest.mark.parametrize("row", [0, 3])
def test_a_plugin_keeping_its_selected_row_as_cursor_is_drawn(cursor_app,
                                                              row):
    app, moves, shown = cursor_app(_Rows(row))
    app.draw()                       # 1.25.0: TypeError, and the program ends
    app.draw()
    assert moves == [] and shown == []
    assert app.left.plugin.cursor == row          # its own state, untouched


def test_a_plugin_without_the_hook_has_no_cursor(cursor_app):
    app, moves, shown = cursor_app(_Quiet())
    app.draw()
    assert moves == [] and shown == []


def test_a_hook_that_is_not_a_method_is_not_called(cursor_app):
    plugin = _Quiet()
    plugin.screen_cursor = (3, 4)    # a value, not something to call
    app, moves, shown = cursor_app(plugin)
    app.draw()
    assert moves == [] and shown == []


def test_a_hook_that_raises_means_no_cursor(cursor_app):
    app, moves, shown = cursor_app(_Placed(RuntimeError("broken hook")))
    app.draw()
    assert moves == [] and shown == []


@pytest.mark.parametrize("answer", [
    None, "x", 5, (1,), (1, 2, 3), ("1", 2), (1.0, 2), [1, 2], (None, None),
])
def test_an_answer_that_is_not_a_place_means_no_cursor(cursor_app, answer):
    app, moves, shown = cursor_app(_Placed(answer))
    app.draw()
    assert moves == [] and shown == []


@pytest.mark.parametrize("spot", [(24, 0), (0, 80), (-1, 5), (999, 999)])
def test_a_place_off_the_screen_means_no_cursor(cursor_app, spot):
    app, moves, shown = cursor_app(_Placed(spot))
    app.draw()
    assert moves == [] and shown == []


def test_the_cursor_goes_where_the_hook_says_and_is_hidden_after(cursor_app):
    plugin = _Placed((5, 7))
    app, moves, shown = cursor_app(plugin)
    app.draw()
    assert moves == [(5, 7)] and shown == [1]
    plugin.answer = (6, 9)                       # it follows the hook
    app.draw()
    assert moves[-1] == (6, 9) and shown == [1, 1]
    plugin.answer = None
    app.draw()
    assert shown == [1, 1, 0]                    # hidden once
    app.draw()
    assert shown == [1, 1, 0]                    # and then left alone


@pytest.mark.parametrize("bad", [
    RuntimeError("broken hook"), "x", (1,), (99, 0),
])
def test_a_hook_that_goes_wrong_hides_a_cursor_it_had_shown(cursor_app, bad):
    plugin = _Placed((5, 7))
    app, moves, shown = cursor_app(plugin)
    app.draw()
    plugin.answer = bad
    app.draw()
    assert moves == [(5, 7)] and shown == [1, 0]


def test_closing_the_plugin_hides_its_cursor(cursor_app):
    app, _, shown = cursor_app(_Placed((5, 7)))
    app.draw()
    app.left.plugin = None
    app.draw()
    app.draw()
    assert shown == [1, 0]


def test_a_place_off_a_real_screen_means_no_cursor(panes, monkeypatch):
    # The refusal on a real window, which the stub screen above imitates.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(panes / "config"))
    shown: list[int] = []
    monkeypatch.setattr(curses, "curs_set", shown.append)
    plugin = _Placed((3, 4))

    def run(stdscr):
        app = App(stdscr, str(panes / "left"), str(panes / "right"))
        app.left.plugin = plugin
        app.draw()
        spot = stdscr.getyx()
        plugin.answer = (30, 100)                 # below and right of 24x80
        app.draw()
        return spot

    assert with_curses_screen(24, 80, run) == (3, 4)
    assert shown == [1, 0]


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
    # The stub screen absorbs every drawing call that is a method on it. The
    # loop makes two that are not -- curses.curs_set() on the way in and
    # curses.doupdate() at the end of each draw() -- and those go to the
    # library itself, which refuses unless a screen has been initialised.
    # These tests passed only because a real-screen test earlier in this file
    # had left one initialised behind them; on their own, all four failed
    # with "must call initscr() first". Absorb the two globals as well, so
    # they depend on nothing but themselves.
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda visibility: 0)

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
    assert app._tick_plugins() == app.PLUGIN_POLL_MS
    assert "Plugin error: tick failed" in app.message


def test_a_plugin_with_no_tick_still_asks_for_polling(loop_app):
    class _NoTick:
        wants_timer = True

    app, _ = loop_app([])
    app.left.plugin = _NoTick()
    assert app._tick_plugins() == app.PLUGIN_POLL_MS


def test_a_busy_plugin_can_ask_for_a_quicker_poll(loop_app):
    """The terminal while output flows: an echo should not wait 120 ms."""
    class _Busy:
        wants_timer = True
        poll_ms = 10

    app, _ = loop_app([])
    app.left.plugin = _Busy()
    assert app._tick_plugins() == 10


@pytest.mark.parametrize("wanted", [None, 500, "10"])
def test_a_plugin_asking_for_no_quicker_poll_keeps_the_usual_one(loop_app,
                                                                 wanted):
    class _Idle:
        wants_timer = True
        poll_ms = wanted

    app, _ = loop_app([])
    app.left.plugin = _Idle()
    assert app._tick_plugins() == app.PLUGIN_POLL_MS


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


# -- the search mode's footer ----------------------------------------------------

def _search_footer(panes, typed, rows=24, cols=80, scheme="turbo"):
    """Search the left pane for ``typed`` and read back its bottom frame.

    Returns the row's text, the attribute of each of its cells, what the
    two footer roles are in ``scheme`` (read while it is the one in use),
    and the app.  The pane's bottom frame is the row above the hint line.
    """
    for name in ("report.txt", "old-report.txt", "notes.txt",
                 "a-name-long-enough-to-overflow-a-half-width-pane.txt"):
        write(str(panes / "left" / name), "x")
    holder = {}

    def run(stdscr):
        theme.init(scheme)
        try:
            app = App(stdscr, str(panes / "left"), str(panes / "right"))
            holder["app"] = app
            app.handle_key(ord("/"))
            for char in typed:
                app.handle_key(ord(char))
            app.draw()
            row = rows - 3
            text = stdscr.instr(row, 0).decode()
            attrs = [stdscr.inch(row, col) & curses.A_ATTRIBUTES
                     for col in range(cols)]
            roles = {role: theme.attr(role) & curses.A_ATTRIBUTES
                     for role in ("panelinfo", "panelerror")}
            return text, attrs, roles
        finally:
            theme.init()

    text, attrs, roles = with_curses_screen(rows, cols, run)
    return text, attrs, roles, holder["app"]


def _role_of(attrs, text, words):
    """The attribute the first cell of ``words`` was drawn in."""
    return attrs[text.index(words)]


@pytest.mark.parametrize("scheme", ["turbo", "midnight", "mono"])
def test_the_footer_shows_the_pattern_and_the_count(panes, scheme):
    text, attrs, roles, _ = _search_footer(panes, "rep", scheme=scheme)
    assert "/ rep  1 of 2" in text
    assert _role_of(attrs, text, "1 of 2") == roles["panelinfo"]
    # The other pane keeps its own footer.
    assert "items" in text[40:]


@pytest.mark.parametrize("scheme", ["turbo", "midnight", "mono"])
def test_no_match_is_drawn_in_the_error_colour(panes, scheme):
    text, attrs, roles, _ = _search_footer(panes, "repx", scheme=scheme)
    assert "/ repx  not found" in text
    assert roles["panelerror"] != roles["panelinfo"]
    assert _role_of(attrs, text, "not found") == roles["panelerror"]
    assert _role_of(attrs, text, "items") == roles["panelinfo"]


def test_nothing_typed_yet_says_what_to_do(panes):
    text, _, _, _ = _search_footer(panes, "")
    assert "/   type a name" in text


def test_a_long_pattern_keeps_its_end_and_the_count_in_a_half_pane(panes):
    typed = "name-long-enough-to-overflow"
    text, _, _, app = _search_footer(panes, typed)
    assert app.active.current_name().startswith("a-name-long")
    left = text[:40]
    assert "1 of 1" in left
    assert "/ ~" in left and "overflow  1 of 1" in left
    # Inside the frame: the pane's bottom-right corner is still drawn.
    assert left[39] in "╝┘+"


def test_the_narrowest_pane_drawn_shows_the_end_of_the_pattern(panes):
    text, _, _, _ = _search_footer(panes, "report", rows=10, cols=24)
    left = text[:12]
    assert "eport" in left
    assert left[11] in "╝┘+"
