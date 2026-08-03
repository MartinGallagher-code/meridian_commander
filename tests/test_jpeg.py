"""The DC-only JPEG decoder.

Fixtures come from the miniature encoder in ``support``: every block carries a
DC coefficient and an immediate end-of-block, which is exactly what this
decoder reads and lets a test say what each output pixel should be.

The decoder was separately measured against Pillow's full decode, box-filtered
to the same 1/8 scale -- under 1 in 255 mean error per channel without chroma
subsampling, about 6 on photographic content at 4:2:0.  That is a statement
about fidelity; these are the statements about behaviour.
"""

from __future__ import annotations

import struct

import pytest

from meridian_commander import jpeg
from meridian_commander.image import ImageError, decode

from support import JPEG_AC_VALUES, exif_bytes, jpeg_bytes, jpeg_segment


def px(image, x=0, y=0):
    at = (y * image.width + x) * 3
    return tuple(image.frames[0].pixels[at:at + 3])


def grey(value: int) -> int:
    """What a DC coefficient of ``value`` becomes as a sample."""
    return (value >> 3) + 128


# -- the shape of the result ---------------------------------------------------

def test_a_single_block_becomes_a_single_pixel():
    image = decode(jpeg_bytes(8, 8, [[80]]))
    assert (image.width, image.height, image.kind) == (1, 1, "JPEG")
    assert image.detail == "baseline, 1 component"
    # The frames are the thumbnail; "size" reports the file itself.
    assert image.size == (8, 8) and image.reduced
    assert px(image) == (grey(80),) * 3


def test_the_result_is_the_image_at_one_eighth():
    image = decode(jpeg_bytes(32, 16, [[10, 20, 30, 40, 50, 60, 70, 80]]))
    assert (image.width, image.height) == (4, 2)
    assert px(image, 0, 0) == (grey(10),) * 3
    assert px(image, 3, 1) == (grey(80),) * 3


def test_a_size_that_is_not_a_multiple_of_eight_rounds_up():
    """37 pixels is four full blocks and a fifth that is mostly padding."""
    image = decode(jpeg_bytes(37, 9, [[1] * 10]))
    assert (image.width, image.height) == (5, 2)


def test_dc_values_are_differences_not_absolutes():
    """Each block stores its difference from the last, which is the point."""
    image = decode(jpeg_bytes(24, 8, [[100, 100, 8]]))
    assert [px(image, x, 0)[0] for x in range(3)] == [
        grey(100), grey(100), grey(8)]


def test_a_negative_coefficient_survives_the_sign_convention():
    image = decode(jpeg_bytes(16, 8, [[0, -200]]))
    assert px(image, 1, 0)[0] == max(0, grey(-200))


def test_the_quantisation_table_scales_the_coefficient():
    plain = decode(jpeg_bytes(8, 8, [[10]], quality=1))
    scaled = decode(jpeg_bytes(8, 8, [[10]], quality=8))
    assert px(plain)[0] == grey(10)
    assert px(scaled)[0] == grey(80)


@pytest.mark.parametrize("value, expected", [
    (0, 0), (1, 1), (3, 3),         # top half of the range is positive
    (0, 0),
])
def test_extend_leaves_positive_values_alone(value, expected):
    assert jpeg.extend(value, 2) == expected or value < 2


def test_extend_makes_the_bottom_half_negative():
    # With two bits, 0 and 1 are negative; 2 and 3 are positive.
    assert jpeg.extend(0, 2) == -3
    assert jpeg.extend(1, 2) == -2
    assert jpeg.extend(2, 2) == 2
    assert jpeg.extend(3, 2) == 3
    assert jpeg.extend(0, 0) == 0


# -- colour --------------------------------------------------------------------

def test_one_component_is_greyscale():
    image = decode(jpeg_bytes(8, 8, [[64]]))
    assert px(image)[0] == px(image)[1] == px(image)[2]


