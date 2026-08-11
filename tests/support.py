"""Shared test harness: screen stand-ins, scripted dialogs and fake backends.

The guiding rule is that only the *terminal* and the *blocking dialogs* are
ever replaced.  Panels, filesystems, plug-ins and on-disk files stay real, so
assertions are about what the application actually did rather than about which
mock it happened to call.
"""

from __future__ import annotations

import curses
import io
import os
import pty
import select
import struct
import tarfile
import threading
import zipfile
import zlib
from xml.sax.saxutils import escape

import pytest

from meridian_commander import dialogs, theme
from meridian_commander.filesystems import LocalFileSystem


def write(path: str, content: str, mtime: float | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


class _StubScreen:
    """A screen that reports a size and silently swallows any drawing.

    Handlers under test mostly never draw, but a few (the sync preview) call
    draw() mid-operation; there is nothing to assert about those pixels, so
    they go nowhere rather than needing a real terminal.
    """

    def __init__(self, height: int = 24, width: int = 80) -> None:
        self._size = (height, width)

    def getmaxyx(self):
        return self._size

    def erase(self):
        pass

    def addstr(self, *args, **kwargs):
        pass

    def addch(self, *args, **kwargs):
        pass

    def attrset(self, *args):
        pass

    def noutrefresh(self):
        pass

    def timeout(self, value):
        pass


class _ScriptedDialogs:
    """Scripted stand-ins for the blocking dialogs.

    Each helper answers from its script and records what it was asked, so a
    test can assert both on the choice made and on the text the user saw.
    A menu answer given as a string picks the first option containing it,
    which keeps tests readable and independent of option ordering.
    """

    def __init__(self, monkeypatch, menu=(), prompt=(), confirm=()):
        self.menu_answers = list(menu)
        self.prompt_answers = list(prompt)
        self.confirm_answers = list(confirm)
        self.menus: list[tuple] = []
        self.menu_keys: list[list] = []   # shortcut letters each menu offered
        self.prompts: list[str] = []
        self.messages: list[tuple] = []
        monkeypatch.setattr(dialogs, "menu", self._menu)
        monkeypatch.setattr(dialogs, "prompt", self._prompt)
        monkeypatch.setattr(dialogs, "confirm", self._confirm)
        monkeypatch.setattr(dialogs, "message", self._message)

    def _menu(self, stdscr, title, options, keys=None):
        self.menus.append((title, list(options)))
        self.menu_keys.append(list(keys) if keys else [])
        answer = self.menu_answers.pop(0)
        if isinstance(answer, str):
            return next(i for i, o in enumerate(options) if answer in o)
        return answer

    def _prompt(self, stdscr, title, label, default="", is_password=False):
        self.prompts.append(label)
        return self.prompt_answers.pop(0)

    def _confirm(self, stdscr, title, text, default_yes=False):
        self.messages.append((title, text, False))
        return self.confirm_answers.pop(0)

    def _message(self, stdscr, title, text, error=False):
        self.messages.append((title, text, error))

    @property
    def last_message(self) -> str:
        return self.messages[-1][1] if self.messages else ""

    @property
    def all_text(self) -> str:
        return "\n".join(text for _, text, _ in self.messages)


class _FakeRemoteBackend(LocalFileSystem):
    """A local backend wearing an SFTP name tag.

    Real enough to back a panel (it lists actual directories) while matching
    a remote preset, which is what the reuse and reconnect paths key on.
    """

    scheme = "sftp"

    def __init__(self, host="web1", username="deploy", port=22):
        super().__init__()
        self.host = host
        self.typed_username = username
        self.port = port
        self.key_filename = ""

    def label(self) -> str:
        return f"sftp://{self.typed_username}@{self.host}"


# -- real curses screens -------------------------------------------------------
#
# Drawing code is tested against a real curses screen on a pseudo-terminal
# rather than a stand-in window.  Only a real window reports the errors that
# overflowing it produces, so only a real window can catch a regression in the
# code that guards against them.

def _drain(fd: int, stop) -> None:
    """Read and throw away everything written to a pseudo-terminal.

    Without this the screens below deadlock: curses writes the whole picture
    to the terminal, nothing is reading the other end, and once the pipe
    buffer fills the write blocks for ever.  A blank screen fitted; a screen
    with a painted background does not, which is a property of the terminal
    rather than of the code under test.
    """
    while not stop.is_set():
        try:
            if select.select([fd], [], [], 0.05)[0] and not os.read(fd, 65536):
                return
        except OSError:
            return


def with_curses_screen(rows: int, cols: int, fn, colours: int | None = None):
    """Run ``fn(stdscr)`` on a real curses screen ``rows`` x ``cols``.

    The terminal type is fixed at ``xterm-256color`` for every screen, because
    curses reads terminfo once per process: whichever screen initialises first
    decides the colour count for every screen after it, so a test that asked
    for a different one would pass or fail on its position in the run.

    ``colours`` overrides the reported ``curses.COLORS`` for the duration, for
    code that only *consults* the count.  It cannot conjure colours that are
    not there -- ``init_pair`` still answers to the real terminal -- so it is
    only useful for the paths that check the count and then decline to use it.
    """
    master, slave = pty.openpty()
    stop = threading.Event()
    reader = threading.Thread(target=_drain, args=(master, stop), daemon=True)
    reader.start()
    saved_out, saved_in = os.dup(1), os.dup(0)
    saved_term = os.environ.get("TERM")
    os.environ["TERM"] = "xterm-256color"
    os.dup2(slave, 1)
    os.dup2(slave, 0)
    started = False
    try:
        try:
            stdscr = curses.initscr()
        except curses.error as exc:   # a machine with no terminfo database
            pytest.skip(f"curses screen unavailable: {exc}")
        started = True
        curses.start_color()
        if colours is not None:
            saved_colours = getattr(curses, "COLORS", 8)
            curses.COLORS = colours
        # Give the screen the colours the application would have given it, so
        # drawing is tested in the palette it actually runs in -- and so a
        # test that deliberately breaks the theme cannot leak into the next.
        theme.init()
        curses.resizeterm(rows, cols)
        # curses keeps one screen per process, so wipe whatever an earlier
        # test left behind: every caller expects a blank terminal.
        stdscr.erase()
        return fn(stdscr)
    finally:
        if started:
            if colours is not None:
                curses.COLORS = saved_colours
            curses.endwin()
        stop.set()
        reader.join(timeout=2)
        os.dup2(saved_out, 1)
        os.dup2(saved_in, 0)
        for fd in (saved_out, saved_in, master, slave):
            os.close(fd)
        if saved_term is None:
            os.environ.pop("TERM", None)
        else:
            os.environ["TERM"] = saved_term


class _ScriptedWindow:
    """A real curses window with its keystrokes scripted and draws recorded."""

    def __init__(self, win, keys, fail_draws: bool = False):
        self._win = win
        self._keys = list(keys)
        self._fail_draws = fail_draws
        self.drawn: list[tuple] = []

    def getch(self):
        return self._keys.pop(0)

    def addstr(self, *args):
        self.drawn.append(args)
        # fail_draws is True to refuse the body rows (the frame and title come
        # from _box, which bounds its own writes), or a predicate taking
        # (y, x) to target exactly the writes the code under test guards.
        if callable(self._fail_draws):
            refuse = self._fail_draws(args[0], args[1])
        else:
            refuse = self._fail_draws and args[0] >= 2
        if refuse:
            raise curses.error("addwstr() returned ERR")
        return self._win.addstr(*args)

    def __getattr__(self, name):
        return getattr(self._win, name)


class _KeyScript:
    """A screen whose getch() replays a script, for full-screen key loops.

    Anything the code under test draws goes to the real window underneath, so
    the drawing is genuinely exercised; only the input is synthetic.
    """

    def __init__(self, win, keys, fail_draws: bool = False):
        self._win = win
        self._keys = list(keys)
        self._fail_draws = fail_draws
        self.drawn: list[tuple] = []

    def getch(self):
        if not self._keys:
            return 27          # nothing scripted left: behave as Escape
        return self._keys.pop(0)

    def addstr(self, *args):
        self.drawn.append(args)
        if self._fail_draws:
            raise curses.error("addwstr() returned ERR")
        # Errors from the real window are deliberately *not* swallowed here:
        # the code under test has its own guards, and hiding the error would
        # hide whether those guards work.
        return self._win.addstr(*args)

    def __getattr__(self, name):
        return getattr(self._win, name)

    @property
    def text(self) -> str:
        return "\n".join(str(a) for a in self.drawn)


def scripted_menu(monkeypatch, keys, fail_draws=False):
    """Make ``dialogs._center`` hand back a key-scripted real window.

    Returns a one-element dict that will hold the window once the dialog runs.
    """
    real_center = dialogs._center
    captured: dict = {}

    def scripted_center(stdscr, height, width):
        window = _ScriptedWindow(real_center(stdscr, height, width), keys,
                                 fail_draws)
        captured["window"] = window
        return window

    monkeypatch.setattr(dialogs, "_center", scripted_center)
    return captured


def run_menu(monkeypatch, rows, options, keys, fail_draws=False, title="Presets",
             accel=None):
    """Drive ``dialogs.menu`` on an ``rows``-high screen; return (choice, draws).

    ``keys`` are the keystrokes typed at it; ``accel`` is the list of shortcut
    letters the menu is built with, if any.
    """
    captured = scripted_menu(monkeypatch, keys, fail_draws)
    choice = with_curses_screen(
        rows, 60, lambda stdscr: dialogs.menu(stdscr, title, options, accel))
    return choice, captured["window"].drawn


def script_newwin(monkeypatch, keys, fail_draws: bool = False):
    """Patch ``curses.newwin`` so windows created replay ``keys``.

    Full-screen components build their own window and read keys from it.  The
    window handed back is real -- everything drawn goes through curses and is
    bounds-checked by it -- only the input is scripted.  Returns a dict that
    collects the windows as they are created.
    """
    real_newwin = curses.newwin
    captured: dict = {"windows": []}

    def fake_newwin(*args, **kwargs):
        window = _KeyScript(real_newwin(*args, **kwargs), keys, fail_draws)
        captured["windows"].append(window)
        captured["window"] = window
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    return captured


# -- spreadsheet fixtures ------------------------------------------------------
# Workbooks are built as real zip archives with the part layout a producer
# writes, so the reader is exercised on the format rather than on a stand-in.
# The reader was checked against openpyxl-written files while it was written;
# the fixtures below reproduce the same layout so the suite needs no producer.

XLSX_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def xlsx_bytes(parts: dict, *, compress: bool = True) -> bytes:
    """Zip a mapping of archive member name to contents into workbook bytes.

    ``compress=False`` stores the parts verbatim, so a test can damage one of
    them by substituting bytes of the same length -- which leaves the archive
    openable and fails only when that part is read.
    """
    buf = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", mode) as zf:
        for name, content in parts.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _col(index: int) -> str:
    """A1-style column label.  Written out here so the fixtures do not lean
    on the very function under test."""
    name = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def rows_xml(rows) -> str:
    """``<row>``/``<c>`` XML for a list of row lists, every cell inline text.

    Empty cells are left out entirely, the way a real producer writes them, so
    the reader's handling of sparse rows is exercised by every fixture.
    """
    out = []
    for r, row in enumerate(rows, 1):
        cells = "".join(
            f'<c r="{_col(c)}{r}" t="inlineStr"><is><t>'
            f"{escape(str(value))}</t></is></c>"
            for c, value in enumerate(row) if str(value) != "")
        out.append(f'<row r="{r}">{cells}</row>')
    return "".join(out)


def sheet_xml(body: str) -> str:
    """Wrap ``<row>`` XML in a worksheet part."""
    return (f'<?xml version="1.0"?><worksheet xmlns="{XLSX_MAIN}">'
            f"<sheetData>{body}</sheetData></worksheet>")


def xlsx_parts(sheets: dict, *, shared: str = "", styles: str = "",
               workbook_pr: str = "<workbookPr/>") -> dict:
    """The standard archive layout for a workbook.

    ``sheets`` maps a sheet name to its worksheet XML; ``shared`` and
    ``styles`` are part bodies, included (with their relationships) only when
    given, so a workbook without them can be built too.
    """
    entries = list(sheets.items())
    declarations = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (name, _xml) in enumerate(entries, 1))
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="{XLSX_REL}/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(entries) + 1))
    parts = {
        "[Content_Types].xml":
            f'<?xml version="1.0"?><Types xmlns="{XLSX_PKG}"/>',
        "_rels/.rels":
            f'<?xml version="1.0"?><Relationships xmlns="{XLSX_PKG}">'
            f'<Relationship Id="rIdW" Type="{XLSX_REL}/officeDocument" '
            f'Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml":
            f'<?xml version="1.0"?><workbook xmlns="{XLSX_MAIN}" '
            f'xmlns:r="{XLSX_REL}">{workbook_pr}'
            f"<sheets>{declarations}</sheets></workbook>",
    }
    for i, (_name, xml) in enumerate(entries, 1):
        parts[f"xl/worksheets/sheet{i}.xml"] = xml
    if shared:
        rels += (f'<Relationship Id="rIdS" Type="{XLSX_REL}/sharedStrings" '
                 f'Target="sharedStrings.xml"/>')
        parts["xl/sharedStrings.xml"] = shared
    if styles:
        rels += (f'<Relationship Id="rIdT" Type="{XLSX_REL}/styles" '
                 f'Target="styles.xml"/>')
        parts["xl/styles.xml"] = styles
    parts["xl/_rels/workbook.xml.rels"] = (
        f'<?xml version="1.0"?><Relationships xmlns="{XLSX_PKG}">'
        f"{rels}</Relationships>")
    return parts


