"""Directory sizes: the resumable walk, the pane's totals, and the overlay."""

from __future__ import annotations

import curses
import os

import pytest

from meridian_commander.usage import TreeSizer, bar

from support import _ScriptedDialogs, _StubScreen, with_curses_screen, write


@pytest.fixture(autouse=True)
def _quiet_screen(monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)


def _tree(tmp_path, **dirs) -> str:
    """Build ``{name: (files, bytes each)}`` under a fresh root."""
    root = tmp_path / "tree"
    root.mkdir(exist_ok=True)
    for name, (count, size) in dirs.items():
        for i in range(count):
            write(str(root / name / "inner" / f"f{i}"), "x" * size)
    return str(root)


# -- the walk ------------------------------------------------------------------

def test_a_tree_is_totalled(fs, tmp_path):
    root = _tree(tmp_path, big=(4, 1000), small=(2, 10))
    sizer = TreeSizer(fs, [f"{root}/big", f"{root}/small"])
    totals = sizer.run()
    assert totals[f"{root}/big"] == 4000
    assert totals[f"{root}/small"] == 20
    assert sizer.finished is True
    assert sizer.done == {f"{root}/big", f"{root}/small"}


def test_the_walk_is_resumable_and_bounded(fs, tmp_path):
    """Each step does a little; the total grows until the walk is finished."""
    root = _tree(tmp_path, deep=(3, 100))
    sizer = TreeSizer(fs, [f"{root}/deep"])
    steps = 0
    while sizer.step(budget=1):
        steps += 1
        assert steps < 50                      # it must terminate
    assert sizer.totals[f"{root}/deep"] == 300
    # A root, its own listing and the "inner" listing: more than one step.
    assert steps >= 2


def test_stepping_a_finished_walk_says_there_is_no_more(fs, tmp_path):
    sizer = TreeSizer(fs, [_tree(tmp_path, one=(1, 5)) + "/one"])
    sizer.run()
    assert sizer.step() is False


def test_a_walk_with_nothing_to_do_is_finished_at_once(fs):
    sizer = TreeSizer(fs, [])
    assert sizer.finished is True
    assert sizer.step() is False
    assert sizer.run() == {}


def test_files_count_and_subdirectories_are_followed(fs, tmp_path):
    root = str(tmp_path / "mixed")
    write(f"{root}/top.txt", "x" * 50)
    write(f"{root}/a/b/c/deep.txt", "y" * 70)
    assert TreeSizer(fs, [root]).run()[root] == 120


def test_a_symlinked_directory_is_counted_but_not_walked(fs, tmp_path):
    """Following it would count the target twice -- and a loop for ever."""
    root = str(tmp_path / "linky")
    write(f"{root}/real/file", "x" * 100)
    os.symlink(f"{root}/real", f"{root}/link")
    total = TreeSizer(fs, [root]).run()[root]
    assert total < 200                        # the 100 bytes counted once


def test_a_directory_that_cannot_be_read_does_not_stop_the_walk(fs, tmp_path,
                                                                monkeypatch):
    root = _tree(tmp_path, ok=(2, 100))
    real = fs.listdir

    def refuse(path):
        if path.endswith("inner"):
            raise PermissionError("nope")
        return real(path)

    monkeypatch.setattr(fs, "listdir", refuse)
    sizer = TreeSizer(fs, [f"{root}/ok"])
    assert sizer.run()[f"{root}/ok"] == 0     # unreadable, not crashed
    assert sizer.finished is True


def test_run_gives_up_rather_than_spinning_for_ever(fs, tmp_path):
    """The limit is a safety net, not a schedule; it must not hang."""
    root = str(tmp_path / "wide")
    for i in range(40):                        # more directories than a step
        write(f"{root}/d{i}/f", "x" * 10)
    sizer = TreeSizer(fs, [root])
    sizer.run(limit=1)                         # one round, then give up
    assert sizer.finished is False
    assert sizer.totals[root] < 400
    assert sizer.run()[root] == 400            # picking it up again finishes


# -- the bar -------------------------------------------------------------------

def test_the_bar_scales_against_the_biggest_entry():
    assert bar(100, 100, 10) == "#" * 10
    assert bar(50, 100, 10) == "#" * 5 + "-" * 5


def test_anything_non_zero_shows_at_least_one_mark():
    """A 2 KB file beside a 4 GB one is still not nothing."""
    assert bar(1, 1_000_000, 10).startswith("#")


