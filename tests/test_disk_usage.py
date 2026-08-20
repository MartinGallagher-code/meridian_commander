"""The Disk-usage built-in plug-in."""

from __future__ import annotations


from meridian_commander.plugins.disk_usage import DiskUsage


def _run(ctx, command=""):
    result = DiskUsage(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


def test_greeting_names_the_target(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "Measuring" in DiskUsage(ctx).greeting


def test_rejects_arguments(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "no arguments" in DiskUsage(ctx).process("something")


def test_sizes_items_biggest_first_with_a_total(data_ctx):
    ctx = data_ctx({
        "small.txt": "x",
        "big/one.bin": "y" * 400,
        "big/two.bin": "z" * 400,
    })
    plugin = DiskUsage(ctx)
    summary = plugin.process("")
    lines = [line for line in plugin.output if "small.txt" in line or "big/" in line]
    # The directory (800 bytes) is listed before the small file.
    assert "big/" in lines[0]
    assert "small.txt" in lines[1]
    assert "big/" in lines[0] and "(2 files)" in lines[0]
    assert "across 2 item(s)" in summary


def test_a_bar_is_drawn(data_ctx):
    ctx = data_ctx({"a.bin": "x" * 100})
    plugin = DiskUsage(ctx)
    plugin.process("")
    assert any("#" in line for line in plugin.output)


def test_an_empty_directory(data_ctx):
    ctx = data_ctx({})
    assert DiskUsage(ctx).process("") == "Nothing here to measure."


def test_an_unreadable_root_is_reported(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1"})

    def boom(path):
        raise PermissionError("denied")

    monkeypatch.setattr(ctx.other_fs, "listdir", boom)
    assert "Could not read" in DiskUsage(ctx).process("")


def test_bar_is_empty_when_everything_is_zero_bytes(data_ctx):
    ctx = data_ctx({"empty1": "", "empty2": ""})
    plugin = DiskUsage(ctx)
    summary = plugin.process("")
    # Nothing to scale a bar against, but it still lists and totals cleanly.
    assert "Total" in summary
    assert any("-" * 20 in line for line in plugin.output)