def simple_xlsx(sheets: dict) -> bytes:
    """Workbook bytes from ``{sheet name: list of row lists}``."""
    return xlsx_bytes(xlsx_parts(
        {name: sheet_xml(rows_xml(rows)) for name, rows in sheets.items()}))


def write_xlsx(path: str, sheets: dict) -> str:
    """Write a workbook to ``path`` and return the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(simple_xlsx(sheets))
    return path


# -- document fixtures ---------------------------------------------------------
DOCX_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx_p(text: str, style: str = "", num=None) -> str:
    """A ``<w:p>``: optionally styled, optionally part of list ``(numId, ilvl)``."""
    pr = ""
    if style:
        pr += f'<w:pStyle w:val="{escape(style)}"/>'
    if num is not None:
        num_id, level = num
        pr += (f'<w:numPr><w:ilvl w:val="{level}"/>'
               f'<w:numId w:val="{num_id}"/></w:numPr>')
    pr = f"<w:pPr>{pr}</w:pPr>" if pr else ""
    return f"<w:p>{pr}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def docx_table(rows) -> str:
    """A ``<w:tbl>`` from a list of row lists."""
    out = []
    for row in rows:
        cells = "".join(
            f"<w:tc><w:p><w:r><w:t>{escape(str(c))}</w:t></w:r></w:p></w:tc>"
            for c in row)
        out.append(f"<w:tr>{cells}</w:tr>")
    return f"<w:tbl>{''.join(out)}</w:tbl>"


def docx_numbering(definitions) -> str:
    """``numbering.xml`` from ``{numId: [format per level]}``.

    Each numId gets its own abstract definition, which is the shape Word
    writes and the reason reading a list marker takes two hops.
    """
    abstracts, nums = [], []
    for num_id, formats in definitions.items():
        levels = "".join(
            f'<w:lvl w:ilvl="{i}"><w:numFmt w:val="{f}"/></w:lvl>'
            for i, f in enumerate(formats))
        abstracts.append(
            f'<w:abstractNum w:abstractNumId="A{num_id}">{levels}</w:abstractNum>')
        nums.append(f'<w:num w:numId="{num_id}">'
                    f'<w:abstractNumId w:val="A{num_id}"/></w:num>')
    return (f'<?xml version="1.0"?><w:numbering xmlns:w="{DOCX_W}">'
            f"{''.join(abstracts)}{''.join(nums)}</w:numbering>")


def docx_parts(body: str, numbering: str = "") -> dict:
    """The standard archive layout for a Word document."""
    rels = ""
    parts = {
        "[Content_Types].xml":
            f'<?xml version="1.0"?><Types xmlns="{XLSX_PKG}"/>',
        "_rels/.rels":
            f'<?xml version="1.0"?><Relationships xmlns="{XLSX_PKG}">'
            f'<Relationship Id="rIdD" Type="{XLSX_REL}/officeDocument" '
            f'Target="word/document.xml"/></Relationships>',
        "word/document.xml":
            f'<?xml version="1.0"?><w:document xmlns:w="{DOCX_W}" '
            f'xmlns:r="{XLSX_REL}">'
            f"<w:body>{body}</w:body></w:document>",
    }
    if numbering:
        rels += (f'<Relationship Id="rIdN" Type="{XLSX_REL}/numbering" '
                 f'Target="numbering.xml"/>')
        parts["word/numbering.xml"] = numbering
    parts["word/_rels/document.xml.rels"] = (
        f'<?xml version="1.0"?><Relationships xmlns="{XLSX_PKG}">'
        f"{rels}</Relationships>")
    return parts


def simple_docx(body: str, numbering: str = "") -> bytes:
    return xlsx_bytes(docx_parts(body, numbering))


def write_docx(path: str, body: str, numbering: str = "") -> str:
    """Write a document to ``path`` and return the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(simple_docx(body, numbering))
    return path


