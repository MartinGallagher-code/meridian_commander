"""Built-in plugin: compose datasets from files in the *other* pane.

This leans on Meridian Commander's two-pane model: tag one or more CSV/TSV files
in the opposite pane, open this plugin, and combine or reshape them.  Results are
written into the other pane's directory and the pane is refreshed.

Verbs::

    concat                   stack all tagged files (union of columns)
    join <key>               join the first two tagged files on a key column
    sample <n|n%>            random-free head sample of the tagged file
    split <col>              one file per value of a column
    to-jsonl                 convert the tagged CSV to JSON-lines
    from-jsonl               convert the tagged JSON-lines file to CSV
    groupby <col> <agg>[:col]  aggregate rows (count/sum/mean/min/max)

``groupby`` uses pandas when the optional ``meridian-commander[data]`` extra is
installed (faster on large files) and otherwise falls back to a pure-stdlib
implementation that produces identical output.

Configuration lives in ``[plugin:csv_build]``: ``delimiter`` (blank =
auto-detect), ``encoding``, ``has_header`` (yes/no), ``max_bytes``.
"""

from __future__ import annotations

from ..config import plugin_settings
from ..plugin_api import InputOutputPlugin
from . import _tabular as tabular

DEFAULTS = {
    "delimiter": "",
    "encoding": "utf-8",
    "has_header": "yes",
    "max_bytes": tabular.MAX_BYTES,
}

_AGGS = ("count", "sum", "mean", "min", "max")


def has_pandas() -> bool:
    try:
        import pandas  # noqa: F401
        return True
    except Exception:
        return False


