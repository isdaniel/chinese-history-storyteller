import itertools

import pytest

import render_motion as rm
from shot_plan import MOTIONS

SIZES = [(1536, 1024), (2000, 900), (1280, 720), (1000, 1400)]
FOCI = [(0.5, 0.5), (0.0, 0.0), (1.0, 1.0), (0.2, 0.8)]


@pytest.mark.parametrize("motion", MOTIONS)
def test_every_window_is_16_by_9_inside_the_image_and_never_over_zoomed(motion):
    for (iw, ih), focus, p in itertools.product(SIZES, FOCI, (0.0, 0.25, 0.5, 1.0, -0.2, 1.4)):
        x0, y0, x1, y1 = rm.window_for(motion, p, iw, ih, focus)
        w, h = x1 - x0, y1 - y0
        assert x0 >= -1e-6 and y0 >= -1e-6 and x1 <= iw + 1e-6 and y1 <= ih + 1e-6
        assert w / h == pytest.approx(16 / 9, rel=1e-6), "window must be 16:9 so the output is never stretched"
        bw, _ = rm.base_window(iw, ih)
        assert bw / w <= rm.MAX_ZOOM + 1e-6


def test_unknown_motion_raises():
    with pytest.raises(ValueError):
        rm.window_for("spin", 0.5, 1536, 1024, (0.5, 0.5))


def test_push_in_zooms_towards_the_focus_and_pull_out_reverses_it():
    start = rm.window_for("push_in", 0.0, 1536, 1024, (0.8, 0.5))
    end = rm.window_for("push_in", 1.0, 1536, 1024, (0.8, 0.5))
    assert (end[2] - end[0]) < (start[2] - start[0])
    assert (end[0] + end[2]) / 2 > (start[0] + start[2]) / 2
    assert rm.window_for("pull_out", 1.0, 1536, 1024, (0.8, 0.5)) == pytest.approx(start)


def test_pan_moves_horizontally_and_tilt_moves_vertically():
    left = rm.window_for("pan_lr", 0.0, 1536, 1024, (0.5, 0.5))
    right = rm.window_for("pan_lr", 1.0, 1536, 1024, (0.5, 0.5))
    assert right[0] > left[0] and right[1] == pytest.approx(left[1])
    up_start = rm.window_for("tilt_up", 0.0, 1536, 1024, (0.5, 0.5))
    up_end = rm.window_for("tilt_up", 1.0, 1536, 1024, (0.5, 0.5))
    assert up_end[1] < up_start[1] and up_end[0] == pytest.approx(up_start[0])


def test_detail_reaches_the_focus():
    x0, y0, x1, y1 = rm.window_for("detail", 1.0, 1536, 1024, (0.4, 0.5))
    assert abs((x0 + x1) / 2 - 0.4 * 1536) < 1 and abs((y0 + y1) / 2 - 0.5 * 1024) < 1


def test_ease_is_clamped_and_monotonic():
    assert rm.ease(-1) == 0.0 and rm.ease(2) == 1.0 and rm.ease(0.5) == 0.5
    assert rm.ease(0.25) < rm.ease(0.5) < rm.ease(0.75)


def _approx(window):
    return pytest.approx(window, abs=1e-3)


def _finite(window):
    return all(abs(v) < float("inf") for v in window)


def test_constants_match_the_spec():
    assert (rm.MAX_ZOOM, rm.PAN_SCALE, rm.PUSH_SCALE) == (1.65, 1.3, 1.24)


def test_base_window_is_the_largest_16_by_9_that_fits():
    assert rm.base_window(1536, 1024) == (1536, 864)
    assert rm.base_window(1600, 900) == (1600, 900)
    assert rm.base_window(1280, 720) == (1280, 720)
    assert rm.base_window(2000, 900) == (1600, 900)
    assert rm.base_window(1000, 1400) == (1000, 562.5)


