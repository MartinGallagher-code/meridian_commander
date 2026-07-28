"""Read ``.xlsx`` / ``.xlsm`` workbooks using the standard library alone.

An OOXML spreadsheet is a zip archive of XML parts, so :mod:`zipfile` and
:mod:`xml.etree.ElementTree` are the whole dependency list.  That keeps the
package's promise that local browsing needs nothing installed: a spreadsheet
opens on a bare machine exactly like a text file does.

Legacy ``.xls`` is deliberately *not* handled.  Despite the name it is an
unrelated format -- a compound-document binary (BIFF), not zipped XML -- with
no standard-library path, so supporting it would mean a third-party reader.

What is read
------------
Values, as they would be displayed: shared and inline strings, numbers,
booleans, formula results (the cached value, not the formula), and serial
dates rendered through the workbook's number formats.  Styling, charts,
images and merged-cell geometry are ignored -- this is a reader for looking
at data, not a renderer.

Unlike a CSV, a spreadsheet cannot be read in part: the zip central directory
lives at the *end* of the archive, so a truncated file does not parse at all.
The size cap is therefore a refusal rather than a silent truncation, and the
per-sheet row and column caps apply to parsing, not to the download.
"""

from __future__ import annotations

import datetime
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm")

# The archive is read whole (see the module docstring), so this cap bounds the
# memory a stray "view" on a huge workbook can cost.
MAX_BYTES = 64 * 1024 * 1024
# A part that inflates beyond this is refused unread: the compressed size in
# the central directory says nothing about what a decompressor will produce.
MAX_PART_BYTES = 256 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLS = 1024
# One cell can legally hold 32k characters; nothing that long is readable in a
# grid, and the browser shows the value again in its footer.
MAX_CELL_CHARS = 1024

_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class XlsxError(Exception):
    """A workbook could not be read."""


@dataclass
class Sheet:
    name: str
    rows: list[list[str]] = field(default_factory=list)
    row_limit: bool = False   # stopped at MAX_ROWS
    col_limit: bool = False   # a row reached MAX_COLS

    @property
    def truncated(self) -> bool:
        return self.row_limit or self.col_limit

    @property
    def ncols(self) -> int:
        return max((len(r) for r in self.rows), default=0)


@dataclass
class Workbook:
    name: str
    sheets: list[Sheet] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return any(s.truncated for s in self.sheets)


def is_spreadsheet(name: str) -> bool:
    """True if ``name`` looks like a workbook this module can read."""
    return name.lower().endswith(SPREADSHEET_SUFFIXES)


# -- helpers ----------------------------------------------------------------
def _local(tag: str) -> str:
    """Local name of an element tag, discarding the namespace.

    Matching on the local name means a producer that declares the schema under
    a different (or no) namespace still parses, which costs nothing here: no
    two parts of the format reuse a name with a different meaning.
    """
    return tag.rpartition("}")[2]


def column_index(ref: str) -> int | None:
    """Column number of a cell reference: ``A1`` -> 0, ``AA7`` -> 26.

    Returns ``None`` when the reference has no letters to read.
    """
    value = 0
    seen = False
    for ch in ref.upper():
        if not ("A" <= ch <= "Z"):
            break
        value = value * 26 + (ord(ch) - 64)
        seen = True
    return value - 1 if seen else None


def column_name(index: int) -> str:
    """Spreadsheet column label for a zero-based index: 0 -> ``A``."""
    name = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _read_part(zf: zipfile.ZipFile, name: str) -> bytes | None:
    """Read one archive member, or ``None`` if absent or implausibly large."""
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise XlsxError(f"{name} is too large to read "
                        f"({info.file_size} bytes)")
    try:
        return zf.read(name)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise XlsxError(f"cannot read {name}: {exc}") from exc


def _parse_xml(data: bytes, name: str):
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise XlsxError(f"{name} is not valid XML: {exc}") from exc


def _resolve(base_part: str, target: str) -> str:
    """Turn a relationship target into an archive member name."""
    if target.startswith("/"):
        return target[1:]
    base_dir = posixpath.dirname(base_part)
    if not base_dir:
        return target
    return posixpath.normpath(posixpath.join(base_dir, target))


