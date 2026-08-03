"""Decode image files to pixels using the standard library alone.

Every format handled here decompresses with something already in Python:
PNG is zlib plus a five-case filter loop, GIF is LZW, BMP and the Netpbm
family are barely compressed at all.

JPEG needs a Huffman decoder and lives in :mod:`meridian_commander.jpeg`.

The formats built on video codecs -- WebP, AVIF, HEIC -- are *identified and
measured* rather than decoded, because no standard library ships the codec.
That is still worth doing: telling the reader "WebP, 1920x1080, VP8-coded" is
more use than refusing to open the file.

The output is deliberately dumb: one ``bytearray`` of RGB triples per frame,
row-major, no alpha.  Transparency is composited here, against a checkerboard,
so nothing downstream has to think about it -- by the time a terminal is
showing an image a few hundred pixels across, an alpha channel is not
information the reader can use.

Sizes are capped on two axes.  ``MAX_BYTES`` bounds the download, because a
remote pane has to fetch the whole file before any of it can be decoded, and
``MAX_PIXELS`` bounds the decode, because a width and a height cost four bytes
each to write into a header and three bytes per pixel to believe.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

# Formats with a decoder here or in the jpeg module, plus the ones we can only
# measure.  The browser opens all of them: reporting "WebP, 1920x1080, no
# decoder for VP8" beats refusing to open the file at all.
IMAGE_SUFFIXES = (
    ".png", ".gif", ".bmp", ".dib", ".jpg", ".jpeg", ".jpe", ".jfif",
    ".pnm", ".ppm", ".pgm", ".pbm",
    ".webp", ".tif", ".tiff", ".ico", ".avif", ".heic", ".heif",
)

MAX_BYTES = 64 * 1024 * 1024
# 40 megapixels is beyond any camera likely to be in a file manager's path,
# and still only 120 MB of RGB.
MAX_PIXELS = 40_000_000
# Animations stop here.  Frames are stepped through by hand in a terminal, so
# the tail of a 900-frame GIF is never going to be looked at.
MAX_FRAMES = 64

# The transparency checkerboard, in the shades image editors have used for
# thirty years.  At 1:1 the squares are visible; downsampled they average to a
# flat mid-grey, which is exactly the right reading.
CHECK_LIGHT = 0x99
CHECK_DARK = 0x66
CHECK_SIZE = 8


class ImageError(Exception):
    """A file that cannot be decoded, with a reason fit to show the user."""


@dataclass
class Frame:
    """One decoded still: ``width * height * 3`` bytes of RGB."""

    pixels: bytearray
    delay: float = 0.0          # seconds until the next frame; 0 if still


@dataclass
class Image:
    width: int
    height: int
    kind: str                   # "PNG", "JPEG", ... -- shown in the title
    detail: str = ""            # "8-bit truecolour+alpha", "256-colour", ...
    frames: list[Frame] = field(default_factory=list)
    truncated: bool = False     # more frames existed than MAX_FRAMES
    note: str = ""              # why there are no pixels, when there are none

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1


def is_image(name: str) -> bool:
    """Whether ``name`` looks like an image this module will try to open."""
    return name.lower().endswith(IMAGE_SUFFIXES)


def check_colour(x: int, y: int) -> int:
    """The checkerboard shade showing through at pixel ``(x, y)``."""
    if (x // CHECK_SIZE + y // CHECK_SIZE) % 2:
        return CHECK_DARK
    return CHECK_LIGHT


def _guard_size(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ImageError(f"bad image size {width}x{height}")
    if width * height > MAX_PIXELS:
        raise ImageError(
            f"image is {width}x{height}; the limit is {MAX_PIXELS:,} pixels")


def _composite(r: int, g: int, b: int, alpha: int, x: int, y: int):
    """Blend a pixel onto the checkerboard.  Opaque pixels pass straight through."""
    if alpha >= 255:
        return r, g, b
    back = check_colour(x, y)
    rest = 255 - alpha
    return ((r * alpha + back * rest) // 255,
            (g * alpha + back * rest) // 255,
            (b * alpha + back * rest) // 255)


# -- format sniffing -----------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def sniff(data: bytes) -> str:
    """The format's name, decided by content rather than by file name.

    Content wins because the name is so often wrong -- screenshots saved as
    ``.jpg`` that are really PNG are a genre of their own -- and because the
    decoder has the bytes in hand either way.
    """
    if data.startswith(PNG_MAGIC):
        return "PNG"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if data.startswith(b"BM"):
        return "BMP"
    if data[:2] == b"\xff\xd8":
        return "JPEG"
    if data[:2] in (b"P1", b"P2", b"P3", b"P4", b"P5", b"P6"):
        return "PNM"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ICO"
    if data[4:8] == b"ftyp":
        if data[8:12] in (b"avif", b"avis"):
            return "AVIF"
        return "HEIF"
    raise ImageError("not an image, or a format this cannot recognise")


# -- PNG -----------------------------------------------------------------------

#: Samples per pixel for each PNG colour type.
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
PNG_KIND = {
    0: "greyscale", 2: "truecolour", 3: "palette",
    4: "greyscale+alpha", 6: "truecolour+alpha",
}
#: Bit depths the specification allows for each colour type.
PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
              4: (8, 16), 6: (8, 16)}
#: Adam7: (first column, first row, column step, row step) for each pass.
#: Read off the specification's 8x8 map, where each cell names its pass::
#:
#:     1 6 4 6 2 6 4 6        Passes 5, 6 and 7 are the ones worth checking
#:     7 7 7 7 7 7 7 7        twice: 5 takes the even columns of every fourth
#:     5 6 5 6 5 6 5 6        row, 6 takes the odd columns of every even row,
#:     7 7 7 7 7 7 7 7        and 7 takes every odd row entire.  Getting the
#:     3 6 4 6 3 6 4 6        column and row offsets the wrong way round in
#:     7 7 7 7 7 7 7 7        those three still decodes a plausible-looking
#:     5 6 5 6 5 6 5 6        image, which is how it hides.
#:     7 7 7 7 7 7 7 7
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
         (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _png_chunks(data: bytes):
    """Yield ``(type, payload)`` for each chunk after the signature."""
    at = len(PNG_MAGIC)
    while at + 8 <= len(data):
        (length,) = struct.unpack(">I", data[at:at + 4])
        kind = data[at + 4:at + 8]
        start = at + 8
        end = start + length
        if end > len(data):
            raise ImageError("truncated PNG: a chunk runs past the end of file")
        yield kind, data[start:end]
        # The CRC is skipped: zlib already rejects damaged image data, and a
        # bad checksum on a chunk we ignore anyway is not worth refusing over.
        at = end + 4
        if kind == b"IEND":
            return
    raise ImageError("truncated PNG: no IEND chunk")


def _paeth(a: int, b: int, c: int) -> int:
    """PNG's Paeth predictor: whichever neighbour the gradient points nearest."""
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> bytearray:
    """Undo the per-scanline filters, returning ``height * stride`` bytes.

    ``bpp`` is the distance back to the matching byte of the pixel to the left,
    which is 1 for anything under 8 bits per sample: sub-byte pixels filter
    against the previous *byte*, not the previous pixel.
    """
    if len(raw) < height * (stride + 1):
        raise ImageError("truncated PNG: not enough image data")
    out = bytearray(height * stride)
    prev = bytearray(stride)
    at = 0
    for y in range(height):
        kind = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += stride + 1
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif kind:
            raise ImageError(f"PNG scanline filter {kind} is not defined")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


