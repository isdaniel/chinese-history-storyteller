import base64
import io
import json

import pytest
from PIL import Image

import ground_focus as gf
from fakes import FakeClient


def png_bytes(color=(1, 2, 3)):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


BEATS = [{"id": "b1", "scene": "s1", "focus": [0.5, 0.5], "focus_hint": "the farmer"},
         {"id": "b2", "scene": "s1", "focus": [0.5, 0.5], "focus_hint": "the mounds"}]


def test_parse_targets_clamps_and_ignores_bad_entries():
    data = {"targets": [{"beat": "b1", "x": 0.0, "y": 1.5}, {"beat": "zz", "x": 0.3, "y": 0.3},
                        {"beat": "b2", "x": "left", "y": 0.2}, {"beat": "b2"}, "junk",
                        {"beat": "b2", "x": "0.7", "y": 0.4}]}
    assert gf.parse_targets(data, {"b1", "b2"}) == {"b1": [0.05, 0.95], "b2": [0.7, 0.4]}


def test_parse_targets_tolerates_malformed_output():
    assert gf.parse_targets(None, {"b1"}) == {} and gf.parse_targets({"targets": "x"}, {"b1"}) == {}


def test_ground_scene_returns_focus_points(tmp_path):
    image = tmp_path / "img_s1.png"
    image.write_bytes(png_bytes())
    client = FakeClient([{"targets": [{"beat": "b1", "x": 0.2, "y": 0.3}, {"beat": "b2", "x": 0.8, "y": 0.6}]}])
    assert gf.ground_scene(client, image, BEATS) == {"b1": [0.2, 0.3], "b2": [0.8, 0.6]}
    assert "the farmer" in client.prompts()[0]
    assert client.calls[0]["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_ground_scene_failure_returns_empty(tmp_path):
    image = tmp_path / "img_s1.png"
    image.write_bytes(png_bytes())
    assert gf.ground_scene(FakeClient([RuntimeError("down")]), image, BEATS) == {}


def test_run_updates_only_grounded_beats_and_skips_fallback_scenes(tmp_path):
    for name in ("img_s1.png", "img_s2.png"):
        (tmp_path / name).write_bytes(png_bytes())
    shots = {"scenes": [{"id": "s1", "image": "img_s1.png"}, {"id": "s2", "image": "img_s2.png"}],
             "beats": BEATS + [{"id": "b3", "scene": "s2", "focus": [0.5, 0.5], "focus_hint": "x"}]}
    client = FakeClient([{"targets": [{"beat": "b1", "x": 0.2, "y": 0.3}]}])
    report = gf.run(client, shots, tmp_path, skip_scenes={"s2"})
    assert shots["beats"][0]["focus"] == [0.2, 0.3] and shots["beats"][1]["focus"] == [0.5, 0.5]
    assert shots["beats"][2]["focus"] == [0.5, 0.5]
    assert report == {"grounded": 1, "missing": ["b2", "b3"]}
    assert len(client.calls) == 1


def test_main_is_a_no_op_outside_shots_mode(monkeypatch):
    monkeypatch.setenv("VIDEO_MODE", "slideshow")
    assert gf.main(1) == 0


# ---- fix round 1: coordinate validation (I1) ----

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "nan", "NaN", "inf", "-Infinity",
                                 10 ** 400, True, False, None, [0.5], {"a": 1}])
def test_parse_targets_ignores_non_finite_overflow_bool_and_non_numeric(bad):
    data = {"targets": [{"beat": "b1", "x": bad, "y": 0.5}, {"beat": "b1", "x": 0.5, "y": bad},
                        {"beat": "b2", "x": 0.3, "y": 0.4}]}
    assert gf.parse_targets(data, {"b1", "b2"}) == {"b2": [0.3, 0.4]}


def test_ground_scene_nan_literal_from_model_never_reaches_focus(tmp_path):
    image = tmp_path / "i.png"
    image.write_bytes(png_bytes())
    client = FakeClient(['{"targets":[{"beat":"b1","x":NaN,"y":0.5},{"beat":"b2","x":0.3,"y":0.4}]}'])
    assert gf.ground_scene(client, image, BEATS) == {"b2": [0.3, 0.4]}


# ---- fix round 1: ground_scene is total (I2) ----

