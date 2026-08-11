"""Read the text out of a PDF, and its page images.

A PDF page is a program, not a document: a content stream of operators that
paint glyphs at coordinates.  There are no lines, no paragraphs and no reading
order in the file -- only "put this string at this point in this font".  Text
extraction means running enough of that program to record where each run of
glyphs landed, and then inferring the lines back from the geometry.

Three things decide whether the result is readable:

Encoding
    A string in a content stream is a sequence of *character codes* in some
    font's own encoding, which is frequently not any standard one -- a subset
    font may number its glyphs from 1 in the order they first appear.  The
    font's ``/ToUnicode`` CMap is what maps them back, and without following it
    a subset font yields confident mojibake.  This is the single biggest
    difference between a working extractor and a toy.

Position
    Runs have to be grouped into lines by their baseline, ordered across by
    their x, and given spaces where the gap between two runs is wide enough to
    have been one.  Many producers emit a separate run per word, or per
    kerning pair, with no space characters anywhere in the file.

Scanned pages
    A scan has no text at all.  Those pages carry a single large image, which
    is handed to the image browser instead -- see :func:`page_images`.
"""

from __future__ import annotations

import re

from . import pdfobj, theme
from .filesystems import FileSystem
from .image import Image, ImageError
from .util import truncate
from .pdfobj import (
    Document,
    Lexer,
    Name,
    PdfError,
    Stream,
    String,
    as_list,
)

#: Pages beyond this are not laid out.  A page is only rendered when looked
#: at, but the plumbing walks them all, and a 20,000-page file is a mistake.
MAX_PAGES = 5000
#: Text runs kept per page, to bound a pathological content stream.
MAX_RUNS = 200_000

#: Two runs whose baselines differ by less than this fraction of the font size
#: are on the same line.  Superscripts and accents sit slightly off the line.
LINE_TOLERANCE = 0.35
#: A gap wider than this fraction of the font size becomes a space.
SPACE_GAP = 0.18
#: And wider than this becomes several, so columns stay apart.
WIDE_GAP = 1.2

#: Where WinAnsiEncoding differs from Latin-1: the 0x80-0x9F block, which
#: holds the curly quotes and dashes that real documents are full of.
WINANSI_HIGH = {
    0x80: "€", 0x82: "‚", 0x83: "ƒ", 0x84: "„",
    0x85: "…", 0x86: "†", 0x87: "‡", 0x88: "ˆ",
    0x89: "‰", 0x8A: "Š", 0x8B: "‹", 0x8C: "Œ",
    0x8E: "Ž", 0x91: "‘", 0x92: "’", 0x93: "“",
    0x94: "”", 0x95: "•", 0x96: "–", 0x97: "—",
    0x98: "˜", 0x99: "™", 0x9A: "š", 0x9B: "›",
    0x9C: "œ", 0x9E: "ž", 0x9F: "Ÿ",
}

#: Glyph names that appear in /Differences often enough to be worth naming.
#: Anything else falls back to the "uniXXXX" and "gNN" conventions below.
GLYPH_NAMES = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#",
    "dollar": "$", "percent": "%", "ampersand": "&", "quotesingle": "'",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+",
    "comma": ",", "hyphen": "-", "period": ".", "slash": "/",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=",
    "greater": ">", "question": "?", "at": "@", "bracketleft": "[",
    "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|",
    "braceright": "}", "asciitilde": "~",
    "quoteleft": "‘", "quoteright": "’",
    "quotedblleft": "“", "quotedblright": "”",
    "endash": "–", "emdash": "—", "bullet": "•",
    "fi": "fi", "fl": "fl", "ffi": "ffi", "ffl": "ffl",
    "nbspace": " ", "hyphenminus": "-",
}

_UNI_NAME = re.compile(r"^uni([0-9A-Fa-f]{4,6})$")
_U_NAME = re.compile(r"^u([0-9A-Fa-f]{4,6})$")


