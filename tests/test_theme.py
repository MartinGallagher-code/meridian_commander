"""The colour engine and the chrome it draws: roles, glyphs, frames, shadows."""

from __future__ import annotations

import curses
import locale

import pytest

from meridian_commander import theme

from support import with_curses_screen


@pytest.fixture(autouse=True)
def restore_theme():
    """Put the module back the way the rest of the suite expects it."""
    scheme, glyphs = theme.current, theme.GLYPHS
    yield
    theme.GLYPHS = glyphs
    theme.init(scheme)


# -- roles ---------------------------------------------------------------------

def test_every_role_names_colours_the_palette_has():
    for role, (fg, bg, _attr, _mono) in theme.ROLES.items():
        assert fg in theme.EGA, role
        assert bg in theme.EGA, role


def test_a_scheme_only_overrides_roles_that_exist():
    for name, table in theme.SCHEMES.items():
        for role in table:
            assert role in theme.ROLES, f"{name} names an unknown role {role}"


def test_init_hands_out_a_pair_per_colour_combination():
    def run(stdscr):
        theme.init("turbo")
        return theme.in_colour(), theme.next_free_pair(), theme.attr("panel")

    coloured, free, panel = with_curses_screen(10, 40, run)
    assert coloured is True
    # Every role resolved to a pair, and the next one is left for the image
    # viewer -- which is the whole reason the number is published.
    assert 1 < free <= len(theme.ROLES) + 1
    assert panel != curses.A_NORMAL


def test_the_same_colours_share_one_pair():
    def run(stdscr):
        theme.init("turbo")
        # Two roles that differ only in their attributes, not their colours.
        return (theme.attr("menu") & curses.A_COLOR,
                theme.attr("keybar") & curses.A_COLOR)

    menu, keybar = with_curses_screen(10, 40, run)
    assert menu == keybar


def test_bold_carries_the_bright_half_on_an_eight_colour_terminal():
    def run(stdscr):
        theme.init("turbo")
        # "title" is yellow, which is bright brown: on eight colours that has
        # to be spelled with bold, and on sixteen it is a colour of its own.
        return theme.attr("title") & curses.A_BOLD

    assert with_curses_screen(10, 40, run)


def test_a_monochrome_terminal_falls_back_to_attributes(monkeypatch):
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    theme.init("turbo")
    assert theme.in_colour() is False
    assert theme.attr("panelcursor") == curses.A_REVERSE
    assert theme.attr("panel") == curses.A_NORMAL


def test_the_mono_scheme_declines_colour_even_where_there_is_some():
    def run(stdscr):
        theme.init("mono")
        return theme.in_colour(), theme.attr("dialog")

    coloured, dialog = with_curses_screen(10, 40, run)
    assert coloured is False
    assert dialog == curses.A_REVERSE


def test_a_terminal_that_refuses_a_pair_is_not_fatal(monkeypatch):
    def refuse(*args):
        raise curses.error("no colour here")

    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "init_pair", refuse)
    theme.init("turbo")
    assert theme.in_colour() is False
    assert theme.attr("menu") == curses.A_REVERSE


def test_running_out_of_pairs_leaves_the_rest_monochrome(monkeypatch):
    def run(stdscr):
        monkeypatch.setattr(curses, "COLOR_PAIRS", 4)
        theme.init("turbo")
        return theme.next_free_pair()

    assert with_curses_screen(10, 40, run) == 4


def test_a_terminal_that_cannot_say_whether_it_has_colour(monkeypatch):
    def explode():
        raise curses.error("no screen")

    monkeypatch.setattr(curses, "has_colors", explode)
    theme.init("turbo")
    assert theme.in_colour() is False


def test_a_pair_number_curses_will_not_turn_into_an_attribute(monkeypatch):
    """``init_pair`` can succeed on a screen ``color_pair`` still refuses."""
    def refuse(_index):
        raise curses.error("must call initscr() first")

    def run(stdscr):
        monkeypatch.setattr(curses, "color_pair", refuse)
        theme.init("turbo")
        return theme.in_colour(), theme.attr("menu")

    coloured, menu = with_curses_screen(10, 40, run)
    assert coloured is False
    assert menu == curses.A_REVERSE


def test_an_unknown_scheme_falls_back_to_the_default():
    assert theme.init("chartreuse") == theme.DEFAULT_SCHEME
    assert "turbo" in theme.names() and "midnight" in theme.names()


# -- glyphs --------------------------------------------------------------------