# -- archive fixtures ----------------------------------------------------------
def write_zip(path: str, members: dict) -> str:
    """Write a zip.  A member name ending in "/" becomes a directory entry."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


def write_tar(path: str, members: dict, *, mode: str = "w",
              symlinks: dict | None = None, dirs=(), fifos=()) -> str:
    """Write a tar (optionally compressed via ``mode``, e.g. ``"w:gz"``)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tarfile.open(path, mode) as tf:
        for name in dirs:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        for name, content in members.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1700000000
            tf.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
        for name in fifos:
            info = tarfile.TarInfo(name)
            info.type = tarfile.FIFOTYPE
            tf.addfile(info)
    return path


class _NoLocalPath(LocalFileSystem):
    """A backend with no local path, the way every remote one is.

    The archive reader asks for a real file and falls back to streaming the
    bytes when there is none; this is what makes that fallback reachable
    without a network.
    """

    scheme = "sftp"
    local_path = None

    def label(self) -> str:
        return "sftp://elsewhere"


# -- presentation fixtures -----------------------------------------------------
PPTX_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
PPTX_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def pptx_p(text: str, level: int = 0) -> str:
    """An ``<a:p>`` at an outline level, its text split across two runs.

    Split deliberately: DrawingML breaks a line wherever formatting changes,
    so a reader that takes only the first run loses half of every sentence.
    """
    head, _, tail = text.partition(" ")
    pr = f'<a:pPr lvl="{level}"/>' if level else ""
    runs = f"<a:r><a:t>{escape(head)}</a:t></a:r>"
    if tail:
        runs += f"<a:r><a:t> {escape(tail)}</a:t></a:r>"
    return f"<a:p>{pr}{runs}</a:p>"


