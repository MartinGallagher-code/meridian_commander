"""The PDF object layer: lexer, object graph, cross-references and filters.

A PDF is not a document format so much as a small object database with a
page-description language stored inside it.  This module deals with the
database half -- tokenising, resolving indirect references, following the
cross-reference tables to find where an object lives, and decoding the stream
filters -- so :mod:`meridian_commander.pdf` can deal only with the pages.

Everything decompresses with the standard library.  ``FlateDecode`` is zlib,
``ASCII85Decode`` is :func:`base64.a85decode`, and the rest are short enough
to write out.  The image codecs (``DCTDecode``, ``JPXDecode``,
``CCITTFaxDecode``, ``JBIG2Decode``) are left encoded and handed on: a
``DCTDecode`` stream is a JPEG file, which this project already has a decoder
for.

Two things here are worth knowing about, because a reader that skips them
fails on most files made this century:

``/ObjStm``
    Since PDF 1.5 most indirect objects are not in the file at top level.
    They are packed together, uncompressed, inside *object streams*, which are
    themselves compressed.  Without following those, a modern PDF looks almost
    empty.

Cross-reference streams
    The classic ``xref`` table is likewise often replaced by a compressed
    binary stream, which is where the pointers into those object streams live.
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib

MAX_BYTES = 64 * 1024 * 1024
#: A stream that decompresses past this is refused rather than held in memory.
MAX_STREAM_BYTES = 128 * 1024 * 1024
#: How deep an indirect reference will be chased before calling it a loop.
MAX_INDIRECTION = 32

#: Filters left encoded, because their payload is an image rather than text.
IMAGE_FILTERS = frozenset([
    "DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode",
])

_WHITESPACE = b"\x00\t\n\x0c\r "
_DELIMITERS = b"()<>[]{}/%"
_NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)$")


class PdfError(Exception):
    """A PDF that cannot be read, with a reason fit to show the user."""


class Name(str):
    """A PDF name (``/Type``).  A ``str`` subclass so it compares like one.

    Distinct from ``str`` only so a name and a string literal with the same
    characters can be told apart, which the content-stream parser needs: an
    operand of ``/F1`` is a font name, ``(F1)`` is text to be drawn.
    """

    __slots__ = ()


class String(bytes):
    """A PDF string literal, ``(like this)`` or ``<4C696B65>``.

    A ``bytes`` subclass purely so it can be told apart from a bare keyword
    token, which the lexer also hands back as bytes.  Without the distinction
    ``(Tj)`` in a content stream reads as the Tj operator, and a page of text
    extracts as nothing at all.
    """

    __slots__ = ()


def is_keyword(token, word: bytes) -> bool:
    """True when ``token`` is the bare keyword ``word`` and not a string."""
    return type(token) is bytes and token == word


class Ref:
    """An indirect reference: object number and generation."""

    __slots__ = ("num", "gen")

    def __init__(self, num: int, gen: int) -> None:
        self.num = num
        self.gen = gen

    def __eq__(self, other) -> bool:
        return (isinstance(other, Ref) and other.num == self.num
                and other.gen == self.gen)

    def __hash__(self) -> int:
        return hash((self.num, self.gen))

    def __repr__(self) -> str:
        return f"Ref({self.num}, {self.gen})"


class Stream:
    """A stream object: its dictionary, and its bytes still encoded."""

    __slots__ = ("dict", "raw", "_decoded")

    def __init__(self, dictionary: dict, raw: bytes) -> None:
        self.dict = dictionary
        self.raw = raw
        self._decoded: bytes | None = None


def is_number(token: bytes) -> bool:
    return bool(_NUMBER.match(token))


class Lexer:
    """Tokenises PDF syntax.  Shared by the file body and content streams."""

    def __init__(self, data: bytes, at: int = 0) -> None:
        self.data = data
        self.at = at

    def skip_space(self) -> None:
        data = self.data
        while self.at < len(data):
            char = data[self.at]
            if char in _WHITESPACE:
                self.at += 1
            elif char == 0x25:                      # "%" comment to end of line
                while self.at < len(data) and data[self.at] not in b"\r\n":
                    self.at += 1
            else:
                return

    def token(self):
        """The next token: bytes for bare words, or a parsed object."""
        self.skip_space()
        data = self.data
        if self.at >= len(data):
            return None
        char = data[self.at]
        if char == 0x2F:                            # "/" name
            return self._name()
        if char == 0x28:                            # "(" literal string
            return self._literal_string()
        if char == 0x3C:                            # "<" dict or hex string
            if data[self.at + 1:self.at + 2] == b"<":
                self.at += 2
                return b"<<"
            return self._hex_string()
        if char == 0x3E and data[self.at + 1:self.at + 2] == b">":
            self.at += 2
            return b">>"
        if char in b"[]{}":
            self.at += 1
            return bytes([char])
        start = self.at
        while self.at < len(data) and data[self.at] not in _WHITESPACE \
                and data[self.at] not in _DELIMITERS:
            self.at += 1
        if start == self.at:                        # a stray delimiter
            self.at += 1
            return bytes([char])
        return data[start:self.at]

    def _name(self) -> Name:
        self.at += 1
        data = self.data
        out = bytearray()
        while self.at < len(data):
            char = data[self.at]
            if char in _WHITESPACE or char in _DELIMITERS:
                break
            if char == 0x23 and self.at + 2 < len(data):    # "#" hex escape
                try:
                    out.append(int(data[self.at + 1:self.at + 3], 16))
                    self.at += 3
                    continue
                except ValueError:
                    pass
            out.append(char)
            self.at += 1
        return Name(out.decode("latin-1"))

    def _literal_string(self) -> bytes:
        """A ``(...)`` string, with nesting, escapes and octal codes."""
        self.at += 1
        data = self.data
        out = bytearray()
        depth = 1
        while self.at < len(data):
            char = data[self.at]
            self.at += 1
            if char == 0x5C:                        # backslash
                if self.at >= len(data):
                    break
                nxt = data[self.at]
                self.at += 1
                if nxt in b"nrtbf":
                    out.append({0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8,
                                0x66: 12}[nxt])
                elif 0x30 <= nxt <= 0x37:           # octal, up to three digits
                    digits = chr(nxt)
                    while len(digits) < 3 and self.at < len(data) \
                            and 0x30 <= data[self.at] <= 0x37:
                        digits += chr(data[self.at])
                        self.at += 1
                    out.append(int(digits, 8) & 0xFF)
                elif nxt in b"\r\n":
                    # A backslash before a newline continues the line, and
                    # contributes nothing to the string.
                    if nxt == 0x0D and data[self.at:self.at + 1] == b"\n":
                        self.at += 1
                else:
                    out.append(nxt)
            elif char == 0x28:
                depth += 1
                out.append(char)
            elif char == 0x29:
                depth -= 1
                if depth == 0:
                    break
                out.append(char)
            else:
                out.append(char)
        return String(out)

    def _hex_string(self) -> bytes:
        self.at += 1
        data = self.data
        digits = []
        while self.at < len(data) and data[self.at] != 0x3E:
            char = data[self.at]
            if char not in _WHITESPACE:
                digits.append(chr(char))
            self.at += 1
        self.at += 1                                # the closing ">"
        text = "".join(digits)
        if len(text) % 2:
            text += "0"                             # an odd digit pads with zero
        try:
            return String(bytes.fromhex(text))
        except ValueError:
            raise PdfError("hex string contains a non-hex digit") from None

    def object(self, depth: int = 0):
        """The next complete object, following arrays and dictionaries."""
        return self._from(self.token(), depth)

    def _from(self, token, depth: int):
        if depth > MAX_INDIRECTION:
            raise PdfError("PDF object nesting is too deep")
        if token is None or isinstance(token, (Name, str)):
            return token
        if isinstance(token, String):
            return token
        if isinstance(token, bytes) and token == b"[":
            out = []
            while True:
                nxt = self.token()
                if nxt is None or (is_keyword(nxt, b"]")):
                    return out
                out.append(self._from(nxt, depth + 1))
        if isinstance(token, bytes) and token == b"<<":
            return self._dictionary(depth)
        if isinstance(token, bytes):
            if token == b"true":
                return True
            if token == b"false":
                return False
            if token == b"null":
                return None
            if is_number(token):
                return self._number_or_ref(token, depth)
        return token

    def _number_or_ref(self, token: bytes, depth: int):
        """A number -- unless two more tokens make it ``n g R``."""
        text = token.decode("latin-1")
        if b"." in token:
            return float(text)
        mark = self.at
        second = self.token()
        if type(second) is bytes and is_number(second) and b"." not in second:
            third = self.token()
            if is_keyword(third, b"R"):
                return Ref(int(text), int(second))
        self.at = mark
        return int(text)

    def _dictionary(self, depth: int) -> dict:
        out = {}
        while True:
            key = self.token()
            if key is None or (is_keyword(key, b">>")):
                return out
            if not isinstance(key, Name):
                # A malformed dictionary: skip the token rather than stopping,
                # since the rest of it is usually still readable.
                continue
            out[str(key)] = self.object(depth + 1)


# -- stream filters ------------------------------------------------------------

def apply_predictor(data: bytes, predictor: int, colors: int, bpc: int,
                    columns: int) -> bytes:
    """Undo a PNG or TIFF predictor, which Flate streams often carry.

    Cross-reference streams in particular almost always use PNG "up"
    prediction, so this is not an exotic path.
    """
    if predictor < 2:
        return data
    per_pixel = max(1, colors * bpc // 8)
    stride = (columns * colors * bpc + 7) // 8
    if predictor == 2:
        if bpc != 8:
            raise PdfError(f"TIFF prediction at {bpc} bits is not handled")
        out = bytearray(data)
        for row in range(len(out) // stride):
            base = row * stride
            for i in range(per_pixel, stride):
                out[base + i] = (out[base + i] + out[base + i - per_pixel]) & 0xFF
        return bytes(out)

    # PNG predictors: one filter-type byte in front of every row.
    out = bytearray()
    previous = bytearray(stride)
    at = 0
    while at + 1 <= len(data) - 1:
        kind = data[at]
        row = bytearray(data[at + 1:at + 1 + stride])
        at += 1 + stride
        if len(row) < stride:
            row.extend(bytes(stride - len(row)))
        for i in range(stride):
            left = row[i - per_pixel] if i >= per_pixel else 0
            up = previous[i]
            if kind == 0:
                pass
            elif kind == 1:
                row[i] = (row[i] + left) & 0xFF
            elif kind == 2:
                row[i] = (row[i] + up) & 0xFF
            elif kind == 3:
                row[i] = (row[i] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                upleft = previous[i - per_pixel] if i >= per_pixel else 0
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                if pa <= pb and pa <= pc:
                    row[i] = (row[i] + left) & 0xFF
                elif pb <= pc:
                    row[i] = (row[i] + up) & 0xFF
                else:
                    row[i] = (row[i] + upleft) & 0xFF
            else:
                raise PdfError(f"PNG predictor row type {kind} is not defined")
        out += row
        previous = row
    return bytes(out)


def ascii_hex_decode(data: bytes) -> bytes:
    text = re.sub(rb"\s", b"", data.split(b">")[0])
    if len(text) % 2:
        text += b"0"
    try:
        return bytes.fromhex(text.decode("latin-1"))
    except ValueError:
        raise PdfError("ASCIIHexDecode stream has a non-hex digit") from None


def ascii85_decode(data: bytes) -> bytes:
    text = re.sub(rb"\s", b"", data)
    if text.startswith(b"<~"):
        text = text[2:]
    end = text.find(b"~>")
    if end >= 0:
        text = text[:end]
    try:
        return base64.a85decode(text)
    except (ValueError, binascii.Error) as exc:
        raise PdfError(f"ASCII85Decode stream is damaged: {exc}") from None


def run_length_decode(data: bytes) -> bytes:
    out = bytearray()
    at = 0
    while at < len(data):
        length = data[at]
        at += 1
        if length == 128:                   # end of data
            break
        if length < 128:
            out += data[at:at + length + 1]
            at += length + 1
        else:
            if at >= len(data):
                break
            out += bytes([data[at]]) * (257 - length)
            at += 1
    return bytes(out)


def lzw_decode(data: bytes, early: int = 1) -> bytes:
    """PDF's LZW, which is GIF's read the other way up: codes are MSB-first.

    ``early`` is the ``EarlyChange`` parameter: nearly every writer increases
    the code width one code sooner than the plain algorithm would, and a
    decoder that disagrees produces plausible-looking rubbish.
    """
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    code_size = 9
    previous = b""
    at = 0
    total = len(data) * 8
    while at + code_size <= total:
        byte = at >> 3
        window = int.from_bytes(data[byte:byte + 3].ljust(3, b"\x00"), "big")
        code = (window >> (24 - code_size - (at & 7))) & ((1 << code_size) - 1)
        at += code_size
        if code == 256:
            table = [bytes([i]) for i in range(256)] + [b"", b""]
            code_size = 9
            previous = b""
            continue
        if code == 257:
            break
        if code < len(table):
            entry = table[code]
        elif previous:
            entry = previous + previous[:1]
        else:
            raise PdfError("LZW stream starts with an undefined code")
        if previous:
            table.append(previous + entry[:1])
        out += entry
        previous = entry
        if len(table) + early >= (1 << code_size) and code_size < 12:
            code_size += 1
    return bytes(out)


FILTERS = {
    "FlateDecode": "flate", "Fl": "flate",
    "LZWDecode": "lzw", "LZW": "lzw",
    "ASCIIHexDecode": "hex", "AHx": "hex",
    "ASCII85Decode": "a85", "A85": "a85",
    "RunLengthDecode": "rle", "RL": "rle",
    "Crypt": "none",
}

_OBJ_HEADER = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_STARTXREF = re.compile(rb"startxref\s+(\d+)")


def as_list(value):
    """A PDF value that may be a single item or an array, always as a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class Document:
    """A parsed PDF: the object graph, with the cross-references followed."""

    def __init__(self, data: bytes) -> None:
        if not data.lstrip()[:5] == b"%PDF-":
            raise PdfError("not a PDF: the file does not start with %PDF-")
        self.data = data
        self.trailer: dict = {}
        #: object number -> byte offset, or ("stm", container number, index)
        self.xref: dict[int, object] = {}
        self._cache: dict[int, object] = {}
        self._scanned = False
        self._read_xref()
        if "Encrypt" in self.trailer:
            raise PdfError(
                "this PDF is encrypted; decryption is not implemented")

    # -- cross-references ---------------------------------------------------
    def _read_xref(self) -> None:
        found = None
        for match in _STARTXREF.finditer(self.data[-2048:]):
            found = int(match.group(1))
        seen = set()
        at = found
        while at is not None and at not in seen and 0 <= at < len(self.data):
            seen.add(at)
            try:
                at = self._read_xref_section(at)
            except PdfError:
                break
        if not self.xref or "Root" not in self.trailer:
            # A damaged or unconventional file: find the objects by looking.
            self._scan_objects()

    def _read_xref_section(self, at: int):
        lexer = Lexer(self.data, at)
        mark = lexer.at
        token = lexer.token()
        if is_keyword(token, b"xref"):
            return self._read_xref_table(lexer)
        lexer.at = mark
        return self._read_xref_stream(lexer)

    def _read_xref_table(self, lexer: Lexer):
        """The classic table: subsection headers, then 20-byte entries."""
        while True:
            mark = lexer.at
            first = lexer.token()
            if is_keyword(first, b"trailer"):
                trailer = lexer.object()
                if isinstance(trailer, dict):
                    for key, value in trailer.items():
                        self.trailer.setdefault(key, value)
                    # A hybrid file points at a cross-reference stream that
                    # carries the entries this table does not.
                    if "XRefStm" in trailer:
                        self._read_xref_section(int(trailer["XRefStm"]))
                    previous = trailer.get("Prev")
                    return int(previous) if previous is not None else None
                return None
            if not (type(first) is bytes and is_number(first)):
                lexer.at = mark
                return None
            start = int(first)
            count = lexer.token()
            if not (type(count) is bytes and is_number(count)):
                return None
            for i in range(int(count)):
                offset = lexer.token()
                _gen = lexer.token()
                kind = lexer.token()
                if offset is None or kind is None:
                    return None
                if is_keyword(kind, b"n") and (start + i) not in self.xref:
                    self.xref[start + i] = int(offset)

    def _read_xref_stream(self, lexer: Lexer):
        number = lexer.token()
        _gen = lexer.token()
        keyword = lexer.token()
        if not (is_keyword(keyword, b"obj")):
            raise PdfError("cross-reference section is not a table or a stream")
        obj = self._parse_object_body(lexer)
        if not isinstance(obj, Stream):
            raise PdfError("cross-reference object is not a stream")
        widths = [int(w) for w in as_list(obj.dict.get("W", []))]
        if len(widths) < 3:
            raise PdfError("cross-reference stream has no field widths")
        size = int(obj.dict.get("Size", 0) or 0)
        index = [int(v) for v in as_list(obj.dict.get("Index", [0, size]))]
        body = self.decode_stream(obj)
        row = sum(widths)
        at = 0
        for pair in range(0, len(index) - 1, 2):
            start, count = index[pair], index[pair + 1]
            for i in range(count):
                if at + row > len(body):
                    break
                fields = []
                for width in widths:
                    fields.append(int.from_bytes(body[at:at + width], "big")
                                  if width else None)
                    at += width
                kind = 1 if fields[0] is None else fields[0]
                num = start + i
                if num in self.xref:
                    continue
                if kind == 1:
                    self.xref[num] = fields[1]
                elif kind == 2:
                    self.xref[num] = ("stm", fields[1], fields[2])
        for key, value in obj.dict.items():
            self.trailer.setdefault(key, value)
        previous = obj.dict.get("Prev")
        return int(previous) if previous is not None else None

    def _scan_objects(self) -> None:
        """Find every ``n g obj`` by looking, when the xref cannot be trusted.

        Damaged files, files edited by hand and files written by careless
        producers all end up here.  A later definition wins, which matches how
        an incremental update is meant to behave.
        """
        if self._scanned:
            return
        self._scanned = True
        for match in _OBJ_HEADER.finditer(self.data):
            self.xref[int(match.group(1))] = match.start()
        if "Root" not in self.trailer:
            for match in re.finditer(rb"trailer", self.data):
                lexer = Lexer(self.data, match.end())
                trailer = lexer.object()
                if isinstance(trailer, dict) and "Root" in trailer:
                    self.trailer.update(trailer)
        if "Root" not in self.trailer:
            # No trailer either: the catalogue has to be recognised by type.
            for num in sorted(self.xref):
                obj = self.object(num)
                if isinstance(obj, dict) and obj.get("Type") == "Catalog":
                    self.trailer["Root"] = Ref(num, 0)
                    break

    # -- objects ------------------------------------------------------------
    def object(self, num: int):
        """The object numbered ``num``, parsed and cached."""
        if num in self._cache:
            return self._cache[num]
        self._cache[num] = None                 # break reference cycles
        where = self.xref.get(num)
        if where is None:
            if not self._scanned:
                self._scan_objects()
                where = self.xref.get(num)
            if where is None:
                return None
        try:
            if isinstance(where, tuple):
                value = self._from_object_stream(where[1], where[2], num)
            else:
                value = self._at_offset(where, num)
        except PdfError:
            value = None
        self._cache[num] = value
        return value

    def _at_offset(self, offset: int, expected: int):
        if not 0 <= offset < len(self.data):
            return None
        lexer = Lexer(self.data, offset)
        number = lexer.token()
        _gen = lexer.token()
        keyword = lexer.token()
        if not (is_keyword(keyword, b"obj")):
            # The offset is wrong -- common in files with a stale xref. Fall
            # back to scanning, which finds the object wherever it really is.
            if not self._scanned:
                self._scan_objects()
                where = self.xref.get(expected)
                if isinstance(where, int) and where != offset:
                    return self._at_offset(where, expected)
            return None
        if type(number) is bytes and is_number(number) \
                and int(number) != expected:
            return None
        return self._parse_object_body(lexer)

    def _parse_object_body(self, lexer: Lexer):
        value = lexer.object()
        mark = lexer.at
        keyword = lexer.token()
        if not (is_keyword(keyword, b"stream")):
            lexer.at = mark
            return value
        if not isinstance(value, dict):
            raise PdfError("a stream whose dictionary is not a dictionary")
        data = lexer.data
        at = lexer.at
        if data[at:at + 2] == b"\r\n":
            at += 2
        elif data[at:at + 1] in (b"\n", b"\r"):
            at += 1
        length = self.resolve(value.get("Length"))
        raw = None
        if isinstance(length, int) and 0 <= length <= len(data) - at:
            raw = data[at:at + length]
            # A wrong /Length is common enough to be worth checking: what
            # follows the stream must be "endstream".
            tail = data[at + length:at + length + 20].lstrip(_WHITESPACE)
            if not tail.startswith(b"endstream"):
                raw = None
        if raw is None:
            end = data.find(b"endstream", at)
            if end < 0:
                raise PdfError("a stream with no endstream keyword")
            raw = data[at:end].rstrip(b"\r\n")
        return Stream(value, raw)

    def _from_object_stream(self, container: int, index: int, expected: int):
        """One object out of an ``/ObjStm``, where most modern objects live."""
        stream = self.object(container)
        if not isinstance(stream, Stream):
            return None
        body = self.decode_stream(stream)
        count = int(self.resolve(stream.dict.get("N", 0)) or 0)
        first = int(self.resolve(stream.dict.get("First", 0)) or 0)
        header = Lexer(body, 0)
        offsets = []
        for _ in range(count):
            num = header.token()
            at = header.token()
            if num is None or at is None:
                break
            offsets.append((int(num), int(at)))
        for position, (num, at) in enumerate(offsets):
            if num == expected or position == index:
                if num != expected:
                    continue
                return Lexer(body, first + at).object()
        return None

    def resolve(self, value, depth: int = 0):
        """Follow indirect references until something concrete comes back."""
        while isinstance(value, Ref) and depth < MAX_INDIRECTION:
            value = self.object(value.num)
            depth += 1
        return value

    def get(self, dictionary, key, default=None):
        """``dictionary[key]``, resolved.  Tolerates a missing dictionary."""
        if not isinstance(dictionary, dict):
            return default
        if key not in dictionary:
            return default
        found = self.resolve(dictionary[key])
        return default if found is None else found

    # -- streams ------------------------------------------------------------
    def decode_stream(self, stream: Stream) -> bytes:
        """A stream's bytes with its filters undone.

        Image filters are left alone -- their payload is a picture, and the
        caller wants it in the form the image decoders can read.
        """
        if stream._decoded is not None:
            return stream._decoded
        data = stream.raw
        filters = [str(f) for f in as_list(self.resolve(
            stream.dict.get("Filter") or stream.dict.get("F")))
            if isinstance(f, str)]
        params = as_list(self.resolve(
            stream.dict.get("DecodeParms") or stream.dict.get("DP")))
        for i, name in enumerate(filters):
            if name in IMAGE_FILTERS:
                break
            kind = FILTERS.get(name)
            if kind is None:
                raise PdfError(f"stream filter {name} is not one this reads")
            setting = self.resolve(params[i]) if i < len(params) else None
            data = self._one_filter(kind, data, setting)
            if len(data) > MAX_STREAM_BYTES:
                raise PdfError("a stream decompresses to more than the limit")
        stream._decoded = data
        return data

    def _one_filter(self, kind: str, data: bytes, setting) -> bytes:
        if kind == "flate":
            try:
                data = zlib.decompress(data)
            except zlib.error:
                # Truncated streams are common; keep whatever decompressed.
                decompressor = zlib.decompressobj()
                try:
                    data = decompressor.decompress(data)
                except zlib.error:
                    raise PdfError("a Flate stream is corrupt") from None
        elif kind == "lzw":
            early = 1
            if isinstance(setting, dict):
                early = int(self.get(setting, "EarlyChange", 1) or 0)
            data = lzw_decode(data, early)
        elif kind == "hex":
            data = ascii_hex_decode(data)
        elif kind == "a85":
            data = ascii85_decode(data)
        elif kind == "rle":
            data = run_length_decode(data)
        if isinstance(setting, dict) and kind in ("flate", "lzw"):
            predictor = int(self.get(setting, "Predictor", 1) or 1)
            if predictor > 1:
                data = apply_predictor(
                    data, predictor,
                    int(self.get(setting, "Colors", 1) or 1),
                    int(self.get(setting, "BitsPerComponent", 8) or 8),
                    int(self.get(setting, "Columns", 1) or 1))
        return data

    def image_filter(self, stream: Stream) -> str | None:
        """The image codec a stream ends in, if any."""
        for name in as_list(self.resolve(
                stream.dict.get("Filter") or stream.dict.get("F"))):
            if str(name) in IMAGE_FILTERS:
                return str(name)
        return None

    # -- the page tree ------------------------------------------------------
    def pages(self) -> list[dict]:
        """Every page, in reading order, with inherited attributes filled in."""
        root = self.get(self.trailer, "Root")
        node = self.get(root, "Pages")
        out: list[dict] = []
        if isinstance(node, dict):
            self._walk_pages(node, {}, out, set(), 0)
        if not out:
            # A file whose page tree is broken: take anything of /Type /Page.
            self._scan_objects()
            for num in sorted(self.xref):
                obj = self.object(num)
                if isinstance(obj, dict) and obj.get("Type") == "Page":
                    out.append(obj)
        return out

    #: Attributes a page inherits from its ancestors in the tree.
    INHERITED = ("Resources", "MediaBox", "CropBox", "Rotate")

    def _walk_pages(self, node: dict, inherited: dict, out: list,
                    seen: set, depth: int) -> None:
        if depth > MAX_INDIRECTION or id(node) in seen:
            return
        seen.add(id(node))
        passed = dict(inherited)
        for key in self.INHERITED:
            if key in node:
                passed[key] = node[key]
        kids = self.get(node, "Kids")
        if not isinstance(kids, list):
            page = dict(passed)
            page.update(node)
            out.append(page)
            return
        for kid in kids:
            child = self.resolve(kid)
            if isinstance(child, dict):
                self._walk_pages(child, passed, out, seen, depth + 1)