@pytest.mark.parametrize("size, scale, cx, cy, expected", [
    ((1600, 900), 1.0, 800, 450, (0, 0, 1600, 900)),
    ((1600, 900), 2.0, 800, 450, (400, 225, 1200, 675)),
    ((1600, 900), 2.0, 0, 0, (0, 0, 800, 450)),
    ((1600, 900), 2.0, 1600, 900, (800, 450, 1600, 900)),
    ((1600, 900), 2.0, -500, -500, (0, 0, 800, 450)),
    ((1600, 900), 2.0, 9999, 9999, (800, 450, 1600, 900)),
    ((1600, 900), 2.0, 0, 450, (0, 225, 800, 675)),
    ((1600, 900), 2.0, 1600, 450, (800, 225, 1600, 675)),
    ((1600, 900), 2.0, 800, 0, (400, 0, 1200, 450)),
    ((1600, 900), 2.0, 800, 900, (400, 450, 1200, 900)),
    ((2000, 900), 1.0, 1000, 450, (200, 0, 1800, 900)),
    ((2000, 900), 1.0, 0, 450, (0, 0, 1600, 900)),
    ((1000, 1400), 1.0, 500, 700, (0, 418.75, 1000, 981.25)),
    ((1000, 1400), 1.0, 500, 0, (0, 0, 1000, 562.5)),
    ((1000, 1400), 1.0, 500, 1400, (0, 837.5, 1000, 1400)),
])
def test_make_box_centres_scales_and_clamps_each_edge_independently(size, scale, cx, cy, expected):
    assert rm.make_box(*size, scale, cx, cy) == _approx(expected)


def test_ease_is_smoothstep_and_lerp_is_linear():
    assert rm.ease(0.0) == 0.0 and rm.ease(1.0) == 1.0
    assert rm.ease(0.25) == pytest.approx(0.15625) and rm.ease(0.75) == pytest.approx(0.84375)
    assert rm.lerp(2, 10, 0) == 2 and rm.lerp(2, 10, 1) == 10
    assert rm.lerp(2, 10, 0.25) == 4 and rm.lerp(10, 2, 0.25) == 8


