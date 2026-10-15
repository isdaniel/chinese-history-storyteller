"""鏡頭切分:把相鄰的子句單位併成鏡頭 (beats)。純程式、可重現,LLM 不參與。"""

BOUNDS = ((3.5, 10.5), (3.0, 11.5), (2.5, 12.5))
TARGET_SEC = 6.5
MID_SENTENCE_PENALTY = 6.0
SENTENCE_END = "。！？"


class BeatSegmentationError(RuntimeError):
    pass


def _segment(units, lo, hi, target):
    n = len(units)
    inf = float("inf")
    best = [inf] * (n + 1)
    prev = [-1] * (n + 1)
    best[0] = 0.0
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            if units[i]["section"] != units[j - 1]["section"]:
                break
            duration = units[j - 1]["end"] - units[i]["start"]
            if duration > hi:
                break
            if duration < lo or best[i] == inf:
                continue
            cost = (duration - target) ** 2
            if j < n and units[j - 1]["text"][-1] not in SENTENCE_END:
                cost += MID_SENTENCE_PENALTY
            if best[i] + cost < best[j]:
                best[j], prev[j] = best[i] + cost, i
    if best[n] == inf:
        return None
    cuts, j = [], n
    while j > 0:
        cuts.append((prev[j], j))
        j = prev[j]
    return [list(range(a, b)) for a, b in reversed(cuts)]


def segment_beats(units, target=TARGET_SEC):
    """回傳 (beats, bounds):beats 是由 unit id 組成的連續區段;無解時逐步放寬長度範圍。"""
    if not units:
        return [], BOUNDS[0]
    for lo, hi in BOUNDS:
        beats = _segment(units, lo, hi, target)
        if beats is not None:
            return beats, (lo, hi)
    raise BeatSegmentationError("no beat segmentation satisfies the duration bounds")


def one_beat_per_unit(units):
    """切分無解時的最後手段:每個單位自成一個鏡頭。"""
    return [[i] for i in range(len(units))]
