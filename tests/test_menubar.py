"""The menu bar: its layout, the keys that open it, and what it runs."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import config as config_mod
from meridian_commander import dialogs, theme
from meridian_commander import app as app_mod
from meridian_commander.app import MENUS, App, menu_layout

from support import (
    _ScriptedDialogs,
    _ScriptedWindow,
    _StubScreen,
    with_curses_screen,
    write,
)


@pytest.fixture(autouse=True)
def _quiet_screen(monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)


@pytest.fixture
def app(tmp_path):
    write(str(tmp_path / "left" / "file.txt"), "hello")
    (tmp_path / "right").mkdir()
    return App(_StubScreen(), str(tmp_path / "left"), str(tmp_path / "right"))


class _Screen(_StubScreen):
    """A screen whose getch() replays a script, for the Esc/Alt handling."""

    def __init__(self, keys=()):
        super().__init__()
        self.keys = list(keys)

    def getch(self):
        return self.keys.pop(0) if self.keys else -1


def _script_dropdown(monkeypatch, answers):
    """Answer each drop-down from a script; record where it was opened."""
    opened: list[tuple] = []

    def fake(stdscr, items, y, x):
        opened.append((y, x, [i.get("name") for i in items]))
        return answers.pop(0)

    monkeypatch.setattr(dialogs, "dropdown", fake)
    return opened


# -- the tree ------------------------------------------------------------------

def test_every_menu_entry_runs_something(app):
    """A menu that names an action nothing answers to is a dead entry."""
    actions = app._actions()
    names = [item["name"] for menu in MENUS for item in menu["items"]
             if not item.get("sep")]
    assert names
    assert not [name for name in names if name not in actions]


def test_an_action_nothing_names_is_still_harmless(app):
    before = app.message
    app._dispatch("no-such-action")
    assert app.message == before


def test_every_menu_caption_has_an_accelerator():
    for menu in MENUS[1:]:                    # the system menu is a glyph
        assert theme.hotkey_letter(menu["label"])
    letters = [theme.hotkey_letter(m["label"]) for m in MENUS[1:]]
    assert len(set(letters)) == len(letters)  # and no two menus share one


def test_the_accelerators_within_a_menu_are_distinct():
    for menu in MENUS:
        letters = [theme.hotkey_letter(item.get("label", ""))
                   for item in menu["items"] if not item.get("sep")]
        given = [letter for letter in letters if letter]
        assert len(set(given)) == len(given), menu["name"]


def test_the_layout_leaves_a_gap_around_every_caption():
    spans = menu_layout()
    assert len(spans) == len(MENUS)
    for (_start, width), menu in zip(spans, MENUS):
        assert width == len(theme.strip_hotkey(menu["label"])) + 2
    # The captions follow one another without overlapping.
    for (start, width), (next_start, _w) in zip(spans, spans[1:]):
        assert start + width == next_start


# -- opening a menu ------------------------------------------------------------

def test_a_chosen_entry_is_run(app, monkeypatch):
    _script_dropdown(monkeypatch, ["reload"])
    app.open_menu(2)
    assert "Reloaded" in app.message


def test_a_cancelled_menu_runs_nothing(app, monkeypatch):
    _script_dropdown(monkeypatch, [None])
    before = app.message
    app.open_menu(1)
    assert app.message == before
    assert app._open_menu is None


def test_the_arrows_walk_to_the_neighbouring_menu(app, monkeypatch):
    opened = _script_dropdown(monkeypatch,
                              [dialogs.NEXT_MENU, dialogs.PREVIOUS_MENU, None])
    app.open_menu(1)
    # File -> Command -> File, and each opened under its own caption.
    assert [where[1] for where in opened] == [menu_layout()[i][0]
                                              for i in (1, 2, 1)]


def test_walking_off_the_end_wraps_around(app, monkeypatch):
    opened = _script_dropdown(monkeypatch, [dialogs.NEXT_MENU, None])
    app.open_menu(len(MENUS) - 1)
    assert opened[-1][1] == menu_layout()[0][0]


def test_a_menu_is_open_while_its_drop_down_is(app, monkeypatch):
    seen = []

    def fake(stdscr, items, y, x):
        seen.append(app._open_menu)
        return None

    monkeypatch.setattr(dialogs, "dropdown", fake)
    app.open_menu(3)
    assert seen == [3]
    assert app._open_menu is None


def test_the_hidden_files_entry_carries_a_tick(app):
    def hidden_entry():
        return [item for item in app._menu_items(3)
                if item.get("name") == "hidden"][0]

    app.active.show_hidden = True
    assert hidden_entry()["checked"] is True
    app.active.show_hidden = False
    assert hidden_entry()["checked"] is False


# -- the keys that open it -----------------------------------------------------

def test_escape_alone_opens_the_first_menu(app, monkeypatch):
    app.stdscr = _Screen([])              # nothing follows the Escape
    opened = _script_dropdown(monkeypatch, [None])
    app.handle_key(27)
    assert opened and opened[0][1] == menu_layout()[0][0]


@pytest.mark.parametrize("letter, index", [("f", 1), ("c", 2), ("o", 3),
                                           ("h", 4)])
def test_alt_and_a_letter_open_that_menu(app, monkeypatch, letter, index):
    app.stdscr = _Screen([ord(letter)])   # Escape, then the letter
    opened = _script_dropdown(monkeypatch, [None])
    app.handle_key(27)
    assert opened and opened[0][1] == menu_layout()[index][0]


def test_alt_and_a_letter_no_menu_wants_does_nothing(app, monkeypatch):
    app.stdscr = _Screen([ord("z")])
    opened = _script_dropdown(monkeypatch, [None])
    app.handle_key(27)
    assert not opened


def test_a_screen_that_cannot_be_asked_for_a_key_still_opens_the_menu(
        app, monkeypatch):
    """A stand-in screen with no getch() reads as a bare Escape."""
    opened = _script_dropdown(monkeypatch, [None])
    app.handle_key(27)
    assert opened


def test_a_key_the_screen_cannot_be_asked_for_reads_as_escape(app,
                                                              monkeypatch):
    class _Refuses(_StubScreen):
        def getch(self):
            raise curses.error("no input")

    app.stdscr = _Refuses()
    opened = _script_dropdown(monkeypatch, [None])
    app.handle_key(27)
    assert opened and opened[0][1] == menu_layout()[0][0]


def test_a_bar_narrower_than_its_captions_stops_drawing(app):
    """The menus that fit are drawn; the rest are dropped, not wrapped."""
    drawn: list[tuple] = []

    class _Recording(_StubScreen):
        def addstr(self, y, x, text, *args):
            drawn.append((y, x, text))

    app.stdscr = _Recording()
    app._draw_menu_bar(14)
    assert not any("Options" in text for _y, _x, text in drawn)
    assert any("File" in text for _y, _x, text in drawn)


def test_a_pane_too_small_to_frame_is_left_alone(app):
    drawn: list[tuple] = []

    class _Recording(_StubScreen):
        def addstr(self, y, x, text, *args):
            drawn.append((y, x, text))

    app.stdscr = _Recording()
    app._draw_panel(app.left, 0, 0, 3, 6, active=True)
    assert drawn == []


def test_a_click_on_the_bar_opens_the_menu_under_it(app, monkeypatch):
    start, width = menu_layout()[2]
    monkeypatch.setattr(curses, "getmouse",
                        lambda: (0, start + 1, 0, 0, curses.BUTTON1_CLICKED))
    opened = _script_dropdown(monkeypatch, [None])
    app._handle_mouse()
    assert opened and opened[0][1] == start


def test_a_click_on_the_empty_end_of_the_bar_opens_nothing(app, monkeypatch):
    monkeypatch.setattr(curses, "getmouse",
                        lambda: (0, 78, 0, 0, curses.BUTTON1_CLICKED))
    opened = _script_dropdown(monkeypatch, [None])
    app._handle_mouse()
    assert not opened


def test_alt_c_then_r_reloads_the_panes_end_to_end(app, monkeypatch, tmp_path):
    """The whole path, on a real screen: Alt+C, then Reload's own letter."""
    real_newwin = curses.newwin

    def scripted(*args, **kwargs):
        return _ScriptedWindow(real_newwin(*args, **kwargs), [ord("r")])

    def run(stdscr):
        app.stdscr = stdscr
        monkeypatch.setattr(curses, "newwin", scripted)
        # A second key is already waiting, which is what makes this Alt+C
        # rather than a bare Escape.
        monkeypatch.setattr(app, "_peek_key", lambda: ord("c"))
        app.handle_key(27)
        return "\n".join(stdscr.instr(row, 0).decode() for row in range(24))

    screen = with_curses_screen(24, 80, run)
    assert "Reloaded" in app.message
    # The bar was drawn under the drop-down while it was open.
    assert "Command" in screen.splitlines()[0]