def _relationships(zf: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """``{rId: (type, member)}`` for the part's ``.rels`` companion."""
    base_dir = posixpath.dirname(part)
    rels_name = posixpath.join(base_dir, "_rels",
                               posixpath.basename(part) + ".rels")
    data = _read_part(zf, rels_name)
    if data is None:
        return {}
    root = _parse_xml(data, rels_name)
    out = {}
    for node in root:
        if _local(node.tag) != "Relationship":
            continue
        rid = node.get("Id")
        target = node.get("Target")
        if not rid or not target:
            continue
        # An external relationship (a link to another workbook) has no member
        # in this archive, so there is nothing here to resolve it against.
        if node.get("TargetMode") == "External":
            continue
        out[rid] = (node.get("Type", ""), _resolve(part, target))
    return out


def _by_type(rels: dict, suffix: str) -> str | None:
    for _rid, (rtype, member) in rels.items():
        if rtype.rsplit("/", 1)[-1] == suffix:
            return member
    return None


# -- number formats ---------------------------------------------------------
# The built-in formats that mean a date, a time, or both.  Everything else in
# the built-in range is a number, and custom ids (164 and up) carry their own
# format code, which _format_kind() reads.
_BUILTIN_KINDS = {
    14: "date", 15: "date", 16: "date", 17: "date",
    18: "time", 19: "time", 20: "time", 21: "time",
    22: "datetime",
    45: "time", 46: "time", 47: "time",
}

_QUOTED = re.compile(r'"[^"]*"')
_BRACKETED = re.compile(r"\[[^\]]*\]")
_ESCAPED = re.compile(r"\\.")


def _format_kind(code: str) -> str | None:
    """Classify a number format code as date, time, datetime -- or a number.

    Literal text, escapes and bracketed sections (locale ids, colours, the
    ``[h]`` elapsed-hours marker) are removed first so their letters cannot be
    mistaken for date codes.
    """
    code = _ESCAPED.sub("", _BRACKETED.sub("", _QUOTED.sub("", code)))
    # Only the first section applies to positive numbers, which is the one
    # that decides what a value looks like.
    code = code.split(";")[0].lower()
    has_date = "y" in code or "d" in code
    has_time = "h" in code or "s" in code
    if not has_date and not has_time and "m" in code:
        # "m" is month or minute depending on company; with no hours or
        # seconds beside it there is nothing for a minute to belong to.
        has_date = True
    if has_date and has_time:
        return "datetime"
    if has_date:
        return "date"
    if has_time:
        return "time"
    return None


def _style_kinds(zf: zipfile.ZipFile, member: str) -> list[str | None]:
    """Date/time kind per cell-format index, as cells reference it with ``s``."""
    data = _read_part(zf, member)
    if data is None:
        return []
    root = _parse_xml(data, member)
    custom: dict[int, str] = {}
    kinds: list[str | None] = []
    for node in root:
        tag = _local(node.tag)
        if tag == "numFmts":
            for fmt in node:
                if _local(fmt.tag) != "numFmt":
                    continue
                try:
                    fid = int(fmt.get("numFmtId", ""))
                except ValueError:
                    continue
                custom[fid] = fmt.get("formatCode", "")
        elif tag == "cellXfs":
            # cellStyleXfs holds <xf> too, and cells never point at it, so the
            # list has to come from this element rather than a tree-wide search.
            for xf in node:
                if _local(xf.tag) != "xf":
                    continue
                try:
                    fid = int(xf.get("numFmtId", "0"))
                except ValueError:
                    fid = 0
                if fid in custom:
                    kinds.append(_format_kind(custom[fid]))
                else:
                    kinds.append(_BUILTIN_KINDS.get(fid))
    return kinds


# -- value rendering --------------------------------------------------------
_EPOCH_1900 = datetime.datetime(1899, 12, 30)
_EPOCH_1904 = datetime.datetime(1904, 1, 1)


def format_number(value: str) -> str:
    """Render a stored number the way a spreadsheet would show it."""
    try:
        x = float(value)
    except ValueError:
        return value
    if x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    # Excel writes the full binary expansion (0.1 arrives as
    # 0.10000000000000001); ten significant digits is past any precision a
    # reader can use and puts the value back the way it was typed.
    return f"{x:.10g}"


def serial_to_text(serial: float, kind: str, date1904: bool = False) -> str | None:
    """Render an Excel date serial, or ``None`` if it is not a date at all."""
    if serial < 0:
        return None
    # Round to the second before splitting, so a value a hair under midnight
    # cannot produce an impossible 24:00:00.
    days, seconds = divmod(int(round(serial * 86400)), 86400)
    if date1904:
        base = _EPOCH_1904
    elif days > 60:
        base = _EPOCH_1900
    elif days == 60:
        # Excel inherited a non-existent 29 February 1900 from Lotus 1-2-3.
        # There is no real date to convert this to, so it is spelled out the
        # way the spreadsheet itself displays it.
        return _join("1900-02-29", seconds, kind)
    else:
        # Below the phantom day the serials are off by one against the real
        # calendar, so they count from a day later.
        base = datetime.datetime(1899, 12, 31)
    try:
        when = base + datetime.timedelta(days=days)
    except (OverflowError, ValueError):
        return None
    return _join(when.strftime("%Y-%m-%d"), seconds, kind)


def _join(date_text: str, seconds: int, kind: str) -> str:
    time_text = "%02d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60,
                                    seconds % 60)
    if kind == "time":
        return time_text
    if kind == "date":
        return date_text
    return f"{date_text} {time_text}"


