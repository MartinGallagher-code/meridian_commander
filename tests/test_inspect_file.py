"""The Inspect-file built-in plug-in."""

from __future__ import annotations

import pytest

from meridian_commander.plugins.inspect_file import (
    InspectFile,
    hexdump,
    identify,
)


def _run(ctx, command=""):
    result = InspectFile(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


# -- identify / hexdump in isolation ------------------------------------------

@pytest.mark.parametrize("data, label", [
    (b"\x89PNG\r\n\x1a\n\x00\x00", "PNG image"),
    (b"%PDF-1.7", "PDF document"),
    (b"PK\x03\x04rest", "ZIP archive (or .docx/.xlsx/.jar)"),
    (b"\x7fELF\x02", "ELF executable"),
    (b"#!/bin/sh\n", "script (shebang)"),
    (b"plain text here", "text"),
    (b"a\x00b", "binary data"),
    (b"", "empty file"),
])
def test_identify(data, label):
    assert identify(data) == label


def test_hexdump_layout():
    rows = hexdump(b"AB\x01")
    assert rows[0].startswith("  00000000  ")
    assert "41 42 01" in rows[0]
    assert rows[0].rstrip().endswith("AB.")


def test_hexdump_multiple_rows():
    assert len(hexdump(b"x" * 33)) == 3     # 16 + 16 + 1


# -- the plug-in ---------------------------------------------------------------

def test_greeting(data_ctx):
    ctx = data_ctx({"a.bin": "x"})
    assert "Inspect the file" in InspectFile(ctx).greeting


def test_inspects_the_cursor_file(data_ctx):
    ctx = data_ctx({"hello.txt": "hello world"}, selected={"hello.txt"})
    out = _run(ctx)
    assert "hello.txt" in out
    assert "type:  text" in out
    assert "11 bytes" in out
    assert "68 65 6c 6c 6f" in out       # "hello" in hex


def test_reports_a_detected_type(data_ctx, tmp_path):
    ctx = data_ctx({"img.dat": "placeholder"}, selected={"img.dat"})
    # Write real bytes (text-mode writing would mangle the high magic bytes).
    (tmp_path / "data" / "img.dat").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00rest")
    assert "PNG image" in _run(ctx)


def test_needs_a_file_under_the_cursor(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    ctx.other_panel.move_to(0)                  # the ".." entry
    with pytest.raises(RuntimeError, match="cursor on a file"):
        InspectFile(ctx).process("")


def test_bytes_sets_the_dump_length(data_ctx):
    ctx = data_ctx({"a.bin": "x" * 100}, selected={"a.bin"})
    plugin = InspectFile(ctx)
    assert "Dump length set to 16" in plugin.process("bytes 16")
    result = plugin.process("")
    dump_rows = [line for line in result if line.strip().startswith("000000")]
    assert len(dump_rows) == 1                  # 16 bytes -> one row


def test_bytes_is_capped(data_ctx):
    ctx = data_ctx({"a.bin": "x"}, selected={"a.bin"})
    plugin = InspectFile(ctx)
    out = plugin.process("bytes 999999")
    assert "4096" in out                        # clamped to MAX_DUMP


def test_bytes_rejects_a_bad_argument(data_ctx):
    ctx = data_ctx({"a.bin": "x"}, selected={"a.bin"})
    assert "usage: bytes" in InspectFile(ctx).process("bytes lots")


def test_other_arguments_are_rejected(data_ctx):
    ctx = data_ctx({"a.bin": "x"}, selected={"a.bin"})
    assert "Press Enter to inspect" in InspectFile(ctx).process("wobble")


def test_a_truncated_dump_is_noted(data_ctx):
    ctx = data_ctx({"big.bin": "y" * 500}, selected={"big.bin"})
    plugin = InspectFile(ctx)
    plugin.process("bytes 16")
    out = "\n".join(plugin.process(""))
    assert "more not shown" in out


def test_a_read_error_is_reported(data_ctx, monkeypatch):
    ctx = data_ctx({"a.bin": "x"}, selected={"a.bin"})

    def boom(path):
        raise OSError("unreadable")

    monkeypatch.setattr(ctx.other_fs, "open_read", boom)
    assert "Could not read a.bin" in _run(ctx)


def test_a_directory_selection_is_ignored(data_ctx):
    ctx = data_ctx({"d/inner.txt": "x", "a.txt": "hello"},
                   selected={"d", "a.txt"})
    # The directory is filtered out; the file is inspected.
    assert "a.txt" in _run(ctx)
