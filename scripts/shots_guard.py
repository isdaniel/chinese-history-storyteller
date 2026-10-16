"""shots 階段的重試與降級。

規劃 (plan)、產圖 (images)、聚焦 (ground) 三個階段各自重試;重試用完仍失敗時:
- plan / images (fatal):寫 `shots_degraded.json` 並發 Discord;之後的階段看到這個標記就不再跑分鏡流程,
  make_video 直接組裝舊的圖片輪播,所以排程不會因為分鏡階段出錯而沒有影片。
- ground (非 fatal):只發 Discord,鏡頭維持置中,影片照常渲染。
SHOTS_STRICT=1 時 (本機 runner 預設) 重試用完會直接拋出最後一個例外,不降級,方便驗證時看到問題。

時間保護 (工作逾時是 180 分鐘,掛住的呼叫不會自己丟例外):
- 每次嘗試有時限 (STAGE_MAX_SEC),用 SIGALRM 中斷,只在 POSIX 的主執行緒有效 (GitHub Actions 是 Linux);
- 「慢失敗」(超時,或一次嘗試跑了 RETRY_MAX_ATTEMPT_SEC 秒以上才失敗) 不再重試,直接降級,
  避免長階段一再重跑把後面降級用的時間吃光。
"""
import contextlib
import json
import os
import signal
import threading
import time
from pathlib import Path
from time import monotonic as _monotonic

from common import notify_discord, setup_logging

log = setup_logging("shots_guard")

MARKER = "shots_degraded.json"
DEFAULT_ATTEMPTS = 3
MAX_ATTEMPTS = 5
RETRY_BASE_SEC = 30
RETRY_MAX_ATTEMPT_SEC = 600
REASON_CHARS = 500
STAGE_NAMES = {"plan": "規劃", "images": "產圖", "ground": "聚焦", "render": "渲染"}
STAGE_MAX_SEC = {"plan": 1800, "images": 5400, "ground": 900}
_TRUE = {"1", "true", "yes", "on"}


class StageTimeout(TimeoutError):
    """單次嘗試超過時限。"""


def strict() -> bool:
    return os.environ.get("SHOTS_STRICT", "").strip().lower() in _TRUE


def attempts() -> int:
    try:
        value = int(os.environ.get("SHOTS_STAGE_ATTEMPTS", "").strip())
    except ValueError:
        return DEFAULT_ATTEMPTS
    return min(value, MAX_ATTEMPTS) if value >= 1 else DEFAULT_ATTEMPTS


def _reason(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc).strip() else type(exc).__name__
    return text if len(text) <= REASON_CHARS else text[:REASON_CHARS - 1] + "…"


def write_marker(ep_dir: Path, stage: str, error: str, tries: int) -> None:
    (ep_dir / MARKER).write_text(
        json.dumps({"stage": stage, "attempts": tries, "error": error[:REASON_CHARS]}, ensure_ascii=False),
        encoding="utf-8")


def read_marker(ep_dir: Path):
    """沒有標記回傳 None;標記存在但讀不懂也算降級 (stage = "unknown")。"""
    path = ep_dir / MARKER
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {"stage": "unknown"}
    if isinstance(data, dict) and isinstance(data.get("stage"), str):
        return data
    return {"stage": "unknown"}


def is_degraded(ep_dir: Path) -> bool:
    return read_marker(ep_dir) is not None


def clear_marker(ep_dir: Path) -> None:
    (ep_dir / MARKER).unlink(missing_ok=True)


def _alert(message: str) -> None:
    try:
        notify_discord(message)
    except Exception:
        log.warning("Discord 通知失敗,略過")


@contextlib.contextmanager
def _deadline(max_sec):
    """讓區塊內的程式在 max_sec 秒後收到 StageTimeout;不支援 SIGALRM 的環境 (Windows、非主執行緒) 不設時限。"""
    usable = (max_sec and hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")
              and threading.current_thread() is threading.main_thread())
    if not usable:
        yield
        return

    def on_alarm(signum, frame):
        raise StageTimeout(f"超過 {max_sec:g} 秒仍未完成")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, max_sec)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous if previous is not None else signal.SIG_DFL)


def run_stage(stage: str, episode_id: int, ep_dir: Path, fn, *, fatal: bool = True, max_sec=None) -> bool:
    """執行 fn,失敗時重試;回傳 True 表示成功,False 表示已降級 (strict 模式改為拋出最後的例外)。"""
    limit = max_sec if max_sec is not None else STAGE_MAX_SEC.get(stage)
    tries = attempts()
    made = 0
    last = None
    for k in range(1, tries + 1):
        started = _monotonic()
        try:
            with _deadline(limit):
                fn()
            return True
        except Exception as exc:
            last, made = exc, k
            elapsed = _monotonic() - started
            log.warning("分鏡階段 %s 第 %d/%d 次失敗 (%.0f 秒): %s", stage, k, tries, elapsed, _reason(exc),
                        exc_info=exc)
            if isinstance(exc, StageTimeout) or elapsed >= RETRY_MAX_ATTEMPT_SEC:
                if k < tries:
                    log.warning("分鏡階段 %s 這次失敗太慢,不再重試", stage)
                break
            if k < tries:
                time.sleep(RETRY_BASE_SEC * 2 ** (k - 1))
    if strict():
        raise last
    reason = _reason(last)
    name = STAGE_NAMES.get(stage, stage)
    if fatal:
        write_marker(ep_dir, stage, reason, made)
        log.error("分鏡階段 %s 嘗試 %d 次仍失敗,降級為圖片輪播: %s", stage, made, reason)
        _alert(f"Episode {episode_id}: 分鏡階段「{name}」嘗試 {made} 次仍失敗,已降級為圖片輪播。原因: {reason}")
    else:
        log.error("分鏡階段 %s 嘗試 %d 次仍失敗,略過該階段: %s", stage, made, reason)
        _alert(f"Episode {episode_id}: 分鏡階段「{name}」嘗試 {made} 次仍失敗,已略過 (鏡頭維持置中,影片照常產出)。原因: {reason}")
    return False