def glyph_to_text(name: str) -> str:
    """A PostScript glyph name as the character it stands for."""
    if name in GLYPH_NAMES:
        return GLYPH_NAMES[name]
    if len(name) == 1:
        return name
    match = _UNI_NAME.match(name) or _U_NAME.match(name)
    if match:
        try:
            return chr(int(match.group(1), 16))
        except ValueError:              # a code point outside Unicode
            return ""
    # "g23", "cid42", "index7" and friends name a glyph by number, which says
    # nothing about what character it is. Better an empty string than a guess.
    return ""


class Font:
    """Enough of a font to turn character codes back into text."""

    #: Used when a font declares no width for a code.
    FALLBACK_WIDTH = 500.0

    #: Rough advance widths, in thousandths, for when a font ships no
    #: ``/Widths`` array at all -- which the standard fourteen fonts usually
    #: do not, since a reader is expected to know them already.  A flat 500
    #: for every glyph overshoots "illit" badly enough to swallow the space
    #: after a narrow word, which is exactly the gap this has to measure.
    ESTIMATE = {
        " ": 278, "i": 222, "j": 222, "l": 222, "I": 278, ".": 278, ",": 278,
        ":": 278, ";": 278, "'": 191, "!": 278, "|": 260, "`": 333,
        "f": 278, "t": 278, "r": 333, "s": 500, "c": 500, "z": 500,
        "(": 333, ")": 333, "[": 278, "]": 278, "{": 334, "}": 334,
        "/": 278, "\\": 278, "-": 333, " ": 278,
        "m": 833, "w": 722, "M": 833, "W": 944, "@": 1015, "%": 889,
    }
    ESTIMATE_UPPER = 667
    ESTIMATE_DEFAULT = 556

    def __init__(self, doc: Document, spec: dict) -> None:
        self.two_byte = False
        self.to_unicode: dict[int, str] = {}
        self.differences: dict[int, str] = {}
        self.widths: dict[int, float] = {}
        self.default_width = self.FALLBACK_WIDTH
        # Whether the font declared any widths of its own. When it did not,
        # a per-character estimate beats one flat number.
        self.declared = False
        self.winansi = False
        self._read(doc, spec)

    def _read(self, doc: Document, spec: dict) -> None:
        subtype = str(doc.get(spec, "Subtype", ""))
        if subtype == "Type0":
            # A composite font. Identity-H and its relatives -- which is to
            # say very nearly all of them -- use two-byte codes.
            self.two_byte = True
            self._read_cid_widths(doc, spec)
        else:
            self._read_simple_widths(doc, spec)
        encoding = doc.get(spec, "Encoding")
        if isinstance(encoding, Name) or isinstance(encoding, str):
            self.winansi = str(encoding) == "WinAnsiEncoding"
        elif isinstance(encoding, dict):
            self.winansi = str(doc.get(encoding, "BaseEncoding", "")) == \
                "WinAnsiEncoding"
            self._read_differences(doc.get(encoding, "Differences"))
        stream = doc.get(spec, "ToUnicode")
        if isinstance(stream, Stream):
            try:
                self.to_unicode = parse_cmap(doc.decode_stream(stream))
            except PdfError:
                self.to_unicode = {}

    def _read_simple_widths(self, doc: Document, spec: dict) -> None:
        """``/FirstChar`` and ``/Widths``: one entry per code, in order."""
        widths = doc.get(spec, "Widths")
        first = doc.get(spec, "FirstChar", 0)
        if isinstance(widths, list) and isinstance(first, (int, float)):
            for i, value in enumerate(widths):
                value = doc.resolve(value)
                if isinstance(value, (int, float)):
                    self.widths[int(first) + i] = float(value)
        self.declared = bool(self.widths)
        descriptor = doc.get(spec, "FontDescriptor")
        missing = doc.get(descriptor, "MissingWidth")
        if isinstance(missing, (int, float)):
            self.default_width = float(missing)

    def _read_cid_widths(self, doc: Document, spec: dict) -> None:
        """``/W``: runs of codes, written either as a list or as a range.

        The two forms are ``c [w1 w2 ...]`` and ``cfirst clast w``, freely
        mixed in one array.
        """
        descendants = doc.get(spec, "DescendantFonts")
        child = doc.resolve(descendants[0]) if isinstance(descendants, list) \
            and descendants else None
        if not isinstance(child, dict):
            return
        default = doc.get(child, "DW")
        self.default_width = float(default) if isinstance(
            default, (int, float)) else 1000.0
        self.declared = True
        table = doc.get(child, "W")
        if not isinstance(table, list):
            return
        at = 0
        while at < len(table):
            start = doc.resolve(table[at])
            if not isinstance(start, (int, float)) or at + 1 >= len(table):
                break
            following = doc.resolve(table[at + 1])
            if isinstance(following, list):
                for i, value in enumerate(following):
                    value = doc.resolve(value)
                    if isinstance(value, (int, float)):
                        self.widths[int(start) + i] = float(value)
                at += 2
            elif at + 2 < len(table):
                last = following
                value = doc.resolve(table[at + 2])
                if isinstance(last, (int, float)) and isinstance(
                        value, (int, float)) and last >= start \
                        and last - start <= 65535:
                    for code in range(int(start), int(last) + 1):
                        self.widths[code] = float(value)
                at += 3
            else:
                break

    def width(self, raw: bytes, state) -> float:
        """How far the pen moves for ``raw``, in unscaled text space."""
        total = 0.0
        for code in self.codes(raw):
            glyph = self._glyph_width(code) / 1000.0
            total += glyph * state.size + state.char_space
            # Word spacing applies to the single byte 32, and to that byte
            # only -- never to a two-byte code that happens to equal it.
            if code == 32 and not self.two_byte:
                total += state.word_space
        return total

    def _glyph_width(self, code: int) -> float:
        """One glyph's advance, in thousandths of an em."""
        if code in self.widths:
            return self.widths[code]
        if self.declared or self.two_byte:
            return self.default_width
        char = chr(code) if code < 0x110000 else ""
        if char in self.ESTIMATE:
            return float(self.ESTIMATE[char])
        if char.isupper():
            return float(self.ESTIMATE_UPPER)
        return float(self.ESTIMATE_DEFAULT)

    def _read_differences(self, differences) -> None:
        if not isinstance(differences, list):
            return
        code = 0
        for item in differences:
            if isinstance(item, (int, float)):
                code = int(item)
            elif isinstance(item, str):
                self.differences[code] = str(item)
                code += 1

    def codes(self, raw: bytes):
        """Split a string into character codes, one or two bytes each."""
        if self.two_byte:
            if len(raw) % 2:
                raw += b"\x00"
            return [raw[i] << 8 | raw[i + 1] for i in range(0, len(raw), 2)]
        return list(raw)

    def decode(self, raw: bytes) -> str:
        out = []
        for code in self.codes(raw):
            if code in self.to_unicode:
                out.append(self.to_unicode[code])
            elif code in self.differences:
                out.append(glyph_to_text(self.differences[code]))
            elif self.two_byte:
                # No ToUnicode on a composite font: the codes are glyph
                # numbers and say nothing about characters. Emitting the
                # numbers as characters would be worse than emitting nothing.
                out.append("")
            elif self.winansi and code in WINANSI_HIGH:
                out.append(WINANSI_HIGH[code])
            else:
                out.append(chr(code))
        return "".join(out)


