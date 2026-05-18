"""共用 retry 工具:對外部 API 呼叫做指數退避 + jitter。

兩種使用方式:
  1. `with_backoff(fn, ...)`         — 包一段 callable,直接重試
  2. `@backoff_retry(...)`           — decorator 形式

設計原則:
  - 預設只重試 transient 錯誤 (429 / 5xx / 連線錯誤 / timeout)
  - 4xx (除 429) 不重試,因為改動 request 才會通過
  - honor `Retry-After` (若 exception 含 response 物件)
  - 指數退避 base * 2^attempt,夾在 [min, max],再加 0~jitter 秒
"""
import logging
import random
import time
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

log = logging.getLogger("retry")


def _extract_retry_after(exc: BaseException) -> float | None:
    """從 exception 的 response.headers 取 Retry-After (秒)。沒有就回 None。"""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    val = headers.get("Retry-After") or headers.get("retry-after")
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _is_retryable_status(exc: BaseException) -> bool:
    """檢查是否為可重試 HTTP 狀態 (429 / 5xx)。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    if status is None:
        # 無 status 資訊 (例如連線錯誤、timeout) → 視為可重試
        return True
    return status == 429 or 500 <= status < 600


def sleep_with_jitter(base_sec: float, jitter_sec: float = 0.0) -> float:
    """sleep base + uniform(0, jitter) 秒,回傳實際 sleep 時間。"""
    actual = base_sec + (random.uniform(0, jitter_sec) if jitter_sec > 0 else 0)
    time.sleep(actual)
    return actual


def with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int = 5,
    base_sec: float = 2.0,
    max_sec: float = 60.0,
    jitter_sec: float = 3.0,
    retry_on: Iterable[type] = (Exception,),
    op_name: str = "external API",
) -> T:
    """執行 fn,失敗時指數退避重試。

    Args:
        fn: 無參 callable;若需參數請用 lambda 或 functools.partial 包好
        max_attempts: 含第一次嘗試的總次數 (5 = 1 次主要 + 4 次重試)
        base_sec: 退避基底秒數
        max_sec: 單次退避上限,避免暴衝
        jitter_sec: 在每次退避上再加 0~jitter 秒隨機,避免多 client 對齊 burst
        retry_on: 哪些 exception 類別應該重試 (預設所有 Exception,再交給狀態判斷)
        op_name: log 用名稱

    Raises:
        最後一次嘗試的 exception。
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except tuple(retry_on) as exc:  # type: ignore[misc]
            last_exc = exc
            if not _is_retryable_status(exc):
                log.warning("[%s] non-retryable error, 直接拋: %s", op_name, exc)
                raise

            if attempt >= max_attempts:
                log.error(
                    "[%s] 已重試 %d 次仍失敗,放棄: %s",
                    op_name, max_attempts, exc,
                )
                raise

            retry_after = _extract_retry_after(exc)
            if retry_after is not None:
                wait = min(retry_after, max_sec) + random.uniform(0, jitter_sec)
            else:
                wait = min(base_sec * (2 ** (attempt - 1)), max_sec)
                wait += random.uniform(0, jitter_sec)

            log.warning(
                "[%s] 第 %d/%d 次失敗 (%s),退避 %.1fs 後重試",
                op_name, attempt, max_attempts, exc, wait,
            )
            time.sleep(wait)

    # 不應該到達這裡,但為 type checker 補一個 raise
    assert last_exc is not None
    raise last_exc


def backoff_retry(**kwargs):
    """Decorator 版本的 with_backoff。

    用法:
        @backoff_retry(max_attempts=5, op_name="discord webhook")
        def post_webhook(...):
            ...
    """
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kw) -> T:
            return with_backoff(lambda: fn(*args, **kw), **kwargs)
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco
