"""
logger_factory.py — UnivAgent 시스템 이벤트 JSONL 로거
=======================================================
system_events.jsonl 에 구조화된 이벤트를 기록합니다.
devops_reporter.py / e2e_tester.py 등 모든 스크립트가 공유합니다.
"""

import asyncio
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_루트        = Path(__file__).resolve().parent.parent
_EVENT_LOG   = _루트 / "data" / "logs" / "system_events.jsonl"
_WRITE_LOCK  = threading.Lock()


# ─────────────────────────────────────────────────────────────
# 동기 로거
# ─────────────────────────────────────────────────────────────

def log_event(
    event_type: str,
    source: str,
    message: str,
    level: str = "INFO",
    extra: dict | None = None,
) -> None:
    """system_events.jsonl 에 이벤트를 JSONL 한 줄로 기록합니다 (동기)."""
    entry: dict = {
        "ts":         datetime.now().isoformat(timespec="seconds"),
        "level":      level.upper(),
        "event_type": event_type,
        "source":     source,
        "message":    message[:500],
    }
    if extra:
        entry.update(extra)
    try:
        _EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            with open(_EVENT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[logger_factory] system_events.jsonl 기록 실패: {e}")


def log_error(
    event_type: str,
    source: str,
    error: Exception,
    extra: dict | None = None,
) -> None:
    """에러를 system_events.jsonl 에 JSONL 형식으로 기록합니다 (동기)."""
    import traceback as _tb
    err_extra: dict = {
        "error_type": type(error).__name__,
        "error_msg":  str(error)[:500],
        "traceback":  _tb.format_exc()[:2000],
    }
    if extra:
        err_extra.update(extra)
    log_event(event_type, source, str(error)[:200], level="ERROR", extra=err_extra)


# ─────────────────────────────────────────────────────────────
# 비동기 래퍼 (이벤트 루프 블로킹 방지)
# ─────────────────────────────────────────────────────────────

async def async_log_event(
    event_type: str,
    source: str,
    message: str,
    level: str = "INFO",
    extra: dict | None = None,
) -> None:
    """이벤트 루프를 블로킹하지 않고 system_events.jsonl 에 기록합니다."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: log_event(event_type, source, message, level, extra),
    )


async def async_log_error(
    event_type: str,
    source: str,
    error: Exception,
    extra: dict | None = None,
) -> None:
    """이벤트 루프를 블로킹하지 않고 에러를 system_events.jsonl 에 기록합니다."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: log_error(event_type, source, error, extra),
    )


# ─────────────────────────────────────────────────────────────
# devops_reporter 연동: system_events.jsonl 최근 N줄 읽기
# ─────────────────────────────────────────────────────────────

def read_recent_events(
    n: int = 100,
    event_type_filter: str | None = None,
) -> list[dict]:
    """system_events.jsonl 에서 최근 N개 이벤트를 읽어 반환합니다."""
    if not _EVENT_LOG.exists():
        return []
    events: list[dict] = []
    try:
        with open(_EVENT_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if event_type_filter and obj.get("event_type") != event_type_filter:
                        continue
                    events.append(obj)
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        logger.warning(f"[logger_factory] system_events.jsonl 읽기 실패: {e}")
    return events[-n:]
