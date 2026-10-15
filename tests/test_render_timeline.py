import copy
import math

import numpy as np
import pytest
from PIL import Image

import render_motion as rm
import render_overlays as ro
import render_timeline as rt


def solid(color):
    return Image.new("RGB", (1536, 1024), color)


IMAGES = {"a.png": solid((255, 0, 0)), "b.png": solid((0, 0, 255))}


def make_shots(overlays=()):
    return {"scenes": [{"id": "s1", "image": "a.png"}, {"id": "s2", "image": "b.png"}],
            "beats": [{"id": "b1", "scene": "s1", "start": 0.0, "end": 3.0, "motion": "push_in",
                       "focus": [0.5, 0.5], "overlays": list(overlays)},
                      {"id": "b2", "scene": "s2", "start": 3.0, "end": 6.0, "motion": "pull_out",
                       "focus": [0.5, 0.5], "overlays": []}]}


@pytest.fixture
def cjk_font():
    try:
        ro.find_font(True)
        ro.find_font(False)
    except FileNotFoundError:
        pytest.skip("no CJK font installed")


def pixel(frame, x=960, y=270):
    return np.asarray(frame)[y, x]


def test_frame_has_the_output_size_and_shows_the_current_beat():
    composer = rt.FrameComposer(make_shots(), IMAGES, [], 6.0)
    frame = composer.compose(30)
    assert frame.size == (1920, 1080)
    r, g, b = pixel(frame)
    assert r >= 253 and b <= 2
    r, g, b = pixel(composer.compose(150))
    assert b >= 253 and r <= 2


def test_beats_are_made_contiguous_and_cover_the_whole_duration():
    shots = make_shots()
    shots["beats"][0]["end"] = 2.5
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0)
    assert composer.beats[0]["start"] == 0.0 and composer.beats[0]["end"] == 3.0
    assert composer.beats[-1]["end"] == 6.0
    assert shots["beats"][0]["end"] == 2.5, "input must not be mutated"


def test_crossfade_blends_halfway_at_the_boundary_only():
    composer = rt.FrameComposer(make_shots(), IMAGES, [], 6.0)
    r, g, b = pixel(composer.compose(90))
    assert 120 <= r <= 135 and 120 <= b <= 135
    r, g, b = pixel(composer.compose(80))
    assert r > 135 and b < 120


def test_card_appears_only_inside_its_window(cjk_font):
    card = {"text": "1974年", "label": "年代", "at": 1.0, "dur": 2.0}
    with_card = rt.FrameComposer(make_shots([card]), IMAGES, [], 6.0)
    without = rt.FrameComposer(make_shots(), IMAGES, [], 6.0)
    region = (slice(650, 790), slice(100, 520))
    inside = np.abs(np.asarray(with_card.compose(60), dtype=int) - np.asarray(without.compose(60), dtype=int))
    outside = np.abs(np.asarray(with_card.compose(5), dtype=int) - np.asarray(without.compose(5), dtype=int))
    assert inside[region].sum() > 0
    assert outside.sum() == 0


def test_subtitle_is_drawn_while_its_cue_is_active(cjk_font):
    cues = [(0.0, 3.0, "測試字幕")]
    with_cue = rt.FrameComposer(make_shots(), IMAGES, cues, 6.0)
    band = (slice(1080 - 150, 1080 - 40), slice(0, 1920))

    def white(frame):
        return int((np.asarray(frame)[band].min(axis=2) > 240).sum())

    assert white(with_cue.compose(30)) > 50
    assert white(with_cue.compose(120)) == 0


