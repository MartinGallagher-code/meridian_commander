"""The modal dialogs, driven on real curses screens."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import dialogs, theme

from support import (
    _ScriptedWindow,
    run_menu,
    scripted_menu,
    with_curses_screen,
)


def _run(monkeypatch, fn, keys, rows=24, cols=70, fail_draws=False):
    """Run a dialog with its window's keystrokes scripted."""
    captured = scripted_menu(monkeypatch, keys, fail_draws)
    result = with_curses_screen(rows, cols, fn)
    return result, captured.get("window")


def _text(window) -> str:
    return "\n".join(str(draw) for draw in window.drawn)


# -- geometry ------------------------------------------------------------------

def test_a_window_is_centred_and_clipped_to_the_screen():
    def check(stdscr):
        small = dialogs._center(stdscr, 6, 20)
        oversized = dialogs._center(stdscr, 500, 500)
        return small.getmaxyx(), small.getbegyx(), oversized.getmaxyx()

    size, origin, clipped = with_curses_screen(20, 60, check)
    assert size == (6, 20)
    assert origin == ((20 - 6) // 2, (60 - 20) // 2)
    # Never larger than the screen it has to fit inside.
    assert clipped == (18, 58)


def test_the_title_is_bracketed_and_truncated():
    def check(stdscr):
        win = dialogs._center(stdscr, 5, 20)
        dialogs._box(win, "Title")
        titled = win.instr(0, 0).decode()
        dialogs._box(win, "")
        untitled = win.instr(0, 0).decode()
        wide = dialogs._center(stdscr, 5, 12)
        dialogs._box(wide, "A very long title indeed")
        return titled, untitled, wide.instr(0, 0).decode()

    titled, untitled, long_title = with_curses_screen(20, 60, check)
    # The caption sits in the top border between Turbo Vision's brackets,
    # after the close box in the corner.
    assert "\u2561 Title \u255e" in titled
    assert titled.startswith("\u2554[\u25a0]")
    assert "Title" not in untitled
    # An over-long title is cut to fit rather than overflowing the frame.
    assert "A very" in long_title


# -- message -------------------------------------------------------------------

def test_message_shows_its_text_and_waits_for_a_key(monkeypatch):
    _, window = _run(monkeypatch,
                     lambda s: dialogs.message(s, "Note", "all done"),
                     [ord(" ")])
    assert "all done" in _text(window)
    assert "press any key" in _text(window)


def test_message_renders_several_lines(monkeypatch):
    _, window = _run(monkeypatch,
                     lambda s: dialogs.message(s, "Note", "one\ntwo\nthree"),
                     [10])
    text = _text(window)
    assert "one" in text and "two" in text and "three" in text


def test_an_error_message_is_emphasised(monkeypatch):
    _, window = _run(monkeypatch,
                     lambda s: dialogs.message(s, "Error", "it broke",
                                               error=True),
                     [27])
    shouted = [d for d in window.drawn
               if len(d) > 3 and d[3] == theme.attr("dialogerror")]
    assert any(d[2] == "it broke" for d in shouted)


def test_a_message_offers_an_ok_button(monkeypatch):
    _, window = _run(monkeypatch,
                     lambda s: dialogs.message(s, "Note", "all done"), [27])
    drawn = _text(window)
    assert "OK" in drawn


def test_message_clips_text_too_tall_for_the_screen(monkeypatch):
    body = "\n".join(f"line {i}" for i in range(60))
    _, window = _run(monkeypatch, lambda s: dialogs.message(s, "Long", body),
                     [10], rows=10)
    # It draws what fits and still offers the dismiss hint.
    assert "press any key" in _text(window)


def test_message_survives_a_window_that_refuses_to_draw(monkeypatch):
    """A refused body write must not take the dialog down."""
    _run(monkeypatch, lambda s: dialogs.message(s, "Note", "one\ntwo"),
         [10], fail_draws=lambda y, x: y in (1, 2))


# -- confirm -------------------------------------------------------------------

@pytest.mark.parametrize("key, expected", [
    (ord("y"), True),
    (ord("Y"), True),
    (ord("n"), False),
    (ord("N"), False),
    (27, False),
])
def test_confirm_answers_by_letter(monkeypatch, key, expected):
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.confirm(s, "Delete", "Sure?"), [key])
    assert answer is expected


