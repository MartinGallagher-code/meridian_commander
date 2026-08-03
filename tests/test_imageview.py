"""The image browser: quantisation, half-block drawing, panning and zoom.

Drawing is tested on a real curses screen over a pseudo-terminal, the same way
the other browsers are.  Only a real window reports the errors that writing
past its edge produces, so only a real window can prove the guards work.
"""

from __future__ import annotations

import curses

import pytest

from meridian_commander import imageview
from meridian_commander.browsers import viewer_for
from meridian_commander.imageview import (
    HALF_BLOCK,
    RAMP,
    ZOOM_STEPS,
    ImageView,
    Pairs,
    cube_index,
    fit,
    luminance,
    palette_index,
    sample,
)

from support import (
    gif_bytes,
    gif_frame,
    rgb_png,
    script_newwin,
    with_curses_screen,
)


def with_colour_screen(rows, cols, fn):
    """A screen reporting the 256 colours the half-block drawing needs."""
    return with_curses_screen(rows, cols, fn, colours=256)

RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)
WHITE, BLACK = (255, 255, 255), (0, 0, 0)


def write_png(path, width, height, pixels):
    with open(path, "wb") as f:
        f.write(rgb_png(width, height, pixels))
    return str(path)


@pytest.fixture
def picture(tmp_path, fs):
    """A 4x2 image: red, green, blue, white on the top row; greys below."""
    path = write_png(tmp_path / "p.png", 4, 2,
                     [RED, GREEN, BLUE, WHITE,
                      BLACK, (85, 85, 85), (170, 170, 170), WHITE])
    return ImageView(fs, path)


# -- colour quantisation -------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (0, 0), (40, 0), (60, 1), (95, 1), (135, 2), (175, 3), (215, 4), (255, 5),
])
def test_cube_index_picks_the_nearest_axis(value, expected):
    assert cube_index(value) == expected


def test_the_primaries_land_on_the_cube_corners():
    assert palette_index(0, 0, 0) == 16
    assert palette_index(255, 0, 0) == 16 + 36 * 5
    assert palette_index(0, 255, 0) == 16 + 6 * 5
    assert palette_index(0, 0, 255) == 16 + 5
    assert palette_index(255, 255, 255) == 16 + 36 * 5 + 6 * 5 + 5


def test_greys_prefer_the_grey_ramp_over_the_cube():
    """The cube has six grey points; without the ramp midtones visibly step."""
    index = palette_index(120, 120, 120)
    assert 232 <= index <= 255
    # A colour that is not grey must not be dragged onto the ramp.
    assert palette_index(200, 40, 40) < 232


