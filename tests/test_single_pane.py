# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Single mode: one pane at full width, the other kept in reserve.

The rule under test throughout is the one the feature exists to keep: with
one pane on screen, nothing acts on the pane out of sight without naming it
first, and anything that has to work in both panes brings both back for as
long as it runs.
"""

from __future__ import annotations

import curses

import pytest

from meridian_commander import app as app_mod
from meridian_commander import config, dialogs
from meridian_commander.app import App, main
from meridian_commander.peek import PeekPane
from meridian_commander.plugin_api import (
    InputOutputPlugin,
    PanePlugin,
    PluginContext,
)
from meridian_commander.plugins.terminal import MENU_KEY, TerminalPlugin

from support import _ScriptedDialogs, read, with_curses_screen, write, write_zip


@pytest.fixture(autouse=True)
def _quiet_progress(monkeypatch):
    """The progress window needs a real screen and adds nothing here."""

    class _Progress:
        def __init__(self, stdscr, title):
            pass

        def set_overall(self, text):
            pass

        def update(self, cur, total, label):
            pass

        def cancelled(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(dialogs, "ProgressDialog", _Progress)


@pytest.fixture
def single(app):
    """The suite's two-pane app, switched to one pane with the left shown."""
    app._toggle_single()
    return app


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def _shown(app):
    return app._panes_on_screen()


# -- drawing, on a real screen --------------------------------------------------

@pytest.fixture
def panes(tmp_path):
    write(str(tmp_path / "left" / "leftonly.txt"), "left")
    write(str(tmp_path / "left" / "second.txt"), "left again")
    write(str(tmp_path / "right" / "rightonly.txt"), "right")
    write(str(tmp_path / "right" / "other.txt"), "right again")
    return tmp_path


def _on_screen(panes, script, rows=24, cols=80, single=True):
    """Run ``script(app, snap)`` on a real screen; ``snap()`` draws and reads.

    Rows are read whole and decoded, so a column index is a screen column
    even where the frame is drawn in three-byte box-drawing characters.
    """
    def run(stdscr):
        app = App(stdscr, str(panes / "left"), str(panes / "right"),
                  single=single)

        def snap():
            app.draw()
            return [stdscr.instr(row, 0).decode() for row in range(rows)]

        return script(app, snap)

    return with_curses_screen(rows, cols, run)


_CORNERS = "╔┌+"          # double, single and ASCII top-left corners


def test_single_mode_draws_one_pane_across_the_whole_width(panes):
    screen = _on_screen(panes, lambda app, snap: snap())
    text = "\n".join(screen)
    assert "leftonly.txt" in text
    assert "rightonly.txt" not in text
    # One window: one top-left corner, at the left edge, and its top-right
    # corner at the right edge -- not a second frame in the middle.
    top = screen[1]
    assert sum(top.count(c) for c in _CORNERS) == 1
    assert top[0] == "╔"
    assert top[79] == "╗"
    body = screen[3]
    assert body[40] not in "|║│"


def test_two_panes_are_still_two(panes):
    screen = _on_screen(panes, lambda app, snap: snap(), single=False)
    text = "\n".join(screen)
    assert "leftonly.txt" in text and "rightonly.txt" in text
    assert sum(screen[1].count(c) for c in _CORNERS) == 2


def test_the_toggle_message_fits_beside_the_note_at_80_columns(panes):
    def script(app, snap):
        app._toggle_single()
        app._toggle_single()
        return snap()[22]

    assert "Single pane -- Tab flips, z shows both" in _on_screen(panes, script)


def test_the_hint_line_says_where_the_hidden_pane_is(panes):
    def script(app, snap):
        app._set_message("Copy complete")
        return snap()[22]

    hint = _on_screen(panes, script)
    assert hint[1:].startswith("Copy complete")
    assert "right pane hidden: " in hint
    # Trimmed from the left, as a caption is: the deepest directory stays.
    assert hint.rstrip("\u2591 ").endswith("/right")