def test_the_glyph_sets_agree_on_what_they_can_draw():
    assert set(theme.UNICODE_GLYPHS) == set(theme.ASCII_GLYPHS)


def test_the_ascii_set_is_actually_ascii():
    for name, text in theme.ASCII_GLYPHS.items():
        assert text.isascii(), name


def test_a_latin_1_terminal_gets_the_plain_frames(monkeypatch):
    monkeypatch.setenv("MERIDIAN_ASCII", "1")
    theme.init("turbo")
    assert theme.glyph("v") == "|"
    assert theme.glyph("shade") == "."


def test_an_unknown_glyph_is_a_space():
    assert theme.glyph("nonesuch") == " "


def test_an_encoding_python_has_never_heard_of_gets_the_plain_frames(
        monkeypatch):
    monkeypatch.setattr(locale, "getpreferredencoding",
                        lambda do_setlocale=True: "klingon-1")
    theme.init("turbo")
    assert theme.glyph("v") == "|"


# -- hot keys ------------------------------------------------------------------

@pytest.mark.parametrize("caption, text, index", [
    ("~F~ile", "File", 0),
    ("Sa~v~e as", "Save as", 2),
    ("Plain", "Plain", -1),
    ("half ~open", "half open", -1),      # an unclosed marker is just text
])
def test_a_hot_key_is_read_out_of_its_caption(caption, text, index):
    assert theme.hotkey(caption) == (text, index)
    assert theme.strip_hotkey(caption) == text


def test_the_hot_letter_comes_back_lowercased():
    assert theme.hotkey_letter("~S~ync") == "s"
    assert theme.hotkey_letter("no marker") == ""


# -- drawing -------------------------------------------------------------------

def _screen(rows, cols, fn):
    def run(stdscr):
        theme.init("turbo")
        fn(stdscr)
        return [stdscr.instr(row, 0).decode() for row in range(rows)]

    return with_curses_screen(rows, cols, run)


def test_a_frame_is_drawn_with_its_caption_bracketed():
    rows = _screen(8, 40, lambda s: theme.frame(s, 0, 0, 5, 30, "frame",
                                                title="local:/tmp",
                                                title_role="title"))
    assert rows[0].startswith("╔[■]")
    assert "╡ local:/tmp ╞" in rows[0]
    assert rows[0][29] == "╗"
    assert rows[1][0] == "║" and rows[1][29] == "║"
    assert rows[4].startswith("╚") and rows[4][29] == "╝"


def test_an_inactive_frame_is_drawn_single():
    rows = _screen(8, 40, lambda s: theme.frame(s, 0, 0, 4, 20, "framenc",
                                                title="other", double=False,
                                                close=False))
    assert rows[0].startswith("┌")
    assert "╡ other ╞" in rows[0]
    assert rows[1][0] == "│"


def test_a_footer_sits_in_the_bottom_border():
    rows = _screen(8, 40, lambda s: theme.frame(s, 0, 0, 4, 24, "frame",
                                                footer="12 items"))
    assert "╡ 12 items ╞" in rows[3]


def test_a_caption_too_long_for_its_frame_keeps_the_caption():
    rows = _screen(6, 40,
                   lambda s: theme.frame(s, 0, 0, 3, 14, "frame",
                                         title="an extremely long caption"))
    assert "an extre" in rows[0]
    assert len(rows[0].rstrip()) <= 14


def test_a_frame_too_small_to_draw_is_skipped():
    rows = _screen(4, 20, lambda s: theme.frame(s, 0, 0, 1, 1, "frame"))
    assert rows[0].strip() == ""


def test_a_scrollbar_has_arrows_a_track_and_a_thumb():
    def draw(stdscr):
        theme.scrollbar(stdscr, 0, 0, 6, top=8, visible=6, total=20)

    rows = _screen(8, 10, draw)
    assert rows[0][0] == "▲"
    assert rows[5][0] == "▼"
    track = "".join(row[0] for row in rows[1:5])
    assert "█" in track and "░" in track


def test_a_scrollbar_with_nothing_to_scroll_is_all_track():
    def draw(stdscr):
        theme.scrollbar(stdscr, 0, 0, 5, top=0, visible=9, total=4)

    rows = _screen(6, 10, draw)
    assert "█" not in "".join(row[0] for row in rows[:5])