def test_every_colour_maps_somewhere_in_range():
    for value in (0, 7, 63, 128, 200, 255):
        assert 16 <= palette_index(value, value // 2, 255 - value) <= 255


@pytest.mark.parametrize("rgb, expected", [
    (BLACK, 0), (WHITE, 255),
])
def test_luminance_endpoints(rgb, expected):
    assert luminance(*rgb) == expected


def test_luminance_weights_green_most():
    """Rec. 601: the eye takes most of its brightness from green."""
    assert luminance(*GREEN) > luminance(*RED) > luminance(*BLUE)


# -- colour pairs --------------------------------------------------------------

def test_pairs_are_allocated_once_and_reused():
    def run(stdscr):
        pairs = Pairs()
        first = pairs.get(21, 45)
        assert pairs.get(21, 45) == first        # the same pair comes back
        assert pairs.get(45, 21) != first        # order matters
        return len(pairs), pairs.exhausted

    assert with_colour_screen(10, 40, run) == (2, False)


def test_running_out_of_pairs_reuses_one_rather_than_giving_up():
    """A wrong shade in one cell beats refusing to draw the picture."""

    def run(stdscr):
        pairs = Pairs(limit=3)
        pairs.get(1, 2)
        pairs.get(3, 4)
        overflow = pairs.get(5, 6)
        return overflow, pairs.exhausted, len(pairs)

    overflow, exhausted, count = with_colour_screen(10, 40, run)
    assert exhausted is True
    assert overflow == 1                 # falls back to the first pair
    assert count == 2


def test_a_pair_curses_refuses_is_not_fatal():
    def run(stdscr):
        pairs = Pairs()
        # A colour number past the terminal's range. Which exception this
        # raises varies by Python version, which is the point of the test.
        assert pairs.get(300, 300) == 0
        return pairs.exhausted

    assert with_colour_screen(10, 40, run) is True


def test_the_first_pair_request_of_all_falls_back_to_zero():
    def run(stdscr):
        return Pairs(limit=1).get(4, 5)

    assert with_colour_screen(10, 40, run) == 0


# -- geometry ------------------------------------------------------------------

def test_fit_uses_two_pixels_per_row():
    """A cell is twice as tall as it is wide, so rows count double."""
    assert fit(100, 100, 50, 25) == (50, 50)


def test_fit_keeps_the_aspect_ratio():
    wide = fit(200, 50, 40, 40)
    assert wide[0] / wide[1] == pytest.approx(4.0, abs=0.2)


def test_fit_zooms_about_the_fitted_size():
    base = fit(100, 100, 50, 25, zoom=100)
    doubled = fit(100, 100, 50, 25, zoom=200)
    assert doubled == (base[0] * 2, base[1] * 2)


@pytest.mark.parametrize("args", [(0, 10, 5, 5), (10, 0, 5, 5),
                                  (10, 10, 0, 5), (10, 10, 5, 0)])
def test_fit_of_nothing_is_nothing(args):
    assert fit(*args) == (0, 0)


def test_fit_never_returns_a_zero_dimension():
    """A very wide image in a short window still has to have one row."""
    assert min(fit(1000, 1, 10, 10)) >= 1


# -- resampling ----------------------------------------------------------------

def test_sample_takes_the_window_it_is_asked_for(picture):
    rows = sample(picture.image, 0, 4, 2, 0, 0, 4, 2)
    assert rows[0] == [RED, GREEN, BLUE, WHITE]
    assert rows[1][0] == BLACK


def test_sample_scales_up_by_repeating_pixels(picture):
    """Nearest neighbour: a doubled image has each pixel twice, not blurred."""
    rows = sample(picture.image, 0, 8, 4, 0, 0, 8, 4)
    assert rows[0][:4] == [RED, RED, GREEN, GREEN]
    assert rows[0] == rows[1]


def test_sample_offsets_into_the_scaled_image(picture):
    # Doubled to 8x4 and started four across: source column two, which is blue.
    rows = sample(picture.image, 0, 8, 4, 4, 0, 4, 2)
    assert rows[0][0] == BLUE
    assert rows[0] == rows[1]               # each source pixel spans two rows
    # Two down in that space is source row one, the grey ramp.
    lower = sample(picture.image, 0, 8, 4, 4, 2, 4, 2)
    assert lower[0][0] == (170, 170, 170)


def test_sample_clamps_at_the_edges(picture):
    """Panning past the edge repeats the border rather than reading rubbish."""
    rows = sample(picture.image, 0, 4, 2, -2, -1, 3, 2)
    assert rows[0][0] == RED
    rows = sample(picture.image, 0, 4, 2, 3, 1, 3, 2)
    assert rows[-1][-1] == WHITE


# -- loading -------------------------------------------------------------------

def test_the_browser_reads_and_decodes(picture):
    assert picture.error is None
    assert picture.image.kind == "PNG"
    assert (picture.image.width, picture.image.height) == (4, 2)


def test_a_file_that_is_not_an_image_says_so(tmp_path, fs):
    path = tmp_path / "notes.png"
    path.write_bytes(b"just some text")
    view = ImageView(fs, str(path))
    assert view.image is None
    assert "not an image" in view.error


def test_a_file_that_cannot_be_read_says_so(tmp_path, fs):
    view = ImageView(fs, str(tmp_path / "absent.png"))
    assert view.error


def test_an_image_larger_than_the_cap_is_refused(tmp_path, fs, monkeypatch):
    monkeypatch.setattr(imageview, "MAX_BYTES", 8)
    path = write_png(tmp_path / "p.png", 2, 1, [RED, GREEN])
    view = ImageView(fs, path)
    assert "larger than" in view.error


def test_the_dispatch_opens_images_in_the_image_browser(tmp_path, fs):
    path = write_png(tmp_path / "p.png", 1, 1, [RED])
    assert isinstance(viewer_for(fs, path), ImageView)


# -- panning and zoom ----------------------------------------------------------

def test_an_image_smaller_than_the_window_is_centred(picture):
    # A 4x2 image in a 40x20 window fits across but not down, so it is
    # centred vertically; a window it filled exactly would prove nothing.
    picture.clamp(40, 20)
    # A negative offset is what centres it: the window starts before the image.
    assert picture.off_y < 0
    picture.clamp(80, 10)
    assert picture.off_x < 0


def test_panning_a_zoomed_image_moves_the_view(picture):
    picture.set_zoom(3, 20, 10)
    before = picture.off_x
    picture.pan(5, 0, 20, 10)
    assert picture.off_x == before + 5


def test_panning_stops_at_the_edge(picture):
    picture.set_zoom(3, 20, 10)
    picture.pan(10_000, 10_000, 20, 10)
    scale_w, scale_h = picture.scaled(20, 10)
    assert picture.off_x == scale_w - 20
    assert picture.off_y == scale_h - 20


def test_zoom_is_bounded_at_both_ends(picture):
    picture.set_zoom(-5, 20, 10)
    assert picture.zoom == 0
    picture.set_zoom(99, 20, 10)
    assert picture.zoom == len(ZOOM_STEPS) - 1


def test_zooming_keeps_the_centre_of_the_view(picture):
    """Zooming about the corner makes the picture run away from the reader."""
    picture.set_zoom(3, 20, 10)
    picture.off_x = 40
    picture.clamp(20, 10)
    middle = picture.off_x + 10
    scale_w = picture.scaled(20, 10)[0]
    picture.set_zoom(1, 20, 10)
    new_w = picture.scaled(20, 10)[0]
    assert picture.off_x + 10 == pytest.approx(middle * new_w / scale_w, abs=2)


def test_scaled_is_nothing_when_there_is_no_image(tmp_path, fs):
    view = ImageView(fs, str(tmp_path / "absent.png"))
    assert view.scaled(10, 10) == (0, 0)


# -- animation -----------------------------------------------------------------

@pytest.fixture
def animation(tmp_path, fs):
    palette = bytes(RED) + bytes(GREEN) + bytes(BLUE) + b"\xff\xff\xff"
    data = gif_bytes(1, 1, [gif_frame([0], 1, 1, delay=10),
                            gif_frame([1], 1, 1, delay=10),
                            gif_frame([2], 1, 1, delay=10)], palette=palette)
    path = tmp_path / "a.gif"
    path.write_bytes(data)
    return ImageView(fs, str(path))


def test_stepping_through_frames_wraps(animation):
    assert animation.frame == 0
    animation.step_frame(1)
    assert animation.frame == 1
    animation.step_frame(-2)
    assert animation.frame == 2                 # wrapped backwards
    animation.step_frame(1)
    assert animation.frame == 0


def test_a_still_image_says_it_has_only_one_frame(picture):
    picture.step_frame(1)
    assert picture.frame == 0
    assert "only one frame" in picture.notice


# -- drawing -------------------------------------------------------------------

def draw(view, rows=8, cols=64, colour=True):
    def run(stdscr):
        curses.use_default_colors()
        view.colour = colour
        win = curses.newwin(rows, cols, 0, 0)
        view.draw(win)
        return [win.instr(y, 0, cols * 3).decode("utf-8", "replace")
                for y in range(rows)]

    return with_colour_screen(rows + 2, cols + 2, run)


def test_the_title_names_the_file_and_the_format(picture):
    lines = draw(picture)
    assert "Image: p.png" in lines[0]
    assert "PNG 4x2" in lines[0]


def test_the_body_is_drawn_with_half_blocks(picture):
    lines = draw(picture)
    assert HALF_BLOCK in "".join(lines[1:-1])


def test_ascii_mode_uses_the_ramp_instead(picture):
    lines = draw(picture, colour=False)
    body = "".join(lines[1:-1])
    assert HALF_BLOCK not in body
    assert any(ch in body for ch in RAMP.strip())
    assert "ASCII" in lines[-1]


def test_the_footer_shows_the_zoom_and_the_keys(picture):
    lines = draw(picture)
    assert "100%" in lines[-1]
    assert "[q]uit" in lines[-1]


def test_an_animation_reports_which_frame(animation):
    lines = draw(animation)
    assert "frame 1/3" in lines[-1]


def test_a_truncated_animation_says_so(animation):
    animation.image.truncated = True
    assert "[truncated]" in draw(animation)[-1]


def test_a_notice_replaces_the_footer(picture):
    picture.notice = " nothing to see "
    assert "nothing to see" in draw(picture)[-1]


def test_an_unreadable_file_draws_its_reason(tmp_path, fs):
    path = tmp_path / "notes.png"
    path.write_bytes(b"just some text")
    lines = draw(ImageView(fs, str(path)))
    assert "Cannot show this image" in "".join(lines)
    assert "[q]uit" in lines[-1]


def test_a_format_with_no_decoder_shows_what_it_is(tmp_path, fs):
    """A WebP cannot be drawn, but its name and size are still worth having."""
    import struct

    data = (b"RIFF" + b"\x00" * 4 + b"WEBPVP8 " + b"\x00" * 10
            + struct.pack("<HH", 320, 240))
    path = tmp_path / "shot.webp"
    path.write_bytes(data)
    lines = draw(ImageView(fs, str(path)))
    body = "".join(lines)
    assert "WEBP" in body
    assert "320x240" in body
    assert "VP8-coded" in body


def test_a_jpeg_reports_the_files_size_not_the_thumbnails(tmp_path, fs):
    """The frames are one eighth of the file; the title says the file."""
    from support import jpeg_bytes

    path = tmp_path / "photo.jpg"
    path.write_bytes(jpeg_bytes(64, 32, [[10] * 32]))
    view = ImageView(fs, str(path))
    assert (view.image.width, view.image.height) == (8, 4)
    assert view.image.size == (64, 32)
    title = draw(view, cols=80)[0]
    assert "JPEG 64x32" in title
    assert "shown at 1/8" in title


def test_drawing_into_a_window_with_no_room_does_not_raise(picture):
    for rows, cols in ((1, 20), (2, 20), (3, 1)):
        draw(picture, rows=rows, cols=cols)


def test_the_area_outside_a_small_image_is_left_alone(picture):
    """A picture narrower than the window must not paint the whole screen."""
    lines = draw(picture, rows=8, cols=40)
    # The image is 4x2 and keeps its aspect ratio, so it cannot fill 40 cells.
    assert any(line.strip() == "" or " " in line for line in lines[1:-1])


def test_exhausting_the_pairs_is_reported_in_the_footer(picture, monkeypatch):
    def run(stdscr):
        curses.use_default_colors()
        picture.pairs = Pairs(limit=1)
        win = curses.newwin(8, 64, 0, 0)
        picture.draw(win)
        return win.instr(7, 0, 64).decode()

    assert "colours exhausted" in with_colour_screen(12, 70, run)


def test_a_terminal_without_256_colours_gets_the_ramp(picture, monkeypatch):
    """Eight colours cannot approximate a photograph; the ramp can.

    The colour count is patched rather than driven from TERM: curses reads
    terminfo once per process, so a later screen in the same test run keeps
    whatever the first one reported.
    """
    def run(stdscr):
        # Patched inside, after start_color(): that call resets curses.COLORS
        # from terminfo, so patching before the screen exists is undone.
        monkeypatch.setattr(curses, "COLORS", 8, raising=False)
        win = curses.newwin(8, 68, 0, 0)
        picture.draw(win)
        return [win.instr(y, 0, 68 * 3).decode("utf-8", "replace")
                for y in range(8)]

    lines = with_curses_screen(10, 70, run, colours=8)
    assert HALF_BLOCK not in "".join(lines[1:-1])
    assert "no 256-colour palette" in lines[-1]


# -- the key loop --------------------------------------------------------------

def run_keys(view, keys, rows=10, cols=40, monkeypatch=None):
    """Drive the browser's key loop on a real screen with scripted input."""

    def run(stdscr):
        captured = script_newwin(monkeypatch, list(keys) + [ord("q")])
        view.run(stdscr)
        return captured["window"]

    return with_colour_screen(rows + 2, cols + 2, run)


def test_quitting_leaves_the_loop(picture, monkeypatch):
    run_keys(picture, [], monkeypatch=monkeypatch)      # only the q


@pytest.mark.parametrize("key", [27, ord("Q"), curses.KEY_F3, curses.KEY_F10])
def test_every_way_out_works(picture, monkeypatch, key):
    def run(stdscr):
        script_newwin(monkeypatch, [key])
        picture.run(stdscr)

    with_colour_screen(12, 42, run)


@pytest.mark.parametrize("keys, axis", [
    ([curses.KEY_RIGHT], "x"), ([ord("l")], "x"),
    ([curses.KEY_DOWN], "y"), ([ord("j")], "y"),
])
def test_panning_keys_move_the_view(picture, monkeypatch, keys, axis):
    picture.zoom = 3
    run_keys(picture, keys, monkeypatch=monkeypatch)
    assert getattr(picture, f"off_{axis}") > 0


@pytest.mark.parametrize("keys", [[curses.KEY_LEFT], [ord("h")],
                                  [curses.KEY_UP], [ord("k")]])
def test_panning_back_stops_at_the_top_left(picture, monkeypatch, keys):
    picture.zoom = 3
    run_keys(picture, keys, monkeypatch=monkeypatch)
    assert picture.off_x <= 0 or picture.off_y <= 0


@pytest.mark.parametrize("key", [ord("+"), ord("=")])
def test_zooming_in(picture, monkeypatch, key):
    run_keys(picture, [key], monkeypatch=monkeypatch)
    assert picture.zoom == 1


@pytest.mark.parametrize("key", [ord("-"), ord("_")])
def test_zooming_out_from_the_bottom_stays_put(picture, monkeypatch, key):
    run_keys(picture, [key], monkeypatch=monkeypatch)
    assert picture.zoom == 0


@pytest.mark.parametrize("key", [ord("f"), ord("F"), curses.KEY_HOME])
def test_fitting_returns_to_full_view(picture, monkeypatch, key):
    run_keys(picture, [ord("+"), ord("+"), key], monkeypatch=monkeypatch)
    assert picture.zoom == 0


@pytest.mark.parametrize("key, expected", [
    (ord("n"), 1), (ord(" "), 1), (ord("p"), 2), (ord("N"), 2),
])
def test_frame_keys_step_the_animation(animation, monkeypatch, key, expected):
    run_keys(animation, [key], monkeypatch=monkeypatch)
    assert animation.frame == expected


@pytest.mark.parametrize("key", [ord("c"), ord("C")])
def test_the_colour_key_toggles(picture, monkeypatch, key):
    run_keys(picture, [key], monkeypatch=monkeypatch)
    assert picture.colour is False


def test_an_unknown_key_is_ignored(picture, monkeypatch):
    run_keys(picture, [ord("z")], monkeypatch=monkeypatch)
    assert picture.zoom == 0


# -- drawing that curses refuses -----------------------------------------------

def test_a_window_that_refuses_every_write_does_not_raise(picture, monkeypatch):
    """Every write is guarded; a hostile window must not take the app down."""

    def run(stdscr):
        script_newwin(monkeypatch, [ord("q")], fail_draws=True)
        picture.run(stdscr)

    with_colour_screen(12, 42, run)


def test_the_error_screen_survives_a_window_that_refuses(tmp_path, fs,
                                                          monkeypatch):
    path = tmp_path / "notes.png"
    path.write_bytes(b"just some text")
    view = ImageView(fs, str(path))

    def run(stdscr):
        script_newwin(monkeypatch, [ord("q")], fail_draws=True)
        view.run(stdscr)

    with_colour_screen(12, 42, run)


def test_a_note_longer_than_the_window_is_cut_off(tmp_path, fs):
    """Three lines of explanation in a two-row window: it stops, not crashes."""
    import struct

    data = (b"RIFF" + b"\x00" * 4 + b"WEBPVP8 " + b"\x00" * 10
            + struct.pack("<HH", 320, 240))
    path = tmp_path / "shot.webp"
    path.write_bytes(data)
    view = ImageView(fs, str(path))

    def run(stdscr):
        win = curses.newwin(3, 30, 0, 0)
        view.draw(win)
        return win.instr(1, 0, 30).decode()

    assert "WEBP" in with_colour_screen(6, 40, run)
