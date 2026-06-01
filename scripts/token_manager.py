"""
token_manager.py — LLM 라우터 & Knowledge Distillation Pipeline

엔진 정책 (2026-05):
  - code / gemini : 코드 수정·자가치유·에러 분석 (Gemini 전용)
  - crawl         : 크롤링·시딩·PDF 파싱 (Groq → Ollama, Gemini 제외) — 학습 모드 바이패스
  - (기본)        : Gemini → Groq → Ollama 3-Tier 폴백

집중 학습 모드(system_config.json training_mode=true) 활성 시:
  - 기본 라우팅(force_engine=None): Tier1/Tier2 차단, Tier3(Ollama)만 허용
  - force_engine="crawl" 및 "gemini"/"code" : 학습·자가치유용으로 바이패스 허용

Tier 1/2(Gemini/Groq) 성공 응답은 ShareGPT JSONL 형식으로
data/training/ollama_finetune_dataset.jsonl 에 비동기 저장됩니다.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import threading
import time as _time_mod
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 경로 ───────────────────────────────────────────────────────
_루트           = Path(__file__).resolve().parent.parent
_TRAINING_DIR   = _루트 / "data" / "training"
_TRAINING_FILE  = _TRAINING_DIR / "ollama_finetune_dataset.jsonl"
_TRAINING_LOCK  = threading.Lock()

# ── 집중 학습 모드 캐시 (5초 TTL — system_config.json 매번 읽지 않도록) ──
_SYS_CONFIG_PATH     = _루트 / "system_config.json"
_TRAINING_MODE_CACHE: dict = {"val": False, "ts": 0.0}
_TRAINING_MODE_TTL   = 5.0


def _is_training_mode() -> bool:
    """
    system_config.json 에서 집중 학습 모드 여부를 읽습니다 (5초 TTL 캐시).
    파일이 없거나 읽기 실패 시 False(일반 모드) 로 폴백합니다.
    """
    now = _time_mod.monotonic()
    if now - _TRAINING_MODE_CACHE["ts"] < _TRAINING_MODE_TTL:
        return bool(_TRAINING_MODE_CACHE["val"])
    val = False
    try:
        if _SYS_CONFIG_PATH.exists():
            cfg = json.loads(_SYS_CONFIG_PATH.read_text(encoding="utf-8"))
            val = bool(cfg.get("training_mode", False))
    except Exception:
        pass
    _TRAINING_MODE_CACHE["val"] = val
    _TRAINING_MODE_CACHE["ts"]  = now
    return val

# API 키는 .env에서 항상 덮어씀 (Streamlit 장시간 실행 시 구 키 캐시 방지)
_ENV_OVERWRITE_KEYS = frozenset({
    "GEMINI_API_KEY", "GROQ_API_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "ADMIN_TELEGRAM_ID",
})

_LAST_GROQ_ERROR: str = ""


def reload_env() -> None:
    """`.env`를 다시 읽고 모듈 수준 API 키 변수를 갱신합니다."""
    global GEMINI_API_KEY, GROQ_API_KEY
    env_path = _루트 / ".env"
    if not env_path.exists():
        GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
        GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not k:
            continue
        if k in _ENV_OVERWRITE_KEYS or k not in os.environ:
            os.environ[k] = v
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")


reload_env()

# ── 모델 설정 ──────────────────────────────────────────────────
_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]
# Groq Console 현행 모델 (구 llama3-70b-8192 등은 decommission → 404/빈 응답)
_GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama-3.1-70b-versatile",
]
_OLLAMA_EXPERT_MODEL  = "univagent-expert"  # 학습된 커스텀 모델 (우선 시도)
_OLLAMA_MODEL         = "llama3"            # 베이스 폴백 모델
_OLLAMA_HEALTH_URL    = "http://localhost:11434"   # Health Check 엔드포인트

# ── API 키 정규식 포맷 (포맷 오류 즉시 판별, 네트워크 불필요) ──
_GEMINI_KEY_RE = re.compile(r"^AIza[A-Za-z0-9_-]{35,}$")
_GROQ_KEY_RE   = re.compile(r"^gsk_[A-Za-z0-9]{50,}$")

# ── 헬스체크 결과 캐시 (60초 TTL, 매 Streamlit 렌더마다 HTTP 재호출 방지) ──
_HEALTH_CACHE: dict = {"ts": 0.0, "result": {}}
_HEALTH_TTL   = 60.0


# ─────────────────────────────────────────────────────────────
# Ollama 서버 Health Check (모듈 임포트 시 백그라운드 1회 실행)
# ─────────────────────────────────────────────────────────────

def _check_ollama_health() -> None:
    """
    Ollama 서버(포트 11434)가 응답하는지 확인합니다.
    응답 없으면 system_events.jsonl 에 WARNING 을 기록합니다.
    이벤트 루프 외부에서 호출되므로 완전히 동기·독립적으로 실행됩니다.
    """
    try:
        import requests as _req
        resp = _req.get(_OLLAMA_HEALTH_URL, timeout=3)
        if resp.status_code < 500:
            logger.info(
                f"[TokenManager] Ollama 서버 정상 응답 "
                f"(HTTP {resp.status_code}, {_OLLAMA_HEALTH_URL})"
            )
            return
        raise ConnectionError(f"HTTP {resp.status_code}")
    except Exception as exc:
        _msg = f"WARNING: Ollama 서버 미작동 — {_OLLAMA_HEALTH_URL} 응답 없음 ({exc})"
        logger.warning(f"[TokenManager] {_msg}")
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent))
            import logger_factory as _lf
            _lf.log_event(
                "OLLAMA_HEALTH_FAIL", "token_manager", _msg,
                level="WARNING",
                extra={
                    "ollama_url": _OLLAMA_HEALTH_URL,
                    "error":      str(exc)[:200],
                },
            )
        except Exception as _le:
            logger.debug(f"[TokenManager] logger_factory 기록 실패 (무시): {_le}")


# 모듈 임포트 시 즉시 백그라운드 실행 (봇 기동 타임라인을 블로킹하지 않음)
threading.Thread(target=_check_ollama_health, daemon=True, name="ollama-health-check").start()


# ─────────────────────────────────────────────────────────────
# 공개 API 헬스체크 (Low-Load) — web_dashboard.py 상태 표시등용
# ─────────────────────────────────────────────────────────────

def check_api_health() -> dict[str, str]:
    """
    Gemini / Groq / Ollama API 키 유효성을 저부하(Low-Load)로 검증합니다.

    검증 단계:
      1단계 — 키 정규식 포맷 검증 (즉시, 네트워크·토큰 소비 없음)
      2단계 — 모델 리스트 엔드포인트 호출 (텍스트 생성 없음, 토큰 미소비)
              Gemini: GET /v1beta/models?key=...
              Groq:   GET /v1/models  (Authorization: Bearer ...)
              Ollama: GET localhost:11434  (기존 _OLLAMA_HEALTH_URL)

    Returns dict with keys "gemini" / "groq" / "ollama":
      "ok"             — 정상 응답
      "key_missing"    — 키 미설정
      "key_format_err" — 정규식 포맷 불일치 (오타 등)
      "api_error:NNN"  — HTTP 오류 코드
      "unreachable"    — 네트워크 도달 불가 (Ollama)
    """
    now = _time_mod.monotonic()
    if now - _HEALTH_CACHE["ts"] < _HEALTH_TTL:
        return dict(_HEALTH_CACHE["result"])

    reload_env()
    result: dict[str, str] = {}

    # ── Gemini ──────────────────────────────────────────────
    if not GEMINI_API_KEY:
        result["gemini"] = "key_missing"
    elif not _GEMINI_KEY_RE.match(GEMINI_API_KEY):
        result["gemini"] = "key_format_err"
    else:
        try:
            import requests as _req
            resp = _req.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": GEMINI_API_KEY},
                timeout=5,
            )
            result["gemini"] = "ok" if resp.status_code == 200 else f"api_error:{resp.status_code}"
        except Exception as exc:
            result["gemini"] = f"api_error:{str(exc)[:60]}"

    # ── Groq ────────────────────────────────────────────────
    if not GROQ_API_KEY:
        result["groq"] = "key_missing"
    elif not _GROQ_KEY_RE.match(GROQ_API_KEY):
        result["groq"] = "key_format_err"
    else:
        try:
            import requests as _req
            resp = _req.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                timeout=5,
            )
            result["groq"] = "ok" if resp.status_code == 200 else f"api_error:{resp.status_code}"
        except Exception as exc:
            result["groq"] = f"api_error:{str(exc)[:60]}"

    # ── Ollama ──────────────────────────────────────────────
    try:
        import requests as _req
        resp = _req.get(_OLLAMA_HEALTH_URL, timeout=2)
        result["ollama"] = "ok" if resp.status_code < 500 else "unreachable"
    except Exception:
        result["ollama"] = "unreachable"

    _HEALTH_CACHE["ts"]     = now
    _HEALTH_CACHE["result"] = result
    logger.info(f"[TokenManager] API 헬스체크 완료: {result}")
    return dict(result)


# ── 쿼터/레이트리밋 감지 키워드 ──────────────────────────────
_QUOTA_KEYWORDS = (
    "429", "quota", "rate_limit", "resource_exhausted",
    "too_many_requests", "generaterequestsperdaY", "free_tier",
    "overloaded", "503",
)


# ─────────────────────────────────────────────────────────────
# Knowledge Distillation: ShareGPT JSONL 저장
# ─────────────────────────────────────────────────────────────

def save_for_local_training(
    prompt: str, system_prompt: str, response: str
) -> None:
    """Tier 1/2 성공 응답을 ShareGPT JSONL 형식으로 비동기 저장합니다."""
    def _write() -> None:
        try:
            _TRAINING_DIR.mkdir(parents=True, exist_ok=True)
            entry = {
                "messages": [
                    {"role": "system",    "content": system_prompt or ""},
                    {"role": "user",      "content": prompt},
                    {"role": "assistant", "content": response},
                ]
            }
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with _TRAINING_LOCK:
                with _TRAINING_FILE.open("a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as _e:
            logger.debug(f"[TokenManager] 학습 데이터 저장 실패 (무시): {_e}")

    threading.Thread(target=_write, daemon=True).start()


ERROR_FIX_TRAINING_SYSTEM = (
    "You are a Senior DevOps & Python engineer for the UnivAgent project. "
    "Given system error logs, explain root causes clearly (Korean OK) and write "
    "a complete English prompt for Claude Code to fix the identified bugs."
)


def save_error_fix_for_training(
    user_prompt: str,
    assistant_response: str,
    *,
    system_prompt: str | None = None,
    source: str = "error_fix",
) -> None:
    """
    에러 로그 분석·수정 프롬프트 생성 결과를 Ollama 학습용 JSONL에 저장합니다.
    (data/training/ollama_finetune_dataset.jsonl — 훈련소 페이지 3에서 활용)
    """
    if not (user_prompt.strip() and assistant_response.strip()):
        return
    sys_p = system_prompt or ERROR_FIX_TRAINING_SYSTEM
    tagged = f"{sys_p}\n[training_task=error_fix][source={source}]"
    save_for_local_training(user_prompt[:12_000], tagged, assistant_response[:12_000])
    logger.info(f"[TokenManager] 에러 수정 학습 페어 저장 ({source}, {len(assistant_response)}자)")


# ─────────────────────────────────────────────────────────────
# Tier 1 — Gemini
# ─────────────────────────────────────────────────────────────

def _tier1_gemini(prompt: str, system_prompt: str) -> tuple[str, str] | None:
    if not GEMINI_API_KEY:
        logger.debug("[TokenManager/Tier1] GEMINI_API_KEY 미설정 → 건너뜀")
        return None
    try:
        from google import genai
        from google.genai import types as _gt
    except ImportError:
        logger.warning("[TokenManager/Tier1] google-genai 미설치 → Tier 2 폴백")
        return None

    client = genai.Client(api_key=GEMINI_API_KEY)

    for model in _GEMINI_MODELS:
        try:
            cfg_kwargs: dict = {"temperature": 0.3, "max_output_tokens": 4096}
            if system_prompt:
                cfg_kwargs["system_instruction"] = system_prompt
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=_gt.GenerateContentConfig(**cfg_kwargs),
            )
            text = (resp.text or "").strip()
            if text:
                try:
                    _um = getattr(resp, "usage_metadata", None)
                    _tokens = int(
                        getattr(_um, "total_token_count", 0)
                        or (getattr(_um, "prompt_token_count", 0) + getattr(_um, "candidates_token_count", 0))
                        or max(1, (len(prompt) + len(system_prompt) + len(text)) // 4)
                    )
                    import db_manager as _db
                    _db.토큰_사용량_추가("gemini", _tokens)
                except Exception:
                    pass
                logger.info(
                    f"[TokenManager/Tier1] Gemini/{model} 성공 ({len(text)}자)"
                )
                return text, f"Gemini ({model})"
            logger.warning(f"[TokenManager/Tier1] Gemini/{model} 빈 응답 — 다음 모델")
        except Exception as exc:
            err_lower = str(exc).lower()
            if any(k in err_lower for k in _QUOTA_KEYWORDS):
                try:
                    import db_manager as _db
                    _db.쉴드방어_기록(1)
                except Exception:
                    pass
                logger.warning(
                    f"[TokenManager/Tier1] Gemini/{model} 쿼터/429 → 다음 모델"
                )
            else:
                logger.warning(
                    f"[TokenManager/Tier1] Gemini/{model} 오류: {str(exc)[:120]}"
                )

    logger.warning("[TokenManager/Tier1] 모든 Gemini 모델 소진 → Tier 2 폴백")
    return None


# ─────────────────────────────────────────────────────────────
# Tier 2 — Groq (Llama 3)
# ─────────────────────────────────────────────────────────────

def _tier2_groq(prompt: str, system_prompt: str) -> tuple[str, str] | None:
    global _LAST_GROQ_ERROR
    _LAST_GROQ_ERROR = ""
    reload_env()

    if not GROQ_API_KEY:
        _LAST_GROQ_ERROR = "GROQ_API_KEY 미설정 (.env 확인)"
        logger.warning("[TokenManager/Tier2] GROQ_API_KEY 미설정 → Tier 3 폴백")
        return None
    try:
        from groq import Groq
    except ImportError:
        _LAST_GROQ_ERROR = "groq 패키지 미설치"
        logger.warning("[TokenManager/Tier2] groq 미설치 → Tier 3 폴백")
        return None

    client = Groq(api_key=GROQ_API_KEY)

    for model in _GROQ_MODELS:
        try:
            messages: list[dict] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.3,
                max_tokens=4096,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                try:
                    _usage = getattr(resp, "usage", None)
                    _tokens = int(
                        getattr(_usage, "total_tokens", 0)
                        or (getattr(_usage, "prompt_tokens", 0) + getattr(_usage, "completion_tokens", 0))
                        or max(1, (len(prompt) + len(system_prompt) + len(text)) // 4)
                    )
                    import db_manager as _db
                    _db.토큰_사용량_추가("groq", _tokens)
                except Exception:
                    pass
                logger.info(
                    f"[TokenManager/Tier2] Groq/{model} 성공 ({len(text)}자)"
                )
                return text, f"Groq ({model})"
            logger.warning(f"[TokenManager/Tier2] Groq/{model} 빈 응답 — 다음 모델")
        except Exception as exc:
            _LAST_GROQ_ERROR = f"{model}: {str(exc)[:200]}"
            err_lower = str(exc).lower()
            if any(k in err_lower for k in _QUOTA_KEYWORDS):
                logger.warning(
                    f"[TokenManager/Tier2] Groq/{model} 레이트리밋 → 다음 모델"
                )
            else:
                logger.warning(
                    f"[TokenManager/Tier2] Groq/{model} 오류: {str(exc)[:120]}"
                )

    if not _LAST_GROQ_ERROR:
        _LAST_GROQ_ERROR = "모든 Groq 모델에서 빈 응답 또는 미지원 모델"
    logger.warning("[TokenManager/Tier2] 모든 Groq 모델 소진 → Tier 3 폴백")
    return None


# ─────────────────────────────────────────────────────────────
# Tier 3 — Ollama (로컬, 최종 폴백)
# ─────────────────────────────────────────────────────────────

def _tier3_ollama(
    prompt: str, system_prompt: str, timeout: int = 120
) -> tuple[str, str] | None:
    try:
        import ollama as _ollama
    except ImportError:
        logger.error("[TokenManager/Tier3] ollama 미설치 — 모든 티어 불가")
        return None

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    client = _ollama.Client(host="http://localhost:11434", timeout=timeout)

    # univagent-expert(학습된 커스텀 모델) 우선 시도 → 없으면 llama3 폴백
    for model in (_OLLAMA_EXPERT_MODEL, _OLLAMA_MODEL):
        try:
            resp = client.chat(model=model, messages=messages)
            text = (resp.message.content or "").strip()
            if text:
                logger.info(
                    f"[TokenManager/Tier3] Ollama/{model} 성공 ({len(text)}자)"
                )
                return text, f"Ollama ({model})"
            logger.warning(f"[TokenManager/Tier3] Ollama/{model} 빈 응답 — 다음 모델 시도")
        except Exception as exc:
            logger.warning(
                f"[TokenManager/Tier3] Ollama/{model} 실패: {str(exc)[:100]}"
            )

    logger.error("[TokenManager/Tier3] Ollama 모든 모델 실패")
    return None


# ─────────────────────────────────────────────────────────────
# Crawl 전용 라우트 — Groq → Ollama (Gemini 토큰 절약)
# ─────────────────────────────────────────────────────────────

def _crawl_route(prompt: str, system_prompt: str) -> tuple[str, str] | None:
    """크롤링·데이터 생성·PDF 파싱 전용. Gemini를 사용하지 않습니다."""
    pair = _tier2_groq(prompt, system_prompt)
    if pair:
        text, engine = pair
        save_for_local_training(prompt, system_prompt, text)
        return text, engine

    pair = _tier3_ollama(prompt, system_prompt)
    if pair:
        return pair

    return None


# ─────────────────────────────────────────────────────────────
# 공개 인터페이스
# ─────────────────────────────────────────────────────────────

def generate_text_sync(
    prompt: str,
    system_prompt: str = "",
    force_engine: str | None = None,
) -> tuple[str, str]:
    """
    3-Tier LLM 라우터 (동기 버전).

    force_engine=None    : Tier 1(Gemini) → Tier 2(Groq) → Tier 3(Ollama) 폴백
    force_engine="gemini"
    force_engine="code"  : Gemini만 (코드 수정·에러 분석 전용)
    force_engine="crawl" : Groq → Ollama만 (크롤링·시딩·PDF 파싱 전용)
    force_engine="groq"  : Groq만 시도, 실패 시 ("", "없음(Groq 강제 실패)")
    force_engine="ollama": Ollama(로컬)만 시도, 120초 타임아웃
    Tier 1/2 성공 시 학습 데이터를 비동기 저장합니다.
    반환: (생성_텍스트, 엔진명)
    """
    if force_engine in ("gemini", "code"):
        force_engine = "gemini"

    if force_engine == "crawl":
        pair = _crawl_route(prompt, system_prompt)
        if pair:
            text, engine = pair
            return text, engine
        logger.warning("[TokenManager] force_engine=crawl 실패 — Groq/Ollama 모두 소진")
        return "", "없음(Crawl 실패)"

    if force_engine == "gemini":
        pair = _tier1_gemini(prompt, system_prompt)
        if pair:
            text, engine = pair
            save_for_local_training(prompt, system_prompt, text)
            return text, engine
        logger.warning("[TokenManager] force_engine=gemini 실패 — 빈 문자열 반환")
        return "", "없음(Gemini 강제 실패)"

    if force_engine == "groq":
        pair = _tier2_groq(prompt, system_prompt)
        if pair:
            text, engine = pair
            save_for_local_training(prompt, system_prompt, text)
            return text, engine
        _detail = (_LAST_GROQ_ERROR or "빈 응답")[:120]
        logger.warning(f"[TokenManager] force_engine=groq 실패 — {_detail}")
        return "", f"없음(Groq 강제 실패: {_detail})"

    if force_engine == "ollama":
        pair = _tier3_ollama(prompt, system_prompt, timeout=120)
        if pair:
            text, engine = pair
            return text, engine
        logger.warning("[TokenManager] force_engine=ollama 실패 — 빈 문자열 반환")
        return "", "없음(Ollama 강제 실패)"

    # ── 집중 학습 모드 하드 락 ─────────────────────────────────
    # force_engine=None(기본 라우팅) 일 때만 적용됩니다.
    # "crawl" → 데이터 포집 바이패스 / "gemini"/"code" → 자가치유 바이패스
    if _is_training_mode():
        logger.info(
            "[TokenManager] 🔥 집중 학습 모드 활성 — "
            "Tier1(Gemini)/Tier2(Groq) 일반 응답 차단 → Tier3(Ollama) 전용"
        )
        pair = _tier3_ollama(prompt, system_prompt)
        if pair:
            text, engine = pair
            return text, engine
        logger.warning("[TokenManager] 학습 모드 Ollama 응답 실패 — 빈 문자열 반환")
        return "", "없음(학습모드-Ollama실패)"

    # 기본 3-Tier 폴백 라우팅 (일반 모드)
    pair = _tier1_gemini(prompt, system_prompt)
    if pair:
        text, engine = pair
        save_for_local_training(prompt, system_prompt, text)
        return text, engine

    pair = _tier2_groq(prompt, system_prompt)
    if pair:
        text, engine = pair
        save_for_local_training(prompt, system_prompt, text)
        return text, engine

    # Tier 3 (학습 데이터 저장 없음 — 로컬 모델 출력은 품질 보장 불가)
    pair = _tier3_ollama(prompt, system_prompt)
    if pair:
        text, engine = pair
        return text, engine

    logger.error("[TokenManager] 모든 티어 실패 — 빈 문자열 반환")
    return "", "없음"


async def generate_text(
    prompt: str,
    system_prompt: str = "",
    force_engine: str | None = None,
) -> tuple[str, str]:
    """
    3-Tier LLM 라우터 (비동기 버전).

    동기 구현을 ThreadPoolExecutor 에서 실행하므로
    이벤트 루프를 블로킹하지 않습니다.
    force_engine: "gemini"|"code"|"crawl"|"groq"|"ollama"|None
    반환: (생성_텍스트, 엔진명)
    """
    loop = asyncio.get_event_loop()
    fn = functools.partial(generate_text_sync, prompt, system_prompt, force_engine)
    return await loop.run_in_executor(None, fn)
