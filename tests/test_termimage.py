"""The terminal graphics protocols: detection, geometry, Sixel and kitty.

The encoders are checked by decoding their output again rather than by
matching bytes.  A sequence can look plausible and still paint the wrong
picture, and the point of these protocols is the picture.
"""

from __future__ import annotations

import base64

import pytest

from meridian_commander import termimage
from meridian_commander.imageview import palette_index, palette_rgb


# -- detection -----------------------------------------------------------------

def test_a_terminal_that_says_nothing_gets_no_protocol():
    assert termimage.detect({"TERM": "xterm-256color"}) is None
    assert termimage.detect({}) is None


@pytest.mark.parametrize("env", [
    {"KITTY_WINDOW_ID": "1"},
    {"TERM": "xterm-kitty"},
    {"TERM": "xterm-ghostty"},
    {"TERM_PROGRAM": "ghostty"},
])
def test_the_kitty_family_is_recognised(env):
    assert termimage.detect(env) == termimage.KITTY


@pytest.mark.parametrize("env", [
    {"TERM": "xterm-sixel"},
    {"TERM": "mlterm"},
    {"TERM": "foot"},
    {"TERM_PROGRAM": "WezTerm"},
    {"TERM_PROGRAM": "iTerm.app"},
    {"WEZTERM_PANE": "0"},
])
def test_the_sixel_family_is_recognised(env):
    assert termimage.detect(env) == termimage.SIXEL


def test_kitty_wins_when_a_terminal_could_do_either():
    """WezTerm speaks both; the one that keeps every colour is worth more."""
    assert termimage.detect(
        {"TERM_PROGRAM": "WezTerm", "KITTY_WINDOW_ID": "3"}) == termimage.KITTY


@pytest.mark.parametrize("value", ["off", "none", "no", "0", "OFF"])
def test_the_override_can_switch_graphics_off(value):
    assert termimage.detect({termimage.OVERRIDE_ENV: value,
                             "TERM": "xterm-kitty"}) is None


def test_the_override_can_force_a_protocol():
    """For the terminal we guess wrong about, in either direction."""
    env = {termimage.OVERRIDE_ENV: "sixel", "TERM": "xterm-kitty"}
    assert termimage.detect(env) == termimage.SIXEL
    env = {termimage.OVERRIDE_ENV: "kitty", "TERM": "dumb"}
    assert termimage.detect(env) == termimage.KITTY


def test_an_override_of_nonsense_falls_back_to_sniffing():
    assert termimage.detect(
        {termimage.OVERRIDE_ENV: "banana", "TERM": "foot"}) == termimage.SIXEL


# -- cell geometry -------------------------------------------------------------

def test_cell_pixels_answers_none_for_something_that_is_not_a_terminal(tmp_path):
    path = tmp_path / "notatty"
    path.write_text("")
    with open(path) as f:
        assert termimage.cell_pixels(f.fileno()) is None


def test_the_default_cell_is_a_sane_shape():
    """Taller than wide, or every fallback picture comes out stretched."""
    width, height = termimage.DEFAULT_CELL
    assert 0 < width < height


# -- sixel ---------------------------------------------------------------------

def decode_sixel(payload: bytes):
    """Expand a sixel sequence back into (width, height, {(x, y): (r, g, b)}).

    Deliberately a separate implementation from the encoder: a shared helper
    would agree with itself about a wrong format.
    """
    assert payload.startswith(termimage.SIXEL_START)
    assert payload.endswith(termimage.SIXEL_END)
    body = payload[len(termimage.SIXEL_START):-len(termimage.SIXEL_END)]

    palette: dict[int, tuple[int, int, int]] = {}
    pixels: dict[tuple[int, int], tuple[int, int, int]] = {}
    raster = None
    colour = 0
    x = 0
    band = 0
    i = 0

    def number(at):
        start = at
        while at < len(body) and body[at:at + 1].isdigit():
            at += 1
        return int(body[start:at]), at

    while i < len(body):
        ch = body[i:i + 1]
        if ch == b'"':                       # raster attributes
            i += 1
            values = []
            for _ in range(4):
                value, i = number(i)
                values.append(value)
                if body[i:i + 1] == b";":
                    i += 1
            raster = (values[2], values[3])
        elif ch == b"#":                     # select or define a colour
            i += 1
            index, i = number(i)
            if body[i:i + 1] == b";":
                i += 1
                mode, i = number(i)
                i += 1                       # ";"
                r, i = number(i)
                i += 1
                g, i = number(i)
                i += 1
                b, i = number(i)
                assert mode == 2             # RGB, not HLS
                palette[index] = (round(r * 255 / 100), round(g * 255 / 100),
                                  round(b * 255 / 100))
            colour = index
            x = 0
        elif ch == b"$":                     # back to the left of this band
            x = 0
            i += 1
        elif ch == b"-":                     # next band
            band += 6
            x = 0
            i += 1
        elif ch == b"!":                     # run length
            i += 1
            count, i = number(i)
            mask = body[i] - 0x3F
            i += 1
            for _ in range(count):
                for bit in range(6):
                    if mask & (1 << bit):
                        pixels[(x, band + bit)] = palette[colour]
                x += 1
        else:
            mask = body[i] - 0x3F
            i += 1
            for bit in range(6):
                if mask & (1 << bit):
                    pixels[(x, band + bit)] = palette[colour]
            x += 1

    return raster[0], raster[1], pixels


