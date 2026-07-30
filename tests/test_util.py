"""Formatting helpers, config parsing, and the awkward corners of copy/sync."""

from __future__ import annotations

import runpy
import sys
import time

import pytest

from meridian_commander import config, sync, util
from meridian_commander.filesystems import DirEntry, LocalFileSystem
from meridian_commander.operations import (
    OperationCancelled,
    copy_file,
    copy_path,
    count_tree,
    move_path,
)
from meridian_commander.sync import build_sync_plan, execute_sync_plan

from support import read, write


# -- util: sizes and times -----------------------------------------------------

@pytest.mark.parametrize("size, expected", [
    (None, "     ?"),
    (0, "     0"),
    (999, "   999"),
    (1023, "  1023"),
    (1024, "  1.0K"),
    (10 * 1024, "   10K"),
    (1024 ** 2, "  1.0M"),
    (1024 ** 3, "  1.0G"),
    (1024 ** 4, "  1.0T"),
    (1024 ** 5, "  1.0P"),
    (1024 ** 6, "  1.0E"),
    # Beyond the last unit it degrades to a big number rather than misreporting.
    (1024 ** 7, " 1024E"),
])
def test_human_size(size, expected):
    assert util.human_size(size) == expected
    # Every rendering is the same width, so the size column stays aligned.
    assert len(util.human_size(size)) == 6


def test_human_size_switches_to_whole_numbers_above_ten():
    assert util.human_size(int(9.5 * 1024)) == "  9.5K"
    assert util.human_size(int(10.5 * 1024)) == "   10K"


def test_human_time_shows_the_clock_for_this_year():
    now = time.time()
    rendered = util.human_time(now)
    assert time.strftime("%b %d %H:%M", time.localtime(now)) == rendered


def test_human_time_shows_the_year_for_older_files():
    old = time.mktime((2001, 6, 15, 12, 30, 0, 0, 0, -1))
    assert util.human_time(old) == time.strftime("%b %d  %Y", time.localtime(old))


@pytest.mark.parametrize("mtime", [None, 0, float("nan"), 1e30])
def test_human_time_copes_with_a_missing_or_absurd_timestamp(mtime):
    assert util.human_time(mtime) == "     ?      "


# -- util: truncation ----------------------------------------------------------

@pytest.mark.parametrize("text, width, expected", [
    ("hello", 0, ""),
    ("hello", -3, ""),
    ("hello", 1, "~"),
    ("hello", 4, "hel~"),
    ("hello", 5, "hello"),
    ("hello", 9, "hello"),
])
def test_truncate(text, width, expected):
    assert util.truncate(text, width) == expected


def test_ljust_and_rjust_always_fill_the_width():
    assert util.ljust("ab", 5) == "ab   "
    assert util.rjust("ab", 5) == "   ab"
    # Over-long text is cut to the width rather than pushing the layout out.
    assert util.ljust("abcdefgh", 4) == "abc~"
    assert util.rjust("abcdefgh", 4) == "abc~"


# -- config --------------------------------------------------------------------

def test_config_load_survives_a_damaged_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "meridian-commander" / "config.ini"
    path.parent.mkdir(parents=True)
    path.write_text("this is not an ini file\n[unclosed\n")
    # A broken config falls back to defaults rather than stopping the app.
    assert config.load().sections() == []


def test_plugin_settings_merge_over_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "meridian-commander" / "config.ini"
    path.parent.mkdir(parents=True)
    path.write_text(
        "[plugin:demo]\n"
        "rows = 40\n"
        "bad_number = not-a-number\n"
        "blank =\n"
        "name = custom\n"
    )
    merged = config.plugin_settings(
        "demo", {"rows": 10, "bad_number": 7, "blank": "kept", "name": "default"})
    assert merged["rows"] == 40                 # coerced back to int
    assert merged["bad_number"] == "not-a-number"  # int coercion failed: kept raw
    assert merged["blank"] == "kept"            # empty value leaves the default
    assert merged["name"] == "custom"