def parse_cmap(data: bytes) -> dict[int, str]:
    """The code-to-text mapping out of a ``/ToUnicode`` CMap."""
    mapping: dict[int, str] = {}
    lexer = Lexer(data)
    pending: list = []
    mode = None
    while True:
        token = lexer.token()
        if token is None:
            break
        if isinstance(token, bytes):
            word = token.decode("latin-1", "replace")
            if word.endswith("beginbfchar"):
                mode, pending = "char", []
                continue
            if word.endswith("beginbfrange"):
                mode, pending = "range", []
                continue
            if word.startswith("end"):
                mode, pending = None, []
                continue
        if mode is None:
            continue
        if isinstance(token, bytes) and token == b"[":
            pending.append(_cmap_array(lexer))
        else:
            pending.append(token)
        if mode == "char" and len(pending) == 2:
            source, target = pending
            pending = []
            # String, not bytes: a bare number in a malformed CMap also
            # arrives as bytes, and reading it as a hex code would invent a
            # mapping out of nothing.
            if isinstance(source, String) and isinstance(target, String):
                mapping[int.from_bytes(source, "big")] = _utf16(target)
        elif mode == "range" and len(pending) == 3:
            low, high, target = pending
            pending = []
            if not (isinstance(low, String) and isinstance(high, String)):
                continue
            start, end = int.from_bytes(low, "big"), int.from_bytes(high, "big")
            if end < start or end - start > 65535:
                continue
            if isinstance(target, list):
                for i, item in enumerate(target):
                    if isinstance(item, String) and start + i <= end:
                        mapping[start + i] = _utf16(item)
            elif isinstance(target, String):
                base = _utf16(target)
                for code in range(start, end + 1):
                    mapping[code] = _shift(base, code - start)
    return mapping


