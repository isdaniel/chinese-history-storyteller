import subtitles as sub


def test_split_chunks_breaks_on_punctuation_within_the_limit():
    text = "一二三四五六七，八九十甲乙丙丁，戊己庚辛壬癸子，丑寅卯辰巳午。"
    chunks = sub.split_chunks(text, maxlen=16)
    assert "".join(chunks) == text
    assert all(len(c) <= 16 for c in chunks)
    assert chunks[0].endswith("，")


def test_split_chunks_hard_splits_text_without_punctuation():
    text = "天" * 50
    chunks = sub.split_chunks(text, maxlen=22)
    assert "".join(chunks) == text and all(len(c) <= 22 for c in chunks) and len(chunks) == 3


def test_split_chunks_empty_text():
    assert sub.split_chunks("") == []


def test_build_cues_use_the_true_unit_times_and_are_contiguous():
    units = [{"id": 0, "section": 1, "text": "甲乙丙丁戊己庚辛壬，子丑寅卯辰巳午未申酉戌亥。", "start": 2.0, "end": 8.0},
             {"id": 1, "section": 1, "text": "短句。", "start": 8.0, "end": 9.0}]
    cues = sub.build_cues(units, maxlen=12)
    assert cues[0][0] == 2.0 and cues[-1][1] == 9.0
    for (a0, a1, _), (b0, b1, _) in zip(cues, cues[1:]):
        assert abs(a1 - b0) < 1e-9
    first_unit = [c for c in cues if c[1] <= 8.0 + 1e-9]
    assert abs(sum(c[1] - c[0] for c in first_unit) - 6.0) < 1e-9


def test_fmt_srt_time():
    assert sub.fmt_srt_time(3661.5) == "01:01:01,500"
    assert sub.fmt_srt_time(0.0) == "00:00:00,000"
    assert sub.fmt_srt_time(59.9996) == "00:01:00,000"


def test_write_srt(tmp_path):
    out = tmp_path / "s.srt"
    sub.write_srt([(0.0, 1.5, "你好"), (1.5, 3.0, "再見")], out)
    assert out.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:01,500\n你好\n\n2\n00:00:01,500 --> 00:00:03,000\n再見\n")


# ---- 額外測試:每個行為、常數與邊界各有一個會失敗的測試 ----


def unit(text, start, end):
    return {"id": 0, "section": 1, "text": text, "start": start, "end": end}


def test_default_limit_is_22_characters():
    assert sub.CHUNK_MAX == 22
    three = ("甲" * 10 + "，") * 3
    assert [len(c) for c in sub.split_chunks(three)] == [22, 11]
    twelve_plus_eleven = "甲" * 11 + "，" + "乙" * 10 + "，"
    assert [len(c) for c in sub.split_chunks(twelve_plus_eleven)] == [12, 11]


def test_split_chunks_joins_pieces_up_to_exactly_maxlen():
    text = "甲乙丙丁，戊己庚辛，壬癸子丑，"
    assert sub.split_chunks(text, maxlen=10) == ["甲乙丙丁，戊己庚辛，", "壬癸子丑，"]


def test_split_chunks_splits_after_every_punctuation_mark():
    for mark in "，、；：。！？,;:!?":
        chunks = sub.split_chunks("甲" * 4 + mark + "乙" * 10, maxlen=12)
        assert chunks == ["甲" * 4 + mark, "乙" * 10], mark


def test_split_chunks_strips_whitespace_and_returns_nothing_for_blank_text():
    assert sub.split_chunks("  甲乙，  丙丁。 ", maxlen=4) == ["甲乙，", "丙丁。"]
    assert sub.split_chunks("   ") == []
    assert sub.split_chunks("\n") == []


DISTINCT = "".join(chr(0x4E00 + i) for i in range(200))


def test_split_chunks_splits_an_oversized_piece_evenly_at_exact_positions():
    sizes_by_length = {22: [22], 23: [12, 11], 44: [22, 22], 45: [15, 15, 15], 66: [22, 22, 22], 67: [17, 17, 17, 16]}
    for length, sizes in sizes_by_length.items():
        text = DISTINCT[:length]
        cuts = [sum(sizes[:i]) for i in range(len(sizes) + 1)]
        assert sub.split_chunks(text, maxlen=22) == [text[a:b] for a, b in zip(cuts, cuts[1:])], length


def test_split_chunks_even_split_spreads_the_remainder_over_the_first_parts():
    assert [len(c) for c in sub.split_chunks(DISTINCT[:50], maxlen=22)] == [17, 17, 16]
    assert [len(c) for c in sub.split_chunks(DISTINCT[:13], maxlen=4)] == [4, 3, 3, 3]
    assert [len(c) for c in sub.split_chunks(DISTINCT[:10], maxlen=4)] == [4, 3, 3]
    assert [len(c) for c in sub.split_chunks(DISTINCT[:9], maxlen=4)] == [3, 3, 3]


