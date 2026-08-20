"""Built-in plugin: what is taking up the space under the other pane.

An ``ncdu``-lite: it sums every file beneath each of the other pane's immediate
children and lists them biggest-first with a little bar, so an overgrown
directory is obvious at a glance.  It walks through the pane's own filesystem --
nothing but ``listdir``/``stat`` -- so it measures a remote (SFTP/SSH/FTP) pane
just as well as a local one, without a shell on the far side.
"""

from __future__ import annotations

from .. import sync
from ..plugin_api import Command, InputOutputPlugin
from ..util import human_size

BAR_WIDTH = 20      # columns of bar drawn for the largest entry


def _tree_bytes(fs, path: str) -> tuple[int, int]:
    """Total ``(bytes, file_count)`` beneath ``path`` (a file counts as itself)."""
    index = sync._index_tree(fs, path)
    total = sum(e.size or 0 for e in index.values())
    return total, len(index)


class DiskUsage(InputOutputPlugin):
    name = "Disk usage"
    commands = (
        Command("", "size everything under the other pane", label="measure"),
    )
    description = "Size of each item under the other pane, biggest first"
    prompt = "enter to measure> "

    @property
    def greeting(self) -> str:
        return (f"Measuring {self.ctx.other_fs.label()}:{self.ctx.other_path}\n"
                "Press Enter to size each item under it, largest first.")

    def process(self, line: str):
        if line.strip():
            return "Press Enter (no arguments) to measure the other pane."

        fs, root = self.ctx.other_fs, self.ctx.other_path
        try:
            entries = fs.listdir(root)
        except Exception as exc:
            return f"Could not read {root}: {exc}"
        if not entries:
            return "Nothing here to measure."

        rows: list[tuple[int, int, str]] = []
        grand = 0
        for entry in entries:
            path = fs.join(root, entry.name)
            if entry.is_dir and not entry.is_symlink:
                size, count = _tree_bytes(fs, path)
                name = entry.name + "/"
            else:
                size, count = (entry.size or 0), 1
                name = entry.name
            rows.append((size, count, name))
            grand += size

        rows.sort(key=lambda r: r[0], reverse=True)
        biggest = rows[0][0]
        for size, count, name in rows:
            bar = self._bar(size, biggest)
            self.print(f"  {human_size(size)}  {bar}  {name}"
                       + (f"  ({count} files)" if count > 1 else ""))
        return f"Total {human_size(grand)} across {len(rows)} item(s)."

    @staticmethod
    def _bar(size: int, biggest: int) -> str:
        filled = round(BAR_WIDTH * size / biggest) if biggest > 0 else 0
        return "#" * filled + "-" * (BAR_WIDTH - filled)