def test_three_components_are_ycbcr_by_convention():
    """No Adobe marker and three components means YCbCr, always."""
    # Y at mid, Cb and Cr at zero difference -> neutral grey.
    neutral = decode(jpeg_bytes(8, 8, [[0], [0], [0]]))
    assert px(neutral) == (128, 128, 128)
    # Pushing Cr up turns the pixel red and pulls green down.
    red = decode(jpeg_bytes(8, 8, [[0], [0], [400]]))
    assert red.frames[0].pixels[0] > 128
    assert red.frames[0].pixels[1] < 128


def test_an_adobe_transform_of_zero_means_the_data_is_already_rgb():
    image = decode(jpeg_bytes(8, 8, [[400], [0], [0]], adobe=0))
    assert px(image) == (grey(400), 128, 128)


def test_four_components_are_cmyk():
    # JPEG samples are shifted down by 128, so "no ink" is a DC of -1024,
    # not zero -- zero is mid-scale.
    image = decode(jpeg_bytes(8, 8, [[-1024], [-1024], [-1024], [1016]],
                              adobe=0))
    assert image.detail.startswith("baseline, 4 components")
    # No ink, with the key channel full, leaves white.
    assert px(image) == (255, 255, 255)


def test_four_components_with_transform_two_are_ycck():
    image = decode(jpeg_bytes(8, 8, [[0], [0], [0], [1016]], adobe=2))
    assert px(image) == (128, 128, 128)


@pytest.mark.parametrize("y, cb, cr, expected", [
    (0, 128, 128, (0, 0, 0)),
    (255, 128, 128, (255, 255, 255)),
    (128, 128, 128, (128, 128, 128)),
])
def test_ycbcr_conversion(y, cb, cr, expected):
    assert tuple(jpeg._ycbcr(y, cb, cr)) == expected


def test_ycbcr_clamps_rather_than_wrapping():
    """Out-of-gamut combinations are legal in the file and must not wrap."""
    assert all(0 <= c <= 255 for c in jpeg._ycbcr(255, 0, 255))
    assert all(0 <= c <= 255 for c in jpeg._ycbcr(0, 255, 0))


# -- sampling factors ----------------------------------------------------------

def test_chroma_subsampling_is_upsampled_to_the_luma_grid():
    """4:2:0 -- one chroma block covers four luma blocks."""
    image = decode(jpeg_bytes(16, 16, [[0, 0, 0, 0], [0], [400]],
                              sampling=[(2, 2), (1, 1), (1, 1)]))
    assert (image.width, image.height) == (2, 2)
    # Every one of the four pixels takes its colour from the single chroma
    # block, so they are all the same and all reddish.
    corners = [px(image, x, y) for y in range(2) for x in range(2)]
    assert len(set(corners)) == 1
    assert corners[0][0] > 128


def test_no_subsampling_gives_each_block_its_own_chroma():
    image = decode(jpeg_bytes(16, 8, [[0, 0], [0, 0], [0, 400]],
                              sampling=[(1, 1), (1, 1), (1, 1)]))
    assert px(image, 0, 0) == (128, 128, 128)
    assert px(image, 1, 0)[0] > 128


@pytest.mark.parametrize("sampling, message", [
    (0x01, "sampling factor 0x1"),      # a zero factor would divide by zero
    (0x51, "sampling factor 5x1"),      # four is the largest the spec allows
])
def test_a_sampling_factor_out_of_range_is_refused(sampling, message):
    # Built by hand: the fixture encoder would divide by the bad factor first.
    frame = struct.pack(">BHHB", 8, 8, 8, 1) + bytes([1, sampling, 0])
    with pytest.raises(ImageError, match=message):
        decode(b"\xff\xd8" + jpeg_segment(0xC0, frame) + b"\xff\xd9")


# -- scan structure ------------------------------------------------------------

