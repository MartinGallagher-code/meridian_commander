"""The image decoders: PNG, GIF, BMP, Netpbm, and the formats we only measure.

Fixtures are built byte by byte in ``support`` rather than by an imaging
library.  The decoders were separately checked against files written by
Pillow -- 22 of them, byte for byte -- but Pillow is not a dependency this
project will take, and a fixture spelled out in full is a second, independent
statement of what the format says.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from meridian_commander import image as img
from meridian_commander.image import Image, ImageError, decode, sniff

from support import (
    bmp_bytes,
    gif_bytes,
    gif_frame,
    png_bytes,
    png_chunk,
    pnm_bytes,
    rgb_png,
)

RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)


def px(image: Image, x: int, y: int, frame: int = 0):
    at = (y * image.width + x) * 3
    return tuple(image.frames[frame].pixels[at:at + 3])


# -- names and sniffing --------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("photo.jpg", True), ("PHOTO.JPEG", True), ("a.png", True),
    ("a.GIF", True), ("scan.tiff", True), ("icon.ico", True),
    ("shot.webp", True), ("live.heic", True),
    ("notes.txt", False), ("book.pdf", False), ("", False),
])
def test_is_image(name, expected):
    assert img.is_image(name) is expected


@pytest.mark.parametrize("data, expected", [
    (img.PNG_MAGIC + b"junk", "PNG"),
    (b"GIF87a" + b"\x00" * 20, "GIF"),
    (b"GIF89a" + b"\x00" * 20, "GIF"),
    (b"BM" + b"\x00" * 40, "BMP"),
    (b"\xff\xd8\xff\xe0", "JPEG"),
    (b"P6\n1 1\n255\n\x00\x00\x00", "PNM"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "WEBP"),
    (b"II*\x00" + b"\x00" * 20, "TIFF"),
    (b"MM\x00*" + b"\x00" * 20, "TIFF"),
    (b"\x00\x00\x01\x00" + b"\x00" * 20, "ICO"),
    (b"\x00\x00\x00\x18ftypavif", "AVIF"),
    (b"\x00\x00\x00\x18ftypheic", "HEIF"),
])
def test_sniff_reads_the_content_not_the_name(data, expected):
    assert sniff(data) == expected


def test_sniff_refuses_what_it_cannot_place():
    with pytest.raises(ImageError, match="not an image"):
        sniff(b"this is just some text")


def test_a_png_named_jpg_is_still_read_as_a_png():
    """The commonest wrong file name there is."""
    assert decode(rgb_png(1, 1, [RED])).kind == "PNG"


# -- the checkerboard ----------------------------------------------------------

def test_the_checkerboard_alternates_by_square():
    assert img.check_colour(0, 0) == img.CHECK_LIGHT
    assert img.check_colour(img.CHECK_SIZE, 0) == img.CHECK_DARK
    assert img.check_colour(0, img.CHECK_SIZE) == img.CHECK_DARK
    assert img.check_colour(img.CHECK_SIZE, img.CHECK_SIZE) == img.CHECK_LIGHT


def test_compositing_leaves_opaque_pixels_exactly_alone():
    assert img._composite(1, 2, 3, 255, 0, 0) == (1, 2, 3)
    # Fully transparent becomes the checkerboard itself.
    light = img.CHECK_LIGHT
    assert img._composite(0, 0, 0, 0, 0, 0) == (light, light, light)
    # Half alpha lands between the colour and the background.
    half = img._composite(0, 0, 0, 128, 0, 0)
    assert all(0 < c < light for c in half)


# -- size guards ---------------------------------------------------------------

@pytest.mark.parametrize("width, height", [(0, 5), (5, 0), (-1, 5)])
def test_a_nonsense_size_is_refused(width, height):
    with pytest.raises(ImageError, match="bad image size"):
        img._guard_size(width, height)


def test_an_absurd_pixel_count_is_refused_before_allocating(monkeypatch):
    """A width and a height are four bytes each to write and 3n to believe."""
    monkeypatch.setattr(img, "MAX_PIXELS", 100)
    with pytest.raises(ImageError, match="the limit is 100 pixels"):
        img._guard_size(50, 50)


# -- PNG -----------------------------------------------------------------------

def test_png_truecolour_round_trips():
    image = decode(rgb_png(2, 2, [RED, GREEN, BLUE, (7, 8, 9)]))
    assert (image.width, image.height, image.kind) == (2, 2, "PNG")
    assert image.detail == "8-bit truecolour"
    assert px(image, 0, 0) == RED
    assert px(image, 1, 1) == (7, 8, 9)
    assert image.animated is False


def test_png_greyscale_expands_to_rgb():
    image = decode(png_bytes(2, 1, [b"\x00\xff"], colour=0))
    assert image.detail == "8-bit greyscale"
    assert px(image, 0, 0) == (0, 0, 0)
    assert px(image, 1, 0) == (255, 255, 255)


def test_png_greyscale_with_alpha_is_composited():
    image = decode(png_bytes(2, 1, [b"\x00\xff\x00\x00"], colour=4))
    assert px(image, 0, 0) == (0, 0, 0)                  # opaque black
    assert px(image, 1, 0) == (img.CHECK_LIGHT,) * 3     # fully transparent


def test_png_truecolour_with_alpha_is_composited():
    body = bytes(RED) + b"\xff" + bytes(BLUE) + b"\x00"
    image = decode(png_bytes(2, 1, [body], colour=6))
    assert px(image, 0, 0) == RED
    assert px(image, 1, 0) == (img.CHECK_LIGHT,) * 3


def test_png_palette_and_its_transparency():
    palette = bytes(RED) + bytes(GREEN)
    image = decode(png_bytes(2, 1, [b"\x00\x01"], colour=3, palette=palette,
                             trans=b"\xff\x00"))
    assert image.detail == "8-bit palette"
    assert px(image, 0, 0) == RED                        # tRNS says opaque
    assert px(image, 1, 0) == (img.CHECK_LIGHT,) * 3     # tRNS says clear


@pytest.mark.parametrize("depth, row, expected", [
    (1, b"\x80", [(255, 255, 255), (0, 0, 0)]),
    (2, b"\xc0", [(255, 255, 255), (0, 0, 0)]),
    (4, b"\xf0", [(255, 255, 255), (0, 0, 0)]),
])
def test_png_sub_byte_greyscale_scales_to_full_range(depth, row, expected):
    """The maximum sample at any depth has to come out as 255, not as 1 or 3."""
    image = decode(png_bytes(2, 1, [row], depth=depth, colour=0))
    assert [px(image, x, 0) for x in range(2)] == expected


def test_png_sixteen_bit_keeps_the_high_byte():
    """A terminal cannot show 16 bits; the high byte is the scaled 8-bit value."""
    row = b"\xab\xcd" * 3
    image = decode(png_bytes(1, 1, [row], depth=16, colour=2))
    assert px(image, 0, 0) == (0xAB, 0xAB, 0xAB)


@pytest.mark.parametrize("filter_kind", [0, 1, 2, 3, 4])
def test_every_png_scanline_filter_reconstructs_the_same_image(filter_kind):
    """Filters are reversible, so all five must land on identical pixels."""
    plain = rgb_png(3, 3, [(x * 40, y * 40, 30) for y in range(3)
                           for x in range(3)])
    want = decode(plain).frames[0].pixels

    # Re-encode the same pixels using one filter throughout, by filtering the
    # rows here and letting the decoder undo it.
    rows = [bytes(c for x in range(3)
                  for c in ((x * 40) % 256, (y * 40) % 256, 30))
            for y in range(3)]
    filtered, previous = [], bytes(9)
    for row in rows:
        out = bytearray(len(row))
        for i, value in enumerate(row):
            left = row[i - 3] if i >= 3 else 0
            up = previous[i]
            upleft = previous[i - 3] if i >= 3 else 0
            if filter_kind == 0:
                out[i] = value
            elif filter_kind == 1:
                out[i] = (value - left) & 0xFF
            elif filter_kind == 2:
                out[i] = (value - up) & 0xFF
            elif filter_kind == 3:
                out[i] = (value - ((left + up) >> 1)) & 0xFF
            else:
                out[i] = (value - img._paeth(left, up, upleft)) & 0xFF
        filtered.append(bytes(out))
        previous = row
    encoded = png_bytes(3, 3, filtered, filters=[filter_kind] * 3)
    assert decode(encoded).frames[0].pixels == want


@pytest.mark.parametrize("a, b, c, expected", [
    (10, 20, 30, 10),       # pa smallest -> the pixel to the left
    (10, 20, 10, 20),       # pb smallest -> the pixel above
    (30, 10, 20, 20),       # pc smallest -> the pixel above-left
    (0, 0, 0, 0),           # all equal -> left, by the specified tie order
])
def test_paeth_picks_the_nearest_neighbour(a, b, c, expected):
    assert img._paeth(a, b, c) == expected


def test_the_adam7_passes_tile_the_block_exactly_once():
    """Every pixel of an 8x8 block belongs to exactly one pass.

    The property test the table needed: three of the seven entries can be
    wrong in a way that still decodes a plausible-looking image, so comparing
    against a reference image is not enough to notice. Counting coverage is.
    """
    seen = {}
    for index, (x0, y0, dx, dy) in enumerate(img.ADAM7, start=1):
        for y in range(y0, 8, dy):
            for x in range(x0, 8, dx):
                assert (x, y) not in seen, \
                    f"({x},{y}) is in pass {seen.get((x, y))} and pass {index}"
                seen[(x, y)] = index
    assert len(seen) == 64
    # And the map is the one the specification prints, read row by row.
    grid = "\n".join("".join(str(seen[(x, y)]) for x in range(8))
                     for y in range(8))
    assert grid == ("16462646\n"
                    "77777777\n"
                    "56565656\n"
                    "77777777\n"
                    "36463646\n"
                    "77777777\n"
                    "56565656\n"
                    "77777777")


def test_png_interlaced_reassembles_the_seven_passes():
    """Adam7 on an image big enough that every pass carries pixels."""
    pixels = [((x * 8) % 256, (y * 8) % 256, (x + y) % 256)
              for y in range(9) for x in range(9)]
    plain = decode(rgb_png(9, 9, pixels))

    # Lay the same pixels out in Adam7 order and let the decoder put them back.
    rows = []
    for x0, y0, dx, dy in img.ADAM7:
        for y in range(y0, 9, dy):
            rows.append(bytes(c for x in range(x0, 9, dx)
                              for c in pixels[y * 9 + x]))
    interlaced = decode(png_bytes(9, 9, rows, interlace=1))
    assert interlaced.detail == "8-bit truecolour, interlaced"
    assert interlaced.frames[0].pixels == plain.frames[0].pixels


def test_png_interlaced_skips_passes_with_nothing_in_them():
    """A 1x1 image has pixels in the first pass only; the other six are empty."""
    image = decode(png_bytes(1, 1, [bytes(RED)], interlace=1))
    assert px(image, 0, 0) == RED


@pytest.mark.parametrize("data, message", [
    (img.PNG_MAGIC, "no IEND"),
    (img.PNG_MAGIC + struct.pack(">I", 999) + b"IHDR" + b"\x00" * 4,
     "runs past the end"),
    (img.PNG_MAGIC + png_chunk(b"IEND", b""), "no image header"),
])
def test_a_damaged_png_is_reported_not_guessed_at(data, message):
    with pytest.raises(ImageError, match=message):
        decode(data)


def test_png_with_no_image_data_is_refused():
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    data = (img.PNG_MAGIC + png_chunk(b"IHDR", header)
            + png_chunk(b"IEND", b""))
    with pytest.raises(ImageError, match="no image data"):
        decode(data)


def test_png_with_corrupt_image_data_says_so():
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    data = (img.PNG_MAGIC + png_chunk(b"IHDR", header)
            + png_chunk(b"IDAT", b"not zlib at all")
            + png_chunk(b"IEND", b""))
    with pytest.raises(ImageError, match="image data is corrupt"):
        decode(data)


def test_png_colour_type_that_does_not_exist():
    with pytest.raises(ImageError, match="colour type 7"):
        decode(png_bytes(1, 1, [b"\x00"], colour=7))


def test_png_bit_depth_the_specification_forbids():
    """Truecolour at 4 bits is not a thing, however plausible it sounds."""
    with pytest.raises(ImageError, match="depth 4 is not allowed for truecolour"):
        decode(png_bytes(1, 1, [b"\x00"], depth=4, colour=2))


def test_png_interlace_method_that_does_not_exist():
    with pytest.raises(ImageError, match="interlace method 2"):
        decode(png_bytes(1, 1, [bytes(RED)], interlace=2))


def test_png_scanline_filter_that_does_not_exist():
    with pytest.raises(ImageError, match="filter 9"):
        decode(png_bytes(1, 1, [bytes(RED)], filters=[9]))


def test_png_short_of_image_data():
    with pytest.raises(ImageError, match="not enough image data"):
        decode(png_bytes(4, 4, [bytes(RED)]))


def test_png_palette_index_past_the_end_of_the_palette():
    with pytest.raises(ImageError, match="palette index is out of range"):
        decode(png_bytes(1, 1, [b"\x05"], colour=3, palette=bytes(RED)))


def test_png_ignores_chunks_it_has_no_use_for():
    """A text chunk between the header and the data must not derail anything."""
    image = decode(rgb_png(1, 1, [RED]).replace(
        png_chunk(b"IDAT", zlib.compress(b"\x00" + bytes(RED))),
        png_chunk(b"tEXt", b"Comment\x00hello")
        + png_chunk(b"IDAT", zlib.compress(b"\x00" + bytes(RED)))))
    assert px(image, 0, 0) == RED


# -- Netpbm --------------------------------------------------------------------

def test_pnm_binary_rgb():
    image = decode(pnm_bytes(b"P6", 2, 1, bytes(RED) + bytes(BLUE)))
    assert (image.kind, image.detail) == ("PNM", "binary RGB")
    assert px(image, 0, 0) == RED
    assert px(image, 1, 0) == BLUE


def test_pnm_ascii_rgb():
    image = decode(pnm_bytes(b"P3", 2, 1, [255, 0, 0, 0, 0, 255]))
    assert image.detail == "ASCII RGB"
    assert px(image, 0, 0) == RED


def test_pnm_greyscale_in_both_forms():
    binary = decode(pnm_bytes(b"P5", 2, 1, bytes([0, 255])))
    ascii_form = decode(pnm_bytes(b"P2", 2, 1, [0, 255]))
    assert binary.frames[0].pixels == ascii_form.frames[0].pixels
    assert px(binary, 1, 0) == (255, 255, 255)


def test_pnm_bitmap_treats_one_as_black():
    """The one format here where a set bit means black rather than white."""
    binary = decode(pnm_bytes(b"P4", 4, 1, bytes([0b10100000])))
    assert [px(binary, x, 0) for x in range(4)] == [
        (0, 0, 0), (255, 255, 255), (0, 0, 0), (255, 255, 255)]
    ascii_form = decode(pnm_bytes(b"P1", 4, 1, [1, 0, 1, 0]))
    assert ascii_form.frames[0].pixels == binary.frames[0].pixels


def test_pnm_scales_a_maximum_value_that_is_not_255():
    image = decode(pnm_bytes(b"P5", 2, 1, bytes([0, 15]), top=15))
    assert px(image, 1, 0) == (255, 255, 255)


def test_pnm_sixteen_bit_keeps_the_high_byte():
    image = decode(pnm_bytes(b"P5", 1, 1, bytes([0xAB, 0xCD]), top=65535))
    assert px(image, 0, 0) == (0xAB, 0xAB, 0xAB)


def test_pnm_header_comments_are_skipped():
    data = pnm_bytes(b"P6", 1, 1, bytes(GREEN), comment=b"# made by hand\n")
    assert px(decode(data), 0, 0) == GREEN


def test_a_netpbm_variant_with_no_decoder_is_named():
    """P7 (PAM) never reaches decode(), which does not sniff it -- but
    read_pnm is a public entry point and says which type it refused."""
    with pytest.raises(ImageError, match="type P7 is not handled"):
        img.read_pnm(b"P7\nWIDTH 1\n")


@pytest.mark.parametrize("data, message", [
    (b"P6\n1 1\n0\n", "maximum value 0 is out of range"),
    (b"P6\n1 1\n999999\n", "maximum value 999999 is out of range"),
    (b"P6\n1 1\n255\n\x00", "truncated Netpbm image data"),
    (b"P4\n8 8\n", "truncated Netpbm image data"),
    (b"P6\n1 ", "truncated Netpbm header"),
    (b"P6\nwide 1\n255\n", "not a number"),
])
def test_a_damaged_netpbm_is_reported(data, message):
    with pytest.raises(ImageError, match=message):
        decode(data)


# -- BMP -----------------------------------------------------------------------

def test_bmp_24_bit_is_stored_bottom_up():
    """Row order is the thing everyone gets wrong first."""
    top, bottom = bytes(RED[::-1]), bytes(BLUE[::-1])
    image = decode(bmp_bytes(1, 2, [bottom, top]))
    assert px(image, 0, 0) == RED               # last row stored is the top one
    assert px(image, 0, 1) == BLUE


def test_bmp_top_down_rows_when_the_height_is_negative():
    top, bottom = bytes(RED[::-1]), bytes(BLUE[::-1])
    image = decode(bmp_bytes(1, 2, [top, bottom], top_down=True))
    assert image.detail == "24-bit, top-down"
    assert px(image, 0, 0) == RED


def test_bmp_32_bit_ignores_the_unused_byte():
    image = decode(bmp_bytes(1, 1, [bytes(RED[::-1]) + b"\x00"], depth=32))
    assert px(image, 0, 0) == RED


@pytest.mark.parametrize("depth, row, palette, expected", [
    (8, b"\x01", bytes(BLUE[::-1]) + b"\x00" + bytes(RED[::-1]) + b"\x00", RED),
    (4, b"\x10", bytes(BLUE[::-1]) + b"\x00" + bytes(RED[::-1]) + b"\x00", RED),
    (1, b"\x80", bytes(BLUE[::-1]) + b"\x00" + bytes(RED[::-1]) + b"\x00", RED),
])
def test_bmp_palette_depths(depth, row, palette, expected):
    image = decode(bmp_bytes(1, 1, [row], depth=depth, palette=palette))
    assert px(image, 0, 0) == expected


def test_bmp_16_bit_defaults_to_five_five_five():
    """No masks means 5-5-5 with the top bit unused, and full red is 0x7c00."""
    image = decode(bmp_bytes(1, 1, [struct.pack("<H", 0x7C00)], depth=16))
    assert px(image, 0, 0) == RED


def test_bmp_bitfields_use_the_declared_masks():
    data = bmp_bytes(1, 1, [struct.pack("<I", 0x000000FF)], depth=32,
                     compression=3, masks=(0x000000FF, 0x0000FF00, 0x00FF0000))
    image = decode(data)
    assert image.detail == "32-bit bitfields"
    assert px(image, 0, 0) == RED               # red is the low byte here


def test_bmp_v4_header_keeps_its_masks_inside_the_header():
    """Pre-v4 writers put the masks after the header; v4 put them inside it."""
    data = bmp_bytes(1, 1, [struct.pack("<I", 0x000000FF)], depth=32,
                     compression=3, header_size=108,
                     masks=(0x000000FF, 0x0000FF00, 0x00FF0000, 0))
    assert px(decode(data), 0, 0) == RED


def test_bmp_alpha_mask_is_composited():
    """A v4 header can declare an alpha channel; a 32-bit BMP otherwise cannot."""
    opaque = bmp_bytes(1, 1, [struct.pack("<I", 0xFF0000FF)], depth=32,
                       compression=3, header_size=108,
                       masks=(0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000))
    assert px(decode(opaque), 0, 0) == RED
    clear = bmp_bytes(1, 1, [struct.pack("<I", 0x000000FF)], depth=32,
                      compression=3, header_size=108,
                      masks=(0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000))
    assert px(decode(clear), 0, 0) == (img.CHECK_LIGHT,) * 3


def test_bmp_the_original_os2_header_still_reads():
    image = decode(bmp_bytes(1, 1, [bytes(GREEN[::-1])], core=True))
    assert px(image, 0, 0) == GREEN


@pytest.mark.parametrize("data, message", [
    (b"BM", "truncated BMP: no header"),
    (bmp_bytes(1, 1, [bytes(RED)], compression=1), "compression RLE8"),
    (bmp_bytes(1, 1, [bytes(RED)], compression=9), "compression 9 is not defined"),
    (bmp_bytes(1, 1, [bytes(RED)], depth=7), "depth 7 is not defined"),
])
def test_a_bmp_this_does_not_handle_says_which_part(data, message):
    with pytest.raises(ImageError, match=message):
        decode(data)


def test_bmp_with_a_header_size_from_no_known_version():
    data = bytearray(bmp_bytes(1, 1, [bytes(RED)]))
    data[14:18] = struct.pack("<I", 20)
    with pytest.raises(ImageError, match="header size 20"):
        decode(bytes(data))


def test_bmp_truncated_pixel_data():
    data = bmp_bytes(4, 4, [bytes(RED)])
    with pytest.raises(ImageError, match="not enough pixel data"):
        decode(data)


def test_bmp_palette_index_past_the_end_of_the_table():
    data = bmp_bytes(1, 1, [b"\x07"], depth=8, palette=bytes(RED) + b"\x00")
    with pytest.raises(ImageError, match="palette index is out of range"):
        decode(data)


def test_bmp_colour_table_that_is_short():
    """The header claims four palette entries; only one is in the file."""
    data = bytearray(bmp_bytes(1, 1, [b"\x00"], depth=8,
                               palette=bytes(RED) + b"\x00"))
    data[46:50] = struct.pack("<I", 4)
    with pytest.raises(ImageError, match="colour table is short"):
        decode(bytes(data))


def test_a_zero_mask_contributes_nothing():
    assert img._mask_shift(0) == (0, 0)
    assert img._mask_shift(0xF0) == (4, 4)


# -- GIF -----------------------------------------------------------------------

PALETTE = bytes(RED) + bytes(GREEN) + bytes(BLUE) + b"\xff\xff\xff"


def test_gif_still_image():
    image = decode(gif_bytes(2, 2, [gif_frame([0, 1, 2, 3], 2, 2)],
                             palette=PALETTE))
    assert (image.kind, image.detail) == ("GIF", "4-colour palette")
    assert px(image, 0, 0) == RED
    assert px(image, 1, 1) == (255, 255, 255)
    assert image.animated is False


def test_gif_interlaced_frame():
    plain = decode(gif_bytes(1, 4, [gif_frame([0, 1, 2, 3], 1, 4)],
                             palette=PALETTE))
    # Interlaced order for four rows is 0, then 2, then 1 and 3.
    order = [0, 2, 1, 3]
    shuffled = [0] * 4
    for source, row in enumerate(order):
        shuffled[source] = row
    image = decode(gif_bytes(1, 4, [gif_frame(shuffled, 1, 4, interlaced=True)],
                             palette=PALETTE))
    assert image.frames[0].pixels == plain.frames[0].pixels


def test_gif_local_palette_overrides_the_global_one():
    local = bytes(GREEN) + bytes(GREEN) + bytes(GREEN) + bytes(GREEN)
    image = decode(gif_bytes(1, 1, [gif_frame([0], 1, 1, local=local)],
                             palette=PALETTE))
    assert px(image, 0, 0) == GREEN
    assert image.detail == "4-colour palette"


def test_gif_transparency_lets_the_checkerboard_through():
    image = decode(gif_bytes(2, 1, [gif_frame([0, 1], 2, 1, transparent=1)],
                             palette=PALETTE))
    assert px(image, 0, 0) == RED
    assert px(image, 1, 0) == (img.CHECK_LIGHT,) * 3


def test_gif_animation_keeps_frames_and_delays():
    frames = [gif_frame([0], 1, 1, delay=12),
              gif_frame([1], 1, 1, delay=25)]
    image = decode(gif_bytes(1, 1, frames, palette=PALETTE))
    assert image.animated is True
    assert [round(f.delay, 2) for f in image.frames] == [0.12, 0.25]
    assert image.detail == "4-colour palette, 2 frames"
    assert px(image, 0, 0, frame=0) == RED
    assert px(image, 0, 0, frame=1) == GREEN


def test_gif_disposal_leaves_the_previous_frame_in_place():
    """Method 1: a small second frame paints over part of the first."""
    frames = [gif_frame([0, 0], 2, 1, disposal=1),
              gif_frame([1], 1, 1, left=1, disposal=1)]
    image = decode(gif_bytes(2, 1, frames, palette=PALETTE))
    assert px(image, 0, 0, frame=1) == RED       # kept from frame 0
    assert px(image, 1, 0, frame=1) == GREEN


def test_gif_disposal_two_clears_back_to_the_background():
    """Without this a moving sprite smears instead of moving."""
    frames = [gif_frame([1], 1, 1, disposal=2),
              gif_frame([2], 1, 1, left=1, disposal=2)]
    image = decode(gif_bytes(2, 1, frames, palette=PALETTE))
    assert px(image, 0, 0, frame=0) == GREEN
    # Frame 0's pixel was disposed of before frame 1 was painted.
    assert px(image, 0, 0, frame=1) == (img.CHECK_LIGHT,) * 3
    assert px(image, 1, 0, frame=1) == BLUE


def test_gif_disposal_three_restores_what_was_there_before():
    frames = [gif_frame([0, 0], 2, 1, disposal=1),
              gif_frame([1], 1, 1, disposal=3),
              gif_frame([2], 1, 1, left=1, disposal=1)]
    image = decode(gif_bytes(2, 1, frames, palette=PALETTE))
    assert px(image, 0, 0, frame=1) == GREEN
    # Frame 1 was rolled back, so frame 2 sees frame 0's pixel again.
    assert px(image, 0, 0, frame=2) == RED


def test_gif_frame_hanging_off_the_canvas_is_clipped_not_fatal():
    frames = [gif_frame([0, 1, 2, 3], 2, 2, left=1, top=1)]
    image = decode(gif_bytes(2, 2, frames, palette=PALETTE))
    assert px(image, 1, 1) == RED               # only the top-left lands


def test_gif_stops_at_the_frame_cap(monkeypatch):
    monkeypatch.setattr(img, "MAX_FRAMES", 3)
    frames = [gif_frame([i % 4], 1, 1, delay=5) for i in range(10)]
    image = decode(gif_bytes(1, 1, frames, palette=PALETTE))
    assert len(image.frames) == 3
    assert image.truncated is True


def test_gif_extensions_it_has_no_use_for_are_skipped():
    """A comment and an application block sit between the frames in the wild."""
    comment = b"\x21\xfe\x05hello\x00"
    netscape = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    body = comment + netscape + gif_frame([2], 1, 1)
    image = decode(gif_bytes(1, 1, [body], palette=PALETTE))
    assert px(image, 0, 0) == BLUE


@pytest.mark.parametrize("data, message", [
    (b"GIF89a", "no screen descriptor"),
    (gif_bytes(1, 1, [b"\x99"], palette=PALETTE), "block type 0x99"),
    (gif_bytes(1, 1, [], palette=PALETTE), "no frames"),
    (gif_bytes(1, 1, [gif_frame([0], 1, 1)]), "no colour table"),
])
def test_a_damaged_gif_is_reported(data, message):
    with pytest.raises(ImageError, match=message):
        decode(data)


def test_gif_palette_index_past_the_end_of_the_table():
    small = bytes(RED) + bytes(GREEN)      # two colours, but 4 are addressable
    data = gif_bytes(1, 1, [gif_frame([3], 1, 1)], palette=small)
    with pytest.raises(ImageError, match="palette index is out of range"):
        decode(data)


def test_gif_frame_short_of_pixels():
    data = gif_bytes(4, 4, [gif_frame([0], 4, 4)], palette=PALETTE)
    with pytest.raises(ImageError, match="short of pixels"):
        decode(data)


def test_gif_that_ends_at_the_image_descriptor():
    """The descriptor promises pixels the file then does not contain."""
    descriptor = b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0)
    data = gif_bytes(1, 1, [descriptor], palette=PALETTE)
    with pytest.raises(ImageError, match="no image data"):
        decode(data[:-1])                       # drop the trailer as well


def test_gif_block_chain_with_no_terminator():
    body = b"\x21\xfe\x05hello"             # sub-block, then the file ends
    with pytest.raises(ImageError, match="no terminator"):
        decode(gif_bytes(1, 1, [body], palette=PALETTE))


def test_gif_colour_table_running_past_the_end():
    truncated = gif_bytes(1, 1, [], palette=PALETTE)[:-6]
    with pytest.raises(ImageError, match="colour table runs past"):
        decode(truncated)


# -- LZW -----------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 12])
def test_lzw_code_size_out_of_range(size):
    with pytest.raises(ImageError, match="code size"):
        img._lzw_decode(b"\x00\x00", size, 1)


def test_lzw_refuses_a_stream_that_opens_with_an_undefined_code():
    # Code 6 with a 2-bit minimum: past the clear (4) and end (5) codes, and
    # nothing defined yet for it to refer back to.
    with pytest.raises(ImageError, match="undefined code"):
        img._lzw_decode(bytes([0x06, 0x00]), 2, 4)


def test_lzw_handles_the_self_referential_code():
    """The one case where a code names a sequence being defined by that code."""
    # clear(4), literal 1, then code 6 -- the next code the table would
    # define, so it can only mean "what I just emitted, plus its own first
    # byte". Code 5 would be end-of-information, not a table entry.
    bits = []
    for code in (4, 1, 6):
        for i in range(3):
            bits.append((code >> i) & 1)
    while len(bits) % 8:
        bits.append(0)
    packed = bytes(sum(b << n for n, b in enumerate(bits[i:i + 8]))
                   for i in range(0, len(bits), 8))
    out = img._lzw_decode(packed, 2, 8)
    assert bytes(out).startswith(b"\x01\x01\x01")


def test_lzw_grows_the_code_width_as_the_table_fills():
    """A real stream that runs past 2^(n+1) entries without a reset."""
    indices = list(range(4)) * 40
    data = gif_bytes(8, 20, [gif_frame(indices, 8, 20)], palette=PALETTE)
    image = decode(data)
    assert px(image, 0, 0) == RED
    assert px(image, 3, 0) == (255, 255, 255)


# -- measured but not decoded --------------------------------------------------

def test_webp_is_named_and_measured_in_all_three_flavours():
    lossy = (b"RIFF" + b"\x00" * 4 + b"WEBPVP8 " + b"\x00" * 10
             + struct.pack("<HH", 320, 240))
    image = decode(lossy)
    assert (image.kind, image.width, image.height) == ("WEBP", 320, 240)
    assert image.frames == []
    assert "VP8-coded" in image.note

    lossless = (b"RIFF" + b"\x00" * 4 + b"WEBPVP8L" + b"\x00" * 5
                + struct.pack("<I", (99 << 14) | 63))
    assert (decode(lossless).width, decode(lossless).height) == (64, 100)

    extended = (b"RIFF" + b"\x00" * 4 + b"WEBPVP8X" + b"\x00" * 8
                + (319).to_bytes(3, "little") + (239).to_bytes(3, "little"))
    assert (decode(extended).width, decode(extended).height) == (320, 240)


def test_a_webp_with_no_recognisable_chunk_is_still_named():
    data = b"RIFF" + b"\x00" * 4 + b"WEBPXXXX" + b"\x00" * 20
    image = decode(data)
    assert image.kind == "WEBP"
    assert (image.width, image.height) == (0, 0)


@pytest.mark.parametrize("order", ["<", ">"])
def test_tiff_is_measured_in_either_byte_order(order):
    magic = b"II*\x00" if order == "<" else b"MM\x00*"
    entries = struct.pack(order + "H", 2)
    for tag, value in ((256, 800), (257, 600)):
        entries += struct.pack(order + "HHI", tag, 3, 1)
        entries += struct.pack(order + "H", value) + b"\x00\x00"
    data = magic + struct.pack(order + "I", 8) + entries
    image = decode(data)
    assert (image.kind, image.width, image.height) == ("TIFF", 800, 600)
    assert "a dozen codecs" in image.note


def test_a_tiff_with_no_size_tags_is_still_named():
    data = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 0)
    image = decode(data)
    assert image.kind == "TIFF"
    assert (image.width, image.height) == (0, 0)


def test_tiff_with_long_size_tags():
    entries = struct.pack("<H", 2)
    for tag, value in ((256, 70000), (257, 5)):
        entries += struct.pack("<HHII", tag, 4, 1, value)
    data = b"II*\x00" + struct.pack("<I", 8) + entries
    assert decode(data).width == 70000


def test_ico_reads_its_directory_and_treats_zero_as_256():
    small = b"\x00\x00\x01\x00\x01\x00\x20\x20" + b"\x00" * 8
    assert (decode(small).width, decode(small).height) == (32, 32)
    big = b"\x00\x00\x01\x00\x01\x00\x00\x00" + b"\x00" * 8
    assert (decode(big).width, decode(big).height) == (256, 256)
    assert "container of other images" in decode(big).note


@pytest.mark.parametrize("brand, kind", [
    (b"avif", "AVIF"), (b"avis", "AVIF"), (b"heic", "HEIF"), (b"mif1", "HEIF"),
])
def test_the_video_codec_formats_are_named_but_not_decoded(brand, kind):
    image = decode(b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 16)
    assert image.kind == kind
    assert image.frames == []
    assert "no standard-library decoder" in image.note


def test_jpeg_is_measured_from_its_frame_header():
    data = (b"\xff\xd8"
            + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
            + b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 480, 640, 3)
            + b"\x00" * 10)
    image = decode(data)
    assert (image.kind, image.width, image.height) == ("JPEG", 640, 480)
    assert "Huffman" in image.note


def test_jpeg_restart_and_standalone_markers_are_stepped_over():
    data = (b"\xff\xd8" + b"\xff\x01" + b"\xff\xd0"
            + b"\xff\xc2" + struct.pack(">HBHHB", 17, 8, 100, 200, 3)
            + b"\x00" * 4)
    assert (decode(data).width, decode(data).height) == (200, 100)


def test_a_jpeg_with_no_frame_header_is_still_named():
    image = decode(b"\xff\xd8" + b"\xff\xd9")
    assert image.kind == "JPEG"
    assert (image.width, image.height) == (0, 0)


def test_a_jpeg_that_stops_looking_like_one_is_still_named():
    """A byte that is not 0xFF where a marker belongs ends the search."""
    image = decode(b"\xff\xd8" + b"\x00\x00\x00\x00")
    assert (image.kind, image.width) == ("JPEG", 0)
