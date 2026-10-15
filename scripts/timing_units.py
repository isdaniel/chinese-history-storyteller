"""配音邊界事件 → 子句層級時間單位 (units) 與詞時間 (words)。

純函式,不依賴 Azure Speech SDK。事件格式 (時間單位為秒):
    {"type": "...Sentence" | "...Word" | "...Punctuation", "offset": float, "dur": float, "text": str}
"""
import re

CLEAN_RE = re.compile(r"[「」『』“”\"'（）()\s]")
SENTENCE_RE = re.compile(r"[^。！？]+[。！？]?")
TERMINATORS = "。！？"
PUNCT_ALL = "，、；：。！？"
CLAUSE_END = "，；：。！？"
MIN_CLAUSE_CHARS = 9
PROPORTIONAL = "char_proportional_FALLBACK"


def clean_text(text: str) -> str:
    """移除引號、括號與空白;TTS 輸入與事件對齊都使用清理後的文字。"""
    return CLEAN_RE.sub("", text)


def split_sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_RE.findall(clean_text(text)) if s]


def tts_text(narration: str) -> str:
    return "".join(split_sentences(narration))


def event_from_sdk(evt) -> dict:
    """把 Speech SDK 的 SpeechSynthesisWordBoundaryEventArgs 轉成純 dict (offset 單位 100ns)。"""
    duration = evt.duration
    dur = duration.total_seconds() if hasattr(duration, "total_seconds") else float(duration) / 1e7
    return {"type": str(evt.boundary_type), "offset": evt.audio_offset / 1e7, "dur": dur, "text": evt.text}


def sentence_starts(events: list[dict], sentences: list[str], audio_len: float) -> tuple[list[float], str]:
    """每句在該段音檔內的起點 (秒) 與使用的方法。"""
    n = len(sentences)
    if n == 0:
        return [], "empty"
    sentence_events = [e for e in events if "Sentence" in e["type"]]
    if len(sentence_events) == n:
        starts = [e["offset"] for e in sentence_events]
        starts[0] = 0.0
        return starts, "sentence_boundary_events"
    enders = [e for e in events if "Punctuation" in e["type"] and e["text"] in TERMINATORS]
    if len(enders) == n:
        return [0.0] + [e["offset"] + e["dur"] for e in enders[:-1]], "terminal_punctuation_events"
    total = sum(len(s) for s in sentences) or 1
    starts, cursor = [], 0.0
    for sentence in sentences:
        starts.append(cursor)
        cursor += audio_len * len(sentence) / total
    return starts, PROPORTIONAL


def _merge_short(clauses: list[list]) -> list[list]:
    merged, i = [], 0
    while i < len(clauses):
        text, end = clauses[i]
        while len(text) < MIN_CLAUSE_CHARS and i + 1 < len(clauses):
            i += 1
            text += clauses[i][0]
            end = clauses[i][1]
        merged.append([text, end])
        i += 1
    if len(merged) > 1 and len(merged[-1][0]) < MIN_CLAUSE_CHARS:
        merged[-2][0] += merged[-1][0]
        merged[-2][1] = merged[-1][1]
        merged.pop()
    return merged


def section_units(sentences, events, audio_len, section, offset, first_id):
    """一段旁白 → (units, words, report);時間皆為全集絕對秒數 (已加上 offset)。"""
    starts, method = sentence_starts(events, sentences, audio_len)
    ends = [starts[i + 1] if i + 1 < len(sentences) else audio_len for i in range(len(sentences))]
    punct_events = [e for e in events if "Punctuation" in e["type"]]
    text = "".join(sentences)
    clause_level = method != PROPORTIONAL and [e["text"] for e in punct_events] == [
        c for c in text if c in PUNCT_ALL
    ]
    units, uid, pi = [], first_id, 0
    for i, sentence in enumerate(sentences):
        if not clause_level:
            clauses = [[sentence, ends[i]]]
        else:
            clauses, current = [], ""
            for k, ch in enumerate(sentence):
                current += ch
                if ch in PUNCT_ALL:
                    event = punct_events[pi]
                    pi += 1
                    if ch in CLAUSE_END:
                        is_last = k == len(sentence) - 1
                        clauses.append([current, ends[i] if is_last else event["offset"] + event["dur"]])
                        current = ""
            if current:
                clauses.append([current, ends[i]])
            clauses = _merge_short(clauses)
        start = starts[i]
        for clause_text, clause_end in clauses:
            units.append({"id": uid, "section": section, "text": clause_text,
                          "start": round(offset + start, 3), "end": round(offset + clause_end, 3)})
            uid += 1
            start = clause_end
    words = [{"text": e["text"], "start": round(offset + e["offset"], 3), "section": section}
             for e in events if "Word" in e["type"]]
    report = {"section": section, "method": method, "clause_level": clause_level,
              "sentences": len(sentences), "units": len(units), "audio_sec": round(audio_len, 3)}
    return units, words, report


def build_timings(parts: list[dict]) -> dict:
    """parts: [{section_id, narration, audio_len, events}] → timings.json 的內容。

    sections / total 的結構與舊版完全相同 (upload_youtube 章節時間軸依賴它),units / words / timing_report 為新增欄位。
    """
    sections, units, words, report = [], [], [], []
    cursor = 0.0
    for part in parts:
        sentences = split_sentences(part["narration"])
        u, w, r = section_units(sentences, part["events"], part["audio_len"], part["section_id"],
                                cursor, len(units))
        units.extend(u)
        words.extend(w)
        report.append(r)
        sections.append({"section_id": part["section_id"], "start": cursor, "end": cursor + part["audio_len"],
                         "duration": part["audio_len"], "narration": part["narration"]})
        cursor += part["audio_len"]
    return {"sections": sections, "total": cursor, "units": units, "words": words, "timing_report": report}