def test_a_failing_motion_falls_back_to_a_gentle_push_instead_of_failing(monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(rt, "window_for", broken)
    composer = rt.FrameComposer(make_shots(), IMAGES, [], 6.0)
    frame = composer.compose(30)
    assert frame.size == (1920, 1080) and composer.fallback_beats == {"b1"}
    composer.compose(150)
    assert composer.fallback_beats == {"b1", "b2"}


# ---------------------------------------------------------------------------
# Beyond the brief: helpers
# ---------------------------------------------------------------------------

CARD_GREEN = (0, 255, 0)
CARD_SIZE = (200, 100)
SUBTITLE_SPECS = {"A": ((101, 31), (255, 0, 255)), "B": ((61, 21), (0, 255, 255)),
                  "wide": ((2400, 40), (255, 0, 255))}


@pytest.fixture
def fake_overlays(monkeypatch):
    """Font-independent overlays: opaque solid rectangles whose size and colour the tests control."""
    calls = {"card": [], "subtitle": []}

    def card(text, label):
        calls["card"].append((text, label))
        colour = {"Y": (255, 255, 0)}.get(text, CARD_GREEN)
        return Image.new("RGBA", CARD_SIZE, colour + (255,))

    def subtitle(text):
        calls["subtitle"].append(text)
        size, colour = SUBTITLE_SPECS[text]
        return Image.new("RGBA", size, colour + (255,))

    monkeypatch.setattr(rt, "render_card", card)
    monkeypatch.setattr(rt, "render_subtitle", subtitle)
    return calls


@pytest.fixture
def window_calls(monkeypatch):
    calls = []
    real = rt.window_for

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(rt, "window_for", spy)
    return calls


@pytest.fixture
def gradients():
    arr = np.zeros((1024, 1536, 3), np.uint8)
    arr[..., 0] = np.linspace(0, 255, 1536, dtype=np.float32)[None, :]
    arr[..., 1] = np.linspace(0, 255, 1024, dtype=np.float32)[:, None]
    arr[..., 2] = np.random.default_rng(7).integers(0, 256, (1024, 1536))  # texture: interpolation kernels differ on noise, not on ramps
    return {"a.png": Image.fromarray(arr), "b.png": Image.fromarray(255 - arr)}


def broken_window(*args, **kwargs):
    raise ValueError("boom")


def card_dict(at=1.0, dur=2.0, text="1974年", label="年代"):
    return {"text": text, "label": label, "at": at, "dur": dur}


def shots_for(spans, motions=None, focus=(0.5, 0.5)):
    """Beats over the given (start, end) spans, alternating the two scenes of make_shots()."""
    beats = [{"id": f"b{k + 1}", "scene": "s1" if k % 2 == 0 else "s2", "start": a, "end": b,
              "motion": (motions or {}).get(k, "push_in"), "focus": list(focus), "overlays": []}
             for k, (a, b) in enumerate(spans)]
    return {**make_shots(), "beats": beats}


def bbox_of(frame, colour):
    ys, xs = np.where(np.all(np.asarray(frame) == colour, axis=2))
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def max_diff(a, b):
    return int(np.abs(np.asarray(a, dtype=int) - np.asarray(b, dtype=int)).max())


def reference(image, beat, t, size=(1920, 1080)):
    """The single-beat frame computed straight from the motion library, without FrameComposer."""
    return old_way_shade(window_frame(image, beat, t, size))


def window_frame(image, beat, t, size):
    progress = (t - beat["start"]) / (beat["end"] - beat["start"])
    box = rm.window_for(beat["motion"], progress, *image.size, beat["focus"])
    return image.resize(size, Image.BICUBIC, box=box)


def old_way_shade(frame):
    """The float32 broadcast multiply the composer used before it was tiled (kept as the bit-exact reference)."""
    return (np.asarray(frame, dtype=np.float32) * ro.shade_factor(*frame.size)).astype(np.uint8)


def structured_shots():
    shots = make_shots()
    shots["beats"][0].update(motion="pan_lr", focus=[0.8, 0.3])
    shots["beats"][1].update(motion="detail", focus=[0.2, 0.7])
    return shots


RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)
COLOUR_IMAGES = {"r.png": Image.new("RGB", (160, 90), RED), "g.png": Image.new("RGB", (160, 90), GREEN),
                 "u.png": Image.new("RGB", (160, 90), BLUE)}


def colour_shots(spans):
    """One solid-colour scene per beat (red, green, blue, red, ...), for tests that count who is visible."""
    names = ["r.png", "g.png", "u.png"]
    scenes = [{"id": f"s{k + 1}", "image": names[k % 3]} for k in range(len(spans))]
    beats = [{"id": f"b{k + 1}", "scene": f"s{k + 1}", "start": a, "end": b, "motion": "push_in",
              "focus": [0.5, 0.5], "overlays": []} for k, (a, b) in enumerate(spans)]
    return {"scenes": scenes, "beats": beats}


