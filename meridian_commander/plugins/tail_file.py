"""Built-in plugin: show the tail of a file, once or as it grows.

Play a log into the output area, from whichever pane it lives on:

* ``tail <file> [n]``   -- show the last ``n`` lines (default 20)
* ``follow <file> [n]`` -- show the last ``n`` lines, then keep appending new
  ones as the file grows (like ``tail -f``)
* ``stop``              -- stop following

``<file>`` is resolved against the *other* pane, so it reads local and remote
(SFTP/SSH/FTP) files alike.  Only the last :data:`MAX_TAIL_BYTES` are ever held
in memory, so following a busy multi-gigabyte log cannot exhaust it; if the file
is rotated (it shrinks), following notices and picks up the new file.
"""

from __future__ import annotations

from . import _io
from ..plugin_api import Command, InputOutputPlugin

DEFAULT_LINES = 20
MAX_TAIL_BYTES = 256 * 1024      # rolling window kept while reading


def read_tail(fs, path: str, max_bytes: int | None = None):
    """Return ``(last_bytes, total_size)`` for ``path``.

    Reads the whole file but keeps only the trailing ``max_bytes`` in memory, so
    the size is exact while the buffer stays bounded.
    """
    if max_bytes is None:
        max_bytes = MAX_TAIL_BYTES
    stream = fs.open_read(path)
    buf = bytearray()
    total = 0
    try:
        while True:
            chunk = stream.read(_io.CHUNK)
            if not chunk:
                break
            total += len(chunk)
            buf += chunk
            if len(buf) > max_bytes:
                del buf[:-max_bytes]
    finally:
        _io.close(stream)
    return bytes(buf), total


class TailFile(InputOutputPlugin):
    name = "Tail file"
    commands = (
        Command("tail", "show the end of a file", arg="path",
                allow_bare=False),
        Command("follow", "stream a file as it grows", arg="path",
                allow_bare=False),
        Command("stop", "stop following"),
    )
    description = "Show the end of a file, once or as it grows (tail -f)"
    prompt = "F2 for commands, or type one> "
    wants_timer = True     # the app polls tick() so 'follow' can stream

    def on_start(self) -> None:
        super().on_start()
        self._follow_path: str | None = None
        self._last_size = 0

    @property
    def greeting(self) -> str:
        return (f"Files resolve against {self.ctx.other_fs.label()}:"
                f"{self.ctx.other_path}\n"
                "Commands: tail <file> [n] | follow <file> [n] | stop")

    def process(self, line: str):
        parts = line.split()
        if not parts:
            return None
        verb = parts[0].lower()

        if verb == "stop":
            if self._follow_path is None:
                return "Not following anything."
            was = self._follow_path
            self._follow_path = None
            return f"Stopped following {was}."

        if verb not in ("tail", "follow"):
            return f"unknown command '{verb}' (try tail, follow or stop)"
        if len(parts) < 2:
            return f"usage: {verb} <file> [lines]"

        path = self._resolve(parts[1])
        lines = DEFAULT_LINES
        if len(parts) > 2:
            if not parts[2].isdigit() or int(parts[2]) < 1:
                return "usage: {} <file> [positive number]".format(verb)
            lines = int(parts[2])

        try:
            data, total = read_tail(self.ctx.other_fs, path)
        except Exception as exc:
            return f"Could not read {parts[1]}: {exc}"

        tail_lines = data.decode("utf-8", errors="replace").splitlines()
        if total > len(data):
            self.print(f"  ... (showing the last {len(data) // 1024} KiB)")
        for text in tail_lines[-lines:]:
            self.print(f"  {text}")

        if verb == "follow":
            self._follow_path = path
            self._last_size = total
            return f"Following {parts[1]} -- 'stop' to end."
        return f"({len(tail_lines)} line(s) in the last block)"

    def tick(self) -> None:
        """Poll a followed file and print whatever has been appended."""
        if self._follow_path is None:
            return
        fs = self.ctx.other_fs
        try:
            new_size = fs.stat(self._follow_path).size or 0
        except Exception:
            return   # file vanished for now; keep following, try again next tick
        if new_size == self._last_size:
            return
        if new_size < self._last_size:
            self.print("  ... (file rotated)")
            self._last_size = 0

        try:
            data, total = read_tail(fs, self._follow_path)
        except Exception:
            return
        delta = total - self._last_size
        if delta > 0:
            fresh = data[-delta:] if delta <= len(data) else data
            for text in fresh.decode("utf-8", errors="replace").splitlines():
                self.print(f"  {text}")
        self._last_size = total

    def _resolve(self, arg: str) -> str:
        if arg.startswith("/"):
            return arg
        return self.ctx.other_fs.join(self.ctx.other_path, arg)
