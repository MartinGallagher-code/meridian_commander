"""Built-in plugin: browse a provost store -- datasets, captures and sources.

`provost <https://pypi.org/project/provost/>`_ turns command output into data:
each capture lands as a tidy TSV dataset in a git-backed store, with the exact
original input kept alongside.  This plugin is a read-only browser for such a
store, found from the **other pane's** location:

* **Datasets** (the opening view) -- every dataset with its row count; Enter
  opens one as an aligned, scrollable table.  The ``unsorted`` triage dataset
  (low-confidence / unmatched captures) is base64-decoded for display, exactly
  as ``provost triage`` decodes it.
* **Captures log** (``l``) -- the capture history, newest first: when it ran,
  where the input came from, which strategy matched and what dataset it fed.
  Enter shows one capture in full -- its metadata, context and captured output.
* **Row provenance** -- on any dataset row, ``s`` (or Enter) follows the row's
  ``#N`` index through ``provenance.tsv`` back to the capture that produced it
  and shows the original input, like ``provost source N``.

The store is found the way provost itself finds it: an explicit path first
(``store =`` under ``[plugin:provost]`` in ``config.ini``, or the local
``$PROVOST_WORKSPACE``), then the other pane's directory itself, then a
``.provost`` instance in that directory or any parent (a *controlled*
directory), and finally the global store (``$PROVOST_HOME`` / ``~/.provost``).
Everything is read through the pane's own filesystem backend, so a store on an
SFTP/SSH pane browses exactly like a local one -- no provost installation is
needed on either side.  Reads are bounded, so a huge dataset cannot hang the
interface.
"""

from __future__ import annotations

import base64
import binascii
import curses
import os
import time

from .. import theme
from ..config import plugin_settings
from ..plugin_api import PanePlugin
from ..util import human_size
from ._tabular import read_text

DEFAULTS = {
    "store": "",               # explicit store path; wins over discovery
}

#: Read caps: a dataset table, and a single capture's stored output.
MAX_DATASET_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024

#: A column wider than this is clipped so one long field cannot push the
#: rest of the table off the screen (Left/Right still pan to see everything).
MAX_COL_WIDTH = 48

#: Files that mark a directory as a provost store.  ``.provostrc`` is written
#: when provost initialises any workspace; ``CONTROLLED`` marks a per-directory
#: instance.  A bare datasets+captures pair is accepted as a fallback (e.g. a
#: store copied without its dotfiles).
_STORE_MARKERS = (".provostrc", "CONTROLLED")


# -- store discovery -----------------------------------------------------------
def is_store(fs, path: str) -> bool:
    """Whether ``path`` looks like a provost store directory."""
    try:
        if not fs.is_dir(path):
            return False
        for marker in _STORE_MARKERS:
            if fs.exists(fs.join(path, marker)):
                return True
        return (fs.is_dir(fs.join(path, "datasets"))
                and fs.is_dir(fs.join(path, "captures")))
    except Exception:
        return False


def find_store(fs, path: str, environ, configured: str = ""):
    """Resolve the store to browse, mirroring provost's own resolution order.

    Returns ``(store_path, how)`` on success, or ``(None, tried)`` where
    ``tried`` lists the places that were checked, for the error message.
    """
    tried: list[str] = []

    if configured:
        if is_store(fs, configured):
            return configured, "configured"
        tried.append(configured)

    local = getattr(fs, "scheme", "") == "local"
    if local:
        override = environ.get("PROVOST_WORKSPACE", "")
        if override:
            if is_store(fs, override):
                return override, "PROVOST_WORKSPACE"
            tried.append(override)

    if is_store(fs, path):
        return path, "this directory"

    # A controlled directory holds its instance at <dir>/.provost; walk up.
    here = fs.normpath(path)
    while True:
        candidate = fs.join(here, ".provost")
        if is_store(fs, candidate):
            return candidate, f"controlled: {here}"
        parent = fs.parent(here)
        if parent == here:
            break
        here = parent
    tried.append(fs.join(path, ".provost") + " (and parents)")

    home = environ.get("PROVOST_HOME", "") if local else ""
    if not home:
        try:
            home = fs.join(fs.home(), ".provost")
        except Exception:
            home = ""
    if home:
        if is_store(fs, home):
            return home, "global"
        tried.append(home)

    return None, ", ".join(tried)