class CsvBuild(InputOutputPlugin):
    name = "Build dataset"
    description = "Concat/join/sample/split/convert files from the other pane"
    prompt = "build> "
    config_section = "csv_build"

    def on_start(self) -> None:
        self.config = plugin_settings(self.config_section, DEFAULTS)
        super().on_start()

    @property
    def greeting(self) -> str:
        files = tabular.selected_files(self.ctx)
        names = ", ".join(f.name for f in files) or "<nothing selected>"
        return (f"Tagged (other pane): {names}\n"
                "Verbs: concat, join <key>, sample <n|n%>, split <col>,\n"
                "       to-jsonl, from-jsonl, groupby <col> <agg>[:col].")

    # -- helpers -----------------------------------------------------------
    def _fs(self):
        return self.ctx.other_fs

    def _has_header(self) -> bool:
        return str(self.config["has_header"]).strip().lower() not in ("no", "false", "0")

    def _read(self, entry):
        fs = self._fs()
        path = fs.join(self.ctx.other_path, entry.name)
        return tabular.read_table(
            fs, path,
            delimiter=tabular.resolve_delimiter(self.config["delimiter"]),
            encoding=self.config["encoding"] or "utf-8",
            has_header=self._has_header(),
            max_bytes=int(self.config["max_bytes"]),
        )

    def _write(self, name: str, header, rows) -> str:
        fs = self._fs()
        path = fs.join(self.ctx.other_path, name)
        if fs.exists(path):
            path = tabular.derive_output_path(fs, path, "new")
        tabular.write_table(fs, path, header, rows,
                            encoding=self.config["encoding"] or "utf-8")
        try:
            self.ctx.refresh_other()
        except Exception:
            pass
        return fs.basename(path)

    def _tagged(self, minimum: int = 1):
        files = tabular.selected_files(self.ctx)
        if len(files) < minimum:
            raise RuntimeError(
                f"Tag at least {minimum} file(s) in the other pane "
                "(space to tag) first.")
        return files

    # -- dispatch ----------------------------------------------------------
    def process(self, line: str):
        cmd = line.strip()
        if not cmd:
            return None
        verb, _, rest = cmd.partition(" ")
        verb = verb.lower()
        rest = rest.strip()
        handler = {
            "concat": self._concat,
            "join": self._join,
            "sample": self._sample,
            "split": self._split,
            "to-jsonl": self._to_jsonl,
            "from-jsonl": self._from_jsonl,
            "groupby": self._groupby,
        }.get(verb)
        if handler is None:
            return (f"unknown verb: {verb!r}\n"
                    "try: concat, join, sample, split, to-jsonl, from-jsonl, groupby")
        return handler(rest)

    # -- verbs -------------------------------------------------------------
    def _concat(self, rest: str):
        files = self._tagged(1)
        header: list[str] = []
        seen: set[str] = set()
        tables = []
        for entry in files:
            table = self._read(entry)
            tables.append(table)
            for col in table.header:
                if col not in seen:
                    seen.add(col)
                    header.append(col)
        rows = []
        for table in tables:
            pos = {col: i for i, col in enumerate(table.header)}
            for row in table.rows:
                rows.append([row[pos[col]] if col in pos and pos[col] < len(row)
                             else "" for col in header])
        out = self._write("dataset.csv", header, rows)
        return f"concat {len(files)} file(s) -> {out}  ({len(rows)} rows)"

    def _join(self, rest: str):
        key = rest.strip()
        if not key:
            return "usage: join <key-column>"
        files = self._tagged(2)
        left, right = self._read(files[0]), self._read(files[1])
        li, ri = left.index(key), right.index(key)
        # Index the right table by key (last row wins on duplicate keys).
        right_cols = [c for i, c in enumerate(right.header) if i != ri]
        rindex = {}
        for row in right.rows:
            k = row[ri] if ri < len(row) else ""
            rindex[k] = [row[i] if i < len(row) else "" for i in range(len(right.header))
                         if i != ri]
        header = list(left.header) + right_cols
        blanks = [""] * len(right_cols)
        rows = []
        matched = 0
        for row in left.rows:
            k = row[li] if li < len(row) else ""
            extra = rindex.get(k)
            if extra is not None:
                matched += 1
            rows.append(list(row) + (extra if extra is not None else blanks))
        out = self._write("joined.csv", header, rows)
        return (f"left join on {key!r}: {files[0].name} + {files[1].name} -> {out}  "
                f"({matched}/{len(rows)} rows matched)")

    def _sample(self, rest: str):
        entry = self._tagged(1)[0]
        table = self._read(entry)
        spec = rest.strip()
        if spec.endswith("%"):
            try:
                pct = float(spec[:-1])
            except ValueError:
                return "usage: sample <n|n%>"
            count = int(len(table.rows) * pct / 100)
        else:
            try:
                count = int(spec)
            except ValueError:
                return "usage: sample <n|n%>"
        rows = table.rows[:max(0, count)]
        out = self._write(f"{_stem(entry.name)}.sample.csv", table.header, rows)
        return f"sample {len(rows)} row(s) -> {out}"

    def _split(self, rest: str):
        col = rest.strip()
        if not col:
            return "usage: split <column>"
        entry = self._tagged(1)[0]
        table = self._read(entry)
        idx = table.index(col)
        groups: dict[str, list] = {}
        order: list[str] = []
        for row in table.rows:
            val = row[idx] if idx < len(row) else ""
            if val not in groups:
                groups[val] = []
                order.append(val)
            groups[val].append(row)
        written = []
        for val in order:
            safe = _safe_token(val) or "blank"
            name = f"{_stem(entry.name)}.{col}-{safe}.csv"
            written.append(self._write(name, table.header, groups[val]))
        return [f"split by {col!r} into {len(written)} file(s):"] + \
               [f"  {n}" for n in written]

    def _to_jsonl(self, rest: str):
        entry = self._tagged(1)[0]
        table = self._read(entry)
        text = tabular.to_jsonl(table.header, table.rows)
        fs = self._fs()
        path = fs.join(self.ctx.other_path, f"{_stem(entry.name)}.jsonl")
        if fs.exists(path):
            path = tabular.derive_output_path(fs, path, "new")
        tabular.write_bytes(fs, path, text.encode(self.config["encoding"] or "utf-8"))
        try:
            self.ctx.refresh_other()
        except Exception:
            pass
        return f"to-jsonl {len(table.rows)} row(s) -> {fs.basename(path)}"

    def _from_jsonl(self, rest: str):
        entry = self._tagged(1)[0]
        fs = self._fs()
        path = fs.join(self.ctx.other_path, entry.name)
        text, truncated = tabular.read_text(
            fs, path, encoding=self.config["encoding"] or "utf-8",
            max_bytes=int(self.config["max_bytes"]))
        header, rows = tabular.from_jsonl(text)
        out = self._write(f"{_stem(entry.name)}.csv", header, rows)
        msg = f"from-jsonl {len(rows)} row(s) -> {out}"
        return msg + ("  (source truncated)" if truncated else "")

    def _groupby(self, rest: str):
        parts = rest.split()
        if len(parts) < 2:
            return "usage: groupby <col> <agg>[:valuecol]   agg: " + "/".join(_AGGS)
        key_col, agg_spec = parts[0], parts[1]
        agg, _, val_col = agg_spec.partition(":")
        agg = agg.lower()
        if agg not in _AGGS:
            return f"unknown agg {agg!r}; use one of {', '.join(_AGGS)}"
        if agg != "count" and not val_col:
            return f"{agg} needs a value column: groupby <col> {agg}:<valuecol>"

        entry = self._tagged(1)[0]
        table = self._read(entry)
        engine = "pandas" if has_pandas() else "stdlib"
        header, rows = _aggregate(table, key_col, agg, val_col or None,
                                  use_pandas=(engine == "pandas"))
        out = self._write(f"{_stem(entry.name)}.groupby.csv", header, rows)
        return f"groupby {key_col!r} {agg} [{engine}] -> {out}  ({len(rows)} groups)"