def smoothstep(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def mix(a, b, alpha):
    return [a[i] * (1 - alpha) + b[i] * alpha for i in range(3)]


# ---------------------------------------------------------------------------
# Construction: malformed shots fail early and clearly
# ---------------------------------------------------------------------------

def test_a_shot_list_without_beats_is_rejected_up_front():
    shots = make_shots()
    shots["beats"] = []
    with pytest.raises(ValueError, match="no beats"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)


def test_a_beat_of_an_unknown_scene_is_rejected_naming_it():
    shots = make_shots()
    shots["beats"][1]["scene"] = "s9"
    with pytest.raises(ValueError, match="b2.*s9"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)


def test_a_missing_scene_image_is_rejected_at_construction_naming_the_file():
    with pytest.raises(ValueError, match="b.png"):
        rt.FrameComposer(make_shots(), {"a.png": IMAGES["a.png"]}, [], 6.0)


def test_a_beat_without_a_scene_is_rejected_naming_the_beat():
    shots = make_shots()
    del shots["beats"][1]["scene"]
    with pytest.raises(ValueError, match="b2.*scene"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)


@pytest.mark.parametrize("key", ["id", "image"])
def test_a_scene_without_an_id_or_an_image_is_rejected(key):
    shots = make_shots()
    del shots["scenes"][1][key]
    with pytest.raises(ValueError, match="scene.*id.*image"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)


def test_a_scene_that_is_not_an_object_is_rejected():
    shots = make_shots()
    shots["scenes"][1] = "s2"
    with pytest.raises(ValueError, match="scene.*id.*image"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)


def test_a_beat_without_an_id_is_still_reported_as_a_value_error_not_a_key_error():
    shots = make_shots()
    del shots["beats"][1]["id"]
    shots["beats"][1]["scene"] = "s9"
    with pytest.raises(ValueError, match="s9"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)
    shots["beats"][1]["scene"] = "s2"
    with pytest.raises(ValueError, match="b.png"):
        rt.FrameComposer(shots, {"a.png": IMAGES["a.png"]}, [], 6.0)


@pytest.mark.parametrize("k", [0, 1])
@pytest.mark.parametrize("patch", [{"start": "soon"}, {"start": float("nan")}, {"end": float("inf")}, {"end": None},
                                   {"end": 10 ** 400}])
def test_beats_with_unusable_times_are_rejected_naming_the_beat(k, patch):
    shots = make_shots()
    shots["beats"][k].update(patch)
    with pytest.raises(ValueError, match=f"b{k + 1}"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)


@pytest.mark.parametrize("k", [0, 1])
@pytest.mark.parametrize("key", ["start", "end"])
def test_a_beat_without_a_start_or_an_end_is_rejected_naming_the_beat(k, key):
    shots = make_shots()
    del shots["beats"][k][key]
    with pytest.raises(ValueError, match=f"b{k + 1}"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)


def test_a_beat_that_overruns_the_next_one_is_cut_at_the_next_start():
    shots = make_shots()
    shots["beats"][0]["end"] = 3.5
    assert rt.FrameComposer(shots, IMAGES, [], 6.0).beats[0]["end"] == 3.0


def test_beat_and_overlay_times_are_normalised_to_floats(fake_overlays):
    shots = make_shots([{"text": "x", "label": "y", "at": "1.0", "dur": 2}])
    shots["beats"][1]["start"] = "3"
    shots["beats"][1]["end"] = 6
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0)
    assert [type(b[key]) for b in composer.beats for key in ("start", "end")] == [float] * 4
    assert [(at, dur) for at, dur, _ in composer.cards] == [(1.0, 2.0)]
    assert all(type(v) is float for at, dur, _ in composer.cards for v in (at, dur))


def test_beats_out_of_chronological_order_are_rejected():
    shots = make_shots()
    shots["beats"][0]["start"] = 5.0
    with pytest.raises(ValueError, match="order"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)


@pytest.mark.parametrize("overlay", [
    {"text": "x", "label": "y", "dur": 1.0},
    {"text": "x", "label": "y", "at": float("nan"), "dur": 1.0},
    {"text": "x", "label": "y", "at": 1.0, "dur": float("inf")},
    {"text": "x", "label": "y", "at": "soon", "dur": 1.0},
    {"text": "x", "label": "y", "at": 10 ** 400, "dur": 1.0},
    {"text": 1974, "label": "y", "at": 1.0, "dur": 1.0},
    {"text": "x", "at": 1.0, "dur": 1.0},
    {"text": "x", "label": "y", "at": 1.0},
    {"label": "y", "at": 1.0, "dur": 1.0},
    {"text": "x", "label": 5, "at": 1.0, "dur": 1.0},
    None,
])
def test_a_malformed_overlay_is_rejected_naming_the_beat(fake_overlays, overlay):
    shots = make_shots()
    shots["beats"][0]["overlays"] = [overlay]
    with pytest.raises(ValueError, match="b1.*overlay"):
        rt.FrameComposer(shots, IMAGES, [], 6.0)


@pytest.mark.parametrize("overlays", [None, "absent"])
def test_beats_without_an_overlays_list_simply_have_no_cards(fake_overlays, overlays):
    shots = make_shots()
    if overlays is None:
        shots["beats"][0]["overlays"] = None
    else:
        del shots["beats"][0]["overlays"]
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0)
    assert composer.cards == [] and composer.compose(30).size == (1920, 1080)


def test_inputs_are_never_mutated_or_aliased():
    shots = make_shots([{"text": "x", "label": "y", "at": 1.0, "dur": 1.0}])
    shots["beats"][0]["start"] = 0.4
    shots["beats"][0]["end"] = 2.5
    cues = [(0.0, 3.0, "字幕")]
    images = {"a.png": Image.new("RGBA", (64, 36), (255, 0, 0, 128)), "b.png": solid((0, 0, 255))}
    snapshot = copy.deepcopy((shots, cues))
    kept = dict(images)
    composer = rt.FrameComposer(shots, images, cues, 6.0, size=(320, 180))
    composer.compose(10)
    composer.compose(100)
    assert (shots, cues) == snapshot
    assert images == kept and all(images[k] is kept[k] for k in kept)
    assert images["a.png"].mode == "RGBA", "the caller's image must stay as it was"
    assert composer.beats[0] is not shots["beats"][0]