def test_plugin_settings_with_no_section_returns_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.plugin_settings("absent", {"a": 1}) == {"a": 1}


def test_extra_plugin_dirs_reads_a_colon_separated_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "meridian-commander" / "config.ini"
    path.parent.mkdir(parents=True)
    path.write_text("[plugins]\ndirs = ~/one: :/two\n")
    dirs = config.extra_plugin_dirs()
    assert dirs[-1] == "/two"
    assert dirs[0].endswith("/one") and "~" not in dirs[0]
    assert len(dirs) == 2                       # the blank entry is dropped


def test_extra_plugin_dirs_defaults_to_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.extra_plugin_dirs() == []


# -- sync: rendering and the corners of the comparison -------------------------

def test_sync_action_renders_and_totals(fs, tmp_path):
    write(str(tmp_path / "l" / "a.txt"), "aaaa")
    (tmp_path / "r").mkdir()
    plan = build_sync_plan(fs, str(tmp_path / "l"), fs, str(tmp_path / "r"))
    assert plan.total_bytes == 4
    assert bool(plan) is True
    rendered = plan.actions[0].render()
    assert rendered == " ->  a.txt    (new on left)"

    empty = build_sync_plan(fs, str(tmp_path / "r"), fs, str(tmp_path / "r"))
    assert bool(empty) is False
    assert empty.total_bytes == 0


def test_sync_without_mtimes_falls_back_to_size(fs, tmp_path):
    """A backend that cannot report times still syncs on a size difference."""

    class _NoMtimeFS(LocalFileSystem):
        def listdir(self, path):
            entries = super().listdir(path)
            for entry in entries:
                entry.mtime = None
            return entries

    timeless = _NoMtimeFS()
    write(str(tmp_path / "l" / "a.txt"), "longer text")
    write(str(tmp_path / "r" / "a.txt"), "short")
    write(str(tmp_path / "l" / "same.txt"), "identical")
    write(str(tmp_path / "r" / "same.txt"), "identical")

    plan = build_sync_plan(timeless, str(tmp_path / "l"),
                           timeless, str(tmp_path / "r"))
    changed = {a.rel: a for a in plan.actions}
    assert "a.txt" in changed
    assert changed["a.txt"].reason == "differs (no mtime)"
    assert changed["a.txt"].direction == "->"
    # Matching sizes are left alone: with no times there is nothing to prefer.
    assert "same.txt" not in changed


def _three_file_plan(fs, tmp_path):
    for name in ("a.txt", "b.txt", "c.txt"):
        write(str(tmp_path / "l" / name), name)
    (tmp_path / "r").mkdir()
    return build_sync_plan(fs, str(tmp_path / "l"), fs, str(tmp_path / "r"))


def test_execute_sync_plan_announces_each_action(fs, tmp_path):
    plan = _three_file_plan(fs, tmp_path)
    seen = []
    copied = execute_sync_plan(
        plan, fs, fs,
        on_action=lambda action, i, total: seen.append((action.rel, i, total)))
    assert copied == 3
    assert [s[0] for s in seen] == ["a.txt", "b.txt", "c.txt"]
    assert [s[1] for s in seen] == [0, 1, 2]
    assert {s[2] for s in seen} == {3}


def test_execute_sync_plan_stops_when_cancelled(fs, tmp_path):
    plan = _three_file_plan(fs, tmp_path)
    assert execute_sync_plan(plan, fs, fs, cancel=lambda: True) == 0
    assert list((tmp_path / "r").iterdir()) == []


# -- sync: the scan reports in, and can be got out of --------------------------

def test_scan_can_be_cancelled(fs, tmp_path):
    """The slow half of a sync is interruptible -- the point of the exercise."""
    write(str(tmp_path / "l" / "a.txt"), "a")
    (tmp_path / "r").mkdir()
    with pytest.raises(OperationCancelled):
        build_sync_plan(fs, str(tmp_path / "l"), fs, str(tmp_path / "r"),
                        cancel=lambda: True)