def test_the_hint_names_the_left_pane_when_that_is_the_hidden_one(panes):
    def script(app, snap):
        app.handle_key(9)                      # Tab: the right pane, alone
        return snap()

    screen = _on_screen(panes, script, cols=200)
    text = "\n".join(screen)
    assert "rightonly.txt" in text and "leftonly.txt" not in text
    assert ("left pane hidden: local:" + str(panes / "left")) in screen[22]


def test_toggling_back_restores_both_panes_as_they_were(panes):
    def script(app, snap):
        app.right.chdir(str(panes / "right"))
        _point_at(app.right, "rightonly.txt")
        app.right.selected = {"other.txt"}
        cursor = app.right.cursor
        hidden = "\n".join(snap())
        app.handle_key(ord("z"))
        both = "\n".join(snap())
        return hidden, both, cursor, app

    hidden, both, cursor, app = _on_screen(panes, script)
    assert "rightonly.txt" not in hidden
    assert "rightonly.txt" in both and "leftonly.txt" in both
    assert app.right.path == str(panes / "right")
    assert app.right.cursor == cursor
    assert app.right.selected == {"other.txt"}
    assert app.message == "Two panes"


def test_single_mode_works_down_to_the_smallest_screen(panes):
    def script(app, snap):
        screen = snap()
        app.handle_key(curses.KEY_RESIZE)
        return screen, app.single

    screen, still_single = _on_screen(panes, script, rows=8, cols=24)
    assert still_single
    assert "Terminal too small" not in "\n".join(screen)
    assert sum(screen[1].count(c) for c in _CORNERS) == 1
    assert "right hidden" in screen[6]


def test_resizing_keeps_single_mode(app):
    app._toggle_single()
    app.handle_key(curses.KEY_RESIZE)
    assert app.single
    assert _shown(app) == [app.left]


# -- the hint note, measured --------------------------------------------------

def test_a_long_location_is_trimmed_from_the_left(app, tmp_path):
    app.single = True
    deep = tmp_path / "right" / ("d" * 60)
    deep.mkdir()
    app.right.chdir(str(deep))
    note = app._hidden_note(app.right, 80)
    assert len(note) == 39
    assert note.startswith("right pane hidden: ~")
    assert note.endswith("d" * 19)


def test_a_narrow_screen_says_only_which_side(app):
    app.single = True
    assert app._hidden_note(app.right, 30) == "right hidden"
    assert app._hidden_note(app.left, 24) == "left hidden"


# -- the toggle: key, menu, startup ---------------------------------------------

def test_z_switches_between_one_pane_and_two(app):
    app.handle_key(ord("z"))
    assert app.single
    assert _shown(app) == [app.left]
    assert app.message == "Single pane -- Tab flips, z shows both"
    app.handle_key(ord("z"))
    assert not app.single
    assert _shown(app) == [app.left, app.right]


def test_the_options_menu_entry_toggles_it_and_carries_a_tick(app):
    options = next(i for i, m in enumerate(app_mod.MENUS)
                   if m["name"] == "options")

    def ticked():
        return next(item for item in app._menu_items(options)
                    if item.get("name") == "single")["checked"]

    assert ticked() is False
    app._dispatch("single")
    assert app.single and ticked() is True
    entry = next(item for item in app_mod.MENUS[options]["items"]
                 if item.get("name") == "single")
    assert entry["key"] == "z"


def test_the_help_screen_lists_the_key(app, monkeypatch):
    dialogs_seen = _ScriptedDialogs(monkeypatch)
    app._help()
    assert "  z              single pane" in dialogs_seen.all_text


def test_a_tab_flips_to_the_other_pane_at_full_width(single):
    single.handle_key(9)
    assert single.active is single.right
    assert _shown(single) == [single.right]
    single.handle_key(9)
    assert _shown(single) == [single.left]


# -- features that only need a destination: named, not shown --------------------

