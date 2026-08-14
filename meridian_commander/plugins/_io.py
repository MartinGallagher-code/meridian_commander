"""Filesystem I/O helpers shared by the built-in plug-ins.

Everything goes through the :class:`~meridian_commander.filesystems.FileSystem`
abstraction, so a plug-in that uses these reads and writes local *and* remote
(SFTP/SSH/FTP) files with the same code.  The close/read error handling lives
here once instead of being copied into every plug-in.

The module name starts with an underscore so plug-in discovery ignores it -- it
holds no plug-in classes, only helpers.
"""

from __future__ import annotations

import hashlib

CHUNK = 64 * 1024


def close(stream) -> None:
    """Close ``stream`` if it can be closed, swallowing any error."""
    fn = getattr(stream, "close", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def read_bytes(fs, path, *, max_bytes: int | None = None):
    """Read ``path`` and return ``(data, truncated)``.

    With ``max_bytes`` the read stops at that many bytes and ``truncated`` says
    whether more was left, so one huge file cannot exhaust memory; without it
    the whole file is read and ``truncated`` is always ``False``.
    """
    stream = fs.open_read(path)
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        while max_bytes is None or total < max_bytes:
            want = CHUNK if max_bytes is None else min(CHUNK, max_bytes - total)
            chunk = stream.read(want)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        else:
            if stream.read(1):
                truncated = True
    finally:
        close(stream)
    return b"".join(chunks), truncated


def write_bytes(fs, path, data: bytes) -> None:
    """Write ``data`` to ``path`` through the filesystem backend."""
    stream = fs.open_write(path)
    try:
        stream.write(data)
    finally:
        close(stream)


def hash_file(fs, path, algo: str) -> str:
    """The hex digest of ``path`` under ``algo``, read in bounded chunks."""
    digest = hashlib.new(algo)
    stream = fs.open_read(path)
    try:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        close(stream)
    return digest.hexdigest()
