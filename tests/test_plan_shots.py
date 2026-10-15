import json
import logging
import math
import re

import pytest

import plan_shots
import shot_plan
from fakes import FakeClient


def unit(i, section, start, text="天地玄黃宇宙洪荒。"):
    return {"id": i, "section": section, "text": text, "start": start, "end": start + 4.0}


def timings():
    units = [unit(i, 1 if i < 4 else 2, i * 4.0) for i in range(8)]
    return {"units": units, "words": [], "total": 32.0}


SCRIPT = {"title": "題材", "sections": [
    {"section_id": 1, "narration": "第一段旁白。", "image_prompt": "legacy prompt one"},
    {"section_id": 2, "narration": "第二段旁白。", "image_prompt": "legacy prompt two"}]}


def content(first, second, prompt):
    return {"scenes": [{"id": "A", "beats": [first, second], "image_prompt": prompt}],
            "beats": [{"id": first, "motion": "push_in", "focus_hint": "x", "overlay": None},
                      {"id": second, "motion": "pull_out", "focus_hint": "y", "overlay": None}]}


BIBLE = {"bible": "Qin dynasty, 221-206 BCE, ochre palette"}
CARD = {"text": "秦", "label": "人物"}
MOTION_CYCLE = ("push_in", "pull_out")


def make_timings(units_per_section, words=()):
    units, t = [], 0.0
    for section, count in enumerate(units_per_section, start=1):
        for _ in range(count):
            units.append({"id": len(units), "section": section, "text": f"第{len(units)}句。",
                          "start": t, "end": t + 4.0})
            t += 4.0
    return {"units": units, "words": list(words), "total": t}


def make_script(section_count):
    return {"title": "題材", "sections": [
        {"section_id": k, "narration": f"旁白{k}。", "image_prompt": f"legacy prompt {k}"}
        for k in range(1, section_count + 1)]}


def planned(scenes, overlays=None):
    """scenes: [(image_prompt, [beat ids])]; motions alternate inside each scene."""
    overlays = overlays or {}
    return {"scenes": [{"id": f"S{k}", "beats": ids, "image_prompt": prompt}
                       for k, (prompt, ids) in enumerate(scenes)],
            "beats": [{"id": bid, "motion": MOTION_CYCLE[i % 2], "focus_hint": "x", "overlay": overlays.get(bid)}
                      for _, ids in scenes for i, bid in enumerate(ids)]}


