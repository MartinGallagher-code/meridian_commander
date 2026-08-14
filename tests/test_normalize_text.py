"""The Normalise-text built-in plug-in."""

from __future__ import annotations

import pytest

from meridian_commander.plugins.normalize_text import NormalizeText, transform


def _run(ctx, command):
    result = NormalizeText(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


def _read(tmp_path, name):
    return (tmp_path / "data" / name).read_bytes()


# -- the transform, in isolation ----------------------------------------------

@pytest.mark.parametrize("data, verb, width, expected", [
    (b"a\r\nb\rc\n", "lf", 4, b"a\nb\nc\n"),
    (b"a\nb\n", "crlf", 4, b"a\r\nb\r\n"),
    (b"a\t b", "untabs", 2, b"a   b"),
    (b"a  \nb\t\n", "trim", 4, b"a\nb\n"),
    (b"a\n\n\n", "finalnl", 4, b"a\n"),
    (b"", "finalnl", 4, b""),
])
def test_transform(data, verb, width, expected):
    assert transform(data, verb, width) == expected


# -- the plug-in ---------------------------------------------------------------

def test_greeting_lists_the_tagged_files(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert "a.txt" in NormalizeText(ctx).greeting

    ctx.other_panel.selected = set()
    ctx.other_panel.move_to(0)
    assert "<nothing tagged>" in NormalizeText(ctx).greeting


def test_empty_input_does_nothing(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert NormalizeText(ctx).process("") is None


def test_rejects_an_unknown_rule(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert "unknown rule" in NormalizeText(ctx).process("wobble")


def test_needs_a_tagged_file(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    ctx.other_panel.move_to(0)                  # the ".." entry
    with pytest.raises(RuntimeError, match="Tag at least 1"):
        NormalizeText(ctx).process("lf")


def test_lf_rewrites_in_place(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "x\r\ny\r\n"}, selected={"a.txt"})
    out = _run(ctx, "lf")
    assert "changed 1 file(s)" in out
    assert _read(tmp_path, "a.txt") == b"x\ny\n"


def test_untabs_takes_a_width(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "a\tb"}, selected={"a.txt"})
    _run(ctx, "untabs 2")
    assert _read(tmp_path, "a.txt") == b"a  b"


def test_untabs_rejects_a_bad_width(data_ctx):
    ctx = data_ctx({"a.txt": "a\tb"}, selected={"a.txt"})
    assert "usage: untabs" in NormalizeText(ctx).process("untabs zero")


def test_an_already_clean_file_is_skipped(data_ctx):
    ctx = data_ctx({"a.txt": "clean\n"}, selected={"a.txt"})
    out = _run(ctx, "lf")
    assert "changed 0 file(s), 1 already clean/skipped" in out


def test_preview_writes_nothing(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "x\r\n"}, selected={"a.txt"})
    plugin = NormalizeText(ctx)
    summary = plugin.process("preview lf")
    assert any("would change a.txt" in line for line in plugin.output)
    assert "would change 1 file(s)" in summary
    assert _read(tmp_path, "a.txt") == b"x\r\n"


def test_preview_needs_a_rule(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert NormalizeText(ctx).process("preview") == "usage: preview <rule> ..."


def test_a_binary_file_is_skipped(data_ctx, tmp_path):
    ctx = data_ctx({"blob.bin": "a\x00b\r\n"}, selected={"blob.bin"})
    plugin = NormalizeText(ctx)
    summary = plugin.process("lf")
    assert any("looks binary" in line for line in plugin.output)
    assert _read(tmp_path, "blob.bin") == b"a\x00b\r\n"
    assert "changed 0 file(s)" in summary


def test_a_read_error_is_reported(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "x\r\n"}, selected={"a.txt"})

    def boom(path, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(ctx.other_fs, "open_read",
                        lambda p: (_ for _ in ()).throw(OSError("unreadable")))
    plugin = NormalizeText(ctx)
    plugin.process("lf")
    assert any("! a.txt: unreadable" in line for line in plugin.output)


def test_a_write_error_is_reported(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "x\r\n"}, selected={"a.txt"})

    def boom(path):
        raise OSError("read-only")

    monkeypatch.setattr(ctx.other_fs, "open_write", boom)
    plugin = NormalizeText(ctx)
    summary = plugin.process("lf")
    assert any("! a.txt: read-only" in line for line in plugin.output)
    assert "changed 0 file(s)" in summary


def test_survives_a_pane_that_cannot_refresh(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "x\r\n"}, selected={"a.txt"})

    def broken():
        raise RuntimeError("pane went away")

    monkeypatch.setattr(ctx.other_panel, "refresh", broken)
    assert "changed 1 file(s)" in _run(ctx, "lf")