@pytest.mark.parametrize("mode, build", [
    ("RGBA", lambda: Image.new("RGBA", (64, 36), (255, 0, 0, 128))),
    ("L", lambda: Image.new("L", (64, 36), 200)),
    ("P", lambda: Image.new("RGB", (64, 36), (255, 0, 0)).convert("P")),
])
def test_non_rgb_images_are_converted_so_frames_stay_rgb_and_crossfades_work(mode, build):
    image = build()
    assert image.mode == mode
    composer = rt.FrameComposer(make_shots(), {"a.png": image, "b.png": solid((0, 0, 255))}, [], 6.0,
                                size=(320, 180))
    for n in (0, 30, 90, 150):
        frame = composer.compose(n)
        assert frame.mode == "RGB" and frame.size == (320, 180)
    r, g, b = np.asarray(composer.compose(30))[45, 160]
    if mode == "L":
        assert r == g == b and 190 <= r <= 200
    else:
        assert r >= 250 and g <= 5 and b <= 5


# ---------------------------------------------------------------------------
# Timeline: beat selection, progress and crossfade
# ---------------------------------------------------------------------------

def test_first_beat_starts_at_zero_and_later_beats_keep_their_start():
    shots = make_shots()
    shots["beats"][0]["start"] = 0.5
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0)
    assert composer.beats[0]["start"] == 0.0 and composer.beats[1]["start"] == 3.0


def test_progress_runs_from_zero_start_of_the_first_beat(window_calls):
    shots = make_shots()
    shots["beats"][0]["start"] = 0.5
    shots["beats"][0]["end"] = 3.0
    rt.FrameComposer(shots, IMAGES, [], 6.0).compose(45)
    motion, progress, iw, ih, focus = window_calls[0]
    assert (motion, iw, ih, focus) == ("push_in", 1536, 1024, [0.5, 0.5])
    assert progress == pytest.approx(0.5)


@pytest.mark.parametrize("total, last_end, progress", [(8.0, 8.0, 0.3), (4.0, 6.0, 0.5), (6.0, 6.0, 0.5)])
def test_last_beat_lasts_until_the_longer_of_its_own_end_and_the_audio(window_calls, total, last_end, progress):
    composer = rt.FrameComposer(make_shots(), IMAGES, [], total)
    assert composer.beats[-1]["end"] == last_end
    composer.compose(135)
    assert window_calls[-1][1] == pytest.approx(progress)


@pytest.mark.parametrize("n, colour", [(0, "red"), (-5, "red"), (-500, "red"), (150, "blue"), (179, "blue"),
                                       (180, "blue"), (195, "blue"), (5000, "blue")])
def test_frames_before_the_start_and_after_the_end_hold_the_nearest_beat(n, colour):
    composer = rt.FrameComposer(make_shots(), IMAGES, [], 6.0)
    r, g, b = pixel(composer.compose(n))
    assert (r >= 253 and b <= 2) if colour == "red" else (b >= 253 and r <= 2)


@pytest.mark.parametrize("n", [108, 111, 112, 120, 128, 129, 132])
def test_crossfade_is_a_smoothstep_over_exactly_the_crossfade_window(n):
    composer = rt.FrameComposer(make_shots(), IMAGES, [], 6.0, fps=40)
    x = min(max((n / 40 - 2.775) / 0.45, 0.0), 1.0)
    alpha = x * x * (3 - 2 * x)
    r, g, b = pixel(composer.compose(n))
    assert abs(r - 255 * (1 - alpha)) <= 2 and abs(b - 255 * alpha) <= 2


def test_the_crossfade_length_is_exactly_the_crossfade_constant_to_one_level():
    composer = rt.FrameComposer(make_shots(), IMAGES, [], 6.0, fps=40)
    alpha = smoothstep((114 / 40 - 2.775) / 0.45)
    r, g, b = pixel(composer.compose(114))
    assert abs(b - 255 * alpha) <= 1 and abs(r - 255 * (1 - alpha)) <= 1


@pytest.mark.parametrize("size", [(192, 108), (101, 57), (64, 200)])
def test_shaded_frames_are_bit_identical_to_the_float32_formula(gradients, size):
    composer = rt.FrameComposer(structured_shots(), gradients, [], 6.0, size=size, fps=40)
    first, second = composer.beats
    for n in (0, 50, 100, 150, 239):
        beat, image = (first, gradients["a.png"]) if n < 120 else (second, gradients["b.png"])
        assert composer.compose(n).tobytes() == reference(image, beat, n / 40, size).tobytes(), n
    t = 120 / 40
    blended = Image.blend(window_frame(gradients["a.png"], first, t, size),
                          window_frame(gradients["b.png"], second, t, size), rm.ease((t - 2.775) / 0.45))
    assert composer.compose(120).tobytes() == old_way_shade(blended).tobytes()


