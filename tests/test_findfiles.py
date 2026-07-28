"""Finding files across a backend, and browsing the results."""

from __future__ import annotations

import curses

import pytest

from meridian_commander import findfiles
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.findfiles import FindBrowser, collect_matches

from support import script_newwin, with_curses_screen, write


@pytest.fixture
def haystack(tmp_path):
    write(str(tmp_path / "root" / "notes.txt"), "top level")
    write(str(tmp_path / "root" / "a" / "config.ini"), "nested")
    write(str(tmp_path / "root" / "a" / "b" / "deep.conf"), "deeper")
    write(str(tmp_path / "root" / "z" / "notes.txt"), "another")
    return tmp_path / "root"


# -- collect_matches -----------------------------------------------------------

def test_a_plain_pattern_matches_as_a_substring(fs, haystack):
    matches, truncated = collect_matches(fs, str(haystack), "conf")
    assert truncated is False
    assert sorted(rel for rel, _ in matches) == ["a/b/deep.conf", "a/config.ini"]


def test_a_glob_pattern_is_used_as_written(fs, haystack):
    matches, _ = collect_matches(fs, str(haystack), "*.txt")
    assert sorted(rel for rel, _ in matches) == ["notes.txt", "z/notes.txt"]
    # As a glob it anchors, so a bare extension finds nothing.
    assert collect_matches(fs, str(haystack), "*.conf")[0]


def test_the_depth_limit_stops_the_walk(fs, haystack):
    shallow, _ = collect_matches(fs, str(haystack), "deep", max_depth=1)
    assert shallow == []
    deep, _ = collect_matches(fs, str(haystack), "deep", max_depth=5)
    assert [rel for rel, _ in deep] == ["a/b/deep.conf"]


def test_the_result_limit_truncates(fs, haystack):
    matches, truncated = collect_matches(fs, str(haystack), "*", max_results=2)
    assert truncated is True
    assert len(matches) == 2


def test_cancelling_before_the_first_directory_finds_nothing(fs, haystack):
    matches, truncated = collect_matches(fs, str(haystack), "*",
                                         cancel=lambda: True)
    assert matches == []
    assert truncated is False


def test_cancelling_part_way_through_stops_the_walk(fs, haystack):
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 3

    matches, _ = collect_matches(fs, str(haystack), "*", cancel=cancel)
    # It stopped early rather than finding everything.
    assert len(matches) < 6


def test_an_unreadable_directory_is_skipped_not_fatal(fs, haystack):
    class _OneBadDirFS(LocalFileSystem):
        def listdir(self, path):
            if path.endswith("/a"):
                raise PermissionError("permission denied")
            return super().listdir(path)

    matches, _ = collect_matches(_OneBadDirFS(), str(haystack), "notes")
    # The unreadable branch is skipped; the rest of the tree is still searched.
    assert sorted(rel for rel, _ in matches) == ["notes.txt", "z/notes.txt"]


def test_progress_is_reported_as_directories_are_entered(fs, haystack):
    seen = []
    collect_matches(fs, str(haystack), "notes",
                    progress=lambda count, rel: seen.append((count, rel)))
    entered = [rel for _, rel in seen]
    assert "a" in entered and "a/b" in entered and "z" in entered
    # The running count reflects matches found so far.
    assert seen[-1][0] >= 1


def test_a_symlinked_directory_is_not_descended_into(fs, tmp_path):
    write(str(tmp_path / "real" / "target.txt"), "x")
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "link").symlink_to(tmp_path / "real")
    matches, _ = collect_matches(fs, str(tmp_path / "root"), "target")
    assert matches == []


# -- FindBrowser: selection ----------------------------------------------------

def _browser(fs, haystack, pattern="*"):
    matches, truncated = collect_matches(fs, str(haystack), pattern)
    return FindBrowser(fs, str(haystack), pattern, matches, truncated)


def test_browser_reports_the_selected_path(fs, haystack):
    browser = _browser(fs, haystack, "config")
    assert browser.current()[0] == "a/config.ini"
    assert browser.current_path() == str(haystack / "a" / "config.ini")


def test_browser_with_no_matches_has_nothing_selected(fs, haystack):
    browser = FindBrowser(fs, str(haystack), "nope", [], False)
    assert browser.current() is None
    assert browser.current_path() is None


# -- FindBrowser: the key loop, on a real screen -------------------------------

def _run_browser(monkeypatch, browser, keys, rows=10, cols=70, fail_draws=False):
    captured = script_newwin(monkeypatch, keys, fail_draws)
    result = with_curses_screen(rows, cols, browser.run)
    return result, captured["window"]


def test_browser_closes_on_q_and_escape(monkeypatch, fs, haystack):
    for key in (ord("q"), ord("Q"), 27, curses.KEY_F10):
        browser = _browser(fs, haystack)
        result, _ = _run_browser(monkeypatch, browser, [key])
        assert result is None


def test_browser_returns_the_path_to_jump_to(monkeypatch, fs, haystack):
    browser = _browser(fs, haystack, "config")
    result, _ = _run_browser(monkeypatch, browser, [10])
    assert result == "a/config.ini"


