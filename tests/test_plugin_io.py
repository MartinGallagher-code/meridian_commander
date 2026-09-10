"""The shared plug-in I/O helpers."""

from __future__ import annotations

import hashlib

import pytest

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


# -- writing, and hearing about it when it fails -------------------------------

class _FailsOnClose(LocalFileSystem):
    """A backend that sends on close, the way a remote one does."""

    def open_write(self, path):
        real = super().open_write(path)

        class _Handle:
            def write(self, data):
                return len(data)          # buffered; nothing has gone yet

            def close(self):
                real.close()
                raise OSError("connection reset while sending")

        return _Handle()


def test_write_bytes_writes(tmp_path):
    path = tmp_path / "out"
    _io.write_bytes(LocalFileSystem(), str(path), b"payload")
    assert path.read_bytes() == b"payload"


def test_write_bytes_reports_a_failure_on_close(tmp_path):
    """paramiko flushes on close, and the FTP writer raises there.

    Swallowing it turned a write that never arrived into a plug-in reporting
    a file it had in fact emptied.
    """
    with pytest.raises(OSError, match="connection reset"):
        _io.write_bytes(_FailsOnClose(), str(tmp_path / "out"), b"payload")


def test_write_bytes_copes_with_a_handle_that_cannot_close(tmp_path):
    class _NoClose(LocalFileSystem):
        def open_write(self, path):
            written = []

            class _Handle:
                write = written.append

            return _Handle()

    _io.write_bytes(_NoClose(), str(tmp_path / "out"), b"payload")