def pptx_shape(paragraphs: str, placeholder: str = "") -> str:
    """A ``<p:sp>`` text shape, optionally a placeholder of the given type."""
    ph = f'<p:ph type="{placeholder}"/>' if placeholder else ""
    return (f"<p:sp><p:nvSpPr><p:nvPr>{ph}</p:nvPr></p:nvSpPr>"
            f"<p:txBody>{paragraphs}</p:txBody></p:sp>")


def pptx_table(rows) -> str:
    """A ``<p:graphicFrame>`` holding an ``<a:tbl>``."""
    body = ""
    for row in rows:
        cells = "".join(
            f"<a:tc><a:txBody><a:p><a:r><a:t>{escape(str(c))}</a:t>"
            f"</a:r></a:p></a:txBody></a:tc>" for c in row)
        body += f"<a:tr>{cells}</a:tr>"
    return f"<p:graphicFrame><a:graphic><a:tbl>{body}</a:tbl></a:graphic></p:graphicFrame>"


def slide_xml(shapes: str) -> str:
    return (f'<?xml version="1.0"?><p:sld xmlns:p="{PPTX_P}" '
            f'xmlns:a="{PPTX_A}"><p:cSld><p:spTree>{shapes}'
            "</p:spTree></p:cSld></p:sld>")


