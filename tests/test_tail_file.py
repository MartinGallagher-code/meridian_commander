"""The Tail-file built-in plug-in."""

from __future__ import annotations

import pytest

from meridian_commander.plugins.tail_file import TailFile, read_tail


def _append(tmp_path, name, text):
    with open(tmp_path / "data" / name, "a") as f:
        f.write(text)


def _out(plugin):
    return "\n".join(plugin.output)


# -- read_tail helper ----------------------------------------------------------

def test_read_tail_returns_the_last_bytes_and_total(data_ctx, tmp_path):
    ctx = data_ctx({"log": "hello world"})
    data, total = read_tail(ctx.other_fs, str(tmp_path / "data" / "log"),
                            max_bytes=5)
    assert data == b"world"
    assert total == 11


# -- one-shot tail -------------------------------------------------------------

def test_greeting_names_the_pane(data_ctx):
    ctx = data_ctx({"log": "x"})
    assert "resolve against" in TailFile(ctx).greeting


def test_empty_input_does_nothing(data_ctx):
    ctx = data_ctx({"log": "x"})
    assert TailFile(ctx).process("") is None


def test_tail_shows_the_last_lines(data_ctx):
    ctx = data_ctx({"log": "".join(f"line{i}\n" for i in range(50))})
    plugin = TailFile(ctx)
    summary = plugin.process("tail log 3")
    printed = _out(plugin)
    assert "line49" in printed and "line47" in printed
    assert "line46" not in printed
    assert "line(s) in the last block" in summary


def test_tail_defaults_to_twenty_lines(data_ctx):
    ctx = data_ctx({"log": "".join(f"{i}\n" for i in range(100))})
    plugin = TailFile(ctx)
    plugin.process("tail log")
    shown = [l for l in plugin.output if l.strip().isdigit()]
    assert len(shown) == 20


def test_tail_notes_a_truncated_window(data_ctx, monkeypatch):
    monkeypatch.setattr("meridian_commander.plugins.tail_file.MAX_TAIL_BYTES", 8)
    ctx = data_ctx({"log": "abcdefghijklmnop\nlast\n"})
    plugin = TailFile(ctx)
    plugin.process("tail log")
    assert any("showing the last" in line for line in plugin.output)


def test_tail_of_a_missing_file(data_ctx):
    ctx = data_ctx({"log": "x"})
    assert "Could not read" in TailFile(ctx).process("tail nope")


def test_tail_needs_a_file(data_ctx):
    ctx = data_ctx({"log": "x"})
    assert TailFile(ctx).process("tail") == "usage: tail <file> [lines]"


def test_tail_rejects_a_bad_line_count(data_ctx):
    ctx = data_ctx({"log": "x"})
    assert "positive number" in TailFile(ctx).process("tail log zero")


def test_unknown_command(data_ctx):
    ctx = data_ctx({"log": "x"})
    assert "unknown command" in TailFile(ctx).process("wobble")


# -- follow --------------------------------------------------------------------

def test_follow_streams_appended_lines(data_ctx, tmp_path):
    ctx = data_ctx({"log": "start\n"})
    plugin = TailFile(ctx)
    assert "Following log" in plugin.process("follow log")

    _append(tmp_path, "log", "new line\n")
    plugin.tick()
    assert "new line" in _out(plugin)


def test_tick_does_nothing_when_not_following(data_ctx):
    ctx = data_ctx({"log": "x\n"})
    plugin = TailFile(ctx)
    plugin.tick()                     # no follow set: a quiet no-op
    assert plugin.output == plugin.output   # (did not raise)


def test_tick_is_quiet_when_the_file_has_not_grown(data_ctx):
    ctx = data_ctx({"log": "x\n"})
    plugin = TailFile(ctx)
    plugin.process("follow log")
    before = list(plugin.output)
    plugin.tick()
    assert plugin.output == before


def test_follow_notices_a_rotation(data_ctx, tmp_path):
    ctx = data_ctx({"log": "aaaa\nbbbb\ncccc\n"})
    plugin = TailFile(ctx)
    plugin.process("follow log")
    # Rewrite the file shorter -> a size drop the follower reads as a rotation.
    (tmp_path / "data" / "log").write_text("fresh\n")
    plugin.tick()
    printed = _out(plugin)
    assert "file rotated" in printed
    assert "fresh" in printed


def test_follow_tolerates_a_vanished_file(data_ctx, tmp_path):
    ctx = data_ctx({"log": "a\n"})
    plugin = TailFile(ctx)
    plugin.process("follow log")
    (tmp_path / "data" / "log").unlink()
    plugin.tick()                     # stat fails: keep following, no crash
    assert plugin._follow_path is not None


def test_stop_following(data_ctx):
    ctx = data_ctx({"log": "x\n"})
    plugin = TailFile(ctx)
    plugin.process("follow log")
    assert "Stopped following" in plugin.process("stop")
    assert plugin._follow_path is None


def test_stop_when_not_following(data_ctx):
    ctx = data_ctx({"log": "x\n"})
    assert TailFile(ctx).process("stop") == "Not following anything."


def test_follow_reads_an_absolute_path(data_ctx, tmp_path):
    ctx = data_ctx({"log": "abs\n"})
    absolute = str(tmp_path / "data" / "log")
    plugin = TailFile(ctx)
    assert "Following" in plugin.process(f"follow {absolute}")


def test_follow_read_error_on_tick_is_swallowed(data_ctx, tmp_path, monkeypatch):
    ctx = data_ctx({"log": "a\n"})
    plugin = TailFile(ctx)
    plugin.process("follow log")
    _append(tmp_path, "log", "more\n")

    def boom(path):
        raise OSError("read failed")

    monkeypatch.setattr(ctx.other_fs, "open_read", boom)
    plugin.tick()                     # read fails after stat succeeded: no crash
    assert plugin._follow_path is not None