def test_confirm_defaults_to_no_on_enter(monkeypatch):
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.confirm(s, "Delete", "Sure?"), [10])
    assert answer is False


def test_confirm_can_default_to_yes(monkeypatch):
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.confirm(s, "Quit", "Exit?",
                                               default_yes=True), [10])
    assert answer is True


@pytest.mark.parametrize("toggle", [curses.KEY_LEFT, curses.KEY_RIGHT, 9])
def test_confirm_selection_can_be_moved_then_accepted(monkeypatch, toggle):
    answer, window = _run(monkeypatch,
                          lambda s: dialogs.confirm(s, "Delete", "Sure?"),
                          [toggle, 13])
    assert answer is True
    # Turbo Vision push buttons: the caption, its accelerator picked out, and
    # no brackets -- the colour and the shadow are what make them buttons.
    drawn = _text(window)
    assert "Yes" in drawn and "No" in drawn


def test_confirm_shows_several_lines(monkeypatch):
    _, window = _run(monkeypatch,
                     lambda s: dialogs.confirm(s, "Delete", "one\ntwo"), [27])
    assert "one" in _text(window) and "two" in _text(window)


def test_confirm_survives_a_window_that_refuses_to_draw(monkeypatch):
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.confirm(s, "Delete", "a\nb"),
                     [ord("y")], fail_draws=lambda y, x: y in (1, 2))
    assert answer is True


# -- prompt --------------------------------------------------------------------

def _typed(text):
    return [ord(c) for c in text]


def test_prompt_returns_what_was_typed(monkeypatch):
    answer, window = _run(monkeypatch,
                          lambda s: dialogs.prompt(s, "Name", "New name:"),
                          _typed("hello") + [10])
    assert answer == "hello"
    assert "New name:" in _text(window)
    assert "Enter=OK" in _text(window)


def test_prompt_escape_cancels(monkeypatch):
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.prompt(s, "Name", "New name:"), [27])
    assert answer is None


def test_prompt_starts_from_a_default(monkeypatch):
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.prompt(s, "Name", "New name:",
                                              default="preset"), [10])
    assert answer == "preset"


def test_prompt_editing_keys(monkeypatch):
    keys = _typed("hello") + [
        curses.KEY_LEFT, curses.KEY_LEFT, curses.KEY_BACKSPACE,
        curses.KEY_HOME, curses.KEY_DC,
        curses.KEY_END, curses.KEY_RIGHT,
        10,
    ]
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.prompt(s, "Name", "Edit:"), keys)
    # "hello" -> backspace before "lo" -> "helo" -> delete first -> "elo"
    assert answer == "elo"


def test_prompt_edits_at_the_ends_do_nothing(monkeypatch):
    keys = [curses.KEY_BACKSPACE, curses.KEY_DC, curses.KEY_LEFT, 10]
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.prompt(s, "Name", "Edit:"), keys)
    assert answer == ""


def test_prompt_masks_a_password(monkeypatch):
    answer, window = _run(
        monkeypatch,
        lambda s: dialogs.prompt(s, "Login", "Password:", is_password=True),
        _typed("hunter2") + [10])
    assert answer == "hunter2"
    drawn = _text(window)
    assert "hunter2" not in drawn
    assert "*******" in drawn


def test_prompt_scrolls_a_value_wider_than_the_field(monkeypatch):
    long_value = "x" * 200
    answer, window = _run(monkeypatch,
                          lambda s: dialogs.prompt(s, "Path", "Go to:",
                                                   default=long_value),
                          [10])
    assert answer == long_value
    # Only the tail around the cursor is on screen: no write is wider than
    # the input field, whatever the value's length.
    widest = max(len(d[2]) for d in window.drawn if len(d) > 2)
    assert widest < 60


def test_prompt_ignores_keys_it_does_not_handle(monkeypatch):
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.prompt(s, "Name", "Edit:"),
                     [curses.KEY_F5, ord("a"), 10])
    assert answer == "a"


# -- menu ----------------------------------------------------------------------

def test_menu_returns_the_chosen_index(monkeypatch):
    choice, _ = run_menu(monkeypatch, 24, ["alpha", "beta", "gamma"],
                         [curses.KEY_DOWN, 10])
    assert choice == 1