# -- aggregation (shared by both engines for identical output) --------------
def _aggregate(table: tabular.Table, key_col: str, agg: str, val_col,
               *, use_pandas: bool):
    ki = table.index(key_col)
    vi = table.index(val_col) if val_col else None
    out_col = f"{agg}_{val_col}" if val_col else "count"
    header = [key_col, out_col]

    if use_pandas:
        rows = _aggregate_pandas(table, ki, vi, agg)
    else:
        rows = _aggregate_stdlib(table, ki, vi, agg)
    return header, rows


def _aggregate_stdlib(table: tabular.Table, ki: int, vi, agg: str):
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for row in table.rows:
        k = row[ki] if ki < len(row) else ""
        if k not in groups:
            groups[k] = []
            order.append(k)
        if vi is not None:
            groups[k].append(row[vi] if vi < len(row) else "")
        else:
            groups[k].append("")
    rows = []
    for k in order:
        rows.append([k, _apply_agg(agg, groups[k])])
    return rows


def _aggregate_pandas(table: tabular.Table, ki: int, vi, agg: str):
    import pandas as pd

    key = [row[ki] if ki < len(row) else "" for row in table.rows]
    if vi is None:
        df = pd.DataFrame({"k": key})
        grouped = df.groupby("k", sort=False).size()
        return [[k, str(int(v))] for k, v in grouped.items()]
    raw = [row[vi] if vi < len(row) else "" for row in table.rows]
    nums = pd.to_numeric(pd.Series(raw), errors="coerce")
    df = pd.DataFrame({"k": key, "v": nums})
    grouped = getattr(df.groupby("k", sort=False)["v"], agg)()
    rows = []
    for k, v in grouped.items():
        rows.append([k, "" if pd.isna(v) else tabular.format_number(float(v))])
    return rows


def _apply_agg(agg: str, values) -> str:
    if agg == "count":
        return str(len(values))
    nums = tabular.numeric_values(values)
    if not nums:
        return ""
    if agg == "sum":
        return tabular.format_number(sum(nums))
    if agg == "min":
        return tabular.format_number(min(nums))
    if agg == "max":
        return tabular.format_number(max(nums))
    if agg == "mean":
        return tabular.format_number(sum(nums) / len(nums))
    return ""


def _stem(name: str) -> str:
    stem, dot, _ = name.rpartition(".")
    return stem if dot else name


def _safe_token(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value.strip())[:40]