def test_single_beat_frames_match_an_independent_render_of_the_motion_window(gradients):
    composer = rt.FrameComposer(structured_shots(), gradients, [], 6.0)
    first, second = composer.beats
    for n in (0, 30, 60, 80):
        assert max_diff(composer.compose(n), reference(gradients["a.png"], first, n / 30)) == 0, n
    for n in (100, 150, 179):
        assert max_diff(composer.compose(n), reference(gradients["b.png"], second, n / 30)) == 0, n


def test_crossfade_frames_blend_each_beats_own_window(gradients):
    composer = rt.FrameComposer(structured_shots(), gradients, [], 6.0, fps=40)
    first, second = composer.beats
    start = reference(gradients["a.png"], first, 111 / 40)
    end = reference(gradients["b.png"], second, 129 / 40)
    middle = (reference(gradients["a.png"], first, 120 / 40).astype(float)
              + reference(gradients["b.png"], second, 120 / 40).astype(float)) / 2
    assert max_diff(composer.compose(111), start) <= 1
    assert max_diff(composer.compose(129), end) <= 1
    assert max_diff(composer.compose(120), middle) <= 3
    assert max_diff(composer.compose(120), start) > 40 and max_diff(composer.compose(120), end) > 40


def test_a_pan_actually_moves_across_the_picture_during_its_beat(gradients):
    shots = structured_shots()
    shots["beats"][0].update(motion="pan_lr", focus=[0.5, 0.5])
    composer = rt.FrameComposer(shots, gradients, [], 6.0)
    means = [np.asarray(composer.compose(n))[..., 0].mean() for n in (0, 30, 60, 80)]
    assert means == sorted(means) and means[-1] - means[0] > 40


def test_frames_are_shaded_darker_at_the_bottom_and_corners():
    composer = rt.FrameComposer(make_shots(), IMAGES, [], 6.0)
    frame = np.asarray(composer.compose(30))
    expected = ro.shade_factor(1920, 1080)
    for x, y in ((960, 1070), (0, 0), (1919, 540)):
        assert abs(int(frame[y, x, 0]) - 255 * float(expected[y, x, 0])) <= 1.5
    assert frame[1070, 960, 0] < 130 and frame[0, 0, 0] < 200 and frame[270, 960, 0] >= 253


def test_the_window_comes_from_the_motion_library_with_the_beats_own_arguments(window_calls):
    shots = shots_for([(0.0, 4.0)], motions={0: "detail"}, focus=(0.25, 0.75))
    rt.FrameComposer(shots, IMAGES, [], 4.0).compose(30)
    motion, progress, iw, ih, focus = window_calls[0]
    assert (motion, iw, ih, focus) == ("detail", 1536, 1024, [0.25, 0.75])
    assert progress == pytest.approx(0.25)


def test_a_zero_length_beat_does_not_divide_by_zero(window_calls):
    shots = shots_for([(0.0, 3.0), (3.0, 3.0), (3.0, 6.0)], motions={1: "detail"})
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0, size=(480, 270))
    for n in range(80, 106):
        assert composer.compose(n).size == (480, 270)
    assert composer.fallback_beats == set()
    assert all(math.isfinite(call[1]) for call in window_calls)


def test_a_zero_length_last_beat_held_after_the_audio_ends_does_not_divide_by_zero(window_calls):
    shots = shots_for([(0.0, 3.0), (3.0, 3.0)])
    composer = rt.FrameComposer(shots, IMAGES, [], 3.0, size=(192, 108))
    for n in (60, 89, 90, 100):
        r, g, b = pixel(composer.compose(n), 96, 27)
        assert (r >= 253 and b <= 2) if n < 90 else (b >= 253 and r <= 2), n
    assert composer.fallback_beats == set()
    assert all(math.isfinite(call[1]) for call in window_calls)


def test_the_progress_of_a_short_beat_is_measured_against_its_own_length(window_calls):
    shots = shots_for([(0.0, 3.0), (3.0, 3.2), (3.2, 6.0)], motions={1: "detail"})
    rt.FrameComposer(shots, IMAGES, [], 6.0, size=(192, 108), fps=25).compose(78)
    progress = [call[1] for call in window_calls if call[0] == "detail"]
    assert progress == [pytest.approx(0.6)]