def test_copy_names_the_hidden_destination_and_lands_there(single, tmp_path,
                                                           monkeypatch):
    _point_at(single.left, "file.txt")
    seen = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right" / "sub")])
    single.handle_key(curses.KEY_F5)
    assert seen.prompts == ["Copy 'file.txt' to the hidden pane, local:"]
    assert read(str(tmp_path / "right" / "sub" / "file.txt")) == "left"
    assert _shown(single) == [single.left]


def test_move_of_several_names_the_hidden_destination(single, tmp_path,
                                                      monkeypatch):
    single.left.selected = {"file.txt", "sub"}
    seen = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right" / "sub")])
    single.handle_key(curses.KEY_F6)
    assert seen.prompts == ["Move 2 items to the hidden pane, local:"]
    assert (tmp_path / "right" / "sub" / "sub").is_dir()
    assert not (tmp_path / "left" / "file.txt").exists()


def test_with_two_panes_the_prompt_is_as_it_was(app, monkeypatch):
    _point_at(app.left, "file.txt")
    seen = _ScriptedDialogs(monkeypatch, prompt=[None])
    app._copy()
    assert seen.prompts == ["Copy 'file.txt' to:"]


def test_extract_names_the_hidden_destination(single, tmp_path, monkeypatch):
    write_zip(str(tmp_path / "left" / "bundle.zip"), {"inside.txt": "zipped"})
    single.left.refresh()
    _point_at(single.left, "bundle.zip")
    seen = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    single.handle_key(ord("x"))
    assert seen.prompts == [
        "Extract 'bundle.zip' into its own folder in the hidden pane, local:"]
    assert read(str(tmp_path / "right" / "bundle" / "inside.txt")) == "zipped"


def test_extracting_several_names_the_hidden_destination(single, tmp_path,
                                                         monkeypatch):
    for name in ("one.zip", "two.zip"):
        write_zip(str(tmp_path / "left" / name), {"a.txt": name})
    single.left.refresh()
    single.left.selected = {"one.zip", "two.zip"}
    seen = _ScriptedDialogs(monkeypatch, prompt=[None])
    single._extract()
    assert seen.prompts == ["Extract 2 archives, each into its own folder, "
                            "in the hidden pane, local:"]


def test_extract_with_two_panes_is_as_it_was(app, tmp_path, monkeypatch):
    write_zip(str(tmp_path / "left" / "bundle.zip"), {"inside.txt": "zipped"})
    app.left.refresh()
    _point_at(app.left, "bundle.zip")
    seen = _ScriptedDialogs(monkeypatch, prompt=[None])
    app._extract()
    assert seen.prompts == ["Extract 'bundle.zip' into its own folder under:"]


def test_sync_marks_the_hidden_side_in_its_confirmation(single, tmp_path,
                                                        monkeypatch):
    write(str(tmp_path / "left" / "fresh.txt"), "new on the left")
    single.left.refresh()
    seen = _ScriptedDialogs(monkeypatch, confirm=[False])
    single.handle_key(curses.KEY_F9)
    preview = seen.messages[0][1].splitlines()
    assert preview[0].endswith(":" + str(tmp_path / "left"))
    assert preview[1].endswith(str(tmp_path / "right") + "   (hidden pane)")
    assert not (tmp_path / "right" / "fresh.txt").exists()


def test_the_sync_size_warning_marks_the_hidden_side(single, tmp_path,
                                                     monkeypatch):
    for index in range(30):
        (tmp_path / "right" / f"d{index:02}").mkdir()
    single.right.refresh()
    seen = _ScriptedDialogs(monkeypatch, confirm=[False])
    single._sync()
    warning = seen.messages[0][1].splitlines()
    assert "(hidden pane)" not in warning[0]
    assert warning[2].endswith("   (hidden pane)")
    assert single.message == "Sync cancelled"


def test_compare_says_when_the_hidden_side_has_nothing_to_offer(single,
                                                                monkeypatch):
    _point_at(single.left, "file.txt")
    single.right.move_to(0)                  # "..", which is no file
    single.handle_key(ord("D"))
    assert single.message == "Compare: right pane (hidden) -- nothing is selected"


