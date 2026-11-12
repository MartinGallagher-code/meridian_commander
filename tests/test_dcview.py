# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The floor view: claiming a ``.dc``, walking the floor, and drawing it.

Choosing the browser and walking the floor are tested without a screen;
drawing is tested on a real curses screen over a pseudo-terminal, as the other
browsers are, so a write past the edge of a window shows up as the error it
is.  The layouts are small and written for the test.
"""

from __future__ import annotations

import curses
import time

import pytest

from meridian_commander import dcview, quickview, theme
from meridian_commander.app import App
from meridian_commander.browsers import browser_for, has_own_browser, viewer_for
from meridian_commander.dclayout import parse
from meridian_commander.dcview import (
    CURSOR,
    EMPTY,
    UNSET,
    FILL,
    MARK,
    PALETTE,
    FloorView,
    Legend,
    category,
    devices,
    is_layout_file,
    is_layout_name,
)
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.imageview import HALF_BLOCK
from meridian_commander.theme import Pairs
from meridian_commander.viewer import Viewer

from dc_examples import EXAMPLES
from support import (
    _FakeRemoteBackend,
    _StubScreen,
    script_newwin,
    with_curses_screen,
    write,
    write_zip,
)

FLOOR = """\
# A small floor for the tests.
dc Site name="Test site" region=eu +site
  room Hall name="Hall One"
    row A..B +compute
      rack R[1..3] u=12
        node tor at=12 role=tor +switch
        node s[1..4] u=2 role=server model=m1
    row C
      rack Empty u=6
      rack Full u=4
        node big u=4 role=storage
  room Core
    row X
      rack N1 u=10
        node spine[1..2] u=2 role=spine +switch
"""

CALCULATOR = "# a dc script\n2 3 + p\n"


@pytest.fixture
def floor(tmp_path):
    path = tmp_path / "floor.dc"
    write(str(path), FLOOR)
    return str(path)


@pytest.fixture
def view(fs, floor):
    return FloorView(fs, floor)


def at(view, path):
    """The element at ``path`` in the view's layout."""
    for element in view.layout.walk():
        if element.path == path:
            return element
    raise AssertionError(path)


# -- claiming a file -----------------------------------------------------------

@pytest.mark.parametrize("name, claimed", [
    ("floor.dc", True), ("FLOOR.DC", True), ("a.Dc", True), (".dc", True),
    ("floor.dcx", False), ("dc", False), ("floor.d", False),
])
def test_the_name_has_to_end_in_dc(name, claimed):
    assert is_layout_name(name) is claimed


@pytest.mark.parametrize("text, claimed", [
    (FLOOR, True),
    ("title Floor\nrow A\n", True),
    ("model m u=1\n", True),
    ("\n\n  # spare\nDC Site\n", True),
    (CALCULATOR, False),
    ("", False),
    ("# only comments\n# here\n", False),
    ("room A\n  rack R\n", False),
])
def test_the_content_has_to_open_like_a_layout(fs, tmp_path, text, claimed):
    path = str(tmp_path / "x.dc")
    write(path, text)
    assert is_layout_file(fs, path) is claimed


class _Recorded(LocalFileSystem):
    """A backend that says how much of each file was asked for."""

    def __init__(self):
        super().__init__()
        self.asked: list[int] = []

    def open_read(self, path):
        real = super().open_read(path)
        asked = self.asked

        class Reader:
            def read(self, size=-1):
                asked.append(size)
                return real.read(size)

            def close(self):
                real.close()

        return Reader()


def test_only_the_start_of_the_file_is_read_to_decide(tmp_path):
    path = str(tmp_path / "big.dc")
    # The probe's worth of comment, then a layout: past what is read, so
    # not claimed -- and nothing past the probe was asked for.
    write(path, "#" * dcview.PROBE_BYTES + "\ndc Site\n")
    backend = _Recorded()
    assert is_layout_file(backend, path) is False
    assert backend.asked == [dcview.PROBE_BYTES]


class _Unreadable(LocalFileSystem):
    def open_read(self, path):
        raise PermissionError("denied")


def test_a_file_that_cannot_be_read_is_left_to_the_text_viewer(tmp_path):
    path = str(tmp_path / "locked.dc")
    write(path, FLOOR)
    backend = _Unreadable()
    assert is_layout_file(backend, path) is False
    shown = viewer_for(backend, path)
    assert type(shown) is Viewer and "denied" in shown.error