WIDE, WIDE_FOCUS = (1600, 900), (0.55, 0.45)
EXACT_WINDOWS = [
    ("push_in", 0.0, WIDE, WIDE_FOCUS, (0, 0, 1600, 900)),
    ("push_in", 0.5, WIDE, WIDE_FOCUS, (113.7143, 32.4643, 1542.2857, 836.0357)),
    ("push_in", 1.0, WIDE, WIDE_FOCUS, (210.8387, 55.5968, 1501.1613, 781.4032)),
    ("pull_out", 0.0, WIDE, WIDE_FOCUS, (234.8387, 42.0968, 1525.1613, 767.9032)),
    ("pull_out", 0.5, WIDE, WIDE_FOCUS, (137.7143, 18.9643, 1566.2857, 822.5357)),
    ("pull_out", 1.0, WIDE, WIDE_FOCUS, (0, 0, 1600, 900)),
    ("detail", 0.0, WIDE, WIDE_FOCUS, (0, 0, 1600, 900)),
    ("detail", 0.5, WIDE, WIDE_FOCUS, (236.2264, 87.8774, 1443.7736, 767.1226)),
    ("detail", 1.0, WIDE, WIDE_FOCUS, (395.1515, 132.2727, 1364.8485, 677.7273)),
    ("detail_hold", 0.0, WIDE, WIDE_FOCUS, (395.1515, 132.2727, 1364.8485, 677.7273)),
    ("detail_hold", 0.5, WIDE, WIDE_FOCUS, (380, 123.75, 1380, 686.25)),
    ("detail_hold", 1.0, WIDE, WIDE_FOCUS, (363.871, 114.6774, 1396.129, 695.3226)),
    ("pan_lr", 0.0, WIDE, WIDE_FOCUS, (0, 58.8462, 1230.7692, 751.1538)),
    ("pan_lr", 0.25, WIDE, WIDE_FOCUS, (57.6923, 58.8462, 1288.4615, 751.1538)),
    ("pan_lr", 0.5, WIDE, WIDE_FOCUS, (184.6154, 58.8462, 1415.3846, 751.1538)),
    ("pan_lr", 1.0, WIDE, WIDE_FOCUS, (369.2308, 58.8462, 1600, 751.1538)),
    ("pan_rl", 0.0, WIDE, WIDE_FOCUS, (369.2308, 58.8462, 1600, 751.1538)),
    ("pan_rl", 0.5, WIDE, WIDE_FOCUS, (184.6154, 58.8462, 1415.3846, 751.1538)),
    ("pan_rl", 1.0, WIDE, WIDE_FOCUS, (0, 58.8462, 1230.7692, 751.1538)),
    ("pan_lr", 0.5, WIDE, (0.5, 0.0), (184.6154, 0, 1415.3846, 692.3077)),
    ("pan_lr", 0.5, WIDE, (0.5, 1.0), (184.6154, 207.6923, 1415.3846, 900)),
    ("pan_lr", 0.0, (2000, 900), (0.3, 0.5), (0, 103.8462, 1230.7692, 796.1538)),
    ("pan_lr", 1.0, (2000, 900), (0.3, 0.5), (769.2308, 103.8462, 2000, 796.1538)),
    ("tilt_up", 0.0, (1000, 1400), (0.4, 0.9), (15.3846, 967.3077, 784.6154, 1400)),
    ("tilt_up", 0.5, (1000, 1400), (0.4, 0.9), (15.3846, 483.6538, 784.6154, 916.3462)),
    ("tilt_up", 1.0, (1000, 1400), (0.4, 0.9), (15.3846, 0, 784.6154, 432.6923)),
    ("tilt_down", 0.0, (1000, 1400), (0.4, 0.9), (15.3846, 0, 784.6154, 432.6923)),
    ("tilt_down", 0.5, (1000, 1400), (0.4, 0.9), (15.3846, 483.6538, 784.6154, 916.3462)),
    ("tilt_down", 1.0, (1000, 1400), (0.4, 0.9), (15.3846, 967.3077, 784.6154, 1400)),
    ("tilt_up", 0.5, (1000, 1400), (0.0, 0.1), (0, 483.6538, 769.2308, 916.3462)),
    ("tilt_up", 0.5, (1000, 1400), (1.0, 0.1), (230.7692, 483.6538, 1000, 916.3462)),
    ("tilt_up", 0.0, WIDE, (0.5, 0.5), (184.6154, 207.6923, 1415.3846, 900)),
    # smoothstep wiring at p=0.25 / 0.75 for every motion on the production 3:2 size
    ("push_in", 0.25, (1536, 1024), (0.3, 0.7), (0.0, 118.0145, 1480.4819, 950.7855)),
    ("pull_out", 0.25, (1536, 1024), (0.3, 0.7), (0.0, 305.4969, 1277.3389, 1024.0)),
    ("detail", 0.25, (1536, 1024), (0.3, 0.7), (22.8085, 151.8298, 1417.1915, 936.1702)),
    ("detail_hold", 0.25, (1536, 1024), (0.3, 0.7), (0.0, 452.4788, 939.8088, 981.1212)),
    ("pan_lr", 0.25, (1536, 1024), (0.3, 0.7), (55.3846, 359.3846, 1236.9231, 1024.0)),
    ("pan_rl", 0.25, (1536, 1024), (0.3, 0.7), (299.0769, 359.3846, 1480.6154, 1024.0)),
    ("tilt_up", 0.25, (1536, 1024), (0.3, 0.7), (0.0, 303.2308, 1181.5385, 967.8462)),
    ("tilt_down", 0.25, (1536, 1024), (0.3, 0.7), (0.0, 56.1538, 1181.5385, 720.7692)),
    ("push_in", 0.75, (1536, 1024), (0.3, 0.7), (0.0, 273.7084, 1277.3389, 992.2116)),
    ("pull_out", 0.75, (1536, 1024), (0.3, 0.7), (0.0, 179.4545, 1480.4819, 1012.2255)),
    ("detail", 0.75, (1536, 1024), (0.3, 0.7), (12.8161, 405.8091, 1004.7839, 963.7909)),
    ("detail_hold", 0.75, (1536, 1024), (0.3, 0.7), (0.0, 440.8719, 981.0778, 992.7281)),
    ("pan_lr", 0.75, (1536, 1024), (0.3, 0.7), (299.0769, 359.3846, 1480.6154, 1024.0)),
    ("pan_rl", 0.75, (1536, 1024), (0.3, 0.7), (55.3846, 359.3846, 1236.9231, 1024.0)),
    ("tilt_up", 0.75, (1536, 1024), (0.3, 0.7), (0.0, 56.1538, 1181.5385, 720.7692)),
    ("tilt_down", 0.75, (1536, 1024), (0.3, 0.7), (0.0, 303.2308, 1181.5385, 967.8462)),
    # wide image (base window narrower than the image) and pan/tilt on the 3:2 size
    ("push_in", 0.5, (2000, 900), (0.8, 0.3), (495.7143, 0.0, 1924.2857, 803.5714)),
    ("pull_out", 0.5, (2000, 900), (0.8, 0.3), (571.4286, 0.0, 2000.0, 803.5714)),
    ("pull_out", 0.5, (2000, 900), (0.3, 0.3), (25.7143, 0.0, 1454.2857, 803.5714)),
    ("detail", 0.5, (2000, 900), (0.8, 0.3), (696.2264, 20.3774, 1903.7736, 699.6226)),
    ("detail_hold", 0.5, (2000, 900), (0.8, 0.3), (1000.0, 0.0, 2000.0, 562.5)),
    ("tilt_up", 0.5, (2000, 900), (0.8, 0.3), (769.2308, 103.8462, 2000.0, 796.1538)),
    ("pan_lr", 0.5, (1536, 1024), (0.5, 0.5), (177.2308, 179.6923, 1358.7692, 844.3077)),
    ("pan_lr", 0.0, (1536, 1024), (0.5, 0.2), (0.0, 0.0, 1181.5385, 664.6154)),
    # focus clamp bounds by value: y over/under on 3:2, x over/under on a wide image
    ("push_in", 0.25, (1536, 1024), (0.5, 1.0), (27.759, 151.6145, 1508.241, 984.3855)),
    ("push_in", 0.25, (1536, 1024), (0.5, 1.5), (27.759, 151.6145, 1508.241, 984.3855)),
    ("push_in", 0.25, (1536, 1024), (0.5, 0.0), (27.759, 39.6145, 1508.241, 872.3855)),
    ("push_in", 0.25, (2000, 900), (0.0, 0.5), (119.5407, 16.2651, 1661.7093, 883.7349)),
    ("push_in", 0.25, (2000, 900), (-0.5, 0.5), (119.5407, 16.2651, 1661.7093, 883.7349)),
    ("push_in", 0.25, (2000, 900), (1.0, 0.5), (338.2907, 16.2651, 1880.4593, 883.7349)),
]