def test_nothing_to_show_draws_an_empty_bar():
    assert bar(0, 100, 4) == "----"
    assert bar(10, 0, 4) == "----"


def test_a_bar_with_no_room_is_no_bar():
    assert bar(5, 10, 0) == ""


# -- the pane's totals ---------------------------------------------------------

@pytest.fixture
def sized(app, tmp_path):
    """The left pane over a tree with an obvious hog, sizes turned on."""
    for name, (count, size) in {"photos": (4, 1000), "src": (3, 20)}.items():
        for i in range(count):
            write(str(tmp_path / "left" / name / f"f{i}"), "x" * size)
    write(str(tmp_path / "left" / "big.tar"), "z" * 8000)
    app.left.refresh()
    app.left.toggle_sizes()
    return app.left


def _finish(panel):
    for _ in range(500):
        if not panel.step_sizing():
            return
    raise AssertionError("the walk did not finish")


def test_the_toggle_reports_its_new_state(sized):
    assert sized.show_sizes is True
    assert sized.toggle_sizes() is False
    assert sized.sizer is None
    assert sized.toggle_sizes() is True


def test_a_directory_reports_what_is_under_it(sized, tmp_path):
    _finish(sized)
    photos = next(e for e in sized.entries if e.name == "photos")
    assert sized.size_of(photos) == 4000


def test_a_file_reports_its_own_size_either_way(sized):
    big = next(e for e in sized.entries if e.name == "big.tar")
    assert sized.size_of(big) == 8000
    sized.toggle_sizes()
    assert sized.size_of(big) == 8000


def test_a_directory_is_unknown_until_the_walk_reaches_it(sized):
    photos = next(e for e in sized.entries if e.name == "photos")
    assert sized.size_of(photos) == 0          # queued, nothing counted yet
    assert sized.sizing(photos) is True
    _finish(sized)
    assert sized.sizing(photos) is False


def test_with_the_sizes_off_a_directory_has_no_size_to_show(sized):
    sized.toggle_sizes()
    photos = next(e for e in sized.entries if e.name == "photos")
    assert sized.size_of(photos) is None
    assert sized.sizing(photos) is False


def test_a_symlinked_directory_has_no_total_of_its_own(app, tmp_path):
    """It is not walked, so the pane has nothing to show for it but <DIR>."""
    (tmp_path / "left" / "real").mkdir()
    write(str(tmp_path / "left" / "real" / "f"), "x" * 100)
    os.symlink(str(tmp_path / "left" / "real"), str(tmp_path / "left" / "link"))
    app.left.refresh()
    app.left.toggle_sizes()
    link = next(e for e in app.left.entries if e.name == "link")
    assert app.left.size_of(link) is None
    assert app.left.sizing(link) is False


def test_the_parent_entry_is_never_measured(app, tmp_path):
    app.left.chdir(str(tmp_path / "left" / "sub"))
    app.left.toggle_sizes()
    parent = app.left.entries[0]
    assert parent.name == ".."
    assert app.left.size_of(parent) is None


def test_the_biggest_entry_sets_the_scale(sized):
    _finish(sized)
    assert sized.biggest() == 8000             # big.tar, not a directory


def test_a_measured_directory_is_remembered_across_a_visit(sized, tmp_path):
    _finish(sized)
    measured = dict(sized.sizes)
    sized.chdir(str(tmp_path / "left" / "photos"))
    sized.chdir(str(tmp_path / "left"))
    # Nothing left to walk: the totals were kept, so coming back is free.
    assert sized.sizer is None
    assert sized.sizes == measured


def test_forgetting_the_sizes_measures_them_again(sized):
    _finish(sized)
    sized.forget_sizes()
    assert sized.sizes == {}
    assert sized.sizer is not None


def test_stepping_when_nothing_is_being_measured_is_quiet(app):
    assert app.left.step_sizing() is False


# -- the overlay ---------------------------------------------------------------

@pytest.fixture
def ascii_glyphs(monkeypatch):
    """Draw with the ASCII glyph set, so one column is one byte.

    ``instr`` reads bytes, and the box-drawing characters are three each, so
    a unicode frame truncates the row long before its right-hand edge -- the
    reading is the problem, not the drawing.
    """
    from meridian_commander import theme

    # theme.init() re-picks the set from the environment when the screen comes
    # up, so the variable it reads is what has to change -- patching GLYPHS
    # alone is undone the moment a screen is created.
    monkeypatch.setenv("MERIDIAN_ASCII", "1")
    monkeypatch.setattr(theme, "GLYPHS", theme.ASCII_GLYPHS)


