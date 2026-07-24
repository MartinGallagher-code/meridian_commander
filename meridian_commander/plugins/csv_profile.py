"""Built-in plugin: profile the tabular file selected in the *other* pane.

Highlight (or tag) a CSV/TSV in the opposite pane, open this plugin, and it
reports the table's shape and a per-column profile -- inferred type, null
count/percentage, distinct count and, for numeric columns, min/max/mean/median.
Follow-up commands drill into one column or preview rows.

Everything is pure standard library and bounded: the file is read at most once
per command, capped at ``max_bytes`` (configurable), and the profile is built
from that sample.  Works on remote panes (SFTP/SSH/FTP) unchanged because it
reads through the filesystem abstraction.

Configuration lives in ``[plugin:csv_profile]`` (press ``C`` -> Edit
configuration): ``delimiter`` (blank = auto-detect), ``encoding``,
``has_header`` (yes/no), ``top_n`` most-common values per categorical column,
``preview_rows`` for head/tail, and ``max_bytes``.
"""

from __future__ import annotations

import statistics
from collections import Counter

from ..config import plugin_settings
from ..plugin_api import InputOutputPlugin
from . import _tabular as tabular

DEFAULTS = {
    "delimiter": "",          # blank = auto-detect (comma/tab/;/|)
    "encoding": "utf-8",
    "has_header": "yes",
    "top_n": 5,               # most-common values shown per categorical column
    "preview_rows": 20,       # rows shown by head/tail
    "max_bytes": tabular.MAX_BYTES,
}


class CsvProfile(InputOutputPlugin):
    name = "Profile table"
    description = "Profile the CSV/TSV selected in the other pane"
    prompt = "profile> "
    config_section = "csv_profile"

    def on_start(self) -> None:
        self.config = plugin_settings(self.config_section, DEFAULTS)
        super().on_start()

    @property
    def greeting(self) -> str:
        entry = tabular.selected_file(self.ctx)
        target = entry.name if entry else "<nothing selected>"
        return (f"Target (other pane): {target}\n"
                "Enter        full profile\n"
                "col <name>   drill into one column\n"
                "head [n] / tail [n]   preview rows")

    # -- helpers -----------------------------------------------------------
    def _read(self):
        entry = tabular.selected_file(self.ctx)
        if entry is None:
            raise RuntimeError("Select a CSV/TSV file in the other pane first.")
        fs = self.ctx.other_fs
        path = fs.join(self.ctx.other_path, entry.name)
        cfg = self.config
        has_header = str(cfg["has_header"]).strip().lower() not in ("no", "false", "0")
        table = tabular.read_table(
            fs, path,
            delimiter=tabular.resolve_delimiter(cfg["delimiter"]),
            encoding=cfg["encoding"] or "utf-8",
            has_header=has_header,
            max_bytes=int(cfg["max_bytes"]),
        )
        return entry.name, table

    # -- command dispatch --------------------------------------------------
    def process(self, line: str):
        cmd = line.strip()
        name, table = self._read()
        if cmd == "" or cmd.lower() == "profile":
            return self._profile(name, table)
        verb, _, rest = cmd.partition(" ")
        verb = verb.lower()
        rest = rest.strip()
        if verb == "col":
            return self._column(table, rest)
        if verb in ("head", "tail"):
            return self._preview(table, verb, rest)
        return (f"unknown command: {cmd!r}\n"
                "try: <Enter>, col <name>, head [n], tail [n]")

    # -- full profile ------------------------------------------------------
    def _profile(self, name: str, table: tabular.Table):
        out = [f"{name}: {len(table.rows)} row(s) x {table.ncols} column(s)"
               f"  (delimiter {table.delimiter!r})"]
        if table.truncated:
            out.append("  ! file was truncated at the byte cap; stats are partial")
        top_n = int(self.config["top_n"])
        for i, col in enumerate(table.header):
            values = table.column(i)
            out.extend(self._column_lines(col, values, top_n))
        return out

    def _column_lines(self, col: str, values, top_n: int):
        n = len(values)
        nulls = sum(1 for v in values if tabular.is_null(v))
        non_null = [v for v in values if not tabular.is_null(v)]
        distinct = len(set(non_null))
        ctype = tabular.infer_type(values)
        pct = (nulls / n * 100) if n else 0.0
        head = (f"  {col} [{ctype}]  nulls={nulls} ({pct:.0f}%)  "
                f"distinct={distinct}")
        lines = [head]
        if ctype in ("int", "float"):
            nums = tabular.numeric_values(values)
            if nums:
                fn = tabular.format_number
                stats = (f"    min={fn(min(nums))} max={fn(max(nums))} "
                         f"mean={fn(statistics.mean(nums))} "
                         f"median={fn(statistics.median(nums))}")
                lines.append(stats)
        else:
            common = Counter(non_null).most_common(top_n)
            if common:
                shown = ", ".join(f"{v!r}x{c}" for v, c in common)
                lines.append(f"    top: {shown}")
        return lines

    # -- one column --------------------------------------------------------
    def _column(self, table: tabular.Table, name: str):
        if not name:
            return "usage: col <name>"
        idx = table.index(name)  # raises with a clear message if missing
        values = table.column(idx)
        ctype = tabular.infer_type(values)
        out = self._column_lines(table.header[idx], values, int(self.config["top_n"]))
        if ctype in ("int", "float"):
            out.append("    distribution:")
            out.extend(tabular.histogram(tabular.numeric_values(values)))
        else:
            non_null = [v for v in values if not tabular.is_null(v)]
            common = Counter(non_null).most_common(20)
            out.append("    value counts:")
            for v, c in common:
                out.append(f"      {v!r}: {c}")
        return out

    # -- preview -----------------------------------------------------------
    def _preview(self, table: tabular.Table, verb: str, rest: str):
        try:
            count = int(rest) if rest else int(self.config["preview_rows"])
        except ValueError:
            return f"usage: {verb} [n]"
        rows = table.rows[:count] if verb == "head" else table.rows[-count:]
        return tabular.format_rows(table.header, rows)