def test_restart_markers_reset_the_predictor():
    """After a restart the difference is from zero again, not from the last."""
    image = decode(jpeg_bytes(32, 8, [[10, 20, 30, 40]], restart=2))
    assert [px(image, x, 0)[0] for x in range(4)] == [
        grey(10), grey(20), grey(30), grey(40)]


def test_a_progressive_first_scan_is_dc_only_and_needs_no_ac():
    image = decode(jpeg_bytes(16, 8, [[100, 50]], sof=0xC2))
    assert image.detail.startswith("progressive")
    assert px(image, 0, 0)[0] == grey(100)


def test_extended_sequential_is_read_like_baseline():
    image = decode(jpeg_bytes(8, 8, [[64]], sof=0xC1))
    assert image.detail.startswith("extended sequential")


def test_the_ac_coefficients_are_stepped_over_not_kept():
    """A block with real AC content still yields its DC, and stays in step."""
    image = decode(jpeg_bytes(16, 8, [[100, 40]],
                              ac_extra=[(0x11, 1), (0xF0, 0), (0x21, 1)]))
    assert [px(image, x, 0)[0] for x in range(2)] == [grey(100), grey(40)]


def test_a_frame_type_with_no_decoder_says_which():
    with pytest.raises(ImageError, match="frame type 0xc3"):
        decode(jpeg_bytes(8, 8, [[0]], sof=0xC3))


def test_a_sixteen_bit_quantisation_table_is_read():
    table = b"\x10" + struct.pack(">64H", *([2] * 64))
    data = jpeg_bytes(8, 8, [[10]], extra=jpeg_segment(0xDB, table))
    # The later table replaces the earlier one, so the coefficient doubles.
    assert px(decode(data))[0] == grey(20)


# -- EXIF orientation ----------------------------------------------------------

def test_an_upright_photo_is_left_alone():
    image = decode(jpeg_bytes(16, 8, [[10, 200]], exif=exif_bytes(1)))
    assert (image.width, image.height) == (2, 1)
    assert px(image, 0, 0)[0] == grey(10)


def test_a_quarter_turn_swaps_the_axes():
    """Orientation 6 is the one a phone held sideways produces."""
    image = decode(jpeg_bytes(16, 8, [[10, 200]], exif=exif_bytes(6)))
    assert (image.width, image.height) == (1, 2)


def test_a_mirrored_orientation_flips_left_for_right():
    image = decode(jpeg_bytes(16, 8, [[10, 200]], exif=exif_bytes(2)))
    assert (image.width, image.height) == (2, 1)
    assert px(image, 0, 0)[0] == grey(200)      # was on the right


def test_orientation_three_turns_the_photo_upside_down():
    image = decode(jpeg_bytes(16, 8, [[10, 200]], exif=exif_bytes(3)))
    assert px(image, 0, 0)[0] == grey(200)


def test_big_endian_exif_is_read_too():
    image = decode(jpeg_bytes(16, 8, [[10, 200]],
                              exif=exif_bytes(6, order=b"MM")))
    assert (image.width, image.height) == (1, 2)


@pytest.mark.parametrize("payload", [
    b"not exif at all",
    b"Exif\x00\x00" + b"XX" + b"\x00" * 20,          # unknown byte order
    b"Exif\x00\x00II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 0),
    b"Exif\x00\x00II*\x00" + struct.pack("<I", 8),   # runs off the end
])
def test_exif_that_says_nothing_useful_leaves_the_photo_alone(payload):
    assert jpeg._orientation(payload) == 1


def test_an_orientation_number_that_is_not_defined_is_ignored():
    image = decode(jpeg_bytes(16, 8, [[10, 200]], exif=exif_bytes(99)))
    assert (image.width, image.height) == (2, 1)


# -- damaged files -------------------------------------------------------------

def test_a_truncated_scan_shows_what_did_arrive():
    """The top of a half-downloaded photo is more use than an error."""
    data = jpeg_bytes(64, 8, [[10] * 8])
    image = decode(data[:len(data) - 6])
    assert image.width == 8
    assert px(image, 0, 0)[0] == grey(10)


