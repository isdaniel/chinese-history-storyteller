from datetime import timedelta
from types import SimpleNamespace

import timing_units as tu


def ev(kind, offset, dur, text):
    return {"type": f"SpeechSynthesisBoundaryType.{kind}", "offset": offset, "dur": dur, "text": text}


S0 = "甲乙丙丁戊己庚辛壬，子丑寅卯辰巳午未申。"
S1 = "天地玄黃宇宙洪荒日月盈昃。"
EVENTS = [
    ev("Sentence", 0.05, 4.5, S0),
    ev("Word", 0.05, 1.9, "甲乙丙丁戊己庚辛壬"),
    ev("Punctuation", 2.0, 0.3, "，"),
    ev("Word", 2.3, 1.6, "子丑寅卯辰巳午未申"),
    ev("Punctuation", 4.0, 0.2, "。"),
    ev("Sentence", 4.6, 2.8, S1),
    ev("Word", 4.6, 2.2, "天地玄黃宇宙洪荒日月盈昃"),
    ev("Punctuation", 7.0, 0.2, "。"),
]


def test_clean_text_and_split_do_not_create_stray_sentences():
    assert tu.clean_text("「你好。」 他說！」") == "你好。他說！"
    assert tu.split_sentences("「你好。」他說！」") == ["你好。", "他說！"]
    assert tu.tts_text("「甲。」「乙？」") == "甲。乙？"


def test_event_from_sdk_accepts_timedelta_and_ticks():
    a = SimpleNamespace(duration=timedelta(seconds=0.5), boundary_type="X.Word", audio_offset=12_000_000, text="詞")
    b = SimpleNamespace(duration=5_000_000, boundary_type="X.Word", audio_offset=12_000_000, text="詞")
    assert tu.event_from_sdk(a) == tu.event_from_sdk(b) == {"type": "X.Word", "offset": 1.2, "dur": 0.5, "text": "詞"}


def test_sentence_starts_prefers_sentence_events():
    starts, method = tu.sentence_starts(EVENTS, [S0, S1], 7.5)
    assert method == "sentence_boundary_events"
    assert starts == [0.0, 4.6]


def test_sentence_starts_falls_back_to_terminal_punctuation():
    events = [e for e in EVENTS if "Sentence" not in e["type"]]
    starts, method = tu.sentence_starts(events, [S0, S1], 7.5)
    assert method == "terminal_punctuation_events"
    assert starts == [0.0, 4.2]


def test_sentence_starts_falls_back_to_char_proportion():
    starts, method = tu.sentence_starts([], [S0, S1], 6.6)
    assert method == tu.PROPORTIONAL
    assert [round(s, 3) for s in starts] == [0.0, 4.0]


def test_section_units_split_into_clauses_with_pauses_on_the_preceding_clause():
    units, words, report = tu.section_units([S0, S1], EVENTS, 7.5, section=1, offset=0.0, first_id=0)
    assert [(u["id"], u["text"], u["start"], u["end"]) for u in units] == [
        (0, "甲乙丙丁戊己庚辛壬，", 0.0, 2.3),
        (1, "子丑寅卯辰巳午未申。", 2.3, 4.6),
        (2, S1, 4.6, 7.5),
    ]
    assert report["clause_level"] is True and report["method"] == "sentence_boundary_events"
    assert [w["text"] for w in words] == ["甲乙丙丁戊己庚辛壬", "子丑寅卯辰巳午未申", "天地玄黃宇宙洪荒日月盈昃"]


def test_section_units_apply_offset_and_first_id():
    units, words, _ = tu.section_units([S0, S1], EVENTS, 7.5, section=2, offset=10.0, first_id=5)
    assert [(u["id"], u["start"], u["end"], u["section"]) for u in units] == [
        (5, 10.0, 12.3, 2), (6, 12.3, 14.6, 2), (7, 14.6, 17.5, 2)]
    assert words[0]["start"] == 10.05 and words[0]["section"] == 2


def test_short_leading_clause_merges_into_next():
    sentence = "天地玄黃，宇宙洪荒日月盈昃辰宿列張。"
    events = [ev("Sentence", 0.05, 5.0, sentence), ev("Punctuation", 1.0, 0.2, "，"),
              ev("Punctuation", 5.0, 0.2, "。")]
    units, _, _ = tu.section_units([sentence], events, 5.5, 1, 0.0, 0)
    assert [(u["text"], u["start"], u["end"]) for u in units] == [(sentence, 0.0, 5.5)]


def test_mismatched_punctuation_events_keep_sentence_level_units():
    events = [e for e in EVENTS if not (e["type"].endswith("Punctuation") and e["text"] == "，")]
    units, _, report = tu.section_units([S0, S1], events, 7.5, 1, 0.0, 0)
    assert [(u["start"], u["end"]) for u in units] == [(0.0, 4.6), (4.6, 7.5)]
    assert report["clause_level"] is False


def test_no_events_at_all_still_produces_proportional_sentence_units():
    units, words, report = tu.section_units([S0, S1], [], 6.6, 1, 0.0, 0)
    assert report["method"] == tu.PROPORTIONAL
    assert [(u["start"], u["end"]) for u in units] == [(0.0, 4.0), (4.0, 6.6)]
    assert words == []


def test_build_timings_keeps_legacy_section_shape_and_adds_units():
    second = [ev("Sentence", 0.05, 7.0, S1), ev("Word", 0.05, 6.9, "天地玄黃宇宙洪荒日月盈昃"),
              ev("Punctuation", 7.0, 0.2, "。")]
    parts = [{"section_id": 1, "narration": "「" + S0 + S1 + "」", "audio_len": 7.5, "events": EVENTS},
             {"section_id": 2, "narration": S1, "audio_len": 7.5, "events": second}]
    timings = tu.build_timings(parts)
    assert timings["sections"][0] == {"section_id": 1, "start": 0.0, "end": 7.5, "duration": 7.5,
                                      "narration": "「" + S0 + S1 + "」"}
    assert timings["sections"][1]["start"] == 7.5
    assert timings["total"] == 15.0
    assert [u["id"] for u in timings["units"]] == [0, 1, 2, 3]
    assert timings["units"][3]["start"] == 7.5 and timings["units"][3]["end"] == 15.0
    assert [r["section"] for r in timings["timing_report"]] == [1, 2]
    assert {w["section"] for w in timings["words"]} == {1, 2}
