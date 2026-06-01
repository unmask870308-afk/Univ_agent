"""
dashboard_utils.py — 대시보드 핵심 지표 수집 유틸리티
======================================================
web_dashboard.py 에서 호출하는 데이터 파싱 및 상태 판단 함수 모음.
DB 직접 임포트는 지양하고 파일 파싱 + 선택적 db_manager 호출만 수행합니다.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 경로 상수
# ─────────────────────────────────────────────────────────────

_SYSTEM_EVENTS  = _ROOT / "data" / "logs" / "system_events.jsonl"
_AI_ERRORS      = _ROOT / "data" / "logs" / "Backup_log" / "ai_runtime_errors.json"
_AI_ERRORS_ALT  = _ROOT / "data" / "logs" / "ai_runtime_errors.json"
_GEMINI_ERR_LOG = _ROOT / "data" / "fix_error" / "gemini_api_errors.log"
_BACKUP_DIR     = _ROOT / "data" / "backups"          # daily_backup.py 실제 저장 위치
_GOLDEN_JSONL   = _ROOT / "data" / "golden_dataset.jsonl"
_MODELFILES_DIR = _ROOT / "data" / "training" / "modelfiles"


# ─────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────

def _load_ai_errors() -> list[dict]:
    """ai_runtime_errors.json 을 파싱해 리스트로 반환 (실패 시 [])."""
    for p in (_AI_ERRORS, _AI_ERRORS_ALT):
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return [data]
            except Exception:
                pass
    return []


def _load_system_events() -> list[dict]:
    """system_events.jsonl 을 파싱해 리스트로 반환 (실패 시 [])."""
    if not _SYSTEM_EVENTS.exists():
        return []
    events = []
    try:
        for line in _SYSTEM_EVENTS.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return events


# ─────────────────────────────────────────────────────────────
# 1. 유저 퍼널 (Funnel) 데이터
# ─────────────────────────────────────────────────────────────

def get_user_funnel() -> dict:
    """
    3단계 퍼널 지표를 반환합니다.

    단계 1: 프로필 시작 (students 테이블 전체)
    단계 2: 학과/성적 입력 완료 (target_major 또는 grade_raw 가 채워진 학생)
    단계 3: 처방전 발급 성공 (system_events PDF_GOLDEN_SAVED 이벤트 기준)

    Returns
    -------
    {
        "stage1": int,   # 프로필 시작 수
        "stage2": int,   # 학과/성적 입력 수
        "stage3": int,   # 처방전 발급 수
        "drop12": float, # 1→2 이탈률 (%)
        "drop23": float, # 2→3 이탈률 (%)
    }
    """
    stage1 = stage2 = stage3 = 0

    # ── DB 조회 ───────────────────────────────────────────────
    try:
        import db_manager as _db
        _db.init_db()
        stage1 = _db.DB().execute("SELECT COUNT(*) FROM students").fetchone()[0] or 0
        stage2 = _db.DB().execute(
            "SELECT COUNT(*) FROM students "
            "WHERE (target_major != '' AND target_major IS NOT NULL) "
            "   OR (grade_raw    != '' AND grade_raw    IS NOT NULL)"
        ).fetchone()[0] or 0
    except Exception as e:
        logger.warning(f"[FunnelUtil] DB 조회 실패: {e}")

    # ── system_events.jsonl 에서 PDF 발급 수 집계 ─────────────
    try:
        events = _load_system_events()
        # 유일 PDF 경로 집합으로 중복 제거
        pdf_paths = {
            ev.get("output_path") or ev.get("file_path", "")
            for ev in events
            if ev.get("event_type") in ("PDF_GOLDEN_SAVED",)
            and (ev.get("level") or ev.get("level", "")).upper() != "ERROR"
        }
        stage3 = len([p for p in pdf_paths if p])
    except Exception as e:
        logger.warning(f"[FunnelUtil] system_events 파싱 실패: {e}")

    # ── 이탈률 계산 ───────────────────────────────────────────
    def _drop(a: int, b: int) -> float:
        if a <= 0:
            return 0.0
        return round(max(0.0, (1 - b / a) * 100), 1)

    return {
        "stage1": max(stage1, 1),   # 0 방어 (분모 0 방지)
        "stage2": stage2,
        "stage3": stage3,
        "drop12": _drop(stage1, stage2),
        "drop23": _drop(stage2, stage3),
    }


# ─────────────────────────────────────────────────────────────
# 2. AI 엔진 상태 체크
# ─────────────────────────────────────────────────────────────

def get_engine_status() -> dict[str, str]:
    """
    최근 1시간 내 ai_runtime_errors.json 에 429 에러가 있으면 해당 엔진을 '잠금' 으로 표시.
    Ollama 는 HTTP 핑으로 직접 확인.

    Returns
    -------
    {
        "gemini": "🟢 Online" | "🔴 Rate Limited",
        "groq":   "🟢 Online" | "🔴 Rate Limited",
        "ollama": "🟢 Online" | "🔴 Offline",
        "gemini_last_error": str | None,
        "groq_last_error":   str | None,
    }
    """
    cutoff = datetime.now() - timedelta(hours=1)
    gemini_locked = False
    groq_locked   = False
    gemini_last   = None
    groq_last     = None

    errors = _load_ai_errors()
    for rec in errors:
        ts_str = rec.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str[:19])
        except Exception:
            continue
        if ts < cutoff:
            continue

        msg = (rec.get("error_message") or "").lower()
        ctx = str(rec.get("context") or "").lower()

        is_429 = "429" in msg or "resource_exhausted" in msg or "quota" in msg
        if not is_429:
            continue

        if "groq" in msg or "groq" in ctx:
            groq_locked = True
            groq_last   = ts_str
        else:
            # 기본적으로 Gemini (google generativelanguage)
            gemini_locked = True
            gemini_last   = ts_str

    # ── Ollama 핑 ─────────────────────────────────────────────
    ollama_ok = False
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3)
        ollama_ok = True
    except Exception:
        pass

    return {
        "gemini":           "🔴 Rate Limited" if gemini_locked else "🟢 Online",
        "groq":             "🔴 Rate Limited" if groq_locked   else "🟢 Online",
        "ollama":           "🟢 Online"        if ollama_ok    else "🔴 Offline",
        "gemini_last_error": gemini_last,
        "groq_last_error":   groq_last,
    }


def get_engine_daily_counts() -> dict[str, int]:
    """
    오늘 날짜의 system_metrics 에서 엔진별 생성 건수를 조회합니다.
    없으면 0을 반환합니다.
    """
    counts = {"gemini": 0, "groq": 0, "ollama": 0}
    try:
        import db_manager as _db
        today = datetime.now().strftime("%Y-%m-%d")
        row = _db.DB().execute(
            "SELECT gemini_daily_tokens, groq_daily_tokens FROM system_metrics "
            "WHERE date_str = ? ORDER BY id DESC LIMIT 1",
            (today,),
        ).fetchone()
        if row:
            # token 수를 사용량 비율로 환산 (실제 건수 대용)
            counts["gemini"] = int((row["gemini_daily_tokens"] or 0) // 1000)
            counts["groq"]   = int((row["groq_daily_tokens"]   or 0) // 1000)
    except Exception as e:
        logger.debug(f"[EngineUtil] system_metrics 조회 실패: {e}")

    # system_events 에서 오늘 생성된 PDF 이벤트 카운트 (대용 지표)
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        events    = _load_system_events()
        pdf_today = sum(
            1 for ev in events
            if ev.get("event_type") == "PDF_GOLDEN_SAVED"
            and (ev.get("ts") or "").startswith(today_str)
        )
        counts["_pdf_today"] = pdf_today
    except Exception:
        counts["_pdf_today"] = 0

    return counts


# ─────────────────────────────────────────────────────────────
# 3. 야간 빌드 무결성 체크
# ─────────────────────────────────────────────────────────────

def get_nightly_build_status() -> dict:
    """
    야간 빌드 파이프라인 3개 항목의 오늘 완료 여부를 반환합니다.

    Returns
    -------
    {
        "backup_ok":        bool,
        "backup_path":      str | None,  # 가장 최신 백업 파일 이름
        "golden_ok":        bool,
        "golden_count_today": int,        # 오늘 신규 golden_dataset 건수
        "modelfile_ok":     bool,
        "modelfile_path":   str | None,  # 오늘 생성된 Modelfile 이름
        "backup_age_h":     float | None, # 최신 백업 경과 시간 (시간)
    }
    """
    today     = datetime.now().strftime("%Y%m%d")
    today_iso = datetime.now().strftime("%Y-%m-%d")
    now       = datetime.now()

    # ── 백업 파일 확인 ────────────────────────────────────────
    backup_ok   = False
    backup_path = None
    backup_age  = None
    if _BACKUP_DIR.exists():
        backups = sorted(_BACKUP_DIR.glob(f"UnivAgent_Backup_{today}*.tar.gz"))
        if backups:
            backup_ok   = True
            backup_path = backups[-1].name
            mtime       = backups[-1].stat().st_mtime
            backup_age  = (now.timestamp() - mtime) / 3600
    if not backup_ok:
        # 24시간 내 가장 최신 백업이라도 있는지 확인
        if _BACKUP_DIR.exists():
            all_backups = sorted(_BACKUP_DIR.glob("UnivAgent_Backup_*.tar.gz"))
            if all_backups:
                newest = all_backups[-1]
                age_h  = (now.timestamp() - newest.stat().st_mtime) / 3600
                if age_h <= 24:
                    backup_ok   = True
                    backup_path = newest.name
                    backup_age  = age_h

    # ── Golden Dataset 오늘 신규 건수 ─────────────────────────
    golden_ok          = False
    golden_count_today = 0
    try:
        import db_manager as _db
        golden_count_today = _db.DB().execute(
            "SELECT COUNT(*) FROM golden_dataset WHERE created_at LIKE ?",
            (f"{today_iso}%",),
        ).fetchone()[0] or 0
        golden_ok = golden_count_today > 0
    except Exception:
        pass
    # JSONL 파일 수정 일자 fallback
    if not golden_ok and _GOLDEN_JSONL.exists():
        mtime_date = datetime.fromtimestamp(_GOLDEN_JSONL.stat().st_mtime).strftime("%Y-%m-%d")
        if mtime_date == today_iso:
            golden_ok = True

    # ── Modelfile 오늘 생성 여부 ──────────────────────────────
    modelfile_ok   = False
    modelfile_path = None
    if _MODELFILES_DIR.exists():
        todays = sorted(
            [f for f in _MODELFILES_DIR.iterdir()
             if f.name.startswith(f"Modelfile_{today}")],
        )
        if todays:
            modelfile_ok   = True
            modelfile_path = todays[-1].name

    return {
        "backup_ok":          backup_ok,
        "backup_path":        backup_path,
        "golden_ok":          golden_ok,
        "golden_count_today": golden_count_today,
        "modelfile_ok":       modelfile_ok,
        "modelfile_path":     modelfile_path,
        "backup_age_h":       round(backup_age, 1) if backup_age is not None else None,
    }
