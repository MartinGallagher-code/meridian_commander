"""Built-in plugin: identify a file and show a hex dump of its start.

Point the other pane's cursor at a file (or tag one) and open this to see what
it actually is -- the type guessed from its magic bytes, its size, and a classic
offset/hex/ASCII dump of the first bytes.  Handy for a file with no extension,
or one the viewers decline to open.

``bytes <n>`` changes how many bytes are dumped.  It reads through the pane's
filesystem, so it inspects remote files too, and never reads more than the dump
length.
"""

from __future__ import annotations

from . import _io
from ..plugin_api import Command, InputOutputPlugin
from ..util import human_size

DEFAULT_DUMP = 256
MAX_DUMP = 4096

#: (offset, signature bytes, label), checked in order; first match wins.
_MAGIC = [
    (0, b"\x89PNG\r\n\x1a\n", "PNG image"),
    (0, b"\xff\xd8\xff", "JPEG image"),
    (0, b"GIF87a", "GIF image"),
    (0, b"GIF89a", "GIF image"),
    (0, b"BM", "BMP image"),
    (0, b"%PDF-", "PDF document"),
    (0, b"PK\x03\x04", "ZIP archive (or .docx/.xlsx/.jar)"),
    (0, b"PK\x05\x06", "ZIP archive (empty)"),
    (0, b"\x1f\x8b", "gzip data"),
    (0, b"BZh", "bzip2 data"),
    (0, b"\xfd7zXZ\x00", "xz data"),
    (0, b"7z\xbc\xaf\x27\x1c", "7-zip archive"),
    (0, b"\x7fELF", "ELF executable"),
    (0, b"\xca\xfe\xba\xbe", "Java class / Mach-O fat binary"),
    (0, b"#!", "script (shebang)"),
    (0, b"\xef\xbb\xbf", "UTF-8 text (BOM)"),
    (0, b"OggS", "Ogg media"),
    (0, b"fLaC", "FLAC audio"),
    (0, b"ID3", "MP3 audio (ID3)"),
    (0, b"\x25\x21", "PostScript"),
    (0, b"SQLite format 3\x00", "SQLite database"),
]


def identify(data: bytes) -> str:
    """A human label for a file from its leading bytes."""
    for offset, sig, label in _MAGIC:
        if data[offset:offset + len(sig)] == sig:
            return label
    if not data:
        return "empty file"
    if b"\x00" in data:
        return "binary data"
    return "text"


def hexdump(data: bytes) -> list[str]:
    """Classic 16-byte-per-row offset / hex / ASCII dump lines."""
    rows = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        hexpart = f"{hexpart:<47}"      # 16*3-1 columns, padded
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append(f"  {off:08x}  {hexpart}  {ascii_}")
    return rows


class InspectFile(InputOutputPlugin):
    name = "Inspect file"
    commands = (
        Command("", "identify the cursor file and dump its start",
                label="inspect"),
        Command("bytes", "change how many bytes are dumped", arg="text"),
    )
    description = "Identify the other pane's file and hex-dump its start"
    prompt = "enter to inspect | bytes <n>> "

    def on_start(self) -> None:
        super().on_start()
        self._dump = DEFAULT_DUMP

    @property
    def greeting(self) -> str:
        return ("Inspect the file under the other pane's cursor.\n"
                "Enter to inspect; 'bytes <n>' to change the dump length.")

    def process(self, line: str):
        parts = line.split()
        if parts and parts[0].lower() == "bytes":
            if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) < 1:
                return "usage: bytes <positive number>"
            self._dump = min(int(parts[1]), MAX_DUMP)
            return f"Dump length set to {self._dump} bytes."
        if parts:
            return "Press Enter to inspect, or 'bytes <n>' to set the length."

        selected = [e for e in self.ctx.other_selected() if not e.is_dir]
        if not selected:
            raise RuntimeError("Put the other pane's cursor on a file first.")
        entry = selected[0]

        fs, root = self.ctx.other_fs, self.ctx.other_path
        path = fs.join(root, entry.name)
        try:
            data, truncated = _io.read_bytes(fs, path, max_bytes=self._dump)
        except Exception as exc:
            return f"Could not read {entry.name}: {exc}"

        result = [
            f"{entry.name}",
            f"  type:  {identify(data)}",
            f"  size:  {human_size(entry.size or 0).strip()} "
            f"({entry.size or 0} bytes)",
            f"  dump:  first {len(data)} byte(s)"
            + ("  (more not shown)" if truncated else ""),
            "",
        ]
        result.extend(hexdump(data))
        return result