# -- parts ------------------------------------------------------------------
def _shared_strings(zf: zipfile.ZipFile, member: str) -> list[str]:
    data = _read_part(zf, member)
    if data is None:
        return []
    root = _parse_xml(data, member)
    out = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        parts = []
        for node in si:
            tag = _local(node.tag)
            if tag == "t":
                parts.append(node.text or "")
            elif tag == "r":
                # A rich-text run: the styling is dropped, the text kept.
                for sub in node:
                    if _local(sub.tag) == "t":
                        parts.append(sub.text or "")
            # rPh (a phonetic guide) also holds <t>, but it is an annotation
            # rather than the cell's text, so it stays out.
        out.append("".join(parts))
    return out


def _cell_text(cell, shared: list[str], kinds: list[str | None],
               date1904: bool) -> str:
    ctype = cell.get("t") or "n"
    if ctype == "inlineStr":
        return "".join(node.text or ""
                       for node in cell.iter() if _local(node.tag) == "t")
    raw = None
    for child in cell:
        if _local(child.tag) == "v":
            raw = child.text
            break
    if raw is None:
        return ""
    if ctype == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if ctype == "b":
        return "FALSE" if raw.strip() in ("0", "") else "TRUE"
    if ctype in ("str", "e", "d"):
        # str: a formula's cached string result.  e: an error such as #N/A.
        # d: an ISO 8601 date, already in the form we would render.
        return raw
    kind = None
    try:
        style = int(cell.get("s", "0"))
    except ValueError:
        style = 0
    if 0 <= style < len(kinds):
        kind = kinds[style]
    if kind:
        try:
            text = serial_to_text(float(raw), kind, date1904)
        except ValueError:
            text = None
        if text is not None:
            return text
    return format_number(raw)


def _read_sheet(zf: zipfile.ZipFile, member: str, name: str,
                shared: list[str], kinds: list[str | None],
                date1904: bool) -> Sheet:
    sheet = Sheet(name)
    try:
        info = zf.getinfo(member)
    except KeyError:
        return sheet
    if info.file_size > MAX_PART_BYTES:
        raise XlsxError(f"sheet '{name}' is too large to read "
                        f"({info.file_size} bytes)")
    expected = 0
    with zf.open(member) as stream:
        # iterparse rather than a whole-tree parse: a sheet is by far the
        # biggest part, and every row can be dropped as soon as it is read.
        try:
            events = ET.iterparse(stream, events=("end",))
            for _event, elem in events:
                if _local(elem.tag) != "row":
                    continue
                index = _row_index(elem, expected)
                if index >= MAX_ROWS:
                    sheet.row_limit = True
                    break
                # Rows Excel never wrote because they are empty still occupy
                # their place on screen, so the gaps are filled in.
                while len(sheet.rows) < index:
                    sheet.rows.append([])
                sheet.rows.append(_read_row(elem, sheet, shared, kinds,
                                            date1904))
                expected = index + 1
                elem.clear()
        except ET.ParseError as exc:
            raise XlsxError(f"sheet '{name}' is not valid XML: {exc}") from exc
        except (zipfile.BadZipFile, OSError) as exc:
            # The part is decompressed as it is parsed, so damage shows up
            # here rather than when the archive was opened.
            raise XlsxError(f"cannot read sheet '{name}': {exc}") from exc
    return sheet