def quantised(rows):
    """``rows`` as the encoder will render them, after quantisation."""
    return [[palette_rgb(palette_index(*px)) for px in row] for row in rows]


def encode(rows):
    height = len(rows)
    width = len(rows[0]) if height else 0
    return termimage.sixel_encode(rows, width, height,
                                  palette_index, palette_rgb)


def test_a_single_pixel_survives_the_round_trip():
    rows = [[(255, 0, 0)]]
    width, height, pixels = decode_sixel(encode(rows))
    assert (width, height) == (1, 1)
    assert pixels[(0, 0)] == quantised(rows)[0][0]


def test_every_pixel_of_a_block_lands_where_it_started():
    """Two bands and three colours, so bands, "$" and "-" all get exercised."""
    red, green, blue = (255, 0, 0), (0, 255, 0), (0, 0, 255)
    rows = [[red, green, blue, red] for _ in range(8)]
    rows[3] = [blue, blue, green, green]
    width, height, pixels = decode_sixel(encode(rows))

    assert (width, height) == (4, 8)
    want = quantised(rows)
    for y in range(8):
        for x in range(4):
            assert pixels[(x, y)] == want[y][x], f"pixel {x},{y}"


def test_a_band_shorter_than_six_rows_is_not_padded_out():
    """The last band of a 4-row image must not paint two phantom rows."""
    rows = [[(255, 255, 255)] * 3 for _ in range(4)]
    width, height, pixels = decode_sixel(encode(rows))
    assert (width, height) == (3, 4)
    assert max(y for _x, y in pixels) == 3


def test_a_flat_run_is_compressed():
    """A wide band of one colour should not cost a byte per column."""
    rows = [[(0, 0, 0)] * 200 for _ in range(6)]
    payload = encode(rows)
    assert b"!" in payload
    # Comfortably smaller than one character per pixel.
    assert len(payload) < 200
    _w, _h, pixels = decode_sixel(payload)
    assert len(pixels) == 200 * 6


def test_a_short_run_is_written_out_rather_than_counted():
    """"!2x" is no shorter than "xx", so the introducer is not worth it."""
    rows = [[(0, 0, 0), (0, 0, 0), (255, 255, 255)]]
    assert b"!" not in encode(rows)


def test_the_declared_size_matches_the_pixels():
    rows = [[(10, 20, 30)] * 7 for _ in range(5)]
    width, height, _pixels = decode_sixel(encode(rows))
    assert (width, height) == (7, 5)


def test_each_colour_is_declared_once_however_often_it_is_used():
    rows = [[(255, 0, 0), (0, 0, 255)] * 8 for _ in range(12)]
    payload = encode(rows)
    # "#n;2;..." is a definition; a bare "#n" is a selection.
    assert payload.count(b";2;") == 2


# -- kitty ---------------------------------------------------------------------

def kitty_chunks(payload: bytes):
    """Split a kitty sequence into its (control, data) pairs."""
    out = []
    rest = payload
    while rest:
        assert rest.startswith(termimage.KITTY_START)
        rest = rest[len(termimage.KITTY_START):]
        end = rest.index(termimage.KITTY_END)
        control, _, data = rest[:end].partition(b";")
        out.append((control.decode(), data))
        rest = rest[end + len(termimage.KITTY_END):]
    return out


def test_kitty_carries_the_pixels_untouched():
    """No palette and no quantising: the bytes handed over are the bytes."""
    pixels = bytes(range(0, 96))              # 32 RGB triples
    payload = termimage.kitty_encode(pixels, 8, 4, 2, 1)
    chunks = kitty_chunks(payload)
    assert base64.b64decode(b"".join(data for _c, data in chunks)) == pixels