def test_a_zero_length_beat_is_never_shown():
    spans = [(0.0, 3.0), (3.0, 3.0), (3.0, 6.0), (6.0, 9.0)]
    composer = rt.FrameComposer(colour_shots(spans), COLOUR_IMAGES, [], 9.0, size=(64, 36), fps=40)
    for n in range(100, 140):
        r, g, b = np.asarray(composer.compose(n))[18, 32]
        assert g <= 2, n
        assert (r >= 253) if n < 120 else (b >= 253), n


SHORT_MIDDLE = [(0.0, 2.5), (2.5, 2.6), (2.6, 6.0)]


def short_middle_colour(t):
    """Red -> green fades over [2.45, 2.55], green -> blue over [2.55, 2.65]: each fade is limited to the short beat."""
    if t < 2.55:
        return mix(RED, GREEN, smoothstep((t - 2.45) / 0.1))
    return mix(GREEN, BLUE, smoothstep((t - 2.55) / 0.1))


def test_a_beat_shorter_than_the_crossfade_limits_the_fade_on_both_of_its_sides():
    composer = rt.FrameComposer(colour_shots(SHORT_MIDDLE), COLOUR_IMAGES, [], 6.0, size=(64, 36), fps=1000)
    shade = float(ro.shade_factor(64, 36)[18, 32, 0])
    for t in (2.3, 2.44, 2.47, 2.5, 2.53, 2.55, 2.58, 2.6, 2.62, 2.66, 2.8):
        got = np.asarray(composer.compose(round(t * 1000)))[18, 32]
        for channel, want in zip(got, short_middle_colour(t)):
            assert abs(int(channel) - want * shade) <= 2, (t, got, short_middle_colour(t))


def test_the_crossfade_never_jumps_when_beats_are_shorter_than_it():
    composer = rt.FrameComposer(colour_shots(SHORT_MIDDLE), COLOUR_IMAGES, [], 6.0, size=(64, 36), fps=1000)
    pixels = np.array([np.asarray(composer.compose(n))[18, 32] for n in range(2300, 2900)], dtype=int)
    assert np.abs(np.diff(pixels, axis=0)).max() <= 6


def test_each_boundary_fades_over_the_shorter_of_the_two_beats_around_it():
    composer = rt.FrameComposer(colour_shots([(0.0, 1.0), (1.0, 1.2), (1.2, 4.0)]), COLOUR_IMAGES, [], 4.0,
                                size=(64, 36), fps=1000)
    shade = float(ro.shade_factor(64, 36)[18, 32, 0])
    for t, want in ((0.85, RED), (0.95, mix(RED, GREEN, smoothstep(0.25))), (1.0, mix(RED, GREEN, 0.5)),
                    (1.05, mix(RED, GREEN, smoothstep(0.75))), (1.1, GREEN),
                    (1.15, mix(GREEN, BLUE, smoothstep(0.25))), (1.2, mix(GREEN, BLUE, 0.5)), (1.3, BLUE),
                    (1.5, BLUE)):
        got = np.asarray(composer.compose(round(t * 1000)))[18, 32]
        assert all(abs(int(c) - w * shade) <= 2 for c, w in zip(got, want)), (t, got, want)


def test_a_last_beat_that_ends_before_it_starts_does_not_take_over_earlier_frames():
    composer = rt.FrameComposer(colour_shots([(0.0, 3.0), (3.0, 2.0)]), COLOUR_IMAGES, [], 2.5,
                                size=(64, 36), fps=40)
    assert composer.beats[-1]["end"] == 2.5
    for n in (0, 60, 100, 119):
        r, g, b = np.asarray(composer.compose(n))[18, 32]
        assert r >= 253 and g <= 2, n
    r, g, b = np.asarray(composer.compose(121))[18, 32]
    assert g >= 253 and r <= 2


def test_beats_shorter_than_a_crossfade_still_give_valid_frames():
    composer = rt.FrameComposer(shots_for([(0.0, 2.5), (2.5, 2.6), (2.6, 6.0)]), IMAGES, [], 6.0, size=(480, 270))
    for n in range(60, 100):
        frame = composer.compose(n)
        assert frame.mode == "RGB" and frame.size == (480, 270)
    r, g, b = pixel(composer.compose(87), 240, 67)
    assert r >= 253 and b <= 2, "after every short beat's fade window the last beat shows alone"


# ---------------------------------------------------------------------------
# Motion failure fallback
# ---------------------------------------------------------------------------

def test_fallback_push_is_centred_and_grows_with_eased_progress(monkeypatch):
    boxes = []
    real = rt.make_box

    def spy(*args):
        boxes.append(args)
        return real(*args)

    monkeypatch.setattr(rt, "window_for", broken_window)
    monkeypatch.setattr(rt, "make_box", spy)
    rt.FrameComposer(shots_for([(0.0, 4.0)]), IMAGES, [], 4.0).compose(30)
    iw, ih, scale, cx, cy = boxes[0]
    assert (iw, ih, cx, cy) == (1536, 1024, 768.0, 512.0)
    assert scale == pytest.approx(1.0 + 0.08 * 0.15625)


