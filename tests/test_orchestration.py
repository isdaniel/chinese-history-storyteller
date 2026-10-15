import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

import common
import generate_script as gs
import make_video
import notify
import run_episode_local as local
from fakes import FakeClient

REPO = Path(__file__).resolve().parents[1]
TOPIC = {"title": "題材", "category": "history", "key_points": ["一", "二"], "length_min": 12}


def test_script_length_defaults_are_unchanged(monkeypatch):
    for key in ("TARGET_LENGTH_MIN", "MIN_LENGTH_MIN", "MIN_SECTIONS", "SECTIONS_PER_EPISODE"):
        monkeypatch.delenv(key, raising=False)
    assert gs.resolve_length_min(TOPIC) == 9
    assert gs.resolve_length_min({**TOPIC, "length_min": 5}) == 8
    assert gs.required_sections() == 6


def test_short_mode_overrides(monkeypatch):
    monkeypatch.setenv("TARGET_LENGTH_MIN", "2")
    monkeypatch.setenv("MIN_LENGTH_MIN", "1")
    monkeypatch.setenv("SECTIONS_PER_EPISODE", "2")
    monkeypatch.setenv("MIN_SECTIONS", "2")
    assert gs.resolve_length_min(TOPIC) == 2
    assert gs.required_sections() == 2


def test_prompt_asks_for_narration_that_can_be_matched_to_pictures(monkeypatch):
    for key in ("TARGET_LENGTH_MIN", "MIN_LENGTH_MIN", "SECTIONS_PER_EPISODE"):
        monkeypatch.delenv(key, raising=False)
    prompt = gs.build_prompt(TOPIC)
    assert "不要出現「畫面」「鏡頭」" in prompt and "不要使用任何引號或括號" in prompt
    assert "驚嘆號不超過 3 個" in prompt and "具體的年代、地名、人物與數字" in prompt
    assert "{" not in prompt.replace("{{", "").replace("}}", "").split("# 輸出格式")[0]


def test_local_runner_never_touches_publishing_modules():
    assert local.STAGES == ("script", "tts", "plan", "images", "ground", "video")
    source = Path(local.__file__).read_text(encoding="utf-8")
    for forbidden in ("import_module(\"upload_youtube\")", "import_module(\"publish_podcast\")",
                      "import_module(\"notify\")", "mark_published"):
        assert forbidden not in source


def test_local_runner_env_and_stage_selection():
    env = local.build_env(161, short=True, style="cinematic")
    assert env["TOPIC_ID"] == "161" and env["VIDEO_MODE"] == "shots" and env["VISUAL_STYLE"] == "cinematic"
    assert env["MAX_IMAGES"] == "8" and env["SECTIONS_PER_EPISODE"] == "2"
    assert "MAX_IMAGES" not in local.build_env(161, short=False, style="ink")
    assert local.select_stages(None, None) == list(local.STAGES)
    assert local.select_stages("images", None) == ["images", "ground", "video"]
    assert local.select_stages("images", "tts") == ["tts"]


def _episode(tmp_path):
    import json
    shots = {"scenes": [{"id": "s1", "image": "img_s1.png"}, {"id": "s2", "image": "img_s2.png"}],
             "beats": [{"id": "b1", "unit_ids": [0], "scene": "s1"}, {"id": "b2", "unit_ids": [1], "scene": "s1"},
                       {"id": "b3", "unit_ids": [2], "scene": "s2"}]}
    timings = {"units": [{"id": 0, "section": 1}, {"id": 1, "section": 1}, {"id": 2, "section": 2}]}
    (tmp_path / "shots.json").write_text(json.dumps(shots), encoding="utf-8")
    (tmp_path / "timings.json").write_text(json.dumps(timings), encoding="utf-8")
    (tmp_path / "img_s1.png").write_bytes(b"one")
    (tmp_path / "img_s2.png").write_bytes(b"two")


def test_slideshow_inputs_reuse_the_first_scene_image_of_each_section(tmp_path):
    _episode(tmp_path)
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_02.png"]
    assert (tmp_path / "img_01.png").read_bytes() == b"one" and (tmp_path / "img_02.png").read_bytes() == b"two"
    assert make_video.make_slideshow_inputs(tmp_path) == []