# -- Options > Editor ----------------------------------------------------------

def test_the_editor_menu_offers_the_built_in_one_and_the_usual_suspects(
        app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    scripted = _ScriptedDialogs(monkeypatch, menu=["Cancel"])
    app._editor_menu()
    _title, options = scripted.menus[0]
    assert options[0].startswith("Built-in editor")
    assert "vim" in options
    assert "Other..." in options
    # Nothing is configured, so the built-in editor is the one marked.
    assert "(current)" in options[0]


def test_choosing_an_editor_remembers_it(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(app_mod.shutil, "which", lambda name: "/usr/bin/vim")
    _ScriptedDialogs(monkeypatch, menu=["vim"])
    app._editor_menu()
    assert config_mod.external_editor() == "vim"
    assert app.message == "Editor: vim"


def test_choosing_the_built_in_editor_puts_it_back(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert config_mod.save_editor("vim") is True
    _ScriptedDialogs(monkeypatch, menu=["Built-in editor"])
    app._editor_menu()
    assert config_mod.external_editor() == ""
    assert app.message == "Editor: built-in editor"


def test_an_editor_not_on_the_path_is_saved_but_flagged(app, monkeypatch,
                                                        tmp_path):
    """Saved anyway -- it may be installed later, or live on another PATH --
    but silently pointing F4 at a missing program would be worse."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(app_mod.shutil, "which", lambda name: None)
    _ScriptedDialogs(monkeypatch, menu=["nano"])
    app._editor_menu()
    assert config_mod.external_editor() == "nano"
    assert "nano is not on your PATH" in app.message


def test_other_takes_any_command_line(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(app_mod.shutil, "which", lambda name: "/usr/bin/emacs")
    _ScriptedDialogs(monkeypatch, menu=["Other..."], prompt=["  emacs -nw  "])
    app._editor_menu()
    assert config_mod.external_editor() == "emacs -nw"


def test_other_offers_the_current_command_to_edit(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert config_mod.save_editor("vim") is True
    defaults = []
    _ScriptedDialogs(monkeypatch, menu=["Other..."])
    # After the scripted set, so this is the prompt the menu actually calls.
    monkeypatch.setattr(
        dialogs, "prompt",
        lambda stdscr, title, label, default="", is_password=False:
            defaults.append(default) or None)
    app._editor_menu()
    assert defaults == ["vim"]
    assert config_mod.external_editor() == "vim"


def test_a_command_that_cannot_work_is_refused(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    scripted = _ScriptedDialogs(monkeypatch, menu=["Other..."],
                                prompt=["vim 'unclosed"])
    app._editor_menu()
    assert "not a usable command" in scripted.messages[0][1]
    assert config_mod.external_editor() == ""


def test_the_editor_menu_can_be_cancelled(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _ScriptedDialogs(monkeypatch, menu=[None])
    app._editor_menu()
    assert config_mod.external_editor() == ""
    _ScriptedDialogs(monkeypatch, menu=["Cancel"])
    app._editor_menu()
    assert config_mod.external_editor() == ""


def test_an_editor_that_cannot_be_saved_is_still_reported(app, monkeypatch,
                                                          tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(app_mod.shutil, "which", lambda name: "/usr/bin/vim")
    monkeypatch.setattr(config_mod, "save_editor", lambda command: False)
    _ScriptedDialogs(monkeypatch, menu=["vim"])
    app._editor_menu()
    assert "could not be saved" in app.message


# -- Options > Viewer ----------------------------------------------------------

def test_the_viewer_menu_offers_the_built_in_one_and_the_usual_pagers(
        app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    scripted = _ScriptedDialogs(monkeypatch, menu=["Cancel"])
    app._viewer_menu()
    title, options = scripted.menus[0]
    assert title == "Viewer"
    assert options[0].startswith("Built-in viewer")
    assert "less" in options
    assert "Other..." in options
    assert "(current)" in options[0]


def test_choosing_a_viewer_remembers_it(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(app_mod.shutil, "which", lambda name: "/usr/bin/less")
    _ScriptedDialogs(monkeypatch, menu=["less -R"])
    app._viewer_menu()
    assert config_mod.external_viewer() == "less -R"
    assert app.message == "Viewer: less -R"


def test_choosing_the_built_in_viewer_puts_it_back(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert config_mod.save_viewer("less") is True
    _ScriptedDialogs(monkeypatch, menu=["Built-in viewer"])
    app._viewer_menu()
    assert config_mod.external_viewer() == ""
    assert app.message == "Viewer: built-in viewer"


def test_a_pager_not_on_the_path_is_saved_but_flagged(app, monkeypatch,
                                                      tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(app_mod.shutil, "which", lambda name: None)
    _ScriptedDialogs(monkeypatch, menu=["more"])
    app._viewer_menu()
    assert config_mod.external_viewer() == "more"
    assert "more is not on your PATH" in app.message


def test_other_takes_any_pager_command_line(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(app_mod.shutil, "which", lambda name: "/usr/bin/bat")
    _ScriptedDialogs(monkeypatch, menu=["Other..."],
                     prompt=["  bat --paging=always  "])
    app._viewer_menu()
    assert config_mod.external_viewer() == "bat --paging=always"


def test_a_pager_command_that_cannot_work_is_refused(app, monkeypatch,
                                                     tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    scripted = _ScriptedDialogs(monkeypatch, menu=["Other..."],
                                prompt=["less 'unclosed"])
    app._viewer_menu()
    assert "not a usable command" in scripted.messages[0][1]
    assert config_mod.external_viewer() == ""


def test_the_viewer_menu_can_be_cancelled(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _ScriptedDialogs(monkeypatch, menu=[None])
    app._viewer_menu()
    _ScriptedDialogs(monkeypatch, menu=["Cancel"])
    app._viewer_menu()
    assert config_mod.external_viewer() == ""


def test_the_two_menus_do_not_write_over_each_other(app, monkeypatch,
                                                    tmp_path):
    """One routine behind both, so this is worth pinning: they are two keys."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(app_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    _ScriptedDialogs(monkeypatch, menu=["vim"])
    app._editor_menu()
    _ScriptedDialogs(monkeypatch, menu=["less"])
    app._viewer_menu()
    assert config_mod.external_editor() == "vim"
    assert config_mod.external_viewer() == "less"


# -- Options > Colours ---------------------------------------------------------

def test_the_colour_menu_switches_and_remembers_the_scheme(app, monkeypatch,
                                                           tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    scripted = _ScriptedDialogs(monkeypatch, menu=["midnight"])
    try:
        app._colour_menu()
        assert theme.current == "midnight"
        assert config_mod.colour_scheme() == "midnight"
    finally:
        theme.init("turbo")
    assert scripted.menus[0][0] == "Colours"


def test_the_colour_menu_can_be_cancelled(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _ScriptedDialogs(monkeypatch, menu=[None])
    app._colour_menu()
    assert theme.current == "turbo"


def test_a_scheme_that_cannot_be_saved_is_still_applied(app, monkeypatch,
                                                        tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(config_mod, "save_scheme", lambda name: False)
    _ScriptedDialogs(monkeypatch, menu=["midnight"])
    try:
        app._colour_menu()
        assert theme.current == "midnight"
        assert "could not be saved" in app.message
    finally:
        theme.init("turbo")


def test_about_says_which_version_it_is(app, monkeypatch):
    from meridian_commander import __version__

    scripted = _ScriptedDialogs(monkeypatch)
    app._about()
    assert __version__ in scripted.last_message