@pytest.mark.parametrize("motion, p, size, focus, expected", EXACT_WINDOWS)
def test_exact_windows(motion, p, size, focus, expected):
    assert rm.window_for(motion, p, *size, focus) == _approx(expected)


def test_focus_can_be_a_json_list():
    assert rm.window_for("push_in", 0.5, 1600, 900, [0.55, 0.45]) == _approx(
        rm.window_for("push_in", 0.5, 1600, 900, (0.55, 0.45)))


@pytest.mark.parametrize("motion", MOTIONS)
def test_progress_outside_zero_one_holds_the_end_window(motion):
    args = (1536, 1024, (0.3, 0.6))
    assert rm.window_for(motion, -0.2, *args) == _approx(rm.window_for(motion, 0.0, *args))
    assert rm.window_for(motion, 1.4, *args) == _approx(rm.window_for(motion, 1.0, *args))


NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("motion", MOTIONS)
def test_non_finite_progress_never_yields_a_non_finite_window(motion):
    args = (1536, 1024, (0.3, 0.6))
    assert rm.window_for(motion, NAN, *args) == _approx(rm.window_for(motion, 0.0, *args))
    assert rm.window_for(motion, INF, *args) == _approx(rm.window_for(motion, 1.0, *args))
    assert rm.window_for(motion, -INF, *args) == _approx(rm.window_for(motion, 0.0, *args))


def test_ease_of_nan_is_the_start_of_the_shot():
    assert rm.ease(NAN) == 0.0 and rm.ease(INF) == 1.0 and rm.ease(-INF) == 0.0


BAD_FOCI = [(NAN, 0.5), (0.5, NAN), (INF, 0.5), (0.5, -INF), None, (0.5,), [], {"x": 0.5, "y": 0.5},
            ("a", "b"), 5, (10**400, 0.5), (0.5, -(10**400)), "37", "0.3", b"ab"]


@pytest.mark.parametrize("motion", MOTIONS)
@pytest.mark.parametrize("focus", BAD_FOCI, ids=lambda focus: repr(focus)[:24])
def test_malformed_or_non_finite_focus_falls_back_to_the_image_centre(motion, focus):
    for p in (0.0, 0.5, 1.0):
        got = rm.window_for(motion, p, 1536, 1024, focus)
        assert _finite(got)
        assert got == _approx(rm.window_for(motion, p, 1536, 1024, (0.5, 0.5)))