@pytest.mark.parametrize("backend", [LocalFileSystem, _FakeRemoteBackend])
def test_viewer_for_and_the_pager_rule_agree(tmp_path, backend):
    fs = backend()
    layout, script = str(tmp_path / "Floor.DC"), str(tmp_path / "calc.dc")
    write(layout, FLOOR)
    write(script, CALCULATOR)
    assert isinstance(viewer_for(fs, layout), FloorView)
    assert browser_for(fs, layout) is FloorView
    assert has_own_browser(fs, layout) is True
    assert type(viewer_for(fs, script)) is Viewer
    assert browser_for(fs, script) is None
    assert has_own_browser(fs, script) is False


def test_a_layout_inside_an_archive_opens_as_its_floor(tmp_path):
    from meridian_commander.archive import ArchiveFileSystem

    archive = write_zip(str(tmp_path / "site.zip"),
                        {"site/floor.dc": FLOOR, "site/calc.dc": CALCULATOR})
    fs = ArchiveFileSystem(LocalFileSystem(), archive)
    shown = viewer_for(fs, "/site/floor.dc")
    assert isinstance(shown, FloorView) and shown.layout.total == 49
    assert type(viewer_for(fs, "/site/calc.dc")) is Viewer


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    left = tmp_path / "left"
    write(str(left / "floor.dc"), FLOOR)
    write(str(left / "calc.dc"), CALCULATOR)
    (tmp_path / "right").mkdir()
    return App(_StubScreen(), str(left), str(tmp_path / "right"))


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def _paging(monkeypatch, app):
    from meridian_commander import config as config_mod

    monkeypatch.setattr(config_mod, "external_viewer", lambda: "less")
    ran = []
    monkeypatch.setattr(app, "_suspend_and_run",
                        lambda cmd, cwd, banner, **kw: ran.append(cmd) or 0)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    return ran