def test_make_video_degrades_to_the_slideshow_when_rendering_fails(monkeypatch, tmp_path):
    import common
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    ep = common.get_episode_dir(5)
    _episode(ep)
    calls, alerts = [], []
    monkeypatch.setattr(make_video.render_video, "main", lambda eid: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(make_video.build_video, "main", lambda eid: calls.append(eid) or 0)
    monkeypatch.setattr(make_video, "notify_discord", alerts.append)
    assert make_video.main(5) == 0
    assert calls == [5] and (ep / "img_01.png").exists() and "降級" in alerts[0]


def test_make_video_uses_the_shot_renderer_when_it_works(monkeypatch, tmp_path):
    import common
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    ep = common.get_episode_dir(6)
    _episode(ep)
    rendered = []

    def render(eid):   # 真的渲染器會寫出 final.mp4;make_video 把「沒產出 final.mp4」視為失敗
        (ep / "final.mp4").write_bytes(b"mp4")
        rendered.append(eid)
        return 0

    monkeypatch.setattr(make_video.render_video, "main", render)
    monkeypatch.setattr(make_video.build_video, "main", lambda eid: (_ for _ in ()).throw(AssertionError("no")))
    assert make_video.main(6) == 0 and rendered == [6]


def test_make_video_slideshow_mode_skips_the_shot_renderer(monkeypatch):
    monkeypatch.setenv("VIDEO_MODE", "slideshow")
    monkeypatch.setattr(make_video.build_video, "main", lambda eid: 0)
    monkeypatch.setattr(make_video.render_video, "main", lambda eid: (_ for _ in ()).throw(AssertionError("no")))
    assert make_video.main(1) == 0


def test_make_video_requires_shots_json_in_shots_mode(monkeypatch, tmp_path):
    import common
    import pytest
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    with pytest.raises(RuntimeError, match="shots.json"):
        make_video.main(7)


def test_high_fallback_ratio_triggers_a_discord_alert(monkeypatch, tmp_path):
    import json
    alerts = []
    monkeypatch.setattr(make_video, "notify_discord", alerts.append)
    (tmp_path / "video_report.json").write_text(json.dumps({"fallback_beats": ["b1", "b2"], "beats": 5}))
    make_video.check_fallback_ratio(3, tmp_path)
    assert len(alerts) == 1 and "2/5" in alerts[0]
    alerts.clear()
    (tmp_path / "video_report.json").write_text(json.dumps({"fallback_beats": ["b1"], "beats": 5}))
    make_video.check_fallback_ratio(3, tmp_path)
    assert alerts == []


def test_notify_message_includes_run_statistics(tmp_path, monkeypatch):
    import json
    monkeypatch.delenv("IMAGE_UNIT_COST_USD", raising=False)
    (tmp_path / "image_report.json").write_text(json.dumps({"generated": 20, "fallback": 2, "skipped": 0}))
    (tmp_path / "video_report.json").write_text(json.dumps({"render_sec": 412.0, "fallback_beats": ["b1"], "beats": 80}))
    summary = notify.summarize_run(tmp_path)
    assert "圖片 22 張" in summary and "$1.32" in summary and "備援 2" in summary
    assert "渲染 412s" in summary and "1/80" in summary
    message = notify.build_message("標題", "https://youtu.be/x", summary)
    assert "標題" in message and "製作統計" in message
    assert "製作統計" not in notify.build_message("標題", "https://youtu.be/x", "")
    assert notify.summarize_run(tmp_path / "nothing") == ""


# ---------------------------------------------------------------- generate_script: length / section overrides

SCRIPT_ENV = ("TARGET_LENGTH_MIN", "MIN_LENGTH_MIN", "MIN_SECTIONS", "SECTIONS_PER_EPISODE", "CHARS_PER_MIN",
              "AZURE_OPENAI_GPT_DEPLOYMENT")


@pytest.fixture
def clean_script_env(monkeypatch):
    for key in SCRIPT_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("per_episode, expected", [(None, 6), ("10", 8), ("8", 6), ("7", 5), ("6", 4), ("3", 4)])
def test_required_sections_is_two_below_sections_per_episode_with_a_floor_of_four(
        monkeypatch, clean_script_env, per_episode, expected):
    if per_episode:
        monkeypatch.setenv("SECTIONS_PER_EPISODE", per_episode)
    assert gs.required_sections() == expected


@pytest.mark.parametrize("value, expected", [("2", 2), ("9", 9), ("", 6)])
def test_min_sections_overrides_the_default(monkeypatch, clean_script_env, value, expected):
    monkeypatch.setenv("MIN_SECTIONS", value)
    assert gs.required_sections() == expected


@pytest.mark.parametrize("topic_min, target, lowest, expected", [
    (12, None, None, 9), (5, None, None, 8), (12, "12", None, 10), (12, "30", None, 10), (9, "30", None, 9),
    (5, None, "3", 5), (2, None, "3", 3), (12, "2", "1", 2), (12, "2", None, 8)])
def test_resolve_length_min_clamps_between_min_length_and_ten(
        monkeypatch, clean_script_env, topic_min, target, lowest, expected):
    if target:
        monkeypatch.setenv("TARGET_LENGTH_MIN", target)
    if lowest:
        monkeypatch.setenv("MIN_LENGTH_MIN", lowest)
    assert gs.resolve_length_min({**TOPIC, "length_min": topic_min}) == expected


def test_prompt_numbers_the_new_narration_rules_after_the_length_check(clean_script_env):
    prompt = gs.build_prompt(TOPIC)
    for fragment in ("8. **旁白是給耳朵聽的**", "「畫面」「鏡頭」「如圖」「如你所見」", "9. 每句以「。」或「？」結尾",
                     "每句約 18~45 字", "10. 盡量在句子中點出具體的年代、地名、人物與數字",
                     "「公元前 210 年」「驪山」「數十萬人」"):
        assert fragment in prompt
    order = [prompt.index(marker) for marker in ("7. **字數自我檢查**", "8. **旁白", "9. 每句", "10. 盡量", "# 輸出格式")]
    assert order == sorted(order)


def test_prompt_template_only_uses_known_format_fields():
    import string
    template = (common.TEMPLATES_DIR / "script_prompt.txt").read_text(encoding="utf-8")
    fields = {name for _, name, _, _ in string.Formatter().parse(template) if name}
    assert fields <= {"title", "category", "key_points", "length_min", "target_chars", "min_chars", "sections",
                      "last_section", "chars_per_section", "min_chars_per_section"}


def _script_reply(sections):
    return {"title": "T", "sections": [{"section_id": i + 1, "narration": "字" * 3000} for i in range(sections)]}


def test_generate_builds_its_client_with_a_300_second_timeout(monkeypatch, clean_script_env):
    seen = []
    client = FakeClient([_script_reply(6)])
    monkeypatch.setattr(gs, "make_client", lambda **kwargs: seen.append(kwargs) or client)
    script = gs.generate(TOPIC)
    assert seen == [{"timeout": 300.0}] and len(script["sections"]) == 6
    assert client.calls[0]["model"] == "gpt-5-mini"


@pytest.mark.parametrize("reply", [_script_reply(5), {"title": "T"}])
def test_generate_rejects_scripts_with_too_few_sections(monkeypatch, clean_script_env, reply):
    monkeypatch.setattr(gs, "make_client", lambda **kwargs: FakeClient([reply]))
    with pytest.raises(RuntimeError, match="腳本格式錯誤"):
        gs.generate(TOPIC)


def test_generate_honours_min_sections(monkeypatch, clean_script_env):
    monkeypatch.setenv("MIN_SECTIONS", "2")
    monkeypatch.setattr(gs, "make_client", lambda **kwargs: FakeClient([_script_reply(2)]))
    assert len(gs.generate(TOPIC)["sections"]) == 2
    monkeypatch.setenv("MIN_SECTIONS", "3")
    monkeypatch.setattr(gs, "make_client", lambda **kwargs: FakeClient([_script_reply(2)]))
    with pytest.raises(RuntimeError, match="腳本格式錯誤"):
        gs.generate(TOPIC)


# ---------------------------------------------------------------- make_video: slideshow inputs (degradation rungs)

@pytest.fixture
def placeholder(monkeypatch):
    written = []

    def write(path):
        written.append(path.name)
        path.write_bytes(b"placeholder")

    monkeypatch.setattr(make_video, "_write_fallback", write)
    return written


def _put(ep, name, data):
    (ep / name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def test_slideshow_inputs_use_the_scene_of_the_first_beat_of_each_section(tmp_path, placeholder):
    _put(tmp_path, "shots.json", {
        "scenes": [{"id": "s1", "image": "img_s1.png"}, {"id": "s2", "image": "img_s2.png"}],
        "beats": [{"id": "b1", "unit_ids": [0, 1], "scene": "s1"}, {"id": "b2", "unit_ids": [2], "scene": "s2"},
                  {"id": "b3", "unit_ids": [3], "scene": "s2"}, {"id": "b4", "unit_ids": [4], "scene": "s1"}]})
    _put(tmp_path, "timings.json", {"units": [{"id": 0, "section": 1}, {"id": 1, "section": 1}, {"id": 2, "section": 1},
                                              {"id": 3, "section": 2}, {"id": 4, "section": 2}]})
    (tmp_path / "img_s1.png").write_bytes(b"one")
    (tmp_path / "img_s2.png").write_bytes(b"two")
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_02.png"]
    assert (tmp_path / "img_01.png").read_bytes() == b"one" and (tmp_path / "img_02.png").read_bytes() == b"two"
    assert placeholder == []


def test_slideshow_inputs_keep_existing_section_images_and_replace_empty_ones(tmp_path, placeholder):
    _episode(tmp_path)
    (tmp_path / "img_01.png").write_bytes(b"keep me")
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_02.png"]
    assert (tmp_path / "img_01.png").read_bytes() == b"keep me" and (tmp_path / "img_02.png").read_bytes() == b"two"
    (tmp_path / "img_02.png").write_bytes(b"")
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_02.png"]
    assert (tmp_path / "img_02.png").read_bytes() == b"two"


@pytest.mark.parametrize("damage", ["missing", "empty", "directory"])
def test_a_missing_or_unusable_scene_image_becomes_the_placeholder(tmp_path, placeholder, caplog, damage):
    _episode(tmp_path)
    scene = tmp_path / "img_s1.png"
    scene.unlink()
    if damage == "empty":
        scene.write_bytes(b"")
    elif damage == "directory":
        scene.mkdir()
    with caplog.at_level(logging.INFO):
        assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_02.png"]
    assert (tmp_path / "img_01.png").read_bytes() == b"placeholder" and (tmp_path / "img_02.png").read_bytes() == b"two"
    assert placeholder == ["img_01.png"]
    assert any(r.levelno == logging.ERROR and "找不到可用的場景圖" in r.getMessage() for r in caplog.records)


def test_a_failed_copy_falls_back_to_the_placeholder(monkeypatch, tmp_path, placeholder):
    _episode(tmp_path)

    def broken_copy(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(make_video.shutil, "copyfile", broken_copy)
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_02.png"]
    assert placeholder == ["img_01.png", "img_02.png"]


def test_a_file_that_cannot_be_inspected_counts_as_unusable(monkeypatch, tmp_path, placeholder):
    _episode(tmp_path)
    real_is_file = Path.is_file

    def is_file(self):
        if self.name == "img_s1.png":
            raise PermissionError("locked")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_02.png"]
    assert placeholder == ["img_01.png"]


def test_the_real_placeholder_is_a_readable_png(tmp_path):
    _put(tmp_path, "timings.json", {"sections": [{"section_id": 1}]})
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png"]
    with Image.open(tmp_path / "img_01.png") as image:
        assert image.format == "PNG" and image.size[0] > 0


def test_malformed_beats_units_and_scenes_are_skipped_not_fatal(tmp_path, placeholder):
    _put(tmp_path, "shots.json", {
        "scenes": ["junk", {"id": "s2", "image": "img_s2.png"}, {"id": 7, "image": "x.png"}, {"id": "s3"},
                   {"id": "s1", "image": "img_s1.png"}],
        "beats": ["junk", {"id": "e", "unit_ids": [], "scene": "s1"}, {"id": "n", "scene": "s1"},
                  {"id": "u", "unit_ids": [99], "scene": "s1"}, {"id": "a", "unit_ids": ["0"], "scene": "s1"},
                  {"id": "t", "unit_ids": [True], "scene": "s1"}, {"id": "z", "unit_ids": 5, "scene": "s1"},
                  {"id": "w", "unit_ids": "0", "scene": "s1"}, {"id": "k", "unit_ids": [0], "scene": "ghost"},
                  {"id": "l", "unit_ids": [0], "scene": ["s1"]}, {"id": "i", "unit_ids": [0], "scene": "s3"},
                  {"id": "ok", "unit_ids": [0], "scene": "s2"}, {"id": "late", "unit_ids": [0], "scene": "s1"}]})
    _put(tmp_path, "timings.json", {"units": ["junk", {"id": 0, "section": 1}, {"id": True, "section": 1},
                                              {"id": 1, "section": 1}, {"id": 3, "section": "2"}, {"id": 2}]})
    (tmp_path / "img_s1.png").write_bytes(b"one")
    (tmp_path / "img_s2.png").write_bytes(b"two")
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png"]
    assert (tmp_path / "img_01.png").read_bytes() == b"two"


def test_sections_come_from_timings_so_a_section_without_a_beat_still_gets_an_image(tmp_path, placeholder):
    _episode(tmp_path)
    _put(tmp_path, "timings.json", {"units": [{"id": 0, "section": 1}, {"id": 1, "section": 1}, {"id": 2, "section": 2}],
                                    "sections": [{"section_id": 1}, {"section_id": 2}, {"section_id": 3},
                                                 {"section_id": 4}]})
    (tmp_path / "img_04.png").write_bytes(b"existing four")
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_02.png", "img_03.png"]
    assert placeholder == ["img_03.png"] and (tmp_path / "img_04.png").read_bytes() == b"existing four"


def test_section_list_priority_is_timings_then_shot_plan_then_script(tmp_path, placeholder):
    _put(tmp_path, "script.json", {"sections": [{"section_id": 1}, {"section_id": 2}, {"section_id": 3}]})
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_02.png", "img_03.png"]
    for name in ("img_01.png", "img_02.png", "img_03.png"):
        (tmp_path / name).unlink()
    _episode(tmp_path)
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_02.png"]
    assert not (tmp_path / "img_03.png").exists()
    for name in ("img_01.png", "img_02.png"):
        (tmp_path / name).unlink()
    _put(tmp_path, "timings.json", {"units": [{"id": 0, "section": 1}], "sections": [{"section_id": 1}]})
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png"]


def test_section_ids_that_are_not_integers_are_ignored(tmp_path, placeholder):
    _put(tmp_path, "timings.json", {"sections": ["junk", {"section_id": True}, {"section_id": "2"}, {"section_id": 1},
                                                 {"section_id": 1}, {}]})
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png"]


@pytest.mark.parametrize("raw", ["{ not json", "", "[]", "null", '{"scenes": 5, "beats": "x"}', "{}"])
def test_unusable_shots_json_falls_back_to_timings_sections_and_existing_images(tmp_path, placeholder, caplog, raw):
    _put(tmp_path, "shots.json", raw)
    _put(tmp_path, "timings.json", {"sections": [{"section_id": 1}, {"section_id": 2}]})
    (tmp_path / "img_02.png").write_bytes(b"already there")
    with caplog.at_level(logging.INFO):
        assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png"]
    assert placeholder == ["img_01.png"] and (tmp_path / "img_02.png").read_bytes() == b"already there"
    assert any(r.levelno == logging.ERROR and "shots.json / timings.json" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw", [None, "{ broken", "[]"])
def test_unusable_timings_json_derives_sections_from_script_json(tmp_path, placeholder, raw):
    _put(tmp_path, "script.json", {"sections": [{"section_id": 1}, {"section_id": 2}, {"section_id": 3}]})
    if raw is not None:
        _put(tmp_path, "timings.json", raw)
    (tmp_path / "img_02.png").write_bytes(b"already there")
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_03.png"]
    assert placeholder == ["img_01.png", "img_03.png"]


def test_nothing_to_derive_sections_from_is_logged_loudly_and_not_fatal(tmp_path, placeholder, caplog):
    with caplog.at_level(logging.INFO):
        assert make_video.make_slideshow_inputs(tmp_path) == []
    assert any(r.levelno == logging.ERROR and "任何段落" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- make_video.main: the degradation ladder

@pytest.fixture
def ladder(monkeypatch, tmp_path, placeholder):
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    ep = common.get_episode_dir(9)
    _episode(ep)
    events = []
    monkeypatch.setattr(make_video, "notify_discord", lambda message: events.append(("alert", message)))

    def slideshow(eid):
        events.append(("build", eid, sorted(p.name for p in ep.iterdir())))
        (ep / "final.mp4").write_bytes(b"slideshow")
        return 0

    monkeypatch.setattr(make_video.build_video, "main", slideshow)
    return ep, events


def _alerts(events):
    return [e[1] for e in events if e[0] == "alert"]


def _builds(events):
    return [e for e in events if e[0] == "build"]


def _raises(exc):
    def render(eid):
        raise exc
    return render


def _returns(value, ep=None):
    def render(eid):
        if ep is not None:
            (ep / "final.mp4").write_bytes(b"mp4")
        return value
    return render


@pytest.mark.parametrize("kind, reason", [
    ("raises", "RenderError: boom"),
    ("nonzero", "回傳非零結束碼 1"),
    ("string_code", "回傳非零結束碼 'failed'"),
    ("no_final", "final.mp4"),
    ("empty_final", "final.mp4"),
])
def test_every_kind_of_render_failure_degrades_with_an_alert_that_names_the_reason(monkeypatch, ladder, kind, reason):
    ep, events = ladder
    if kind == "raises":
        render = _raises(make_video.render_video.RenderError("boom"))
    elif kind == "nonzero":
        render = _returns(1, ep)
    elif kind == "string_code":
        render = _returns("failed", ep)
    elif kind == "no_final":
        render = _returns(0)
    else:
        def render(eid):
            (ep / "final.mp4").write_bytes(b"")
            return 0
    monkeypatch.setattr(make_video.render_video, "main", render)
    assert make_video.main(9) == 0
    alert, = _alerts(events)
    assert "降級" in alert and "Episode 9" in alert and reason in alert
    build, = _builds(events)
    assert build[1] == 9 and "img_01.png" in build[2] and "img_02.png" in build[2]
    assert (ep / "final.mp4").read_bytes() == b"slideshow"


@pytest.mark.parametrize("code", [0, None])
def test_a_zero_or_none_return_with_a_final_video_is_a_success(monkeypatch, ladder, code):
    ep, events = ladder
    monkeypatch.setattr(make_video.render_video, "main", _returns(code, ep))
    assert make_video.main(9) == 0
    assert events == [] and (ep / "final.mp4").read_bytes() == b"mp4"


def test_degrade_discards_the_failed_attempt_before_building_the_slideshow(monkeypatch, ladder):
    ep, events = ladder
    for name in ("video_report.json", "contact_sheet.png"):
        (ep / name).write_bytes(b"stale")

    def half_written(eid):
        (ep / "final.mp4").write_bytes(b"half")
        raise RuntimeError("encoder died")

    monkeypatch.setattr(make_video.render_video, "main", half_written)
    assert make_video.main(9) == 0
    build, = _builds(events)
    assert not {"final.mp4", "video_report.json", "contact_sheet.png"} & set(build[2])
    assert (ep / "final.mp4").read_bytes() == b"slideshow"
    assert not (ep / "video_report.json").exists() and not (ep / "contact_sheet.png").exists()


def test_stale_outputs_of_an_earlier_run_never_pass_for_a_fresh_render(monkeypatch, ladder):
    ep, events = ladder
    for name in ("final.mp4", "video_report.json", "contact_sheet.png"):
        (ep / name).write_bytes(b"stale")
    monkeypatch.setattr(make_video.render_video, "main", lambda eid: 0)
    assert make_video.main(9) == 0
    assert len(_builds(events)) == 1 and (ep / "final.mp4").read_bytes() == b"slideshow"


def test_degrade_steps_happen_in_order_alert_then_inputs_then_slideshow(monkeypatch, ladder):
    ep, events = ladder
    monkeypatch.setattr(make_video.render_video, "main", _raises(RuntimeError("boom")))
    inputs = make_video.make_slideshow_inputs
    monkeypatch.setattr(make_video, "make_slideshow_inputs",
                        lambda ep_dir: events.append(("inputs",)) or inputs(ep_dir))
    make_video.main(9)
    assert [e[0] for e in events] == ["alert", "inputs", "build"]


def test_the_alert_is_sent_even_when_the_slideshow_itself_fails(monkeypatch, ladder):
    ep, events = ladder
    monkeypatch.setattr(make_video.render_video, "main", _raises(RuntimeError("boom")))

    def broken(eid):
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr(make_video.build_video, "main", broken)
    with pytest.raises(RuntimeError, match="ffmpeg died"):
        make_video.main(9)
    assert len(_alerts(events)) == 1 and "降級" in _alerts(events)[0]


def test_preparing_slideshow_inputs_failing_does_not_stop_the_slideshow(monkeypatch, ladder):
    ep, events = ladder
    monkeypatch.setattr(make_video.render_video, "main", _raises(RuntimeError("boom")))

    def broken(ep_dir):
        raise ValueError("odd shots.json")

    monkeypatch.setattr(make_video, "make_slideshow_inputs", broken)
    assert make_video.main(9) == 0
    assert len(_builds(events)) == 1


def test_a_stale_file_that_cannot_be_deleted_does_not_stop_the_degrade(monkeypatch, ladder):
    ep, events = ladder
    (ep / "video_report.json").write_bytes(b"stale")
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name == "video_report.json":
            raise PermissionError("locked")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(make_video.render_video, "main", _raises(RuntimeError("boom")))
    assert make_video.main(9) == 0
    assert len(_alerts(events)) == 1 and len(_builds(events)) == 1


def test_main_returns_the_slideshow_exit_code(monkeypatch, ladder):
    ep, events = ladder
    monkeypatch.setattr(make_video.render_video, "main", _raises(RuntimeError("boom")))
    monkeypatch.setattr(make_video.build_video, "main", lambda eid: 3)
    assert make_video.main(9) == 3


def test_a_very_long_failure_reason_is_truncated_so_the_alert_fits_discord(monkeypatch, ladder):
    ep, events = ladder
    monkeypatch.setattr(make_video.render_video, "main", _raises(RuntimeError("x" * 5000)))
    make_video.main(9)
    alert, = _alerts(events)
    assert "降級" in alert and "原因" in alert and alert.endswith("…") and len(alert) < 700


@pytest.mark.parametrize("extra, truncated", [(0, False), (1, True), (2, True), (4000, True)])
def test_the_failure_reason_in_the_alert_is_cut_at_exactly_five_hundred_characters(
        monkeypatch, ladder, extra, truncated):
    ep, events = ladder
    assert make_video.ALERT_REASON_CHARS == 500
    prefix = "RuntimeError: "
    full = prefix + "x" * (500 - len(prefix) + extra)
    monkeypatch.setattr(make_video.render_video, "main", _raises(RuntimeError(full[len(prefix):])))
    make_video.main(9)
    alert, = _alerts(events)
    reason = alert.split("原因: ", 1)[1]
    assert len(reason) == 500
    assert reason == (full[:499] + "…" if truncated else full)


def test_an_exception_without_a_message_still_gives_a_reason(monkeypatch, ladder):
    ep, events = ladder
    monkeypatch.setattr(make_video.render_video, "main", _raises(KeyError()))
    make_video.main(9)
    assert "原因: KeyError" in _alerts(events)[0]


def test_keyboard_interrupt_is_not_swallowed_by_the_ladder(monkeypatch, ladder):
    ep, events = ladder
    monkeypatch.setattr(make_video.render_video, "main", _raises(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        make_video.main(9)
    assert events == []


def test_a_successful_render_checks_the_fallback_ratio(monkeypatch, ladder):
    ep, events = ladder

    def render(eid):
        (ep / "final.mp4").write_bytes(b"mp4")
        _put(ep, "video_report.json", {"fallback_beats": ["b1", "b2"], "beats": 5})
        return 0

    monkeypatch.setattr(make_video.render_video, "main", render)
    assert make_video.main(9) == 0
    alert, = _alerts(events)
    assert "2/5" in alert and "降級" not in alert and _builds(events) == []


def test_the_shot_renderer_is_not_degraded_by_a_broken_report(monkeypatch, ladder):
    ep, events = ladder

    def render(eid):
        (ep / "final.mp4").write_bytes(b"mp4")
        _put(ep, "video_report.json", "{ truncated")
        return 0

    monkeypatch.setattr(make_video.render_video, "main", render)
    assert make_video.main(9) == 0 and events == []


def test_the_real_renderer_failing_on_missing_inputs_degrades(monkeypatch, ladder):
    ep, events = ladder   # real render_video.main: audio_full.mp3 is missing -> RenderError
    assert make_video.main(9) == 0
    alert, = _alerts(events)
    assert "降級" in alert and "RenderError" in alert
    assert (ep / "img_01.png").read_bytes() == b"one" and len(_builds(events)) == 1


def test_missing_shots_json_is_an_error_not_a_degrade(monkeypatch, ladder):
    ep, events = ladder
    (ep / "shots.json").unlink()
    monkeypatch.setattr(make_video.render_video, "main", lambda eid: (_ for _ in ()).throw(AssertionError("no")))
    with pytest.raises(RuntimeError, match="shots.json.*plan_shots.py"):
        make_video.main(9)
    assert events == []


@pytest.mark.parametrize("mode", [None, "", "slideshow", "SHOTS", "bogus"])
def test_every_mode_but_shots_runs_the_old_slideshow_and_returns_its_exit_code(monkeypatch, ladder, mode):
    ep, events = ladder
    if mode is None:
        monkeypatch.delenv("VIDEO_MODE")
    else:
        monkeypatch.setenv("VIDEO_MODE", mode)
    monkeypatch.setattr(make_video.render_video, "main", lambda eid: (_ for _ in ()).throw(AssertionError("no")))
    monkeypatch.setattr(make_video.build_video, "main", lambda eid: 4)
    assert make_video.main(9) == 4 and events == []


def test_slideshow_mode_does_not_touch_the_episode_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "slideshow")
    monkeypatch.setattr(make_video.build_video, "main", lambda eid: 0)
    assert make_video.main(12) == 0 and list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- make_video.check_fallback_ratio

def _ratio_alerts(monkeypatch, tmp_path, report, episode_id=3):
    alerts = []
    monkeypatch.setattr(make_video, "notify_discord", alerts.append)
    if report is not None:
        _put(tmp_path, "video_report.json", report)
    make_video.check_fallback_ratio(episode_id, tmp_path)
    return alerts


def test_the_fallback_threshold_is_strictly_more_than_thirty_percent(monkeypatch, tmp_path):
    assert make_video.FALLBACK_ALERT_RATIO == 0.3
    assert _ratio_alerts(monkeypatch, tmp_path, {"fallback_beats": ["a", "b", "c"], "beats": 10}) == []
    alerts = _ratio_alerts(monkeypatch, tmp_path, {"fallback_beats": ["x"] * 31, "beats": 100})
    assert len(alerts) == 1


def test_the_fallback_alert_wording(monkeypatch, tmp_path):
    alert, = _ratio_alerts(monkeypatch, tmp_path, {"fallback_beats": ["b1", "b2"], "beats": 5}, episode_id=3)
    assert alert == "Episode 3: 2/5 個鏡頭使用備援 (40%),請檢查圖片產生與渲染日誌。"


def test_no_video_report_means_no_check(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        assert _ratio_alerts(monkeypatch, tmp_path, None) == []
    assert caplog.records == []


DEEP_JSON = "[" * 100_000   # json.load raises RecursionError, which is not a ValueError


def test_a_deeply_nested_video_report_is_ignored_like_any_other_broken_report(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert _ratio_alerts(monkeypatch, tmp_path, DEEP_JSON) == []
    assert any("video_report.json" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("name", ["shots.json", "timings.json"])
def test_a_deeply_nested_plan_file_is_an_unusable_plan_not_a_crash(tmp_path, placeholder, name):
    _episode(tmp_path)
    _put(tmp_path, name, DEEP_JSON)
    _put(tmp_path, "script.json", {"sections": [{"section_id": 1}, {"section_id": 2}]})
    assert make_video.make_slideshow_inputs(tmp_path) == ["img_01.png", "img_02.png"]
    assert placeholder == ["img_01.png", "img_02.png"]


def test_quality_problems_of_a_successful_render_neither_alert_nor_degrade(monkeypatch, ladder):
    ep, events = ladder

    def render(eid):
        (ep / "final.mp4").write_bytes(b"mp4")
        _put(ep, "video_report.json", {"fallback_beats": [], "beats": 5, "duration_ok": False, "frames_ok": False,
                                       "contact_sheet": False, "quality": {"black_segments": 4, "freeze_segments": 2}})
        return 0

    monkeypatch.setattr(make_video.render_video, "main", render)
    assert make_video.main(9) == 0
    assert events == [] and (ep / "final.mp4").read_bytes() == b"mp4"


def test_zero_beats_do_not_divide_by_zero(monkeypatch, tmp_path):
    assert _ratio_alerts(monkeypatch, tmp_path, {"fallback_beats": [], "beats": 0}) == []
    alert, = _ratio_alerts(monkeypatch, tmp_path, {"fallback_beats": ["b1"], "beats": 0})
    assert "1/0" in alert and "100%" in alert


@pytest.mark.parametrize("report", [
    "", "not json", "[]", "null", '"text"', "{}", '{"beats": 5}', '{"fallback_beats": ["a"]}',
    '{"fallback_beats": null, "beats": 5}', '{"fallback_beats": "ab", "beats": 5}',
    '{"fallback_beats": {"a": 1}, "beats": 5}', '{"fallback_beats": ["a", "b"], "beats": "5"}',
    '{"fallback_beats": ["a", "b"], "beats": null}', '{"fallback_beats": ["a", "b"], "beats": true}',
    '{"fallback_beats": ["a", "b"], "beats": -1}', '{"fallback_beats": ["a", "b"], "beats": NaN}',
    '{"fallback_beats": ["a", "b"], "beats": 2.5}', '{"fallback_beats": ["a", "b"], "beats": [5]}'])
def test_an_odd_video_report_never_raises_and_never_alerts(monkeypatch, tmp_path, report):
    assert _ratio_alerts(monkeypatch, tmp_path, report) == []


# Beats that came from the fallback plan (LLM planning failed) are reported separately from fallback render beats.
def _plan_sections(*sections):
    return {"sections": [{"section": k + 1, "beats": beats, "attempts": 4 if fallback else 1, "fallback": fallback,
                          "errors": ["bad"] if fallback else []} for k, (beats, fallback) in enumerate(sections)]}


def _check_alerts(monkeypatch, tmp_path, video=None, plan=None, episode_id=3):
    alerts = []
    monkeypatch.setattr(make_video, "notify_discord", alerts.append)
    if video is not None:
        _put(tmp_path, "video_report.json", video)
    if plan is not None:
        _put(tmp_path, "plan_report.json", plan)
    make_video.check_fallback_ratio(episode_id, tmp_path)
    return alerts


CLEAN_VIDEO = {"fallback_beats": [], "beats": 30}


def test_the_plan_fallback_alert_wording(monkeypatch, tmp_path):
    alert, = _check_alerts(monkeypatch, tmp_path, video=CLEAN_VIDEO,
                           plan=_plan_sections((10, False), (10, True), (10, False)))
    assert alert == "Episode 3: 10/30 個鏡頭來自規劃保底 (33%),LLM 分鏡規劃多次失敗,請檢查 plan_report.json。"


@pytest.mark.parametrize("fallback_beats, total, alerts", [(3, 10, 0), (30, 100, 0), (31, 100, 1), (4, 10, 1), (10, 10, 1)])
def test_the_plan_fallback_threshold_is_strictly_more_than_thirty_percent(
        monkeypatch, tmp_path, fallback_beats, total, alerts):
    plan = _plan_sections((fallback_beats, True), (total - fallback_beats, False))
    assert len(_check_alerts(monkeypatch, tmp_path, video={"fallback_beats": [], "beats": total}, plan=plan)) == alerts


def test_the_plan_alert_is_relative_to_the_beats_of_the_video_report(monkeypatch, tmp_path):
    plan = _plan_sections((5, True), (5, False))
    assert _check_alerts(monkeypatch, tmp_path, video={"fallback_beats": [], "beats": 50}, plan=plan) == []
    alert, = _check_alerts(monkeypatch, tmp_path, video={"fallback_beats": [], "beats": 12}, plan=plan)
    assert "5/12" in alert and "(42%)" in alert


def test_without_a_video_report_the_plan_alert_still_works_against_the_plans_own_total(monkeypatch, tmp_path):
    alert, = _check_alerts(monkeypatch, tmp_path, plan=_plan_sections((5, True), (5, False)))
    assert alert == "Episode 3: 5/10 個鏡頭來自規劃保底 (50%),LLM 分鏡規劃多次失敗,請檢查 plan_report.json。"


@pytest.mark.parametrize("video", [
    "", "not json", "[]", "{}", '{"beats": "50"}', '{"beats": null}', '{"beats": 0}', '{"beats": -1}', '{"beats": true}',
    '{"beats": 2.5}', '{"beats": [50]}', '{"fallback_beats": [], "beats": 0}'])
def test_an_unusable_video_report_means_the_plan_alert_uses_the_plans_own_total(monkeypatch, tmp_path, video):
    alert, = _check_alerts(monkeypatch, tmp_path, video=video, plan=_plan_sections((5, True), (5, False)))
    assert "5/10" in alert and "規劃保底" in alert


def test_a_video_report_with_valid_beats_but_no_fallback_list_still_gives_the_plan_its_denominator(monkeypatch, tmp_path):
    alert, = _check_alerts(monkeypatch, tmp_path, video={"fallback_beats": None, "beats": 12},
                           plan=_plan_sections((5, True), (5, False)))
    assert "5/12" in alert and "規劃保底" in alert


def test_both_alerts_fire_independently_and_never_add_up(monkeypatch, tmp_path):
    video = {"fallback_beats": ["a", "b", "c", "d"], "beats": 10}
    first, second = _check_alerts(monkeypatch, tmp_path, video=video, plan=_plan_sections((4, True), (6, False)))
    assert first == "Episode 3: 4/10 個鏡頭使用備援 (40%),請檢查圖片產生與渲染日誌。"
    assert second == "Episode 3: 4/10 個鏡頭來自規劃保底 (40%),LLM 分鏡規劃多次失敗,請檢查 plan_report.json。"


def test_only_the_video_alert_fires_when_the_plan_is_mostly_fine(monkeypatch, tmp_path):
    alert, = _check_alerts(monkeypatch, tmp_path, video={"fallback_beats": ["a", "b", "c", "d"], "beats": 10},
                           plan=_plan_sections((1, True), (9, False)))
    assert "使用備援" in alert and "規劃保底" not in alert


def test_only_the_plan_alert_fires_when_the_render_is_mostly_fine(monkeypatch, tmp_path):
    alert, = _check_alerts(monkeypatch, tmp_path, video={"fallback_beats": ["a"], "beats": 10},
                           plan=_plan_sections((4, True), (6, False)))
    assert "規劃保底" in alert and "使用備援" not in alert


def test_two_modest_ratios_are_not_added_together(monkeypatch, tmp_path):
    video = {"fallback_beats": ["a", "b"], "beats": 10}   # 20% render fallbacks + 20% plan fallbacks = 40% if summed
    assert _check_alerts(monkeypatch, tmp_path, video=video, plan=_plan_sections((2, True), (8, False))) == []


def test_a_clean_plan_sends_nothing(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        assert _check_alerts(monkeypatch, tmp_path, video=CLEAN_VIDEO, plan=_plan_sections((15, False), (15, False))) == []
    assert caplog.records == []


@pytest.mark.parametrize("fallback", [False, True])
def test_a_plan_without_any_beats_is_quiet(monkeypatch, tmp_path, caplog, fallback):
    with caplog.at_level(logging.INFO):
        assert _check_alerts(monkeypatch, tmp_path, plan=_plan_sections((0, fallback))) == []
    assert caplog.records == []


def test_without_any_report_there_is_nothing_to_check_and_nothing_to_log(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        assert _check_alerts(monkeypatch, tmp_path) == []
    assert caplog.records == []


def test_a_missing_plan_report_is_normal_and_silent(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        assert _check_alerts(monkeypatch, tmp_path, video=CLEAN_VIDEO) == []
    assert caplog.records == []


def test_odd_entries_of_the_plan_are_ignored_not_counted(monkeypatch, tmp_path):
    plan = {"sections": ["junk", None, [], {"beats": True, "fallback": True}, {"beats": "9", "fallback": True},
                         {"beats": 4, "fallback": True}, {"beats": 6, "fallback": False}, {"beats": -3, "fallback": True},
                         {"fallback": True}]}
    alert, = _check_alerts(monkeypatch, tmp_path, video={"fallback_beats": [], "beats": 10}, plan=plan)
    assert "4/10" in alert
    alert, = _check_alerts(monkeypatch, tmp_path, plan=plan)   # no video report: the plan's own total is 4 + 6
    assert "4/10" in alert


@pytest.mark.parametrize("plan", [
    "", "not json", "[]", "null", '"x"', "{}", '{"sections": null}', '{"sections": "ab"}', '{"sections": {"a": 1}}',
    '{"sections": []}', '{"sections": ["junk", 5, null, []]}',
    '{"sections": [{"beats": 9, "fallback": "true"}]}', '{"sections": [{"beats": 9, "fallback": 1}]}',
    '{"sections": [{"beats": 9}]}', '{"sections": [{"beats": 9, "fallback": null}]}',
    '{"sections": [{"beats": "9", "fallback": true}]}', '{"sections": [{"beats": null, "fallback": true}]}',
    '{"sections": [{"beats": true, "fallback": true}]}', '{"sections": [{"beats": -9, "fallback": true}]}',
    '{"sections": [{"beats": 9.5, "fallback": true}]}', '{"sections": [{"beats": NaN, "fallback": true}]}',
    '{"sections": [{"beats": [9], "fallback": true}]}', '{"sections": [{"fallback": true}]}'])
def test_an_odd_plan_report_never_raises_and_never_alerts(monkeypatch, tmp_path, plan):
    assert _check_alerts(monkeypatch, tmp_path, video=CLEAN_VIDEO, plan=plan) == []
    (tmp_path / "video_report.json").unlink()
    assert _check_alerts(monkeypatch, tmp_path, plan=plan) == []


@pytest.mark.parametrize("plan", ["", "not json", "[]", "{}", '{"sections": null}', '{"sections": []}',
                                  '{"sections": ["junk"]}', '{"sections": [{"beats": "9", "fallback": true}]}', DEEP_JSON],
                         ids=["empty", "not-json", "list", "no-sections", "null-sections", "empty-sections", "junk-only",
                              "string-beats", "deeply-nested"])
def test_an_unusable_plan_report_is_logged_and_skipped(monkeypatch, tmp_path, caplog, plan):
    with caplog.at_level(logging.WARNING):
        assert _check_alerts(monkeypatch, tmp_path, video=CLEAN_VIDEO, plan=plan) == []
    assert any("plan_report.json" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_absurd_numbers_in_the_plan_never_raise(monkeypatch, tmp_path, caplog):
    plan = f'{{"sections": [{{"beats": {HUGE}, "fallback": true}}]}}'   # int / int overflows the float quotient
    with caplog.at_level(logging.WARNING):
        assert _check_alerts(monkeypatch, tmp_path, video={"fallback_beats": [], "beats": 1}, plan=plan) == []
    assert any("plan_report.json" in r.getMessage() for r in caplog.records)


def test_a_broken_plan_report_does_not_hide_the_video_alert(monkeypatch, tmp_path):
    alert, = _check_alerts(monkeypatch, tmp_path, video={"fallback_beats": ["a", "b"], "beats": 5}, plan="{ truncated")
    assert "2/5" in alert and "使用備援" in alert


def test_a_broken_video_report_does_not_hide_the_plan_alert(monkeypatch, tmp_path):
    alert, = _check_alerts(monkeypatch, tmp_path, video="{ truncated", plan=_plan_sections((5, True), (5, False)))
    assert "5/10" in alert and "規劃保底" in alert


def test_a_successful_render_also_checks_the_plan_fallbacks(monkeypatch, ladder):
    ep, events = ladder
    _put(ep, "plan_report.json", _plan_sections((4, True), (6, False)))   # plan_shots wrote it long before the render

    def render(eid):
        (ep / "final.mp4").write_bytes(b"mp4")
        _put(ep, "video_report.json", {"fallback_beats": [], "beats": 10})
        return 0

    monkeypatch.setattr(make_video.render_video, "main", render)
    assert make_video.main(9) == 0
    alert, = _alerts(events)
    assert "4/10" in alert and "規劃保底" in alert and "降級" not in alert and _builds(events) == []
    assert (ep / "plan_report.json").exists() and (ep / "final.mp4").read_bytes() == b"mp4"


def test_a_degraded_render_sends_only_the_degrade_alert(monkeypatch, ladder):
    ep, events = ladder
    _put(ep, "plan_report.json", _plan_sections((10, True)))
    monkeypatch.setattr(make_video.render_video, "main", _raises(RuntimeError("boom")))
    assert make_video.main(9) == 0
    alert, = _alerts(events)
    assert "降級" in alert and "規劃保底" not in alert
    assert (ep / "plan_report.json").exists()


# ---------------------------------------------------------------- notify

def _reports(tmp_path, image=None, video=None):
    if image is not None:
        _put(tmp_path, "image_report.json", image)
    if video is not None:
        _put(tmp_path, "video_report.json", video)
    return notify.summarize_run(tmp_path)


@pytest.fixture
def default_cost(monkeypatch):
    monkeypatch.delenv("IMAGE_UNIT_COST_USD", raising=False)


@pytest.fixture(autouse=True)
def podcast_flag_unset(monkeypatch):
    monkeypatch.delenv("PUBLISH_PODCAST", raising=False)


def test_summary_text_is_exactly_what_the_message_promises(tmp_path, default_cost):
    summary = _reports(tmp_path, {"generated": 20, "fallback": 2, "skipped": 7},
                       {"render_sec": 412.4, "fallback_beats": ["b1"], "beats": 80})
    assert summary == "圖片 22 張 (估算 $1.32,備援 2)；渲染 412s,備援鏡頭 1/80"


def test_skipped_images_cost_nothing_and_are_not_counted(tmp_path, default_cost):
    assert _reports(tmp_path, {"generated": 3, "fallback": 0, "skipped": 5}) == "圖片 3 張 (估算 $0.18,備援 0)"


def test_each_report_is_summarised_on_its_own(tmp_path, default_cost):
    assert _reports(tmp_path, image={"generated": 1, "fallback": 0}) == "圖片 1 張 (估算 $0.06,備援 0)"
    (tmp_path / "image_report.json").unlink()
    assert _reports(tmp_path, video={"render_sec": 9, "fallback_beats": [], "beats": 4}) == "渲染 9s,備援鏡頭 0/4"


@pytest.mark.parametrize("value, expected", [
    ("0.1", "$2.20"), (" 0.5 ", "$11.00"), ("0", "$0.00"), ("abc", "$1.32"), ("", "$1.32"), ("   ", "$1.32"),
    ("-1", "$1.32"), ("nan", "$1.32"), ("inf", "$1.32"), ("-inf", "$1.32")])
def test_image_unit_cost_comes_from_the_environment_with_a_safe_default(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv("IMAGE_UNIT_COST_USD", value)
    assert expected in _reports(tmp_path, {"generated": 20, "fallback": 2})


@pytest.mark.parametrize("value, warns", [("", False), ("   ", False), ("0.1", False), ("abc", True), ("-1", True),
                                           ("nan", True)])
def test_only_an_invalid_image_unit_cost_is_worth_a_warning(monkeypatch, tmp_path, caplog, value, warns):
    monkeypatch.setenv("IMAGE_UNIT_COST_USD", value)
    with caplog.at_level(logging.WARNING):
        _reports(tmp_path, {"generated": 1, "fallback": 0})
    assert any("IMAGE_UNIT_COST_USD" in r.getMessage() for r in caplog.records) is warns


def test_missing_reports_are_normal_and_silent_but_a_broken_one_is_logged(tmp_path, default_cost, caplog):
    with caplog.at_level(logging.WARNING):
        assert notify.summarize_run(tmp_path) == ""
    assert caplog.records == []
    with caplog.at_level(logging.WARNING):
        _reports(tmp_path, image="{ truncated")
    assert any("image_report.json" in r.getMessage() for r in caplog.records)


def test_the_default_image_cost_is_six_cents(default_cost):
    assert notify.DEFAULT_IMAGE_COST_USD == 0.06


@pytest.mark.parametrize("image", [
    "", "not json", "[]", "null", '"x"', "{}", '{"generated": 3}', '{"fallback": 3}', '{"generated": "3", "fallback": 1}',
    '{"generated": null, "fallback": null}', '{"generated": 3, "fallback": true}', '{"generated": -1, "fallback": 1}',
    '{"generated": 2.5, "fallback": 1}', '{"generated": [3], "fallback": 1}', '{"generated": NaN, "fallback": 1}'])
def test_an_odd_image_report_is_skipped_without_raising(tmp_path, default_cost, image):
    assert _reports(tmp_path, image=image) == ""
    assert _reports(tmp_path, image=image, video={"render_sec": 5, "fallback_beats": [], "beats": 2}) == \
        "渲染 5s,備援鏡頭 0/2"


@pytest.mark.parametrize("video, expected", [
    ("", ""), ("not json", ""), ("[]", ""), ("null", ""), ("{}", ""),
    ('{"render_sec": 412}', "渲染 412s"), ('{"render_sec": "412"}', ""), ('{"render_sec": null}', ""),
    ('{"render_sec": true}', ""), ('{"render_sec": -1}', ""), ('{"render_sec": NaN}', ""), ('{"render_sec": Infinity}', ""),
    ('{"fallback_beats": ["a"], "beats": 5}', "備援鏡頭 1/5"), ('{"fallback_beats": ["a"]}', ""),
    ('{"beats": 5}', ""), ('{"fallback_beats": "ab", "beats": 5}', ""), ('{"fallback_beats": ["a"], "beats": "5"}', ""),
    ('{"fallback_beats": ["a"], "beats": null}', ""), ('{"fallback_beats": ["a"], "beats": true}', ""),
    ('{"fallback_beats": null, "beats": 5, "render_sec": 7.6}', "渲染 8s")])
def test_a_partial_or_odd_video_report_gives_what_it_can_without_raising(tmp_path, video, expected):
    assert _reports(tmp_path, video=video) == expected


def test_a_report_that_cannot_be_opened_is_skipped_and_logged(tmp_path, default_cost, caplog):
    (tmp_path / "image_report.json").mkdir()   # open() raises PermissionError / IsADirectoryError, both OSError
    with caplog.at_level(logging.WARNING):
        assert _reports(tmp_path, video={"render_sec": 9, "fallback_beats": [], "beats": 4}) == "渲染 9s,備援鏡頭 0/4"
    assert any("image_report.json 無法讀取" in r.getMessage() for r in caplog.records)   # the reader, not the wrapper


def test_main_still_notifies_when_the_files_cannot_be_opened(episode):
    ep, sent = episode
    (ep / "script.json").mkdir()
    (ep / "youtube_info.json").mkdir()
    assert notify.main(21) == 0
    assert sent == [" **新集數已發布**\n (未知標題)\n YouTube: (未上傳)\n Podcast: 已更新 RSS feed"]


def test_a_deeply_nested_report_is_skipped_and_logged(tmp_path, default_cost, caplog):
    with caplog.at_level(logging.WARNING):
        assert _reports(tmp_path, image=DEEP_JSON, video=DEEP_JSON) == ""
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "image_report.json" in logged and "video_report.json" in logged


HUGE = "9" * 400   # float(int) overflows


def test_absurdly_large_numbers_cost_only_their_own_part_of_the_summary(tmp_path, default_cost, caplog):
    _put(tmp_path, "image_report.json", f'{{"generated": {HUGE}, "fallback": 1}}')
    _put(tmp_path, "video_report.json", '{"render_sec": 9, "fallback_beats": [], "beats": 4}')
    with caplog.at_level(logging.WARNING):
        assert notify.summarize_run(tmp_path) == "渲染 9s,備援鏡頭 0/4"
    assert any("image_report.json" in r.getMessage() for r in caplog.records)
    caplog.clear()
    _put(tmp_path, "image_report.json", '{"generated": 1, "fallback": 0}')
    _put(tmp_path, "video_report.json", f'{{"render_sec": {HUGE}, "fallback_beats": [], "beats": 4}}')
    with caplog.at_level(logging.WARNING):
        assert notify.summarize_run(tmp_path) == "圖片 1 張 (估算 $0.06,備援 0)"
    assert any("video_report.json" in r.getMessage() for r in caplog.records)
    _put(tmp_path, "image_report.json", f'{{"generated": {HUGE}, "fallback": 0}}')
    assert notify.summarize_run(tmp_path) == ""


@pytest.mark.parametrize("broken", ["_image_part", "_video_part"])
def test_summarize_run_never_raises_whatever_goes_wrong_in_one_part(monkeypatch, tmp_path, default_cost, caplog, broken):
    def boom(report):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(notify, broken, boom)
    expected = {"_image_part": "渲染 9s,備援鏡頭 0/4", "_video_part": "圖片 1 張 (估算 $0.06,備援 0)"}[broken]
    with caplog.at_level(logging.WARNING):
        summary = _reports(tmp_path, {"generated": 1, "fallback": 0}, {"render_sec": 9, "fallback_beats": [], "beats": 4})
    assert summary == expected
    name = "image_report.json" if broken == "_image_part" else "video_report.json"
    assert any(name in r.getMessage() and "unexpected" in r.getMessage() for r in caplog.records)


def test_main_still_notifies_when_the_reports_are_absurd(episode):
    ep, sent = episode
    _put(ep, "script.json", DEEP_JSON)
    _put(ep, "youtube_info.json", DEEP_JSON)
    _put(ep, "image_report.json", f'{{"generated": {HUGE}, "fallback": 1}}')
    _put(ep, "video_report.json", DEEP_JSON)
    assert notify.main(21) == 0
    assert sent == [" **新集數已發布**\n (未知標題)\n YouTube: (未上傳)\n Podcast: 已更新 RSS feed"]


# A render that succeeded but looks wrong is flagged in the summary text only; it is never degraded or alerted.
RENDERED = {"render_sec": 412, "fallback_beats": [], "beats": 80}
CLEAN_SUMMARY = "渲染 412s,備援鏡頭 0/80"


def test_a_clean_report_with_every_check_passing_gives_exactly_the_old_text(tmp_path):
    video = {**RENDERED, "duration_ok": True, "frames_ok": True, "contact_sheet": True,
             "quality": {"black_segments": 0, "freeze_segments": 0}}
    assert _reports(tmp_path, video=video) == CLEAN_SUMMARY


@pytest.mark.parametrize("extra, flag", [
    ({"duration_ok": False}, "長度異常"),
    ({"frames_ok": False}, "影格數不符"),
    ({"contact_sheet": False}, "縮圖總表失敗"),
    ({"quality": {"black_segments": 2}}, "黑畫面 2 段"),
    ({"quality": {"freeze_segments": 3}}, "靜止 3 段"),
    ({"quality": {"black_segments": 2, "freeze_segments": 3}}, "黑畫面 2 段,靜止 3 段"),
    ({"quality": {"black_segments": 0, "freeze_segments": 1}}, "靜止 1 段"),
    ({"quality": {"black_segments": 1, "freeze_segments": 0}}, "黑畫面 1 段")])
def test_each_quality_problem_is_named_inside_the_video_part(tmp_path, extra, flag):
    assert _reports(tmp_path, video={**RENDERED, **extra}) == f"{CLEAN_SUMMARY},{flag}"


def test_every_quality_problem_at_once_comes_in_a_fixed_order(tmp_path):
    video = {**RENDERED, "fallback_beats": ["b1"], "duration_ok": False, "frames_ok": False, "contact_sheet": False,
             "quality": {"black_segments": 2, "freeze_segments": 3}}
    assert _reports(tmp_path, video=video) == "渲染 412s,備援鏡頭 1/80,長度異常,影格數不符,縮圖總表失敗,黑畫面 2 段,靜止 3 段"


def test_quality_problems_stay_in_the_video_part_next_to_the_image_part(tmp_path, default_cost):
    summary = _reports(tmp_path, {"generated": 1, "fallback": 0}, {**RENDERED, "duration_ok": False})
    assert summary == f"圖片 1 張 (估算 $0.06,備援 0)；{CLEAN_SUMMARY},長度異常"


def test_quality_problems_are_reported_even_when_the_other_numbers_are_missing(tmp_path):
    assert _reports(tmp_path, video={"duration_ok": False, "quality": {"black_segments": 1}}) == "長度異常,黑畫面 1 段"


@pytest.mark.parametrize("key", ["duration_ok", "frames_ok", "contact_sheet"])
@pytest.mark.parametrize("value", [None, True, 0, 1, "", "false", "no", [], {}, [False], 0.0])
def test_a_check_flag_is_a_problem_only_when_it_is_exactly_false(tmp_path, key, value):
    assert _reports(tmp_path, video={**RENDERED, key: value}) == CLEAN_SUMMARY


def test_a_missing_check_flag_is_not_a_problem(tmp_path):
    assert _reports(tmp_path, video=dict(RENDERED)) == CLEAN_SUMMARY


@pytest.mark.parametrize("quality", [
    None, [], "x", 5, {}, {"black_segments": None}, {"black_segments": "2"}, {"black_segments": True},
    {"black_segments": 0}, {"black_segments": -1}, {"black_segments": 2.5}, {"black_segments": float("nan")},
    {"black_segments": [2]}, {"freeze_segments": None}, {"freeze_segments": "3"}, {"freeze_segments": False},
    {"freeze_segments": 0}, {"freeze_segments": -2}, {"freeze_segments": 1.5}, {"freeze_segments": {"n": 1}},
    {"black_segments": None, "freeze_segments": None}])
def test_odd_or_unknown_quality_counts_are_ignored(tmp_path, quality):
    assert _reports(tmp_path, video={**RENDERED, "quality": quality}) == CLEAN_SUMMARY


# Plan fallbacks, failed grounding and an image stage that gave up are worth a line even though the render itself passed.
def _plan_report(*sections):
    return {"sections": [{"section": k + 1, "beats": beats, "attempts": 4 if fallback else 1, "fallback": fallback,
                          "errors": ["bad"] if fallback else []} for k, (beats, fallback) in enumerate(sections)],
            "budget": {"limit": 24, "needed": 20, "used": 20, "exceeded": False}}


def test_plan_fallback_sections_are_counted_out_of_all_sections(tmp_path):
    _put(tmp_path, "plan_report.json", _plan_report((9, True), (8, False), (7, True)))
    assert notify.summarize_run(tmp_path) == "規劃保底 2/3 段"


def test_a_plan_without_fallback_sections_adds_nothing(tmp_path):
    _put(tmp_path, "plan_report.json", _plan_report((9, False), (8, False)))
    assert notify.summarize_run(tmp_path) == ""
    _put(tmp_path, "plan_report.json", {"sections": []})
    assert notify.summarize_run(tmp_path) == ""


def test_every_section_falling_back_reads_n_of_n(tmp_path):
    _put(tmp_path, "plan_report.json", _plan_report((4, True)))
    assert notify.summarize_run(tmp_path) == "規劃保底 1/1 段"


@pytest.mark.parametrize("plan, expected", [
    ("", ""), ("not json", ""), ("[]", ""), ("null", ""), ("{}", ""), ('"x"', ""),
    ('{"sections": null}', ""), ('{"sections": "ab"}', ""), ('{"sections": {"a": {"fallback": true}}}', ""),
    ('{"sections": 5}', ""), ('{"sections": [[{"fallback": true}]]}', ""),
    ('{"sections": ["junk", 5, null, {"fallback": true}]}', "規劃保底 1/1 段"),
    ('{"sections": [{"fallback": true}, {"fallback": "true"}, {"fallback": 1}, {"fallback": null}, {}]}',
     "規劃保底 1/5 段"),
    ('{"sections": [{"fallback": false}, {"fallback": "yes"}, {"fallback": [true]}]}', "")])
def test_an_odd_plan_report_gives_what_it_can_without_raising(tmp_path, caplog, plan, expected):
    _put(tmp_path, "plan_report.json", plan)
    with caplog.at_level(logging.WARNING):
        assert notify.summarize_run(tmp_path) == expected
    assert not [r for r in caplog.records if "摘要失敗" in r.getMessage()]   # ignored by the part, not by the safety net


def test_failed_grounding_is_counted_in_shots(tmp_path):
    _put(tmp_path, "ground_report.json", {"grounded": 40, "missing": ["b3", "b9", "b12"]})
    assert notify.summarize_run(tmp_path) == "聚焦失敗 3 鏡頭"


@pytest.mark.parametrize("ground, expected", [
    ({"grounded": 5, "missing": []}, ""), ({"grounded": 5}, ""), ({"grounded": 0, "missing": ["b1"]}, "聚焦失敗 1 鏡頭"),
    ("", ""), ("not json", ""), ("[]", ""), ("null", ""), ("{}", ""), ('{"missing": null}', ""),
    ('{"missing": "b1"}', ""), ('{"missing": 3}', ""), ('{"missing": {"b1": 1}}', ""), ('{"missing": true}', "")])
def test_an_odd_ground_report_gives_what_it_can_without_raising(tmp_path, caplog, ground, expected):
    _put(tmp_path, "ground_report.json", ground)
    with caplog.at_level(logging.WARNING):
        assert notify.summarize_run(tmp_path) == expected
    assert not [r for r in caplog.records if "摘要失敗" in r.getMessage()]


@pytest.mark.parametrize("reason, text", [
    ("consecutive_failures", "連續失敗"), ("time_limit", "逾時"), ("quota_exhausted", "quota_exhausted"),
    (" time_limit ", "逾時"), ("連續", "連續")])
def test_an_image_stage_that_stopped_early_says_why(tmp_path, default_cost, reason, text):
    summary = _reports(tmp_path, {"generated": 5, "fallback": 3, "skipped": 0, "stopped_early": reason})
    assert summary == f"圖片 8 張 (估算 $0.48,備援 3)；產圖中止 ({text})"


@pytest.mark.parametrize("reason", [None, "", "   ", 5, True, [], ["time_limit"], {"a": 1}, 1.5])
def test_a_missing_or_odd_stopped_early_adds_nothing(tmp_path, default_cost, caplog, reason):
    with caplog.at_level(logging.WARNING):
        assert _reports(tmp_path, {"generated": 5, "fallback": 3, "stopped_early": reason}) ==             "圖片 8 張 (估算 $0.48,備援 3)"
    assert not [r for r in caplog.records if "摘要失敗" in r.getMessage()]


def test_the_early_stop_is_reported_even_when_the_image_counts_are_unusable(tmp_path, default_cost):
    assert _reports(tmp_path, {"stopped_early": "time_limit", "generated": "5"}) == "產圖中止 (逾時)"
    assert _reports(tmp_path, {"stopped_early": "consecutive_failures"}) == "產圖中止 (連續失敗)"


def test_all_parts_come_in_a_fixed_order_with_the_usual_separator(tmp_path, default_cost):
    _put(tmp_path, "image_report.json", {"generated": 20, "fallback": 2, "skipped": 0, "stopped_early": "time_limit"})
    _put(tmp_path, "plan_report.json", _plan_report((9, True), (8, False), (7, True)))
    _put(tmp_path, "ground_report.json", {"grounded": 40, "missing": ["b3", "b9", "b12"]})
    _put(tmp_path, "video_report.json", {**RENDERED, "fallback_beats": ["b1"], "duration_ok": False})
    assert notify.summarize_run(tmp_path) == (
        "圖片 22 張 (估算 $1.32,備援 2)；產圖中止 (逾時)；規劃保底 2/3 段；聚焦失敗 3 鏡頭；"
        "渲染 412s,備援鏡頭 1/80,長度異常")


def test_each_new_part_stands_alone_between_the_old_ones(tmp_path, default_cost):
    _put(tmp_path, "image_report.json", {"generated": 1, "fallback": 0})
    _put(tmp_path, "plan_report.json", _plan_report((3, True)))
    _put(tmp_path, "video_report.json", RENDERED)
    assert notify.summarize_run(tmp_path) == f"圖片 1 張 (估算 $0.06,備援 0)；規劃保底 1/1 段；{CLEAN_SUMMARY}"
    (tmp_path / "plan_report.json").unlink()
    _put(tmp_path, "ground_report.json", {"missing": ["b1"]})
    assert notify.summarize_run(tmp_path) == f"圖片 1 張 (估算 $0.06,備援 0)；聚焦失敗 1 鏡頭；{CLEAN_SUMMARY}"


def test_clean_plan_and_ground_reports_leave_the_old_summary_untouched(tmp_path, default_cost):
    _put(tmp_path, "plan_report.json", _plan_report((9, False), (8, False)))
    _put(tmp_path, "ground_report.json", {"grounded": 17, "missing": []})
    summary = _reports(tmp_path, {"generated": 20, "fallback": 2, "skipped": 7},
                       {"render_sec": 412.4, "fallback_beats": ["b1"], "beats": 80})
    assert summary == "圖片 22 張 (估算 $1.32,備援 2)；渲染 412s,備援鏡頭 1/80"


@pytest.mark.parametrize("name", ["plan_report.json", "ground_report.json"])
@pytest.mark.parametrize("raw", ["{ truncated", DEEP_JSON], ids=["truncated", "deeply-nested"])
def test_a_broken_or_deeply_nested_new_report_is_logged_and_skipped(tmp_path, caplog, name, raw):
    _put(tmp_path, name, raw)
    with caplog.at_level(logging.WARNING):
        assert notify.summarize_run(tmp_path) == ""
    assert any(name in r.getMessage() for r in caplog.records)


def test_a_new_report_that_cannot_be_opened_is_skipped_and_logged(tmp_path, caplog):
    (tmp_path / "plan_report.json").mkdir()
    (tmp_path / "ground_report.json").mkdir()
    with caplog.at_level(logging.WARNING):
        assert _reports(tmp_path, video=RENDERED) == CLEAN_SUMMARY
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "plan_report.json 無法讀取" in logged and "ground_report.json 無法讀取" in logged


@pytest.mark.parametrize("broken, survivors", [
    ("_image_part", "產圖中止 (逾時)；規劃保底 1/1 段；聚焦失敗 1 鏡頭；" + CLEAN_SUMMARY),
    ("_image_stop_part", "圖片 1 張 (估算 $0.06,備援 0)；規劃保底 1/1 段；聚焦失敗 1 鏡頭；" + CLEAN_SUMMARY),
    ("_plan_part", "圖片 1 張 (估算 $0.06,備援 0)；產圖中止 (逾時)；聚焦失敗 1 鏡頭；" + CLEAN_SUMMARY),
    ("_ground_part", "圖片 1 張 (估算 $0.06,備援 0)；產圖中止 (逾時)；規劃保底 1/1 段；" + CLEAN_SUMMARY),
    ("_video_part", "圖片 1 張 (估算 $0.06,備援 0)；產圖中止 (逾時)；規劃保底 1/1 段；聚焦失敗 1 鏡頭")])
def test_a_failing_part_costs_only_itself(monkeypatch, tmp_path, default_cost, caplog, broken, survivors):
    def boom(report):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(notify, broken, boom)
    _put(tmp_path, "image_report.json", {"generated": 1, "fallback": 0, "stopped_early": "time_limit"})
    _put(tmp_path, "plan_report.json", _plan_report((3, True)))
    _put(tmp_path, "ground_report.json", {"missing": ["b1"]})
    _put(tmp_path, "video_report.json", RENDERED)
    with caplog.at_level(logging.WARNING):
        assert notify.summarize_run(tmp_path) == survivors
    assert any("unexpected" in r.getMessage() for r in caplog.records)


def test_a_reader_that_fails_unexpectedly_costs_only_its_own_report(monkeypatch, tmp_path, caplog):
    real_read = notify._read_object

    def read(path):
        if path.name == "plan_report.json":
            raise RuntimeError("disk on fire")
        return real_read(path)

    monkeypatch.setattr(notify, "_read_object", read)
    _put(tmp_path, "plan_report.json", _plan_report((3, True)))
    _put(tmp_path, "ground_report.json", {"missing": ["b1"]})
    with caplog.at_level(logging.WARNING):
        assert notify.summarize_run(tmp_path) == "聚焦失敗 1 鏡頭"
    assert any("plan_report.json" in r.getMessage() and "disk on fire" in r.getMessage() for r in caplog.records)


def test_absurdly_large_numbers_in_the_new_reports_cost_nothing(tmp_path):
    _put(tmp_path, "plan_report.json", f'{{"sections": [{{"fallback": true, "beats": {HUGE}}}]}}')
    _put(tmp_path, "ground_report.json", f'{{"grounded": {HUGE}, "missing": ["b1"]}}')
    assert notify.summarize_run(tmp_path) == "規劃保底 1/1 段；聚焦失敗 1 鏡頭"


def test_main_puts_plan_and_grounding_problems_into_the_message(episode):
    ep, sent = episode
    _put(ep, "script.json", {"title": "秦始皇"})
    _put(ep, "plan_report.json", _plan_report((9, True), (8, False)))
    _put(ep, "ground_report.json", {"grounded": 3, "missing": ["b1", "b2"]})
    assert notify.main(21) == 0
    assert sent == [" **新集數已發布**\n 秦始皇\n YouTube: (未上傳)\n Podcast: 已更新 RSS feed\n"
                    " 製作統計: 規劃保底 1/2 段；聚焦失敗 2 鏡頭"]


def test_build_message_layout_is_exact():
    assert notify.build_message("標題", "https://youtu.be/x", "S") == (
        " **新集數已發布**\n 標題\n YouTube: https://youtu.be/x\n Podcast: 已更新 RSS feed\n 製作統計: S")
    assert notify.build_message("標題", "https://youtu.be/x", "") == (
        " **新集數已發布**\n 標題\n YouTube: https://youtu.be/x\n Podcast: 已更新 RSS feed")


TEST_RUN_HEADER = " **測試執行完成 (未發布 Podcast)**"


def test_build_message_says_the_podcast_was_not_published_on_a_test_run(monkeypatch):
    monkeypatch.setenv("PUBLISH_PODCAST", "false")
    assert notify.build_message("標題", "https://youtu.be/x", "S") == (
        f"{TEST_RUN_HEADER}\n 標題\n YouTube: https://youtu.be/x\n Podcast: 未發布 (測試執行)\n 製作統計: S")
    assert notify.build_message("標題", "https://youtu.be/x", "") == (
        f"{TEST_RUN_HEADER}\n 標題\n YouTube: https://youtu.be/x\n Podcast: 未發布 (測試執行)")


@pytest.mark.parametrize("value", ["false", "False", " FALSE ", "false\n"])
def test_the_header_of_a_test_run_never_claims_a_new_episode_was_published(monkeypatch, value):
    monkeypatch.setenv("PUBLISH_PODCAST", value)
    header = notify.build_message("T", "U", "S").splitlines()[0]
    assert header == TEST_RUN_HEADER and "新集數已發布" not in header


@pytest.mark.parametrize("value", [None, "", "true", "True", "1", "no", "falsey", "   "])
def test_the_published_header_is_byte_identical_for_anything_but_false(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PUBLISH_PODCAST", raising=False)
    else:
        monkeypatch.setenv("PUBLISH_PODCAST", value)
    assert notify.build_message("T", "U", "S").splitlines()[0] == " **新集數已發布**"
    assert "測試執行完成" not in notify.build_message("T", "U", "S")


def test_notify_has_no_placeholder_less_f_strings():
    # flake8 F541: an f-string without a placeholder is a typo waiting to happen
    import ast
    tree = ast.parse(Path(notify.__file__).read_text(encoding="utf-8"))
    format_specs = {id(n.format_spec) for n in ast.walk(tree) if isinstance(n, ast.FormattedValue) and n.format_spec}
    bare = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) and id(n) not in format_specs
            and not any(isinstance(v, ast.FormattedValue) for v in n.values)]
    assert bare == []


@pytest.mark.parametrize("value", ["false", "False", "FALSE", " false ", "false\n"])
def test_the_podcast_line_says_not_published_for_false_in_any_case(monkeypatch, value):
    monkeypatch.setenv("PUBLISH_PODCAST", value)
    assert notify.build_message("T", "U", "").splitlines()[-1] == " Podcast: 未發布 (測試執行)"


@pytest.mark.parametrize("value", ["", "true", "True", "1", "0", "no", "falsey", "garbage", "   "])
def test_the_podcast_line_stays_published_for_anything_but_false(monkeypatch, value):
    monkeypatch.setenv("PUBLISH_PODCAST", value)
    assert notify.build_message("T", "U", "").splitlines()[-1] == " Podcast: 已更新 RSS feed"


def test_main_reads_the_podcast_flag_and_changes_nothing_else(monkeypatch, tmp_path, default_cost):
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(notify, "notify_discord", sent.append)
    ep = common.get_episode_dir(21)
    _put(ep, "script.json", {"title": "秦始皇"})
    _put(ep, "youtube_info.json", {"url": "https://youtu.be/abc"})
    _put(ep, "image_report.json", {"generated": 1, "fallback": 0})
    monkeypatch.setenv("PUBLISH_PODCAST", "false")
    assert notify.main(21) == 0
    monkeypatch.delenv("PUBLISH_PODCAST")
    assert notify.main(21) == 0
    assert [m.replace(TEST_RUN_HEADER, " **新集數已發布**").replace("未發布 (測試執行)", "已更新 RSS feed")
            for m in sent[:1]] == sent[1:]
    assert sent[0] == (f"{TEST_RUN_HEADER}\n 秦始皇\n YouTube: https://youtu.be/abc\n Podcast: 未發布 (測試執行)\n"
                       " 製作統計: 圖片 1 張 (估算 $0.06,備援 0)")


@pytest.fixture
def episode(monkeypatch, tmp_path, default_cost):
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(notify, "notify_discord", sent.append)
    return common.get_episode_dir(21), sent


def test_main_sends_title_url_and_statistics(episode):
    ep, sent = episode
    _put(ep, "script.json", {"title": "秦始皇"})
    _put(ep, "youtube_info.json", {"url": "https://youtu.be/abc"})
    _put(ep, "image_report.json", {"generated": 1, "fallback": 0})
    assert notify.main(21) == 0
    assert sent == [" **新集數已發布**\n 秦始皇\n YouTube: https://youtu.be/abc\n Podcast: 已更新 RSS feed\n"
                    " 製作統計: 圖片 1 張 (估算 $0.06,備援 0)"]


def test_main_without_youtube_info_says_not_uploaded(episode):
    ep, sent = episode
    _put(ep, "script.json", {"title": "秦始皇"})
    assert notify.main(21) == 0
    assert "YouTube: (未上傳)" in sent[0] and "製作統計" not in sent[0]


@pytest.mark.parametrize("script", [None, "{ broken", "[]", "null", "{}", '{"title": null}', '{"title": ""}',
                                    '{"title": "  "}', '{"title": 5}'])
def test_main_degrades_to_an_unknown_title_instead_of_failing(episode, script):
    ep, sent = episode
    if script is not None:
        _put(ep, "script.json", script)
    assert notify.main(21) == 0
    assert len(sent) == 1 and " (未知標題)\n" in sent[0]


@pytest.mark.parametrize("info", ["{ broken", "[]", "null", "{}", '{"url": null}', '{"url": ""}', '{"url": 5}',
                                  '{"url": ["https://youtu.be/x"]}'])
def test_main_degrades_to_not_uploaded_for_an_odd_youtube_info(episode, info):
    ep, sent = episode
    _put(ep, "script.json", {"title": "T"})
    _put(ep, "youtube_info.json", info)
    assert notify.main(21) == 0
    assert "YouTube: (未上傳)" in sent[0]


def test_main_with_an_empty_episode_folder_still_notifies(episode):
    ep, sent = episode
    assert notify.main(21) == 0
    assert sent == [" **新集數已發布**\n (未知標題)\n YouTube: (未上傳)\n Podcast: 已更新 RSS feed"]


def test_main_strips_the_padding_around_the_title_and_url(episode):
    ep, sent = episode
    _put(ep, "script.json", {"title": "  標題 \n"})
    _put(ep, "youtube_info.json", {"url": " https://youtu.be/x\t"})
    assert notify.main(21) == 0
    assert sent == [" **新集數已發布**\n 標題\n YouTube: https://youtu.be/x\n Podcast: 已更新 RSS feed"]


# ---------------------------------------------------------------- run_episode_local

@pytest.fixture
def runner_env(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in ("TOPIC_ID", "VIDEO_MODE", "VISUAL_STYLE", *local.SHORT_MODE_ENV):
        os.environ.pop(key, None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)


@pytest.fixture
def runner(monkeypatch, runner_env):
    calls = []
    monkeypatch.setattr(local, "run_stage", lambda name, eid, limit: calls.append((name, eid, limit)))
    return calls


def test_short_mode_sets_exactly_the_documented_values():
    assert local.SHORT_MODE_ENV == {"SECTIONS_PER_EPISODE": "2", "TARGET_LENGTH_MIN": "2", "MIN_LENGTH_MIN": "1",
                                    "MIN_SECTIONS": "2", "MAX_IMAGES": "8"}
    assert local.build_env(7, short=True, style="ink") == {"TOPIC_ID": "7", "VIDEO_MODE": "shots",
                                                           "VISUAL_STYLE": "ink", **local.SHORT_MODE_ENV}
    assert local.build_env(7, short=False, style="ink", mode="slideshow") == {
        "TOPIC_ID": "7", "VIDEO_MODE": "slideshow", "VISUAL_STYLE": "ink"}


@pytest.mark.parametrize("stage", local.STAGES)
def test_select_stages_from_each_stage_runs_it_and_everything_after(stage):
    assert local.select_stages(stage, None) == list(local.STAGES[local.STAGES.index(stage):])
    assert local.select_stages(None, stage) == [stage] == local.select_stages(stage, stage)


def test_select_stages_rejects_an_unknown_start_stage():
    with pytest.raises(ValueError):
        local.select_stages("bogus", None)


def test_run_stage_dispatches_every_stage_to_its_module(monkeypatch):
    calls = []

    class Module:
        def __init__(self, name):
            self.name = name

        def main(self, *args, **kwargs):
            calls.append((self.name, args, kwargs))
            return 0

    monkeypatch.setattr(local.importlib, "import_module", lambda name: Module(name))
    for stage in local.STAGES:
        local.run_stage(stage, 161, None)
    assert calls == [("generate_script", (), {}), ("synthesize_speech", (161,), {}), ("plan_shots", (161,), {}),
                     ("generate_images", (161,), {}), ("ground_focus", (161,), {}), ("make_video", (161,), {})]
    calls.clear()
    local.run_stage("video", 161, 30.0)
    local.run_stage("video", 161, 0)
    local.run_stage("images", 161, 30.0)
    assert calls == [("render_video", (161,), {"limit_seconds": 30.0}), ("render_video", (161,), {"limit_seconds": 0}),
                     ("generate_images", (161,), {})]


@pytest.mark.parametrize("code", [1, 2, "bad"])
def test_a_stage_returning_a_non_zero_code_is_a_failure(monkeypatch, code):
    monkeypatch.setattr(local.importlib, "import_module", lambda name: type("M", (), {"main": lambda *a, **k: code}))
    with pytest.raises(RuntimeError, match="generate_script 回傳非零結束碼"):
        local.run_stage("script", 1, None)


@pytest.mark.parametrize("code", [0, None])
def test_a_stage_returning_zero_or_none_is_fine(monkeypatch, code):
    monkeypatch.setattr(local.importlib, "import_module", lambda name: type("M", (), {"main": lambda *a, **k: code}))
    local.run_stage("script", 1, None)


def test_run_stage_rejects_an_unknown_stage():
    with pytest.raises(ValueError, match="unknown stage"):
        local.run_stage("publish", 1, None)


def test_local_runner_imports_no_publishing_module():
    import ast
    tree = ast.parse(Path(local.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            imported.add(node.value)
    assert not imported & {"upload_youtube", "publish_podcast", "notify", "get_youtube_refresh_token"}


def test_main_runs_every_stage_in_order_with_the_environment_set(runner, capsys):
    assert local.main(["--topic-id", "161", "--short", "--style", "cinematic"]) == 0
    assert runner == [(stage, 161, None) for stage in local.STAGES]
    assert os.environ["TOPIC_ID"] == "161" and os.environ["VIDEO_MODE"] == "shots"
    assert os.environ["VISUAL_STYLE"] == "cinematic" and os.environ["MAX_IMAGES"] == "8"
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.startswith("====")] == [f"==== {s}" for s in local.STAGES]
    expected = (f"\n完成。成品與報告在 {common.get_episode_dir(161)}:final.mp4、contact_sheet.png、video_report.json、"
                "plan_report.json、image_report.json、subtitles.srt\n")
    assert expected in out


def test_main_without_short_leaves_the_short_mode_variables_alone(runner):
    assert local.main(["--topic-id", "3"]) == 0
    assert os.environ["VISUAL_STYLE"] == "ink"
    assert not set(local.SHORT_MODE_ENV) & set(os.environ)


@pytest.fixture
def webhook_probe(monkeypatch, runner_env):
    """Stage stand-in that records the webhook variable and really calls common.notify_discord with requests faked."""
    import requests
    posts, seen = [], []
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: posts.append(url) or type(
        "Response", (), {"raise_for_status": lambda self: None})())

    def stage(name, eid, limit):
        seen.append((name, os.environ.get("DISCORD_WEBHOOK_URL")))
        common.notify_discord(f"alert from {name}")
        if name == os.environ.get("FAIL_STAGE"):
            raise RuntimeError("stage failed")

    monkeypatch.setattr(local, "run_stage", stage)
    return posts, seen


@pytest.mark.parametrize("configured", ["https://discord.example/hook", None, ""])
def test_a_dry_run_never_posts_to_discord_even_when_a_webhook_is_configured(webhook_probe, configured):
    posts, seen = webhook_probe
    if configured is None:
        os.environ.pop("DISCORD_WEBHOOK_URL", None)
    else:
        os.environ["DISCORD_WEBHOOK_URL"] = configured
    assert local.main(["--topic-id", "3"]) == 0
    assert seen == [(stage, "") for stage in local.STAGES] and posts == []


def test_the_webhook_stays_blank_when_a_stage_fails_too(monkeypatch, webhook_probe):
    posts, seen = webhook_probe
    os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.example/hook"
    os.environ["FAIL_STAGE"] = "script"
    assert local.main(["--topic-id", "3"]) == 1
    assert seen == [("script", "")] and posts == []


def test_the_webhook_is_left_alone_when_the_arguments_are_bad(webhook_probe):
    os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.example/hook"
    with pytest.raises(SystemExit):
        local.main(["--topic-id", "0"])
    assert os.environ["DISCORD_WEBHOOK_URL"] == "https://discord.example/hook"


def test_the_help_text_says_no_discord_message_is_sent():
    assert "不會發 Discord 通知" in local.__doc__


def test_main_from_stage_and_only(runner, capsys):
    local.main(["--topic-id", "3", "--from-stage", "images"])
    assert [c[0] for c in runner] == ["images", "ground", "video"]
    runner.clear()
    local.main(["--topic-id", "3", "--only", "plan"])
    assert [c[0] for c in runner] == ["plan"]
    assert "--only" not in capsys.readouterr().out


def test_only_wins_over_from_stage_and_says_so(runner, capsys):
    assert local.main(["--topic-id", "3", "--from-stage", "images", "--only", "tts"]) == 0
    assert [c[0] for c in runner] == ["tts"]
    assert "--only tts" in capsys.readouterr().out


def test_limit_seconds_reaches_the_video_stage_without_a_warning(runner, capsys):
    assert local.main(["--topic-id", "3", "--only", "video", "--limit-seconds", "30"]) == 0
    assert runner == [("video", 3, 30.0)]
    assert "--limit-seconds" not in capsys.readouterr().out


def test_limit_seconds_without_the_video_stage_is_ignored_and_says_so(runner, capsys):
    assert local.main(["--topic-id", "3", "--only", "images", "--limit-seconds", "30"]) == 0
    assert "--limit-seconds" in capsys.readouterr().out
    assert local.main(["--topic-id", "3", "--from-stage", "ground", "--limit-seconds", "30"]) == 0
    assert "--limit-seconds" not in capsys.readouterr().out


def test_a_failing_stage_stops_the_run_and_names_the_stage(monkeypatch, runner, capsys):
    def stage(name, eid, limit):
        runner.append(name)
        if name == "tts":
            raise RuntimeError("az login expired")

    monkeypatch.setattr(local, "run_stage", stage)
    assert local.main(["--topic-id", "3"]) == 1
    assert runner == ["script", "tts"]
    captured = capsys.readouterr()
    assert "==== FAILED tts: az login expired" in captured.err and "Traceback" in captured.err
    assert "完成" not in captured.out and "==== plan" not in captured.out


def test_a_render_error_in_the_limited_video_stage_is_reported_not_raised(monkeypatch, runner_env, capsys):
    import render_video

    def render(episode_id, limit_seconds=None):
        raise render_video.RenderError("no audio")

    monkeypatch.setattr(local.importlib, "import_module", lambda name: type("M", (), {"main": staticmethod(render)}))
    assert local.main(["--topic-id", "3", "--only", "video", "--limit-seconds", "5"]) == 1
    assert "==== FAILED video: no audio" in capsys.readouterr().err


def test_main_puts_the_scripts_folder_first_on_sys_path_once(monkeypatch, runner):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if os.path.abspath(p) != local.SCRIPTS_DIR])
    local.main(["--topic-id", "3", "--only", "plan"])
    assert sys.path[0] == local.SCRIPTS_DIR
    local.main(["--topic-id", "3", "--only", "plan"])
    assert sys.path.count(local.SCRIPTS_DIR) == 1


@pytest.mark.parametrize("argv", [
    [], ["--topic-id", "0"], ["--topic-id", "-3"], ["--topic-id", "x"], ["--topic-id", "1.5"],
    ["--topic-id", "1", "--limit-seconds", "0"], ["--topic-id", "1", "--limit-seconds", "-5"],
    ["--topic-id", "1", "--limit-seconds", "nan"], ["--topic-id", "1", "--limit-seconds", "inf"],
    ["--topic-id", "1", "--limit-seconds", "abc"], ["--topic-id", "1", "--style", "bogus"],
    ["--topic-id", "1", "--only", "bogus"], ["--topic-id", "1", "--from-stage", "publish"]])
def test_bad_arguments_exit_before_anything_runs(runner, argv):
    with pytest.raises(SystemExit) as stopped:
        local.main(argv)
    assert stopped.value.code == 2 and runner == [] and "TOPIC_ID" not in os.environ


def test_style_choices_and_default_match_the_visual_style_presets():
    import visual_style
    parser = local.build_parser()
    assert parser.parse_args(["--topic-id", "1"]).style == visual_style.DEFAULT_STYLE
    for style in visual_style.STYLE_SUFFIXES:
        assert parser.parse_args(["--topic-id", "1", "--style", style]).style == style
    for stage in local.STAGES:
        assert parser.parse_args(["--topic-id", "1", "--from-stage", stage, "--only", stage]).only == stage


def _cli(*args, encoding="utf-8"):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": encoding, "DISCORD_WEBHOOK_URL": ""}
    return subprocess.run([sys.executable, local.__file__, *args], cwd=REPO, env=env,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=60)


def test_help_prints_the_traditional_chinese_usage():
    result = _cli("--help")
    assert result.returncode == 0
    for text in ("本機 dry-run", "不會上傳 YouTube", "本檔不 import upload_youtube", "--topic-id", "--short", "--style",
                 "--from-stage", "--only", "--limit-seconds", "只跑這一階段", "約 2 分鐘、2 段旁白、最多 8 張圖",
                 "只渲染前 N 秒", "az login"):
        assert text in result.stdout


def test_help_does_not_crash_on_a_console_that_cannot_show_chinese():
    result = _cli("--help", encoding="cp1252")
    assert result.returncode == 0 and "usage:" in result.stdout and "--topic-id" in result.stdout


def test_importing_the_runner_loads_no_heavy_module():
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import run_episode_local; "
            "print(sorted(m for m in ('common', 'openai', 'azure', 'PIL', 'numpy', 'render_video', 'make_video', "
            "'generate_script') if m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", code, str(Path(local.__file__).parent)], capture_output=True,
                            text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0 and result.stdout.strip() == "[]"


def test_the_script_exits_non_zero_and_names_the_failed_stage():
    result = _cli("--topic-id", "99999", "--only", "script")   # not in the topic queue: fails before any network call
    assert result.returncode == 1
    assert "==== script" in result.stdout and "完成" not in result.stdout
    assert "==== FAILED script:" in result.stderr and "99999" in result.stderr and "Traceback" in result.stderr