def notes_xml(paragraphs: str) -> str:
    return (f'<?xml version="1.0"?><p:notes xmlns:p="{PPTX_P}" '
            f'xmlns:a="{PPTX_A}"><p:cSld><p:spTree><p:sp><p:txBody>'
            f"{paragraphs}</p:txBody></p:sp></p:spTree></p:cSld></p:notes>")


def pptx_parts(slides, notes=None, order=None) -> dict:
    """The standard archive layout for a deck.

    ``slides`` is a list of worksheet-style slide XML; ``order`` gives the
    ``sldIdLst`` order as zero-based indices, which is how a reordered deck is
    built -- the parts keep their original names.
    """
    notes = notes or {}
    order = list(range(len(slides))) if order is None else order
    ids = "".join(
        f'<p:sldId id="{256 + n}" r:id="rIdS{i}"/>' for n, i in enumerate(order))
    rels = "".join(
        f'<Relationship Id="rIdS{i}" Type="{XLSX_REL}/slide" '
        f'Target="slides/slide{i + 1}.xml"/>' for i in range(len(slides)))
    parts = {
        "[Content_Types].xml":
            f'<?xml version="1.0"?><Types xmlns="{XLSX_PKG}"/>',
        "_rels/.rels":
            f'<?xml version="1.0"?><Relationships xmlns="{XLSX_PKG}">'
            f'<Relationship Id="rIdP" Type="{XLSX_REL}/officeDocument" '
            f'Target="ppt/presentation.xml"/></Relationships>',
        "ppt/presentation.xml":
            f'<?xml version="1.0"?><p:presentation xmlns:p="{PPTX_P}" '
            f'xmlns:r="{XLSX_REL}"><p:sldIdLst>{ids}</p:sldIdLst>'
            "</p:presentation>",
        "ppt/_rels/presentation.xml.rels":
            f'<?xml version="1.0"?><Relationships xmlns="{XLSX_PKG}">'
            f"{rels}</Relationships>",
    }
    for i, xml in enumerate(slides):
        parts[f"ppt/slides/slide{i + 1}.xml"] = xml
        slide_rels = ""
        if i in notes:
            parts[f"ppt/notesSlides/notesSlide{i + 1}.xml"] = notes[i]
            slide_rels = (f'<Relationship Id="rIdN" Type="{XLSX_REL}/notesSlide"'
                          f' Target="../notesSlides/notesSlide{i + 1}.xml"/>')
        parts[f"ppt/slides/_rels/slide{i + 1}.xml.rels"] = (
            f'<?xml version="1.0"?><Relationships xmlns="{XLSX_PKG}">'
            f"{slide_rels}</Relationships>")
    return parts


def simple_pptx(slides, notes=None, order=None) -> bytes:
    return xlsx_bytes(pptx_parts(slides, notes, order))


def write_pptx(path: str, slides, notes=None, order=None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(simple_pptx(slides, notes, order))
    return path


# -- image fixtures ------------------------------------------------------------
#
# Built by hand rather than by an imaging library, for the same reason the
# OOXML fixtures above are: the project takes no dependency it does not need,
# and a fixture written out byte by byte is a second, independent statement of
# what the format actually says. The decoders were separately checked against
# files produced by Pillow; these keep that honest without importing it.

def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def png_bytes(width: int, height: int, rows, *, depth: int = 8,
              colour: int = 2, palette: bytes = b"", trans: bytes = b"",
              interlace: int = 0, filters=None, extra: bytes = b"") -> bytes:
    """A PNG from already-packed scanlines.

    ``rows`` are the raw bytes of each scanline *before* filtering; ``filters``
    picks the filter byte per row (all 0 -- None -- by default), so the
    unfilter loop can be exercised one case at a time.
    """
    header = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0,
                         interlace)
    if filters is None:
        filters = [0] * len(rows)
    raw = b"".join(bytes([f]) + bytes(r) for f, r in zip(filters, rows))
    out = [b"\x89PNG\r\n\x1a\n", png_chunk(b"IHDR", header)]
    if palette:
        out.append(png_chunk(b"PLTE", palette))
    if trans:
        out.append(png_chunk(b"tRNS", trans))
    out.append(extra)
    out.append(png_chunk(b"IDAT", zlib.compress(raw)))
    out.append(png_chunk(b"IEND", b""))
    return b"".join(out)


def rgb_png(width: int, height: int, pixels) -> bytes:
    """An 8-bit truecolour PNG from a flat list of ``(r, g, b)`` tuples."""
    rows = [bytes(c for x in range(width)
                  for c in pixels[y * width + x]) for y in range(height)]
    return png_bytes(width, height, rows)


