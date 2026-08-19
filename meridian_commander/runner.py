"""Deciding how to run a file: the ``#!`` line and the executable bit.

The rule Enter follows in the panels, factored out so it can be tested on its
own: an **executable** file runs directly (the kernel honours its ``#!`` line,
and a binary needs none); a non-executable file with a ``#!`` line is handed
to the interpreter that line names, so a fresh ``script.py`` runs without a
``chmod +x`` first.  A file with neither is not runnable and Enter falls back
to the viewer.

Everything reads through the :class:`~meridian_commander.filesystems.FileSystem`
abstraction, so the same rule serves local and remote (SFTP/SSH) panes -- and
the head of the file is only read when the executable bit alone cannot decide,
which keeps Enter on a remote pane from paying a round trip it does not need.
"""

from __future__ import annotations

#: How much of the file's head is read when looking for the ``#!`` line.
SHEBANG_BYTES = 256


def parse_shebang(head: bytes) -> list[str] | None:
    """The interpreter argv from a file's first line, or ``None``.

    POSIX allows the interpreter plus at most one argument, so the result is
    one or two strings (e.g. ``["/usr/bin/env", "python3"]``).
    """
    line = head.split(b"\n", 1)[0]
    if not line.startswith(b"#!"):
        return None
    text = line[2:].strip().decode("utf-8", errors="replace")
    if not text:
        return None
    return text.split(None, 1)


def read_head(fs, path: str) -> bytes:
    """The first :data:`SHEBANG_BYTES` of ``path`` through the pane's backend."""
    stream = fs.open_read(path)
    try:
        return stream.read(SHEBANG_BYTES) or b""
    finally:
        try:
            stream.close()
        except Exception:
            pass


def run_argv(fs, path: str, mode: int) -> list[str] | None:
    """How to start ``path``: an argv, or ``None`` when it is not runnable.

    ``mode`` is the file's permission bits, as the panel listing already
    holds them.  An unreadable file is simply not runnable rather than an
    error -- Enter then falls back to the viewer, which will say why.
    """
    if mode & 0o111:
        return [path]
    try:
        shebang = parse_shebang(read_head(fs, path))
    except Exception:
        return None
    if shebang:
        return shebang + [path]
    return None
