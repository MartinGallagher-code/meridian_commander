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
    # The target's 100 bytes exactly once, plus whatever the link itself
    # weighs -- which is the length of the path it holds, and so depends on
    # where the temporary directory happens to be.  Taking that from the same
    # listing the walk read is what keeps the assertion exact rather than a
    # threshold that passes on a short path and fails on a long one.
    link = next(e for e in fs.listdir(root) if e.name == "link")
    assert total == 100 + (link.size or 0)


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


def test_a_step_stops_when_its_slice_is_spent(fs, tmp_path, monkeypatch):
    """The slice is a *time* box: it fits in whatever the backend can manage.

    A count would have to be guessed, and the two backends differ by four
    orders of magnitude -- which is how a budget of 8 listings once held a
    local pane to a few dozen a second.  Here a fake clock makes each listing
    cost 4 ms, so a 15 ms slice must stop after four of them.
    """
    from meridian_commander import usage as usage_mod

    root = str(tmp_path / "wide")
    for i in range(40):
        write(f"{root}/d{i}/f", "x" * 10)

    now = [0.0]
    monkeypatch.setattr(usage_mod.time, "monotonic", lambda: now[0])
    real = fs.listdir

    def slow(path):
        now[0] += 0.004                        # 4 ms a listing
        return real(path)

    monkeypatch.setattr(fs, "listdir", slow)
    sizer = TreeSizer(fs, [root])
    assert sizer.step(seconds=0.015) is True   # more to do
    assert now[0] == pytest.approx(0.016)      # four listings, then stopped


def test_a_step_always_makes_progress(fs, tmp_path, monkeypatch):
    """However short the slice, one listing happens: a step that could do
    nothing would let a caller loop for ever getting nowhere."""
    root = _tree(tmp_path, one=(2, 10))
    sizer = TreeSizer(fs, [f"{root}/one"])
    while sizer.step(seconds=0.0):
        pass
    assert sizer.totals[f"{root}/one"] == 20


def test_run_gives_up_rather_than_spinning_for_ever(fs, tmp_path, monkeypatch):
    """The limit is a safety net, not a schedule; it must not hang.

    A slice of ``run``'s is a whole second, so making it bite needs a backend
    slow enough to spend one -- the fake clock here charges 0.6 s a listing,
    which puts two in a slice and leaves the rest for the next.
    """
    from meridian_commander import usage as usage_mod

    root = str(tmp_path / "wide")
    for i in range(6):
        write(f"{root}/d{i}/f", "x" * 10)

    now = [0.0]
    monkeypatch.setattr(usage_mod.time, "monotonic", lambda: now[0])
    real = fs.listdir

    def slow(path):
        now[0] += 0.6
        return real(path)

    monkeypatch.setattr(fs, "listdir", slow)
    sizer = TreeSizer(fs, [root])
    sizer.run(limit=1)                          # two slices, then give up
    assert sizer.finished is False
    assert sizer.totals[root] < 60
    assert sizer.run()[root] == 60              # picking it up again finishes


def test_a_budget_counts_listings_rather_than_time(fs, tmp_path):
    """What a test wants when it is asking about the walk, not the clock."""
    root = _tree(tmp_path, one=(2, 10))
    sizer = TreeSizer(fs, [f"{root}/one"])
    assert sizer.step(budget=1) is True         # the root listed, not its child
    assert sizer.totals[f"{root}/one"] == 0
    assert sizer.step(budget=1) is True
    assert sizer.totals[f"{root}/one"] == 20


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


@pytest.fixture
def unhurried(sized, monkeypatch):
    """Make each listing cost a whole slice, on a clock the test controls.

    The fixture's tree is small enough that one real slice finishes it -- the
    point of the change being measured -- so a walk that is still going has to
    be arranged rather than assumed.
    """
    from meridian_commander import usage as usage_mod

    now = [0.0]
    monkeypatch.setattr(usage_mod.time, "monotonic", lambda: now[0])
    real = sized.fs.listdir

    def slow(path):
        now[0] += 0.02
        return real(path)

    monkeypatch.setattr(sized.fs, "listdir", slow)
    return sized


def test_the_main_loop_keeps_counting_between_keystrokes(app, unhurried):
    """A pane still counting asks for the tight poll; an idle one blocks.

    The interval is what *paces* the walk -- it gets one slice per poll -- so
    a pane that is still counting must not be left waiting the 120 ms a
    terminal plug-in is happy with.
    """
    assert app._tick_plugins() == app.SIZING_POLL_MS
    assert app.SIZING_POLL_MS < app.PLUGIN_POLL_MS
    _finish(unhurried)
    assert app._tick_plugins() is None          # nothing left: block on getch


def test_a_pane_counting_beats_a_plugin_ticking(app, unhurried):
    """Both want the loop; the one doing real work between polls sets it."""
    class _Ticker:
        wants_timer = True

        def tick(self):
            pass

    app.right.plugin = _Ticker()
    assert app._tick_plugins() == app.SIZING_POLL_MS
    _finish(unhurried)
    assert app._tick_plugins() == app.PLUGIN_POLL_MS


def test_a_small_tree_is_finished_before_the_first_poll_is_over(app, sized):
    """The whole point: a local tree of this size costs one slice, not many.

    Before the walk was time-boxed it got eight listings per 120 ms poll --
    67 a second against the fifty thousand the same walk does unpaced.
    """
    assert app._tick_plugins() is None
    photos = next(e for e in sized.entries if e.name == "photos")
    assert sized.size_of(photos) == 4000        # already counted, first slice


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
