"""The shared plug-in I/O helpers."""

from __future__ import annotations

import hashlib

from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.plugins import _io


def test_read_bytes_reads_the_whole_file(tmp_path):
    (tmp_path / "a").write_bytes(b"hello")
    assert _io.read_bytes(LocalFileSystem(), str(tmp_path / "a")) == (b"hello", False)


def test_read_bytes_truncates_at_the_cap_and_says_so(tmp_path):
    (tmp_path / "a").write_bytes(b"hello world")
    data, truncated = _io.read_bytes(LocalFileSystem(), str(tmp_path / "a"),
                                     max_bytes=5)
    assert data == b"hello"
    assert truncated is True


def test_read_bytes_at_the_exact_cap_is_not_truncated(tmp_path):
    (tmp_path / "a").write_bytes(b"hello")
    assert _io.read_bytes(LocalFileSystem(), str(tmp_path / "a"),
                          max_bytes=5) == (b"hello", False)


def test_write_bytes_round_trips(tmp_path):
    _io.write_bytes(LocalFileSystem(), str(tmp_path / "a"), b"data")
    assert (tmp_path / "a").read_bytes() == b"data"


def test_hash_file_matches_hashlib(tmp_path):
    (tmp_path / "a").write_bytes(b"hello")
    assert _io.hash_file(LocalFileSystem(), str(tmp_path / "a"), "sha256") == \
        hashlib.sha256(b"hello").hexdigest()


def test_close_swallows_a_failing_close():
    class _S:
        def close(self):
            raise OSError("boom")

    _io.close(_S())   # must not raise


def test_close_ignores_a_stream_without_a_close():
    _io.close(object())
