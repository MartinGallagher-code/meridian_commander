"""Decode JPEG to a 1/8-scale image, by reading DC coefficients only.

A full JPEG decoder in pure Python is slow in a way a file manager cannot
afford: the inverse DCT runs once per 8x8 block per component, so a 12
megapixel photo is some three million of them, and the result is an image a
terminal then throws away 99% of.

Every one of those blocks has a DC coefficient that *is* the block's mean
value, so taking DC alone yields the image at exactly 1/8 scale with no IDCT
at all.  At the size a terminal draws -- a couple of hundred cells across at
best -- a 1/8 thumbnail is still more pixels than can be shown.

This is not free.  The AC coefficients still have to be Huffman-decoded,
because they are what says where the next block starts; they are decoded and
dropped.  What is saved is the dequantise, the IDCT, the chroma upsample and
the full-resolution buffer, which is the overwhelming majority of the work.

Handled: baseline and extended sequential (SOF0/SOF1), and the first DC scan
of a progressive file (SOF2), which is all a progressive JPEG needs for this
-- its opening scan is DC-only by construction, so progressive is if anything
easier than baseline here.  Restart markers, all sampling factors, greyscale,
YCbCr, Adobe CMYK/YCCK, and EXIF orientation.

Chroma is upsampled nearest-neighbour rather than interpolated.  Measured
against a full decode box-filtered to the same scale, that costs a mean error
per channel of under 1 in 255 with no chroma subsampling, about 6 on
photographic content at 4:2:0, and about 28 on content that changes colour
every eight pixels -- which is to say, at exactly the frequency 4:2:0 exists
to throw away.  Interpolating would narrow the last case, but nothing that
survives being drawn as terminal cells depends on it.
"""

from __future__ import annotations

import struct

from .image import Frame, Image, ImageError, _guard_size

# Markers that carry no payload and can simply be stepped over.
STANDALONE = frozenset([0x01] + list(range(0xD0, 0xD8)))
#: Start-of-frame markers, minus the three in that range that are not frames.
SOF_MARKERS = frozenset(set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC})
#: The ones this can actually decode: baseline, extended sequential, progressive.
SOF_SUPPORTED = {0xC0: "baseline", 0xC1: "extended sequential",
                 0xC2: "progressive"}

#: EXIF orientation -> (quarter turns anticlockwise, mirrored)
ORIENTATIONS = {1: (0, False), 2: (0, True), 3: (2, False), 4: (2, True),
                5: (3, True), 6: (3, False), 7: (1, True), 8: (1, False)}


class _EndOfData(Exception):
    """The entropy-coded stream ran into a marker or the end of the file."""


class _Huffman:
    """A canonical JPEG Huffman table, in the decoder's own working form."""

    def __init__(self, counts: bytes, values: bytes) -> None:
        self.values = values
        # mincode/maxcode/valptr per code length, which is how the standard
        # describes decoding: shift a bit in, then ask whether the code so far
        # is inside this length's range.
        self.mincode = [0] * 17
        self.maxcode = [-1] * 17
        self.valptr = [0] * 17
        code = at = 0
        for length in range(1, 17):
            self.valptr[length] = at
            self.mincode[length] = code
            code += counts[length - 1]
            at += counts[length - 1]
            self.maxcode[length] = code - 1
            if counts[length - 1] == 0:
                self.maxcode[length] = -1
            code <<= 1
        if at > len(values):
            raise ImageError("JPEG Huffman table is short of values")