def _row_index(elem, expected: int) -> int:
    """Zero-based row number, from the ``r`` attribute or the running count."""
    raw = elem.get("r")
    if raw:
        try:
            return max(0, int(raw) - 1)
        except ValueError:
            pass
    return expected


def _read_row(elem, sheet: Sheet, shared: list[str],
              kinds: list[str | None], date1904: bool) -> list[str]:
    cells: list[str] = []
    for cell in elem:
        if _local(cell.tag) != "c":
            continue
        index = column_index(cell.get("r", ""))
        if index is None:
            index = len(cells)
        if index >= MAX_COLS:
            sheet.col_limit = True
            break
        while len(cells) < index:
            cells.append("")
        text = _cell_text(cell, shared, kinds, date1904)
        if len(text) > MAX_CELL_CHARS:
            text = text[:MAX_CELL_CHARS]
        cells.append(text)
    return cells


# -- entry points -----------------------------------------------------------
def parse_workbook(data: bytes, name: str = "workbook") -> Workbook:
    """Parse workbook bytes.  Raises :class:`XlsxError` on anything unreadable."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise XlsxError(f"not a readable .xlsx archive: {exc}") from exc
    with zf:
        # The package's own relationships live in "_rels/.rels", which is what
        # the naming rule produces for the empty (root) part name.
        root_rels = _relationships(zf, "")
        part = _by_type(root_rels, "officeDocument") or "xl/workbook.xml"
        book_data = _read_part(zf, part)
        if book_data is None:
            raise XlsxError("no workbook part in the archive")
        book = _parse_xml(book_data, part)
        rels = _relationships(zf, part)

        date1904 = False
        entries: list[tuple[str, str | None]] = []
        for node in book:
            tag = _local(node.tag)
            if tag == "workbookPr":
                date1904 = (node.get("date1904") or "").lower() in ("1", "true")
            elif tag == "sheets":
                for item in node:
                    if _local(item.tag) != "sheet":
                        continue
                    rid = item.get(f"{{{_REL_NS}}}id") or item.get("id")
                    member = rels.get(rid, ("", None))[1] if rid else None
                    entries.append((item.get("name", "Sheet"), member))

        shared_member = _by_type(rels, "sharedStrings")
        shared = _shared_strings(zf, shared_member) if shared_member else []
        styles_member = _by_type(rels, "styles")
        kinds = _style_kinds(zf, styles_member) if styles_member else []

        workbook = Workbook(name)
        for sheet_name, member in entries:
            if member is None:
                # A sheet whose relationship is missing or external has no
                # data here; it stays in the list so the sheet numbering the
                # user sees still matches the file.
                workbook.sheets.append(Sheet(sheet_name))
                continue
            workbook.sheets.append(
                _read_sheet(zf, member, sheet_name, shared, kinds, date1904))
        if not workbook.sheets:
            raise XlsxError("the workbook declares no sheets")
        return workbook


def load_bytes(fs, path: str) -> bytes:
    """Read a whole workbook through any backend, refusing oversized files."""
    stream = fs.open_read(path)
    try:
        data = stream.read(MAX_BYTES + 1)
    finally:
        stream.close()
    if len(data) > MAX_BYTES:
        raise XlsxError(
            f"file is larger than {MAX_BYTES // (1024 * 1024)} MiB; a "
            "spreadsheet cannot be read in part")
    return data


def read_workbook(fs, path: str) -> Workbook:
    """Load and parse the workbook at ``path`` on filesystem ``fs``."""
    return parse_workbook(load_bytes(fs, path), fs.basename(path))