@pytest.mark.parametrize("motion", MOTIONS)
def test_out_of_range_focus_is_clamped_into_the_unit_square(motion):
    for p in (0.0, 0.5, 1.0):
        assert rm.window_for(motion, p, 1536, 1024, (5, -3)) == _approx(
            rm.window_for(motion, p, 1536, 1024, (1.0, 0.0)))
        assert rm.window_for(motion, p, 1536, 1024, (1e308, -1e308)) == _approx(
            rm.window_for(motion, p, 1536, 1024, (1.0, 0.0)))


@pytest.mark.parametrize("size", [(0, 1024), (1536, 0), (-1536, 1024), (1536, -1024), (0, 0),
                                  (INF, 1024), (1536, INF), (INF, INF), (-INF, 1024),
                                  (NAN, 1024), (1536, NAN)])
def test_non_positive_or_non_finite_image_size_raises(size):
    with pytest.raises(ValueError, match="image size"):
        rm.window_for("push_in", 0.5, *size, (0.5, 0.5))


@pytest.mark.parametrize("size", [(1366, 768), (1000, 562), (1921, 1080), (1920, 1080), (1023, 576)])
def test_near_16_by_9_images_never_get_a_window_larger_than_the_image(size):
    iw, ih = size
    bw, bh = rm.base_window(iw, ih)
    assert bw <= iw + 1e-9 and bh <= ih + 1e-9 and bw / bh == pytest.approx(16 / 9)
    for motion in MOTIONS:
        for p in (0.0, 0.5, 1.0):
            x0, y0, x1, y1 = rm.window_for(motion, p, iw, ih, (0.3, 0.6))
            assert x0 >= -1e-6 and y0 >= -1e-6 and x1 <= iw + 1e-6 and y1 <= ih + 1e-6


@pytest.mark.parametrize("motion", MOTIONS)
def test_windows_are_inside_the_image_with_zero_tolerance(motion):
    for (iw, ih), focus, p in itertools.product(SIZES, FOCI, (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0, -0.2, 1.4)):
        x0, y0, x1, y1 = rm.window_for(motion, p, iw, ih, focus)
        assert x0 >= 0 and y0 >= 0 and x1 <= iw and y1 <= ih, (motion, p, iw, ih, focus)


from PIL import Image  # noqa: E402  (Pillow rejects any negative or overshooting resize box)


@pytest.mark.parametrize("motion", MOTIONS)
@pytest.mark.parametrize("size", [(1536, 1024), (2000, 900), (1000, 1400)])
def test_windows_are_accepted_as_pillow_resize_boxes(motion, size):
    image = Image.new("L", size)
    for focus, p in itertools.product(FOCI, (0.0, 0.25, 0.5, 0.75, 1.0)):
        assert image.resize((32, 18), box=rm.window_for(motion, p, *size, focus)).size == (32, 18)


def test_a_huge_focus_is_clamped_to_the_corner_not_mistaken_for_non_finite():
    assert rm.window_for("push_in", 0.25, 1536, 1024, (1e308, 1e308)) == _approx(
        rm.window_for("push_in", 0.25, 1536, 1024, (1.0, 1.0)))


def test_numeric_strings_inside_a_sequence_are_accepted_like_numbers():
    assert rm.window_for("push_in", 0.25, 1536, 1024, ("0.3", "0.7")) == _approx(
        rm.window_for("push_in", 0.25, 1536, 1024, (0.3, 0.7)))


def test_unexpected_errors_from_the_focus_object_are_not_swallowed():
    class Broken:
        def __getitem__(self, index):
            raise RuntimeError("bug in the caller")

    with pytest.raises(RuntimeError):
        rm.window_for("push_in", 0.5, 1536, 1024, Broken())


@pytest.mark.parametrize("motion", MOTIONS)
@pytest.mark.parametrize("size", [(1536, 1024), (2000, 900), (1000, 1400)])
def test_out_of_range_focus_gives_exactly_the_boundary_window(motion, size):
    for p in (0.25, 0.5, 0.75):
        assert rm.window_for(motion, p, *size, (1.5, -0.5)) == rm.window_for(motion, p, *size, (1.0, 0.0))
        assert rm.window_for(motion, p, *size, (-0.5, 1.5)) == rm.window_for(motion, p, *size, (0.0, 1.0))