def _cmap_array(lexer: Lexer) -> list:
    out = []
    while True:
        token = lexer.token()
        if token is None or (isinstance(token, bytes) and token == b"]"):
            return out
        out.append(token)


def _utf16(raw: bytes) -> str:
    """A CMap target: big-endian UTF-16, possibly several characters.

    Odd lengths are padded rather than refused.  A hex target is always an
    even number of bytes, but a CMap may legally write its target as a
    literal string instead, and those are any length at all.
    """
    if len(raw) % 2:
        raw += b"\x00"
    # "replace" rather than a try/except: it cannot raise, so an except
    # clause here would be a branch no test could ever reach.
    return raw.decode("utf-16-be", "replace").rstrip("\x00")


def _shift(base: str, by: int) -> str:
    """The next character along, for a bfrange with a single target."""
    if not base:
        return ""
    return base[:-1] + chr(min(0x10FFFF, ord(base[-1]) + by))


# -- text placement ------------------------------------------------------------

def multiply(a, b):
    """Compose two PDF matrices, given as ``(a, b, c, d, e, f)``."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4],
            a[4] * b[1] + a[5] * b[3] + b[5])


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _number(value, default=0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


class Run:
    """One run of glyphs, with where it started and how wide it is."""

    __slots__ = ("x", "y", "text", "size", "width")

    def __init__(self, x, y, text, size, width) -> None:
        self.x = x
        self.y = y
        self.text = text
        self.size = size
        self.width = width

    @property
    def end(self) -> float:
        return self.x + self.width


class _TextState:
    """The subset of the graphics state that decides where glyphs land."""

    def __init__(self) -> None:
        self.matrix = IDENTITY          # text matrix
        self.line = IDENTITY            # text line matrix
        self.leading = 0.0
        self.size = 0.0
        self.char_space = 0.0
        self.word_space = 0.0
        self.scale = 1.0                # horizontal scaling, Tz, as a fraction
        self.rise = 0.0
        self.font: Font | None = None


def extract_runs(doc: Document, page: dict, fonts: dict) -> list[Run]:
    """Run a page's content stream far enough to place every run of text."""
    data = page_content(doc, page)
    lexer = Lexer(data)
    runs: list[Run] = []
    stack: list = []
    ctm = IDENTITY
    saved: list = []
    state = _TextState()
    while len(runs) < MAX_RUNS:
        mark = lexer.at
        token = lexer.token()
        if token is None:
            break
        if not _is_operator(token):
            lexer.at = mark
            stack.append(lexer.object())
            if len(stack) > 64:
                del stack[:-32]         # a malformed stream, not an operator
            continue
        operator = token.decode("latin-1", "replace")
        if operator == "q":
            saved.append(ctm)
        elif operator == "Q":
            ctm = saved.pop() if saved else ctm
        elif operator == "cm" and len(stack) >= 6:
            ctm = multiply(tuple(_number(v) for v in stack[-6:]), ctm)
        elif operator == "BT":
            state.matrix = state.line = IDENTITY
        elif operator == "Tf" and len(stack) >= 2:
            state.font = fonts.get(str(stack[-2]))
            state.size = _number(stack[-1])
        elif operator == "TL" and stack:
            state.leading = _number(stack[-1])
        elif operator == "Tc" and stack:
            state.char_space = _number(stack[-1])
        elif operator == "Tw" and stack:
            state.word_space = _number(stack[-1])
        elif operator == "Tz" and stack:
            state.scale = _number(stack[-1], 100.0) / 100.0
        elif operator == "Ts" and stack:
            state.rise = _number(stack[-1])
        elif operator == "Tm" and len(stack) >= 6:
            state.matrix = state.line = tuple(_number(v) for v in stack[-6:])
        elif operator in ("Td", "TD") and len(stack) >= 2:
            tx, ty = _number(stack[-2]), _number(stack[-1])
            if operator == "TD":
                state.leading = -ty
            state.line = multiply((1, 0, 0, 1, tx, ty), state.line)
            state.matrix = state.line
        elif operator == "T*":
            state.line = multiply((1, 0, 0, 1, 0, -state.leading), state.line)
            state.matrix = state.line
        elif operator in ("Tj", "'", '"') and stack:
            if operator != "Tj":
                if operator == '"' and len(stack) >= 3:
                    state.word_space = _number(stack[-3])
                    state.char_space = _number(stack[-2])
                state.line = multiply((1, 0, 0, 1, 0, -state.leading),
                                      state.line)
                state.matrix = state.line
            _show(runs, state, ctm, [stack[-1]])
        elif operator == "TJ" and stack and isinstance(stack[-1], list):
            _show(runs, state, ctm, stack[-1])
        stack = []
    return runs


