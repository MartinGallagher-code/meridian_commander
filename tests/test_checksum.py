"""The Checksum / verify built-in plug-in."""

from __future__ import annotations

import hashlib

import pytest

from meridian_commander.plugins.checksum import (
    Checksum,
    _parse_sums_line,
    hash_stream,
)


def _run(ctx, command):
    result = Checksum(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# -- helpers in isolation ------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("abc123  file.txt", ("abc123", "file.txt")),
    ("deadBEEF *binmode.bin", ("deadBEEF", "binmode.bin")),
    ("", None),
    ("   ", None),
    ("# a comment", None),
    ("notahash file.txt", None),
    ("onlyonefield", None),
])
def test_parse_sums_line(line, expected):
    assert _parse_sums_line(line) == expected


def test_hash_stream_matches_hashlib(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "hello"})
    digest = hash_stream(ctx.other_fs, str(tmp_path / "data" / "a.txt"), "sha256")
    assert digest == _sha256("hello")


# -- hashing -------------------------------------------------------------------

def test_greeting_lists_the_tagged_files(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert "a.txt" in Checksum(ctx).greeting

    ctx.other_panel.selected = set()
    ctx.other_panel.move_to(0)
    assert "<nothing tagged>" in Checksum(ctx).greeting


def test_bare_enter_hashes_with_sha256(data_ctx):
    ctx = data_ctx({"a.txt": "hello"}, selected={"a.txt"})
    out = _run(ctx, "")
    assert f"{_sha256('hello')}  a.txt" in out


def test_named_algorithms(data_ctx):
    ctx = data_ctx({"a.txt": "hello"}, selected={"a.txt"})
    assert hashlib.md5(b"hello").hexdigest() in _run(ctx, "md5")
    assert hashlib.sha1(b"hello").hexdigest() in _run(ctx, "sha1")


def test_needs_a_tagged_file(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    ctx.other_panel.move_to(0)                  # the ".." entry
    with pytest.raises(RuntimeError, match="Tag at least 1"):
        Checksum(ctx).process("sha256")


def test_rejects_an_unknown_command(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert "unknown command" in _run(ctx, "wobble")


def test_a_directory_in_the_selection_is_ignored(data_ctx):
    ctx = data_ctx({"a.txt": "1", "d/inner.txt": "2"},
                   selected={"a.txt", "d"})
    out = _run(ctx, "sha256")
    assert "a.txt" in out and "  d" not in out


def test_hash_reports_a_file_error(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})

    def boom(path):
        raise OSError("nope")

    monkeypatch.setattr(ctx.other_fs, "open_read", boom)
    assert "! a.txt: nope" in _run(ctx, "sha256")


# -- write ---------------------------------------------------------------------

def test_write_produces_a_sums_file(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "hello", "b.txt": "world"},
                   selected={"a.txt", "b.txt"})
    out = _run(ctx, "write")
    assert "Wrote SHA256SUMS (2 file(s), sha256)" in out
    written = (tmp_path / "data" / "SHA256SUMS").read_text()
    assert f"{_sha256('hello')}  a.txt" in written
    assert f"{_sha256('world')}  b.txt" in written


def test_write_takes_an_algorithm_and_a_name(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "hello"}, selected={"a.txt"})
    out = _run(ctx, "write md5 sums.txt")
    assert "Wrote sums.txt (1 file(s), md5)" in out
    written = (tmp_path / "data" / "sums.txt").read_text()
    assert hashlib.md5(b"hello").hexdigest() in written


def test_write_reports_errors_and_writes_nothing_when_all_fail(data_ctx,
                                                               monkeypatch,
                                                               tmp_path):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})

    def boom(path):
        raise OSError("nope")

    monkeypatch.setattr(ctx.other_fs, "open_read", boom)
    plugin = Checksum(ctx)
    assert plugin.process("write") == "Nothing hashed; wrote no sums file."
    assert any("! a.txt: nope" in line for line in plugin.output)
    assert not (tmp_path / "data" / "SHA256SUMS").exists()


# -- verify --------------------------------------------------------------------

def test_verify_reports_ok_failed_and_missing(data_ctx, tmp_path):
    sums = (f"{_sha256('hello')}  a.txt\n"
            f"{_sha256('WRONG')}  b.txt\n"
            f"{_sha256('x')}  gone.txt\n")
    ctx = data_ctx({"a.txt": "hello", "b.txt": "world", "SHA256SUMS": sums})
    plugin = Checksum(ctx)
    summary = plugin.process("verify")
    printed = "\n".join(plugin.output)
    assert "OK       a.txt" in printed
    assert "FAILED   b.txt" in printed
    assert "missing  gone.txt" in printed
    assert summary == "1 OK, 1 FAILED, 1 missing"


def test_verify_round_trips_with_write(data_ctx):
    ctx = data_ctx({"a.txt": "hello", "b.txt": "world"},
                   selected={"a.txt", "b.txt"})
    Checksum(ctx).process("write")
    # A fresh plugin instance verifies what the first one wrote.
    ctx.other_panel.selected = set()
    assert Checksum(ctx).process("verify") == "2 OK"


def test_verify_detects_the_algorithm_from_the_digest_length(data_ctx):
    ctx = data_ctx({"a.txt": "hello"}, selected={"a.txt"})
    Checksum(ctx).process("write md5 sums.txt")
    ctx.other_panel.selected = set()
    assert Checksum(ctx).process("verify sums.txt") == "1 OK"


def test_verify_reports_a_missing_sums_file(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    assert "Could not read SHA256SUMS" in Checksum(ctx).process("verify")


def test_verify_skips_blank_and_comment_lines(data_ctx):
    sums = f"# a comment\n\n{_sha256('hi')}  a.txt\n"
    ctx = data_ctx({"a.txt": "hi", "SHA256SUMS": sums})
    assert Checksum(ctx).process("verify") == "1 OK"


def test_verify_flags_an_unrecognised_hash_length(data_ctx):
    ctx = data_ctx({"a.txt": "hi", "SHA256SUMS": "abc123  a.txt\n"})
    plugin = Checksum(ctx)
    summary = plugin.process("verify")
    assert any("unrecognised hash length" in line for line in plugin.output)
    assert summary == "0 OK"


def test_verify_reports_a_file_that_fails_to_hash(data_ctx, monkeypatch):
    sums = f"{_sha256('hi')}  a.txt\n"
    ctx = data_ctx({"a.txt": "hi", "SHA256SUMS": sums})
    real_open = ctx.other_fs.open_read

    def boom(path):
        if path.endswith("/a.txt"):
            raise OSError("io error")
        return real_open(path)

    monkeypatch.setattr(ctx.other_fs, "open_read", boom)
    plugin = Checksum(ctx)
    summary = plugin.process("verify")
    assert any("! a.txt: io error" in line for line in plugin.output)
    assert "FAILED" in summary


def test_survives_a_pane_that_cannot_refresh(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})

    def broken():
        raise RuntimeError("pane went away")

    monkeypatch.setattr(ctx.other_panel, "refresh", broken)
    assert "Wrote" in _run(ctx, "write")