def test_ground_scene_huge_integer_only_drops_that_target(tmp_path):
    image = tmp_path / "i.png"
    image.write_bytes(png_bytes())
    reply = '{"targets":[{"beat":"b1","x":' + "9" * 400 + ',"y":0.5},{"beat":"b2","x":0.3,"y":0.4}]}'
    assert gf.ground_scene(FakeClient([reply]), image, BEATS) == {"b2": [0.3, 0.4]}


def test_ground_scene_missing_image_returns_empty(tmp_path):
    client = FakeClient([])
    assert gf.ground_scene(client, tmp_path / "gone.png", BEATS) == {}
    assert client.calls == []


def test_run_survives_a_missing_image_file(tmp_path):
    (tmp_path / "b.png").write_bytes(png_bytes())
    shots = {"scenes": [{"id": "s1", "image": "gone.png"}, {"id": "s2", "image": "b.png"}],
             "beats": [{"id": "b1", "scene": "s1", "focus": [0.5, 0.5], "focus_hint": "a"},
                       {"id": "b2", "scene": "s2", "focus": [0.5, 0.5], "focus_hint": "b"}]}
    client = FakeClient([{"targets": [{"beat": "b2", "x": 0.6, "y": 0.6}]}])
    assert gf.run(client, shots, tmp_path) == {"grounded": 1, "missing": ["b1"]}
    assert [b["focus"] for b in shots["beats"]] == [[0.5, 0.5], [0.6, 0.6]]


# ---- fix round 1: shots-mode main (I3, I4) ----

def centred_beat(beat_id, scene, hint):
    return {"id": beat_id, "scene": scene, "focus": [0.5, 0.5], "focus_hint": hint}


def make_episode(tmp_path, report_text=None):
    shots = {"scenes": [{"id": "s1", "image": "img_s1.png"}, {"id": "s2", "image": "img_s2.png"}],
             "beats": [centred_beat("b1", "s1", "the farmer"), centred_beat("b2", "s1", "the mounds"),
                       centred_beat("b3", "s2", "x")]}
    (tmp_path / "shots.json").write_text(json.dumps(shots), encoding="utf-8")
    for name in ("img_s1.png", "img_s2.png"):
        (tmp_path / name).write_bytes(png_bytes())
    if report_text is not None:
        (tmp_path / "image_report.json").write_text(report_text, encoding="utf-8")


def patch_main(monkeypatch, tmp_path, client):
    monkeypatch.setenv("VIDEO_MODE", "shots")
    monkeypatch.setattr(gf, "get_episode_dir", lambda _eid: tmp_path)
    monkeypatch.setattr(gf, "make_client", lambda: client)


def test_main_shots_mode_grounds_skips_fallback_and_writes_both_files(tmp_path, monkeypatch):
    make_episode(tmp_path, json.dumps({"scenes": [{"scene": "s1", "status": "generated"},
                                                  {"scene": "s2", "status": "fallback"}]}))
    client = FakeClient([{"targets": [{"beat": "b1", "x": 0.2, "y": 0.3}]}])
    patch_main(monkeypatch, tmp_path, client)
    assert gf.main(1) == 0
    saved = json.loads((tmp_path / "shots.json").read_text(encoding="utf-8"))
    assert [b["focus"] for b in saved["beats"]] == [[0.2, 0.3], [0.5, 0.5], [0.5, 0.5]]
    assert json.loads((tmp_path / "ground_report.json").read_text(encoding="utf-8")) == \
        {"grounded": 1, "missing": ["b2", "b3"]}
    assert len(client.calls) == 1


def test_main_without_image_report_grounds_every_scene(tmp_path, monkeypatch):
    make_episode(tmp_path)
    client = FakeClient([{"targets": []}, {"targets": []}])
    patch_main(monkeypatch, tmp_path, client)
    assert gf.main(1) == 0
    assert len(client.calls) == 2


@pytest.mark.parametrize("report", ["[]", '{"scenes": null}', '{"scenes": [{"scene": "s1"}]}',
                                    '{"scenes": ["x"]}', '{"scenes": [', ""])
def test_main_tolerates_odd_image_report(tmp_path, monkeypatch, report):
    make_episode(tmp_path, report)
    client = FakeClient([{"targets": []}, {"targets": []}])
    patch_main(monkeypatch, tmp_path, client)
    assert gf.main(1) == 0
    assert len(client.calls) == 2
    assert (tmp_path / "shots.json").exists() and (tmp_path / "ground_report.json").exists()