def _is_operator(token) -> bool:
    """Whether a token is an operator rather than an operand.

    A string literal arrives as bytes like a keyword does, which is why the
    lexer gives strings their own type: without it ``(Tj)`` -- a perfectly
    ordinary piece of text -- reads as the Tj operator.
    """
    if type(token) is not bytes:
        return False
    return not pdfobj.is_number(token) and token not in (b"[", b"<<", b"]",
                                                          b">>")


def _show(runs: list, state: _TextState, ctm, items) -> None:
    """Place one string (or a TJ array) and advance the text matrix."""
    if state.font is None:
        return
    for item in items:
        if isinstance(item, (int, float)):
            # A TJ number moves the pen back, in thousandths of an em.
            shift = -float(item) / 1000.0 * state.size * state.scale
            state.matrix = multiply((1, 0, 0, 1, shift, 0), state.matrix)
            continue
        if not isinstance(item, bytes):
            continue
        text = state.font.decode(item)
        placed = multiply(state.matrix, ctm)
        width = state.font.width(item, state) * state.scale
        if text.strip():
            size = abs(state.size * placed[3]) or abs(state.size) or 1.0
            runs.append(Run(placed[4], placed[5], text, size,
                            width * abs(placed[0] or 1.0)))
        state.matrix = multiply((1, 0, 0, 1, width, 0), state.matrix)


def page_content(doc: Document, page: dict) -> bytes:
    """A page's content streams, joined.  A page may be split across several."""
    parts = []
    for entry in as_list(doc.get(page, "Contents")):
        stream = doc.resolve(entry)
        if isinstance(stream, Stream):
            try:
                parts.append(doc.decode_stream(stream))
            except PdfError:
                continue
    # Joined with a newline: a content stream may legally end mid-token, and
    # the next one starting flush against it would fuse two operators.
    return b"\n".join(parts)


def page_fonts(doc: Document, page: dict) -> dict:
    """The page's fonts, by the resource name the content stream uses."""
    resources = doc.get(page, "Resources", {})
    table = doc.get(resources, "Font", {})
    out = {}
    if isinstance(table, dict):
        for name, spec in table.items():
            resolved = doc.resolve(spec)
            if isinstance(resolved, dict):
                out[str(name)] = Font(doc, resolved)
    return out


# -- turning placed runs back into lines ---------------------------------------