def bmp_bytes(width: int, height: int, rows, *, depth: int = 24,
              palette: bytes = b"", compression: int = 0,
              masks=None, top_down: bool = False, core: bool = False,
              header_size: int = 40) -> bytes:
    """A BMP from packed scanlines, given bottom-up unless ``top_down``.

    ``header_size`` picks the header vintage.  At 40 (BITMAPINFOHEADER) any
    bitfield masks sit in the gap *after* the header, which is where pre-v4
    writers put them; at 108 or more (BITMAPV4HEADER and later) they live
    inside the header itself, and a fourth mask for alpha becomes available.
    """
    stride = ((width * depth + 31) // 32) * 4
    body = b"".join(bytes(r).ljust(stride, b"\x00") for r in rows)
    if core:
        header = struct.pack("<IHHHH", 12, width, height, 1, depth)
    else:
        header = struct.pack("<IiiHHIIiiII", header_size, width,
                             -height if top_down else height, 1, depth,
                             compression, len(body), 2835, 2835,
                             len(palette) // (3 if core else 4), 0)
        if masks:
            fields = tuple(masks) + (0,) * (4 - len(masks))
            if header_size >= 56:
                header += struct.pack("<IIII", *fields)
            else:
                header += struct.pack("<III", *fields[:3])
        header = header.ljust(header_size, b"\x00")
    offset = 14 + len(header) + len(palette)
    return (b"BM" + struct.pack("<IHHI", offset + len(body), 0, 0, offset)
            + header + palette + body)


def pnm_bytes(magic: bytes, width: int, height: int, body, *, top: int = 255,
              comment: bytes = b"") -> bytes:
    head = magic + b"\n" + comment + b"%d %d\n" % (width, height)
    if magic not in (b"P1", b"P4"):
        head += b"%d\n" % top
    if isinstance(body, (bytes, bytearray)):
        return head + bytes(body)
    return head + b" ".join(b"%d" % v for v in body) + b"\n"


def gif_lzw(indices, min_code_size: int) -> bytes:
    """Encode indices as GIF LZW using literals only.

    A literal-only stream is valid LZW and much easier to be sure of than a
    real compressor: emit a clear code, then one code per pixel, clearing
    again before the table would force the code width up. That deliberately
    exercises the decoder's table-reset path on every fixture.
    """
    clear = 1 << min_code_size
    code_size = min_code_size + 1
    bits, out, count = [], bytearray(), 0

    def put(code):
        for i in range(code_size):
            bits.append((code >> i) & 1)

    put(clear)
    for index in indices:
        # After the clear the table regrows one entry per emitted code; reset
        # before it reaches the width boundary so code_size never changes.
        if count >= clear - 2:
            put(clear)
            count = 0
        put(index)
        count += 1
    put(clear + 1)                      # end of information
    while len(bits) % 8:
        bits.append(0)
    for i in range(0, len(bits), 8):
        out.append(sum(b << n for n, b in enumerate(bits[i:i + 8])))
    return bytes(out)


def gif_sub_blocks(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 255):
        piece = data[i:i + 255]
        out.append(len(piece))
        out += piece
    out.append(0)
    return bytes(out)


def gif_frame(indices, width: int, height: int, *, left: int = 0, top: int = 0,
              min_code_size: int = 2, interlaced: bool = False,
              local: bytes = b"", delay: int = 0, transparent=None,
              disposal: int = 0) -> bytes:
    """One GIF frame, with its graphic-control extension when it needs one."""
    out = bytearray()
    if delay or transparent is not None or disposal:
        flags = (disposal << 2) | (1 if transparent is not None else 0)
        out += (b"\x21\xf9\x04" + bytes([flags])
                + struct.pack("<H", delay) + bytes([transparent or 0]) + b"\x00")
    packed = (0x40 if interlaced else 0)
    if local:
        packed |= 0x80 | ((len(local) // 3).bit_length() - 2)
    out += b"\x2c" + struct.pack("<HHHHB", left, top, width, height, packed)
    out += local
    out += bytes([min_code_size]) + gif_sub_blocks(gif_lzw(indices,
                                                           min_code_size))
    return bytes(out)


def gif_bytes(width: int, height: int, frames, *, palette: bytes = b"",
              version: bytes = b"GIF89a") -> bytes:
    packed = 0
    if palette:
        packed = 0x80 | ((len(palette) // 3).bit_length() - 2)
    return (version + struct.pack("<HHBBB", width, height, packed, 0, 0)
            + palette + b"".join(frames) + b"\x3b")


# -- JPEG fixtures -------------------------------------------------------------
#
# A minimal encoder, enough to produce streams the DC-only decoder can be
# driven through. Both Huffman tables give every symbol the same 4-bit code
# length, so a symbol's code is simply its index -- canonical, valid, and
# short enough to reason about by eye.

#: DC symbols are the coefficient bit-lengths 0..11.
JPEG_DC_VALUES = bytes(range(12))
#: AC symbol 0 is end-of-block and 0xF0 is a run of sixteen zeroes; the rest
#: are (run << 4 | size) pairs used to exercise the coefficient-skipping loop.
JPEG_AC_VALUES = bytes([0x00, 0xF0, 0x01, 0x02, 0x11, 0x21, 0x31, 0x41,
                        0x51, 0x61, 0x71, 0x81, 0x91, 0xA1, 0xB1, 0xC1])
#: Both tables put every code at length 4, so a symbol's code is its index.
JPEG_DC_COUNTS = bytes([0, 0, 0, len(JPEG_DC_VALUES)] + [0] * 12)
JPEG_AC_COUNTS = bytes([0, 0, 0, len(JPEG_AC_VALUES)] + [0] * 12)


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


class JpegBits:
    """Collects bits and writes them out with JPEG's 0xFF stuffing."""

    def __init__(self) -> None:
        self.bits: list[int] = []

    def put(self, value: int, count: int) -> None:
        for i in range(count - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def symbol(self, values: bytes, symbol: int) -> None:
        self.put(values.index(symbol), 4)       # every code is 4 bits

    def coefficient(self, diff: int) -> None:
        """A DC difference, as its bit-length followed by its magnitude bits."""
        size = abs(diff).bit_length()
        self.symbol(JPEG_DC_VALUES, size)
        if size:
            self.put(diff if diff > 0 else diff + (1 << size) - 1, size)

    def bytes(self) -> bytes:
        bits = self.bits + [1] * (-len(self.bits) % 8)   # pad with ones
        out = bytearray()
        for i in range(0, len(bits), 8):
            byte = sum(b << (7 - n) for n, b in enumerate(bits[i:i + 8]))
            out.append(byte)
            if byte == 0xFF:
                out.append(0x00)                # stuffing
        return bytes(out)


def jpeg_bytes(width: int, height: int, blocks, *, sampling=None,
               sof: int = 0xC0, quality: int = 1, restart: int = 0,
               extra: bytes = b"", adobe=None, exif=None,
               ac_extra=None) -> bytes:
    """A JPEG whose blocks carry a DC coefficient and nothing else.

    ``blocks`` is a list per component of that component's DC *values* in
    scan order; they are differenced here.  ``sampling`` gives ``(h, v)`` per
    component, defaulting to 1x1 for every one.
    """
    count = len(blocks)
    sampling = sampling or [(1, 1)] * count
    progressive = sof == 0xC2

    out = bytearray(b"\xff\xd8")
    if exif is not None:
        out += jpeg_segment(0xE1, exif)
    if adobe is not None:
        out += jpeg_segment(0xEE, b"Adobe" + b"\x00" * 6 + bytes([adobe]))
    out += jpeg_segment(0xDB, b"\x00" + bytes([quality] * 64))
    frame = struct.pack(">BHHB", 8, height, width, count)
    for i, (h, v) in enumerate(sampling):
        frame += bytes([i + 1, (h << 4) | v, 0])
    out += jpeg_segment(sof, frame)
    out += jpeg_segment(0xC4, b"\x00" + JPEG_DC_COUNTS + JPEG_DC_VALUES)
    out += jpeg_segment(0xC4, b"\x10" + JPEG_AC_COUNTS + JPEG_AC_VALUES)
    if restart:
        out += jpeg_segment(0xDD, struct.pack(">H", restart))
    out += extra

    scan = bytes([count])
    for i in range(count):
        scan += bytes([i + 1, 0x00])
    scan += bytes([0, 0 if progressive else 63, 0])
    out += jpeg_segment(0xDA, scan)

    bits = JpegBits()
    previous = [0] * count
    units = max(len(b) // (h * v) for b, (h, v) in zip(blocks, sampling))
    for unit in range(units):
        if restart and unit and unit % restart == 0:
            out += bits.bytes()
            out += bytes([0xFF, 0xD0 + ((unit // restart - 1) % 8)])
            bits = JpegBits()
            previous = [0] * count
        for c in range(count):
            h, v = sampling[c]
            for b in range(h * v):
                at = unit * h * v + b
                value = blocks[c][at] if at < len(blocks[c]) else previous[c]
                bits.coefficient(value - previous[c])
                previous[c] = value
                if not progressive:
                    for symbol, size in (ac_extra or ()):
                        bits.symbol(JPEG_AC_VALUES, symbol)
                        if size:
                            bits.put(1, size)
                    bits.symbol(JPEG_AC_VALUES, 0x00)      # end of block
    out += bits.bytes()
    return bytes(out + b"\xff\xd9")


def exif_bytes(orientation: int, *, order: bytes = b"II") -> bytes:
    """An APP1 payload carrying just an orientation tag."""
    pack = "<" if order == b"II" else ">"
    ifd = struct.pack(pack + "H", 1)
    ifd += struct.pack(pack + "HHI", 0x0112, 3, 1)
    ifd += struct.pack(pack + "H", orientation) + b"\x00\x00"
    ifd += struct.pack(pack + "I", 0)
    magic = order + (b"*\x00" if order == b"II" else b"\x00*")
    return b"Exif\x00\x00" + magic + struct.pack(pack + "I", 8) + ifd


# -- PDF fixtures --------------------------------------------------------------
#
# Built byte by byte, like the others. The readers were separately checked
# against files from reportlab, pypdf and qpdf, but none of those is a
# dependency this project will take, and a file spelled out in full says what
# the format says rather than what one producer happens to emit.

def pdf_stream(dictionary: str, body: bytes, *, deflate: bool = False) -> bytes:
    """A stream object body, with /Length filled in from the data."""
    if deflate:
        body = zlib.compress(body)
        dictionary = dictionary.rstrip()[:-2] + " /Filter /FlateDecode >>"
    head = dictionary.rstrip()[:-2] + " /Length %d >>" % len(body)
    return head.encode("latin-1") + b"\nstream\n" + body + b"\nendstream"


def pdf_bytes(objects: dict, *, root: int = 1, trailer_extra: str = "",
              header: bytes = b"%PDF-1.4\n", broken_xref: bool = False,
              no_trailer: bool = False) -> bytes:
    """A PDF with a classic cross-reference table.

    ``objects`` maps object number to its body, as bytes or as a string.
    ``broken_xref`` writes offsets that are all wrong, which is how a
    hand-edited or truncated file behaves and which the reader recovers from
    by scanning.
    """
    out = bytearray(header)
    offsets = {}
    for num in sorted(objects):
        body = objects[num]
        if isinstance(body, str):
            body = body.encode("latin-1")
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    start = len(out)
    top = max(objects) + 1
    out += b"xref\n0 %d\n" % top
    out += b"0000000000 65535 f \n"
    for num in range(1, top):
        at = 0 if broken_xref else offsets.get(num, 0)
        kind = b"n" if num in offsets else b"f"
        out += b"%010d 00000 %s \n" % (at, kind)
    if not no_trailer:
        out += (b"trailer\n<< /Size %d /Root %d 0 R %s>>\n"
                % (top, root, trailer_extra.encode("latin-1")))
    out += b"startxref\n%d\n%%%%EOF\n" % start
    return bytes(out)


def pdf_xref_stream(objects: dict, *, root: int = 1,
                    in_object_stream=()) -> bytes:
    """A PDF using a cross-reference *stream*, and optionally object streams.

    ``in_object_stream`` names the objects to pack into an ``/ObjStm`` rather
    than write at top level -- the layout every PDF made this century uses,
    and the one a reader that only knows classic tables cannot open at all.
    """
    packed = {n: objects[n] for n in in_object_stream}
    loose = {n: objects[n] for n in objects if n not in packed}
    out = bytearray(b"%PDF-1.5\n")
    offsets = {}
    entries = {}

    if packed:
        header, body = "", bytearray()
        for num in sorted(packed):
            value = packed[num]
            if isinstance(value, str):
                value = value.encode("latin-1")
            header += "%d %d " % (num, len(body))
            body += value + b"\n"
        stream_num = max(objects) + 1
        first = len(header)
        content = header.encode("latin-1") + bytes(body)
        offsets[stream_num] = len(out)
        # The container is itself an ordinary object and needs its own entry;
        # without one the reader cannot find the stream that holds the rest.
        entries[stream_num] = (1, len(out), 0)
        out += b"%d 0 obj\n" % stream_num + pdf_stream(
            "<< /Type /ObjStm /N %d /First %d >>" % (len(packed), first),
            content, deflate=True) + b"\nendobj\n"
        for index, num in enumerate(sorted(packed)):
            entries[num] = (2, stream_num, index)

    for num in sorted(loose):
        value = loose[num]
        if isinstance(value, str):
            value = value.encode("latin-1")
        offsets[num] = len(out)
        entries[num] = (1, len(out), 0)
        out += b"%d 0 obj\n" % num + value + b"\nendobj\n"

    xref_num = max(entries) + 1
    start = len(out)
    entries[xref_num] = (1, start, 0)
    top = xref_num + 1
    rows = bytearray()
    for num in range(top):
        kind, second, third = entries.get(num, (0, 0, 65535))
        rows += bytes([kind]) + second.to_bytes(4, "big") + third.to_bytes(2, "big")
    out += b"%d 0 obj\n" % xref_num + pdf_stream(
        "<< /Type /XRef /Size %d /W [1 4 2] /Root %d 0 R >>" % (top, root),
        bytes(rows), deflate=True) + b"\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % start
    return bytes(out)


def pdf_page_objects(content: bytes, *, resources: str = "",
                     media: str = "[0 0 612 792]", pages: int = 1) -> dict:
    """The catalogue, page tree, pages and content streams of a small PDF."""
    kids = " ".join("%d 0 R" % (4 + i * 2) for i in range(pages))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Count %d /Kids [%s] /MediaBox %s >>"
           % (pages, kids, media),
    }
    if not resources:
        resources = ("<< /Font << /F1 3 0 R >> >>")
        objects[3] = ("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
                      " /Encoding /WinAnsiEncoding >>")
    for i in range(pages):
        objects[4 + i * 2] = (
            "<< /Type /Page /Parent 2 0 R /Resources %s /Contents %d 0 R >>"
            % (resources, 5 + i * 2))
        objects[5 + i * 2] = pdf_stream("<< >>", content)
    return objects


#: Which of simple_pdf's keywords belong to the file rather than the page.
_FILE_KEYWORDS = ("root", "trailer_extra", "header", "broken_xref",
                  "no_trailer")


def simple_pdf(content: bytes, **kwargs) -> bytes:
    """A one-page PDF whose content stream is ``content``."""
    file_args = {k: kwargs.pop(k) for k in list(kwargs)
                 if k in _FILE_KEYWORDS}
    return pdf_bytes(pdf_page_objects(content, **kwargs), **file_args)


def text_content(*lines, font: str = "F1", size: int = 12,
                 x: int = 72, y: int = 700, leading: int = 16) -> bytes:
    """A content stream drawing one string per line, top down."""
    out = ["BT", "/%s %d Tf" % (font, size), "%d %d Td" % (x, y),
           "%d TL" % leading]
    for i, line in enumerate(lines):
        if i:
            out.append("T*")
        escaped = (line.replace("\\", "\\\\").replace("(", "\\(")
                   .replace(")", "\\)"))
        out.append("(%s) Tj" % escaped)
    out.append("ET")
    return "\n".join(out).encode("latin-1")
