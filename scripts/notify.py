"""Step 6 (optional): 發布完成後通知 Discord。"""
import math
import os
import sys

from common import get_episode_dir, load_json, notify_discord, setup_logging

log = setup_logging("notify")

DEFAULT_IMAGE_COST_USD = 0.06   # 估計值 (依 gpt-image-1 計價推算);上線後以 Cost Management 核對
UNKNOWN_TITLE = "(未知標題)"
NOT_UPLOADED = "(未上傳)"
PODCAST_UPDATED = "已更新 RSS feed"
PODCAST_SKIPPED = "未發布 (測試執行)"
HEADER_PUBLISHED = " **新集數已發布**"
HEADER_TEST_RUN = " **測試執行完成 (未發布 Podcast)**"
STOP_REASONS = {"consecutive_failures": "連續失敗", "time_limit": "逾時"}


def _read_object(path):
    """讀 JSON 物件;檔案不存在、壞掉、或不是物件都回傳 None (通知不能因為報告有問題而失敗)。"""
    if not path.exists():
        return None
    try:
        data = load_json(path)
    except (OSError, ValueError, RecursionError) as exc:
        log.warning("%s 無法讀取,略過 (%s)", path.name, exc)
        return None
    return data if isinstance(data, dict) else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _seconds(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return value if ok else None


def _text(value, default: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _unit_cost() -> float:
    raw = os.environ.get("IMAGE_UNIT_COST_USD", "").strip()
    try:
        cost = float(raw) if raw else DEFAULT_IMAGE_COST_USD
    except ValueError:
        cost = math.nan
    if not (math.isfinite(cost) and cost >= 0):
        log.warning("IMAGE_UNIT_COST_USD=%r 無效,改用預設 %s", raw, DEFAULT_IMAGE_COST_USD)
        return DEFAULT_IMAGE_COST_USD
    return cost


def _image_part(report: dict):
    generated, fallback = _count(report.get("generated")), _count(report.get("fallback"))
    if generated is None or fallback is None:
        return None
    count = generated + fallback   # skipped (已存在的圖) 這次沒有花錢,不計入
    return f"圖片 {count} 張 (估算 ${count * _unit_cost():.2f},備援 {fallback})"


def _image_stop_part(report: dict):
    reason = report.get("stopped_early")
    reason = reason.strip() if isinstance(reason, str) else ""
    return f"產圖中止 ({STOP_REASONS.get(reason, reason)})" if reason else None


def _plan_part(report: dict):
    sections = report.get("sections")
    if not isinstance(sections, list):
        return None
    sections = [s for s in sections if isinstance(s, dict)]
    fallback = sum(1 for s in sections if s.get("fallback") is True)
    return f"規劃保底 {fallback}/{len(sections)} 段" if fallback else None


def _ground_part(report: dict):
    missing = report.get("missing")
    return f"聚焦失敗 {len(missing)} 鏡頭" if isinstance(missing, list) and missing else None


def _quality_problems(report: dict) -> list[str]:
    """渲染成功但品質有疑慮的項目;報告裡沒有該欄位或型別不對就當作沒問題 (只有明確的 False / 正整數才算)。"""
    problems = [text for key, text in (("duration_ok", "長度異常"), ("frames_ok", "影格數不符"),
                                       ("contact_sheet", "縮圖總表失敗")) if report.get(key) is False]
    quality = report.get("quality")
    if isinstance(quality, dict):
        for key, label in (("black_segments", "黑畫面"), ("freeze_segments", "靜止")):
            count = _count(quality.get(key))
            if count:
                problems.append(f"{label} {count} 段")
    return problems


def _video_part(report: dict):
    pieces = []
    render_sec = _seconds(report.get("render_sec"))
    if render_sec is not None:
        pieces.append(f"渲染 {render_sec:.0f}s")
    fallback_beats, beats = report.get("fallback_beats"), _count(report.get("beats"))
    if isinstance(fallback_beats, list) and beats is not None:
        pieces.append(f"備援鏡頭 {len(fallback_beats)}/{beats}")
    pieces.extend(_quality_problems(report))
    return ",".join(pieces) or None


def summarize_run(ep_dir) -> str:
    """本集製作統計 (圖片張數與估算成本與中止原因、規劃保底段數、聚焦失敗鏡頭數、渲染時間、備援鏡頭數、渲染品質疑慮);
    沒有報告或報告壞掉時略過該項,絕不拋例外。"""
    parts = []
    for name, describers in (("image_report.json", (_image_part, _image_stop_part)),
                             ("plan_report.json", (_plan_part,)), ("ground_report.json", (_ground_part,)),
                             ("video_report.json", (_video_part,))):
        try:
            report = _read_object(ep_dir / name)
        except Exception as exc:
            log.warning("%s 讀取失敗,略過 (%s: %s)", name, type(exc).__name__, exc)
            continue
        if report is None:
            continue
        for describe in describers:
            try:
                part = describe(report)
            except Exception as exc:   # 通知不能因為任何一份報告而失敗 (例如超大整數轉 float 會 OverflowError)
                log.warning("%s 摘要失敗,略過 (%s: %s)", name, type(exc).__name__, exc)
                continue
            if part:
                parts.append(part)
    return "；".join(parts)


def _podcast_published() -> bool:
    """workflow 在手動測試且取消勾選 publish_podcast 時傳 PUBLISH_PODCAST=false;其他情況 (含未設定) 都視為已發布。"""
    return os.environ.get("PUBLISH_PODCAST", "").strip().lower() != "false"


def build_message(title: str, yt_url: str, summary: str) -> str:
    published = _podcast_published()
    lines = [
        HEADER_PUBLISHED if published else HEADER_TEST_RUN,
        f" {title}",
        f" YouTube: {yt_url}",
        f" Podcast: {PODCAST_UPDATED if published else PODCAST_SKIPPED}",
    ]
    if summary:
        lines.append(f" 製作統計: {summary}")
    return "\n".join(lines)


def main(episode_id: int) -> int:
    ep_dir = get_episode_dir(episode_id)
    script = _read_object(ep_dir / "script.json")
    if script is None:
        log.warning("script.json 讀不到,標題改用 %s", UNKNOWN_TITLE)
    info = _read_object(ep_dir / "youtube_info.json") or {}
    msg = build_message(_text((script or {}).get("title"), UNKNOWN_TITLE), _text(info.get("url"), NOT_UPLOADED),
                        summarize_run(ep_dir))
    notify_discord(msg)
    log.info(msg)
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
