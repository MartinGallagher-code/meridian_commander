"""The plugin context and the two plugin base classes."""

from __future__ import annotations

import curses

import pytest

from meridian_commander.plugin_api import (
    InputOutputPlugin,
    PanePlugin,
    PluginContext,
)

from support import with_curses_screen, write


@pytest.fixture
def ctx(app):
    """A context whose own pane is the left one and other pane the right."""
    return PluginContext(app=app, own_panel=app.left, other_panel=app.right)


# -- PluginContext -------------------------------------------------------------

def test_context_exposes_both_panes(ctx, app, tmp_path):
    assert ctx.other_fs is app.right.fs
    assert ctx.other_path == str(tmp_path / "right")
    assert ctx.own_fs is app.left.fs
    assert ctx.own_path == str(tmp_path / "left")


def test_other_entries_hides_the_parent_entry(ctx):
    names = [e.name for e in ctx.other_entries()]
    assert ".." not in names
    assert set(names) == {"sub", "file.txt"}


def test_other_selected_follows_the_pane(ctx, app):
    app.right.selected = {"file.txt"}
    assert [e.name for e in ctx.other_selected()] == ["file.txt"]


def test_refresh_other_picks_up_a_new_file(ctx, app, tmp_path):
    write(str(tmp_path / "right" / "added.txt"), "new")
    assert "added.txt" not in [e.name for e in ctx.other_entries()]
    ctx.refresh_other()
    assert "added.txt" in [e.name for e in ctx.other_entries()]


def test_set_status_reaches_the_application(ctx, app):
    ctx.set_status("halfway through")
    assert app.message == "halfway through"


def test_set_status_on_an_app_that_cannot_take_it_is_ignored(app):
    ctx = PluginContext(app=object(), own_panel=app.left, other_panel=app.right)
    ctx.set_status("nowhere to put this")     # must not raise


def test_focus_other_switches_the_active_pane(ctx, app):
    assert app.active is app.left
    ctx.focus_other()
    assert app.active is app.right


def test_focus_other_on_an_app_that_cannot_take_it_is_ignored(app):
    class _Frozen:
        __slots__ = ()

    ctx = PluginContext(app=_Frozen(), own_panel=app.left, other_panel=app.right)
    ctx.focus_other()                          # must not raise


# -- PanePlugin ----------------------------------------------------------------

def test_the_base_plugin_demands_an_implementation(ctx):
    plugin = PanePlugin(ctx)
    with pytest.raises(NotImplementedError):
        plugin.draw(None, 0, 0, 10, 10)
    with pytest.raises(NotImplementedError):
        plugin.handle_key(ord("x"))


def test_on_exit_is_a_no_op_by_default(ctx):
    assert PanePlugin(ctx).on_exit() is None


def test_put_clips_pads_and_survives_the_screen_edge():
    def draw(stdscr):
        # Clipped to the width, and padded out to it.
        PanePlugin.put(stdscr, 0, 0, 10, "0123456789abc")
        PanePlugin.put(stdscr, 1, 0, 10, "short")
        PanePlugin.put(stdscr, 2, 0, 10, "nopad", pad=False)
        # A zero or negative width writes nothing at all.
        PanePlugin.put(stdscr, 3, 0, 0, "invisible")
        PanePlugin.put(stdscr, 3, 0, -4, "invisible")
        # Writing past the bottom-right corner is refused by curses and
        # swallowed rather than crashing the pane.
        PanePlugin.put(stdscr, 4, 0, 20, "x" * 20)
        return (stdscr.instr(0, 0, 13).decode(),
                stdscr.instr(1, 0, 10).decode(),
                stdscr.instr(3, 0, 9).decode())

    clipped, padded, blank = with_curses_screen(5, 20, draw)
    assert clipped.startswith("0123456789")
    assert clipped[10:13].strip() == ""
    assert padded == "short     "
    assert blank.strip() == ""


# -- InputOutputPlugin ---------------------------------------------------------

class _Echo(InputOutputPlugin):
    name = "Echo"
    description = "echoes what it is given"
    prompt = "echo> "
    greeting = "welcome"

    def process(self, line):
        if line == "boom":
            raise ValueError("that went wrong")
        if line == "list":
            return ["one", "two"]
        if line == "quiet":
            return None
        if line == "number":
            return 42
        return f"you said {line}"