def test_compare_runs_on_the_hidden_pane_file(single, monkeypatch):
    ran = []
    monkeypatch.setattr(app_mod.Comparison, "run",
                        lambda self, stdscr: ran.append(
                            (self.left_name, self.right_name)))
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    _point_at(single.left, "file.txt")
    _point_at(single.right, "file.txt")
    single.handle_key(ord("D"))
    assert ran == [("file.txt", "file.txt")]
    assert _shown(single) == [single.left]


def test_compare_on_screen_sides_are_not_called_hidden(app):
    app.left.move_to(0)
    app._compare()
    assert app.message == "Compare: left pane -- nothing is selected"


def test_equals_names_the_hidden_pane_it_moved(single, tmp_path):
    single.handle_key(ord("="))
    assert single.right.path == str(tmp_path / "left")
    assert single.message == (f"Hidden pane: local:{tmp_path / 'left'} "
                              "-- '<' there goes back")
    assert _shown(single) == [single.left]


def test_equals_with_two_panes_says_other_pane(app, tmp_path):
    app._mirror_to_other_pane()
    assert app.message.startswith("Other pane: local:")


def test_ctrl_u_brings_the_hidden_pane_on_screen(single, tmp_path):
    shown_before = single.active
    hidden_before = single.other
    single.handle_key(21)
    assert single.active is hidden_before
    assert _shown(single) == [hidden_before]
    # The sides swapped too, and the pane that was showing is now the hidden
    # one, on the right.
    assert single.left is hidden_before and single.right is shown_before
    assert single.message == "Panes swapped -- the hidden one is on screen"


def test_the_context_menu_copy_names_the_hidden_pane(single, monkeypatch):
    _point_at(single.left, "file.txt")
    seen = _ScriptedDialogs(monkeypatch, menu=["Copy to other pane"],
                            prompt=[None])
    single._context_menu()
    assert seen.prompts == ["Copy 'file.txt' to the hidden pane, local:"]


# -- features that work in both panes: both come back while they run -------------

def test_head_and_tail_shows_both_panes_while_it_is_open(single):
    single.handle_key(ord("h"))
    assert isinstance(single.left.plugin, PeekPane)
    assert _shown(single) == [single.left, single.right]
    # Steering it from the other pane keeps both: that is how it is used.
    single.handle_key(9)
    assert single.active is single.right
    assert _shown(single) == [single.left, single.right]
    single.handle_key(9)
    single.handle_key(ord("h"))              # h again gives the pane back
    assert single.left.plugin is None
    assert _shown(single) == [single.left]


class _UsesOtherPane(InputOutputPlugin):
    """A stand-in user plug-in, written to the contract as it always was."""

    name = "Stand-in"
    description = "reads the other pane"

    def process(self, line):
        return [self.ctx.other_path,
                ",".join(e.name for e in self.ctx.other_selected())]


class _OwnPaneOnly(PanePlugin):
    name = "Own pane only"
    description = "never looks across"
    uses_other_pane = False

    def draw(self, stdscr, y, x, h, w):
        pass

    def handle_key(self, key):
        return None


def _offer_plugins(monkeypatch, *classes):
    from meridian_commander import plugins

    monkeypatch.setattr(plugins, "discover", lambda: (list(classes), []))


def test_a_user_plugin_opened_in_single_mode_gets_both_panes(single, tmp_path,
                                                             monkeypatch):
    _offer_plugins(monkeypatch, _UsesOtherPane)
    single.right.selected = {"file.txt"}
    _ScriptedDialogs(monkeypatch, menu=[0])
    single.handle_key(ord("p"))
    plugin = single.left.plugin
    assert isinstance(plugin, _UsesOtherPane)
    assert _shown(single) == [single.left, single.right]
    assert single.message.endswith("-- both panes on screen while it runs")
    # The contract is untouched: it sees the pane that was hidden.
    assert plugin.process("") == [str(tmp_path / "right"), "file.txt"]
    # Closing it puts single mode back.
    single._close_plugin(single.left)
    assert _shown(single) == [single.left]


