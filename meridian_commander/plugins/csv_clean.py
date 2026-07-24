"""Built-in plugin: clean the tabular file selected in the *other* pane.

Highlight a CSV/TSV in the opposite pane and run cleaning verbs against it.  The
result is written to a *new* sibling file (``<name>.cleaned.csv``) -- the source
is never modified in place -- and the other pane is refreshed so the output
appears.  Prefix any command with ``preview`` to see the effect and the first
rows without writing anything.

Verbs::

    trim                     strip surrounding whitespace from every cell
    dedupe [c1,c2,...]       drop duplicate rows (by columns, or whole row)
    dropnull c1[,c2]         drop rows where any listed column is empty
    fillnull <col> <value>   replace empty cells in a column
    drop c1[,c2]             remove columns
    keep c1[,c2]             keep only these columns
    rename <old> <new>       rename a column
    filter <col> <op> <val>  keep matching rows (== != > < >= <= contains)
    retype <col> <int|float> reformat/validate a column's values
    normalize-headers        snake_case the header row

Configuration lives in ``[plugin:csv_clean]``: ``delimiter`` (blank =
auto-detect), ``encoding``, ``has_header`` (yes/no), ``max_bytes``.
"""

from __future__ import annotations

import re

from ..config import plugin_settings
from ..plugin_api import InputOutputPlugin
from . import _tabular as tabular

DEFAULTS = {
    "delimiter": "",
    "encoding": "utf-8",
    "has_header": "yes",
    "max_bytes": tabular.MAX_BYTES,
}

_FILTER_OPS = ("contains", ">=", "<=", "!=", "==", ">", "<")


class CsvClean(InputOutputPlugin):
    name = "Clean table"
    description = "Clean the CSV/TSV selected in the other pane (writes a copy)"
    prompt = "clean> "
    config_section = "csv_clean"

    def on_start(self) -> None:
        self.config = plugin_settings(self.config_section, DEFAULTS)
        super().on_start()

    @property
    def greeting(self) -> str:
        entry = tabular.selected_file(self.ctx)
        target = entry.name if entry else "<nothing selected>"
        return (f"Target (other pane): {target}\n"
                "Verbs: trim, dedupe, dropnull, fillnull, drop, keep, rename,\n"
                "       filter, retype, normalize-headers.\n"
                "Prefix with 'preview' to test without writing.")

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
        return fs, path, table

    def process(self, line: str):
        cmd = line.strip()
        if not cmd:
            return None
        preview = False
        if cmd.split(" ", 1)[0].lower() == "preview":
            preview = True
            cmd = cmd[len("preview"):].strip()
        if not cmd:
            return "usage: preview <verb> ..."

        fs, path, table = self._read()
        before = len(table.rows)
        header, rows, note = self._apply(cmd, table)
        after = len(rows)

        summary = [f"{note}  ({before} -> {after} rows)"]
        if table.truncated:
            summary.append("  ! source was truncated at the byte cap")
        if preview:
            summary.append("  (preview -- nothing written)")
            summary.extend(tabular.format_rows(header, rows[:10]))
            return summary

        out_path = tabular.derive_output_path(fs, path, "cleaned")
        tabular.write_table(fs, out_path, header, rows,
                            delimiter=table.delimiter,
                            encoding=self.config["encoding"] or "utf-8")
        try:
            self.ctx.refresh_other()
        except Exception:
            pass
        summary.append(f"  wrote {fs.basename(out_path)}")
        return summary

    # -- operations --------------------------------------------------------
    def _apply(self, cmd: str, table: tabular.Table):
        verb, _, rest = cmd.partition(" ")
        verb = verb.lower()
        rest = rest.strip()
        header = list(table.header)
        rows = [list(r) for r in table.rows]

        if verb == "trim":
            for row in rows:
                for i in range(len(row)):
                    row[i] = row[i].strip()
            return header, rows, "trim"

        if verb == "dedupe":
            idxs = table.indices(rest) if rest else list(range(len(header)))
            seen: set[tuple] = set()
            kept = []
            for row in rows:
                key = tuple(row[i] if i < len(row) else "" for i in idxs)
                if key not in seen:
                    seen.add(key)
                    kept.append(row)
            return header, kept, "dedupe"

        if verb == "dropnull":
            if not rest:
                return header, rows, "dropnull (no columns given)"
            idxs = table.indices(rest)
            kept = [row for row in rows
                    if not any(tabular.is_null(row[i] if i < len(row) else "")
                               for i in idxs)]
            return header, kept, "dropnull"

        if verb == "fillnull":
            col, _, value = rest.partition(" ")
            idx = table.index(col)
            value = value.strip()
            for row in rows:
                while len(row) <= idx:
                    row.append("")
                if tabular.is_null(row[idx]):
                    row[idx] = value
            return header, rows, "fillnull"

        if verb in ("drop", "keep"):
            idxs = table.indices(rest)
            if verb == "drop":
                keep_idx = [i for i in range(len(header)) if i not in set(idxs)]
            else:
                keep_idx = idxs
            new_header = [header[i] for i in keep_idx]
            new_rows = [[row[i] if i < len(row) else "" for i in keep_idx]
                        for row in rows]
            return new_header, new_rows, verb

        if verb == "rename":
            old, _, new = rest.partition(" ")
            idx = table.index(old)
            new = new.strip()
            if not new:
                raise ValueError("usage: rename <old> <new>")
            header[idx] = new
            return header, rows, "rename"

        if verb == "filter":
            return header, self._filter(table, rest), "filter"

        if verb == "retype":
            col, _, typ = rest.partition(" ")
            return header, self._retype(table, rows, col, typ.strip()), "retype"

        if verb in ("normalize-headers", "normalize_headers"):
            header = [_snake_case(c) for c in header]
            return header, rows, "normalize-headers"

        raise ValueError(f"unknown verb: {verb!r}")

    def _filter(self, table: tabular.Table, rest: str):
        for op in _FILTER_OPS:
            marker = f" {op} "
            if marker in rest:
                col, value = rest.split(marker, 1)
                idx = table.index(col.strip())
                value = value.strip()
                return [row for row in table.rows
                        if _match(row[idx] if idx < len(row) else "", op, value)]
        raise ValueError("usage: filter <col> <op> <value>  "
                         "(op: == != > < >= <= contains)")

    def _retype(self, table: tabular.Table, rows, col: str, typ: str):
        idx = table.index(col)
        if typ not in ("int", "float"):
            raise ValueError("retype supports int or float")
        failures = 0
        for row in rows:
            if idx >= len(row) or tabular.is_null(row[idx]):
                continue
            raw = row[idx].strip()
            try:
                num = int(raw) if typ == "int" else float(raw)
            except ValueError:
                failures += 1
                continue
            row[idx] = tabular.format_number(num)
        if failures:
            raise ValueError(f"{failures} value(s) in {col!r} are not {typ}; "
                             "nothing written")
        return rows


def _match(cell: str, op: str, value: str) -> bool:
    if op == "contains":
        return value in cell
    if op == "==":
        return cell == value
    if op == "!=":
        return cell != value
    # numeric comparisons where possible, else lexical
    try:
        a, b = float(cell), float(value)
    except ValueError:
        a, b = cell, value
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    return False


def _snake_case(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip())
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    return s.strip("_").lower() or name
