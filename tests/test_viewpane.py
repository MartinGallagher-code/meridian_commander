# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""F3 and F4 in a pane: ``[ui] open_in = pane`` (MC-025).

With the setting on, the viewer or the editor opens in a pane, alone at full
width; ``z`` (in a viewer) or ``Ctrl-]`` ``z`` switches to two panes with the
listing the file came from beside it, ``Ctrl-]`` ``Tab`` gives the listing the
keyboard, and closing puts the layout and the keyboard back.  The rules held
throughout are the ticket's: the view keeps its place through every switch,
nothing that worked full-screen changes with the setting off, and unsaved
edits are never lost without asking.
"""

from __future__ import annotations

import curses
import os
import time

import pytest

from meridian_commander import config, extedit, viewpane
from meridian_commander.app import MENUS, App
from meridian_commander.dcview import FloorView
from meridian_commander.docx import DocxView
from meridian_commander.editor import Editor
from meridian_commander.imageview import ImageView
from meridian_commander.markdown import MarkdownView
from meridian_commander.pdf import PdfView
from meridian_commander.plugin_api import PanePlugin, PluginContext
from meridian_commander.plugins.terminal import MENU_KEY
from meridian_commander.pptx import SlideView
from meridian_commander.sheetview import SheetView
from meridian_commander.tableview import TableView
from meridian_commander.viewer import Viewer
from meridian_commander.viewpane import ProgramPane, ViewPane

from dc_examples import EXAMPLES
from support import (
    _ScriptedDialogs,
    _StubScreen,
    docx_p,
    pdf_bytes,
    pdf_page_objects,
    pptx_p,
    pptx_shape,
    read,
    rgb_png,
    slide_xml,
    text_content,
    with_curses_screen,
    write,
    write_docx,
    write_pptx,
    write_xlsx,
)

ESC, TAB, ENTER = 27, 9, 10
CTRL_Q = 17


@pytest.fixture
def files(tmp_path):
    left = tmp_path / "left"
    write(str(left / "notes.txt"),
          "".join(f"line {n}\n" for n in range(60)))
    write(str(left / "second.txt"), "second file\n")
    write(str(left / "sub" / "inner.txt"), "inside\n")
    write(str(tmp_path / "right" / "theirs.txt"), "right\n")
    return tmp_path


@pytest.fixture
def app(files, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))
    # A stub screen has no curses behind it, and F3 and F4 hide the cursor
    # through the library on their way out.
    monkeypatch.setattr(curses, "curs_set", lambda visibility: 0)
    app = App(_StubScreen(), str(files / "left"), str(files / "right"))
    app.open_in_pane = True
    app.use_trash = False
    return app


@pytest.fixture(autouse=True)
def _quiet_progress(monkeypatch):
    """The progress window needs a real screen and adds nothing here."""
    from meridian_commander import dialogs

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


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def _view(app, name="notes.txt") -> ViewPane:
    """F3 on ``name`` in the left pane; the pane it opened."""
    _point_at(app.left, name)
    app.handle_key(curses.KEY_F3)
    pane = app.active.plugin
    assert isinstance(pane, ViewPane)
    return pane


def _edit(app, name="notes.txt") -> ViewPane:
    _point_at(app.left, name)
    app.handle_key(curses.KEY_F4)
    pane = app.active.plugin
    assert isinstance(pane, ViewPane) and pane.editing
    return pane


def _type(app, text):
    for ch in text:
        app.handle_key(ord(ch))


# -- the setting -----------------------------------------------------------------

def _write_config(files, text):
    path = files / "config" / "meridian-commander" / "config.ini"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize("value, pane", [
    (None, False), ("pane", True), (" pane ", True), ("screen", False),
    ("panes", False), ("", False),
])
def test_the_setting_is_read_from_the_config_file(files, monkeypatch, value,
                                                  pane):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))
    _write_config(files, "[ui]\n" + ("" if value is None
                                     else f"open_in = {value}\n"))
    assert config.open_in_pane() is pane


def test_the_app_starts_with_the_setting_from_the_file(files, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))
    _write_config(files, "[ui]\nopen_in = pane\n")
    assert App(_StubScreen(), str(files / "left")).open_in_pane is True
    _write_config(files, "[ui]\n")
    assert App(_StubScreen(), str(files / "left")).open_in_pane is False


def test_the_options_entry_is_ticked_and_saves_the_choice(app, files):
    options = next(i for i, m in enumerate(MENUS) if m["name"] == "options")

    def ticked():
        entry = next(e for e in app._menu_items(options)
                     if e.get("name") == "open_in_pane")
        assert "pane" in entry["label"]
        return entry["checked"]

    saved = files / "config" / "meridian-commander" / "config.ini"
    app.open_in_pane = False
    assert ticked() is False
    app._dispatch("open_in_pane")
    assert app.open_in_pane is True and ticked() is True
    assert app.message == "F3 and F4 open in a pane"
    assert "open_in = pane" in read(str(saved))
    app._dispatch("open_in_pane")
    assert app.open_in_pane is False and ticked() is False
    assert app.message == "F3 and F4 open full-screen"
    assert "open_in = screen" in read(str(saved))
    assert config.open_in_pane() is False


def test_saving_the_same_choice_writes_nothing(files, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))
    assert config.save_open_in(False) is True
    assert not (files / "config" / "meridian-commander" / "config.ini").exists()


def test_a_setting_that_cannot_be_saved_says_so(app, monkeypatch):
    monkeypatch.setattr(config, "_save_ui", lambda key, value: False)
    app.open_in_pane = False
    app._toggle_open_in_pane()
    assert app.message.endswith("(could not be saved)")


# -- with the setting off, nothing changes ------------------------------------------

def test_with_the_setting_off_f3_and_f4_run_full_screen(app, monkeypatch):
    app.open_in_pane = False
    ran = []
    monkeypatch.setattr(Viewer, "run", lambda self, stdscr: ran.append("view"))
    monkeypatch.setattr(Editor, "run", lambda self, stdscr: ran.append("edit"))
    _point_at(app.left, "notes.txt")
    app.handle_key(curses.KEY_F3)
    app.handle_key(curses.KEY_F4)
    assert ran == ["view", "edit"]
    assert app.left.plugin is None and app.right.plugin is None
    assert app.single is False and app.active is app.left


def test_with_the_setting_off_an_outside_pager_has_the_terminal(app,
                                                                monkeypatch):
    app.open_in_pane = False
    monkeypatch.setattr(config, "external_viewer", lambda: "less")
    calls = []

    def suspend(cmd, cwd, banner, **kwargs):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(app, "_suspend_and_run", suspend)
    _point_at(app.left, "notes.txt")
    app.handle_key(curses.KEY_F3)
    assert calls and calls[0][0] == "less"
    assert app.right.plugin is None


# -- opening -------------------------------------------------------------------------

def test_f3_opens_the_viewer_alone_in_the_other_pane(app):
    pane = _view(app)
    assert app.right.plugin is pane and app.active is app.right
    assert isinstance(pane.view, Viewer) and pane.view.name == "notes.txt"
    assert app._panes_on_screen() == [app.right]
    assert pane.source is app.left
    assert app.message == "z: two panes  Ctrl-] Tab: listing"


def test_f4_opens_the_editor_the_same_way(app):
    pane = _edit(app)
    assert app.right.plugin is pane and app._panes_on_screen() == [app.right]
    assert pane.view.lines[0] == "line 0"
    assert app.message == "Ctrl-] then z: two panes, Tab: listing"


def test_f4_on_a_new_name_opens_an_empty_file_in_the_editor(app, monkeypatch,
                                                            files):
    _ScriptedDialogs(monkeypatch, prompt=["fresh.txt"])
    app.left.entries = []                         # nothing to edit: a new one
    app.handle_key(curses.KEY_F4)
    pane = app.right.plugin
    assert isinstance(pane, ViewPane) and pane.view.name == "fresh.txt"
    _type(app, "hi")
    app.handle_key(19)                            # Ctrl-S
    assert read(str(files / "left" / "fresh.txt")) == "hi"
    # The listing beside it shows what was saved.
    assert "fresh.txt" in [e.name for e in app.left.entries]


def test_a_directory_is_entered_not_viewed(app):
    _point_at(app.left, "sub")
    app.handle_key(curses.KEY_F3)
    assert app.right.plugin is None
    assert app.left.path.endswith("sub")


def test_a_busy_other_pane_has_the_view_take_this_one(app):
    class _Busy(PanePlugin):
        name = "busy"
        uses_other_pane = False

        def draw(self, *args):
            pass

        def handle_key(self, key):
            return True

    app.right.plugin = busy = _Busy(PluginContext(app, app.right, app.left))
    pane = _view(app)
    assert app.left.plugin is pane and app.right.plugin is busy
    app.handle_key(ord("q"))
    assert app.left.plugin is None and app.active is app.left
    assert app.right.plugin is busy


def test_with_both_panes_busy_it_opens_full_screen(app, monkeypatch):
    ran = []
    monkeypatch.setattr(Viewer, "run", lambda self, stdscr: ran.append(1))
    _point_at(app.left, "notes.txt")
    app.right.plugin = object()
    app.left.plugin = object()
    app._view()
    assert ran == [1]


def test_a_viewer_that_cannot_open_says_why(app, monkeypatch):
    seen = _ScriptedDialogs(monkeypatch)

    def broken(fs, path):
        raise ValueError("no such thing")

    monkeypatch.setattr("meridian_commander.app.viewer_for", broken)
    _point_at(app.left, "notes.txt")
    app.handle_key(curses.KEY_F3)
    assert "no such thing" in seen.last_message
    assert app.right.plugin is None


def test_the_file_menu_reaches_it_too(app):
    _point_at(app.left, "notes.txt")
    app._dispatch("view")
    assert isinstance(app.right.plugin, ViewPane)


# -- one pane and two -------------------------------------------------------------

def test_z_in_a_viewer_shows_two_panes_and_back(app):
    pane = _view(app)
    app.handle_key(ord("z"))
    assert app._panes_on_screen() == [app.left, app.right]
    assert app.active is app.right                 # the keys stay in the view
    app.handle_key(ord("Z"))
    assert app._panes_on_screen() == [app.right]
    assert app.right.plugin is pane


def test_the_editor_types_z_and_switches_with_ctrl_bracket(app):
    pane = _edit(app)
    app.handle_key(ord("z"))
    assert pane.view.lines[0] == "zline 0"
    assert app._panes_on_screen() == [app.right]
    app.handle_key(MENU_KEY)
    assert pane.menu is True
    app.handle_key(ord("z"))
    assert pane.menu is False
    assert app._panes_on_screen() == [app.left, app.right]


def test_ctrl_bracket_tab_gives_the_listing_the_keyboard(app):
    pane = _view(app)
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)
    assert app.active is app.left
    # One pane on screen: the view stays in sight beside the listing.
    assert app._panes_on_screen() == [app.left, app.right]
    app.handle_key(curses.KEY_DOWN)              # the listing moves
    assert app.left.current().name != "notes.txt"
    app.handle_key(TAB)                          # and Tab comes back
    assert app.active is app.right and app.right.plugin is pane
    assert app._panes_on_screen() == [app.right]


def test_any_other_key_after_ctrl_bracket_only_closes_the_menu(app):
    pane = _view(app)
    app.handle_key(MENU_KEY)
    app.handle_key(ESC)
    assert pane.menu is False and app.right.plugin is pane


def test_the_view_keeps_its_place_through_every_switch(app):
    pane = _view(app)
    pane.size = (20, 80)
    app.handle_key(curses.KEY_NPAGE)
    top = pane.view.top
    assert top > 0
    for key in (ord("z"), MENU_KEY, TAB, TAB, ord("z")):
        app.handle_key(key)
    assert app.right.plugin is pane and pane.view.top == top


def test_the_editor_keeps_its_text_and_cursor_through_every_switch(app):
    pane = _edit(app)
    app.handle_key(curses.KEY_DOWN)
    _type(app, "abc")
    for key in (MENU_KEY, ord("z"), MENU_KEY, TAB, TAB, MENU_KEY, ord("z")):
        app.handle_key(key)
    assert pane.view.lines[1] == "abcline 1"
    assert (pane.view.cy, pane.view.cx) == (1, 3)
    assert pane.view.dirty is True


def test_a_mouse_report_goes_to_the_app(app):
    pane = _view(app)
    pane.menu = True
    assert pane.handle_key(curses.KEY_MOUSE) is None
    assert pane.menu is False


# -- the next file -----------------------------------------------------------------

def test_f3_on_the_next_file_shows_it_in_the_same_view(app):
    _view(app)
    app.handle_key(ord("z"))                     # two panes
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)                          # the keyboard in the listing
    _point_at(app.left, "second.txt")
    app.handle_key(curses.KEY_F3)
    pane = app.right.plugin
    assert pane.view.name == "second.txt" and app.active is app.right
    # Two panes stay two ...
    assert app._panes_on_screen() == [app.left, app.right]
    # ... and closing goes back to what there was before the first F3.
    app.handle_key(ord("q"))
    assert app.single is False and app.right.plugin is None
    assert app.active is app.left


def test_a_changed_file_in_the_editor_is_asked_about_before_it_is_replaced(
        app, monkeypatch):
    seen = _ScriptedDialogs(monkeypatch, confirm=[False, True])
    pane = _edit(app)
    _type(app, "x")
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)
    _point_at(app.left, "second.txt")
    app.handle_key(curses.KEY_F3)                # "n": keep the edit
    assert app.right.plugin is pane and pane.view.lines[0] == "xline 0"
    assert "unsaved changes" in seen.last_message
    app.handle_key(TAB)
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)
    app.handle_key(curses.KEY_F3)                # "y": discard it
    assert app.right.plugin.view.name == "second.txt"


# -- closing -----------------------------------------------------------------------

@pytest.mark.parametrize("key", [ord("q"), ESC, curses.KEY_F3,
                                 curses.KEY_F10])
def test_closing_the_viewer_gives_back_the_listing_and_the_layout(app, files,
                                                                  key):
    _view(app)
    app.handle_key(key)
    assert app.right.plugin is None
    assert app.right.path == str(files / "right")
    assert app.single is False and app.active is app.left
    assert app.message == "Closed notes.txt"


def test_ctrl_bracket_q_closes_it_too(app):
    _view(app)
    app.handle_key(MENU_KEY)
    app.handle_key(ord("q"))
    assert app.right.plugin is None and app.active is app.left


def test_closing_from_two_panes_gives_back_two_panes(app):
    _view(app)
    app.handle_key(ord("z"))
    app.handle_key(ord("z"))
    app.handle_key(ord("z"))
    app.handle_key(ord("q"))
    assert app.single is False and app._panes_on_screen() == [app.left,
                                                              app.right]


def test_one_pane_turned_on_with_z_before_f3_stays_on(app):
    app.handle_key(ord("z"))
    assert app.single is True
    _view(app)
    app.handle_key(ord("z"))                     # two, then close
    app.handle_key(ord("q"))
    assert app.single is True and app._panes_on_screen() == [app.left]
    assert app.active is app.left


def test_the_editor_closes_on_f10_and_ctrl_q_when_saved(app):
    _edit(app)
    app.handle_key(curses.KEY_F10)
    assert app.right.plugin is None and app.active is app.left
    _edit(app)
    app.handle_key(CTRL_Q)
    assert app.right.plugin is None


@pytest.mark.parametrize("close", [[curses.KEY_F10], [CTRL_Q],
                                   [MENU_KEY, ord("q")]])
def test_unsaved_edits_are_asked_about_when_the_editor_closes(app, files,
                                                              monkeypatch,
                                                              close):
    seen = _ScriptedDialogs(monkeypatch, confirm=[False, True])
    pane = _edit(app)
    _type(app, "x")
    for key in close:
        app.handle_key(key)
    assert app.right.plugin is pane                # "n" keeps it
    assert seen.messages[-1][0] == "Unsaved changes"
    for key in close:
        app.handle_key(key)
    assert app.right.plugin is None                # "y" lets it go
    assert read(str(files / "left" / "notes.txt")).startswith("line 0")


def test_a_saved_edit_reaches_the_file_and_the_listing(app, files):
    _edit(app, "second.txt")
    _type(app, "more ")
    app.handle_key(curses.KEY_F2)
    assert read(str(files / "left" / "second.txt")) == "more second file\n"
    entry = next(e for e in app.left.entries if e.name == "second.txt")
    assert entry.size == len("more second file\n")
    app.handle_key(curses.KEY_F10)
    assert app.right.plugin is None


# -- unsaved edits and the rest of the application -----------------------------

@pytest.fixture
def dirty(app):
    """An editor in the right pane with a change not saved, keys on the left."""
    pane = _edit(app)
    _type(app, "x")
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)
    assert app.active is app.left
    return pane


def test_quitting_asks_about_unsaved_edits_first(app, dirty, monkeypatch):
    seen = _ScriptedDialogs(monkeypatch, confirm=[False, True, True])
    app.handle_key(curses.KEY_F10)
    assert app.running is True and seen.messages[-1][0] == "Unsaved changes"
    app.handle_key(curses.KEY_F10)                 # discard, then quit
    assert app.running is False
    assert [title for title, _, _ in seen.messages] == [
        "Unsaved changes", "Unsaved changes", "Quit"]


def test_the_menus_exit_asks_too(app, dirty, monkeypatch):
    _ScriptedDialogs(monkeypatch, confirm=[False])
    app._dispatch("quit")
    assert app.running is True


def test_ctrl_c_asks_about_unsaved_edits(files, monkeypatch):
    """Ctrl-C that arrives as an interrupt rather than as a key."""
    from test_app_draw import _LoopScreen

    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda visibility: 0)
    screen = _LoopScreen([KeyboardInterrupt(), KeyboardInterrupt()])
    app = App(screen, str(files / "left"), str(files / "right"))
    app.right.plugin = _Unsaved(PluginContext(app, app.right, app.left))
    seen = _ScriptedDialogs(monkeypatch, confirm=[False, True, True])
    app.run()
    assert [title for title, _, _ in seen.messages] == [
        "kept?", "kept?", "Quit"]


class _Unsaved(PanePlugin):
    """A pane that asks before it is closed."""

    name = "unsaved"
    uses_other_pane = False

    def draw(self, *args):
        pass

    def handle_key(self, key):
        return True

    def may_close(self):
        from meridian_commander import dialogs
        return dialogs.confirm(None, "kept?", "close it?")


def test_a_plug_in_says_yes_unless_it_asks():
    assert PanePlugin.may_close(object()) is True


@pytest.mark.parametrize("action", ["_open_terminal_pane", "_peek"])
def test_nothing_is_opened_over_unsaved_edits_without_asking(app, dirty,
                                                             monkeypatch,
                                                             action):
    _ScriptedDialogs(monkeypatch, confirm=[False])
    app.active = app.right
    getattr(app, action)()
    assert app.right.plugin is dirty


def test_a_plug_in_is_not_opened_over_unsaved_edits_without_asking(
        app, dirty, monkeypatch):
    _ScriptedDialogs(monkeypatch, menu=[0], confirm=[False])
    app.active = app.right
    app._plugin_mode()
    assert app.right.plugin is dirty


@pytest.mark.parametrize("action", ["_delete", "_move", "_rename"])
def test_the_file_open_with_unsaved_edits_is_not_removed_from_under_it(
        app, dirty, monkeypatch, files, action):
    seen = _ScriptedDialogs(monkeypatch, prompt=["renamed.txt"])
    _point_at(app.left, "notes.txt")
    getattr(app, action)()
    assert seen.messages[-1][0] == "Open in the editor"
    assert "notes.txt is open in a pane with unsaved changes" in \
        seen.last_message
    assert (files / "left" / "notes.txt").exists()


def test_nor_is_its_directory_deleted(app, monkeypatch, files):
    _point_at(app.left, "sub")
    app.handle_key(curses.KEY_RIGHT)              # into sub
    _edit(app, "inner.txt")
    _type(app, "x")
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)
    app.left.go_parent()
    _point_at(app.left, "sub")
    seen = _ScriptedDialogs(monkeypatch)
    app._delete()
    assert "inner.txt is open" in seen.last_message
    assert (files / "left" / "sub" / "inner.txt").exists()


def test_nor_is_another_file_copied_over_it(app, dirty, monkeypatch, files):
    write(str(files / "left" / "sub" / "notes.txt"), "another\n")
    _point_at(app.left, "sub")
    app.handle_key(curses.KEY_RIGHT)
    seen = _ScriptedDialogs(monkeypatch, prompt=[str(files / "left")] * 2)
    _point_at(app.left, "notes.txt")
    app._copy()                                   # onto the open file
    assert seen.messages[-1][0] == "Open in the editor"
    assert read(str(files / "left" / "notes.txt")).startswith("line 0")
    _point_at(app.left, "inner.txt")
    app._copy()                                   # beside it: goes ahead
    assert read(str(files / "left" / "inner.txt")) == "inside\n"
    assert seen.messages[-1][0] == "Open in the editor"   # nothing new


def test_a_saved_file_can_be_deleted_from_beside_the_editor(app, monkeypatch,
                                                            files):
    _edit(app)
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)
    _ScriptedDialogs(monkeypatch, confirm=[True])
    _point_at(app.left, "notes.txt")
    app._delete()
    assert not (files / "left" / "notes.txt").exists()


def test_another_file_beside_unsaved_edits_can_be_deleted(app, dirty,
                                                          monkeypatch, files):
    _ScriptedDialogs(monkeypatch, confirm=[True])
    _point_at(app.left, "second.txt")
    app._delete()
    assert not (files / "left" / "second.txt").exists()


# -- every viewer, in a pane ----------------------------------------------------------

def _pdf(path):
    data = pdf_bytes(pdf_page_objects(
        text_content("Chapter one", "The opening line."), pages=2))
    with open(path, "wb") as handle:
        handle.write(data)


VIEWERS = {
    "notes.md": (MarkdownView, ord("r"), lambda v: v.show_source),
    "report.docx": (DocxView, ord("w"), lambda v: v.wrap is False),
    "deck.pptx": (SlideView, TAB, lambda v: v.index == 1),
    "book.pdf": (PdfView, TAB, lambda v: v.index == 1),
    "pic.png": (ImageView, ord("+"), lambda v: v.zoom == 1),
    "book.xlsx": (SheetView, TAB, lambda v: v.sheet_index == 1),
    "runs.tsv": (TableView, ord("s"), lambda v: list(v.order) == [2, 1]),
    "floor.dc": (FloorView, ENTER, lambda v: v.focus is not v.layout.root),
}


@pytest.fixture
def viewers(files):
    left = files / "left"
    write(str(left / "notes.md"), "# Title\n\nSome *text*.\n")
    write_docx(str(left / "report.docx"), docx_p("A paragraph of prose."))
    write_pptx(str(left / "deck.pptx"),
               [slide_xml(pptx_shape(pptx_p("One"), "title")),
                slide_xml(pptx_shape(pptx_p("Two"), "title"))])
    _pdf(str(left / "book.pdf"))
    (left / "pic.png").write_bytes(rgb_png(4, 4, [(255, 0, 0)] * 16))
    write_xlsx(str(left / "book.xlsx"), {"One": [["a"]], "Two": [["b"]]})
    write(str(left / "runs.tsv"), "run\tms\n2\t30\n1\t20\n")
    write(str(left / "floor.dc"), EXAMPLES["small"][0])
    return files


@pytest.mark.parametrize("name", sorted(VIEWERS))
def test_every_viewer_opens_in_a_pane_with_its_own_keys(app, viewers, name):
    kind, key, changed = VIEWERS[name]
    app.left.refresh()
    pane = _view(app, name)
    assert isinstance(pane.view, kind)
    app.handle_key(key)
    assert changed(pane.view), name
    assert app.right.plugin is pane
    app.handle_key(ord("z"))                     # free in every one of them
    assert app._panes_on_screen() == [app.left, app.right]
    app.handle_key(ord("q"))
    assert app.right.plugin is None and app.active is app.left


@pytest.mark.parametrize("name", ["notes.txt", "notes.md", "deck.pptx",
                                  "book.pdf", "book.xlsx", "runs.tsv"])
def test_a_viewers_search_prompt_opens_and_finds(app, viewers, monkeypatch,
                                                  name):
    seen = _ScriptedDialogs(monkeypatch, prompt=["o"])
    app.left.refresh()
    pane = _view(app, name)
    app.handle_key(ord("/"))
    assert seen.prompts and pane.view.search == "o"
    assert app.right.plugin is pane


def test_a_picture_in_a_pane_is_drawn_in_half_blocks(app, viewers,
                                                     monkeypatch):
    app.left.refresh()
    pane = _view(app, "pic.png")
    assert pane.view.using_graphics() is False
    shown = []
    monkeypatch.setattr(ImageView, "run",
                        lambda self, stdscr: shown.append(self.graphics))
    monkeypatch.setattr(curses, "curs_set", lambda visibility: 0)
    for key in (ENTER, ord("g")):
        app.handle_key(key)
    # Full-screen, with whatever pixels the terminal can draw; the pane's
    # own picture is untouched.
    assert len(shown) == 2
    assert pane.view.graphics is False and app.right.plugin is pane


# -- an outside pager or editor, in a pane ------------------------------------------

def _wait_closed(app, panel, seconds=10.0):
    end = time.monotonic() + seconds
    while panel.plugin is not None and time.monotonic() < end:
        app._tick_plugins()
        time.sleep(0.01)
    return panel.plugin is None


def _wait_done(pane, seconds=10.0):
    end = time.monotonic() + seconds
    while not pane.done and time.monotonic() < end:
        pane.tick()
        time.sleep(0.01)
    return pane.done


@pytest.fixture
def outside(app, monkeypatch):
    """Point Options > Viewer and Editor at a command line, for a test."""
    def use(viewer=None, editor=None):
        monkeypatch.setattr(config, "external_viewer", lambda: viewer or "")
        monkeypatch.setattr(config, "external_editor", lambda: editor or "")
    return use


def test_an_outside_pager_runs_in_a_pane_and_closes_when_it_exits(app,
                                                                  outside):
    outside(viewer="sh -c 'cat \"$0\"; read line'")
    _point_at(app.left, "second.txt")
    app.handle_key(curses.KEY_F3)
    pane = app.right.plugin
    assert isinstance(pane, ProgramPane)
    assert app._panes_on_screen() == [app.right]
    assert pane._caption() == " [sh] second.txt "
    assert app.message == "Ctrl-] then z: two panes, Tab: listing"
    end = time.monotonic() + 10
    while "second file" not in "".join(
            line.text() for line in pane.screen.lines):
        assert time.monotonic() < end
        pane.tick()
        time.sleep(0.01)
    assert pane.may_close() is True              # a pager asks nothing
    assert pane.unsaved_file() is None
    app.handle_key(MENU_KEY)                     # the terminal's own z
    app.handle_key(ord("z"))
    assert app._panes_on_screen() == [app.left, app.right]
    app.handle_key(ENTER)                        # the program reads a line
    assert _wait_closed(app, app.right)
    assert app.message == "Viewed second.txt"
    assert app.single is False and app.active is app.left


def test_an_outside_editor_runs_in_a_pane_and_the_listing_follows(app, files,
                                                                  outside):
    outside(editor="sh -c 'printf more >> \"$0\"'")
    _point_at(app.left, "second.txt")
    app.handle_key(curses.KEY_F4)
    assert isinstance(app.right.plugin, ProgramPane)
    assert app.right.plugin.editing is True
    assert _wait_closed(app, app.right)
    assert app.message == "Edited second.txt"
    assert read(str(files / "left" / "second.txt")) == "second file\nmore"
    entry = next(e for e in app.left.entries if e.name == "second.txt")
    assert entry.size == len("second file\nmore")


def test_a_program_that_will_not_start_stays_to_say_so(app, outside):
    outside(editor="no-such-editor-for-meridian")
    _point_at(app.left, "second.txt")
    app.handle_key(curses.KEY_F4)
    pane = app.right.plugin
    assert _wait_done(pane)
    assert app.right.plugin is pane              # left up to be read
    assert "Could not start no-such-editor-for-meridian" in pane.status
    app.handle_key(ord("x"))                     # any key closes it
    assert app.right.plugin is None
    assert app.message == "Could not start no-such-editor-for-meridian"


def test_a_program_that_fails_stays_to_say_so(app, outside):
    outside(viewer="sh -c 'exit 3'")
    _point_at(app.left, "second.txt")
    app.handle_key(curses.KEY_F3)
    pane = app.right.plugin
    assert _wait_done(pane)
    assert pane.status == "[sh exited 3 -- any key closes]"


def test_a_running_outside_editor_asks_before_it_is_closed(app, outside,
                                                           monkeypatch, files):
    outside(editor="sh -c 'read line'")
    seen = _ScriptedDialogs(monkeypatch, confirm=[False, True])
    _point_at(app.left, "second.txt")
    app.handle_key(curses.KEY_F4)
    pane = app.right.plugin
    assert pane.unsaved_file() == (app.left.fs, str(files / "left" /
                                                    "second.txt"))
    app.handle_key(MENU_KEY)
    app.handle_key(ord("q"))                     # "n": it keeps running
    assert app.right.plugin is pane and pane.menu is False
    assert seen.messages[-1][0] == "Still editing"
    app.handle_key(MENU_KEY)
    app.handle_key(ord("q"))                     # "y": it goes
    assert app.right.plugin is None
    assert app.message == "Edited second.txt"


def test_ctrl_bracket_tab_and_back_reach_the_running_program(app, outside):
    outside(viewer="sh -c 'read line'")
    _point_at(app.left, "second.txt")
    app.handle_key(curses.KEY_F3)
    pane = app.right.plugin
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)
    assert app.active is app.left
    app.handle_key(TAB)
    assert app.active is app.right
    app.handle_key(MENU_KEY)
    app.handle_key(ord("q"))
    assert app.right.plugin is None and pane.done


def test_a_remote_file_is_edited_on_a_copy_and_written_back(app, outside):
    from test_extedit import _Remote

    remote = _Remote(b"old\n")
    outside(editor="sh -c 'printf new > \"$0\"'")
    app.left.fs = remote
    assert app._program_in_pane(remote, "/srv/conf.txt",
                                ["sh", "-c", 'printf new > "$0"'], True)
    pane = app.right.plugin
    copy = pane.session.copy
    assert copy and os.path.dirname(copy) == pane.cwd
    assert _wait_done(pane)
    assert remote.written == b"new"
    assert pane.closed_note == "Saved conf.txt (3 bytes)"
    assert not os.path.exists(copy)


def test_a_remote_file_that_cannot_be_fetched_opens_nothing(app, outside):
    from test_extedit import _Remote

    class _Unreadable(_Remote):
        def open_read(self, path):
            raise OSError("connection reset")

    assert app._program_in_pane(_Unreadable(), "/srv/x.txt", ["less"], False)
    assert app.right.plugin is None and app.single is False
    assert app.message == "Cannot read x.txt: connection reset"


def test_one_that_cannot_be_fetched_in_place_of_a_view_puts_back_the_layout(
        app):
    from test_extedit import _Remote

    class _Unreadable(_Remote):
        def open_read(self, path):
            raise OSError("gone")

    _view(app)
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)                          # the keys in the listing
    assert app._program_in_pane(_Unreadable(), "/srv/x.txt", ["less"], False)
    assert app.right.plugin is None
    assert app.single is False and app.active is app.left


def test_a_program_that_cannot_be_forked_says_so(app, monkeypatch):
    seen = _ScriptedDialogs(monkeypatch)
    import pty

    def refuse():
        raise OSError("out of ptys")

    monkeypatch.setattr(pty, "fork", refuse)
    path = str(app.left.path) + "/second.txt"
    assert app._program_in_pane(app.left.fs, path, ["less"], False) is True
    assert "Could not start less: out of ptys" in seen.last_message
    assert app.right.plugin is None


def test_a_program_pane_closed_early_still_writes_back(app):
    from test_extedit import _Remote

    remote = _Remote(b"old\n")
    app._program_in_pane(remote, "/srv/c.txt", ["sh", "-c", "read line"],
                         True)
    pane = app.right.plugin
    with open(pane.session.copy, "wb") as handle:
        handle.write(b"saved by the editor")
    app._close_plugin(app.right)
    assert remote.written == b"saved by the editor"
    assert pane.closed_note == "Saved c.txt (19 bytes)"


def test_the_exit_status_is_read_however_the_end_is_noticed(app, monkeypatch):
    app._program_in_pane(app.left.fs, str(app.left.path) + "/second.txt",
                         ["sh", "-c", "read line"], False)
    pane = app.right.plugin
    pid = pane._pid
    # Already reaped by tick: the status it kept is the answer.
    pane._pid, pane.exit_status = None, 4
    assert pane._reap() == 4
    # Something else reaped it first.
    pane._pid = pid

    def gone(pid, flags):
        raise ChildProcessError()

    with monkeypatch.context() as patched:
        patched.setattr(os, "waitpid", gone)
        assert pane._reap() is None
        # Still running when the wait runs out: not the program's status,
        # and not a freeze.
        patched.setattr(os, "waitpid", lambda pid, flags: (0, 0))
        patched.setattr(viewpane, "REAP_SECONDS", 0.03)
        assert pane._reap() is None
    pane.on_exit()
    app._close_plugin(app.right)


def test_a_status_that_cannot_be_read_counts_as_a_clean_exit(app,
                                                             monkeypatch):
    app._program_in_pane(app.left.fs, str(app.left.path) + "/second.txt",
                         ["sh", "-c", "read line"], False)
    pane = app.right.plugin
    with monkeypatch.context() as patched:
        patched.setattr(pane, "_reap", lambda: None)
        pane._finish()
        pane._finish()                           # once only
    assert pane.wants_close is True and pane.closed_note == "Viewed second.txt"
    assert _wait_closed(app, app.right)


def test_an_outside_program_takes_a_busy_view_pane_in_turn(app, outside):
    _view(app)
    outside(viewer="sh -c 'read line'")
    app.handle_key(MENU_KEY)
    app.handle_key(TAB)
    _point_at(app.left, "second.txt")
    app.handle_key(curses.KEY_F3)
    assert isinstance(app.right.plugin, ProgramPane)
    app.handle_key(MENU_KEY)
    app.handle_key(ord("q"))
    assert app.right.plugin is None and app.single is False


def test_with_both_panes_busy_an_outside_pager_has_the_terminal(app, outside,
                                                                monkeypatch):
    outside(viewer="less")
    app.right.plugin = object()
    app.left.plugin = object()
    calls = []
    monkeypatch.setattr(app, "_suspend_and_run",
                        lambda cmd, cwd, banner, **kw: calls.append(cmd) or 0)
    assert app._view_externally(app.left.fs,
                                str(app.left.path) + "/second.txt")
    assert app._edit_externally(app.left.fs,
                                str(app.left.path) + "/second.txt") is False
    assert calls and calls[0][0] == "less"


# -- drawing, on a real screen ----------------------------------------------------

def _on_screen(files, script, rows=24, cols=80):
    def run(stdscr):
        app = App(stdscr, str(files / "left"), str(files / "right"))
        app.open_in_pane = True

        def snap():
            app.draw()
            return [stdscr.instr(row, 0).decode() for row in range(rows)]

        return script(app, snap)

    return with_curses_screen(rows, cols, run)


def test_the_view_is_drawn_alone_across_the_whole_width(files, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))

    def script(app, snap):
        _view(app)
        alone = snap()
        app.handle_key(ord("z"))
        both = snap()
        app.handle_key(MENU_KEY)
        menu = snap()
        return alone, both, menu

    alone, both, menu = _on_screen(files, script)
    assert alone[1].startswith(" View: notes.txt")
    assert alone[2].startswith(" 1 line 0")
    assert "second.txt" not in "".join(alone)            # the listing hidden
    assert "second.txt" in "".join(both)                  # the listing
    assert both[1][40:].startswith(" View: notes.txt")    # beside the view
    assert viewpane.MENU_HINT in menu[21]


def test_the_editor_puts_the_terminals_cursor_where_it_types(files,
                                                             monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))
    shown = []
    real = curses.curs_set

    def curs_set(visibility):
        shown.append(visibility)
        return real(visibility)

    monkeypatch.setattr(curses, "curs_set", curs_set)

    def script(app, snap):
        _edit(app)
        app.handle_key(curses.KEY_DOWN)
        _type(app, "ab")
        app.draw()
        spot = app.stdscr.getyx()
        app.handle_key(MENU_KEY)                 # the menu hides it
        app.draw()
        app.handle_key(ESC)
        app.draw()
        return spot

    spot = _on_screen(files, script)
    # A row below the pane's title, after a three-column gutter (61 lines).
    assert spot == (1 + 2, 3 + 2)
    assert shown[-3:] == [1, 0, 1]


def test_the_view_draws_at_the_smallest_screen_and_through_a_resize(
        files, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))

    def script(app, snap):
        pane = _view(app)
        snap()
        first = pane._win
        curses.resizeterm(8, 24)
        app.draw()
        small = app.stdscr.instr(1, 0).decode()
        assert pane._win is not first          # a new window for a new size
        assert pane.size == (5, 24)
        curses.resizeterm(24, 80)
        snap()
        return small, pane

    small, pane = _on_screen(files, script)
    assert small.startswith(" View: notes.txt")
    assert pane.size == (21, 80)


def test_a_pane_too_small_to_hold_a_view_draws_nothing(app):
    pane = _view(app)
    pane.draw(None, 1, 0, 2, 80)
    pane.draw(None, 1, 0, 20, 7)
    assert pane._win is None
    assert pane.cursor() is None


def test_an_outside_program_draws_its_own_caption(files, monkeypatch,
                                                  outside):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))

    def script(app, snap):
        app._program_in_pane(app.left.fs, str(files / "left" / "second.txt"),
                             ["sh", "-c", "read line"], False)
        top = snap()[1]
        app._close_plugin(app.right)
        return top

    assert _on_screen(files, script).startswith(" [sh] second.txt")


def test_quitting_the_application_closes_the_view(files, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(files / "config"))
    app = App(_StubScreen(), str(files / "left"), str(files / "right"))
    app.open_in_pane = True
    _view(app)
    app._close_backends()
    assert app.right.plugin is None and app.active is app.left


def test_the_session_tidies_up_a_copy_it_never_ran(tmp_path):
    from test_extedit import _Remote

    session = extedit.Session(_Remote(b"x"), "/srv/x.txt", ["less"], False)
    started = session.start()
    assert isinstance(started, tuple)
    session.close()
    assert not os.path.exists(session.copy)