def test_a_shadow_darkens_what_is_under_it_without_erasing_it():
    def run(stdscr):
        theme.init("turbo")
        for row in range(6):
            stdscr.addstr(row, 0, "abcdefghij")
        theme.shadow(stdscr, 0, 0, 3, 4)
        under = stdscr.inch(1, 4) & curses.A_ATTRIBUTES
        return chr(stdscr.inch(1, 4) & curses.A_CHARTEXT), under

    text, attrs = with_curses_screen(8, 20, run)
    # The character is still there; only its colour changed.
    assert text == "e"
    assert attrs == theme.attr("shadow")


def test_a_shadow_at_the_edge_of_the_screen_is_clipped():
    def run(stdscr):
        theme.init("turbo")
        theme.shadow(stdscr, 3, 15, 4, 6)      # falls off both edges
        return True

    assert with_curses_screen(6, 20, run) is True


def test_a_button_is_captioned_centred_and_shadowed():
    def run(stdscr):
        theme.init("turbo")
        end = theme.button(stdscr, 1, 2, "~O~K", width=10)
        return (stdscr.instr(1, 0).decode(),
                stdscr.inch(1, 12) & curses.A_ATTRIBUTES, end)

    row, shade, end = with_curses_screen(6, 30, run)
    assert "    OK" in row
    assert shade == theme.attr("shadow")
    assert end == 14                          # the next button's column


def test_a_disabled_button_is_greyed_rather_than_green():
    def run(stdscr):
        theme.init("turbo")
        theme.button(stdscr, 0, 0, "~O~K", width=8, disabled=True)
        return stdscr.inch(0, 3) & curses.A_ATTRIBUTES

    assert with_curses_screen(4, 20, run) == theme.attr("dialogdim")


def test_paint_clips_and_pads_to_a_width():
    def run(stdscr):
        theme.init("turbo")
        theme.paint(stdscr, 0, 0, "a very long line indeed", "panel", 8)
        theme.paint(stdscr, 1, 0, "short", "panel", 8)
        return stdscr.instr(0, 0, 12).decode(), stdscr.instr(1, 0, 12).decode()

    long_line, short = with_curses_screen(4, 20, run)
    assert long_line.startswith("a very l")
    assert short.startswith("short   ")


def test_paint_survives_a_write_the_screen_refuses():
    def run(stdscr):
        theme.init("turbo")
        # The bottom-right cell is one curses always refuses.
        theme.paint(stdscr, 3, 0, "x" * 20, "panel", 20)
        return True

    assert with_curses_screen(4, 20, run) is True


def test_a_window_that_refuses_a_background_is_not_fatal():
    class _Refuses:
        def bkgd(self, *args):
            raise curses.error("no background here")

    theme.background(_Refuses(), "panel")     # must not raise


def test_a_shadow_with_no_cells_left_to_darken_does_nothing():
    def run(stdscr):
        theme.init("turbo")
        theme.darken(stdscr, 1, 19, 4)        # only one column left
        theme.darken(stdscr, 1, 20, 4)        # and now none at all
        return stdscr.inch(1, 19) & curses.A_ATTRIBUTES

    assert with_curses_screen(4, 20, run) == theme.attr("shadow")


def test_a_shadow_of_no_width_darkens_nothing():
    """A zero count must not reach chgat, which reads it as "to end of line"."""
    def run(stdscr):
        theme.init("turbo")
        stdscr.addstr(1, 0, "abcdefghij")
        theme.shadow(stdscr, 0, 0, 1, 0)
        return stdscr.inch(1, 5) & curses.A_ATTRIBUTES

    assert with_curses_screen(4, 20, run) != theme.attr("shadow")


def test_a_shadow_curses_refuses_to_recolour_is_not_fatal():
    class _Refuses:
        def getmaxyx(self):
            return (10, 40)

        def chgat(self, *args):
            raise curses.error("no such cell")

    theme.darken(_Refuses(), 1, 1, 4)         # must not raise


def test_a_scrollbar_with_no_room_for_its_arrows_is_skipped():
    def run(stdscr):
        theme.init("turbo")
        theme.scrollbar(stdscr, 0, 0, 1, top=0, visible=1, total=9)
        return stdscr.instr(0, 0).decode()

    assert with_curses_screen(4, 20, run).strip() == ""


def test_a_progress_bar_fills_from_the_left():
    assert theme.progress_bar(10, 0.0) == "░" * 10
    assert theme.progress_bar(10, 1.0) == "█" * 10
    assert theme.progress_bar(10, 0.5) == "█████░░░░░"
    assert theme.progress_bar(0, 0.5) == ""
    # Nonsense fractions are clamped rather than drawn past the end.
    assert len(theme.progress_bar(6, 4.0)) == 6