def lines_from_runs(runs: list[Run]) -> list[str]:
    """Group runs into lines by baseline, order them across, and space them.

    Many producers emit one run per word -- or per kerning pair -- with no
    space characters anywhere in the file, so the spaces have to be inferred
    from the gaps.
    """
    if not runs:
        return []
    ordered = sorted(runs, key=lambda r: (-r.y, r.x))
    lines: list[list[Run]] = []
    current = [ordered[0]]
    for run in ordered[1:]:
        reference = current[-1]
        tolerance = max(1.0, reference.size * LINE_TOLERANCE)
        if abs(run.y - reference.y) <= tolerance:
            current.append(run)
        else:
            lines.append(current)
            current = [run]
    lines.append(current)

    out = []
    for line in lines:
        line.sort(key=lambda r: r.x)
        text = ""
        previous = None
        for run in line:
            if previous is not None:
                gap = run.x - previous.end
                size = max(previous.size, 1.0)
                if gap > size * WIDE_GAP:
                    text += "   "
                elif gap > size * SPACE_GAP and not text.endswith(" ") \
                        and not run.text.startswith(" "):
                    text += " "
            text += run.text
            previous = run
        out.append(text.rstrip())
    return out


# -- page images ---------------------------------------------------------------

#: Colour spaces a raw (unfiltered) image sample array can be read in.
RAW_SPACES = {"DeviceGray": 1, "CalGray": 1, "G": 1,
              "DeviceRGB": 3, "CalRGB": 3, "RGB": 3}


def page_images(doc: Document, page: dict) -> list:
    """Every image drawn on a page, as ``(name, Image)`` or ``(name, reason)``.

    This is what makes a scanned page viewable: it has no text at all, only
    one large image, which the image browser can draw.
    """
    resources = doc.get(page, "Resources", {})
    table = doc.get(resources, "XObject", {})
    out = []
    if not isinstance(table, dict):
        return out
    for name in sorted(table):
        stream = doc.resolve(table[name])
        if not isinstance(stream, Stream):
            continue
        if str(doc.get(stream.dict, "Subtype", "")) != "Image":
            continue
        try:
            out.append((str(name), decode_image(doc, stream)))
        except (PdfError, ImageError) as exc:
            out.append((str(name), str(exc)))
    return out