# -- parsing the store's files ---------------------------------------------------
def parse_tsv(text: str) -> list[list[str]]:
    """Split TSV text into rows of cells (no quoting -- provost writes none)."""
    return [line.split("\t") for line in text.splitlines() if line != ""]


def parse_meta(text: str) -> list[tuple[str, str]]:
    """A capture's ``meta.tsv`` as ordered ``(key, value)`` pairs."""
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        key, _, value = line.partition("\t")
        pairs.append((key, value))
    return pairs


def decode_raw(value: str) -> str:
    """Decode one base64 ``raw`` cell of the ``unsorted`` dataset for display.

    Tabs and newlines inside the decoded text are escaped so the record stays
    on one table row -- the same rendering ``provost triage`` uses.  A value
    that does not decode is shown as-is.
    """
    try:
        decoded = base64.b64decode(value, validate=True).decode(
            "utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return value
    return decoded.replace("\t", "\\t").replace("\n", "\\n")


def provenance_lookup(text: str, row_id: str):
    """Find ``row_id`` in provenance.tsv text: ``(dataset, capture)`` or None."""
    want = row_id.lstrip("#")
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) >= 3 and cells[0] == want:
            return cells[1], cells[2]
    return None


def align_rows(rows: list[list[str]]) -> list[str]:
    """Render rows as lines with columns padded to a shared width.

    The width of each column is the widest cell in it, capped at
    :data:`MAX_COL_WIDTH`; a cell over the cap is clipped with ``…``.
    """
    if not rows:
        return []
    ncols = max(len(row) for row in rows)
    widths = [0] * ncols
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = min(MAX_COL_WIDTH, max(widths[i], len(cell)))
    lines = []
    for row in rows:
        cells = []
        for i in range(ncols):
            cell = row[i] if i < len(row) else ""
            if len(cell) > MAX_COL_WIDTH:
                cell = cell[:MAX_COL_WIDTH - 1] + "…"
            cells.append(cell.ljust(widths[i]))
        lines.append("  ".join(cells).rstrip())
    return lines


def _mtime_text(mtime) -> str:
    if not mtime:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    except (OverflowError, OSError, ValueError):
        return ""


# -- the views -------------------------------------------------------------------
class _View:
    """One level of the browser: a title, lines, and a cursor over items.

    ``items`` runs parallel to ``lines``; a view whose items are ``None`` is a
    plain scrolling text (a capture's detail, a row's source) with no cursor.
    ``filter`` narrows the visible lines by case-insensitive substring.
    """

    def __init__(self, kind: str, title: str, lines: list[str],
                 items: list | None, header: str = "",
                 footer_keys: str = "", truncated: bool = False) -> None:
        self.kind = kind
        self.title = title
        self.header = header
        self.all_lines = lines
        self.all_items = items
        self.footer_keys = footer_keys
        self.truncated = truncated
        self.filter = ""
        self.cursor = 0
        self.scroll = 0
        self.hscroll = 0
        self._apply_filter()

    @property
    def selectable(self) -> bool:
        return self.all_items is not None

    def _apply_filter(self) -> None:
        if self.filter:
            needle = self.filter.lower()
            keep = [i for i, line in enumerate(self.all_lines)
                    if needle in line.lower()]
        else:
            keep = list(range(len(self.all_lines)))
        self.lines = [self.all_lines[i] for i in keep]
        self.items = [self.all_items[i] for i in keep] \
            if self.all_items is not None else None
        self.cursor = min(self.cursor, max(0, len(self.lines) - 1))

    def set_filter(self, text: str) -> None:
        self.filter = text
        self._apply_filter()

    def current_item(self):
        if self.items is not None and 0 <= self.cursor < len(self.items):
            return self.items[self.cursor]
        return None