def _unpack_samples(line: bytes, count: int, depth: int) -> list[int]:
    """``count`` samples out of a packed scanline at ``depth`` bits each."""
    if depth == 8:
        return list(line[:count])
    if depth == 16:
        # Keep the high byte only. A terminal cannot show 16 bits per channel,
        # and the high byte on its own is the correctly scaled 8-bit value.
        return list(line[0:count * 2:2])
    out = []
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    for i in range(count):
        shift = 8 - depth * (i % per_byte + 1)
        out.append((line[i // per_byte] >> shift) & mask)
    return out


def read_png(data: bytes) -> Image:
    header = None
    palette = b""
    trans = b""
    idat = []
    for kind, payload in _png_chunks(data):
        if kind == b"IHDR":
            header = payload
        elif kind == b"PLTE":
            palette = payload
        elif kind == b"tRNS":
            trans = payload
        elif kind == b"IDAT":
            idat.append(payload)
    if header is None or len(header) < 13:
        raise ImageError("PNG has no image header")
    width, height, depth, colour, _comp, _filt, interlace = struct.unpack(
        ">IIBBBBB", header[:13])
    _guard_size(width, height)
    if colour not in PNG_CHANNELS:
        raise ImageError(f"PNG colour type {colour} is not defined")
    if depth not in PNG_DEPTHS[colour]:
        raise ImageError(
            f"PNG bit depth {depth} is not allowed for {PNG_KIND[colour]}")
    if interlace > 1:
        raise ImageError(f"PNG interlace method {interlace} is not defined")
    if not idat:
        raise ImageError("PNG has no image data")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise ImageError(f"PNG image data is corrupt: {exc}") from None

    channels = PNG_CHANNELS[colour]
    if interlace:
        rows = _png_deinterlace(raw, width, height, depth, channels)
    else:
        stride = (width * channels * depth + 7) // 8
        flat = _unfilter(raw, height, stride, max(1, channels * depth // 8))
        rows = [_unpack_samples(flat[y * stride:(y + 1) * stride],
                                width * channels, depth)
                for y in range(height)]

    pixels = _png_to_rgb(rows, width, height, depth, colour, palette, trans)
    detail = f"{depth}-bit {PNG_KIND[colour]}"
    if interlace:
        detail += ", interlaced"
    return Image(width, height, "PNG", detail, [Frame(pixels)])


def _png_deinterlace(raw: bytes, width: int, height: int, depth: int,
                     channels: int) -> list[list[int]]:
    """Reassemble Adam7's seven sub-images into ordinary scanlines."""
    rows = [[0] * (width * channels) for _ in range(height)]
    at = 0
    for x0, y0, dx, dy in ADAM7:
        cols = (width - x0 + dx - 1) // dx
        lines = (height - y0 + dy - 1) // dy
        if cols <= 0 or lines <= 0:
            continue                    # this pass has no pixels in a tiny image
        stride = (cols * channels * depth + 7) // 8
        flat = _unfilter(raw[at:], lines, stride, max(1, channels * depth // 8))
        at += lines * (stride + 1)
        for j in range(lines):
            samples = _unpack_samples(flat[j * stride:(j + 1) * stride],
                                      cols * channels, depth)
            row = rows[y0 + j * dy]
            for i in range(cols):
                target = (x0 + i * dx) * channels
                row[target:target + channels] = \
                    samples[i * channels:(i + 1) * channels]
    return rows


def _png_to_rgb(rows, width, height, depth, colour, palette, trans):
    """Turn unpacked samples into composited RGB."""
    pixels = bytearray(width * height * 3)
    top = (1 << depth) - 1
    at = 0
    for y in range(height):
        row = rows[y]
        for x in range(width):
            alpha = 255
            if colour == 3:
                index = row[x]
                base = index * 3
                if base + 3 > len(palette):
                    raise ImageError("PNG palette index is out of range")
                r, g, b = palette[base], palette[base + 1], palette[base + 2]
                if index < len(trans):
                    alpha = trans[index]
            elif colour == 0:
                r = g = b = row[x] * 255 // top
            elif colour == 4:
                r = g = b = row[x * 2]
                alpha = row[x * 2 + 1]
            elif colour == 2:
                r, g, b = row[x * 3], row[x * 3 + 1], row[x * 3 + 2]
            else:
                r, g, b = row[x * 4], row[x * 4 + 1], row[x * 4 + 2]
                alpha = row[x * 4 + 3]
            pixels[at], pixels[at + 1], pixels[at + 2] = \
                _composite(r, g, b, alpha, x, y)
            at += 3
    return pixels


# -- Netpbm (PBM/PGM/PPM) ------------------------------------------------------

PNM_KIND = {
    b"P1": ("bitmap", 1, True), b"P4": ("bitmap", 1, False),
    b"P2": ("greyscale", 1, True), b"P5": ("greyscale", 1, False),
    b"P3": ("RGB", 3, True), b"P6": ("RGB", 3, False),
}


def _pnm_tokens(data: bytes, at: int, count: int) -> tuple[list[int], int]:
    """Read ``count`` whitespace-separated integers, skipping ``#`` comments."""
    values = []
    while len(values) < count:
        while at < len(data) and data[at:at + 1].isspace():
            at += 1
        if at < len(data) and data[at:at + 1] == b"#":
            while at < len(data) and data[at] != 0x0A:
                at += 1
            continue
        start = at
        while at < len(data) and not data[at:at + 1].isspace() \
                and data[at:at + 1] != b"#":
            at += 1
        if start == at:
            raise ImageError("truncated Netpbm header")
        try:
            values.append(int(data[start:at]))
        except ValueError:
            raise ImageError("Netpbm header is not a number") from None
    return values, at


def read_pnm(data: bytes) -> Image:
    magic = data[:2]
    if magic not in PNM_KIND:
        raise ImageError(f"Netpbm type {magic.decode('latin-1')} is not handled")
    name, channels, ascii_form = PNM_KIND[magic]
    # A bitmap has no maximum-value field; everything else does.
    wanted = 2 if channels == 1 and name == "bitmap" else 3
    values, at = _pnm_tokens(data, 2, wanted)
    width, height = values[0], values[1]
    top = 1 if wanted == 2 else values[2]
    _guard_size(width, height)
    if top < 1 or top > 65535:
        raise ImageError(f"Netpbm maximum value {top} is out of range")

    count = width * height * channels
    if ascii_form:
        samples, _at = _pnm_tokens(data, at, count)
    elif name == "bitmap":
        # Binary bitmaps pack eight pixels per byte, each row padded out to a
        # byte boundary, and 1 means *black* -- the one format here that does.
        at += 1
        stride = (width + 7) // 8
        need = stride * height
        if len(data) - at < need:
            raise ImageError("truncated Netpbm image data")
        samples = []
        for y in range(height):
            row = data[at + y * stride:at + (y + 1) * stride]
            samples.extend((row[x // 8] >> (7 - x % 8)) & 1
                           for x in range(width))
    else:
        at += 1
        wide = top > 255
        need = count * (2 if wide else 1)
        if len(data) - at < need:
            raise ImageError("truncated Netpbm image data")
        raw = data[at:at + need]
        samples = list(raw[0::2]) if wide else list(raw)
        if wide:
            top >>= 8

    pixels = bytearray(count // channels * 3)
    for i in range(width * height):
        if name == "bitmap":
            value = 0 if samples[i] else 255      # 1 is black in Netpbm
            pixels[i * 3:i * 3 + 3] = bytes((value, value, value))
        elif channels == 1:
            value = min(255, samples[i] * 255 // top)
            pixels[i * 3:i * 3 + 3] = bytes((value, value, value))
        else:
            pixels[i * 3:i * 3 + 3] = bytes(
                min(255, samples[i * 3 + c] * 255 // top) for c in range(3))
    form = "ASCII" if ascii_form else "binary"
    return Image(width, height, "PNM", f"{form} {name}", [Frame(pixels)])


# -- BMP -----------------------------------------------------------------------

BMP_RGB, BMP_RLE8, BMP_RLE4, BMP_BITFIELDS = 0, 1, 2, 3
BMP_COMPRESSION = {BMP_RLE8: "RLE8", BMP_RLE4: "RLE4", 4: "JPEG", 5: "PNG"}


def _mask_shift(mask: int) -> tuple[int, int]:
    """``(shift, span)`` for a bitfield mask, so a channel can be scaled to 8 bits."""
    if not mask:
        return 0, 0
    shift = (mask & -mask).bit_length() - 1
    return shift, (mask >> shift).bit_length()


def read_bmp(data: bytes) -> Image:
    if len(data) < 26:
        raise ImageError("truncated BMP: no header")
    (offset,) = struct.unpack("<I", data[10:14])
    (header_size,) = struct.unpack("<I", data[14:18])
    if header_size == 12:                       # the original OS/2 header
        width, height, _planes, depth = struct.unpack("<HHHH", data[18:26])
        height = height           # BITMAPCOREHEADER heights are never negative
        compression, palette_count = BMP_RGB, 0
    elif header_size >= 40:
        width, height, _planes, depth, compression = struct.unpack(
            "<iiHHI", data[18:34])
        (palette_count,) = struct.unpack("<I", data[46:50])
    else:
        raise ImageError(f"BMP header size {header_size} is not one this reads")

    # A negative height means the rows are stored top-down instead of the
    # bottom-up order BMP otherwise uses.
    top_down = height < 0
    height = abs(height)
    _guard_size(width, height)
    if compression in BMP_COMPRESSION:
        raise ImageError(
            f"BMP compression {BMP_COMPRESSION[compression]} is not handled")
    if compression not in (BMP_RGB, BMP_BITFIELDS):
        raise ImageError(f"BMP compression {compression} is not defined")
    if depth not in (1, 4, 8, 16, 24, 32):
        raise ImageError(f"BMP depth {depth} is not defined")

    masks = None
    if compression == BMP_BITFIELDS:
        if header_size >= 56:
            masks = struct.unpack("<IIII", data[54:70])
        else:
            # Pre-v4 headers keep the masks in the gap before the pixel data.
            masks = struct.unpack("<III", data[14 + header_size:26 + header_size])
            masks = masks + (0,)

    palette = b""
    if depth <= 8:
        entries = palette_count or (1 << depth)
        start = 14 + header_size
        step = 3 if header_size == 12 else 4
        palette = data[start:start + entries * step]
        if len(palette) < entries * step:
            raise ImageError("truncated BMP: colour table is short")
    else:
        step = 4

    stride = ((width * depth + 31) // 32) * 4
    if len(data) - offset < stride * height:
        raise ImageError("truncated BMP: not enough pixel data")

    pixels = bytearray(width * height * 3)
    for row in range(height):
        y = row if top_down else height - 1 - row
        line = data[offset + row * stride:offset + (row + 1) * stride]
        for x in range(width):
            r, g, b, alpha = _bmp_pixel(line, x, depth, palette, step, masks)
            at = (y * width + x) * 3
            pixels[at:at + 3] = bytes(_composite(r, g, b, alpha, x, y))
    detail = f"{depth}-bit"
    if masks:
        detail += " bitfields"
    if top_down:
        detail += ", top-down"
    return Image(width, height, "BMP", detail, [Frame(pixels)])


def _bmp_pixel(line: bytes, x: int, depth: int, palette: bytes, step: int,
               masks):
    """One pixel out of a BMP scanline, as ``(r, g, b, alpha)``."""
    if depth <= 8:
        per_byte = 8 // depth
        shift = 8 - depth * (x % per_byte + 1)
        index = (line[x // per_byte] >> shift) & ((1 << depth) - 1)
        at = index * step
        if at + 3 > len(palette):
            raise ImageError("BMP palette index is out of range")
        return palette[at + 2], palette[at + 1], palette[at], 255
    if depth == 24:
        at = x * 3
        return line[at + 2], line[at + 1], line[at], 255
    if depth == 16:
        (value,) = struct.unpack("<H", line[x * 2:x * 2 + 2])
        # Without explicit masks, 16-bit BMP is 5-5-5 with the top bit unused.
        use = masks or (0x7C00, 0x03E0, 0x001F, 0)
    else:
        (value,) = struct.unpack("<I", line[x * 4:x * 4 + 4])
        use = masks or (0x00FF0000, 0x0000FF00, 0x000000FF, 0)
    out = []
    for mask in use[:3]:
        shift, span = _mask_shift(mask)
        top = (1 << span) - 1
        out.append(((value & mask) >> shift) * 255 // top if top else 0)
    alpha = 255
    if use[3]:
        shift, span = _mask_shift(use[3])
        top = (1 << span) - 1
        alpha = ((value & use[3]) >> shift) * 255 // top
    return out[0], out[1], out[2], alpha


# -- GIF -----------------------------------------------------------------------

GIF_TRAILER = 0x3B
GIF_EXTENSION = 0x21
GIF_IMAGE = 0x2C
GIF_GRAPHIC_CONTROL = 0xF9
#: Interlaced GIFs store rows in four passes: (first row, step).
GIF_PASSES = ((0, 8), (4, 8), (2, 4), (1, 2))
#: The largest LZW code before the table stops growing.
LZW_MAX_BITS = 12


def _lzw_decode(data: bytes, min_code_size: int, expected: int) -> bytearray:
    """GIF's variable-width LZW.  Codes are packed little-endian, LSB first."""
    if not 1 <= min_code_size <= 11:
        raise ImageError(f"GIF LZW code size {min_code_size} is out of range")
    clear = 1 << min_code_size
    end = clear + 1
    base = [bytes([i]) for i in range(clear)] + [b"", b""]
    table = list(base)
    code_size = min_code_size + 1
    out = bytearray()
    previous = b""
    at = 0                                  # position in bits
    total = len(data) * 8
    while at + code_size <= total and len(out) < expected:
        byte = at >> 3
        # Three bytes always cover a code of at most 12 bits at any bit offset.
        window = int.from_bytes(data[byte:byte + 3].ljust(3, b"\x00"), "little")
        code = (window >> (at & 7)) & ((1 << code_size) - 1)
        at += code_size
        if code == clear:
            table = list(base)
            code_size = min_code_size + 1
            previous = b""
            continue
        if code == end:
            break
        if code < len(table):
            entry = table[code]
        elif previous:
            # The one self-referential case: a code for a sequence that is
            # only being defined by this very code.
            entry = previous + previous[:1]
        else:
            raise ImageError("GIF image data starts with an undefined code")
        if previous:
            table.append(previous + entry[:1])
            if len(table) >= (1 << code_size) and code_size < LZW_MAX_BITS:
                code_size += 1
        out += entry
        previous = entry
    return out


def _gif_blocks(data: bytes, at: int) -> tuple[bytes, int]:
    """Read a chain of length-prefixed sub-blocks, returning the joined bytes."""
    parts = []
    while at < len(data):
        size = data[at]
        at += 1
        if size == 0:
            return b"".join(parts), at
        parts.append(data[at:at + size])
        at += size
    raise ImageError("truncated GIF: a block chain has no terminator")


def _gif_table(data: bytes, at: int, count: int) -> tuple[bytes, int]:
    end = at + count * 3
    if end > len(data):
        raise ImageError("truncated GIF: colour table runs past the end of file")
    return data[at:end], end


def read_gif(data: bytes) -> Image:
    if len(data) < 13:
        raise ImageError("truncated GIF: no screen descriptor")
    width, height, packed, _bg, _aspect = struct.unpack("<HHBBB", data[6:13])
    _guard_size(width, height)
    at = 13
    global_table = b""
    if packed & 0x80:
        global_table, at = _gif_table(data, at, 2 << (packed & 7))

    # The canvas every frame is drawn onto, and the state a Graphic Control
    # Extension leaves behind for the image that follows it.
    canvas = bytearray(width * height * 3)
    for i in range(width * height):
        shade = check_colour(i % width, i // width)
        canvas[i * 3:i * 3 + 3] = bytes((shade, shade, shade))
    frames: list[Frame] = []
    transparent = -1
    delay = 0.0
    disposal = 0
    truncated = False

    while at < len(data):
        marker = data[at]
        at += 1
        if marker == GIF_TRAILER:
            break
        if marker == GIF_EXTENSION:
            label = data[at]
            block, at = _gif_blocks(data, at + 1)
            if label == GIF_GRAPHIC_CONTROL and len(block) >= 4:
                flags = block[0]
                disposal = (flags >> 2) & 7
                transparent = block[3] if flags & 1 else -1
                delay = struct.unpack("<H", block[1:3])[0] / 100.0
            continue
        if marker != GIF_IMAGE:
            raise ImageError(f"GIF block type 0x{marker:02x} is not defined")
        if len(frames) >= MAX_FRAMES:
            truncated = True
            break

        left, top, fw, fh, fpacked = struct.unpack("<HHHHB", data[at:at + 9])
        at += 9
        table = global_table
        if fpacked & 0x80:
            table, at = _gif_table(data, at, 2 << (fpacked & 7))
        if not table:
            raise ImageError("GIF frame has no colour table")
        if at >= len(data):
            raise ImageError("truncated GIF: no image data")
        min_code_size = data[at]
        block, at = _gif_blocks(data, at + 1)
        indices = _lzw_decode(block, min_code_size, fw * fh)
        if len(indices) < fw * fh:
            raise ImageError("truncated GIF: frame is short of pixels")

        saved = bytes(canvas) if disposal == 3 else b""
        _gif_paint(canvas, width, height, indices, left, top, fw, fh,
                   bool(fpacked & 0x40), table, transparent)
        frames.append(Frame(bytearray(canvas), delay))
        if disposal == 2:
            _gif_clear(canvas, width, left, top, fw, fh)
        elif disposal == 3 and saved:
            canvas[:] = saved

    if not frames:
        raise ImageError("GIF has no frames")
    colours = len(global_table) // 3
    detail = f"{colours}-colour palette" if colours else "local palettes"
    if len(frames) > 1:
        detail += f", {len(frames)} frames"
    return Image(width, height, "GIF", detail, frames, truncated)


def _gif_paint(canvas, width, height, indices, left, top, fw, fh, interlaced,
               table, transparent) -> None:
    """Draw one sub-image onto the canvas, honouring transparency."""
    rows = list(range(fh))
    if interlaced:
        rows = [row for first, step in GIF_PASSES
                for row in range(first, fh, step)]
    for source, row in enumerate(rows):
        y = top + row
        if not 0 <= y < height:
            continue                    # a frame may hang off the canvas edge
        for col in range(fw):
            x = left + col
            if not 0 <= x < width:
                continue
            index = indices[source * fw + col]
            if index == transparent:
                continue
            base = index * 3
            if base + 3 > len(table):
                raise ImageError("GIF palette index is out of range")
            at = (y * width + x) * 3
            canvas[at:at + 3] = table[base:base + 3]


def _gif_clear(canvas, width, left, top, fw, fh) -> None:
    """Disposal method 2: put the frame's area back to the checkerboard."""
    for row in range(fh):
        y = top + row
        for col in range(fw):
            x = left + col
            at = (y * width + x) * 3
            if 0 <= at < len(canvas) - 2:
                shade = check_colour(x, y)
                canvas[at:at + 3] = bytes((shade, shade, shade))


# -- formats we can measure but not decode -------------------------------------

def _webp_size(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        return w, int.from_bytes(data[27:30], "little") + 1
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        return (struct.unpack("<H", data[26:28])[0] & 0x3FFF,
                struct.unpack("<H", data[28:30])[0] & 0x3FFF)
    raise ImageError("WebP has no recognisable size")


def _tiff_size(data: bytes) -> tuple[int, int]:
    order = "<" if data[:2] == b"II" else ">"
    (offset,) = struct.unpack(order + "I", data[4:8])
    (count,) = struct.unpack(order + "H", data[offset:offset + 2])
    found = {}
    for i in range(count):
        at = offset + 2 + i * 12
        tag, kind = struct.unpack(order + "HH", data[at:at + 4])
        if tag in (256, 257):
            # SHORT stores its value in the first two bytes of the field.
            fmt = "H" if kind == 3 else "I"
            (found[tag],) = struct.unpack(order + fmt, data[at + 8:at + 8 +
                                                            struct.calcsize(fmt)])
    if 256 not in found or 257 not in found:
        raise ImageError("TIFF has no size tags")
    return found[256], found[257]


def read_measured(data: bytes, kind: str) -> Image:
    """Size and format for something we cannot decode: better than nothing.

    A file manager's job here is to tell you what you are looking at.  Refusing
    to open a WebP at all is less useful than saying it is a WebP, giving its
    dimensions, and naming the codec that would be needed.
    """
    reason = {
        "WEBP": "WebP pixels are VP8-coded; no standard-library decoder exists",
        "AVIF": "AVIF pixels are AV1-coded; no standard-library decoder exists",
        "HEIF": "HEIF pixels are HEVC-coded; no standard-library decoder exists",
        "TIFF": "TIFF allows a dozen codecs; none are handled here",
        "ICO": "an icon file is a container of other images",
    }[kind]
    try:
        if kind == "WEBP":
            width, height = _webp_size(data)
        elif kind == "TIFF":
            width, height = _tiff_size(data)
        elif kind == "ICO":
            # Sizes of 0 mean 256 in the ICO directory.
            width = data[6] or 256
            height = data[7] or 256
        else:
            width = height = 0
    except (struct.error, IndexError, ImageError):
        # An unreadable header is not a reason to refuse the file: the format
        # is still worth naming even when the size cannot be found.
        width = height = 0
    return Image(width, height, kind, "not decoded", [], note=reason)


# -- the front door ------------------------------------------------------------

def decode(data: bytes) -> Image:
    """Decode ``data``, or describe it if the format has no decoder here."""
    kind = sniff(data)
    if kind == "PNG":
        return read_png(data)
    if kind == "GIF":
        return read_gif(data)
    if kind == "BMP":
        return read_bmp(data)
    if kind == "PNM":
        return read_pnm(data)
    if kind == "JPEG":
        # Imported here rather than at the top: the jpeg module imports this
        # one for Image and the size guard, so a top-level import would be a
        # cycle. This is the only direction that needs breaking.
        from .jpeg import read_jpeg

        return read_jpeg(data)
    return read_measured(data, kind)