def test_only_the_beat_whose_motion_fails_is_marked_as_fallback(monkeypatch):
    real = rt.window_for

    def flaky(motion, *args):
        if motion == "pull_out":
            raise ValueError("boom")
        return real(motion, *args)

    monkeypatch.setattr(rt, "window_for", flaky)
    composer = rt.FrameComposer(make_shots(), IMAGES, [], 6.0)
    composer.compose(30)
    composer.compose(150)
    assert composer.fallback_beats == {"b2"}


def test_an_unknown_motion_degrades_to_the_gentle_push_without_any_patching():
    shots = make_shots()
    shots["beats"][0]["motion"] = "spin"
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0)
    assert composer.compose(30).size == (1920, 1080) and composer.fallback_beats == {"b1"}


@pytest.mark.parametrize("key", ["motion", "focus"])
def test_a_beat_missing_its_motion_or_focus_degrades_to_the_gentle_push(key):
    shots = make_shots()
    del shots["beats"][0][key]
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0)
    assert composer.compose(30).size == (1920, 1080) and composer.fallback_beats == {"b1"}


def test_a_garbage_focus_is_handled_by_the_motion_library_not_the_fallback():
    shots = make_shots()
    shots["beats"][0]["focus"] = [float("nan"), "x"]
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0)
    assert composer.compose(30).size == (1920, 1080) and composer.fallback_beats == set()


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size, pos", [((1920, 1080), (96, 640)), ((1280, 720), (64, 427)),
                                       ((1000, 600), (50, 356)), ((1030, 580), (52, 344))])
def test_card_position_scales_with_the_frame_size_and_is_rounded(fake_overlays, size, pos):
    composer = rt.FrameComposer(make_shots([card_dict()]), IMAGES, [], 6.0, size=size)
    assert bbox_of(composer.compose(60), CARD_GREEN) == (*pos, pos[0] + 200, pos[1] + 100)


@pytest.mark.parametrize("n, green", [(40, 0), (47, 127), (54, 255), (80, 255), (113, 127), (120, 0), (121, 0)])
def test_card_fades_in_and_out_over_the_card_fade_time(fake_overlays, n, green):
    composer = rt.FrameComposer(make_shots([card_dict(at=1.0, dur=2.0)]), IMAGES, [], 6.0, fps=40)
    assert abs(int(np.asarray(composer.compose(n))[690, 196, 1]) - green) <= 2


def test_a_card_too_short_for_both_fades_never_reaches_full_opacity(fake_overlays):
    composer = rt.FrameComposer(make_shots([card_dict(at=1.0, dur=0.35)]), IMAGES, [], 6.0, fps=40)
    peak = max(int(np.asarray(composer.compose(n))[690, 196, 1]) for n in range(40, 55))
    assert 120 <= peak <= 135


def test_card_times_are_absolute_whichever_beat_lists_the_card(fake_overlays):
    shots = make_shots()
    shots["beats"][1]["overlays"] = [card_dict(at=1.0, dur=2.0)]
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0)
    assert bbox_of(composer.compose(45), CARD_GREEN) is not None
    assert bbox_of(composer.compose(150), CARD_GREEN) is None
    assert fake_overlays["card"] == [("1974年", "年代")], "render_card gets (text, label), once per overlay"


def test_a_card_longer_than_its_beat_stays_up_across_the_crossfade(fake_overlays):
    composer = rt.FrameComposer(make_shots([card_dict(at=2.0, dur=2.5)]), IMAGES, [], 6.0)
    for n in (75, 90, 100, 120):
        assert bbox_of(composer.compose(n), CARD_GREEN) is not None, n
    assert bbox_of(composer.compose(140), CARD_GREEN) is None


def test_overlapping_cards_do_not_crash_and_the_later_one_is_on_top(fake_overlays):
    shots = make_shots([card_dict(at=1.0, dur=3.0, text="X"), card_dict(at=1.5, dur=2.0, text="Y")])
    composer = rt.FrameComposer(shots, IMAGES, [], 6.0)
    assert bbox_of(composer.compose(60), (255, 255, 0)) == (96, 640, 296, 740)
    assert bbox_of(composer.compose(45), CARD_GREEN) == (96, 640, 296, 740)


def test_all_cards_of_all_beats_are_rendered_up_front(fake_overlays):
    shots = make_shots([card_dict(text="X")])
    shots["beats"][1]["overlays"] = [card_dict(at=4.0, text="Y")]
    assert len(rt.FrameComposer(shots, IMAGES, [], 6.0).cards) == 2
    assert fake_overlays["card"] == [("X", "年代"), ("Y", "年代")]


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720)])
def test_subtitle_is_centred_above_the_bottom_margin(fake_overlays, size):
    width, height = size
    composer = rt.FrameComposer(make_shots(), IMAGES, [(0.0, 3.0, "A")], 6.0, size=size)
    x, y = (width - 101) // 2, height - 31 - 52
    assert bbox_of(composer.compose(30), (255, 0, 255)) == (x, y, x + 101, y + 31)