@pytest.mark.parametrize("data, message", [
    (b"\xff\xd8\xff\xda", "segment header runs off the end"),
    (b"\xff\xd8\xff\xdb\x00\x01", "segment length is impossible"),
    (b"\xff\xd8\xff\xd9", "no frame header"),
    (b"\xff\xd8" + jpeg_segment(0xC0, struct.pack(">BHHB", 8, 8, 8, 1)),
     "truncated JPEG frame header"),
    (b"\xff\xd8" + jpeg_segment(0xC0, b"\x08\x00"), "truncated JPEG frame header"),
    (b"\xff\xd8" + jpeg_segment(0xDB, b"\x00\x01\x02"),
     "truncated JPEG quantisation table"),
    (b"\xff\xd8" + jpeg_segment(0xC4, b"\x00" + bytes([0, 0, 0, 4]
                                                      + [0] * 12) + b"\x01"),
     "truncated JPEG Huffman table"),
])
def test_a_damaged_jpeg_is_reported(data, message):
    with pytest.raises(ImageError, match=message):
        decode(data)


def test_a_jpeg_with_a_frame_but_no_scan():
    data = b"\xff\xd8" + jpeg_segment(
        0xC0, struct.pack(">BHHB", 8, 8, 8, 1) + b"\x01\x11\x00") + b"\xff\xd9"
    with pytest.raises(ImageError, match="no scan"):
        decode(data)


def test_a_scan_that_arrives_before_its_frame():
    data = b"\xff\xd8" + jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    with pytest.raises(ImageError, match="scan arrived before its frame"):
        decode(data)


def test_a_scan_naming_a_component_that_is_not_in_the_frame():
    data = jpeg_bytes(8, 8, [[0]])
    broken = bytearray(data)
    at = broken.find(b"\xff\xda")
    broken[at + 5] = 9              # the scan's component identifier
    with pytest.raises(ImageError, match="component not in the frame"):
        decode(bytes(broken))


def test_a_truncated_scan_header():
    data = b"\xff\xd8" + jpeg_segment(
        0xC0, struct.pack(">BHHB", 8, 8, 8, 1) + b"\x01\x11\x00")
    data += jpeg_segment(0xDA, b"\x01\x01")
    with pytest.raises(ImageError, match="truncated JPEG scan header"):
        decode(data)


def test_a_scan_header_with_no_body_at_all():
    data = b"\xff\xd8" + jpeg_segment(
        0xC0, struct.pack(">BHHB", 8, 8, 8, 1) + b"\x01\x11\x00")
    data += jpeg_segment(0xDA, b"")
    with pytest.raises(ImageError, match="truncated JPEG scan header"):
        decode(data)


def test_a_frame_naming_a_quantisation_table_that_never_arrived():
    data = b"\xff\xd8" + jpeg_segment(
        0xC0, struct.pack(">BHHB", 8, 8, 8, 1) + b"\x01\x11\x07")
    data += jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\xff\xd9"
    with pytest.raises(ImageError, match="quantisation table it never sent"):
        decode(data)


def test_a_scan_naming_a_huffman_table_that_never_arrived():
    data = b"\xff\xd8" + jpeg_segment(0xDB, b"\x00" + bytes([1] * 64))
    data += jpeg_segment(0xC0, struct.pack(">BHHB", 8, 8, 8, 1) + b"\x01\x11\x00")
    data += jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    data += b"\x00\x00\xff\xd9"
    with pytest.raises(ImageError, match="Huffman table it never sent"):
        decode(data)


def test_an_ac_table_that_never_arrived():
    data = b"\xff\xd8" + jpeg_segment(0xDB, b"\x00" + bytes([1] * 64))
    data += jpeg_segment(0xC0, struct.pack(">BHHB", 8, 8, 8, 1) + b"\x01\x11\x00")
    data += jpeg_segment(0xC4, b"\x00" + bytes([0, 0, 0, 12] + [0] * 12)
                         + bytes(range(12)))
    data += jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    data += b"\x00\x00\xff\xd9"
    with pytest.raises(ImageError, match="Huffman table it never sent"):
        decode(data)


