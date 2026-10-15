import pytest

import shot_plan as sp

IDS = ["b1", "b2", "b3"]


def good():
    return {
        "scenes": [{"id": "A", "beats": ["b1", "b2"], "image_prompt": "a farmer by a well"},
                   {"id": "B", "beats": ["b3"], "image_prompt": "a pit of clay soldiers"}],
        "beats": [{"id": "b1", "motion": "push_in", "focus_hint": "farmer", "overlay": None},
                  {"id": "b2", "motion": "pull_out", "focus_hint": "mounds",
                   "overlay": {"text": "1974年", "label": "年代"}},
                  {"id": "b3", "motion": "push_in", "focus_hint": "soldiers", "overlay": None}],
    }


def test_good_content_passes():
    assert sp.validate_content(good(), IDS, max_scenes=7, max_overlays=3) == []


@pytest.mark.parametrize("raw", [None, [], "text", {}, {"scenes": {}, "beats": []},
                                 {"scenes": [None], "beats": []}, {"scenes": [{"id": "A"}], "beats": []}])
def test_malformed_output_returns_errors_instead_of_raising(raw):
    assert sp.validate_content(raw, IDS, 7, 3) != []


def test_scenes_must_cover_beats_in_order():
    raw = good()
    raw["scenes"][0]["beats"] = ["b2", "b1"]
    assert "exactly once, in order" in sp.validate_content(raw, IDS, 7, 3)[0]


def test_scene_and_overlay_limits():
    one_scene = good()
    one_scene["scenes"] = [{"id": "A", "beats": IDS, "image_prompt": "x"}]
    assert sp.validate_content(one_scene, IDS, max_scenes=0, max_overlays=3)[0].startswith("too many scenes")
    assert any("too many overlays" in e for e in sp.validate_content(good(), IDS, 7, max_overlays=0))
    ids5 = ["b1", "b2", "b3", "b4", "b5"]
    big = {"scenes": [{"id": "A", "beats": ids5, "image_prompt": "x"}],
           "beats": [{"id": b, "motion": sp.MOTIONS[k], "focus_hint": "h"} for k, b in enumerate(ids5)]}
    assert any("more than 4 beats" in e for e in sp.validate_content(big, ids5, 7, 3))


def test_motion_rules():
    raw = good()
    raw["beats"][1]["motion"] = "push_in"
    assert any("repeats the previous motion" in e for e in sp.validate_content(raw, IDS, 7, 3))
    raw = good()
    raw["beats"][0]["motion"] = "zoom_wildly"
    assert any("invalid motion" in e for e in sp.validate_content(raw, IDS, 7, 3))


def test_overlay_and_focus_rules():
    raw = good()
    raw["beats"][1]["overlay"] = {"text": "這是一個太長的字卡文字內容", "label": "年代"}
    assert any("overlay needs" in e for e in sp.validate_content(raw, IDS, 7, 3))
    raw = good()
    raw["beats"][1]["overlay"] = {"text": "1974年", "label": "天氣"}
    assert any("overlay needs" in e for e in sp.validate_content(raw, IDS, 7, 3))
    raw = good()
    raw["beats"][0]["focus_hint"] = "  "
    assert any("needs a focus_hint" in e for e in sp.validate_content(raw, IDS, 7, 3))
    raw = good()
    raw["scenes"][0]["image_prompt"] = ""
    assert any("no image_prompt" in e for e in sp.validate_content(raw, IDS, 7, 3))


def test_allocate_scene_budget():
    assert sp.allocate_scene_budget([10, 12, 8], 24) == [8, 9, 6]
    assert sum(sp.allocate_scene_budget([7, 6], 24)) <= 24
    with pytest.raises(ValueError):
        sp.allocate_scene_budget([100], 24)


def test_allocate_scene_budget_exact_fit_and_empty_section():
    assert sp.allocate_scene_budget([12], 3) == [3]
    assert sp.allocate_scene_budget([0], 24) == [0]


@pytest.mark.parametrize("mutate, expected", [
    (lambda r: r["beats"][0].update(id=["b1"]), "missing beat entry b1"),
    (lambda r: r["beats"][0].update(id={"x": 1}), "missing beat entry b1"),
    (lambda r: r["scenes"][0].update(image_prompt=None), "no image_prompt"),
    (lambda r: r["scenes"][0].update(image_prompt=["a", "b"]), "no image_prompt"),
    (lambda r: r["beats"][0].update(focus_hint=None), "needs a focus_hint"),
    (lambda r: r["beats"][0].update(focus_hint=["farmer"]), "needs a focus_hint"),
    (lambda r: r["beats"][1].update(overlay={"text": 1974, "label": "年代"}), "overlay needs"),
    (lambda r: r["beats"][1].update(overlay={"text": None, "label": "年代"}), "overlay needs"),
    (lambda r: r["beats"][1].update(overlay="1974年"), "overlay needs"),
    (lambda r: r["scenes"].insert(1, {"id": "Z", "beats": [], "image_prompt": "x"}), "scene Z has no beats"),
])
def test_malformed_fields_are_reported_not_raised(mutate, expected):
    raw = good()
    mutate(raw)
    assert any(expected in e for e in sp.validate_content(raw, IDS, 7, 3))