@pytest.mark.parametrize("n, shown", [(29, False), (30, True), (59, True), (60, False)])
def test_subtitle_is_shown_from_its_start_up_to_but_excluding_its_end(fake_overlays, n, shown):
    composer = rt.FrameComposer(make_shots(), IMAGES, [(1.0, 2.0, "A")], 6.0)
    assert (bbox_of(composer.compose(n), (255, 0, 255)) is not None) is shown


def test_only_the_first_of_overlapping_cues_is_drawn(fake_overlays):
    composer = rt.FrameComposer(make_shots(), IMAGES, [(1.0, 3.0, "A"), (2.0, 4.0, "B")], 6.0)
    both = composer.compose(75)
    assert bbox_of(both, (255, 0, 255)) is not None and bbox_of(both, (0, 255, 255)) is None
    later = composer.compose(105)
    assert bbox_of(later, (255, 0, 255)) is None and bbox_of(later, (0, 255, 255)) is not None
    assert fake_overlays["subtitle"] == ["A", "B"], "each cue is rendered once, up front"


def test_a_cue_wider_than_the_frame_is_clipped_without_failing(fake_overlays):
    composer = rt.FrameComposer(make_shots(), IMAGES, [(0.0, 3.0, "wide")], 6.0)
    frame = np.asarray(composer.compose(30))
    assert frame.shape == (1080, 1920, 3)
    assert np.all(frame[1080 - 40 - 52: 1080 - 52] == (255, 0, 255))


def test_a_card_and_a_subtitle_can_share_a_frame(fake_overlays):
    composer = rt.FrameComposer(make_shots([card_dict()]), IMAGES, [(0.0, 3.0, "A")], 6.0)
    frame = composer.compose(60)
    assert bbox_of(frame, CARD_GREEN) is not None and bbox_of(frame, (255, 0, 255)) is not None


# ---------------------------------------------------------------------------
# frame = f(n): purity, no accumulated state
# ---------------------------------------------------------------------------

def busy_composer():
    shots = make_shots([card_dict(at=1.0, dur=2.5), card_dict(at=2.8, dur=1.0, text="Y")])
    return rt.FrameComposer(shots, IMAGES, [(0.5, 2.0, "A"), (2.0, 4.0, "B")], 6.0, size=(640, 360))


FRAMES = [0, 30, 60, 85, 90, 100, 120, 125, 140, 179]


@pytest.mark.parametrize("broken", [False, True])
def test_compose_is_a_pure_function_of_the_frame_number(fake_overlays, monkeypatch, broken):
    if broken:
        monkeypatch.setattr(rt, "window_for", broken_window)
    forward = {n: busy_composer().compose(n).tobytes() for n in FRAMES}
    composer = busy_composer()
    backward = {n: composer.compose(n).tobytes() for n in reversed(FRAMES)}
    again = {n: composer.compose(n).tobytes() for n in FRAMES}
    assert forward == backward == again


def test_composing_frames_does_not_accumulate_state(fake_overlays):
    composer = busy_composer()
    before = {k: len(v) for k, v in vars(composer).items() if hasattr(v, "__len__")}
    for n in range(0, 180, 7):
        composer.compose(n)
    assert {k: len(v) for k, v in vars(composer).items() if hasattr(v, "__len__")} == before
    assert composer.fallback_beats == set()


# ---------------------------------------------------------------------------
# Real fonts (skipped when no CJK font is installed)
# ---------------------------------------------------------------------------

def test_real_card_and_subtitle_are_drawn_together_without_hiding_each_other(cjk_font):
    card = {"text": "1974年", "label": "年代", "at": 1.0, "dur": 2.0}
    both = rt.FrameComposer(make_shots([card]), IMAGES, [(0.0, 3.0, "測試字幕")], 6.0)
    plain = rt.FrameComposer(make_shots(), IMAGES, [], 6.0)
    frame, bare = np.asarray(both.compose(60)), np.asarray(plain.compose(60))
    assert np.abs(frame[650:790, 100:520].astype(int) - bare[650:790, 100:520]).sum() > 0
    assert (frame[1080 - 150: 1080 - 40].min(axis=2) > 240).sum() > 50


def test_a_real_subtitle_longer_than_the_frame_still_renders(cjk_font):
    composer = rt.FrameComposer(make_shots(), IMAGES, [(0.0, 3.0, "測試字幕" * 18)], 6.0)
    band = np.asarray(composer.compose(30))[1080 - 150: 1080 - 40]
    white = band.min(axis=2) > 240
    assert white[:, :100].any() and white[:, -100:].any(), "text spans the whole width"