def test_plan_episode_assigns_global_scene_ids_and_appends_the_bible():
    client = FakeClient([BIBLE, content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    shots, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    assert [s["id"] for s in shots["scenes"]] == ["s1", "s2"]
    assert all("Setting notes: Qin dynasty" in s["image_prompt"] for s in shots["scenes"])
    assert [b["scene"] for b in shots["beats"]] == ["s1", "s1", "s2", "s2"]
    assert [(b["start"], b["end"]) for b in shots["beats"]] == [(0.0, 8.0), (8.0, 16.0), (16.0, 24.0), (24.0, 32.0)]
    assert report["beats"] == 4 and report["scenes"] == 2
    assert [s["fallback"] for s in report["sections"]] == [False, False]


def test_invalid_output_is_retried_with_the_errors_in_the_prompt():
    client = FakeClient([BIBLE, {"scenes": "nope", "beats": []}, content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    shots, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    assert report["sections"][0]["attempts"] == 2 and report["sections"][0]["fallback"] is False
    assert "上一次輸出的錯誤" in client.prompts()[2]
    assert len(shots["scenes"]) == 2


def test_section_that_never_validates_uses_the_fallback_plan():
    client = FakeClient([BIBLE] + ["garbage"] * plan_shots.MAX_ATTEMPTS + [content("b3", "b4", "pB")])
    shots, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    assert report["sections"][0]["fallback"] is True and report["sections"][1]["fallback"] is False
    first = shots["scenes"][0]
    assert first["id"] == "s1" and "legacy prompt one" in first["image_prompt"]
    assert [b["motion"] for b in shots["beats"][:2]] == ["push_in", "pan_lr"]


def test_a_failing_bible_call_does_not_stop_planning():
    client = FakeClient([RuntimeError("boom"), content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    shots, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    assert report["bible"] == ""
    assert all("Setting notes" not in s["image_prompt"] for s in shots["scenes"])


def test_too_small_image_budget_falls_back_without_calling_the_planner():
    client = FakeClient([BIBLE])
    shots, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 1)
    assert len(client.calls) == 1
    assert all(s["fallback"] for s in report["sections"])
    assert len(shots["scenes"]) == 2
    assert report["budget"] == {"limit": 1, "needed": 2, "used": None, "exceeded": True}


def test_segmentation_failure_degrades_to_one_beat_per_unit():
    units = [unit(0, 1, 0.0), {**unit(1, 1, 4.0), "end": 18.0}]
    groups, bounds = plan_shots.segment(units)
    assert groups == [[0], [1]] and bounds is None


def test_main_is_a_no_op_outside_shots_mode(monkeypatch):
    monkeypatch.setenv("VIDEO_MODE", "slideshow")
    assert plan_shots.main(1) == 0


def test_main_writes_shots_and_report(monkeypatch, tmp_path):
    import common
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    monkeypatch.delenv("VISUAL_STYLE", raising=False)
    ep = common.get_episode_dir(7)
    (ep / "script.json").write_text(json.dumps(SCRIPT, ensure_ascii=False), encoding="utf-8")
    (ep / "timings.json").write_text(json.dumps(timings(), ensure_ascii=False), encoding="utf-8")
    client = FakeClient([BIBLE, content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    monkeypatch.setattr(plan_shots, "make_client", lambda: client)
    assert plan_shots.main(7) == 0
    shots = json.loads((ep / "shots.json").read_text(encoding="utf-8"))
    assert shots["style"] == "cinematic" and len(shots["beats"]) == 4
    assert (ep / "plan_report.json").exists()


def test_main_requires_units(monkeypatch, tmp_path):
    import common
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    ep = common.get_episode_dir(8)
    (ep / "script.json").write_text(json.dumps(SCRIPT), encoding="utf-8")
    (ep / "timings.json").write_text(json.dumps({"sections": [], "total": 0}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        plan_shots.main(8)


@pytest.mark.parametrize("junk", [None, "x", 5, {"motion": "push_in"}, {"id": ["b1"], "motion": "push_in"}])
def test_a_junk_beat_entry_in_a_valid_reply_is_ignored(junk):
    reply = content("b1", "b2", "pA")
    reply["beats"].append(junk)
    client = FakeClient([BIBLE, reply, content("b3", "b4", "pB")])
    shots, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    assert report["sections"][0]["fallback"] is False and report["sections"][0]["attempts"] == 1
    assert [b["id"] for b in shots["beats"]] == ["b1", "b2", "b3", "b4"]


@pytest.mark.parametrize("foreign", [
    {"id": "b1", "motion": "tilt_up", "focus_hint": "z", "overlay": None},
    {"id": "b1", "motion": "tilt_up", "focus_hint": "z", "overlay": {"text": "x"}},
])
def test_a_foreign_beat_entry_cannot_override_another_sections_beat(foreign):
    reply = content("b3", "b4", "pB")
    reply["beats"].append(foreign)
    client = FakeClient([BIBLE, content("b1", "b2", "pA"), reply])
    shots, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    assert not any(s["fallback"] for s in report["sections"])
    assert shots["beats"][0]["motion"] == "push_in" and shots["beats"][0]["overlays"] == []


def test_an_api_error_is_retried_and_is_not_fatal():
    client = FakeClient([BIBLE, RuntimeError("503"), content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    shots, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    assert report["sections"][0]["attempts"] == 2 and report["sections"][0]["fallback"] is False
    assert len(shots["scenes"]) == 2


def test_a_failing_second_section_falls_back_to_its_own_prompt():
    client = FakeClient([BIBLE, content("b1", "b2", "pA")] + ["garbage"] * plan_shots.MAX_ATTEMPTS)
    shots, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    assert [s["fallback"] for s in report["sections"]] == [False, True]
    assert shots["scenes"][1]["id"] == "s2" and shots["scenes"][1]["image_prompt"].startswith("legacy prompt two")
    assert [b["motion"] for b in shots["beats"][2:]] == ["push_in", "pan_lr"]


@pytest.mark.parametrize("section_two", [{"image_prompt": ""}, {}])
def test_the_fallback_prompt_uses_the_narration_when_the_section_has_no_image_prompt(section_two):
    script = {**SCRIPT, "sections": [SCRIPT["sections"][0], {"section_id": 2, "narration": "第二段旁白。", **section_two}]}
    client = FakeClient([BIBLE, content("b1", "b2", "pA")] + ["garbage"] * plan_shots.MAX_ATTEMPTS)
    shots, _ = plan_shots.plan_episode(client, script, timings(), "ink", 24)
    assert shots["scenes"][1]["image_prompt"].startswith("A historical scene illustrating: 第二段旁白。")


def test_the_scene_budget_limits_a_section_to_its_share_of_the_image_limit():
    two_scenes = planned([("pA", ["b1"]), ("pB", ["b2"])])
    client = FakeClient([BIBLE, two_scenes, content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    _, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 2)
    assert report["sections"][0]["attempts"] == 2 and report["sections"][0]["fallback"] is False
    assert "too many scenes: 2 > 1" in client.prompts()[2]


def test_every_section_gets_its_own_scene_budget():
    # 8 + 4 units -> 4 + 2 beats; MAX_IMAGES=4 -> budgets [2, 1]
    first_split_in_two = planned([("pA", ["b1", "b2"]), ("pB", ["b3", "b4"])])
    second_split_in_two = planned([("pC", ["b5"]), ("pD", ["b6"])])
    client = FakeClient([BIBLE, first_split_in_two, second_split_in_two, planned([("pC", ["b5", "b6"])])])
    shots, report = plan_shots.plan_episode(client, make_script(2), make_timings([8, 4]), "ink", 4)
    assert report["beats"] == 6 and report["scenes"] == 3
    assert [s["attempts"] for s in report["sections"]] == [1, 2]
    assert "too many scenes: 2 > 1" in client.prompts()[3]


def test_the_per_section_overlay_cap_is_enforced():
    two_cards = planned([("pA", ["b1", "b2"])], {"b1": CARD, "b2": CARD})
    client = FakeClient([BIBLE, two_cards, content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    _, report = plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    assert report["sections"][0]["attempts"] == 2
    assert "too many overlays: 2 > 1" in client.prompts()[2]


def test_the_episode_wide_overlay_cap_drops_the_surplus_cards():
    replies = [planned([(f"p{k}", [f"b{k}"])], {f"b{k}": CARD}) for k in range(1, 5)]
    client = FakeClient([BIBLE] + replies)
    shots, report = plan_shots.plan_episode(client, make_script(4), make_timings([2, 2, 2, 2]), "ink", 24)
    assert report["beats"] == 4 and not any(s["fallback"] for s in report["sections"])
    assert report["overlays"] == 2 and sum(len(b["overlays"]) for b in shots["beats"]) == 2


def test_overlays_are_synced_to_the_spoken_word():
    words = [{"text": "1974年", "start": 5.0, "section": 1}]
    card = {"text": "1974年", "label": "年代"}
    reply = planned([("pA", ["b1", "b2"])], {"b1": card})
    client = FakeClient([BIBLE, reply, content("b3", "b4", "pB")])
    shots, report = plan_shots.plan_episode(client, SCRIPT, {**timings(), "words": words}, "ink", 24)
    assert report["overlays"] == 1
    assert shots["beats"][0]["overlays"] == [{"text": "1974年", "label": "年代", "at": 4.85, "dur": 3.1}]


def test_the_retry_prompt_carries_the_latest_errors_only():
    missing_beats = {"scenes": [{"id": "A", "beats": ["b1"], "image_prompt": "p"}], "beats": []}
    client = FakeClient([BIBLE, {"scenes": "nope", "beats": []}, missing_beats,
                         content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    plan_shots.plan_episode(client, SCRIPT, timings(), "ink", 24)
    prompts = client.prompts()
    assert "output must be a JSON object" in prompts[2]
    assert "scenes must list every beat exactly once" in prompts[3]
    assert "output must be a JSON object" not in prompts[3]
    assert prompts[3].count("上一次輸出的錯誤") == 1


def test_plan_episode_degrades_to_one_beat_per_unit_when_segmentation_fails():
    units = [unit(0, 1, 0.0), {**unit(1, 1, 4.0), "end": 18.0}]
    client = FakeClient([BIBLE, content("b1", "b2", "pA")])
    shots, report = plan_shots.plan_episode(client, make_script(1), {"units": units, "words": []}, "ink", 24)
    assert report["bounds"] is None and report["beats"] == 2
    assert [b["unit_ids"] for b in shots["beats"]] == [[0], [1]]


def test_the_planner_prompt_carries_the_bible_the_beat_text_and_the_seconds():
    client = FakeClient([BIBLE, content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    plan_shots.plan_episode(client, make_script(2), make_timings([4, 4]), "ink", 24)
    first = client.prompts()[1]
    assert "【視覺設定】" + BIBLE["bible"] in first
    assert "b1 [8.0s] 第0句。第1句。" in first and "b2 [8.0s] 第2句。第3句。" in first
    assert "b3 [" not in first


def prepare_episode(monkeypatch, tmp_path, episode_id, client):
    import common
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    ep = common.get_episode_dir(episode_id)
    (ep / "script.json").write_text(json.dumps(SCRIPT, ensure_ascii=False), encoding="utf-8")
    (ep / "timings.json").write_text(json.dumps(timings(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(plan_shots, "make_client", lambda: client)
    return ep


def read_report(ep):
    return json.loads((ep / "plan_report.json").read_text(encoding="utf-8"))


def test_main_plans_with_the_default_image_limit(monkeypatch, tmp_path):
    monkeypatch.delenv("MAX_IMAGES", raising=False)
    client = FakeClient([BIBLE, content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    ep = prepare_episode(monkeypatch, tmp_path, 9, client)
    assert plan_shots.main(9) == 0
    report = read_report(ep)
    assert not any(s["fallback"] for s in report["sections"])
    assert report["budget"] == {"limit": 24, "needed": 2, "used": 24, "exceeded": False}


def test_main_passes_max_images_to_the_planner(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_IMAGES", "2")
    two_scenes = planned([("pA", ["b1"]), ("pB", ["b2"])])
    client = FakeClient([BIBLE, two_scenes, content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    ep = prepare_episode(monkeypatch, tmp_path, 10, client)
    assert plan_shots.main(10) == 0
    sections = read_report(ep)["sections"]
    assert sections[0]["attempts"] == 2 and not any(s["fallback"] for s in sections)


def test_main_honours_visual_style(monkeypatch, tmp_path):
    monkeypatch.setenv("VISUAL_STYLE", "ink")
    monkeypatch.delenv("MAX_IMAGES", raising=False)
    client = FakeClient([BIBLE, content("b1", "b2", "pA"), content("b3", "b4", "pB")])
    ep = prepare_episode(monkeypatch, tmp_path, 11, client)
    assert plan_shots.main(11) == 0
    assert json.loads((ep / "shots.json").read_text(encoding="utf-8"))["style"] == "ink"


def test_max_images_defaults_to_24_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("MAX_IMAGES", raising=False)
    assert plan_shots.max_images() == 24
    monkeypatch.setenv("MAX_IMAGES", "")
    assert plan_shots.max_images() == 24
    monkeypatch.setenv("MAX_IMAGES", "5")
    assert plan_shots.max_images() == 5


MAX_SCENES_RE = re.compile(r"全部最多 (\d+) 個場景")


def valid_replies(beat_counts):
    """One valid reply per section, packing each section's beats four to a scene."""
    replies, first = [], 1
    for count in beat_counts:
        ids = [f"b{first + i}" for i in range(count)]
        replies.append(planned([(f"p{first}-{k}", ids[k:k + 4]) for k in range(0, count, 4)]))
        first += count
    return replies


def plan_counts(beat_counts, image_limit):
    """Plan an episode whose sections have the given beat counts (8 s beats, two 4 s units each)."""
    client = FakeClient([BIBLE] + valid_replies(beat_counts))
    shots, report = plan_shots.plan_episode(client, make_script(len(beat_counts)),
                                            make_timings([2 * n for n in beat_counts]), "ink", image_limit)
    return client, shots, report


def planned_budgets(client):
    return [int(MAX_SCENES_RE.search(p).group(1)) for p in client.prompts()[1:]]


def minimum_scenes(beat_counts):
    return sum(math.ceil(n / shot_plan.MAX_BEATS_PER_SCENE) for n in beat_counts)


# (beats per section, MAX_IMAGES, scene limit actually used or None for the every-section fallback)
SOFT_CAP_CASES = [
    pytest.param([10, 12, 8], 24, 24, id="fits: the spare scenes are shared as before"),
    pytest.param([4] * 7, 8, 8, id="needed below the limit"),
    pytest.param([4] * 8, 8, 8, id="needed == limit"),
    pytest.param([4] * 9, 8, 11, id="needed == limit + 1: two spare scenes"),
    pytest.param([4] * 11, 8, 12, id="needed + 2 is clipped to the ceiling"),
    pytest.param([4] * 12, 8, 12, id="needed == ceiling"),
    pytest.param([4] * 13, 8, None, id="needed == ceiling + 1: every section falls back"),
    pytest.param([4] * 4, 3, 4, id="odd limit: the ceiling is floor(1.5 * 3) = 4"),
    pytest.param([4] * 5, 3, None, id="odd limit: 5 is above floor(4.5)"),
    pytest.param([9] * 8, 24, 24, id="review probe: 8 sections of 9 beats fit exactly"),
    pytest.param([9] * 7 + [13], 24, 27, id="review probe: one long section no longer drops the episode"),
]


@pytest.mark.parametrize("beat_counts, limit, used", SOFT_CAP_CASES)
def test_the_image_limit_is_a_soft_cap(beat_counts, limit, used):
    client, shots, report = plan_counts(beat_counts, limit)
    needed = minimum_scenes(beat_counts)
    assert report["budget"] == {"limit": limit, "needed": needed, "used": used, "exceeded": needed > limit}
    if used is None:
        assert len(client.calls) == 1
        assert all(s["fallback"] and s["errors"] == ["scene budget too small"] for s in report["sections"])
        assert len(shots["scenes"]) == len(beat_counts)
    else:
        assert planned_budgets(client) == shot_plan.allocate_scene_budget(beat_counts, used)
        assert not any(s["fallback"] for s in report["sections"])
        assert report["scenes"] == needed


def test_a_within_cap_episode_keeps_the_old_budgets_and_report_keys():
    client, _, report = plan_counts([10, 12, 8], 24)
    assert planned_budgets(client) == [8, 9, 6]
    assert set(report) == {"beats", "scenes", "bounds", "bible", "overlays", "sections", "budget"}


def plan_warnings(caplog, beat_counts, limit):
    with caplog.at_level(logging.WARNING, logger="plan_shots"):
        plan_counts(beat_counts, limit)
    return [r.getMessage() for r in caplog.records if r.name == "plan_shots" and r.levelno >= logging.WARNING]


def test_no_warning_while_the_episode_fits_the_cap(caplog):
    assert plan_warnings(caplog, [4] * 8, 8) == []


def test_exceeding_the_cap_logs_by_how_much_and_the_limit_used(caplog):
    messages = plan_warnings(caplog, [4] * 9, 8)
    assert len(messages) == 1
    assert "MAX_IMAGES=8 exceeded by 1" in messages[0] and "(9 scenes needed)" in messages[0]
    assert "planning with 11 scenes" in messages[0]


def test_exceeding_the_ceiling_logs_the_fallback(caplog):
    messages = plan_warnings(caplog, [4] * 13, 8)
    assert len(messages) == 1
    assert "MAX_IMAGES=8 is too small: at least 13 scenes are needed" in messages[0]
    assert "every section falls back" in messages[0]


def test_main_reads_max_images_for_the_soft_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_IMAGES", "2")
    client = FakeClient([BIBLE] + valid_replies([1, 1, 1]))
    ep = prepare_episode(monkeypatch, tmp_path, 12, client)
    (ep / "script.json").write_text(json.dumps(make_script(3), ensure_ascii=False), encoding="utf-8")
    (ep / "timings.json").write_text(json.dumps(make_timings([2, 2, 2]), ensure_ascii=False), encoding="utf-8")
    assert plan_shots.main(12) == 0
    report = read_report(ep)
    assert report["budget"] == {"limit": 2, "needed": 3, "used": 3, "exceeded": True}
    assert not any(s["fallback"] for s in report["sections"])
    assert len(json.loads((ep / "shots.json").read_text(encoding="utf-8"))["scenes"]) == 3