def test_limits_are_inclusive():
    assert sp.validate_content(good(), IDS, max_scenes=2, max_overlays=1) == []
    ids4 = ["b1", "b2", "b3", "b4"]
    four = {"scenes": [{"id": "A", "beats": ids4, "image_prompt": "x"}],
            "beats": [{"id": b, "motion": sp.MOTIONS[k], "focus_hint": "h"} for k, b in enumerate(ids4)]}
    assert sp.validate_content(four, ids4, 7, 3) == []


def test_overlay_text_length_boundary():
    raw = good()
    raw["beats"][1]["overlay"] = {"text": "一二三四五六七八", "label": "年代"}
    assert sp.validate_content(raw, IDS, 7, 3) == []
    for text in ("一二三四五六七八九", ""):
        raw["beats"][1]["overlay"] = {"text": text, "label": "年代"}
        assert any("overlay needs" in e for e in sp.validate_content(raw, IDS, 7, 3))


def test_overlay_cap_and_beat_ids():
    assert sp.overlay_cap(97.5) == 4 and sp.overlay_cap(540) == 22 and sp.overlay_cap(1) == 1
    assert sp.beat_ids_for([[0], [1, 2]]) == ["b1", "b2"]


def test_fallback_content_is_valid_even_with_many_beats():
    ids = [f"b{k}" for k in range(1, 15)]
    content = sp.fallback_content(ids, "prompt")
    scene = content["scenes"][0]
    assert scene["beats"] == ids
    previous = None
    for b in content["beats"]:
        assert b["motion"] in sp.MOTIONS and b["motion"] != previous
        previous = b["motion"]


def test_fallback_content_passes_validation_for_up_to_four_beats():
    ids = ["b1", "b2", "b3", "b4"]
    assert sp.validate_content(sp.fallback_content(ids, "p"), ids, max_scenes=1, max_overlays=0) == []


def test_fallback_content_with_many_beats_only_exceeds_the_beat_limit():
    ids = [f"b{k}" for k in range(1, 15)]
    errors = sp.validate_content(sp.fallback_content(ids, "p"), ids, max_scenes=1, max_overlays=0)
    assert len(errors) == 1 and "more than 4 beats" in errors[0]


WORDS = [{"text": "1974", "start": 0.05, "section": 1}, {"text": "秦始皇陵", "start": 9.4, "section": 1}]


def test_overlay_syncs_to_the_spoken_word():
    card = sp.resolve_overlay({"text": "秦始皇", "label": "人物"}, 3.7, 14.0, WORDS)
    assert card == {"text": "秦始皇", "label": "人物", "at": 9.25, "dur": 3.2}


def test_overlay_digits_match():
    card = sp.resolve_overlay({"text": "1974年", "label": "年代"}, 0.0, 3.7, WORDS)
    assert card == {"text": "1974年", "label": "年代", "at": 0.1, "dur": 3.2}


def test_overlay_without_matching_word_uses_fallback_offset():
    spoken_but_unrelated = WORDS + [{"text": "阿房宮", "start": 12.0, "section": 1}]
    card = sp.resolve_overlay({"text": "驪山", "label": "地點"}, 11.0, 17.0, spoken_but_unrelated)
    assert card["at"] == 11.4


@pytest.mark.parametrize("key, word", [("秦始皇", "始皇"), ("7年", "7")])
def test_overlay_matches_a_word_contained_in_the_text_or_a_single_digit(key, word):
    words = [{"text": word, "start": 9.0, "section": 1}]
    assert sp.resolve_overlay({"text": key, "label": "人物"}, 3.7, 14.0, words)["at"] == 8.85


def test_overlay_uses_the_word_occurrence_inside_the_beat():
    spoken = [{"text": "秦始皇陵", "start": s, "section": 1} for s in (2.0, 9.4, 20.0)]
    assert sp.resolve_overlay({"text": "秦始皇", "label": "人物"}, 8.0, 14.0, spoken)["at"] == 9.25
    only_later = [{"text": "秦始皇陵", "start": 20.0, "section": 1}]
    assert sp.resolve_overlay({"text": "秦始皇", "label": "人物"}, 3.7, 14.0, only_later)["at"] == 4.1


def test_overlay_near_the_beat_end_is_pulled_back_so_it_still_fits():
    late = [{"text": "秦始皇陵", "start": 13.0, "section": 1}]
    card = sp.resolve_overlay({"text": "秦始皇", "label": "人物"}, 8.0, 14.0, late)
    assert card == {"text": "秦始皇", "label": "人物", "at": 12.8, "dur": 1.15}


