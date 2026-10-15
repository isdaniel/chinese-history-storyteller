import pytest

import beats


def mk_units(spec):
    """spec: [(text, seconds, section)] → units with contiguous times."""
    units, cursor = [], 0.0
    for i, (text, seconds, section) in enumerate(spec):
        units.append({"id": i, "section": section, "text": text, "start": cursor, "end": cursor + seconds})
        cursor += seconds
    return units


def test_covers_every_unit_once_in_order_within_bounds():
    units = mk_units([("甲。", 3.0, 1)] * 10)
    result, bounds = beats.segment_beats(units)
    assert [i for b in result for i in b] == list(range(10))
    for b in result:
        duration = units[b[-1]]["end"] - units[b[0]]["start"]
        assert bounds[0] <= duration <= bounds[1]


def test_is_deterministic():
    units = mk_units([("甲，", 2.0, 1), ("乙。", 3.0, 1)] * 6)
    assert beats.segment_beats(units) == beats.segment_beats(units)


def test_never_crosses_section_boundaries():
    units = mk_units([("甲。", 4.0, 1)] * 3 + [("乙。", 4.0, 2)] * 3)
    result, _ = beats.segment_beats(units)
    for b in result:
        assert len({units[i]["section"] for i in b}) == 1


def test_mid_sentence_penalty_overrides_closer_duration_fit():
    # Duration cost alone prefers cutting after the comma ([[0, 1], [2]]: 7.25 vs 11.25); only the penalty flips it.
    units = mk_units([("甲。", 3.5, 1), ("乙，", 4.0, 1), ("丙。", 4.0, 1)])
    result, _ = beats.segment_beats(units)
    assert result == [[0], [1, 2]]


def test_relaxes_bounds_when_strict_bounds_have_no_solution():
    units = mk_units([("甲。", 3.2, 1)])
    result, bounds = beats.segment_beats(units)
    assert result == [[0]] and bounds == (3.0, 11.5)


@pytest.mark.parametrize("seconds", [1.0, 14.0])
def test_raises_when_a_unit_cannot_fit_any_bounds(seconds):
    with pytest.raises(beats.BeatSegmentationError):
        beats.segment_beats(mk_units([("甲。", seconds, 1)]))


def test_one_beat_per_unit_last_resort():
    assert beats.one_beat_per_unit(mk_units([("甲。", 14.0, 1), ("乙。", 1.0, 1)])) == [[0], [1]]


def test_empty_units():
    assert beats.segment_beats([])[0] == []
