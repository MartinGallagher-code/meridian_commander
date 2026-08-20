"""The Compare-panes built-in plug-in."""

from __future__ import annotations

import os

import pytest

from meridian_commander.plugin_api import PluginContext
from meridian_commander.plugins.compare_panes import ComparePanes

from support import write


@pytest.fixture
def compare_ctx(app, tmp_path):
    """Build a context after writing a tree into each pane's directory."""
    def make(left: dict, right: dict, times: dict | None = None):
        times = times or {}
        for side, files in (("left", left), ("right", right)):
            base = tmp_path / f"cmp_{side}"
            base.mkdir(exist_ok=True)
            for name, content in files.items():
                p = base / name
                write(str(p), content)
                if name in times:
                    os.utime(str(p), (times[name], times[name]))
            getattr(app, side).chdir(str(base))
        return PluginContext(app=app, own_panel=app.left, other_panel=app.right)
    return make


def _run(ctx, command=""):
    result = ComparePanes(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


def test_greeting_names_both_sides(compare_ctx):
    ctx = compare_ctx({"a.txt": "1"}, {"a.txt": "1"})
    greeting = ComparePanes(ctx).greeting
    assert "Left" in greeting and "Right" in greeting


def test_rejects_an_unknown_argument(compare_ctx):
    ctx = compare_ctx({"a.txt": "1"}, {"a.txt": "1"})
    assert "Press Enter to compare" in ComparePanes(ctx).process("wobble")


def test_identical_trees_report_no_differences(compare_ctx):
    ctx = compare_ctx({"a.txt": "1", "d/b.txt": "2"},
                      {"a.txt": "1", "d/b.txt": "2"},
                      times={"a.txt": 1000.0})
    # Give the right side the same mtimes so nothing is flagged as differing.
    plugin = ComparePanes(ctx)
    summary = plugin.process("")
    assert "2 identical" in summary
    assert "0 differ" in summary


def test_only_left_and_only_right(compare_ctx):
    ctx = compare_ctx({"shared.txt": "x", "leftonly.txt": "l"},
                      {"shared.txt": "x", "rightonly.txt": "r"})
    plugin = ComparePanes(ctx)
    summary = plugin.process("")
    printed = "\n".join(plugin.output)
    assert "<-- leftonly.txt  (only left)" in printed
    assert "--> rightonly.txt  (only right)" in printed
    assert "1 only-left, 1 only-right" in summary


def test_a_size_difference_is_flagged(compare_ctx):
    ctx = compare_ctx({"a.txt": "short"}, {"a.txt": "much longer content"})
    plugin = ComparePanes(ctx)
    summary = plugin.process("")
    assert "a.txt  (differs)" in "\n".join(plugin.output)
    assert "1 differ" in summary


def test_a_time_difference_is_flagged(compare_ctx, tmp_path):
    ctx = compare_ctx({"a.txt": "same"}, {"a.txt": "same"})
    # Same bytes, but modification times far enough apart to flag.
    os.utime(str(tmp_path / "cmp_left" / "a.txt"), (1000.0, 1000.0))
    os.utime(str(tmp_path / "cmp_right" / "a.txt"), (5000.0, 5000.0))
    assert "1 differ" in ComparePanes(ctx).process("")


def test_hash_mode_sees_through_equal_sizes_and_times(compare_ctx):
    # Same length and mtime, different bytes: only a content hash catches it.
    ctx = compare_ctx({"a.txt": "AAAA"}, {"a.txt": "BBBB"},
                      times={"a.txt": 1000.0})
    plugin = ComparePanes(ctx)
    assert "0 differ" in plugin.process("")        # by time: looks identical
    assert "1 differ" in plugin.process("hash")    # by content: differs


def test_hash_mode_reports_an_unreadable_file_as_differing(compare_ctx,
                                                           monkeypatch):
    ctx = compare_ctx({"a.txt": "AAAA"}, {"a.txt": "AAAA"})

    def boom(path):
        raise OSError("denied")

    monkeypatch.setattr(ctx.own_fs, "open_read", boom)
    assert "1 differ" in ComparePanes(ctx).process("hash")


def test_differs_treats_missing_mtimes_as_identical(compare_ctx):
    from meridian_commander.filesystems import DirEntry

    ctx = compare_ctx({"a.txt": "12345"}, {"a.txt": "12345"})
    plugin = ComparePanes(ctx)
    here = DirEntry(name="a", is_dir=False, size=5, mtime=None)
    there = DirEntry(name="a", is_dir=False, size=5, mtime=None)
    # Same size, no clock to compare -> not treated as differing.
    assert plugin._differs("a", here, there, False) is False


def test_listing_stops_at_the_cap_but_the_count_is_whole(compare_ctx, monkeypatch):
    monkeypatch.setattr("meridian_commander.plugins.compare_panes.MAX_RESULTS", 2)
    left = {f"f{i}.txt": "x" for i in range(5)}
    ctx = compare_ctx(left, {})
    summary = ComparePanes(ctx).process("")
    assert "5 only-left" in summary
    assert "listing stopped at 2" in summary