def test_menu_escape_chooses_nothing(monkeypatch):
    choice, _ = run_menu(monkeypatch, 24, ["alpha", "beta"], [27])
    assert choice is None


def test_menu_scrolls_a_list_taller_than_the_screen(monkeypatch):
    options = [f"preset {i}" for i in range(40)]
    choice, drawn = run_menu(monkeypatch, 12, options, [curses.KEY_END, 10])
    assert choice == 39
    assert any("preset 39" in str(d) for d in drawn)
    assert any(" 40/40 " in str(d) for d in drawn)


def test_menu_paging_and_wrapping(monkeypatch):
    options = [f"preset {i}" for i in range(40)]
    keys = [curses.KEY_NPAGE, curses.KEY_NPAGE, curses.KEY_PPAGE,
            curses.KEY_HOME, curses.KEY_UP, curses.KEY_DOWN, 10]
    choice, drawn = run_menu(monkeypatch, 12, options, keys)
    assert choice == 0
    assert any(" 1/40 " in str(d) for d in drawn)


def test_menu_on_a_short_list_shows_no_counter(monkeypatch):
    choice, drawn = run_menu(monkeypatch, 24, ["alpha", "beta"],
                             [ord("j"), ord("k"), ord("j"), 10])
    assert choice == 1
    assert not any("/2 " in str(d) for d in drawn)


def test_menu_survives_a_window_that_refuses_to_draw(monkeypatch):
    options = [f"preset {i}" for i in range(40)]
    choice, drawn = run_menu(monkeypatch, 12, options,
                             [curses.KEY_DOWN, 10], fail_draws=True)
    assert choice == 1
    assert drawn


def test_menu_ignores_keys_it_does_not_handle(monkeypatch):
    choice, _ = run_menu(monkeypatch, 24, ["alpha", "beta"],
                         [curses.KEY_F5, curses.KEY_DOWN, 10])
    assert choice == 1


# -- menu shortcut letters -----------------------------------------------------

def test_a_shortcut_letter_chooses_without_pressing_enter(monkeypatch):
    """One keystroke, no Enter -- the whole point of the letter."""
    choice, _ = run_menu(monkeypatch, 24, ["alpha", "beta", "gamma"],
                         [ord("g")], accel=["a", "b", "g"])
    assert choice == 2


def test_a_shortcut_letter_answers_in_either_case(monkeypatch):
    choice, _ = run_menu(monkeypatch, 24, ["alpha", "beta"],
                         [ord("B")], accel=["a", "b"])
    assert choice == 1


def test_the_letters_are_drawn_beside_their_options(monkeypatch):
    _choice, drawn = run_menu(monkeypatch, 24, ["work", "photos"],
                              [27], accel=["w", "p"])
    assert any("w  work" in str(d) for d in drawn)
    assert any("p  photos" in str(d) for d in drawn)


def test_an_option_without_a_letter_still_lines_up(monkeypatch):
    """A blank gutter, so one shortcut-less row does not shift the column."""
    _choice, drawn = run_menu(monkeypatch, 24, ["work", "nameless"],
                              [27], accel=["w", None])
    assert any("w  work" in str(d) for d in drawn)
    assert any("   nameless" in str(d) for d in drawn)


def test_a_letter_nobody_was_given_does_nothing(monkeypatch):
    choice, _ = run_menu(monkeypatch, 24, ["alpha", "beta"],
                         [ord("z"), curses.KEY_DOWN, 10], accel=["a", "b"])
    assert choice == 1


def test_j_and_k_still_move_when_letters_are_offered(monkeypatch):
    """Navigation keeps its keys; that is why they are never handed out."""
    choice, _ = run_menu(monkeypatch, 24, ["alpha", "beta", "gamma"],
                         [ord("j"), ord("j"), ord("k"), 10],
                         accel=["a", "b", "g"])
    assert choice == 1


# -- assigning the letters -----------------------------------------------------

def test_a_name_gets_its_own_initial():
    assert dialogs.accelerators(["work", "photos", "music"]) == ["w", "p", "m"]


def test_a_taken_initial_falls_back_to_the_alphabet():
    keys = dialogs.accelerators(["alpha", "avocado"])
    assert keys[0] == "a"
    assert keys[1] not in (None, "a")


