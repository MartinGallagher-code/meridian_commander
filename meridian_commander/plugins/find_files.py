"""Built-in plugin: recursively search the *other* pane's directory.

This is the simplest useful demonstration of the plugin API: it reads the
opposite pane's filesystem through the plugin context, so it searches remote
panes (SFTP/SSH/FTP) exactly as it searches local ones.
"""

from __future__ import annotations

import fnmatch

from ..plugin_api import InputOutputPlugin

MAX_RESULTS = 500
MAX_DEPTH = 12


class FindFiles(InputOutputPlugin):
    name = "Find in other pane"
    description = "Recursively search the other pane's directory by name"
    prompt = "pattern> "

    @property
    def greeting(self) -> str:  # dynamic: shows what will be searched
        return (f"Searching under {self.ctx.other_fs.label()}:{self.ctx.other_path}\n"
                f"Enter a glob pattern (e.g. *.py, config*, *.log).")

    def process(self, line: str):
        pattern = line.strip()
        if not pattern:
            return "Give a pattern, e.g. *.txt"
        fs = self.ctx.other_fs
        root = self.ctx.other_path
        matches = 0
        truncated = False

        def walk(path: str, rel: str, depth: int) -> None:
            nonlocal matches, truncated
            if truncated or depth > MAX_DEPTH:
                return
            try:
                entries = fs.listdir(path)
            except Exception as exc:
                self.print(f"  ! {rel or '.'}: {exc}")
                return
            for entry in sorted(entries, key=lambda e: e.name.lower()):
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                if fnmatch.fnmatch(entry.name, pattern):
                    marker = "/" if entry.is_dir else ""
                    self.print(f"  {child_rel}{marker}")
                    matches += 1
                    if matches >= MAX_RESULTS:
                        truncated = True
                        return
                if entry.is_dir and not entry.is_symlink:
                    walk(fs.join(path, entry.name), child_rel, depth + 1)

        walk(root, "", 0)
        summary = f"{matches} match(es)"
        if truncated:
            summary += f" (stopped at {MAX_RESULTS})"
        return summary