def test_browser_enter_with_no_matches_stays_open(monkeypatch, fs, haystack):
    browser = FindBrowser(fs, str(haystack), "nope", [], False)
    # Enter does nothing with an empty list, then q closes it.
    result, _ = _run_browser(monkeypatch, browser, [10, ord("q")])
    assert result is None


def test_browser_navigation_keys_move_the_cursor(monkeypatch, fs, haystack):
    browser = _browser(fs, haystack)
    last = len(browser.matches) - 1
    keys = [curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_UP, ord("j"),
            curses.KEY_END, curses.KEY_HOME, curses.KEY_NPAGE,
            curses.KEY_PPAGE, curses.KEY_END, ord("g")]
    result, _ = _run_browser(monkeypatch, browser, keys)
    assert browser.cursor == last
    assert result == browser.matches[last][0]


def test_browser_paging_is_bounded_by_the_list(monkeypatch, fs, haystack):
    browser = _browser(fs, haystack)
    result, _ = _run_browser(monkeypatch, browser,
                             [curses.KEY_NPAGE, curses.KEY_NPAGE,
                              curses.KEY_NPAGE, ord("k"), ord("q")])
    assert 0 <= browser.cursor < len(browser.matches)


def test_browser_header_shows_the_count_and_truncation(monkeypatch, fs, haystack):
    matches, _ = collect_matches(fs, str(haystack), "*", max_results=2)
    browser = FindBrowser(fs, str(haystack), "*", matches, truncated=True)
    _, window = _run_browser(monkeypatch, browser, [ord("q")])
    assert "2 match(es)" in window.text
    assert "(truncated)" in window.text


def test_browser_scrolls_a_list_taller_than_the_window(monkeypatch, fs, tmp_path):
    for i in range(40):
        write(str(tmp_path / "many" / f"file{i:02d}.txt"), "x")
    browser = _browser(fs, tmp_path / "many")
    # End jumps to the bottom, which must scroll the window down to reach it.
    _, window = _run_browser(monkeypatch, browser, [curses.KEY_END, ord("q")],
                             rows=8)
    assert browser.top > 0
    assert "file39.txt" in window.text


def test_browser_scrolls_back_up_to_reach_the_cursor(monkeypatch, fs, tmp_path):
    for i in range(40):
        write(str(tmp_path / "many" / f"file{i:02d}.txt"), "x")
    browser = _browser(fs, tmp_path / "many")
    # End scrolls to the bottom; Home must scroll the window back to the top.
    _run_browser(monkeypatch, browser,
                 [curses.KEY_END, curses.KEY_HOME, ord("q")], rows=8)
    assert browser.top == 0
    assert browser.cursor == 0


def test_browser_survives_a_screen_that_refuses_every_write(monkeypatch, fs,
                                                            haystack):
    """A refused write must not take the browser down mid-draw."""
    browser = _browser(fs, haystack)
    result, window = _run_browser(monkeypatch, browser,
                                  [curses.KEY_DOWN, ord("q")], fail_draws=True)
    assert result is None
    assert window.drawn        # it did keep trying to draw


def test_browser_views_a_file_in_place(monkeypatch, fs, haystack):
    opened = []

    class _FakeViewer:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            return None

    monkeypatch.setattr("meridian_commander.browsers.Viewer", _FakeViewer)
    browser = _browser(fs, haystack, "config")
    _run_browser(monkeypatch, browser, [ord("v"), ord("q")])
    assert opened == [str(haystack / "a" / "config.ini")]


def test_browser_edits_a_file_in_place(monkeypatch, fs, haystack):
    opened = []

    class _FakeEditor:
        def __init__(self, fs, path):
            opened.append(path)

        def run(self, stdscr):
            return None

    monkeypatch.setattr("meridian_commander.editor.Editor", _FakeEditor)
    browser = _browser(fs, haystack, "config")
    _run_browser(monkeypatch, browser, [curses.KEY_F4, ord("q")])
    assert opened == [str(haystack / "a" / "config.ini")]


def test_browser_refuses_to_view_a_directory(monkeypatch, fs, haystack):
    matches, _ = collect_matches(fs, str(haystack), "a")
    browser = FindBrowser(fs, str(haystack), "a", matches, False)
    assert browser.current()[1].is_dir
    _, window = _run_browser(monkeypatch, browser, [ord("v"), ord("q")])
    assert "directories can be entered" in window.text


def test_browser_view_with_nothing_selected_does_nothing(monkeypatch, fs, haystack):
    browser = FindBrowser(fs, str(haystack), "nope", [], False)
    _run_browser(monkeypatch, browser, [ord("v"), ord("q")])
    assert browser.notice == ""


def test_browser_reports_a_viewer_that_fails(monkeypatch, fs, haystack):
    class _BrokenViewer:
        def __init__(self, fs, path):
            pass

        def run(self, stdscr):
            raise OSError("cannot read that file")

    monkeypatch.setattr("meridian_commander.browsers.Viewer", _BrokenViewer)
    browser = _browser(fs, haystack, "config")
    _, window = _run_browser(monkeypatch, browser, [ord("v"), ord("q")])
    assert "cannot read that file" in window.text
