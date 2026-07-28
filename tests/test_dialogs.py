"""The modal dialogs, driven on real curses screens."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import dialogs

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


def test_the_title_is_centred_and_truncated():
    def check(stdscr):
        win = dialogs._center(stdscr, 5, 20)
        dialogs._box(win, "Title")
        titled = win.instr(0, 0, 20).decode()
        dialogs._box(win, "")
        untitled = win.instr(0, 0, 20).decode()
        wide = dialogs._center(stdscr, 5, 12)
        dialogs._box(wide, "A very long title indeed")
        return titled, untitled, wide.instr(0, 0, 12).decode()

    titled, untitled, long_title = with_curses_screen(20, 60, check)
    assert " Title " in titled
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
    bold = [d for d in window.drawn if len(d) > 3 and d[3] == curses.A_BOLD]
    assert bold


def test_message_clips_text_too_tall_for_the_screen(monkeypatch):
    body = "\n".join(f"line {i}" for i in range(60))
    _, window = _run(monkeypatch, lambda s: dialogs.message(s, "Long", body),
                     [10], rows=10)
    # It draws what fits and still offers the dismiss hint.
    assert "press any key" in _text(window)


def test_message_survives_a_window_that_refuses_to_draw(monkeypatch):
    """A refused body write must not take the dialog down."""
    _run(monkeypatch, lambda s: dialogs.message(s, "Note", "one\ntwo"),
         [10], fail_draws=lambda y, x: y == 2)


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
    assert "[ Yes ]" in _text(window)
    assert "[ No ]" in _text(window)


def test_confirm_shows_several_lines(monkeypatch):
    _, window = _run(monkeypatch,
                     lambda s: dialogs.confirm(s, "Delete", "one\ntwo"), [27])
    assert "one" in _text(window) and "two" in _text(window)


def test_confirm_survives_a_window_that_refuses_to_draw(monkeypatch):
    answer, _ = _run(monkeypatch,
                     lambda s: dialogs.confirm(s, "Delete", "a\nb"),
                     [ord("y")], fail_draws=lambda y, x: y == 2)
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
    # Only the tail around the cursor is on screen.
    assert len(_text(window)) < len(long_value) * 2


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
        rendered = "\n".join(progress.win.instr(row, 0, 58).decode()
                             for row in range(8))
        progress.close()
        return rendered

    rendered = with_curses_screen(20, 70, run)
    assert "2 of 4 files" in rendered
    assert "big.iso" in rendered
    assert " 50%" in rendered
    assert "#" in rendered and "-" in rendered
    assert "Esc/Q = cancel" in rendered


def test_progress_with_no_total_shows_an_empty_bar():
    def run(stdscr):
        progress = dialogs.ProgressDialog(stdscr, "Copying")
        progress.update(0, 0, "counting...")
        rendered = progress.win.instr(5, 0, 58).decode()
        progress.close()
        return rendered

    assert "  0%" in with_curses_screen(20, 70, run)


def test_progress_clamps_an_over_long_count():
    def run(stdscr):
        progress = dialogs.ProgressDialog(stdscr, "Copying")
        progress.update(500, 100, "overshoot")
        rendered = progress.win.instr(5, 0, 58).decode()
        progress.close()
        return rendered

    rendered = with_curses_screen(20, 70, run)
    assert "100%" in rendered
    assert "-" not in rendered.split("]")[0]


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