def test_split_chunks_never_exceeds_maxlen_and_keeps_every_character():
    for maxlen in (3, 4, 10, 22):
        for length in range(1, 200):
            text = DISTINCT[:length]
            chunks = sub.split_chunks(text, maxlen)
            sizes = [len(c) for c in chunks]
            assert "".join(chunks) == text, (maxlen, length)
            assert max(sizes) <= maxlen and max(sizes) - min(sizes) <= 1, (maxlen, length, sizes)
            assert len(chunks) == -(-length // maxlen), (maxlen, length)


def test_split_chunks_mixed_punctuation_text_never_exceeds_maxlen():
    segments = [3, 30, 1, 22, 23, 5, 44, 2]
    text = "".join(DISTINCT[i:i + n] + ("，" if i % 2 else "。") for i, n in enumerate(segments))
    for maxlen in (10, 22):
        chunks = sub.split_chunks(text, maxlen)
        assert "".join(chunks) == text and all(0 < len(c) <= maxlen for c in chunks), maxlen


def test_split_chunks_strips_whitespace_around_an_oversized_piece():
    text = DISTINCT[:23]
    assert sub.split_chunks("  " + text + "\n ", maxlen=22) == [text[:12], text[12:]]


def test_split_chunks_oversized_piece_is_split_alone_and_its_neighbours_stay_separate():
    short, long_ = "甲" * 4 + "，", DISTINCT[:30] + "。"
    assert sub.split_chunks(short + long_ + short, maxlen=22) == [short, long_[:16], long_[16:], short]
    assert sub.split_chunks(long_, maxlen=22) == [long_[:16], long_[16:]]
    assert sub.split_chunks(short + long_, maxlen=22) == [short, long_[:16], long_[16:]]
    assert sub.split_chunks(long_ + short, maxlen=22) == [long_[:16], long_[16:], short]


def test_build_cues_split_an_oversized_clause_into_lines_of_at_most_maxlen():
    cues = sub.build_cues([unit(DISTINCT[:50], 10.0, 20.0)])
    assert [c[2] for c in cues] == [DISTINCT[:17], DISTINCT[17:34], DISTINCT[34:50]]
    assert cues[0][0] == 10.0 and abs(cues[-1][1] - 20.0) < 1e-9
    assert [round(c[1] - c[0], 9) for c in cues] == [3.4, 3.4, 3.2]
    assert all(abs(a[1] - b[0]) < 1e-9 for a, b in zip(cues, cues[1:]))


def test_build_cues_split_a_unit_in_proportion_to_chunk_length():
    cues = sub.build_cues([unit("甲乙丙，丁。", 10.0, 22.0)], maxlen=4)
    assert cues == [(10.0, 18.0, "甲乙丙，"), (18.0, 22.0, "丁。")]


def test_build_cues_pass_maxlen_to_the_splitter_and_default_to_22():
    one = unit("甲乙丙丁戊己庚辛壬，子丑寅卯辰巳午未申酉戌。", 0.0, 4.0)
    assert [c[2] for c in sub.build_cues([one], maxlen=12)] == ["甲乙丙丁戊己庚辛壬，", "子丑寅卯辰巳午未申酉戌。"]
    assert [c[2] for c in sub.build_cues([one])] == [one["text"]]


def test_build_cues_keep_unit_order_and_skip_blank_units():
    units = [unit("甲乙。", 0.0, 1.0), unit("  ", 1.0, 2.0), unit("丙丁。", 2.0, 3.0)]
    assert sub.build_cues(units) == [(0.0, 1.0, "甲乙。"), (2.0, 3.0, "丙丁。")]
    assert sub.build_cues([]) == []


def test_build_cues_drop_units_with_unusable_times_or_shape_and_keep_the_rest():
    good = unit("好。", 0.0, 1.0)
    bad_units = [
        unit("零長。", 5.0, 5.0),
        unit("倒轉。", 6.0, 5.0),
        unit("非數。", float("nan"), 1.0),
        unit("無限。", 0.0, float("inf")),
        unit("負起點。", -1.0, 1.0),
        unit("太大。", 0.0, 1e308),
        unit("巨整數。", 0, 10 ** 400),
        unit("超過兩位時。", 0.0, 360_000.0),
        unit("字串。", "0", "x"),
        unit("空值。", None, 1.0),
        unit(None, 0.0, 1.0),
        {"id": 3, "section": 1, "text": "缺欄位。"},
        None,
        "junk",
    ]
    for bad in bad_units:
        assert sub.build_cues([bad, good]) == [(0.0, 1.0, "好。")], bad


def test_build_cues_keep_a_unit_ending_exactly_at_the_srt_limit():
    assert sub.build_cues([unit("末。", 0.0, 359_999.0)]) == [(0.0, 359_999.0, "末。")]


def test_fmt_srt_time_rounds_to_milliseconds_and_pads_every_field():
    assert sub.fmt_srt_time(0.001) == "00:00:00,001"
    assert sub.fmt_srt_time(0.0004) == "00:00:00,000"
    assert sub.fmt_srt_time(9.009) == "00:00:09,009"
    assert sub.fmt_srt_time(59.999) == "00:00:59,999"
    assert sub.fmt_srt_time(3599.9996) == "01:00:00,000"
    assert sub.fmt_srt_time(36000.25) == "10:00:00,250"
    assert sub.fmt_srt_time(359_999.0) == "99:59:59,000"


def test_write_srt_accepts_a_string_path_and_writes_utf8(tmp_path):
    out = tmp_path / "s.srt"
    sub.write_srt([(0.0, 1.0, "你好")], str(out))
    assert "你好".encode("utf-8") in out.read_bytes()
    assert out.read_text(encoding="utf-8") == "1\n00:00:00,000 --> 00:00:01,000\n你好\n"


def test_write_srt_with_no_cues_writes_an_empty_file(tmp_path):
    out = tmp_path / "s.srt"
    sub.write_srt([], out)
    assert out.read_text(encoding="utf-8") == ""


def test_units_to_srt_end_to_end(tmp_path):
    units = [unit("甲乙丙，丁。", 10.0, 22.0), unit("戊己。", 22.0, 23.5)]
    out = tmp_path / "s.srt"
    sub.write_srt(sub.build_cues(units, maxlen=4), out)
    assert out.read_text(encoding="utf-8") == (
        "1\n00:00:10,000 --> 00:00:18,000\n甲乙丙，\n\n"
        "2\n00:00:18,000 --> 00:00:22,000\n丁。\n\n"
        "3\n00:00:22,000 --> 00:00:23,500\n戊己。\n")