def test_a_restart_marker_that_never_arrives_stops_the_scan():
    """The file promises restarts and then ends; whatever decoded stands."""
    data = jpeg_bytes(32, 8, [[10, 20, 30, 40]], restart=2)
    at = data.find(b"\xff\xd0")
    image = decode(data[:at])
    assert px(image, 0, 0)[0] == grey(10)


# -- Huffman and bit reading ---------------------------------------------------

def test_a_dc_coefficient_size_that_cannot_exist():
    """Twelve bits is the most a DC difference can need; the table said 20."""
    data = b"\xff\xd8" + jpeg_segment(0xDB, b"\x00" + bytes([1] * 64))
    data += jpeg_segment(0xC0, struct.pack(">BHHB", 8, 8, 8, 1) + b"\x01\x11\x00")
    # One DC code, one bit long, whose value is an impossible coefficient size.
    data += jpeg_segment(0xC4, b"\x00" + bytes([1] + [0] * 15) + bytes([20]))
    data += jpeg_segment(0xC4, b"\x10" + bytes([1] + [0] * 15) + bytes([0]))
    data += jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    data += b"\x00\x00\xff\xd9"
    with pytest.raises(ImageError, match="DC coefficient size is impossible"):
        decode(data)


def test_a_huffman_table_short_of_its_values():
    with pytest.raises(ImageError, match="short of values"):
        jpeg._Huffman(bytes([2] + [0] * 15), b"\x01")


def test_a_code_that_is_not_in_the_table():
    table = jpeg._Huffman(bytes([1] + [0] * 15), b"\x05")
    bits = jpeg._Bits(b"\xff\x00" * 4, 0)       # all ones: never matches
    with pytest.raises(ImageError, match="code is not in the table"):
        bits.decode(table)


def test_stuffed_bytes_are_unescaped():
    """0xFF 0x00 in the stream is a literal 0xFF, not a marker."""
    bits = jpeg._Bits(b"\xff\x00", 0)
    assert bits.receive(8) == 0xFF


def test_a_real_marker_ends_the_entropy_data():
    bits = jpeg._Bits(b"\xff\xd9", 0)
    with pytest.raises(jpeg._EndOfData):
        bits.bit()


def test_running_off_the_end_ends_the_entropy_data():
    bits = jpeg._Bits(b"", 0)
    with pytest.raises(jpeg._EndOfData):
        bits.bit()


def test_a_trailing_ff_at_the_very_end_ends_the_data():
    bits = jpeg._Bits(b"\xff", 0)
    with pytest.raises(jpeg._EndOfData):
        bits.bit()


def test_restart_returns_false_when_there_is_no_marker_left():
    assert jpeg._Bits(b"\x01\x02\x03", 0).restart() is False


# -- marker walking ------------------------------------------------------------

def test_fill_bytes_and_standalone_markers_are_stepped_over():
    data = (b"\xff\xd8" + b"\xff\xff" + b"\xff\x01" + b"\xff\xd0"
            + jpeg_segment(0xDB, b"\x00" + bytes([1] * 64)))
    found = [marker for marker, _payload, _after in jpeg._markers(data)]
    assert found == [0xDB]


def test_padding_between_segments_is_skipped():
    data = b"\xff\xd8" + b"\x00\x00" + jpeg_segment(0xDB,
                                                    b"\x00" + bytes([1] * 64))
    assert [m for m, _p, _a in jpeg._markers(data)] == [0xDB]


def test_the_walk_stops_at_end_of_image():
    data = b"\xff\xd8" + b"\xff\xd9" + jpeg_segment(0xDB, b"\x00")
    assert list(jpeg._markers(data)) == []
