"""The Find-duplicates built-in plug-in."""

from __future__ import annotations

import pytest

from meridian_commander.plugins.find_dupes import FindDupes


def _run(ctx, command=""):
    result = FindDupes(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


def test_greeting_names_the_target(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "Scanning" in FindDupes(ctx).greeting


def test_rejects_arguments(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "no arguments" in FindDupes(ctx).process("x")


def test_no_duplicates(data_ctx):
    ctx = data_ctx({"a.txt": "unique one", "b.txt": "unique two here"})
    assert FindDupes(ctx).process("") == "No duplicate files found."


def test_finds_identical_files(data_ctx):
    ctx = data_ctx({
        "a.txt": "same content",
        "dir/b.txt": "same content",
        "c.txt": "different",
    })
    plugin = FindDupes(ctx)
    summary = plugin.process("")
    printed = "\n".join(plugin.output)
    assert "a.txt" in printed and "dir/b.txt" in printed
    assert "c.txt" not in printed
    assert "1 duplicate group(s)" in summary
    assert "recoverable" in summary


def test_same_size_but_different_content_is_not_a_duplicate(data_ctx):
    # Both 4 bytes, so they share a size bucket, but hashing separates them.
    ctx = data_ctx({"a.txt": "AAAA", "b.txt": "BBBB"})
    assert FindDupes(ctx).process("") == "No duplicate files found."


def test_a_file_that_cannot_be_hashed_is_reported(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "dup", "b.txt": "dup"})
    real_open = ctx.other_fs.open_read

    def boom(path):
        if path.endswith("/a.txt"):
            raise OSError("unreadable")
        return real_open(path)

    monkeypatch.setattr(ctx.other_fs, "open_read", boom)
    plugin = FindDupes(ctx)
    summary = plugin.process("")
    assert any("! a.txt: unreadable" in line for line in plugin.output)
    # b.txt is now alone in its hash bucket, so there is no confirmed group.
    assert summary == "No duplicate files found."


def test_listing_stops_at_the_cap(data_ctx, monkeypatch):
    monkeypatch.setattr("meridian_commander.plugins.find_dupes.MAX_GROUPS", 1)
    ctx = data_ctx({
        "a1": "one", "a2": "one",       # group of size-3 content "one"
        "bb1": "twoo", "bb2": "twoo",   # group of size-4 content "twoo"
    })
    summary = FindDupes(ctx).process("")
    assert "2 duplicate group(s)" in summary
    assert "listing stopped at 1" in summary