def decode_image(doc: Document, stream: Stream) -> Image:
    """One image XObject, as pixels."""
    from .image import Frame, decode

    codec = doc.image_filter(stream)
    if codec in ("DCTDecode", "DCT"):
        # A DCTDecode stream is a JPEG file, byte for byte -- so it goes
        # through the decoder this project already has.
        return decode(doc.decode_stream(stream))
    if codec is not None:
        raise PdfError(f"image uses {codec}, which has no decoder here")

    width = int(doc.get(stream.dict, "Width", 0) or 0)
    height = int(doc.get(stream.dict, "Height", 0) or 0)
    depth = int(doc.get(stream.dict, "BitsPerComponent", 8) or 8)
    space = doc.get(stream.dict, "ColorSpace", "")
    if isinstance(space, list) and space:
        space = doc.resolve(space[0])
    name = str(space)
    if bool(doc.get(stream.dict, "ImageMask", False)):
        name, depth = "DeviceGray", 1
    if name not in RAW_SPACES:
        raise PdfError(f"image colour space {name or 'unknown'} is not handled")
    if depth not in (1, 2, 4, 8):
        raise PdfError(f"image at {depth} bits per component is not handled")
    channels = RAW_SPACES[name]
    if width <= 0 or height <= 0:
        raise PdfError("image has no size")

    data = doc.decode_stream(stream)
    stride = (width * channels * depth + 7) // 8
    if len(data) < stride * height:
        raise PdfError("image is short of sample data")
    top = (1 << depth) - 1
    pixels = bytearray(width * height * 3)
    per_byte = 8 // depth
    mask = top
    at = 0
    for y in range(height):
        row = data[y * stride:(y + 1) * stride]
        for x in range(width):
            values = []
            for c in range(channels):
                index = x * channels + c
                if depth == 8:
                    values.append(row[index])
                else:
                    shift = 8 - depth * (index % per_byte + 1)
                    values.append(((row[index // per_byte] >> shift) & mask)
                                  * 255 // top)
            if channels == 1:
                values = values * 3
            pixels[at:at + 3] = bytes(values)
            at += 3
    detail = f"{depth}-bit {name}"
    return Image(width, height, "PDF image", detail, [Frame(pixels)])


# -- the browser ---------------------------------------------------------------

def read_document(fs: FileSystem, path: str) -> Document:
    """Fetch and parse a PDF through any backend."""
    with fs.open_read(path) as reader:
        data = reader.read(pdfobj.MAX_BYTES + 1)
    if len(data) > pdfobj.MAX_BYTES:
        raise PdfError(
            f"PDF is larger than {pdfobj.MAX_BYTES // (1024 * 1024)} MB")
    return Document(data)


class PdfView:
    """A full-screen PDF browser: one page at a time, with its images."""

    def __init__(self, fs: FileSystem, path: str) -> None:
        self.fs = fs
        self.path = path
        self.name = fs.basename(path)
        self.error: str | None = None
        self.doc: Document | None = None
        self.pages: list[dict] = []
        self.index = 0
        self.top = 0
        self.search = ""
        self.notice = ""
        self.truncated = False
        self._lines: dict[int, list[str]] = {}
        self._images: dict[int, list] = {}
        self._load()

    def _load(self) -> None:
        try:
            self.doc = read_document(self.fs, self.path)
            self.pages = self.doc.pages()
            if len(self.pages) > MAX_PAGES:
                self.pages = self.pages[:MAX_PAGES]
                self.truncated = True
            if not self.pages:
                self.error = "this PDF has no pages"
        except PdfError as exc:
            self.error = str(exc)
        except Exception as exc:            # the backend failed, or the file went
            self.error = str(exc)

    # -- content ------------------------------------------------------------
    def lines(self, index: int | None = None) -> list[str]:
        """The text of a page, laid out.  Extracted once and kept."""
        index = self.index if index is None else index
        if index in self._lines:
            return self._lines[index]
        if self.doc is None or not 0 <= index < len(self.pages):
            return []
        page = self.pages[index]
        try:
            runs = extract_runs(self.doc, page, page_fonts(self.doc, page))
            found = lines_from_runs(runs)
        except (PdfError, ValueError, TypeError, KeyError) as exc:
            found = [f"(this page could not be laid out: {exc})"]
        self._lines[index] = found
        return found

    def images(self, index: int | None = None) -> list:
        index = self.index if index is None else index
        if index not in self._images:
            if self.doc is None or not 0 <= index < len(self.pages):
                return []
            self._images[index] = page_images(self.doc, self.pages[index])
        return self._images[index]

    def body(self) -> list[str]:
        """What to show for the current page: its text, or what is there instead."""
        lines = self.lines()
        if lines:
            return lines
        pictures = self.images()
        if not pictures:
            return ["(this page is empty)"]
        out = ["(no text on this page -- it is a picture)", ""]
        for name, item in pictures:
            if isinstance(item, str):
                out.append(f"  {name}: {item}")
            else:
                width, height = item.size
                out.append(f"  {name}: {item.kind} {width}x{height}"
                           f" -- press i to view")
        return out

    def viewable(self):
        """The first image on this page that can actually be drawn."""
        for name, item in self.images():
            if not isinstance(item, str):
                return name, item
        return None

    # -- movement -----------------------------------------------------------
    def go_to(self, index: int) -> None:
        self.index = max(0, min(index, len(self.pages) - 1))
        self.top = 0

    def scroll(self, delta: int, body_h: int) -> None:
        self.top = max(0, min(self.top + delta,
                              max(0, len(self.body()) - body_h)))

    def find_next(self, direction: int = 1) -> bool:
        """Search every page's text, wrapping around."""
        if not self.search or not self.pages:
            return False
        needle = self.search
        fold = needle == needle.lower()
        total = len(self.pages)
        for step in range(total + 1):
            index = (self.index + step * direction) % total
            for row, line in enumerate(self.lines(index)):
                hay = line.lower() if fold else line
                if needle in hay and (index != self.index or step
                                      or row > self.top):
                    self.index = index
                    self.top = row
                    return True
        return False

    # -- drawing ------------------------------------------------------------
    def draw(self, win) -> None:
        import curses

        theme.background(win, "edit")
        win.erase()
        height, width = win.getmaxyx()
        body_h = max(0, height - 2)
        self._draw_title(win, width)
        if self.error:
            theme.paint(win, 1, 2, truncate(f"Cannot read: {self.error}",
                                            max(0, width - 4)), "panelerror")
        else:
            lines = self.body()
            self.top = max(0, min(self.top, max(0, len(lines) - 1)))
            for row in range(body_h):
                at = self.top + row
                if at >= len(lines):
                    break
                try:
                    win.addstr(row + 1, 0, truncate(lines[at], width))
                except curses.error:
                    pass
        self._draw_footer(win, height, width)
        win.noutrefresh()

    def _draw_title(self, win, width: int) -> None:
        title = f" PDF: {self.name}"
        if not self.error:
            title += f" -- page {self.index + 1}/{len(self.pages)}"
            if self.truncated:
                title += " [truncated]"
        title += " "
        theme.paint(win, 0, 0, title, "keybar", width)

    def _draw_footer(self, win, height: int, width: int) -> None:
        if self.notice:
            hint = self.notice
        elif self.error:
            hint = "[q]uit"
        else:
            parts = []
            if self.search:
                parts.append(f"/{self.search}")
            if self.viewable():
                parts.append("[i]mage")
            parts.append("Tab page  [/]find  n/N  [q]uit")
            hint = "  ".join(parts)
        theme.paint(win, height - 1, 0, f" {hint} ", "keybar", width)

    # -- interaction --------------------------------------------------------
    def show_image(self, stdscr) -> None:
        """Open this page's picture in the image browser."""
        from .imageview import ImageView

        found = self.viewable()
        if found is None:
            self.notice = " no picture on this page "
            return
        name, item = found
        ImageView(self.fs, self.path, image=item,
                  name=f"{self.name} page {self.index + 1}").run(stdscr)

    def run(self, stdscr) -> None:
        import curses

        from . import dialogs

        curses.curs_set(0)
        height, width = stdscr.getmaxyx()
        win = curses.newwin(height, width, 0, 0)
        win.keypad(True)
        while True:
            self.draw(win)
            curses.doupdate()
            height, width = stdscr.getmaxyx()
            body_h = max(1, height - 2)
            key = win.getch()
            self.notice = ""
            if key in (ord("q"), ord("Q"), 27, curses.KEY_F3, curses.KEY_F10):
                break
            elif key in (curses.KEY_DOWN, ord("j")):
                self.scroll(1, body_h)
            elif key in (curses.KEY_UP, ord("k")):
                self.scroll(-1, body_h)
            elif key == curses.KEY_NPAGE:
                self.scroll(body_h, body_h)
            elif key == curses.KEY_PPAGE:
                self.scroll(-body_h, body_h)
            elif key == curses.KEY_HOME:
                self.top = 0
            elif key == curses.KEY_END:
                self.scroll(len(self.body()), body_h)
            elif key in (9, ord("]"), curses.KEY_RIGHT):        # 9 = Tab
                self.go_to(self.index + 1)
            elif key in (curses.KEY_BTAB, ord("["), curses.KEY_LEFT):
                self.go_to(self.index - 1)
            elif key in (ord("i"), ord("I")):
                self.show_image(stdscr)
                curses.curs_set(0)
            elif key in (ord("/"), curses.KEY_F7):
                pattern = dialogs.prompt(stdscr, "Search",
                                         "Find in document (smart case):",
                                         default=self.search)
                curses.curs_set(0)
                if pattern is not None:
                    self.search = pattern or ""
                    if pattern and not self.find_next(1):
                        self.notice = f" '{pattern}' not found "
            elif key == ord("n"):
                if self.search and not self.find_next(1):
                    self.notice = " no match "
            elif key in (ord("N"), ord("p")):
                if self.search and not self.find_next(-1):
                    self.notice = " no match "


def is_pdf(name: str) -> bool:
    return name.lower().endswith(".pdf")