class _Bits:
    """Bit reader over entropy-coded data, undoing JPEG's 0xFF byte stuffing."""

    def __init__(self, data: bytes, at: int) -> None:
        self.data = data
        self.at = at
        self._byte = 0
        self._left = 0

    def bit(self) -> int:
        if self._left == 0:
            if self.at >= len(self.data):
                raise _EndOfData()
            byte = self.data[self.at]
            self.at += 1
            if byte == 0xFF:
                # 0xFF 0x00 is a literal 0xFF; anything else is a real marker
                # and means this run of entropy-coded data has ended. A 0xFF
                # with nothing after it is a truncated file, not a literal --
                # reading it as data would invent a byte the encoder never
                # wrote.
                if self.at >= len(self.data):
                    raise _EndOfData()
                if self.data[self.at] == 0x00:
                    self.at += 1
                else:
                    self.at -= 1
                    raise _EndOfData()
            self._byte = byte
            self._left = 8
        self._left -= 1
        return (self._byte >> self._left) & 1

    def receive(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def decode(self, table: _Huffman) -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.bit()
            if code <= table.maxcode[length]:
                return table.values[table.valptr[length]
                                    + code - table.mincode[length]]
        raise ImageError("JPEG Huffman code is not in the table")

    def restart(self) -> bool:
        """Align to the next restart marker and step over it."""
        self._left = 0
        while self.at + 1 < len(self.data):
            if self.data[self.at] == 0xFF and 0xD0 <= self.data[self.at + 1] <= 0xD7:
                self.at += 2
                return True
            self.at += 1
        return False


def extend(value: int, count: int) -> int:
    """JPEG's sign convention: the top half of the range is positive."""
    if count and value < (1 << (count - 1)):
        return value - (1 << count) + 1
    return value


class _Component:
    def __init__(self, ident: int, h: int, v: int, quant: int) -> None:
        self.ident = ident
        self.h = h
        self.v = v
        self.quant = quant
        self.dc_table = 0
        self.pred = 0
        self.plane: list[int] = []
        self.cols = 0
        self.rows = 0


def _markers(data: bytes):
    """Walk the marker segments, yielding ``(marker, payload, offset_after)``."""
    at = 2
    while at + 1 < len(data):
        if data[at] != 0xFF:
            at += 1                     # fill byte, or padding between segments
            continue
        marker = data[at + 1]
        if marker in (0xFF, 0xD8):
            at += 1 if marker == 0xFF else 2
            continue
        if marker in STANDALONE:
            at += 2
            continue
        if marker == 0xD9:              # end of image
            return
        if at + 4 > len(data):
            raise ImageError("truncated JPEG: a segment header runs off the end")
        (length,) = struct.unpack(">H", data[at + 2:at + 4])
        if length < 2:
            raise ImageError("JPEG segment length is impossible")
        payload = data[at + 4:at + 2 + length]
        yield marker, payload, at + 2 + length
        at += 2 + length


def _orientation(payload: bytes) -> int:
    """EXIF orientation out of an APP1 payload, or 1 when it says nothing."""
    if not payload.startswith(b"Exif\x00\x00"):
        return 1
    tiff = payload[6:]
    if tiff[:2] not in (b"II", b"MM"):
        return 1
    order = "<" if tiff[:2] == b"II" else ">"
    try:
        (offset,) = struct.unpack(order + "I", tiff[4:8])
        (count,) = struct.unpack(order + "H", tiff[offset:offset + 2])
        for i in range(count):
            entry = offset + 2 + i * 12
            (tag,) = struct.unpack(order + "H", tiff[entry:entry + 2])
            if tag == 0x0112:
                (value,) = struct.unpack(order + "H", tiff[entry + 8:entry + 10])
                return value
    except struct.error:
        return 1
    return 1


def _turn(pixels: bytearray, width: int, height: int, quarters: int,
          mirrored: bool):
    """Apply an EXIF orientation.  Phone photos are almost never upright."""
    if mirrored:
        flipped = bytearray(len(pixels))
        for y in range(height):
            for x in range(width):
                at = (y * width + x) * 3
                to = (y * width + (width - 1 - x)) * 3
                flipped[to:to + 3] = pixels[at:at + 3]
        pixels = flipped
    for _ in range(quarters):
        turned = bytearray(len(pixels))
        for y in range(height):
            for x in range(width):
                at = (y * width + x) * 3
                # One quarter turn anticlockwise: (x, y) -> (y, width-1-x)
                to = ((width - 1 - x) * height + y) * 3
                turned[to:to + 3] = pixels[at:at + 3]
        pixels = turned
        width, height = height, width
    return pixels, width, height


def _read_frame(payload: bytes):
    """A start-of-frame segment: size and the component sampling factors."""
    if len(payload) < 6:
        raise ImageError("truncated JPEG frame header")
    _precision, height, width, count = struct.unpack(">BHHB", payload[:6])
    if len(payload) < 6 + count * 3:
        raise ImageError("truncated JPEG frame header")
    components = []
    for i in range(count):
        ident, sampling, quant = payload[6 + i * 3:9 + i * 3]
        h, v = sampling >> 4, sampling & 15
        if not (1 <= h <= 4 and 1 <= v <= 4):
            raise ImageError(f"JPEG sampling factor {h}x{v} is out of range")
        components.append(_Component(ident, h, v, quant))
    return width, height, components


def _decode_scan(bits, scan, mcus_x, mcus_y, hmax, vmax, width, height,
                 quant, dc_tables, ac_tables, restart_interval, skip_ac,
                 shift):
    """Fill each component's plane with one DC sample per 8x8 block."""
    single = scan[0] if len(scan) == 1 else None
    if single is not None:
        # A scan of one component is not interleaved: its blocks come in that
        # component's own raster order, which is not the MCU grid.
        across = -(-(width * single.h // hmax) // 8)
        down = -(-(height * single.v // vmax) // 8)
        units = [(0, 0)] * 0
        total = across * down
    else:
        across = down = 0
        total = mcus_x * mcus_y

    for unit in range(total):
        if restart_interval and unit and unit % restart_interval == 0:
            if not bits.restart():
                return
            for component in scan:
                component.pred = 0
        for component in scan:
            table = dc_tables.get(component.dc_table)
            if table is None:
                raise ImageError("JPEG scan names a Huffman table it never sent")
            blocks = ((0, 0),) if single is not None else tuple(
                (bx, by) for by in range(component.v)
                for bx in range(component.h))
            for bx, by in blocks:
                size = bits.decode(table)
                if size > 16:
                    raise ImageError("JPEG DC coefficient size is impossible")
                component.pred += extend(bits.receive(size), size)
                if skip_ac:
                    _skip_ac(bits, ac_tables, component)
                if single is not None:
                    col, row = unit % across, unit // across
                else:
                    col = (unit % mcus_x) * component.h + bx
                    row = (unit // mcus_x) * component.v + by
                if col < component.cols and row < component.rows:
                    dc = (component.pred << shift) * quant[component.quant][0]
                    # The DC coefficient of a block is eight times its mean,
                    # and JPEG stores samples shifted down by 128.
                    value = (dc >> 3) + 128
                    component.plane[row * component.cols + col] = \
                        0 if value < 0 else 255 if value > 255 else value


def _skip_ac(bits, ac_tables, component) -> None:
    """Consume the 63 AC coefficients without keeping any of them.

    They cannot simply be stepped over: their Huffman codes are what say how
    long the block is, so the only way to find the next block is to decode
    them.  Dropping them is still most of the saving -- what is skipped is the
    dequantise and the inverse DCT.
    """
    table = ac_tables.get(component.ac_table)
    if table is None:
        raise ImageError("JPEG scan names a Huffman table it never sent")
    k = 1
    while k <= 63:
        symbol = bits.decode(table)
        run, size = symbol >> 4, symbol & 15
        if size == 0:
            if run != 15:               # end of block
                return
            k += 16                     # a run of sixteen zeroes
            continue
        k += run + 1
        bits.receive(size)


def _to_rgb(components, width, height, transform):
    """Combine the component planes into RGB at 1/8 scale."""
    out_w = max(1, -(-width // 8))
    out_h = max(1, -(-height // 8))
    hmax = max(c.h for c in components)
    vmax = max(c.v for c in components)
    pixels = bytearray(out_w * out_h * 3)

    def sample(component, x, y):
        col = min(component.cols - 1, x * component.h // hmax)
        row = min(component.rows - 1, y * component.v // vmax)
        return component.plane[row * component.cols + col]

    for y in range(out_h):
        for x in range(out_w):
            at = (y * out_w + x) * 3
            if len(components) == 1:
                grey = sample(components[0], x, y)
                pixels[at] = pixels[at + 1] = pixels[at + 2] = grey
            elif len(components) == 3 and transform == 0:
                for i in range(3):
                    pixels[at + i] = sample(components[i], x, y)
            elif len(components) == 3:
                pixels[at:at + 3] = _ycbcr(sample(components[0], x, y),
                                           sample(components[1], x, y),
                                           sample(components[2], x, y))
            else:
                pixels[at:at + 3] = _cmyk(
                    [sample(c, x, y) for c in components], transform)
    return pixels, out_w, out_h


def _ycbcr(y: int, cb: int, cr: int) -> bytes:
    r = y + 1.402 * (cr - 128)
    g = y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    return bytes(0 if v < 0 else 255 if v > 255 else int(v + 0.5)
                 for v in (r, g, b))


def _cmyk(values, transform) -> bytes:
    """Adobe four-component JPEG, which is stored inverted."""
    if transform == 2:                  # YCCK: the colour part is YCbCr
        r, g, b = _ycbcr(values[0], values[1], values[2])
        c, m, yel = 255 - r, 255 - g, 255 - b
    else:
        c, m, yel = values[0], values[1], values[2]
    k = values[3]
    return bytes(max(0, min(255, v * k // 255)) for v in (255 - c, 255 - m,
                                                          255 - yel))


def read_jpeg(data: bytes) -> Image:
    quant: dict[int, list[int]] = {}
    dc_tables: dict[int, _Huffman] = {}
    ac_tables: dict[int, _Huffman] = {}
    components: list[_Component] = []
    width = height = 0
    kind = ""
    progressive = False
    transform = None
    orientation = 1
    restart_interval = 0
    scanned = False

    for marker, payload, after in _markers(data):
        if marker == 0xDB:                                  # quantisation
            at = 0
            while at < len(payload):
                precision, ident = payload[at] >> 4, payload[at] & 15
                step = 128 if precision else 64
                body = payload[at + 1:at + 1 + step]
                if len(body) < step:
                    raise ImageError("truncated JPEG quantisation table")
                quant[ident] = (list(struct.unpack(">64H", body)) if precision
                                else list(body))
                at += 1 + step
        elif marker == 0xC4:                                # Huffman
            at = 0
            while at + 17 <= len(payload):
                kindbits, ident = payload[at] >> 4, payload[at] & 15
                counts = payload[at + 1:at + 17]
                total = sum(counts)
                values = payload[at + 17:at + 17 + total]
                if len(values) < total:
                    raise ImageError("truncated JPEG Huffman table")
                table = _Huffman(counts, values)
                (ac_tables if kindbits else dc_tables)[ident] = table
                at += 17 + total
        elif marker in SOF_MARKERS:
            if marker not in SOF_SUPPORTED:
                raise ImageError(
                    f"JPEG frame type 0x{marker:02x} is not one this decodes")
            kind = SOF_SUPPORTED[marker]
            progressive = marker == 0xC2
            width, height, components = _read_frame(payload)
            _guard_size(width, height)
        elif marker == 0xDD and len(payload) >= 2:          # restart interval
            (restart_interval,) = struct.unpack(">H", payload[:2])
        elif marker == 0xE1:                                # APP1: EXIF
            orientation = _orientation(payload)
        elif marker == 0xEE and payload.startswith(b"Adobe"):
            transform = payload[11] if len(payload) > 11 else 0
        elif marker == 0xDA:                                # start of scan
            if not components:
                raise ImageError("JPEG scan arrived before its frame header")
            scan = _prepare_scan(payload, components, quant)
            hmax = max(c.h for c in components)
            vmax = max(c.v for c in components)
            mcus_x = max(1, -(-width // (8 * hmax)))
            mcus_y = max(1, -(-height // (8 * vmax)))
            for component in components:
                component.cols = mcus_x * component.h
                component.rows = mcus_y * component.v
                component.plane = [128] * (component.cols * component.rows)
            shift = payload[-1] & 15 if progressive else 0
            try:
                _decode_scan(_Bits(data, after), scan, mcus_x, mcus_y,
                             hmax, vmax, width, height, quant, dc_tables,
                             ac_tables, restart_interval,
                             skip_ac=not progressive, shift=shift)
            except _EndOfData:
                # A truncated file still shows whatever arrived, which is more
                # use than an error: the top of the image is already there.
                pass
            scanned = True
            break                       # one DC scan is all this needs

    if not components:
        raise ImageError("JPEG has no frame header")
    if not scanned:
        raise ImageError("JPEG has no scan")
    if transform is None:
        # Without an Adobe marker, three components mean YCbCr by convention.
        transform = 1 if len(components) == 3 else 0
    pixels, out_w, out_h = _to_rgb(components, width, height, transform)
    quarters, mirrored = ORIENTATIONS.get(orientation, (0, False))
    if quarters or mirrored:
        pixels, out_w, out_h = _turn(pixels, out_w, out_h, quarters, mirrored)
    detail = f"{kind}, {len(components)} component"
    detail += "s" if len(components) != 1 else ""
    # The frames are one eighth of the file's size; "full" carries the real
    # one so the title reports the image rather than the thumbnail.
    turned = quarters % 2 == 1
    return Image(out_w, out_h, "JPEG", detail, [Frame(pixels)],
                 full=(height, width) if turned else (width, height))


def _prepare_scan(payload: bytes, components, quant):
    """Match a scan header's component list to the frame's components."""
    if not payload:
        raise ImageError("truncated JPEG scan header")
    count = payload[0]
    if len(payload) < 1 + count * 2 + 3:
        raise ImageError("truncated JPEG scan header")
    scan = []
    for i in range(count):
        ident, tables = payload[1 + i * 2:3 + i * 2]
        for component in components:
            if component.ident == ident:
                component.dc_table = tables >> 4
                component.ac_table = tables & 15
                component.pred = 0
                if component.quant not in quant:
                    raise ImageError(
                        "JPEG names a quantisation table it never sent")
                scan.append(component)
                break
        else:
            raise ImageError("JPEG scan names a component not in the frame")
    return scan