def _type(plugin, text):
    for ch in text:
        plugin.handle_key(ord(ch))


def test_an_io_plugin_must_implement_process(ctx):
    with pytest.raises(NotImplementedError):
        InputOutputPlugin(ctx).process("anything")


def test_the_greeting_is_printed_when_the_plugin_opens(ctx):
    assert _Echo(ctx).output == ["welcome"]


def test_a_plugin_with_no_greeting_starts_empty(ctx):
    class _Silent(_Echo):
        greeting = ""

    assert _Silent(ctx).output == []


def test_typing_and_submitting_a_line(ctx):
    plugin = _Echo(ctx)
    _type(plugin, "hello")
    assert plugin.handle_key(10) is True
    assert "echo> hello" in plugin.output
    assert "you said hello" in plugin.output
    # The input line is cleared ready for the next entry.
    assert plugin.buf == [] and plugin.pos == 0


def test_a_result_list_is_printed_line_by_line(ctx):
    plugin = _Echo(ctx)
    _type(plugin, "list")
    plugin.handle_key(10)
    assert plugin.output[-2:] == ["one", "two"]


def test_a_result_of_none_prints_nothing_extra(ctx):
    plugin = _Echo(ctx)
    before = len(plugin.output)
    _type(plugin, "quiet")
    plugin.handle_key(10)
    # Only the echoed prompt line was added.
    assert len(plugin.output) == before + 1


def test_a_result_that_is_neither_string_nor_iterable_is_stringified(ctx):
    plugin = _Echo(ctx)
    _type(plugin, "number")
    plugin.handle_key(10)
    assert plugin.output[-1] == "42"


def test_an_exception_in_process_is_shown_not_raised(ctx):
    plugin = _Echo(ctx)
    _type(plugin, "boom")
    plugin.handle_key(10)
    assert plugin.output[-1] == "error: that went wrong"
    assert plugin.busy is False


def test_print_splits_multiple_lines_and_keeps_blank_ones(ctx):
    plugin = _Echo(ctx)
    plugin.print("one\ntwo")
    assert plugin.output[-2:] == ["one", "two"]
    plugin.print("")
    assert plugin.output[-1] == ""


def test_editing_the_input_line(ctx):
    plugin = _Echo(ctx)
    _type(plugin, "hello")
    plugin.handle_key(curses.KEY_LEFT)
    plugin.handle_key(curses.KEY_LEFT)
    plugin.handle_key(curses.KEY_BACKSPACE)      # remove the second 'l'
    assert "".join(plugin.buf) == "helo"
    plugin.handle_key(curses.KEY_HOME)
    plugin.handle_key(curses.KEY_DC)             # remove the leading 'h'
    assert "".join(plugin.buf) == "elo"
    plugin.handle_key(curses.KEY_END)
    plugin.handle_key(curses.KEY_RIGHT)          # already at the end
    assert plugin.pos == 3


def test_backspace_and_delete_at_the_ends_do_nothing(ctx):
    plugin = _Echo(ctx)
    plugin.handle_key(curses.KEY_BACKSPACE)      # empty line
    plugin.handle_key(curses.KEY_DC)
    plugin.handle_key(curses.KEY_LEFT)
    assert plugin.buf == [] and plugin.pos == 0


def test_ctrl_u_clears_the_input_line(ctx):
    plugin = _Echo(ctx)
    _type(plugin, "discard me")
    plugin.handle_key(21)
    assert plugin.buf == [] and plugin.pos == 0


def test_a_non_ascii_key_is_inserted(ctx):
    plugin = _Echo(ctx)
    plugin.handle_key(ord("é"))
    assert "".join(plugin.buf) == "é"


def test_a_key_that_is_not_a_character_is_swallowed(ctx):
    plugin = _Echo(ctx)
    plugin.handle_key(0x110000)                  # outside the Unicode range
    assert plugin.buf == []
    # An unrecognised control key is consumed without changing anything.
    assert plugin.handle_key(1) is True
    assert plugin.buf == []