def test_initials_are_handed_out_before_any_fallback():
    """A name with no usable initial cannot take one another name is named for."""
    keys = dialogs.accelerators(["1st", "alpha"])
    assert keys[1] == "a"
    assert keys[0] not in (None, "a")


def test_the_navigation_keys_are_never_handed_out():
    keys = dialogs.accelerators(["jam", "kite"])
    assert "j" not in keys and "k" not in keys
    assert None not in keys


def test_reserved_letters_are_left_alone():
    keys = dialogs.accelerators(["scratch", "downloads"], reserved="sdc")
    assert not {"s", "d", "c"} & set(keys)


def test_a_list_longer_than_the_alphabet_runs_out_honestly():
    """24 letters to give (j and k are spoken for); the rest get none."""
    keys = dialogs.accelerators([f"item{i}" for i in range(30)])
    assert len([k for k in keys if k]) == 24
    assert keys[-6:] == [None] * 6
    given = [k for k in keys if k]
    assert len(set(given)) == len(given)     # and never the same letter twice


# -- drop-down menus -----------------------------------------------------------

def _dropdown(monkeypatch, items, keys, rows=24, cols=70):
    """Run a drop-down with its window's keystrokes scripted."""
    real_newwin = curses.newwin
    captured: dict = {}

    def fake_newwin(*args, **kwargs):
        window = _ScriptedWindow(real_newwin(*args, **kwargs), keys)
        captured["window"] = window
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    chosen = with_curses_screen(
        rows, cols, lambda stdscr: dialogs.dropdown(stdscr, items, 1, 4))
    return chosen, captured.get("window")


ITEMS = [
    {"label": "~V~iew", "name": "view", "key": "F3"},
    {"label": "~E~dit", "name": "edit"},
    {"sep": True},
    {"label": "~D~elete", "name": "delete", "disabled": True},
    {"label": "~H~idden", "name": "hidden", "checked": True},
]


def test_a_drop_down_returns_the_entry_that_was_chosen(monkeypatch):
    chosen, window = _dropdown(monkeypatch, ITEMS, [10])
    assert chosen == "view"
    drawn = _text(window)
    assert "View" in drawn and "F3" in drawn


def test_a_drop_down_skips_separators_and_disabled_entries(monkeypatch):
    # Down three times from View: Edit, past the rule and the dead entry,
    # to Hidden.
    chosen, _ = _dropdown(monkeypatch, ITEMS,
                          [curses.KEY_DOWN, curses.KEY_DOWN, 10])
    assert chosen == "hidden"


def test_a_drop_down_wraps_at_the_ends(monkeypatch):
    chosen, _ = _dropdown(monkeypatch, ITEMS, [curses.KEY_UP, 10])
    assert chosen == "hidden"
    chosen, _ = _dropdown(monkeypatch, ITEMS,
                          [curses.KEY_END, curses.KEY_HOME, 10])
    assert chosen == "view"


def test_a_drop_down_answers_to_an_accelerator(monkeypatch):
    chosen, _ = _dropdown(monkeypatch, ITEMS, [ord("E")])
    assert chosen == "edit"


def test_a_disabled_entry_cannot_be_chosen_by_its_letter(monkeypatch):
    chosen, _ = _dropdown(monkeypatch, ITEMS, [ord("d"), 27])
    assert chosen is None


def test_escape_closes_a_drop_down(monkeypatch):
    chosen, _ = _dropdown(monkeypatch, ITEMS, [27])
    assert chosen is None


@pytest.mark.parametrize("key, expected", [
    (curses.KEY_LEFT, dialogs.PREVIOUS_MENU),
    (curses.KEY_RIGHT, dialogs.NEXT_MENU),
])
def test_arrowing_off_the_sides_asks_for_the_next_menu(monkeypatch, key,
                                                       expected):
    chosen, _ = _dropdown(monkeypatch, ITEMS, [key])
    assert chosen == expected


def test_a_ticked_entry_is_marked(monkeypatch):
    _chosen, window = _dropdown(monkeypatch, ITEMS, [27])
    assert any(d[2] == theme.glyph("check") for d in window.drawn
               if len(d) > 2)


def test_an_empty_drop_down_opens_nothing(monkeypatch):
    assert with_curses_screen(
        20, 60, lambda stdscr: dialogs.dropdown(stdscr, [], 1, 1)) is None