def test_a_layout_keeps_the_floor_view_with_a_pager_configured(
        tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    ran = _paging(monkeypatch, app)
    shown = []
    monkeypatch.setattr(FloorView, "run", lambda self, stdscr: shown.append(self))
    _point_at(app.left, "floor.dc")
    app._view()
    assert ran == [] and len(shown) == 1
    _point_at(app.left, "calc.dc")
    app._view()
    assert ran == [["less", str(tmp_path / "left" / "calc.dc")]]
    assert len(shown) == 1


def test_quick_view_shows_a_layout_as_its_floor_and_a_script_as_text(
        tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _point_at(app.left, "floor.dc")
    app._quick_view()
    pane = app.right.plugin
    pane.follow()
    assert isinstance(pane.view, FloorView)
    assert pane.handle_key(curses.KEY_DOWN) is None     # the app's keys
    _point_at(app.left, "calc.dc")
    pane.follow()
    assert type(pane.view) is quickview._Glance


def test_quick_view_on_a_remote_layout_waits_then_shows_the_floor(
        tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(quickview.time, "monotonic", lambda: clock[0])
    app = _app(tmp_path, monkeypatch)
    remote = _Recorded()
    remote.scheme = "sftp"
    app.left.set_location(remote, str(tmp_path / "left"))
    _point_at(app.left, "floor.dc")
    app._quick_view()
    pane = app.right.plugin
    pane.follow()
    assert remote.asked == [] and pane.view is None
    clock[0] += quickview.SETTLE_SECONDS * 2
    pane.tick()
    assert isinstance(pane.view, FloorView)
    # Two probes of the start, then the layout itself.
    assert remote.asked == [dcview.PROBE_BYTES, dcview.PROBE_BYTES,
                            dcview.MAX_BYTES + 1]


# -- reading -------------------------------------------------------------------

def test_a_missing_file_is_an_error_not_a_crash(fs, tmp_path):
    shown = FloorView(fs, str(tmp_path / "gone.dc"))
    assert shown.error and shown.focus is None and shown.cursor is None
    assert shown.zoom() == "plan" and shown.visible() == []
    shown.move(1, 0)
    shown.zoom_in()
    assert shown.zoom_out() is False


def test_a_layout_over_the_byte_cap_is_refused(fs, floor, monkeypatch):
    monkeypatch.setattr(dcview, "MAX_BYTES", 100)
    shown = FloorView(fs, floor)
    assert shown.error == "layout is larger than 0 MiB -- not read"
    assert shown.focus is None


def test_it_opens_on_the_whole_floor_with_the_first_rack_under_the_cursor(view):
    assert view.zoom() == "plan"
    assert view.focus.path == "Site"
    assert view.cursor.path == "Site/Hall/A/R1"
    assert view.layout.total == 49


# -- walking the floor ---------------------------------------------------------

def test_the_plan_of_a_site_has_its_rooms_over_their_rows(view):
    lines = view.plan_lines()
    assert [(line.element.id, line.header, line.indent) for line in lines] == [
        ("Hall", True, 0), ("A", False, 2), ("B", False, 2), ("C", False, 2),
        ("Core", True, 0), ("X", False, 2)]
    assert [cell.id for cell in lines[3].cells] == ["Empty", "Full"]


def test_enter_zooms_from_the_site_to_a_room_a_row_and_a_rack(view):
    view.move(1, 0)
    view.zoom_in()
    assert (view.zoom(), view.focus.id, view.cursor.path) == (
        "plan", "Hall", "Site/Hall/A/R2")
    view.zoom_in()
    assert (view.zoom(), view.focus.path, view.cursor.id) == ("row", "Site/Hall/A", "R2")
    view.zoom_in()
    assert (view.zoom(), view.focus.path) == ("rack", "Site/Hall/A/R2")
    assert view.cursor.id == "tor"             # the top of the rack
    view.zoom_in()
    assert view.notice == "nothing inside tor"
    assert view.focus.id == "R2"


def test_backspace_zooms_back_out_keeping_the_place(view):
    for _ in range(3):
        view.zoom_in()
    view.move(0, 2)
    assert view.cursor.id == "s3"
    assert view.zoom_out() is True
    assert (view.zoom(), view.cursor.id) == ("row", "R1")
    assert view.zoom_out() and view.zoom_out()
    assert (view.focus.id, view.cursor.path) == ("Site", "Site/Hall/A/R1")
    assert view.zoom_out() is False


def test_the_plan_moves_across_and_between_lines(view):
    view.move(2, 0)
    assert view.cursor.path == "Site/Hall/A/R3"
    view.move(1, 0)
    assert view.notice == "no more this way" and view.cursor.id == "R3"
    view.move(0, 2)                            # row C has two racks
    assert view.cursor.path == "Site/Hall/C/Full"
    view.move(0, 1)                            # over Core's header, to X
    assert view.cursor.path == "Site/Core/X/N1"
    view.move(0, 1)
    assert view.notice == "no more this way"
    view.move(0, -4)
    assert view.cursor.path == "Site/Hall/A/R1"


def test_at_a_row_left_and_right_walk_its_racks_and_up_and_down_its_rows(view):
    view.zoom_in()
    view.zoom_in()
    view.move(1, 0)
    assert view.cursor.id == "R2"
    view.move(0, 1)
    assert (view.focus.path, view.cursor.path) == ("Site/Hall/B", "Site/Hall/B/R2")
    view.move(0, 1)                            # C: only two racks
    assert view.cursor.path == "Site/Hall/C/Full"
    view.move(0, 1)
    assert view.notice == "no more rows this way"
    view.move(-5, 0)
    assert view.cursor.id == "Empty" and view.notice == "no more this way"


def test_at_a_rack_up_and_down_walk_its_devices_and_across_its_neighbours(view):
    for _ in range(3):
        view.zoom_in()
    view.move(0, 1)
    assert (view.cursor.id, view.cursor.u_at) == ("s4", 7)
    view.move(1, 0)                            # on to R2, at the same U
    assert (view.focus.id, view.cursor.id) == ("R2", "s4")
    view.move(1, 0)
    view.move(1, 0)
    assert view.notice == "no more racks this way" and view.focus.id == "R3"
    view.move(0, -9)
    assert view.cursor.id == "tor"


def test_an_empty_rack_has_the_rack_itself_under_the_cursor(view):
    view.move(0, 2)
    view.zoom_in()
    view.zoom_in()
    assert view.cursor.id == "Empty"
    view.zoom_in()
    assert (view.zoom(), view.focus.id, view.cursor.id) == ("rack", "Empty", "Empty")
    view.move(0, 1)
    assert view.notice == "this rack is empty"
    view.zoom_in()
    assert view.notice == "nothing inside Empty"
    view.move(1, 0)                            # to Full, at its top
    assert (view.focus.id, view.cursor.id) == ("Full", "big")
    view.move(-1, 0)
    assert (view.focus.id, view.cursor.id) == ("Empty", "Empty")


def test_a_floor_that_is_one_rack_has_nowhere_to_walk(fs, tmp_path):
    path = str(tmp_path / "rack.dc")
    write(path, "dc R\n  node a\n")            # a dc holding one device
    shown = FloorView(fs, path)
    assert shown.zoom() == "plan"
    assert [line.cells for line in shown.plan_lines()] == [[shown.cursor]]
    shown.zoom_in()
    assert shown.notice == "nothing inside a"
    write(path, "title t\nrack R u=4\n  node a\n")
    single = FloorView(fs, path)
    assert single.zoom() == "rack" and single.cursor.id == "a"
    single.move(1, 0)
    assert single.notice == "no other rack here"
    write(path, "title t\nrow A\n  rack R\n")
    row = FloorView(fs, path)
    assert row.zoom() == "row"
    row.move(0, 1)
    assert row.notice == "no other row here"


def test_a_floor_with_nothing_on_it(fs, tmp_path):
    path = str(tmp_path / "bare.dc")
    write(path, "dc Bare\n")
    shown = FloorView(fs, path)
    assert shown.plan_lines() == [] and shown.cursor is shown.focus
    shown.move(0, 1)
    assert shown.notice == "Bare holds nothing"


def test_a_cursor_from_elsewhere_lands_on_the_first_thing_shown(view):
    view.cursor = at(view, "Site/Core/X/N1")
    view.focus = at(view, "Site/Hall")
    view._settle()
    assert view.cursor.path == "Site/Hall/A/R1"


def test_a_container_among_racks_is_a_column_that_zooms_in(fs, tmp_path):
    path = str(tmp_path / "mixed.dc")
    write(path, "title t\nrow A\n  rack R\n  network N\n    spine s[1..2]\n"
          "  node pdu u=3 role=power\n")
    shown = FloorView(fs, path)
    assert shown.zoom() == "row"
    shown.move(1, 0)
    assert shown.cursor.id == "N"
    shown.zoom_in()
    assert shown.zoom() == "plan" and shown.focus.id == "N"
    assert [line.cells for line in shown.plan_lines()] == [
        [child] for child in shown.focus.children]


# -- what things are -----------------------------------------------------------

def test_categories_by_role_tag_and_model(view):
    server, tor = at(view, "Site/Hall/A/R1/s1"), at(view, "Site/Hall/A/R1/tor")
    assert [category(server, mode) for mode in dcview.COLOUR_MODES] == [
        "server", "compute", "m1", ""]
    assert [category(tor, mode) for mode in dcview.COLOUR_MODES] == [
        "tor", "switch", None, ""]


def test_devices_are_what_holds_nothing_except_an_empty_rack(view):
    found = {element.id for element in devices(view.layout.root)}
    assert "Empty" not in found and {"big", "tor", "s4", "spine2"} <= found
    assert len(found) == 8                    # ids repeat across racks


def test_the_legend_ranks_the_kinds_and_folds_the_rest_into_one(fs, tmp_path):
    lines = ["dc D", "  rack R u=100"]
    for index in range(12):
        lines.append(f"    node k{index}[1..{index + 1}] role=r{index:02}")
    lines.append("    node bare")
    layout = parse("\n".join(lines) + "\n")
    legend = Legend(layout, "role")
    assert [s.name for s in legend.swatches] == [f"r{i:02}" for i in range(11, 1, -1)]
    assert [s.colour for s in legend.swatches] == list(PALETTE)
    assert legend.other.name == "2 more" and legend.other.count == 1 + 2
    assert legend.swatch("r00") is legend.other
    assert legend.swatch(None) is legend.unset and legend.unset.count == 1
    assert legend.entries()[-2:] == [legend.other, legend.unset]
    letters = [s.letter for s in legend.swatches]
    assert len(set(letters)) == len(letters) and letters[0] == "R"
    off = Legend(layout, "off")
    assert off.swatch("r00") is off.used and off.entries() == []


def test_a_letter_is_found_for_every_name():
    taken: set = set()
    names = [dcview._letter("x", taken) for _ in range(37)]
    assert names[0] == "X" and names[-1] == "*"
    assert len(set(names[:-1])) == 36


def test_what_a_rack_mostly_holds_is_weighed_by_u(view):
    rack = at(view, "Site/Hall/A/R1")
    assert view.dominant(rack) == ("server", 6)          # 9U of 12
    assert view.dominant(at(view, "Site/Hall/C/Empty")) == (None, 0)
    assert view.dominant(at(view, "Site/Hall/C/Full")) == ("storage", 8)
    assert view.dominant(at(view, "Site/Hall/A/R1/tor")) == ("tor", 8)
    assert view.dominant(at(view, "Site/Hall/A")) == ("server", 8)
    assert view.dominant(at(view, "Site/Hall/A")) == ("server", 8)  # cached
    view.cycle_colour()
    assert view.dominant(rack) == ("compute", 6)
    for _ in range(3):
        view.cycle_colour()
    assert dcview.COLOUR_MODES[view.mode] == "role"


def test_the_kinds_under_an_element_come_outermost_first(view):
    assert view.kinds_under(view.layout.root) == {
        "room": 2, "row": 4, "rack": 9, "node": 33}
    assert view.kinds_under(view.layout.root) is view.kinds_under(view.layout.root)


# -- drawing -------------------------------------------------------------------

class _Writes:
    """A real window that remembers the attribute each cell was written in.

    Reading it back with ``inch`` would be simpler, but some builds of the
    curses module (Python 3.9's among them) hand back garbage for a cell
    holding a character outside ASCII -- which every cell of the floor is.
    """

    def __init__(self, win):
        self._win = win
        self.attrs: dict[tuple[int, int], int] = {}

    def addstr(self, y, x, text, attr=0):
        for offset in range(len(text)):
            self.attrs[(y, x + offset)] = attr
        return self._win.addstr(y, x, text, attr)

    def __getattr__(self, name):
        return getattr(self._win, name)


def screen(view, rows=30, cols=100, colours=256, steps=()):
    """Draw ``view`` after ``steps`` (callables taking it) and return each
    row's text and the (fg, bg) each cell was written in."""
    def run(stdscr):
        win = _Writes(curses.newwin(rows, cols, 0, 0))
        for step in steps:
            step(view)
        view.draw(win)
        text = [win.instr(y, 0, cols * 4).decode("utf-8", "replace").rstrip()
                for y in range(rows)]
        cells = {cell: curses.pair_content(curses.pair_number(
            attr & curses.A_COLOR)) for cell, attr in win.attrs.items()}
        return text, cells

    return with_curses_screen(rows + 2, cols + 2, run, colours=colours)


def zoom(times):
    return [FloorView.zoom_in] * times


def test_the_plan_draws_each_rack_as_full_as_it_is(view):
    text, cells = screen(view)
    assert text[0].startswith(" Floor: floor.dc -- Site  [plan]")
    assert text[1].startswith("Hall  Hall One  row: 3  rack: 8  node: 31")
    assert text[2].startswith("  A  ")
    # R1 is under the cursor; R2 and R3 are three-quarters full of servers.
    assert text[2][5:13] == MARK * 2 + " " + FILL[6] * 2 + " " + FILL[6] * 2
    server = PALETTE[0]
    assert cells[(2, 5)] == (server, CURSOR)
    assert cells[(2, 8)] == (server, EMPTY)
    assert text[4][5:10] == "   " + FILL[8] * 2      # C: Empty, then Full
    assert text[5].startswith("Core")


def test_a_wide_plan_falls_back_to_a_column_a_rack(fs, tmp_path):
    path = str(tmp_path / "wide.dc")
    write(path, "dc D\n  room H\n    row A\n      rack R[1..60]\n"
          "        node n role=x\n    row B\n      rack S\n")
    shown = FloorView(fs, path)
    text, _ = screen(shown, cols=50)
    assert text[2] == "  A " + MARK + FILL[1] * 44 + ">"
    shown.move(50, 0)
    text, _ = screen(shown, cols=50)
    assert text[2].startswith("  A<" + FILL[1]) and MARK in text[2]
    assert shown.left == 6                     # the cursor is the last shown


def test_a_plan_scrolls_to_keep_the_cursor_on_screen(fs):
    shown = FloorView(fs, "unused")
    shown.error = None
    shown.layout = parse(EXAMPLES["small"][0])
    shown.focus = shown.layout.root
    shown.cursor = shown._first_visible()
    shown.move(0, 8)                           # down to the meet-me room
    text, _ = screen(shown, rows=12)
    assert shown.top > 0
    assert any(line.startswith("  S1") for line in text)


def test_a_plan_in_letters_on_a_screen_without_colour(view):
    text, _ = screen(view, colours=8)
    # Small letters for a rack partly full, a capital for a full one, a dot
    # for an empty one; the cursor's is reversed rather than coloured.
    assert text[2][5:13] == "ss ss ss"
    assert text[4][5:10] == ".. OO"            # storage: S and T are taken
    legend = text[-4]
    assert "S= server" in legend and "role:" in legend


def test_the_row_stands_its_racks_side_by_side(view):
    text, cells = screen(view, steps=zoom(2))
    assert "[row]" in text[0]
    labels = text[-5]
    assert labels.startswith("R1 R2 R3")
    floor = 30 - 6                             # the row above the labels
    # Two U to a cell: the bottom servers fill it, top and bottom halves.
    assert text[floor].startswith(HALF_BLOCK * 2 + " ")
    server, tor = PALETTE[0], PALETTE[1]
    assert cells[(floor, 0)] == (server, server)
    assert cells[(floor - 5, 0)] == (tor, EMPTY)        # U12 over U11
    assert text[floor - 6] == ""                        # above the racks


def test_a_plan_draws_containers_devices_and_empty_containers(fs, tmp_path):
    path = str(tmp_path / "kinds.dc")
    write(path, "dc D\n  room H\n    pod P\n      row A\n        rack R\n"
          "          node n role=x\n      row E\n        rack Q\n"
          "      node pdu role=power\n    pod Bare\n")
    shown = FloorView(fs, path)
    assert [line.element.id for line in shown.plan_lines()] == ["H", "P", "Bare"]
    text, cells = screen(shown)
    # P holds a row with a device in it, a row of one empty rack, and a
    # device of its own; Bare is a line of itself, a device.
    assert text[2] == "  P    " + MARK * 2 + " " + FILL[0] * 2 + " " + \
        dcview.DEVICE * 2
    assert text[3] == "  Bare " + dcview.DEVICE * 2
    shown.move(1, 0)
    text, _ = screen(shown, colours=8)
    assert text[2] == "  P    XX .. PP"
    assert text[3] == "  Bare --"


def test_a_gap_between_a_rack_and_a_device_above_it_is_left_empty(fs, tmp_path):
    path = str(tmp_path / "gap.dc")
    write(path, "title t\nrow A\n  rack R u=2\n    node high at=9 u=2\n")
    shown = FloorView(fs, path)
    text, cells = screen(shown)
    floor = 30 - 6
    assert [text[y][:2] for y in range(floor - 4, floor + 1)] == [
        HALF_BLOCK * 2, "", "", "", HALF_BLOCK * 2]
    assert cells[(floor - 4, 0)] == (UNSET, UNSET)      # U9-10, the device
    assert cells[(floor, 0)] == (EMPTY, EMPTY)          # U1-2, the rack


def test_a_named_element_is_named_in_the_inspector(fs, tmp_path):
    path = str(tmp_path / "named.dc")
    write(path, "dc D\n  rack R name=Primary\n")
    text, _ = screen(FloorView(fs, path))
    assert text[-3].startswith(' rack R "Primary"  D/R  42U, 0U used')


def test_a_window_that_refuses_every_write_does_not_take_it_down(view,
                                                                monkeypatch):
    script_newwin(monkeypatch, [10, 10, 10, ord("q")], fail_draws=True)
    with_curses_screen(30, 100, view.run, colours=256)
    assert view.zoom() == "rack"


def test_a_row_too_tall_for_the_screen_is_scaled_down(fs, tmp_path):
    path = str(tmp_path / "tall.dc")
    write(path, "title t\nrow A\n  rack R u=100\n    node top at=100\n"
          "  rack S u=10\n  node box u=6 role=pdu\n")
    shown = FloorView(fs, path)
    text, cells = screen(shown, rows=20)
    floor = 20 - 6
    column = [text[y][:2] for y in range(1, floor + 1)]
    assert column.count(HALF_BLOCK * 2) == 13            # 100U in 13 cells
    # Columns as wide as the longest id, three: S's 10U is two cells, and
    # the box outside a rack one block of its 6U.
    assert text[floor - 1][4:7] == HALF_BLOCK * 3
    assert text[floor - 2][3:].strip() == ""
    assert text[floor][8:11] == dcview.DEVICE * 3
    assert cells[(floor, 8)][1] == EMPTY
    assert text[-5].startswith("R   S   box")


def test_many_racks_scroll_across_the_row(fs, tmp_path):
    path = str(tmp_path / "long.dc")
    write(path, "title t\nrow A\n  rack R[01..40]\n")
    shown = FloorView(fs, path)
    text, _ = screen(shown, cols=40)
    assert text[-5].endswith(">")
    shown.move(30, 0)
    text, _ = screen(shown, cols=40)
    assert text[-5].endswith("<") and "R31" in text[-5]


def test_a_rack_is_drawn_u_by_u_with_its_devices_named(view):
    text, cells = screen(view, steps=zoom(3))
    assert "Site > Hall > A > R1  [rack]" in text[0]
    assert text[1].startswith(" 12 >") and text[1].endswith("< tor")
    assert text[2].startswith(" 11 |") and text[2].rstrip().endswith("|")
    assert text[4].startswith("  9 |") and text[4].endswith("|")
    assert text[5].startswith("  8 |") and text[5].endswith("| s4")
    assert text[6].startswith("  7 |") and text[6].endswith("|")
    assert text[12].startswith("  1 |")
    assert cells[(1, 5)] == (PALETTE[1], EMPTY)
    assert cells[(2, 5)] == (EMPTY, EMPTY)


def test_a_rack_taller_than_the_screen_follows_the_cursor(fs, tmp_path):
    path = str(tmp_path / "rack.dc")
    write(path, "title t\nrack R u=42\n  node top at=42\n  node low at=1 u=2\n")
    shown = FloorView(fs, path)
    text, _ = screen(shown, rows=15)
    assert text[1].startswith(" 42 >")
    shown.move(0, 1)
    text, _ = screen(shown, rows=15)
    assert text[10].startswith("  1 >") and text[9].endswith("< low")


def test_a_device_above_its_rack_and_two_in_one_slot_are_marked(fs, tmp_path):
    path = str(tmp_path / "odd.dc")
    write(path, "title t\nrack R u=3\n  node a at=2\n  node b at=2\n"
          "  node high at=5\n")
    shown = FloorView(fs, path)
    text, _ = screen(shown)
    assert text[1].startswith("  5!") and text[1].endswith("< high")
    assert text[2].startswith("  4!")
    assert text[4].endswith("a (+1 in the same slot)")
    assert "! 1 not read: line 5: node" in text[-1]


def test_the_inspector_shows_the_element_under_the_cursor(view):
    text, _ = screen(view, steps=zoom(3) + [lambda v: v.move(0, 1)])
    assert text[-3] == (" node s4  Site/Hall/A/R1/s4  U7-8  line 7")
    assert text[-2] == (" u=2  role=server  model=m1  ^region=eu  +compute  "
                        "+site   (^ inherited)").replace("+compute", "^+compute") \
        .replace("+site", "^+site")
    text, _ = screen(view, steps=[FloorView.zoom_out])
    assert text[-3] == (" rack R1  Site/Hall/A/R1  12U, 9U used  holds node: 5  "
                        "line 5")


def test_an_element_with_nothing_to_show_says_so(fs, tmp_path):
    path = str(tmp_path / "plain.dc")
    write(path, "dc D\n  rack R\n")
    text, _ = screen(FloorView(fs, path))
    assert text[-2] == " (no attributes or tags)"


def test_the_legend_and_its_overflow(view):
    text, _ = screen(view)
    assert text[-4].startswith(" role: ") and "server 24" in text[-4]
    assert "tor 6" in text[-4] and "storage 1" in text[-4]
    text, _ = screen(view, cols=40)
    assert text[-4].endswith("more")
    text, _ = screen(view, steps=[FloorView.cycle_colour] * 3)
    assert text[-4] == " off: colouring off"
    assert "[c]olour: off" in text[-1]


def test_running_out_of_colour_pairs_draws_letters_and_says_so(view, monkeypatch):
    monkeypatch.setattr(theme, "shared_pairs", lambda: Pairs(limit=0))
    text, _ = screen(view)
    assert text[2][5:13] == "ss ss ss"
    assert "colours ran out: letters shown" in text[-1]
    assert view.ran_out is True


def test_a_screen_too_small_says_so(view):
    text, _ = screen(view, rows=9, cols=40)
    assert text[0].startswith(" Floor view needs 30x10")


def test_an_unreadable_layout_and_an_empty_one_are_drawn_as_such(fs, tmp_path):
    text, _ = screen(FloorView(fs, str(tmp_path / "gone.dc")))
    assert text[2].startswith("  Cannot show this layout:")
    assert text[-1].strip() == "[q]uit"
    path = str(tmp_path / "empty.dc")
    write(path, "title nothing\n")
    text, _ = screen(FloorView(fs, path))
    assert text[2] == "  This layout makes no elements."
    write(path, "dc Bare\n")
    text, _ = screen(FloorView(fs, path))
    assert text[2] == "  dc Bare holds nothing."


def test_the_footer_names_the_first_thing_not_read_and_the_notice(fs, tmp_path):
    path = str(tmp_path / "bad.dc")
    write(path, "dc D\n  rack R[a..9]\n  rack S u=abc\n")
    shown = FloorView(fs, path)
    shown.move(1, 0)
    text, _ = screen(shown)
    assert text[-1].startswith(
        ' no more this way  |  ! 2 not read: line 2: cannot expand range "a..9"')


def test_quick_view_draws_the_floor_in_a_pane_sized_window(view):
    def run(stdscr):
        pane = stdscr.subwin(20, 40, 1, 40)
        view.draw(pane)
        return [pane.instr(y, 0, 160).decode("utf-8", "replace").rstrip()
                for y in range(20)]

    text = with_curses_screen(24, 80, run, colours=256)
    assert text[0].startswith(" Floor: floor.dc")
    assert text[-1].startswith(" arrows move")


def test_mega_opens_and_draws_a_screen_at_every_zoom(fs, tmp_path):
    path = str(tmp_path / "mega.dc")
    write(path, EXAMPLES["mega"][0])
    started = time.monotonic()
    shown = FloorView(fs, path)
    for _ in range(4):
        text, _ = screen(shown, rows=24, cols=80)
        shown.zoom_in()
    assert shown.zoom() == "rack" and "MEGA" in text[0]
    # A quarter of a million elements is read, but only a screen is drawn.
    assert time.monotonic() - started < 60


# -- the key loop --------------------------------------------------------------

def test_the_keys(view, monkeypatch):
    keys = [curses.KEY_RIGHT, ord("l"), curses.KEY_LEFT, ord("h"),
            curses.KEY_DOWN, ord("j"), curses.KEY_UP, ord("k"),
            10, curses.KEY_BACKSPACE, 13, ord("c"), ord("C"),
            curses.KEY_ENTER, 127, ord("x"), 8, 8, ord("q")]
    captured = script_newwin(monkeypatch, keys)
    with_curses_screen(30, 100, view.run, colours=256)
    assert view.focus is view.layout.root
    assert "this is the whole floor" in captured["window"].text
    assert dcview.COLOUR_MODES[view.mode] == "model"


@pytest.mark.parametrize("key", [27, ord("Q"), curses.KEY_F3, curses.KEY_F10])
def test_escape_at_the_top_and_the_quit_keys_close_it(view, monkeypatch, key):
    script_newwin(monkeypatch, [key, ord("j")])
    with_curses_screen(30, 100, view.run, colours=256)
    assert view.cursor.path == "Site/Hall/A/R1"


def test_escape_inside_zooms_out_first(view, monkeypatch):
    script_newwin(monkeypatch, [10, 10, 27, 27, 27, ord("j")])
    with_curses_screen(30, 100, view.run, colours=256)
    assert view.focus is view.layout.root
    assert view.cursor.path == "Site/Hall/A/R1"