class ProvostBrowser(PanePlugin):
    name = "Provost data"
    description = "Browse the other pane's provost store: datasets, log, sources"

    def on_start(self) -> None:
        self.cfg = plugin_settings("provost", DEFAULTS)
        self.fs = self.ctx.other_fs
        self.status = ""
        self.mode = "list"            # "list" or "filter"

        store, how = find_store(self.fs, self.ctx.other_path, os.environ,
                                str(self.cfg.get("store", "")).strip())
        if store is None:
            raise RuntimeError(
                "No provost store found (looked at: %s). Point the other pane "
                "at a store or a controlled directory, or set store= under "
                "[plugin:provost]." % how)
        self.store = store
        self.store_kind = how
        self.stack: list[_View] = [self._datasets_view()]

    # -- reading through the pane's filesystem --------------------------------
    def _read(self, path: str, max_bytes: int):
        return read_text(self.fs, path, max_bytes=max_bytes)

    def _datasets_dir(self) -> str:
        return self.fs.join(self.store, "datasets")

    def _captures_dir(self) -> str:
        return self.fs.join(self.store, "captures")

    # -- building the views ----------------------------------------------------
    def _datasets_view(self) -> _View:
        directory = self._datasets_dir()
        names: list[str] = []
        rows: list[list[str]] = []
        try:
            entries = self.fs.listdir(directory)
        except Exception:
            entries = []
        for entry in sorted(entries, key=lambda e: e.name):
            if entry.is_dir or not entry.name.endswith(".tsv"):
                continue
            name = entry.name[:-4]
            names.append(name)
            rows.append([name, human_size(entry.size or 0),
                         _mtime_text(entry.mtime)])
        aligned = align_rows([["DATASET", "SIZE", "MODIFIED"]] + rows)
        header = aligned[0]
        lines = aligned[1:]
        if not lines:
            lines = ["(no datasets yet -- pipe something into provost)"]
            names = [None]
        return _View("datasets", f"provost: {self.store}", lines, names,
                     header=header,
                     footer_keys="Enter open   l log   t triage   / filter"
                                 "   r reload   Esc close")

    def _table_view(self, name: str) -> _View:
        path = self.fs.join(self._datasets_dir(), name + ".tsv")
        text, truncated = self._read(path, MAX_DATASET_BYTES)
        rows = parse_tsv(text)
        if truncated and rows:
            rows = rows[:-1]         # drop the possibly cut-off last line
        header_cells = rows[0] if rows else []
        body = rows[1:] if rows else []
        if name == "unsorted" and len(header_cells) >= 2 \
                and header_cells[1] == "raw":
            body = [row[:1] + [decode_raw(row[1])] + row[2:] if len(row) > 1
                    else row for row in body]
        aligned = align_rows([header_cells] + body) if rows else []
        header = aligned[0] if aligned else ""
        lines = aligned[1:]
        items: list = body
        if not lines:
            lines = ["(dataset is empty)"]
            items = [None]
        title = f"dataset: {name} ({len(body)} rows)"
        return _View("table", title, lines, items, header=header,
                     footer_keys="s/Enter source of row   / filter"
                                 "   \u2190\u2192 pan   Backspace back",
                     truncated=truncated)

    def _log_view(self) -> _View:
        directory = self._captures_dir()
        try:
            entries = self.fs.listdir(directory)
        except Exception:
            entries = []
        captures: list[tuple[str, dict]] = []
        for entry in sorted(entries, key=lambda e: e.name, reverse=True):
            if not entry.is_dir:
                continue
            meta_path = self.fs.join(directory, entry.name, "meta.tsv")
            try:
                text, _ = self._read(meta_path, 64 * 1024)
            except Exception:
                continue
            captures.append((entry.name, dict(parse_meta(text))))
        rows = [[meta.get("id", cap), meta.get("ts", ""),
                 meta.get("source", ""), meta.get("strategy", ""),
                 meta.get("dataset", ""), meta.get("row_ids", ""),
                 meta.get("command", "")]
                for cap, meta in captures]
        aligned = align_rows(
            [["ID", "WHEN", "SOURCE", "STRATEGY", "DATASET", "ROWS",
              "COMMAND"]] + rows)
        lines = aligned[1:]
        items: list = [cap for cap, _ in captures]
        if not lines:
            lines = ["(no captures yet -- run 'provost run <cmd>')"]
            items = [None]
        return _View("log", f"captures ({len(captures)}), newest first",
                     lines, items, header=aligned[0] if aligned else "",
                     footer_keys="Enter view capture   / filter"
                                 "   Backspace back")

    def _capture_view(self, capture: str) -> _View:
        directory = self.fs.join(self._captures_dir(), capture)
        lines: list[str] = []
        try:
            text, _ = self._read(self.fs.join(directory, "meta.tsv"),
                                 64 * 1024)
            lines += [f"{key}: {value}" for key, value in parse_meta(text)]
        except Exception as exc:
            lines.append(f"(no metadata: {exc})")
        context_path = self.fs.join(directory, "context.txt")
        if self.fs.exists(context_path):
            try:
                text, _ = self._read(context_path, 64 * 1024)
                lines += ["", "----- context -----"] + text.splitlines()
            except Exception:
                pass
        truncated = False
        lines += ["", f"----- original source (capture {capture}) -----"]
        output_path = self.fs.join(directory, "output.txt")
        try:
            text, truncated = self._read(output_path, MAX_OUTPUT_BYTES)
            lines += text.splitlines()
        except Exception:
            lines.append("(no stored output -- the workspace may be "
                         "detached; try 'provost restore latest')")
        return _View("capture", f"capture {capture}", lines, None,
                     footer_keys="\u2191\u2193 scroll   Backspace back",
                     truncated=truncated)

    def _source_view(self, row_id: str):
        """The capture behind dataset row ``#row_id`` (via provenance.tsv)."""
        prov_path = self.fs.join(self.store, "provenance.tsv")
        try:
            text, _ = self._read(prov_path, MAX_DATASET_BYTES)
        except Exception as exc:
            self.status = f"no provenance: {exc}"
            return None
        found = provenance_lookup(text, row_id)
        if found is None:
            self.status = f"no provenance for row #{row_id}"
            return None
        dataset, capture = found
        view = self._capture_view(capture)
        view.title = f"row #{row_id} -- dataset '{dataset}', " \
                     f"from capture {capture}"
        return view

    # -- opening the item under the cursor --------------------------------------
    def _open_current(self) -> None:
        view = self.stack[-1]
        item = view.current_item()
        if item is None:
            return
        if view.kind == "datasets":
            self.stack.append(self._table_view(item))
        elif view.kind == "log":
            self.stack.append(self._capture_view(item))
        elif view.kind == "table":
            row = item
            if row and row[0].lstrip("#").isdigit():
                source = self._source_view(row[0])
                if source is not None:
                    self.stack.append(source)
            else:
                self.status = "row has no #N index"

    def _reload(self) -> None:
        view = self.stack[-1]
        if view.kind == "datasets":
            self.stack[-1] = self._datasets_view()
        elif view.kind == "log":
            self.stack[-1] = self._log_view()

    def _back(self) -> bool:
        """Pop one view; ``False`` (close the plugin) when at the root."""
        if len(self.stack) > 1:
            self.stack.pop()
            return True
        self.on_exit()
        return False

    # -- keys --------------------------------------------------------------------
    def handle_key(self, key: int):
        if self.mode == "filter":
            return self._filter_key(key)
        return self._list_key(key)

    def _list_key(self, key: int):
        self.status = ""
        view = self.stack[-1]
        last = max(0, len(view.lines) - 1)
        if key == 9:                      # Tab: let the app switch panes
            return None
        if key == curses.KEY_F10:
            self.on_exit()
            return False
        if key == 27:                     # Esc: back, closing at the root
            return self._back()
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if len(self.stack) > 1:
                self.stack.pop()
            return True
        if key == curses.KEY_UP:
            self._move(view, -1)
        elif key == curses.KEY_DOWN:
            self._move(view, 1)
        elif key == curses.KEY_PPAGE:
            self._move(view, -10)
        elif key == curses.KEY_NPAGE:
            self._move(view, 10)
        elif key == curses.KEY_HOME:
            view.cursor = view.scroll = 0
        elif key == curses.KEY_END:
            view.cursor = last
            view.scroll = last
        elif key == curses.KEY_LEFT:
            view.hscroll = max(0, view.hscroll - 8)
        elif key == curses.KEY_RIGHT:
            view.hscroll += 8
        elif key in (10, 13, curses.KEY_ENTER):
            self._open_current()
        elif key == ord("s") and view.kind == "table":
            self._open_current()
        elif key == ord("l") and view.kind == "datasets":
            self.stack.append(self._log_view())
        elif key == ord("t") and view.kind == "datasets":
            if self.fs.exists(self.fs.join(self._datasets_dir(),
                                           "unsorted.tsv")):
                self.stack.append(self._table_view("unsorted"))
            else:
                self.status = "triage queue empty -- nothing unmatched"
        elif key == ord("r"):
            self._reload()
        elif key == ord("/"):
            self.mode = "filter"
        return True

    def _move(self, view: _View, step: int) -> None:
        last = max(0, len(view.lines) - 1)
        if view.selectable:
            view.cursor = min(last, max(0, view.cursor + step))
        else:
            view.scroll = min(last, max(0, view.scroll + step))

    def _filter_key(self, key: int):
        view = self.stack[-1]
        if key == 27:                     # Esc: clear the filter, leave mode
            view.set_filter("")
            self.mode = "list"
        elif key in (10, 13, curses.KEY_ENTER):
            self.mode = "list"
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            view.set_filter(view.filter[:-1])
        elif key == 21:                   # Ctrl-U clears the filter
            view.set_filter("")
        elif key == 9:
            return None
        elif 32 <= key < 0x110000:
            view.set_filter(view.filter + chr(key))
        return True

    # -- drawing --------------------------------------------------------------
    def _footer(self, view: _View) -> str:
        if self.mode == "filter":
            return f" filter> {view.filter} "
        if self.status:
            return f" {self.status} "
        note = "  [truncated]" if view.truncated else ""
        return f" {view.footer_keys}{note} "

    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if h < 3:
            return
        view = self.stack[-1]
        # The root titles the whole store; deeper views say where they hang.
        crumb = f"{self.stack[-2].kind} > {view.title}" \
            if len(self.stack) > 1 else view.title
        flt = f"  [/{view.filter}]" if view.filter else ""
        self.put(stdscr, y, x, w, f" [provost] {crumb}{flt} ",
                 theme.attr("keybar"))

        row0 = y + 1
        rows = h - 2                          # title + footer
        if view.header:
            self.put(stdscr, row0, x, w, view.header[view.hscroll:],
                     theme.attr("panelhead"))
            row0 += 1
            rows -= 1

        if rows > 0:
            if view.selectable:
                if view.cursor < view.scroll:
                    view.scroll = view.cursor
                if view.cursor >= view.scroll + rows:
                    view.scroll = view.cursor - rows + 1
            else:
                view.scroll = min(view.scroll,
                                  max(0, len(view.lines) - rows))
            visible = view.lines[view.scroll:view.scroll + rows]
            for row in range(rows):
                if row < len(visible):
                    attr = None
                    if view.selectable \
                            and view.scroll + row == view.cursor:
                        attr = theme.attr("panelcursor")
                    self.put(stdscr, row0 + row, x, w,
                             visible[row][view.hscroll:], attr)
                elif row == 0 and not visible:
                    self.put(stdscr, row0, x, w, "  (nothing matches)")
                else:
                    self.put(stdscr, row0 + row, x, w, "")

        self.put(stdscr, y + h - 1, x, w, self._footer(view),
                 theme.attr("keybar"))
