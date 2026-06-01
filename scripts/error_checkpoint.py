"""
error_checkpoint.py — 에러 분석 기준 시각 (재시작/처리 완료 이후만 LLM 분석)

data/fix_error/error_analysis_checkpoint.json
  since  : 이 시각 이후 발생한 ERROR만 대시보드·자동프롬프트가 집계
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_루트 = Path(__file__).resolve().parent.parent
CHECKPOINT_FILE = _루트 / "data" / "fix_error" / "error_analysis_checkpoint.json"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_FILE.exists():
        return {}
    try:
        data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[Checkpoint] 로드 실패: {e}")
        return {}


def get_since_datetime() -> datetime | None:
    raw = load_checkpoint().get("since")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def mark_restart(reason: str = "restart_agent.sh") -> Path:
    """시스템 재시작 시 — 이후 쌓이는 에러만 분석 대상."""
    return write_checkpoint(_now_iso(), reason)


def mark_resolved(reason: str = "dashboard_resolved") -> Path:
    """코드 수정·분석 완료 후 — 지금까지 에러는 처리된 것으로 간주."""
    return write_checkpoint(_now_iso(), reason)


def write_checkpoint(since_iso: str, reason: str) -> Path:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "since": since_iso,
        "reason": reason,
        "updated_at": _now_iso(),
    }
    CHECKPOINT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"[Checkpoint] since={since_iso} ({reason})")
    return CHECKPOINT_FILE