def test_a_drop_down_near_the_bottom_opens_upwards(monkeypatch):
    """It has to stay on the desktop; there is nothing below the last row."""
    real_newwin = curses.newwin
    where: list[tuple] = []

    def fake_newwin(h, w, y, x):
        where.append((h, y))
        return _ScriptedWindow(real_newwin(h, w, y, x), [27])

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    with_curses_screen(10, 40,
                       lambda stdscr: dialogs.dropdown(stdscr, ITEMS, 8, 2))
    height, top = where[0]
    assert top + height <= 10


# -- connect dialog ------------------------------------------------------------

class _Answers:
    """Scripted menu and prompt answers for the connect dialog."""

    def __init__(self, monkeypatch, menu_choice, prompts):
        self.prompts = list(prompts)
        self.labels: list[str] = []
        monkeypatch.setattr(dialogs, "menu",
                            lambda stdscr, title, options: menu_choice)
        monkeypatch.setattr(dialogs, "prompt", self._prompt)

    def _prompt(self, stdscr, title, label, default="", is_password=False):
        self.labels.append(label)
        answer = self.prompts.pop(0)
        return default if answer is ... else answer


@pytest.mark.parametrize("choice", [None, 4])
def test_connect_dialog_can_be_cancelled_at_the_menu(monkeypatch, choice):
    _Answers(monkeypatch, choice, [])
    assert dialogs.connect_dialog(None) is None


def test_connect_dialog_local_needs_nothing_more(monkeypatch):
    _Answers(monkeypatch, 0, [])
    assert dialogs.connect_dialog(None) == {"scheme": "local"}


def test_connect_dialog_collects_sftp_details(monkeypatch):
    _Answers(monkeypatch, 1, ["web1", "deploy", "2222", "/k/id", "secret"])
    assert dialogs.connect_dialog(None) == {
        "scheme": "sftp", "host": "web1", "username": "deploy", "port": 2222,
        "key_filename": "/k/id", "password": "secret",
    }


def test_connect_dialog_splits_a_user_at_host(monkeypatch):
    answers = _Answers(monkeypatch, 2, [..., ..., "", "", ""])
    answers.prompts[0] = "deploy@web1"
    result = dialogs.connect_dialog(None)
    assert result["scheme"] == "ssh"
    assert result["host"] == "web1"
    # The user part is offered as the default for the username prompt.
    assert result["username"] == "deploy"


def test_connect_dialog_defaults_the_ssh_port(monkeypatch):
    _Answers(monkeypatch, 1, ["web1", "", "", "", ""])
    result = dialogs.connect_dialog(None)
    assert result["port"] == 22
    # Blank optional fields become None rather than empty strings.
    assert result["username"] is None
    assert result["key_filename"] is None
    assert result["password"] is None


def test_connect_dialog_falls_back_on_a_nonsense_ssh_port(monkeypatch):
    _Answers(monkeypatch, 1, ["web1", "", "not-a-number", "", ""])
    assert dialogs.connect_dialog(None)["port"] == 22


def test_connect_dialog_collects_ftp_details(monkeypatch):
    _Answers(monkeypatch, 3, ["files.example", "anonymous", "2121", "pw"])
    assert dialogs.connect_dialog(None) == {
        "scheme": "ftp", "host": "files.example", "username": "anonymous",
        "port": 2121, "password": "pw",
    }


def test_connect_dialog_falls_back_on_a_nonsense_ftp_port(monkeypatch):
    _Answers(monkeypatch, 3, ["h", "anonymous", "wrong", ""])
    result = dialogs.connect_dialog(None)
    assert result["port"] == 21
    assert result["password"] == ""


@pytest.mark.parametrize("stop_at", [0, 1, 2, 3, 4])
def test_connect_dialog_cancels_at_any_ssh_prompt(monkeypatch, stop_at):
    answers = ["web1", "deploy", "22", "/k", "pw"]
    answers[stop_at] = None
    _Answers(monkeypatch, 1, answers)
    assert dialogs.connect_dialog(None) is None


@pytest.mark.parametrize("stop_at", [0, 1, 2, 3])
def test_connect_dialog_cancels_at_any_ftp_prompt(monkeypatch, stop_at):
    answers = ["h", "anonymous", "21", "pw"]
    answers[stop_at] = None
    _Answers(monkeypatch, 3, answers)
    assert dialogs.connect_dialog(None) is None


