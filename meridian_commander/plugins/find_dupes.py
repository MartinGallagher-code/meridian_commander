"""Built-in plugin: find duplicate files under the other pane.

Files are grouped first by size -- a cheap read of the directory listing -- and
only the groups with more than one file of the same size are hashed to confirm
they are byte-for-byte identical.  So a tree of mostly unique files is sorted
out without hashing anything, and only genuine suspects are read.

It walks and hashes through the pane's own filesystem, so it finds duplicates on
a remote (SFTP/SSH/FTP) pane exactly as on a local one.
"""

from __future__ import annotations

from collections import defaultdict

from . import _io
from .. import sync
from ..plugin_api import InputOutputPlugin
from ..util import human_size

ALGO = "sha256"
MAX_GROUPS = 200      # duplicate groups listed before the rest are summarised


def _path(fs, root: str, rel: str) -> str:
    return fs.join(root, *rel.split("/"))


class FindDupes(InputOutputPlugin):
    name = "Find duplicates"
    description = "Group identical files under the other pane by content"
    prompt = "enter to scan> "

    @property
    def greeting(self) -> str:
        return (f"Scanning {self.ctx.other_fs.label()}:{self.ctx.other_path}\n"
                "Press Enter to find files with identical contents.")

    def process(self, line: str):
        if line.strip():
            return "Press Enter (no arguments) to scan for duplicates."

        fs, root = self.ctx.other_fs, self.ctx.other_path
        index = sync._index_tree(fs, root)

        by_size: dict[int, list[str]] = defaultdict(list)
        for rel, entry in index.items():
            by_size[entry.size or 0].append(rel)

        groups: list[tuple[str, int, list[str]]] = []
        wasted = 0
        for size, rels in by_size.items():
            if len(rels) < 2:
                continue                      # unique size: cannot be a duplicate
            by_hash: dict[str, list[str]] = defaultdict(list)
            for rel in rels:
                try:
                    digest = _io.hash_file(fs, _path(fs, root, rel), ALGO)
                except Exception as exc:
                    self.print(f"  ! {rel}: {exc}")
                    continue
                by_hash[digest].append(rel)
            for digest, members in by_hash.items():
                if len(members) > 1:
                    groups.append((digest, size, sorted(members)))
                    wasted += size * (len(members) - 1)

        if not groups:
            return "No duplicate files found."

        groups.sort(key=lambda g: g[1] * len(g[2]), reverse=True)
        for digest, size, members in groups[:MAX_GROUPS]:
            self.print(f"  {len(members)} x {human_size(size)}  "
                       f"[{digest[:12]}]")
            for rel in members:
                self.print(f"      {rel}")

        summary = (f"{len(groups)} duplicate group(s), "
                   f"{human_size(wasted)} recoverable")
        if len(groups) > MAX_GROUPS:
            summary += f" (listing stopped at {MAX_GROUPS})"
        return summary