def test_a_plugin_that_ignores_the_other_pane_takes_the_single_pane(
        single, monkeypatch):
    _offer_plugins(monkeypatch, _OwnPaneOnly)
    _ScriptedDialogs(monkeypatch, menu=[0])
    single._plugin_mode()
    assert isinstance(single.left.plugin, _OwnPaneOnly)
    assert _shown(single) == [single.left]
    assert single.message == ("Plug-in 'Own pane only' opened -- Esc closes "
                              "it, Tab switches panes")


def test_load_errors_still_say_both_panes_are_on_screen(single, monkeypatch):
    from meridian_commander import plugins

    monkeypatch.setattr(plugins, "discover",
                        lambda: ([_UsesOtherPane], ["broken.py: oops"]))
    _ScriptedDialogs(monkeypatch, menu=[0])
    single._plugin_mode()
    assert single.message == ("Plug-in 'Stand-in' opened (1 plug-in(s) failed "
                              "to load) -- both panes on screen while it runs")


@pytest.mark.parametrize("module, cls", [
    ("processes", "ProcessesPlugin"),
    ("json_push", "JsonPush"),
    ("run_remote_script", "RunRemoteScript"),
    ("ssh_doctor", "SshDoctor"),
    ("terminal", "TerminalPlugin"),
])
def test_the_built_ins_that_ignore_the_other_pane_say_so(module, cls):
    import importlib

    plugin_cls = getattr(
        importlib.import_module(f"meridian_commander.plugins.{module}"), cls)
    assert plugin_cls.uses_other_pane is False


def test_every_other_built_in_is_given_both_panes():
    from meridian_commander.plugins import discover

    alone = {"Processes", "JSON push", "Run remote script", "SSH doctor",
             "Terminal"}
    classes, _errors = discover()
    for cls in classes:
        assert cls.uses_other_pane is (cls.name not in alone), cls.name


def test_z_with_a_plugin_on_the_other_pane_says_both_stay(app):
    app.left.plugin = _UsesOtherPane(
        PluginContext(app=app, own_panel=app.left, other_panel=app.right))
    app.handle_key(9)                        # into the right pane, a listing
    app.handle_key(ord("z"))
    assert app.single
    assert _shown(app) == [app.left, app.right]
    assert app.message == ("Single pane -- both stay on screen while "
                           "Stand-in works on the other pane")


def test_a_plugin_with_no_name_is_still_described(app):
    class _Nameless(_UsesOtherPane):
        name = ""

    app.right.plugin = _Nameless(
        PluginContext(app=app, own_panel=app.right, other_panel=app.left))
    app._toggle_single()
    assert app.message.endswith("while a plug-in works on the other pane")


# -- the terminal: Ctrl-] Tab keeps it beside the pane it hands over to -----------

