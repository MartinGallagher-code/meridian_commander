"""Shared helpers for the CSV/TSV/JSONL data plugins.

Pure standard library (``csv``, ``json``, ``statistics``).  Everything here goes
through the :class:`~meridian_commander.filesystems.FileSystem` abstraction, so
the data plugins read and write local *and* remote (SFTP/SSH/FTP) files with the
same code.  Reads are **bounded**: :func:`read_text` never pulls in more than
``max_bytes``, and the plugins report when a file was truncated at that limit, so
a multi-gigabyte table can never lock up the interface.

The module name starts with an underscore so plugin discovery ignores it -- it
holds no plugin classes, only helpers the three ``csv_*`` plugins share.
"""

from __future__ import annotations

import csv
import io
import json
import re
import statistics
from dataclasses import dataclass, field

# Streaming reads happen in 64 KiB chunks; a single logical read is capped so a
# huge file cannot exhaust memory or hang the TUI.
CHUNK = 64 * 1024
MAX_BYTES = 64 * 1024 * 1024

# Values treated as "missing" when profiling, cleaning and aggregating.
NULL_TOKENS = {"", "na", "n/a", "null", "none", "nan"}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_BOOL_TOKENS = {"true", "false", "yes", "no", "t", "f", "y", "n"}
_DELIM_NAMES = {
    "tab": "\t", "\\t": "\t", "comma": ",", ",": ",",
    "semicolon": ";", ";": ";", "pipe": "|", "|": "|", "space": " ",
}