def _render(app, panel, h=12, w=44):
    def draw(stdscr):
        app.stdscr = stdscr
        app._draw_panel(panel, 0, 0, h, w, True)
        return "\n".join(stdscr.instr(row, 0, w).decode(errors="replace")
                         for row in range(h))

    return with_curses_screen(h + 2, w + 2, draw)


def _row(screen: str, name: str) -> str:
    return next(line for line in screen.splitlines() if name in line)


def test_the_listing_shows_totals_and_a_share_column(app, sized, ascii_glyphs):
    _finish(sized)
    screen = _render(app, sized)
    assert "Share" in screen
    assert "Modify time" not in screen
    assert "3.9K" in _row(screen, "photos")           # 4000 bytes, not <DIR>
    assert "<DIR>" not in _row(screen, "photos")


def test_the_columns_go_back_when_the_sizes_are_off(app, sized, ascii_glyphs):
    sized.toggle_sizes()
    screen = _render(app, sized)
    assert "Modify time" in screen
    assert "<DIR>" in _row(screen, "photos")


def test_the_biggest_entry_gets_the_longest_bar(app, sized, ascii_glyphs):
    _finish(sized)
    screen = _render(app, sized)
    assert _row(screen, "big.tar").count("#") > _row(screen, "photos").count("#")
    assert _row(screen, "src").count("#") == 1        # small, but not nothing


def test_a_directory_still_being_counted_is_drawn_quietly(app, sized):
    """A growing figure must not read like a finished one."""
    painted = []
    from meridian_commander import theme

    real = theme.paint

    def record(win, y, x, text, role, width=None):
        painted.append((text, role))
        return real(win, y, x, text, role, width)

    with_curses_screen(14, 46, lambda stdscr: None)   # warm the theme up
    import unittest.mock as mock

    with mock.patch.object(theme, "paint", record):
        _render(app, sized)
    roles = [role for text, role in painted if "photos" in str(text)]
    assert roles == ["panelinfo"]


# -- from the application ------------------------------------------------------

def test_the_key_toggles_the_sizes_for_the_active_pane(app):
    app.handle_key(ord("u"))
    assert app.left.show_sizes is True
    assert app.right.show_sizes is False       # one pane at a time
    assert "Directory sizes on" in app.message
    app.handle_key(ord("u"))
    assert app.left.show_sizes is False
    assert "Directory sizes off" in app.message


def test_the_menu_reaches_it_too(app):
    app._dispatch("sizes")
    assert app.left.show_sizes is True


def test_the_main_loop_keeps_counting_between_keystrokes(app, sized):
    """The walk runs on the poll the terminal plug-in already established."""
    assert app._tick_plugins() is True          # work outstanding: keep polling
    _finish(sized)
    assert app._tick_plugins() is False


def test_reloading_measures_again(app, sized, tmp_path, monkeypatch):
    _finish(sized)
    write(str(tmp_path / "left" / "photos" / "extra"), "y" * 5000)
    app._reload()
    assert sized.sizes == {}                    # dropped, and a walk started
    _finish(sized)
    photos = next(e for e in sized.entries if e.name == "photos")
    assert sized.size_of(photos) == 9000


def test_reloading_a_pane_without_sizes_leaves_it_alone(app, monkeypatch):
    _ScriptedDialogs(monkeypatch)
    app._reload()
    assert app.left.show_sizes is False
    assert app.left.sizes == {}
    assert "Reloaded" in app.message


def test_a_pane_with_no_subdirectories_has_nothing_to_walk(app, tmp_path):
    empty = tmp_path / "left" / "flat"
    empty.mkdir()
    write(str(empty / "only.txt"), "x")
    app.left.chdir(str(empty))
    app.left.toggle_sizes()
    assert app.left.sizer is None
    assert app.left.step_sizing() is False


def test_the_stub_screen_draws_the_whole_app_with_sizes_on(app, sized):
    """The full draw path, not just the pane: nothing else assumed a <DIR>."""
    app.stdscr = _StubScreen(24, 80)
    app.draw()