def test_the_kitty_control_data_describes_the_image():
    payload = termimage.kitty_encode(b"\x00" * 12, 2, 2, 1, 1)
    control = kitty_chunks(payload)[0][0]
    fields = dict(part.split("=") for part in control.split(","))
    assert fields["a"] == "T"                 # transmit and display
    assert fields["f"] == "24"                # raw RGB
    assert (fields["s"], fields["v"]) == ("2", "2")
    assert (fields["c"], fields["r"]) == ("1", "1")
    assert fields["q"] == "2"                 # no chatty reply into our input


def test_a_big_image_is_chunked_and_flagged():
    pixels = b"\x01" * (termimage.KITTY_CHUNK * 3)
    chunks = kitty_chunks(termimage.kitty_encode(pixels, 100, 10, 10, 1))
    assert len(chunks) > 1
    # Every chunk but the last says more is coming, and none is oversized.
    for control, data in chunks[:-1]:
        assert "m=1" in control
        assert len(data) <= termimage.KITTY_CHUNK
    assert "m=0" in chunks[-1][0]
    # Only the first chunk repeats the geometry.
    assert "a=T" in chunks[0][0]
    assert all("a=T" not in c for c, _d in chunks[1:])
    assert base64.b64decode(b"".join(d for _c, d in chunks)) == pixels


def test_clearing_asks_kitty_to_delete_every_image():
    control = kitty_chunks(termimage.kitty_clear() + b"")[0][0]
    assert "a=d" in control


# -- placing it on the screen --------------------------------------------------

def test_the_cursor_move_is_one_based():
    """curses counts from zero and the escape sequence counts from one."""
    assert termimage.cursor_to(0, 0) == b"\x1b[1;1H"
    assert termimage.cursor_to(4, 9) == b"\x1b[5;10H"


def test_emit_positions_the_payload_then_writes_it():
    written = []
    termimage.emit(b"PAYLOAD", 2, 3, write=written.append)
    assert written == [termimage.cursor_to(2, 3) + b"PAYLOAD"]


# -- the corners ---------------------------------------------------------------

def test_cell_pixels_reports_what_the_terminal_says(monkeypatch):
    """The measuring path itself: CI has no terminal with a pixel size."""
    import fcntl
    import struct

    # rows, cols, xpixels, ypixels -- a 100x40 grid of 9x21 cells.
    packed = struct.pack("HHHH", 40, 100, 900, 840)
    monkeypatch.setattr(fcntl, "ioctl", lambda *a, **k: packed)
    assert termimage.cell_pixels(1) == (9, 21)


def test_a_terminal_that_reports_no_pixel_size_measures_nothing(monkeypatch):
    import fcntl
    import struct

    monkeypatch.setattr(fcntl, "ioctl",
                        lambda *a, **k: struct.pack("HHHH", 40, 100, 0, 0))
    assert termimage.cell_pixels(1) is None


def test_cell_pixels_gives_up_where_there_is_no_termios(monkeypatch):
    """Windows has no termios, and importing it must not be fatal."""
    import sys

    monkeypatch.setitem(sys.modules, "termios", None)
    assert termimage.cell_pixels(1) is None


def test_more_colours_than_registers_reuses_one_rather_than_failing():
    """A quantiser that overruns its promise costs a wrong shade, not a crash."""
    count = termimage.SIXEL_COLOURS + 40
    rows = [[(x % 256, x // 256, 0) for x in range(count)]]
    payload = termimage.sixel_encode(
        rows, count, 1,
        lambda r, g, b: r + g * 256,          # every pixel its own colour
        lambda i: (i % 256, i // 256, 0))
    # Only as many colours as there are registers were ever declared.
    assert payload.count(b";2;") == termimage.SIXEL_COLOURS
    assert payload.endswith(termimage.SIXEL_END)


def test_emit_does_nothing_without_a_byte_stream(monkeypatch):
    """Under a stand-in stdout there is nowhere to put an escape sequence."""
    import sys

    class _TextOnly:
        pass

    monkeypatch.setattr(sys, "stdout", _TextOnly())
    termimage.emit(b"PAYLOAD", 0, 0)          # must not raise


def test_a_stream_that_will_not_flush_is_not_fatal():
    """The bytes are written; a refused flush is the terminal's problem."""
    written = []

    class _Refuses:
        def __call__(self, data):
            written.append(data)

        def flush(self):
            raise OSError("broken pipe")

    termimage.emit(b"PAYLOAD", 1, 1, write=_Refuses())
    assert written and written[0].endswith(b"PAYLOAD")
