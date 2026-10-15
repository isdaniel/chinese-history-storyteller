"""字幕:把子句單位切成單行字幕,並輸出 SRT。"""
import re
from pathlib import Path

CHUNK_MAX = 22
MAX_SRT_SEC = 359_999.0  # SRT 時間的小時欄只有兩位 (99:59:59)
_SPLIT = re.compile(r"(?<=[，、；：。！？,;:!?])")


def split_chunks(text: str, maxlen: int = CHUNK_MAX) -> list[str]:
    """依標點把文字切成每行最多 maxlen 字的字幕;沒有標點可切的超長片段平均切開。"""
    pieces = [p for p in (s.strip() for s in _SPLIT.split(text)) if p]
    chunks, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) > maxlen:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    out = []
    for chunk in chunks:
        parts = -(-len(chunk) // maxlen)
        base, extra = divmod(len(chunk), parts)
        start = 0
        for k in range(parts):
            end = start + base + (1 if k < extra else 0)
            out.append(chunk[start:end])
            start = end
    return out


def build_cues(units: list[dict], maxlen: int = CHUNK_MAX) -> list[tuple[float, float, str]]:
    """每個單位的真實起訖時間,依字數比例分給該單位切出的各行字幕。"""
    cues = []
    for unit in units:
        try:
            start, end, text = float(unit["start"]), float(unit["end"]), unit["text"]
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not (isinstance(text, str) and 0 <= start < end <= MAX_SRT_SEC):
            continue
        chunks = split_chunks(text, maxlen)
        total = sum(len(c) for c in chunks)
        cursor = start
        for chunk in chunks:
            span = (end - start) * len(chunk) / total
            cues.append((cursor, cursor + span, chunk))
            cursor += span
    return cues


def fmt_srt_time(sec: float) -> str:
    millis = int(round(sec * 1000))
    h, rest = divmod(millis, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues, out_path: Path) -> None:
    lines = [f"{i}\n{fmt_srt_time(a)} --> {fmt_srt_time(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, 1)]
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