# -- low-level I/O ----------------------------------------------------------
def _close(stream) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def read_text(fs, path, *, encoding: str = "utf-8", max_bytes: int = MAX_BYTES):
    """Read ``path`` as text, capped at ``max_bytes``.

    Returns ``(text, truncated)``.  Relies only on the ``read(n)`` primitive of
    :meth:`FileSystem.open_read`, so it works for every backend.
    """
    stream = fs.open_read(path)
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        while total < max_bytes:
            chunk = stream.read(min(CHUNK, max_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        else:
            # Stopped at the cap: is there more we are dropping?
            if stream.read(1):
                truncated = True
    finally:
        _close(stream)
    return b"".join(chunks).decode(encoding, errors="replace"), truncated


def write_bytes(fs, path, data: bytes) -> None:
    stream = fs.open_write(path)
    try:
        stream.write(data)
    finally:
        _close(stream)


# -- delimiter / table model ------------------------------------------------
def resolve_delimiter(value):
    """Turn a configured delimiter into a single character, or ``None`` (auto).

    Accepts friendly names (``tab``, ``comma``, ``semicolon``, ``pipe``) as well
    as the literal character.
    """
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    return _DELIM_NAMES.get(v.lower(), v[:1])


def sniff_delimiter(text: str, default: str = ",") -> str:
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        # Sniffer gives up on ragged samples; fall back to the most frequent
        # candidate on the first non-empty line.
        first = next((ln for ln in sample.splitlines() if ln), "")
        counts = {d: first.count(d) for d in (",", "\t", ";", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] else default


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    delimiter: str = ","
    truncated: bool = False

    @property
    def ncols(self) -> int:
        return len(self.header)

    def index(self, name: str) -> int:
        """Column index for ``name`` (case-insensitive), else ``ValueError``."""
        for i, col in enumerate(self.header):
            if col == name:
                return i
        low = name.lower()
        for i, col in enumerate(self.header):
            if col.lower() == low:
                return i
        raise ValueError(f"no such column: {name!r}")

    def indices(self, spec: str) -> list[int]:
        """Resolve a comma-separated list of column names to indices."""
        return [self.index(part.strip()) for part in spec.split(",") if part.strip()]

    def column(self, i: int) -> list[str]:
        return [row[i] if i < len(row) else "" for row in self.rows]


def read_table(fs, path, *, delimiter=None, encoding: str = "utf-8",
               has_header: bool = True, max_bytes: int = MAX_BYTES) -> Table:
    text, truncated = read_text(fs, path, encoding=encoding, max_bytes=max_bytes)
    delim = delimiter or sniff_delimiter(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    # A trailing newline yields a final empty row; drop it.
    if rows and rows[-1] == []:
        rows.pop()
    if has_header and rows:
        header = rows.pop(0)
    elif rows:
        header = [f"col{i + 1}" for i in range(len(rows[0]))]
    else:
        header = []
    return Table(header=header, rows=rows, delimiter=delim, truncated=truncated)


def write_table(fs, path, header, rows, *, delimiter: str = ",",
                encoding: str = "utf-8") -> int:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    if header:
        writer.writerow(header)
    writer.writerows(rows)
    write_bytes(fs, path, buf.getvalue().encode(encoding))
    return len(rows)


def derive_output_path(fs, path: str, suffix: str) -> str:
    """A sibling path like ``sales.cleaned.csv``, numbered if it already exists."""
    directory = fs.dirname(path)
    base = fs.basename(path)
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""

    def build(tag: str) -> str:
        name = f"{stem}.{tag}.{ext}" if ext else f"{stem}.{tag}"
        return fs.join(directory, name)

    candidate = build(suffix)
    n = 2
    while fs.exists(candidate):
        candidate = build(f"{suffix}{n}")
        n += 1
    return candidate


# -- value helpers ----------------------------------------------------------
def is_null(value: str) -> bool:
    return value.strip().lower() in NULL_TOKENS


def numeric_values(values) -> list[float]:
    out = []
    for v in values:
        if is_null(v):
            continue
        try:
            out.append(float(v))
        except ValueError:
            pass
    return out


def format_number(x) -> str:
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, int):
        return str(x)
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.6f}".rstrip("0").rstrip(".")


def infer_type(values) -> str:
    """Infer a column's type: int, float, bool, date, str (or empty)."""
    seen = False
    is_int = is_float = is_bool = is_date = True
    for v in values:
        if is_null(v):
            continue
        seen = True
        s = v.strip()
        if is_int:
            try:
                int(s)
            except ValueError:
                is_int = False
        if is_float:
            try:
                float(s)
            except ValueError:
                is_float = False
        if is_bool and s.lower() not in _BOOL_TOKENS:
            is_bool = False
        if is_date and not _DATE_RE.match(s):
            is_date = False
        if not (is_int or is_float or is_bool or is_date):
            break
    if not seen:
        return "empty"
    if is_int:
        return "int"
    if is_bool:
        return "bool"
    if is_float:
        return "float"
    if is_date:
        return "date"
    return "str"


# -- JSON lines -------------------------------------------------------------
def to_jsonl(header, rows) -> str:
    out = []
    for row in rows:
        obj = {header[i]: (row[i] if i < len(row) else "")
               for i in range(len(header))}
        out.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(out) + ("\n" if out else "")


def from_jsonl(text: str):
    """Parse JSON-lines into ``(header, rows)`` with a union-of-keys header."""
    header: list[str] = []
    seen: set[str] = set()
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            obj = {"value": obj}
        for key in obj:
            if key not in seen:
                seen.add(key)
                header.append(key)
        records.append(obj)
    rows = []
    for obj in records:
        rows.append([_scalar(obj.get(col, "")) for col in header])
    return header, rows


def _scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# -- selection / presentation ----------------------------------------------
def selected_file(ctx):
    """The first real file tagged (or hovered) in the opposite pane, else None."""
    try:
        entries = ctx.other_selected()
    except Exception:
        entries = []
    for e in entries:
        if not getattr(e, "is_dir", False):
            return e
    return None


def selected_files(ctx):
    """All real files tagged (or hovered) in the opposite pane."""
    try:
        entries = ctx.other_selected()
    except Exception:
        entries = []
    return [e for e in entries if not getattr(e, "is_dir", False)]


def format_rows(header, rows, *, max_col: int = 24):
    """Render ``header``/``rows`` as aligned fixed-width lines for previewing."""
    ncols = len(header)
    widths = [min(len(str(c)), max_col) for c in header]
    for row in rows:
        for i in range(ncols):
            cell = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], min(len(cell), max_col))

    def fmt(cells) -> str:
        parts = []
        for i in range(ncols):
            cell = str(cells[i]) if i < len(cells) else ""
            if len(cell) > max_col:
                cell = cell[:max_col]
            parts.append(cell.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    lines = [fmt(header), "  ".join("-" * w for w in widths)]
    lines.extend(fmt(row) for row in rows)
    return lines


def histogram(numbers, *, buckets: int = 10, width: int = 40):
    """ASCII histogram lines for a list of numbers (empty if none)."""
    nums = [float(n) for n in numbers]
    if not nums:
        return []
    lo, hi = min(nums), max(nums)
    if lo == hi:
        return [f"  {format_number(lo)} | {'#' * min(width, len(nums))} {len(nums)}"]
    counts = [0] * buckets
    span = hi - lo
    for n in nums:
        idx = int((n - lo) / span * buckets)
        if idx == buckets:
            idx -= 1
        counts[idx] += 1
    peak = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        edge = lo + span * i / buckets
        bar = "#" * int(c / peak * width)
        lines.append(f"  {format_number(edge):>12} | {bar} {c}")
    return lines