def test_scan_reports_progress_while_it_walks(fs, tmp_path):
    """Counts arrive during the walk, not only at the end."""
    for i in range(5):
        write(str(tmp_path / "l" / "sub" / f"f{i}.txt"), "x")
    (tmp_path / "r").mkdir()
    seen = []
    build_sync_plan(fs, str(tmp_path / "l"), fs, str(tmp_path / "r"),
                    progress=lambda cur, total, label: seen.append(
                        (cur, total, label)))
    # A scan has no denominator, which is what tells the dialog to draw an
    # indeterminate bar rather than a bar pinned at zero.
    assert seen and all(total == 0 for _cur, total, _label in seen)
    assert any(label.startswith("left: ") for _c, _t, label in seen)
    assert any(label.startswith("right: ") for _c, _t, label in seen)
    assert any("comparing" in label for _c, _t, label in seen)


def test_scan_reports_within_one_large_directory(fs, tmp_path, monkeypatch):
    """A single huge directory reports as it goes, not once at the end."""
    monkeypatch.setattr(sync, "SCAN_REPORT_EVERY", 2)
    for i in range(6):
        write(str(tmp_path / "l" / f"f{i}.txt"), "x")
    (tmp_path / "r").mkdir()
    counts = []
    build_sync_plan(fs, str(tmp_path / "l"), fs, str(tmp_path / "r"),
                    progress=lambda cur, total, label: counts.append(cur))
    # 6 files at one report per 2 means the count climbs mid-directory.
    assert sorted(set(counts)) == [0, 2, 4, 6]


def test_scan_skips_unreadable_directories(fs, tmp_path):
    """One denied subdirectory must not cost the whole sync."""

    class _DeniedSubdir(LocalFileSystem):
        def listdir(self, path):
            if path.endswith("locked"):
                raise PermissionError("nope")
            return super().listdir(path)

    write(str(tmp_path / "l" / "keep.txt"), "keep")
    (tmp_path / "l" / "locked").mkdir()
    write(str(tmp_path / "l" / "locked" / "hidden.txt"), "hidden")
    (tmp_path / "r").mkdir()
    plan = build_sync_plan(_DeniedSubdir(), str(tmp_path / "l"),
                           fs, str(tmp_path / "r"))
    assert [a.rel for a in plan.actions] == ["keep.txt"]


def test_scan_handles_a_tree_deeper_than_the_recursion_limit(fs, tmp_path):
    """The walk is a stack, so depth costs heap rather than interpreter stack.

    Synthesised rather than made on disk: Linux gives up at PATH_MAX long
    before Python would give up at its recursion limit, so a real directory
    this deep cannot be created to test against.
    """
    depth = sys.getrecursionlimit() + 50

    class _DeepFS(LocalFileSystem):
        """One directory inside the next, ``depth`` times, then one file."""

        def listdir(self, path):
            level = path.count("/d")
            if level >= depth:
                return [DirEntry("bottom.txt", is_dir=False, size=8, mtime=1.0)]
            return [DirEntry("d", is_dir=True)]

    (tmp_path / "r").mkdir()
    plan = build_sync_plan(_DeepFS(), "/root", fs, str(tmp_path / "r"))
    assert len(plan.actions) == 1
    assert plan.actions[0].rel == "/".join(["d"] * depth + ["bottom.txt"])


# -- operations: the failure corners of copy -----------------------------------

def test_copy_survives_a_backend_that_cannot_stat(fs, tmp_path):
    """A backend with no stat() still copies; only the progress total is lost."""

    class _NoStatFS(LocalFileSystem):
        def stat(self, path):
            raise OSError("stat not supported")

    src = tmp_path / "a.txt"
    src.write_text("payload")
    sizes = []
    copy_file(_NoStatFS(), str(src), fs, str(tmp_path / "b.txt"),
              progress=lambda done, total, label: sizes.append((done, total)))
    assert read(str(tmp_path / "b.txt")) == "payload"
    # With no size to report, progress counts up from a total of zero.
    assert sizes[0][1] == 0