def test_a_terminal_takes_the_whole_width(single, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    single.handle_key(ord("t"))
    try:
        assert isinstance(single.left.plugin, TerminalPlugin)
        assert _shown(single) == [single.left]
    finally:
        single._close_plugin(single.left)


def test_ctrl_bracket_from_a_terminal_shows_both_until_it_is_back(
        single, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    single.handle_key(ord("t"))
    terminal = single.left
    try:
        single.handle_key(MENU_KEY)            # Ctrl-] then Tab
        single.handle_key(9)
        assert single.active is single.right
        assert _shown(single) == [single.left, single.right]
        # Work in the listing goes on with the shell in sight...
        single.handle_key(curses.KEY_DOWN)
        assert _shown(single) == [single.left, single.right]
        # ...and Tab back to the shell gives it the screen again.
        single.handle_key(9)
        assert single.active is terminal
        assert _shown(single) == [terminal]
    finally:
        single._close_plugin(terminal)


def test_ctrl_bracket_with_two_panes_keeps_nothing_beside(app):
    ctx = PluginContext(app=app, own_panel=app.left, other_panel=app.right)
    ctx.focus_other()
    assert app.active is app.right
    assert app._beside is None
    app._toggle_single()
    assert _shown(app) == [app.right]


def test_focus_other_on_an_app_without_single_mode_still_switches():
    class _Plain:
        active = None

    plain = _Plain()
    ctx = PluginContext(app=plain, own_panel="mine", other_panel="theirs")
    ctx.focus_other()
    assert plain.active == "theirs"


def test_turning_single_mode_off_lets_go_of_the_pane_kept_beside(single):
    PluginContext(app=single, own_panel=single.left,
                  other_panel=single.right).focus_other()
    assert single._beside is single.left
    single._toggle_single()
    assert single._beside is None


# -- the hidden pane goes on working ---------------------------------------------

class _Ticker(PanePlugin):
    """A terminal in miniature: output that arrives while nobody looks."""

    name = "Ticker"
    description = "counts its ticks"
    uses_other_pane = False
    wants_timer = True

    def on_start(self):
        self.lines = []

    def tick(self):
        self.lines.append(f"tick {len(self.lines) + 1}")

    def draw(self, stdscr, y, x, h, w):
        for row, line in enumerate(self.lines[-(h - 2):]):
            self.put(stdscr, y + 1 + row, x + 1, w - 2, line)

    def handle_key(self, key):
        return None


def test_a_plugin_in_the_hidden_pane_keeps_running(panes):
    def script(app, snap):
        app.right.plugin = _Ticker(PluginContext(
            app=app, own_panel=app.right, other_panel=app.left))
        before = "\n".join(snap())
        for _ in range(3):
            app._tick_plugins()
        app.handle_key(9)
        after = "\n".join(snap())
        return before, after

    before, after = _on_screen(panes, script)
    assert "tick" not in before
    assert "tick 1" in after and "tick 3" in after


# -- the mouse only reaches what is on screen ------------------------------------

def test_a_click_on_the_right_half_goes_to_the_pane_shown(panes, monkeypatch):
    def script(app, snap):
        snap()
        row = [e.name for e in app.left.entries].index("second.txt")
        monkeypatch.setattr(curses, "getmouse", lambda: (
            0, 60, 3 + row, 0, curses.BUTTON1_CLICKED))
        app.handle_key(curses.KEY_MOUSE)
        return app.active is app.left, app.left.current().name, app.right.cursor

    on_left, name, right_cursor = _on_screen(panes, script)
    assert on_left
    assert name == "second.txt"
    assert right_cursor == 0


# -- starting in single mode -----------------------------------------------------

def test_the_default_configuration_starts_with_two_panes(tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert config.single_pane() is False
    config.ensure_config()
    assert "\npanes = 2\n" in read(config.config_path())
    assert config.single_pane() is False


@pytest.mark.parametrize("value, single", [
    ("1", True), (" 1 ", True), ("2", False), ("", False), ("one", False),
])
def test_panes_1_starts_in_single_mode(tmp_path, monkeypatch, value, single):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    write(config.config_path(), f"[ui]\npanes = {value}\n")
    assert config.single_pane() is single


def test_main_starts_the_app_in_single_mode_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    write(config.config_path(), "[ui]\npanes = 1\n")
    started = {}

    class _Screen:
        def keypad(self, flag):
            pass

    class _FakeApp:
        def __init__(self, stdscr, left_path=None, right_path=None,
                     single=False):
            started["single"] = single

        def run(self):
            pass

        def printwd_path(self):
            return None

    monkeypatch.setattr(curses, "wrapper", lambda fn, args: fn(_Screen(), args))
    monkeypatch.setattr(app_mod, "App", _FakeApp)
    for name in ("use_default_colors", "raw", "start_color"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "mousemask", lambda mask: None)
    monkeypatch.setattr(curses, "mouseinterval", lambda ms: None)
    assert main([str(tmp_path), str(tmp_path)]) == 0
    assert started["single"] is True