def test_history_recalls_previous_lines(ctx):
    plugin = _Echo(ctx)
    for line in ("first", "second"):
        _type(plugin, line)
        plugin.handle_key(10)

    plugin.handle_key(curses.KEY_UP)
    assert "".join(plugin.buf) == "second"
    plugin.handle_key(curses.KEY_UP)
    assert "".join(plugin.buf) == "first"
    plugin.handle_key(curses.KEY_UP)             # already at the oldest
    assert "".join(plugin.buf) == "first"
    plugin.handle_key(curses.KEY_DOWN)
    assert "".join(plugin.buf) == "second"
    # Past the newest entry the line is cleared, ready for something new.
    plugin.handle_key(curses.KEY_DOWN)
    assert plugin.buf == []


def test_history_down_before_any_recall_does_nothing(ctx):
    plugin = _Echo(ctx)
    _type(plugin, "only")
    plugin.handle_key(10)
    plugin.handle_key(curses.KEY_DOWN)
    assert plugin.buf == []


def test_history_with_nothing_recorded_does_nothing(ctx):
    plugin = _Echo(ctx)
    plugin.handle_key(curses.KEY_UP)
    assert plugin.buf == []


def test_blank_lines_are_not_recorded_in_history(ctx):
    plugin = _Echo(ctx)
    plugin.handle_key(10)                        # submit an empty line
    _type(plugin, "   ")
    plugin.handle_key(10)
    assert plugin.history == []


def test_scrolling_the_output(ctx):
    plugin = _Echo(ctx)
    for i in range(30):
        plugin.print(f"line {i}")
    assert plugin.scroll == 0
    plugin.handle_key(curses.KEY_PPAGE)
    assert plugin.scroll == 10
    plugin.handle_key(curses.KEY_PPAGE)
    assert plugin.scroll == 20
    plugin.handle_key(curses.KEY_NPAGE)
    assert plugin.scroll == 10
    for _ in range(5):
        plugin.handle_key(curses.KEY_NPAGE)
    assert plugin.scroll == 0                    # never scrolls past the end


def test_scrolling_up_is_bounded_by_the_output(ctx):
    plugin = _Echo(ctx)
    for _ in range(20):
        plugin.handle_key(curses.KEY_PPAGE)
    assert plugin.scroll == max(0, len(plugin.output) - 1)


def test_escape_closes_and_tab_is_handed_back(ctx):
    plugin = _Echo(ctx)
    assert plugin.handle_key(9) is None          # Tab: the app switches panes
    assert plugin.handle_key(27) is False        # Esc: close the plugin


def test_drawing_the_plugin_pane(ctx):
    plugin = _Echo(ctx)
    plugin.print("some output")
    _type(plugin, "typing")

    def draw(stdscr):
        plugin.draw(stdscr, 0, 0, 20, 40)
        return "\n".join(stdscr.instr(row, 0, 40).decode()
                         for row in range(20))

    rendered = with_curses_screen(24, 60, draw)
    assert "[plugin] Echo" in rendered
    assert "some output" in rendered
    assert "echo> typing" in rendered
    assert "Enter run" in rendered


def test_drawing_shows_a_busy_marker_while_working(ctx):
    plugin = _Echo(ctx)
    plugin.busy = True

    def draw(stdscr):
        plugin.draw(stdscr, 0, 0, 20, 40)
        return stdscr.instr(19, 0, 40).decode()

    assert "working" in with_curses_screen(24, 60, draw)


def test_drawing_in_a_pane_too_short_for_an_output_area(ctx):
    plugin = _Echo(ctx)

    def draw(stdscr):
        plugin.draw(stdscr, 0, 0, 3, 40)         # h - 5 leaves nothing
        return stdscr.instr(0, 0, 40).decode()

    # It draws the title and gives up rather than computing a negative height.
    assert "[plugin] Echo" in with_curses_screen(24, 60, draw)


def test_drawing_wraps_a_long_input_line_onto_the_second_row(ctx):
    plugin = _Echo(ctx)
    _type(plugin, "x" * 90)

    def draw(stdscr):
        plugin.draw(stdscr, 0, 0, 20, 40)
        # h - 5 output rows put the separator at 16 and the input at 17/18.
        return (stdscr.instr(17, 0, 40).decode(),
                stdscr.instr(18, 0, 40).decode())

    first, second = with_curses_screen(24, 60, draw)
    assert first.startswith("echo> ")
    assert "x" in second        # the overflow continues on the next row
