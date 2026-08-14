"""Built-in plugin: recursively search the *other* pane's files by content.

The sibling of :mod:`find_files`: where that matches file *names*, this matches
what is *inside* them.  It walks the opposite pane's filesystem through the
plugin context, so it greps remote panes (SFTP/SSH/FTP) exactly as it greps
local ones -- the search runs wherever the pane is pointed, with no files
dragged across the wire beyond the bytes it reads to scan them.

The whole input line is the text to find; the match is a plain, case-insensitive
substring by default.  Prefix the line with ``re:`` to match a regular
expression instead, e.g. ``re:def\\s+\\w+``.
"""

from __future__ import annotations

import re

from ..plugin_api import InputOutputPlugin

MAX_RESULTS = 500      # matching lines shown before the search stops
MAX_DEPTH = 12         # how deep the walk descends
MAX_FILE_BYTES = 2 * 1024 * 1024   # per-file scan cap; larger files are noted
CHUNK = 64 * 1024


class GrepFiles(InputOutputPlugin):
    name = "Grep in other pane"
    description = "Recursively search the other pane's files by content"
    prompt = "text> "

    @property
    def greeting(self) -> str:  # dynamic: shows what will be searched
        return (f"Searching under {self.ctx.other_fs.label()}:{self.ctx.other_path}\n"
                "Enter text to find (case-insensitive), or re:<pattern> for a regex.")

    def process(self, line: str):
        needle = line.strip()
        if not needle:
            return "Give some text to find, or re:<pattern>."

        if needle.startswith("re:"):
            expr = needle[3:]
            if not expr:
                return "Give a pattern after 're:'."
            try:
                matcher = re.compile(expr, re.IGNORECASE)
            except re.error as exc:
                return f"Bad regular expression: {exc}"
            predicate = lambda text: matcher.search(text) is not None
        else:
            lowered = needle.lower()
            predicate = lambda text: lowered in text.lower()

        fs = self.ctx.other_fs
        matches = 0
        truncated = False

        def scan(path: str, rel: str) -> None:
            nonlocal matches, truncated
            text, clipped = self._read(fs, path)
            if text is None:
                return  # binary or unreadable; already noted where it matters
            for lineno, content in enumerate(text.splitlines(), start=1):
                if predicate(content):
                    self.print(f"  {rel}:{lineno}: {content.strip()[:200]}")
                    matches += 1
                    if matches >= MAX_RESULTS:
                        truncated = True
                        return
            if clipped:
                self.print(f"  ~ {rel}: scanned only the first "
                           f"{MAX_FILE_BYTES // 1024} KiB")

        def walk(path: str, rel: str, depth: int) -> None:
            nonlocal truncated
            if truncated or depth > MAX_DEPTH:
                return
            try:
                entries = fs.listdir(path)
            except Exception as exc:
                self.print(f"  ! {rel or '.'}: {exc}")
                return
            for entry in sorted(entries, key=lambda e: e.name.lower()):
                if truncated:
                    return
                child = f"{rel}/{entry.name}" if rel else entry.name
                if entry.is_dir:
                    if not entry.is_symlink:
                        walk(fs.join(path, entry.name), child, depth + 1)
                elif not entry.is_symlink:
                    scan(fs.join(path, entry.name), child)

        walk(self.ctx.other_path, "", 0)
        summary = f"{matches} match(es)"
        if truncated:
            summary += f" (stopped at {MAX_RESULTS})"
        return summary

    def _read(self, fs, path: str):
        """Return ``(text, truncated)``; ``(None, False)`` for binary/unreadable.

        Reads at most :data:`MAX_FILE_BYTES` so one enormous file cannot stall
        the search, and treats a NUL byte in the first chunk as "binary, skip".
        """
        try:
            stream = fs.open_read(path)
        except Exception as exc:
            self.print(f"  ! {path}: {exc}")
            return None, False
        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            while total < MAX_FILE_BYTES:
                chunk = stream.read(min(CHUNK, MAX_FILE_BYTES - total))
                if not chunk:
                    break
                if not chunks and b"\x00" in chunk:
                    return None, False   # binary file
                chunks.append(chunk)
                total += len(chunk)
            else:
                if stream.read(1):
                    truncated = True
        except Exception as exc:
            self.print(f"  ! {path}: {exc}")
            return None, False
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        return b"".join(chunks).decode("utf-8", errors="replace"), truncated