def test_overlay_never_runs_past_the_beat_and_is_dropped_when_it_cannot_fit():
    card = sp.resolve_overlay({"text": "秦始皇", "label": "人物"}, 8.0, 10.5, WORDS)
    assert card["at"] + card["dur"] <= 10.5
    assert sp.resolve_overlay({"text": "秦始皇", "label": "人物"}, 9.0, 9.8, WORDS) is None


UNITS = [{"id": 0, "section": 1, "text": "甲。", "start": 0.0, "end": 8.0},
         {"id": 1, "section": 1, "text": "乙。", "start": 8.0, "end": 16.0},
         {"id": 2, "section": 1, "text": "丙。", "start": 16.0, "end": 24.0}]


def test_assemble_shots_maps_scenes_focus_and_overlay_gap():
    content = {"scenes": [{"id": "s1", "beats": ["b1", "b2"], "image_prompt": "p1"},
                          {"id": "s2", "beats": ["b3"], "image_prompt": "p2"}],
               "beats": [{"id": "b1", "motion": "push_in", "focus_hint": "h1",
                          "overlay": {"text": "甲甲", "label": "人物"}},
                         {"id": "b2", "motion": "pull_out", "focus_hint": "h2", "overlay": None},
                         {"id": "b3", "motion": "push_in", "focus_hint": "h3", "overlay": None}]}
    words = [{"text": "甲甲", "start": 1.0, "section": 1}]
    shots = sp.assemble_shots("ink", UNITS, [[0], [1], [2]], content, {"b2": [0.2, 0.7]}, words, max_overlays=5)
    assert shots["style"] == "ink"
    assert shots["scenes"] == [{"id": "s1", "image_prompt": "p1", "image": "img_s1.png"},
                               {"id": "s2", "image_prompt": "p2", "image": "img_s2.png"}]
    b1, b2, b3 = shots["beats"]
    assert (b1["scene"], b2["scene"], b3["scene"]) == ("s1", "s1", "s2")
    assert b2["focus"] == [0.2, 0.7] and b1["focus"] == [0.5, 0.5]
    assert b1["unit_ids"] == [0] and (b1["start"], b1["end"]) == (0.0, 8.0)
    assert b1["overlays"][0]["at"] == 0.85


def test_assemble_shots_drops_overlays_closer_than_the_minimum_gap_and_over_the_cap():
    content = {"scenes": [{"id": "s1", "beats": ["b1", "b2", "b3"], "image_prompt": "p"}],
               "beats": [{"id": "b1", "motion": "push_in", "focus_hint": "h", "overlay": {"text": "甲甲", "label": "人物"}},
                         {"id": "b2", "motion": "pull_out", "focus_hint": "h", "overlay": {"text": "乙乙", "label": "人物"}},
                         {"id": "b3", "motion": "push_in", "focus_hint": "h", "overlay": {"text": "丙丙", "label": "人物"}}]}
    words = [{"text": "甲甲", "start": 7.0, "section": 1}, {"text": "乙乙", "start": 8.5, "section": 1},
             {"text": "丙丙", "start": 17.0, "section": 1}]
    shots = sp.assemble_shots("ink", UNITS, [[0], [1], [2]], content, {}, words, max_overlays=5)
    kept = [o["text"] for b in shots["beats"] for o in b["overlays"]]
    assert kept == ["甲甲", "丙丙"]
    capped = sp.assemble_shots("ink", UNITS, [[0], [1], [2]], content, {}, words, max_overlays=1)
    assert [o["text"] for b in capped["beats"] for o in b["overlays"]] == ["甲甲"]


def kept_card_starts(first_word_start, second_word_start=None):
    content = {"scenes": [{"id": "s1", "beats": ["b1", "b2"], "image_prompt": "p"}],
               "beats": [{"id": "b1", "motion": "push_in", "focus_hint": "h", "overlay": {"text": "甲甲", "label": "人物"}},
                         {"id": "b2", "motion": "pull_out", "focus_hint": "h", "overlay": {"text": "乙乙", "label": "人物"}}]}
    words = [{"text": "甲甲", "start": first_word_start, "section": 1}]
    if second_word_start is not None:
        words.append({"text": "乙乙", "start": second_word_start, "section": 1})
    shots = sp.assemble_shots("ink", UNITS[:2], [[0], [1]], content, {}, words, max_overlays=5)
    return [o["at"] for b in shots["beats"] for o in b["overlays"]]


def test_overlay_gap_is_four_seconds_between_card_starts():
    assert kept_card_starts(4.15) == [4.0, 8.4]
    assert kept_card_starts(5.15) == [5.0]
    assert kept_card_starts(4.65, 8.65) == [4.5, 8.5]


def test_overlay_in_a_very_short_beat_is_kept_when_at_least_the_minimum_duration_fits():
    card = sp.resolve_overlay({"text": "驪山", "label": "地點"}, 8.0, 9.0, [])
    assert card == {"text": "驪山", "label": "地點", "at": 8.05, "dur": 0.9}