# -- progress ------------------------------------------------------------------

def test_progress_draws_a_bar_and_a_percentage():
    def run(stdscr):
        progress = dialogs.ProgressDialog(stdscr, "Copying")
        progress.set_overall("2 of 4 files")
        progress.update(50, 100, "big.iso")
        rendered = "\n".join(progress.win.instr(row, 0).decode()
                             for row in range(8))
        progress.close()
        return rendered

    rendered = with_curses_screen(20, 70, run)
    assert "2 of 4 files" in rendered
    assert "big.iso" in rendered
    assert " 50%" in rendered
    # Half a bar: blocks over the shaded track they run along.
    assert theme.glyph("block") in rendered and theme.glyph("shade") in rendered
    assert "Esc/Q = cancel" in rendered


def test_progress_with_no_total_marches_instead_of_sitting_at_zero():
    """A directory scan has no denominator; a bar pinned at 0% reads as a hang."""

    def run(stdscr):
        progress = dialogs.ProgressDialog(stdscr, "Scanning")
        frames = []
        for _ in range(4):
            progress.update(0, 0, "left: 1,000 files")
            frames.append(progress.win.instr(5, 0).decode())
        progress.close()
        return frames

    frames = with_curses_screen(20, 70, run)
    # No percentage -- there is nothing to be a percentage of ...
    assert not any("%" in f for f in frames)
    # ... and the block is somewhere different each time, which is the signal
    # that something is still happening.
    block = theme.glyph("block")
    assert len({f.index(block) for f in frames}) == len(frames)


@pytest.mark.parametrize("width, tick, expected", [
    (0, 0, ""),                       # no room at all
    (4, 0, "BBBB"),                   # narrower than the block: fill it
    (8, 3, "BBBBBBBB"),
    (12, 0, "BBBBBBBB----"),
    (12, 2, "--BBBBBBBB--"),
    (12, 4, "----BBBBBBBB"),          # at the far end
    (12, 5, "---BBBBBBBB-"),          # and turns around rather than wrapping
    (12, 8, "BBBBBBBB----"),          # back to the start: one full cycle
])
def test_marching_bar(width, tick, expected):
    """``B`` stands for the block and ``-`` for the track it travels along."""
    wanted = (expected.replace("B", theme.glyph("block"))
              .replace("-", theme.glyph("shade")))
    assert dialogs._marching_bar(width, tick) == wanted
    if width:
        assert len(dialogs._marching_bar(width, tick)) == width


def test_progress_clamps_an_over_long_count():
    def run(stdscr):
        progress = dialogs.ProgressDialog(stdscr, "Copying")
        progress.update(500, 100, "overshoot")
        rendered = progress.win.instr(5, 0).decode()
        progress.close()
        return rendered

    rendered = with_curses_screen(20, 70, run)
    assert "100%" in rendered
    # A full bar: no unfilled track left inside the brackets.
    assert theme.glyph("shade") not in rendered.split("]")[0]


@pytest.mark.parametrize("key", [27, ord("q"), ord("Q")])
def test_progress_can_be_cancelled(key):
    def run(stdscr):
        progress = dialogs.ProgressDialog(stdscr, "Copying")
        progress.win = _ScriptedWindow(progress.win, [key, key, key])
        cancelled = progress.cancelled()
        progress.close()
        return cancelled

    assert with_curses_screen(20, 70, run) is True


def test_progress_is_not_cancelled_by_an_idle_terminal():
    def run(stdscr):
        progress = dialogs.ProgressDialog(stdscr, "Copying")
        cancelled = progress.cancelled()          # nodelay: no key waiting
        progress.close()
        return cancelled

    assert with_curses_screen(20, 70, run) is False


def test_progress_treats_a_failed_read_as_no_key():
    class _RefusesGetch:
        def __init__(self, win):
            self._win = win

        def getch(self):
            raise curses.error("no input")

        def __getattr__(self, name):
            return getattr(self._win, name)

    def run(stdscr):
        progress = dialogs.ProgressDialog(stdscr, "Copying")
        progress.win = _RefusesGetch(progress.win)
        cancelled = progress.cancelled()
        return cancelled

    assert with_curses_screen(20, 70, run) is False