def test_main_defaults_to_slideshow_when_video_mode_unset(monkeypatch):
    monkeypatch.delenv("VIDEO_MODE", raising=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("must not run outside shots mode")

    monkeypatch.setattr(gf, "get_episode_dir", boom)
    monkeypatch.setattr(gf, "make_client", boom)
    assert gf.main(1) == 0


# ---- fix round 1: what is sent to the model (I5) ----

def sent_image(call):
    return base64.b64decode(call["messages"][-1]["content"][1]["image_url"]["url"].split(",", 1)[1])


def test_ground_scene_prompt_lists_ids_hints_and_main_subject_fallback(tmp_path):
    image = tmp_path / "i.png"
    image.write_bytes(png_bytes())
    beats = [{"id": "b1", "focus_hint": "the farmer"}, {"id": "b2", "focus_hint": None},
             {"id": "b3", "focus_hint": ""}, {"id": "b4"}]
    client = FakeClient([{"targets": []}])
    gf.ground_scene(client, image, beats)
    prompt = client.prompts()[0]
    for line in ("- b1: the farmer", "- b2: main subject", "- b3: main subject", "- b4: main subject"):
        assert line in prompt


@pytest.mark.parametrize("reply", [ValueError("x"), ConnectionError("down"), TimeoutError("slow"),
                                   "not json", None, '{"targets": 5}'])
def test_ground_scene_any_failure_returns_empty(tmp_path, reply):
    image = tmp_path / "i.png"
    image.write_bytes(png_bytes())
    assert gf.ground_scene(FakeClient([reply]), image, BEATS) == {}


def test_ground_scene_ignores_ids_outside_the_scene(tmp_path):
    image = tmp_path / "i.png"
    image.write_bytes(png_bytes())
    client = FakeClient([{"targets": [{"beat": "b1", "x": 0.2, "y": 0.3}, {"beat": "b9", "x": 0.8, "y": 0.8}]}])
    assert gf.ground_scene(client, image, BEATS) == {"b1": [0.2, 0.3]}


def test_run_sends_each_scene_its_own_image_and_only_its_own_beats(tmp_path):
    (tmp_path / "a.png").write_bytes(png_bytes((10, 0, 0)))
    (tmp_path / "b.png").write_bytes(png_bytes((0, 10, 0)))
    shots = {"scenes": [{"id": "s1", "image": "a.png"}, {"id": "s2", "image": "b.png"}],
             "beats": [centred_beat("b1", "s1", "the farmer"), centred_beat("b2", "s1", "the mounds"),
                       centred_beat("b3", "s2", "the general")]}
    client = FakeClient([{"targets": [{"beat": "b1", "x": 0.1, "y": 0.1}, {"beat": "b2", "x": 0.2, "y": 0.2}]},
                         {"targets": [{"beat": "b3", "x": 0.7, "y": 0.7}, {"beat": "b1", "x": 0.9, "y": 0.9}]}])
    assert gf.run(client, shots, tmp_path) == {"grounded": 3, "missing": []}
    assert sent_image(client.calls[0]) == (tmp_path / "a.png").read_bytes()
    assert sent_image(client.calls[1]) == (tmp_path / "b.png").read_bytes()
    first_prompt, second_prompt = client.prompts()
    assert "the general" not in first_prompt and "the farmer" not in second_prompt
    assert [b["focus"] for b in shots["beats"]] == [[0.1, 0.1], [0.2, 0.2], [0.7, 0.7]]


def test_run_reports_missing_in_scene_order_not_sorted(tmp_path):
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(png_bytes())
    shots = {"scenes": [{"id": "s1", "image": "a.png"}, {"id": "s2", "image": "b.png"}],
             "beats": [{"id": "b9", "scene": "s1", "focus": [0.5, 0.5], "focus_hint": "a"},
                       {"id": "b10", "scene": "s2", "focus": [0.5, 0.5], "focus_hint": "b"},
                       {"id": "b2", "scene": "s2", "focus": [0.5, 0.5], "focus_hint": "c"}]}
    report = gf.run(FakeClient([{"targets": []}]), shots, tmp_path, skip_scenes={"s1"})
    assert report == {"grounded": 0, "missing": ["b9", "b10", "b2"]}