def test_copy_can_be_cancelled_mid_file(fs, tmp_path):
    src = tmp_path / "big.txt"
    src.write_text("x" * (1024 * 1024))
    with pytest.raises(OperationCancelled):
        copy_path(fs, str(src), fs, str(tmp_path / "out.txt"),
                  cancel=lambda: True)


def test_copy_ignores_a_reader_that_fails_to_close(fs, tmp_path):
    class _BadCloseFS(LocalFileSystem):
        def open_read(self, path):
            handle = super().open_read(path)

            class _Wrapper:
                def read(self, n):
                    return handle.read(n)

                def close(self):
                    raise OSError("close failed")

            return _Wrapper()

    src = tmp_path / "a.txt"
    src.write_text("payload")
    copy_path(_BadCloseFS(), str(src), fs, str(tmp_path / "b.txt"))
    assert read(str(tmp_path / "b.txt")) == "payload"


def test_copy_ignores_a_backend_that_cannot_set_times(fs, tmp_path):
    class _NoUtimeFS(LocalFileSystem):
        def utime(self, path, mtime):
            raise OSError("utime not supported")

    src = tmp_path / "a.txt"
    src.write_text("payload")
    copy_path(fs, str(src), _NoUtimeFS(), str(tmp_path / "b.txt"),
              preserve_mtime=True)
    # The copy still succeeded; only the timestamp was lost.
    assert read(str(tmp_path / "b.txt")) == "payload"


def test_copy_tree_can_be_cancelled_between_files(fs, tmp_path):
    for name in ("a", "b", "c"):
        write(str(tmp_path / "src" / name), name)
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 2

    with pytest.raises(OperationCancelled):
        copy_path(fs, str(tmp_path / "src"), fs, str(tmp_path / "dst"),
                  cancel=cancel)


def test_move_across_backends_copies_then_deletes(fs, tmp_path):
    """Two different backends cannot rename, so the move falls back to copy."""
    write(str(tmp_path / "src" / "a.txt"), "payload")
    other = LocalFileSystem()
    move_path(fs, str(tmp_path / "src"), other, str(tmp_path / "dst"))
    assert read(str(tmp_path / "dst" / "a.txt")) == "payload"
    assert not (tmp_path / "src").exists()


def test_move_creates_a_missing_destination_parent(fs, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("payload")
    move_path(fs, str(src), fs, str(tmp_path / "new" / "deep" / "a.txt"))
    assert read(str(tmp_path / "new" / "deep" / "a.txt")) == "payload"


def test_count_tree_of_a_single_file(fs, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("12345")
    assert count_tree(fs, str(src)) == (1, 5)


# -- the module entry point ----------------------------------------------------

def test_python_m_runs_the_app(monkeypatch):
    from meridian_commander import app as app_mod

    called = []
    monkeypatch.setattr(app_mod, "main", lambda: called.append(True) or 0)
    monkeypatch.delitem(sys.modules, "meridian_commander.__main__", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("meridian_commander", run_name="__main__")
    assert exit_info.value.code == 0
    assert called == [True]


def test_copy_handles_a_stat_that_reports_no_size(fs, tmp_path):
    """Some backends list an entry without a size; the copy still runs."""

    class _SizelessFS(LocalFileSystem):
        def stat(self, path):
            entry = super().stat(path)
            entry.size = None
            return entry

    src = tmp_path / "a.txt"
    src.write_text("payload")
    totals = []
    copy_file(_SizelessFS(), str(src), fs, str(tmp_path / "b.txt"),
              progress=lambda done, total, label: totals.append(total))
    assert read(str(tmp_path / "b.txt")) == "payload"
    assert totals and set(totals) == {0}


def test_copy_tree_can_be_cancelled_before_it_starts(fs, tmp_path):
    write(str(tmp_path / "src" / "a.txt"), "a")
    with pytest.raises(OperationCancelled):
        copy_path(fs, str(tmp_path / "src"), fs, str(tmp_path / "dst"),
                  cancel=lambda: True)
