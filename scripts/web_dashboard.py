"""
web_dashboard.py — UnivAgent MLOps Dashboard (Streamlit, Mobile-First)

실행: streamlit run scripts/web_dashboard.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# ── 경로 설정 ───────────────────────────────────────────────────
_루트 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_루트 / "scripts"))

_LOG_DIR        = _루트 / "data" / "logs"
_FIX_ERR_DIR    = _루트 / "data" / "fix_error"
_CHECKPOINT_FILE = _FIX_ERR_DIR / "error_analysis_checkpoint.json"
_TRAINING_FILE  = _루트 / "data" / "training" / "ollama_finetune_dataset.jsonl"
_EXPORT_FILE    = _루트 / "data" / "training" / "ready_for_ollama.jsonl"
_MODELFILE_PATH = _루트 / "Modelfile"
_EXPERT_MODEL   = "univagent-expert"
_BASE_MODEL     = "llama3"
_TOP_K_PAIRS    = 25
_G_LIMIT        = 32_000
_GR_LIMIT       = 30_000

# ── Zero-Downtime Hot-Swap 태그 ────────────────────────────────
_EXPERT_MODEL_TEMP   = f"{_EXPERT_MODEL}:temp"
_EXPERT_MODEL_LATEST = f"{_EXPERT_MODEL}:latest"
_BUILD_TIMEOUT       = 300          # ollama create 최대 대기 (초)

# ── Context Window 컷오프 방어 ─────────────────────────────────
_SYSTEM_CONTENT_MAX  = 4000         # SYSTEM 블록 최대 문자 수 (OOM 방지)
_SYSTEM_PREFIX       = (
    "너는 UnivAgent의 최고 입시 전문가야. "
    "제공된 [모범 사례]를 학습하여 동일한 말투와 논리로 대답해.\n\n"
    "[모범 사례]\n"
)

# ── 팩트 기반 추론 하이퍼파라미터 고정 ────────────────────────
_MODELFILE_PARAMS    = (
    "PARAMETER temperature 0.3\n"
    "PARAMETER num_ctx 8192\n"
    'PARAMETER stop "User:"\n'
)

# ── 페이지 설정 ────────────────────────────────────────────────
st.set_page_config(
    page_title="UnivAgent 관제센터",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────
# 사이드바 네비게이션
# ─────────────────────────────────────────────────────────────
st.sidebar.title("🛠️ UnivAgent 관제센터")
st.sidebar.caption(datetime.now().strftime("%Y-%m-%d %H:%M"))

menu = st.sidebar.radio(
    "이동할 메뉴를 선택하세요",
    [
        "📊 종합 상황판",
        "🤖 AI 심야 훈련소",
        "📂 데이터 관제",
        "⚙️ 시스템 로그",
    ],
)

if st.sidebar.button("🔄 데이터 새로고침", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.sidebar.divider()
st.sidebar.info(
    "**엔진 정책**\n\n"
    "🔧 코드 수정: Gemini\n"
    "🕷️ 크롤링/시딩: Groq → Ollama\n\n"
    "1. 🔹 Gemini (코드·자가치유)\n"
    "2. ⚡ Groq (크롤링 1순위)\n"
    "3. 💻 Ollama (크롤링 2순위)\n\n"
    "로컬 모델: `univagent-expert` → `llama3`"
)

# ─────────────────────────────────────────────────────────────
# 집중 학습 모드 토글 (system_config.json 공유 상태)
# ─────────────────────────────────────────────────────────────
_SYS_CONFIG_PATH = _루트 / "system_config.json"


def _read_sys_config() -> dict:
    try:
        if _SYS_CONFIG_PATH.exists():
            return json.loads(_SYS_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"training_mode": False}


def _write_sys_config(cfg: dict) -> None:
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        _SYS_CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as _e:
        st.sidebar.error(f"설정 저장 실패: {_e}")


_sys_cfg            = _read_sys_config()
_training_mode_now  = bool(_sys_cfg.get("training_mode", False))

st.sidebar.divider()
st.sidebar.markdown("#### 🔥 API 자원 관리")

_new_training_mode = st.sidebar.toggle(
    "API 자원 전면 학습용 전환",
    value=_training_mode_now,
    key="sidebar_training_mode_toggle",
    help="활성화 시 Gemini/Groq 를 학습 파이프라인 전용으로 전환합니다. 일반 텔레그램 응답은 Ollama(로컬)로만 처리됩니다.",
)

if _new_training_mode != _training_mode_now:
    _write_sys_config({"training_mode": _new_training_mode})
    st.cache_data.clear()
    if _new_training_mode:
        st.sidebar.success("✅ 학습 모드 활성화 — Gemini/Groq 일반 응답 차단됨")
    else:
        st.sidebar.info("💡 일반 응답 모드로 복귀")
    st.rerun()

if _new_training_mode:
    st.sidebar.warning(
        "🔥 **집중 학습 모드 ON**\n\n"
        "Gemini·Groq 자원이\n학습 파이프라인 전용.\n"
        "텔레그램 → Ollama 전용 응답."
    )

# ─────────────────────────────────────────────────────────────
# 캐시된 데이터 로더
# ─────────────────────────────────────────────────────────────
_LOG_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s+(.*)", re.DOTALL
)


@st.cache_data(ttl=30)
def _load_all_logs() -> pd.DataFrame:
    rows: list[dict] = []
    for path in sorted(_LOG_DIR.glob("*.log")):
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                m = _LOG_PATTERN.match(line.strip())
                if not m:
                    continue
                rows.append({
                    "timestamp": pd.to_datetime(m.group(1), errors="coerce"),
                    "level":     m.group(2).upper(),
                    "message":   m.group(3),
                    "source":    path.name,
                })
        except Exception:
            pass
    for path in sorted(_FIX_ERR_DIR.glob("*.log")):
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                m = _LOG_PATTERN.match(line.strip())
                if not m:
                    continue
                rows.append({
                    "timestamp": pd.to_datetime(m.group(1), errors="coerce"),
                    "level":     m.group(2).upper(),
                    "message":   m.group(3),
                    "source":    f"fix_error/{path.name}",
                })
        except Exception:
            pass
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return (
        df.dropna(subset=["timestamp"])
        .sort_values("timestamp", ascending=False)
        .reset_index(drop=True)
    )


@st.cache_data(ttl=30)
def _load_ai_runtime_errors() -> list[dict]:
    p = _FIX_ERR_DIR / "ai_runtime_errors.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


@st.cache_data(ttl=30)
def _load_training_data() -> list[dict]:
    if not _TRAINING_FILE.exists():
        return []
    entries: list[dict] = []
    for line in _TRAINING_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return entries


@st.cache_data(ttl=60)
def _db_token_usage() -> dict[str, int]:
    try:
        import db_manager
        return db_manager.오늘_토큰_사용량()
    except Exception:
        return {"gemini": 0, "groq": 0}


@st.cache_data(ttl=60)
def _db_growth_metrics() -> dict:
    """
    DB 메트릭 현황 (오늘 누적값 / 주간 델타 / 월간 델타)을 반환합니다.
    devops_reporter._기간별_집계 와 동일한 델타 로직을 적용합니다.
    """
    from datetime import timedelta

    try:
        import db_manager
        rows = db_manager.시스템_메트릭_조회(days=30)
    except Exception:
        return {}
    if not rows:
        return {}

    오늘 = datetime.now().date()

    def _날짜(r):
        try:
            return datetime.strptime(r["date_str"], "%Y-%m-%d").date()
        except Exception:
            return None

    def _최신(key):
        return int(rows[-1].get(key, 0) or 0)

    def _델타(lst, key) -> int:
        vals = [int(r.get(key, 0) or 0) for r in lst if _날짜(r) is not None]
        if not vals:
            return 0
        return max(vals[-1] - vals[0], 0) if len(vals) > 1 else vals[0]

    주간 = [r for r in rows if _날짜(r) and (오늘 - _날짜(r)).days < 7]
    월간 = rows[:]

    return {
        "seteuk_total":    _최신("seteuk_count"),
        "stats_total":     _최신("univ_stats_count"),
        "golden_total":    _최신("golden_count"),
        "seteuk_week":     _델타(주간, "seteuk_count"),
        "stats_week":      _델타(주간, "univ_stats_count"),
        "golden_week":     _델타(주간, "golden_count"),
        "seteuk_month":    _델타(월간, "seteuk_count"),
        "stats_month":     _델타(월간, "univ_stats_count"),
        "golden_month":    _델타(월간, "golden_count"),
        "shield_week":     sum(int(r.get("shield_defenses", 0) or 0) for r in 주간),
        "tokens_month":    sum(int(r.get("total_tokens", 0) or 0) for r in 월간),
        "e2e":             str(rows[-1].get("e2e_test_result", "") or "미실행"),
        "rows":            len(rows),
    }


@st.cache_data(ttl=300)
def _db_daily_growth(days: int = 7) -> pd.DataFrame:
    """
    최근 N일간 일자별 수집 '증가량' DataFrame을 반환합니다.
    system_metrics 의 누적 스냅샷을 diff() 하여 일별 순증분을 계산합니다.
    Columns: date, seteuk_daily, stats_daily
    """
    try:
        import db_manager as _db
        rows = _db.시스템_메트릭_조회(days + 1)   # diff 를 위해 하루 더 조회
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date_str"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    for col in ("seteuk_count", "univ_stats_count"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["seteuk_daily"] = df["seteuk_count"].diff().clip(lower=0).fillna(0).astype(int)
    df["stats_daily"]  = df["univ_stats_count"].diff().clip(lower=0).fillna(0).astype(int)

    return df.tail(days)[["date", "seteuk_daily", "stats_daily"]].reset_index(drop=True)


def _api_health() -> dict[str, str]:
    """token_manager.check_api_health() 래퍼 (60초 TTL 캐시는 token_manager 내부에서 처리)."""
    try:
        import token_manager as _tm
        return _tm.check_api_health()
    except Exception:
        return {}


def _health_indicator(status: str) -> str:
    """API 헬스 상태 코드를 이모지 표시등 문자열로 변환합니다."""
    if status == "ok":
        return "🟢 정상"
    if status == "key_missing":
        return "🔴 키 미설정"
    if status == "key_format_err":
        return "🔴 키 형식 오류"
    if status == "unreachable":
        return "🔴 서버 미작동"
    if status.startswith("api_error:"):
        code = status.split(":", 1)[1]
        return f"🔴 응답 오류 ({code[:30]})"
    return "⚪ 확인 중"


# ─────────────────────────────────────────────────────────────
# 공유 헬퍼 함수
# ─────────────────────────────────────────────────────────────

def _load_checkpoint_info() -> dict:
    try:
        import error_checkpoint as _ecp
        return _ecp.load_checkpoint()
    except Exception:
        if _CHECKPOINT_FILE.exists():
            try:
                return json.loads(_CHECKPOINT_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}


def _since_timestamp() -> pd.Timestamp | None:
    cp = _load_checkpoint_info()
    raw = cp.get("since")
    if not raw:
        return None
    return pd.to_datetime(raw, errors="coerce")


def _filter_since(df: pd.DataFrame, since: pd.Timestamp | None) -> pd.DataFrame:
    if df.empty or since is None or pd.isna(since):
        return df
    col = pd.to_datetime(df["timestamp"], errors="coerce")
    since_cmp = pd.Timestamp(since)
    # tz-naive vs tz-aware 불일치 → 양쪽을 같은 규격으로 정규화
    if col.dt.tz is None and since_cmp.tzinfo is not None:
        since_cmp = since_cmp.tz_localize(None)
    elif col.dt.tz is not None and since_cmp.tzinfo is None:
        since_cmp = since_cmp.tz_localize(col.dt.tz)
    return df[col >= since_cmp].reset_index(drop=True)


def _mark_errors_resolved(reason: str = "dashboard_manual") -> None:
    try:
        import error_checkpoint as _ecp
        _ecp.mark_resolved(reason)
    except Exception as exc:
        st.error(f"체크포인트 저장 실패: {exc}")
        return
    st.cache_data.clear()
    if "batch_result" in st.session_state:
        del st.session_state["batch_result"]
    if "single_result" in st.session_state:
        del st.session_state["single_result"]


def _collect_errors_df(df_all: pd.DataFrame, ai_errors: list[dict]) -> pd.DataFrame:
    """전체 로그에서 ERROR 레벨 + ai_runtime_errors 병합."""
    df_errors = (
        df_all[df_all["level"] == "ERROR"].copy()
        if not df_all.empty
        else pd.DataFrame()
    )
    if ai_errors:
        df_rt = pd.DataFrame([
            {
                "timestamp": pd.to_datetime(e.get("timestamp", ""), errors="coerce"),
                "level":     "ERROR",
                "source":    "ai_runtime_errors.json",
                "message":   str(
                    e.get("error_message", e.get("error", e.get("message", str(e))))
                )[:300],
            }
            for e in ai_errors
        ])
        df_errors = (
            pd.concat([df_errors, df_rt], ignore_index=True)
            .sort_values("timestamp", ascending=False)
            .reset_index(drop=True)
        )
    return df_errors


def get_grouped_errors(df: pd.DataFrame, max_chars: int = 6000) -> str:
    if df.empty:
        return "기록된 에러가 없습니다."

    def _extract_type(msg: str) -> str:
        m = re.search(r'\b([A-Z][a-zA-Z]+(?:Error|Exception|Warning|Fault|Failure))\b', msg)
        if m:
            return m.group(1)
        m2 = re.search(r'\[([^\]]{3,40})\]', msg)
        if m2:
            return m2.group(1)
        return msg[:50].strip() or "Unknown"

    df = df.copy()
    df["error_type"] = df["message"].apply(_extract_type)
    grouped = (
        df.groupby("error_type")
        .agg(count=("message", "count"), snippet=("message", lambda x: x.iloc[0][:200]))
        .sort_values("count", ascending=False)
        .reset_index()
    )

    header = f"총 {len(df)}건의 에러, {len(grouped)}가지 유형:\n"
    lines  = [header]
    used   = len(header)
    for i, (_, row) in enumerate(grouped.iterrows()):
        block = (
            f"\n[{row['error_type']}] {int(row['count'])}회 발생\n"
            f"  예시: {row['snippet']}\n"
        )
        if used + len(block) > max_chars:
            lines.append(f"\n... (이하 {len(grouped) - i}개 유형 토큰 한도로 생략)")
            break
        lines.append(block)
        used += len(block)

    result = "".join(lines)
    _TRUNC = "\n\n...(토큰 한도 초과 방지를 위해 하위 빈도 에러는 생략되었습니다.)"
    if len(result) > max_chars:
        result = result[: max_chars - len(_TRUNC)] + _TRUNC
    return result


def _format_fewshot_pairs(entries: list[dict], top_k: int = _TOP_K_PAIRS) -> str:
    scored = sorted(
        entries,
        key=lambda e: sum(
            len(m.get("content", "")) for m in e.get("messages", [])
            if m.get("role") == "assistant"
        ),
        reverse=True,
    )[:top_k]
    blocks: list[str] = []
    for idx, entry in enumerate(scored, 1):
        msgs     = entry.get("messages", [])
        user_msg = next((m["content"] for m in msgs if m.get("role") == "user"),      "")
        asst_msg = next((m["content"] for m in msgs if m.get("role") == "assistant"), "")
        blocks.append(f"예시 #{idx}\nQ: {user_msg[:200]}\nA: {asst_msg[:500]}")
    return "\n\n".join(blocks)


def _build_modelfile_system(entries: list[dict], top_k: int = _TOP_K_PAIRS) -> str:
    """
    Context window 컷오프 방어 (_SYSTEM_CONTENT_MAX 자 이하 보장).
    JSONL은 append-only이므로 역순(newest first) 우선 포함.
    최종 안전망 슬라이싱으로 절대 초과 없음.
    """
    budget = _SYSTEM_CONTENT_MAX - len(_SYSTEM_PREFIX)
    candidates = list(reversed(entries))[:top_k]
    blocks: list[str] = []
    used = 0
    for idx, entry in enumerate(candidates, 1):
        msgs     = entry.get("messages", [])
        user_msg = next((m["content"] for m in msgs if m.get("role") == "user"),      "")
        asst_msg = next((m["content"] for m in msgs if m.get("role") == "assistant"), "")
        block = f"예시 #{idx}\nQ: {user_msg[:200]}\nA: {asst_msg[:500]}\n\n"
        if used + len(block) > budget:
            break
        blocks.append(block)
        used += len(block)
    body = "".join(blocks).rstrip() or "(학습 데이터 없음)"
    return (_SYSTEM_PREFIX + body)[:_SYSTEM_CONTENT_MAX]


_MASTER_PROMPT_TEMPLATE = (
    "다음은 우리 시스템에서 발생한 전체 에러들을 유형별로 그룹화한 요약본입니다.\n\n"
    "```\n{errors}\n```\n\n"
    "이 에러들의 근본 원인을 종합적으로 분석하고, 이 버그들을 한 번에 수정하기 위해 "
    "'Claude Code' 터미널에 그대로 복사해서 붙여넣을 수 있는 완벽한 영문 마스터 프롬프트(해결 지시문)를 작성해주세요. "
    "한국어 원인 해설을 먼저 적고, 그 아래에 프롬프트를 마크다운 코드 블록으로 제공하세요."
)


def _run_llm(prompt: str, force_engine: str | None = None) -> tuple[str, str]:
    """asyncio.run + RuntimeError 폴백으로 LLM 호출."""
    import token_manager as _tm

    _tm.reload_env()
    try:
        return asyncio.run(_tm.generate_text(prompt, force_engine=force_engine))
    except RuntimeError:
        return _tm.generate_text_sync(prompt, force_engine=force_engine)


def _format_llm_error_message(err: str, engine_hint: str = "") -> str:
    """사용자-facing LLM 실패 메시지 (Groq 키 오류 등 안내)."""
    base = err or "알 수 없는 오류"
    eng = engine_hint or base
    if "Groq 강제 실패" in eng or "Groq" in eng:
        hint = (
            "- `.env`의 `GROQ_API_KEY`가 없거나 잘못되었을 때 401 `invalid_api_key`가 납니다.\n"
            "- [console.groq.com/keys](https://console.groq.com/keys)에서 새 키 발급 후 **Streamlit 재시작**.\n"
        )
        if "decommission" in base.lower() or "not found" in base.lower() or "model" in base.lower():
            hint = (
                "- Groq **모델명 변경**으로 실패했을 수 있습니다 (코드는 최신 모델로 수정됨 → **Streamlit 재시작**).\n"
            ) + hint
        if "401" in base or "invalid_api_key" in base.lower():
            hint = "- 키가 무효합니다. Console에서 새 키 발급 후 `.env` 갱신.\n" + hint
        return (
            f"{base}\n\n"
            "**Groq 연결 실패** — 이 버튼은 Groq만 사용합니다 (Ollama로 자동 전환되지 않음).\n"
            + hint
            + "- 지금 바로 분석하려면 **맥미니 로컬 AI(Ollama)** 버튼을 누르세요."
        )
    if "Gemini 강제 실패" in eng or "Gemini" in eng:
        return (
            f"{base}\n\n"
            "**Gemini 연결 실패** — `GEMINI_API_KEY` 확인 후 봇/대시보드를 재시작하세요.\n"
            "- 또는 **Ollama** 버튼으로 로컬 분석을 시도하세요."
        )
    if "Ollama 강제 실패" in eng:
        return (
            f"{base}\n\n"
            "**Ollama 연결 실패** — 터미널에서 `ollama serve`가 실행 중인지 확인하세요."
        )
    return base


def _persist_fix_training(
    user_prompt: str,
    response_text: str,
    engine: str,
    *,
    source: str,
) -> None:
    """성공한 에러 분석 결과를 Ollama 파인튜닝 데이터셋에 추가."""
    import token_manager as _tm

    _tm.save_error_fix_for_training(
        user_prompt,
        response_text,
        system_prompt=_tm.ERROR_FIX_TRAINING_SYSTEM,
        source=f"{source}:{engine}",
    )


# ─────────────────────────────────────────────────────────────
# Zero-Downtime Hot-Swap 파이프라인 (백그라운드 스레드)
# ─────────────────────────────────────────────────────────────

def _hotswap_pipeline(status: dict, modelfile_path: Path) -> None:
    """
    백그라운드 스레드 실행 함수.
    1) ollama create :temp   ← 신모델 빌드 (기존 :latest 서비스 계속)
    2) ollama cp :temp → :latest  ← 무중단 원자적 교체
    3) ollama rm :temp            ← 임시 모델 정리
    status dict 는 Streamlit session_state 가 참조하는 공유 객체.
    """
    try:
        # Phase 1 — 임시 모델 빌드
        status["phase"] = "1/3 · 임시 모델 빌드 중… (ollama create :temp)"
        proc = subprocess.run(
            ["ollama", "create", _EXPERT_MODEL_TEMP, "-f", str(modelfile_path)],
            capture_output=True, text=True,
            timeout=_BUILD_TIMEOUT,
            cwd=str(modelfile_path.parent),
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"exit {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        _stdout = proc.stdout.strip()

        # Phase 2 — 핫스왑 (cp)
        status["phase"] = "2/3 · 핫스왑 적용 중… (ollama cp :temp → :latest)"
        cp = subprocess.run(
            ["ollama", "cp", _EXPERT_MODEL_TEMP, _EXPERT_MODEL_LATEST],
            capture_output=True, text=True, timeout=30,
        )
        if cp.returncode != 0:
            raise RuntimeError(
                f"ollama cp 실패 (exit {cp.returncode}): {cp.stderr.strip()}"
            )

        # Phase 3 — 임시 모델 정리
        status["phase"] = "3/3 · 임시 모델 정리 중… (ollama rm :temp)"
        subprocess.run(
            ["ollama", "rm", _EXPERT_MODEL_TEMP],
            capture_output=True, text=True, timeout=30,
        )

        status.update({
            "running": False, "success": True,
            "phase":   "완료",
            "stdout":  _stdout,
        })

    except subprocess.TimeoutExpired:
        status.update({
            "running": False, "success": False, "phase": "실패",
            "error": f"ollama create {_BUILD_TIMEOUT}초 초과 — Ollama 실행 여부를 확인하세요.",
        })
    except FileNotFoundError:
        status.update({
            "running": False, "success": False, "phase": "실패",
            "error": "`ollama` 명령을 찾을 수 없습니다. Ollama를 설치하세요.",
        })
    except Exception as exc:
        status.update({
            "running": False, "success": False, "phase": "실패",
            "error": str(exc)[:400],
        })


# ═════════════════════════════════════════════════════════════
# PAGE 1 — 종합 상황판
# ═════════════════════════════════════════════════════════════
if menu == "📊 종합 상황판":
    st.title("📊 종합 상황판")

    # ── 집중 학습 모드 배너 ───────────────────────────────────
    _p1_training = _read_sys_config().get("training_mode", False)
    if _p1_training:
        st.warning(
            "🔥 **집중 학습 모드 활성화 중** — "
            "Gemini·Groq 자원이 학습 파이프라인 전용으로 전환되었습니다. "
            "텔레그램 일반 응답은 로컬 Ollama 엔진으로만 처리됩니다."
        )

    # ── 공통 데이터 로드 ─────────────────────────────────────
    df_all    = _load_all_logs()
    tok_usage = _db_token_usage()
    growth    = _db_growth_metrics()

    gemini_tokens  = tok_usage.get("gemini", 0)
    groq_tokens    = tok_usage.get("groq", 0)
    today_tokens   = gemini_tokens + groq_tokens

    total_errors   = int((df_all["level"] == "ERROR").sum())   if not df_all.empty else 0
    _since_p1      = _since_timestamp()
    _df_err_p1     = df_all[df_all["level"] == "ERROR"] if not df_all.empty else pd.DataFrame()
    new_errors     = (
        len(_filter_since(_df_err_p1, _since_p1))
        if _since_p1 is not None and not pd.isna(_since_p1) and not _df_err_p1.empty
        else total_errors
    )
    total_warnings = int((df_all["level"] == "WARNING").sum()) if not df_all.empty else 0
    total_lines    = len(df_all)

    try:
        _disk = shutil.disk_usage(_루트)
        disk_free_gb = round(_disk.free / (1024 ** 3), 1)
        disk_pct     = round(_disk.used / _disk.total * 100, 1)
    except Exception:
        disk_free_gb = 0.0
        disk_pct     = 0.0

    def _pct_str(delta: int, total: int) -> str:
        if total > 0 and delta > 0:
            return f"+{delta:,}건 ({delta / total * 100:.1f}%↑ 주간)"
        return f"+{delta:,}건 (주간)"

    # ════════════════════════════════════════════════════════
    # 섹션 A: 엔진별 토큰 사용량 + API 상태 표시등 (최우선 배치)
    # ════════════════════════════════════════════════════════
    st.markdown("### ⚡ 엔진별 토큰 사용량 & API 상태")

    _health = _api_health()
    _g_ind  = _health_indicator(_health.get("gemini", ""))
    _gr_ind = _health_indicator(_health.get("groq",   ""))
    _ol_ind = _health_indicator(_health.get("ollama", ""))

    _tc1, _tc2, _tc3 = st.columns(3)
    _tc1.metric(
        label=f"🔹 Gemini (오늘) — {_g_ind}",
        value=f"{gemini_tokens:,}",
        delta=f"한도 {_G_LIMIT:,} 대비 {min(100, round(gemini_tokens / max(_G_LIMIT, 1) * 100, 1))}% 사용",
        delta_color="inverse" if gemini_tokens > _G_LIMIT * 0.8 else "off",
    )
    _tc2.metric(
        label=f"🔸 Groq (오늘) — {_gr_ind}",
        value=f"{groq_tokens:,}",
        delta=f"한도 {_GR_LIMIT:,} 대비 {min(100, round(groq_tokens / max(_GR_LIMIT, 1) * 100, 1))}% 사용",
        delta_color="inverse" if groq_tokens > _GR_LIMIT * 0.8 else "off",
    )
    _tc3.metric(
        label=f"💻 Ollama (로컬) — {_ol_ind}",
        value=f"{growth.get('tokens_month', 0):,}",
        delta="이번달 Gemini + Groq 누적",
        delta_color="off",
    )

    _tg1, _tg2 = st.columns(2)
    with _tg1:
        g_pct = min(100, round(gemini_tokens / max(_G_LIMIT, 1) * 100, 1))
        st.markdown(f"**🔹 Gemini** {_g_ind} — `{gemini_tokens:,}` / `{_G_LIMIT:,}` 토큰")
        st.progress(g_pct / 100, text=f"{g_pct}% {'⚠️ 한도 임박' if g_pct >= 80 else '정상'}")
    with _tg2:
        gr_pct = min(100, round(groq_tokens / max(_GR_LIMIT, 1) * 100, 1))
        st.markdown(f"**🔸 Groq** {_gr_ind} — `{groq_tokens:,}` / `{_GR_LIMIT:,}` 토큰")
        st.progress(gr_pct / 100, text=f"{gr_pct}% {'⚠️ 한도 임박' if gr_pct >= 80 else '정상'}")

    st.divider()

    # ════════════════════════════════════════════════════════
    # 섹션 C: 시스템 상태 요약 메트릭
    # ════════════════════════════════════════════════════════
    st.markdown("### 🖥️ 시스템 상태")
    col1, col2, col3 = st.columns(3)
    _has_cp = _since_p1 is not None and not pd.isna(_since_p1)
    col1.metric(
        label="🔴 신규 에러 수" if _has_cp else "🔴 총 에러 수",
        value=f"{new_errors:,}",
        delta=(
            f"전체 {total_errors:,}건 · 경고 {total_warnings:,}건"
            if _has_cp
            else f"경고 {total_warnings:,}건"
        ),
        delta_color="inverse",
    )
    col2.metric(
        label="🔢 오늘 총 토큰",
        value=f"{today_tokens:,}",
        delta=f"Gemini {gemini_tokens:,} + Groq {groq_tokens:,}",
        delta_color="off",
    )
    col3.metric(
        label="💾 디스크 여유",
        value=f"{disk_free_gb} GB",
        delta=f"사용률 {disk_pct}% · 로그 {total_lines:,}줄",
        delta_color="inverse" if disk_pct >= 85 else "off",
    )

    # 에러 배너
    if total_errors == 0:
        st.info("💡 모든 시스템이 정상적으로 작동 중입니다. (에러 0건)")
    elif _has_cp and new_errors == 0:
        st.info(
            f"💡 재시작·처리 기준 이후 **신규 에러 0건** "
            f"(과거 로그에 ERROR {total_errors:,}건 남음 — 관제 메뉴에서 전체 보기 가능)."
        )
    else:
        st.warning(
            "⚠️ "
            + (
                f"신규 에러 {new_errors:,}건 (전체 {total_errors:,}건)"
                if _has_cp
                else f"에러 {total_errors:,}건"
            )
            + " — '🚨 에러 관제'에서 AI 분석을 실행하세요."
        )

    st.divider()

    # ════════════════════════════════════════════════════════
    # 섹션 D: 로그 시각화 (시간대별 분포) + 최근 로그 테이블
    # ════════════════════════════════════════════════════════
    if df_all.empty:
        st.info("로그 파일이 없거나 파싱 가능한 항목이 없습니다.")
    else:
        st.markdown("#### 시간대별 로그 레벨 분포")
        df_chart = df_all.copy()
        df_chart["hour"] = df_chart["timestamp"].dt.floor("h")
        level_counts = df_chart.groupby(["hour", "level"]).size().reset_index(name="count")
        if not level_counts.empty:
            fig = px.bar(
                level_counts, x="hour", y="count", color="level",
                color_discrete_map={
                    "ERROR": "#EF4444", "WARNING": "#F59E0B",
                    "INFO": "#3B82F6",  "DEBUG": "#6B7280",
                },
                labels={"hour": "시간", "count": "로그 수", "level": "레벨"},
                height=300,
            )
            fig.update_layout(margin=dict(l=0, r=0, t=20, b=0))
            st.plotly_chart(fig, use_container_width=True)

        # ── 최근 로그 테이블 (소스별 에러/경고 차트 제거, 테이블만 유지) ──
        _LIMIT_OPTIONS = {"100건": 100, "500건": 500, "1,000건": 1000, "전체 (위험)": None}
        _lbl = st.selectbox(
            "최근 로그 표시 개수",
            options=list(_LIMIT_OPTIONS.keys()),
            index=0,
            key="p1_log_limit",
        )
        _lim = _LIMIT_OPTIONS[_lbl]
        if _lim is None:
            st.warning("⚠️ 전체 로그를 표시합니다. 수만 건이면 브라우저가 느려질 수 있습니다.")
        _df_show = df_all if _lim is None else df_all.head(_lim)
        st.markdown(f"#### 최근 로그 ({_lbl})")
        st.dataframe(
            _df_show[["timestamp", "level", "source", "message"]],
            use_container_width=True,
            height=300,
        )


# ═════════════════════════════════════════════════════════════
# PAGE 2 — 에러 관제 및 AI 치유
# ═════════════════════════════════════════════════════════════
elif menu == "⚙️ 시스템 로그":
    st.title("⚙️ 시스템 로그 & 에러 관제")

    df_all2   = _load_all_logs()
    ai_errors = _load_ai_runtime_errors()
    df_errors_all = _collect_errors_df(df_all2, ai_errors)

    _cp = _load_checkpoint_info()
    _since_ts = _since_timestamp()

    _fc1, _fc2, _fc3 = st.columns([2, 1, 1])
    with _fc1:
        _since_only = st.checkbox(
            "🆕 재시작·처리 완료 **이후** 신규 에러만 분석 (권장)",
            value=True,
            key="p2_since_only",
        )
    with _fc2:
        if st.button("✅ 지금까지 처리 완료", key="btn_mark_resolved", use_container_width=True):
            _mark_errors_resolved("dashboard_mark_resolved")
            st.rerun()
    with _fc3:
        if st.button("🔄 로그 새로고침", key="btn_refresh_logs", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    if _since_ts is not None and not pd.isna(_since_ts):
        st.caption(
            f"분석 기준 시각: `{_cp.get('since', '')}` "
            f"({_cp.get('reason', 'unknown')}) · "
            f"전체 ERROR **{len(df_errors_all):,}**건 중 신규만 집계"
        )
    else:
        st.caption(
            "분석 기준 시각 없음 — `./restart_agent.sh` 실행 시 자동 설정됩니다. "
            f"현재 전체 ERROR **{len(df_errors_all):,}**건"
        )

    _no_checkpoint = _since_ts is None or pd.isna(_since_ts)
    if _since_only and _no_checkpoint and len(df_errors_all) > 0:
        st.warning(
            "분석 기준 시각이 없어 **과거 ERROR 전체**가 집계됩니다. "
            "코드 수정을 반영했다면 아래 **「지금부터 과거 에러 제외」**를 누르거나 "
            "`./restart_agent.sh`로 재시작하세요."
        )
        if st.button(
            "📌 지금부터 과거 에러 제외 (기준점 설정)",
            key="btn_set_baseline",
            use_container_width=True,
        ):
            _mark_errors_resolved("dashboard_baseline_set")
            st.rerun()

    if not _since_only:
        df_errors_raw = df_errors_all
    elif _no_checkpoint:
        df_errors_raw = pd.DataFrame()  # 기준점 없으면 과거 대량 분석 방지
    else:
        df_errors_raw = _filter_since(df_errors_all, _since_ts)

    # ── 발생한 에러 목록 ──────────────────────────────────────
    st.markdown("### 🚨 발생한 에러 목록")

    if df_errors_all.empty:
        st.success("✅ 기록된 에러가 없습니다. 시스템이 정상 작동 중입니다!")
    elif _since_only and _no_checkpoint and len(df_errors_all) > 0:
        pass  # 상단 경고·기준점 설정 버튼으로 안내
    elif df_errors_raw.empty:
        st.success(
            "✅ **신규 에러 없음** — 기준 시각 이후 추가된 ERROR가 없습니다. "
            "수정이 잘 반영된 상태일 수 있습니다."
        )
        if _since_only and len(df_errors_all) > 0:
            st.info(
                f"과거 로그에 ERROR **{len(df_errors_all):,}**건이 남아 있지만, "
                "기본 모드에서는 분석하지 않습니다. "
                "체크박스를 끄면 전체 분석이 가능합니다."
            )
    else:
        _scope = "신규" if _since_only else "전체"
        st.markdown(
            f"**{_scope} ERROR {len(df_errors_raw):,}건** "
            f"(전체 {len(df_errors_all):,}건) — 아래에서 AI 분석을 실행하세요."
        )

        # 표시 한도 선택 + 원본 데이터를 expander로 축소
        _LIMIT_OPTIONS2 = {"100건": 100, "500건": 500, "1,000건": 1000, "전체 (위험)": None}
        _lbl2 = st.selectbox(
            "에러 표시 한도",
            options=list(_LIMIT_OPTIONS2.keys()),
            index=0,
            key="p2_error_limit",
        )
        _lim2 = _LIMIT_OPTIONS2[_lbl2]
        df_errors = df_errors_raw if _lim2 is None else df_errors_raw.head(_lim2)

        with st.expander("에러 원본 데이터 보기 (터치하여 펼치기)", expanded=False):
            if _lim2 is None:
                st.warning("⚠️ 전체 에러를 표시합니다. 대용량 시 브라우저가 느려질 수 있습니다.")
            st.dataframe(
                df_errors[["timestamp", "source", "message"]],
                use_container_width=True,
                height=300,
            )

        # 에러 빈도 파이 차트
        if len(df_errors_raw) >= 2:
            _src_cnt = df_errors_raw["source"].value_counts().reset_index()
            _src_cnt.columns = ["source", "count"]
            fig3 = px.pie(_src_cnt, names="source", values="count", height=260)
            fig3.update_layout(margin=dict(l=0, r=0, t=20, b=0))
            st.plotly_chart(fig3, use_container_width=True)

        _ollama_train = st.checkbox(
            "🧠 분석 성공 시 Ollama 학습 데이터셋에 자동 저장 (메뉴 3 훈련소에서 `ollama create` 가능)",
            value=True,
            key="p2_save_ollama_training",
        )

        # ── 단건 에러 분석 ────────────────────────────────────
        st.divider()
        st.markdown("#### 🔍 단건 에러 상세 분석")
        _sel_max = min(20, len(df_errors))
        selected_idx = st.selectbox(
            "분석할 에러를 선택하세요",
            options=range(_sel_max),
            format_func=lambda i: (
                f"[{df_errors.iloc[i]['source']}] "
                f"{str(df_errors.iloc[i]['message'])[:80]}"
            ),
            key="p2_single_select",
        )

        if st.button(
            "🔬 선택한 에러 AI 원인 분석",
            key="btn_single_analyze",
            use_container_width=True,
        ):
            row = df_errors.iloc[selected_idx]
            with st.spinner(
                "🤖 AI가 에러 로그를 분석하고 해결 프롬프트를 작성 중입니다. (약 10~20초 소요)..."
            ):
                try:
                    _single_prompt = (
                        f"다음 파이썬 에러 로그를 분석해주세요.\n\n"
                        f"```\nTimestamp: {row['timestamp']}\n"
                        f"Source: {row['source']}\nMessage: {row['message']}\n```\n\n"
                        "1. 에러의 근본 원인을 한국어로 쉽게 설명하세요.\n"
                        "2. 이 에러를 수정하기 위해 'Claude Code'에 그대로 복사해서 붙여넣을 수 있는 "
                        "완벽한 영어 프롬프트(해결 지시문)를 작성해주세요."
                    )
                    _text, _engine = _run_llm(_single_prompt)
                    if not _text:
                        raise ValueError(f"엔진({_engine})에서 빈 응답이 반환되었습니다.")
                    if st.session_state.get("p2_save_ollama_training", True):
                        _persist_fix_training(
                            _single_prompt, _text, _engine, source="dashboard_single"
                        )
                    st.session_state["single_result"] = {
                        "text": _text, "engine": _engine, "error": None,
                        "saved_training": st.session_state.get("p2_save_ollama_training", True),
                    }
                except Exception as exc:
                    st.session_state["single_result"] = {
                        "text": "", "engine": "", "error": str(exc)
                    }

        if "single_result" in st.session_state:
            _sr = st.session_state["single_result"]
            if _sr.get("error"):
                st.error(
                    "❌ AI 분석 중 문제가 발생했습니다.\n\n"
                    + _format_llm_error_message(_sr["error"], _sr.get("engine", ""))
                )
            elif _sr.get("text"):
                st.success("✅ 분석 완료! 아래 프롬프트를 복사하여 Claude Code에 붙여넣으세요.")
                st.caption(f"엔진: {_sr['engine']}")
                if _sr.get("saved_training"):
                    st.caption("🧠 Ollama 학습용 JSONL에 저장됨 → 메뉴 3 훈련소에서 `ollama create` 가능")
                st.markdown(_sr["text"])
                _cm = re.search(r"```(?:[\w]*\n)?([\s\S]+?)```", _sr["text"])
                if _cm:
                    st.markdown("##### 📋 Claude Code 복붙용 프롬프트")
                    st.code(_cm.group(1).strip(), language="")

        # ── 전체 에러 일괄 분석 ───────────────────────────────
        st.divider()
        st.markdown("#### 🧨 전체 에러 일괄 분석 및 마스터 프롬프트 생성")
        st.caption(
            f"**{_scope} {len(df_errors_raw):,}건**만 유형별 압축 후 LLM에 전달합니다 (6,000자 한도). "
            "분석 성공 시 기준 시각이 자동 갱신되어 같은 에러를 반복 분석하지 않습니다."
        )
        st.warning("아래 버튼을 눌러 AI에게 에러 원인 분석과 수정 코드를 요청하세요.")

        # Gemini / Groq — 2-column
        _bc1, _bc2 = st.columns(2)
        with _bc1:
            if st.button(
                "🤖 Gemini 분석",
                key="btn_batch_gemini",
                use_container_width=True,
            ):
                with st.spinner(
                    "🤖 AI가 수백 개의 에러 로그를 분석하고 마스터 프롬프트를 작성 중입니다. "
                    "(약 10~20초 소요)..."
                ):
                    try:
                        _grouped = get_grouped_errors(df_errors_raw)
                        _prompt  = _MASTER_PROMPT_TEMPLATE.format(errors=_grouped)
                        _txt, _eng = _run_llm(_prompt, force_engine="gemini")
                        if not _txt:
                            raise ValueError(f"엔진({_eng})에서 빈 응답이 반환되었습니다.")
                        if st.session_state.get("p2_save_ollama_training", True):
                            _persist_fix_training(
                                _prompt, _txt, _eng, source="dashboard_batch"
                            )
                        st.session_state["batch_result"] = {
                            "text": _txt, "engine": _eng, "error": None,
                            "saved_training": st.session_state.get("p2_save_ollama_training", True),
                        }
                        _mark_errors_resolved("dashboard_batch_analyzed")
                        st.rerun()
                    except Exception as exc:
                        st.session_state["batch_result"] = {
                            "text": "", "engine": "Gemini", "error": str(exc)
                        }

        with _bc2:
            if st.button(
                "⚡ Groq 분석",
                key="btn_batch_groq",
                use_container_width=True,
            ):
                with st.spinner(
                    "🤖 AI가 수백 개의 에러 로그를 분석하고 마스터 프롬프트를 작성 중입니다. "
                    "(약 10~20초 소요)..."
                ):
                    try:
                        _grouped = get_grouped_errors(df_errors_raw)
                        _prompt  = _MASTER_PROMPT_TEMPLATE.format(errors=_grouped)
                        _txt, _eng = _run_llm(_prompt, force_engine="groq")
                        if not _txt:
                            raise ValueError(f"엔진({_eng})에서 빈 응답이 반환되었습니다.")
                        if st.session_state.get("p2_save_ollama_training", True):
                            _persist_fix_training(
                                _prompt, _txt, _eng, source="dashboard_batch"
                            )
                        st.session_state["batch_result"] = {
                            "text": _txt, "engine": _eng, "error": None,
                            "saved_training": st.session_state.get("p2_save_ollama_training", True),
                        }
                        _mark_errors_resolved("dashboard_batch_analyzed")
                        st.rerun()
                    except Exception as exc:
                        st.session_state["batch_result"] = {
                            "text": "", "engine": "없음(Groq 강제 실패)", "error": str(exc)
                        }

        # Ollama — 전폭 버튼
        if st.button(
            "💻 맥미니 로컬 AI(Ollama)로 무제한 분석",
            key="btn_batch_ollama",
            use_container_width=True,
        ):
            with st.spinner(
                "💻 맥미니가 자체 신경망을 가동하여 분석 중입니다. "
                "(로컬 연산이므로 30~60초 정도 소요될 수 있습니다)..."
            ):
                try:
                    _grouped = get_grouped_errors(df_errors_raw)
                    _prompt  = _MASTER_PROMPT_TEMPLATE.format(errors=_grouped)
                    _txt, _eng = _run_llm(_prompt, force_engine="ollama")
                    if not _txt:
                        raise ValueError(f"엔진({_eng})에서 빈 응답이 반환되었습니다.")
                    if st.session_state.get("p2_save_ollama_training", True):
                        _persist_fix_training(
                            _prompt, _txt, _eng, source="dashboard_batch"
                        )
                    st.session_state["batch_result"] = {
                        "text": _txt, "engine": _eng, "error": None,
                        "saved_training": st.session_state.get("p2_save_ollama_training", True),
                    }
                    _mark_errors_resolved("dashboard_batch_analyzed")
                    st.rerun()
                except Exception as exc:
                    st.session_state["batch_result"] = {
                        "text": "", "engine": "Ollama", "error": str(exc)
                    }

        # 결과 표시
        if "batch_result" in st.session_state:
            _br = st.session_state["batch_result"]
            if _br.get("error"):
                st.error(
                    "❌ AI 분석 중 문제가 발생했습니다.\n\n"
                    + _format_llm_error_message(
                        _br["error"], _br.get("engine", "")
                    )
                )
            elif _br.get("text"):
                st.success("✅ 분석 완료! 아래 프롬프트를 복사하여 Claude Code에 붙여넣으세요.")
                st.caption(f"엔진: {_br['engine']}")
                if _br.get("saved_training"):
                    st.caption(
                        "🧠 Ollama 학습용 JSONL 저장됨 → "
                        f"`{_TRAINING_FILE.relative_to(_루트)}`"
                    )
                st.markdown("##### 분석 결과")
                st.markdown(_br["text"])
                _mm = re.search(r"```(?:[\w]*\n)?([\s\S]+?)```", _br["text"])
                if _mm:
                    st.markdown("##### 📋 마스터 프롬프트 (Claude Code 터미널에 복붙)")
                    st.code(_mm.group(1).strip(), language="")

            if st.button("🗑️ 결과 초기화", key="btn_batch_clear"):
                del st.session_state["batch_result"]
                st.rerun()


# PAGE 3 — 로컬 AI(Ollama) 훈련소
# ═════════════════════════════════════════════════════════════
elif menu == "🤖 AI 심야 훈련소":
    st.title("🤖 AI 심야 훈련소")

    # ─────────────────────────────────────────────────────────
    # 심야 자율 훈련 스케줄러 GUI
    # ─────────────────────────────────────────────────────────
    st.markdown("### ⏱️ 자율 훈련 스케줄러")

    _TRAIN_PID  = _루트 / "data" / "logs" / "train.pid"
    _NIGHT_LOG  = _루트 / "data" / "logs" / "training_night.log"
    _SCHED_PY   = _루트 / "scripts" / "train_scheduler.py"

    def _scheduler_pid() -> int | None:
        try:
            pid = int(_TRAIN_PID.read_text().strip())
            os.kill(pid, 0)   # 프로세스 생존 확인
            return pid
        except Exception:
            return None

    _pid = _scheduler_pid()
    _col_stat, _col_ctrl = st.columns([1, 2])
    with _col_stat:
        if _pid:
            st.success(f"🟢 가동 중 (PID {_pid})")
        else:
            st.error("🔴 대기 중")

    with _col_ctrl:
        _sc1, _sc2 = st.columns(2)
        with _sc1:
            _start_t = st.time_input("훈련 시작", value=datetime.strptime("00:00", "%H:%M"), key="sched_start")
        with _sc2:
            _end_t   = st.time_input("훈련 종료", value=datetime.strptime("08:00", "%H:%M"), key="sched_end")

    _btn1, _btn2 = st.columns(2)
    with _btn1:
        if st.button("▶️ 훈련 스케줄러 가동", use_container_width=True, disabled=bool(_pid),
                     key="btn_sched_start"):
            _start_str = _start_t.strftime("%H:%M")
            _end_str   = _end_t.strftime("%H:%M")
            _NIGHT_LOG.parent.mkdir(parents=True, exist_ok=True)
            _log_fp = open(str(_NIGHT_LOG), "a", encoding="utf-8")
            _proc = subprocess.Popen(
                [sys.executable, str(_SCHED_PY),
                 "--start", _start_str, "--end", _end_str],
                stdout=_log_fp, stderr=_log_fp,
                close_fds=True,
                start_new_session=True,
            )
            _TRAIN_PID.write_text(str(_proc.pid))
            st.success(f"✅ 스케줄러 가동 (PID={_proc.pid}, {_start_str}~{_end_str})")
            st.rerun()
    with _btn2:
        if st.button("⏹️ 훈련 강제 종료", use_container_width=True, disabled=not bool(_pid),
                     key="btn_sched_stop"):
            if _pid:
                try:
                    import signal
                    os.kill(_pid, signal.SIGTERM)
                    st.success(f"✅ PID {_pid} 종료 신호 전송")
                except Exception as _e:
                    st.warning(f"종료 실패: {_e}")
                try:
                    _TRAIN_PID.unlink(missing_ok=True)
                except Exception:
                    pass
                st.rerun()

    # 로그 뷰어
    st.markdown("#### 📋 훈련 로그 (최근 30줄)")
    _lcol1, _lcol2 = st.columns([4, 1])
    with _lcol2:
        if st.button("🔄 로그 갱신", key="btn_night_log_refresh"):
            st.rerun()
    if _NIGHT_LOG.exists():
        _log_lines = _NIGHT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        st.code("\n".join(_log_lines[-30:]), language="")
    else:
        st.info("아직 훈련 로그가 없습니다. 스케줄러를 가동하면 여기에 로그가 표시됩니다.")

    st.divider()

    # ─────────────────────────────────────────────────────────
    # Ollama 로컬 모델 Hot-Swap 훈련
    # ─────────────────────────────────────────────────────────
    st.markdown("### 🧠 Ollama 로컬 AI 즉시 학습 (Hot-Swap)")
    st.info(
        "클라우드 AI(Gemini/Groq)가 완벽하게 대답했던 과거의 성공 기록들을 모아, "
        "맥미니의 자체 AI에게 학습시킵니다. "
        "앞으로 무료 API 없이도 똑똑하게 답변할 수 있게 됩니다."
    )

    entries    = _load_training_data()
    total_pairs = len(entries)

    # ── 현황 메트릭 ───────────────────────────────────────────
    if total_pairs:
        total_chars_est = sum(
            sum(len(m.get("content", "")) for m in e.get("messages", []))
            for e in entries
        )
        _tc1, _tc2, _tc3 = st.columns(3)
        _tc1.metric("📦 누적 학습 페어",   f"{total_pairs:,}")
        _tc2.metric("🔤 추정 토큰 합계",   f"{total_chars_est // 4:,}")
        _tc3.metric(
            "💾 데이터 파일 크기",
            f"{_TRAINING_FILE.stat().st_size / 1024:.1f} KB"
            if _TRAINING_FILE.exists() else "0 KB",
        )
    else:
        st.warning(
            "아직 학습 데이터가 없습니다. "
            "봇이 Gemini/Groq로 응답할 때마다 자동으로 데이터가 쌓입니다."
        )

    # 샘플 미리보기
    if entries:
        with st.expander("📋 학습 데이터 샘플 미리보기 (최신 5건)", expanded=False):
            for i, entry in enumerate(entries[-5:][::-1], 1):
                msgs     = entry.get("messages", [])
                user_msg = next((m["content"] for m in msgs if m.get("role") == "user"),      "")
                asst_msg = next((m["content"] for m in msgs if m.get("role") == "assistant"), "")
                sys_msg  = next((m["content"] for m in msgs if m.get("role") == "system"),    "")
                title    = (
                    f"#{i} — {user_msg[:60]}…"
                    if len(user_msg) > 60 else f"#{i} — {user_msg}"
                )
                with st.expander(title):
                    if sys_msg:
                        st.caption(f"🖥️ 시스템: {sys_msg[:120]}")
                    st.markdown(f"**👤 유저:** {user_msg[:300]}")
                    st.markdown(f"**🤖 어시스턴트:** {asst_msg[:500]}")

    st.divider()

    # ── Zero-Downtime Hot-Swap 원클릭 학습 버튼 ─────────────────
    _hs = st.session_state.get("hotswap_status")

    if _hs and _hs.get("running"):
        # ── 빌드 진행 중: 폴링 루프 (2초마다 자동 갱신) ──────────
        _phase_str = _hs.get("phase", "...")
        st.info(f"🔄 **빌드 진행 중**: {_phase_str}")
        _phase_pct = {"1/3": 0.15, "2/3": 0.65, "3/3": 0.92}.get(
            _phase_str[:3], 0.05
        )
        st.progress(_phase_pct, text=_phase_str)
        st.caption("⚡ 기존 `univagent-expert:latest` 모델은 서비스 중 — 빌드 완료 후 원자적 교체")
        _rc1, _rc2 = st.columns([4, 1])
        with _rc2:
            if st.button("🔄 수동 갱신", key="btn_poll_refresh"):
                st.rerun()
        time.sleep(2)
        st.rerun()

    else:
        # ── 빌드 시작 버튼 ────────────────────────────────────────
        if st.button(
            "🚀 즉시 데이터 추출 및 로컬 AI 학습 시작 (Zero-Downtime Hot-Swap)",
            type="primary",
            key="btn_hotswap_train",
            disabled=(total_pairs == 0),
            use_container_width=True,
        ):
            _hs_err = ""

            # 1️⃣ JSONL 추출
            try:
                _EXPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
                _EXPORT_FILE.write_text(
                    "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                st.error(f"❌ JSONL 추출 실패: {exc}")
                _hs_err = str(exc)

            if not _hs_err:
                # 2️⃣ Modelfile 생성 (4000자 컷오프 + 하이퍼파라미터 고정)
                try:
                    _train_entries = [
                        json.loads(l)
                        for l in _EXPORT_FILE.read_text(encoding="utf-8").splitlines()
                        if l.strip()
                    ]
                    if not _train_entries:
                        raise ValueError("추출 파일이 비어 있습니다.")

                    _system_content = _build_modelfile_system(_train_entries, _TOP_K_PAIRS)
                    _modelfile_content = (
                        f"FROM {_BASE_MODEL}\n"
                        f"{_MODELFILE_PARAMS}"
                        f'SYSTEM """{_system_content}"""\n'
                    )
                    _MODELFILE_PATH.write_text(_modelfile_content, encoding="utf-8")
                    _sys_len = len(_system_content)
                except Exception as exc:
                    st.error(f"❌ Modelfile 생성 실패: {exc}")
                    _hs_err = str(exc)
                    _sys_len = 0

            if not _hs_err:
                # 3️⃣ 백그라운드 Hot-Swap 스레드 시작
                _status_dict: dict = {
                    "running": True,
                    "phase":   "1/3 · 준비 중…",
                    "success": False,
                    "error":   "",
                    "stdout":  "",
                    "pairs":   min(total_pairs, _TOP_K_PAIRS),
                    "sys_len": _sys_len,
                }
                st.session_state["hotswap_status"] = _status_dict
                threading.Thread(
                    target=_hotswap_pipeline,
                    args=(_status_dict, _MODELFILE_PATH),
                    daemon=True,
                ).start()
                st.rerun()

    # ── 완료 / 실패 결과 표시 ─────────────────────────────────
    if _hs and not _hs.get("running"):
        if _hs.get("success"):
            st.success(
                f"🎉 학습 완료! `{_EXPERT_MODEL_LATEST}` 으로 핫스왑되었습니다.  "
                f"(Q&A 페어: **{_hs.get('pairs', '?')}**개 · "
                f"SYSTEM 블록: **{_hs.get('sys_len', 0):,}**자 / {_SYSTEM_CONTENT_MAX:,}자)"
            )
            if _hs.get("stdout"):
                with st.expander("빌드 로그 보기", expanded=False):
                    st.code(_hs["stdout"], language="")
        else:
            st.error(
                f"❌ 학습 중 문제가 발생했습니다.\n"
                f"원인: {_hs.get('error', '알 수 없는 오류')}"
            )

        if st.button("🗑️ 결과 초기화", key="btn_hotswap_clear"):
            del st.session_state["hotswap_status"]
            st.rerun()

    st.divider()
    st.markdown("##### ℹ️ 모델 정보")
    st.markdown(
        f"- 서비스 모델: `{_EXPERT_MODEL_LATEST}` (핫스왑 완료 후 즉시 투입)\n"
        f"- 임시 빌드 태그: `{_EXPERT_MODEL_TEMP}` (빌드 중만 존재, 완료 후 자동 삭제)\n"
        f"- 폴백 베이스: `{_BASE_MODEL}` (`{_EXPERT_MODEL_LATEST}` 미존재 시 자동 전환)\n"
        f"- Modelfile 위치: `{_MODELFILE_PATH}`\n"
        f"- SYSTEM 블록 한도: `{_SYSTEM_CONTENT_MAX:,}`자 (OOM 방지 컷오프)\n"
        f"- 추론 파라미터: `temperature=0.3` · `num_ctx=8192` · `stop=\"User:\"`"
    )

    if entries:
        st.markdown("##### 📥 학습 데이터 직접 다운로드")
        _dl = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
        st.download_button(
            label="⬇️ ollama_finetune_dataset.jsonl 다운로드",
            data=_dl.encode("utf-8"),
            file_name="ollama_finetune_dataset.jsonl",
            mime="application/jsonl",
            key="btn_download",
        )


# ═════════════════════════════════════════════════════════════
# PAGE 4 — 데이터 수집 현황
# ═════════════════════════════════════════════════════════════
elif menu == "📂 데이터 관제":
    st.title("📂 데이터 관제")

    growth = _db_growth_metrics()

    def _pct_str_p4(delta: int, total: int) -> str:
        if total > 0 and delta > 0:
            return f"+{delta:,}건 ({delta / total * 100:.1f}%↑ 주간)"
        return f"+{delta:,}건 (주간)"

    # ── 누적 현황 메트릭 ─────────────────────────────────────
    st.markdown("### 📊 누적 데이터 현황")
    _p4c1, _p4c2, _p4c3 = st.columns(3)
    _p4c1.metric(
        label="📚 세특 데이터 (누적)",
        value=f"{growth.get('seteuk_total', 0):,}건",
        delta=_pct_str_p4(growth.get("seteuk_week", 0), growth.get("seteuk_total", 1)),
        delta_color="normal",
    )
    _p4c2.metric(
        label="🏫 입시통계 (누적)",
        value=f"{growth.get('stats_total', 0):,}건",
        delta=_pct_str_p4(growth.get("stats_week", 0), growth.get("stats_total", 1)),
        delta_color="normal",
    )
    _p4c3.metric(
        label="📄 골든문서 PDF (누적)",
        value=f"{growth.get('golden_total', 0):,}건",
        delta=_pct_str_p4(growth.get("golden_week", 0), growth.get("golden_total", 1)),
        delta_color="normal",
    )

    _p4m1, _p4m2, _p4m3, _p4m4 = st.columns(4)
    _p4m1.metric("세특 월간 증가",    f"+{growth.get('seteuk_month', 0):,}건",   delta_color="off")
    _p4m2.metric("입시통계 월간 증가", f"+{growth.get('stats_month', 0):,}건",    delta_color="off")
    _p4m3.metric("🛡️ 쉴드방어 (주간)", f"{growth.get('shield_week', 0)}회",      delta_color="off")
    _p4m4.metric(
        "🤖 E2E 테스트",
        growth.get("e2e", "미실행"),
        delta=f"데이터 {growth.get('rows', 0)}일치",
        delta_color="off",
    )

    st.divider()

    # ── 최근 7일 일별 수집 증가량 시계열 차트 ────────────────
    st.markdown("### 📈 최근 7일 일별 수집 증가량")

    _daily_df = _db_daily_growth(days=7)

    if _daily_df.empty:
        st.info(
            "아직 7일치 수집 이력이 없습니다. "
            "`devops_reporter.py` 또는 `db_manager.시스템_스냅샷_저장()`이 "
            "매일 실행되어야 그래프가 채워집니다."
        )
    else:
        # Plotly 라인 차트: 세특·입시통계 비교
        _chart_df = _daily_df.rename(columns={
            "seteuk_daily": "세특 증가",
            "stats_daily":  "입시통계 증가",
        })
        _fig_daily = px.line(
            _chart_df,
            x="date",
            y=["세특 증가", "입시통계 증가"],
            markers=True,
            labels={"date": "날짜", "value": "일별 증가량 (건)", "variable": "데이터 종류"},
            color_discrete_map={"세특 증가": "#3B82F6", "입시통계 증가": "#10B981"},
            height=350,
        )
        _fig_daily.update_layout(
            margin=dict(l=0, r=0, t=30, b=0),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            xaxis_tickformat="%m/%d",
        )
        _fig_daily.update_traces(line_width=2.5, marker_size=7)
        st.plotly_chart(_fig_daily, use_container_width=True)

        # 수치 테이블
        with st.expander("📋 일별 수치 상세 보기", expanded=False):
            _tbl = _daily_df.copy()
            _tbl["date"] = _tbl["date"].dt.strftime("%Y-%m-%d")
            _tbl.columns = ["날짜", "세특 증가", "입시통계 증가"]
            st.dataframe(_tbl, use_container_width=True, hide_index=True)

    st.divider()

    # ── 🧠 AI 자가 학습 데이터 포집 현황 (Golden Dataset) ──────
    st.markdown("### 🧠 AI 자가 학습 데이터 포집 현황 (Golden Dataset)")
    try:
        import db_manager as _dbm_gd
        _gd_stats = _dbm_gd.get_golden_dataset_stats()
        _gd_total     = _gd_stats.get("total", 0)
        _gd_trained   = _gd_stats.get("trained", 0)
        _gd_untrained = _gd_stats.get("untrained", 0)
        _gd_src       = _gd_stats.get("by_source", {})
        _gd_synthetic = _gd_src.get("synthetic", 0)
        _gd_verified  = _gd_src.get("verified_by_gemini", _gd_src.get("verified", 0))

        _gc1, _gc2, _gc3, _gc4 = st.columns(4)
        _gc1.metric("📦 전체 황금 데이터", f"{_gd_total:,}건")
        _gc2.metric("🤖 시뮬레이터 (합성)", f"{_gd_synthetic:,}건",
                    delta=f"source=synthetic")
        _gc3.metric("✅ Gemini 검증 완료", f"{_gd_verified:,}건",
                    delta=f"source=verified")
        _gc4.metric("🎓 Ollama 학습 완료", f"{_gd_trained:,}건",
                    delta=f"미사용: {_gd_untrained:,}건", delta_color="off")

        if _gd_src:
            _src_df = {"출처": list(_gd_src.keys()), "건수": list(_gd_src.values())}
            import pandas as _pd_gd
            _src_frame = _pd_gd.DataFrame(_src_df)
            st.bar_chart(_src_frame.set_index("출처"))
        else:
            st.info("아직 Golden Dataset 데이터가 없습니다. auto_simulator.py 또는 gemini_verifier.py를 실행하세요.")
    except Exception as _gd_err:
        st.warning(f"Golden Dataset 통계 로드 실패: {_gd_err}")

    # ── 🔋 일일 시뮬레이터 토큰 예산 게이지 ─────────────────────
    st.subheader("🔋 일일 시뮬레이터 토큰 예산")
    _SIM_DAILY_LIMIT = 50_000
    try:
        import db_manager as _dbm_sq
        _sq_usage = _dbm_sq.get_today_simulator_usage()
        _sq_tokens = int(_sq_usage.get("tokens_used", 0) or 0)
        _sq_runs   = int(_sq_usage.get("runs_completed", 0) or 0)
        _sq_pct    = min(_sq_tokens / _SIM_DAILY_LIMIT, 1.0)
        _sq_remain = max(0, _SIM_DAILY_LIMIT - _sq_tokens)

        _sq_color = "정상 🟢" if _sq_pct < 0.7 else ("주의 🟡" if _sq_pct < 0.9 else "한도 임박 🔴")
        st.progress(
            _sq_pct,
            text=(
                f"사용량: {_sq_tokens:,} / {_SIM_DAILY_LIMIT:,} Tokens "
                f"({_sq_runs}회 완료) — {_sq_color} — "
                f"잔여: {_sq_remain:,} tokens"
            ),
        )
        _sq_c1, _sq_c2, _sq_c3 = st.columns(3)
        _sq_c1.metric("오늘 사용 토큰",  f"{_sq_tokens:,}",           delta=f"{_sq_pct*100:.1f}%")
        _sq_c2.metric("완료 시뮬레이션", f"{_sq_runs}회")
        _sq_c3.metric("잔여 토큰",       f"{_sq_remain:,}",
                      delta="한도 초과" if _sq_pct >= 1.0 else None,
                      delta_color="inverse" if _sq_pct >= 1.0 else "off")
    except Exception as _sq_err:
        st.warning(f"시뮬레이터 토큰 사용량 로드 실패: {_sq_err}")

    st.divider()

    # ── 데이터 수집 파이프라인 상태 요약 ────────────────────
    st.markdown("### 🔗 수집 파이프라인 요약")
    st.markdown(
        "| 소스 | 방식 | 주기 | 저장 위치 |\n"
        "|------|------|------|-----------|\n"
        "| 세특 활동 | `seed_seteuk_db.py` (Groq→Ollama) | 12시간 | `successful_seteuks` 테이블 |\n"
        "| 입시통계  | `seed_admissions_stats.py` | 12시간 | `admissions_stats` 테이블 |\n"
        "| PDF 수집  | `pdf_collector.py` | 이벤트 기반 | `golden_documents` 테이블 |\n"
        "| 시스템 스냅샷 | `db_manager.시스템_스냅샷_저장()` | devops 리포트 주기 | `system_metrics` 테이블 |"
    )

    st.divider()

    # ── 유저 이탈률 퍼널 (통합) ──────────────────────────────
    st.markdown("### 📉 유저 전환 퍼널")
    try:
        import dashboard_utils as _du_p4
        _funnel = _du_p4.get_user_funnel()
    except Exception:
        _funnel = {"stage1": 1, "stage2": 0, "stage3": 0, "drop12": 0.0, "drop23": 0.0}

    _s1 = max(_funnel.get("stage1", 1), 1)
    _s2 = _funnel.get("stage2", 0)
    _s3 = _funnel.get("stage3", 0)
    _d12 = _funnel.get("drop12", 0.0)
    _d23 = _funnel.get("drop23", 0.0)
    _fc1, _fc2, _fc3 = st.columns(3)
    _fc1.metric("👤 프로필 시작",    f"{_s1}명",  delta="100%",         delta_color="off")
    _fc2.metric("📝 성적 입력",      f"{_s2}명",  delta=f"-{_d12:.1f}% 이탈", delta_color="inverse")
    _fc3.metric("📄 처방전 발급",    f"{_s3}건",  delta=f"-{_d23:.1f}% 이탈", delta_color="inverse")
    try:
        _f_df = pd.DataFrame({
            "단계":  ["1. 프로필 시작", "2. 성적 입력", "3. 처방전 발급"],
            "사용자": [_s1, _s2, _s3],
        })
        _fig_f = px.funnel(_f_df, x="사용자", y="단계", height=240)
        _fig_f.update_layout(showlegend=False, margin=dict(l=0, r=0, t=20, b=0))
        st.plotly_chart(_fig_f, use_container_width=True)
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════
# PAGE 5 — AI 엔진 및 라우팅 상태 (dead code — 종합 상황판에 통합됨)
# ═════════════════════════════════════════════════════════════
elif menu == "__엔진_상태__":  # 종합 상황판에 통합됨
    st.title("🔀 AI 엔진 및 라우팅 상태")

    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        import dashboard_utils as _du
        _engine = _du.get_engine_status()
        _daily  = _du.get_engine_daily_counts()
    except Exception as _e:
        st.error(f"엔진 상태 로드 실패: {_e}")
        _engine = {"gemini": "알 수 없음", "groq": "알 수 없음", "ollama": "알 수 없음",
                   "gemini_last_error": None, "groq_last_error": None}
        _daily  = {"gemini": 0, "groq": 0, "ollama": 0, "_pdf_today": 0}

    # ── 3열 엔진 상태 카드 ────────────────────────────────────
    st.markdown("### ⚡ 실시간 엔진 상태")
    _e1, _e2, _e3 = st.columns(3)

    def _engine_card(col, name: str, label: str, status: str, last_err=None):
        with col:
            st.markdown(f"**{label}**")
            if "Online" in status:
                st.success(status)
            elif "Rate Limited" in status:
                st.error(status)
                if last_err:
                    st.caption(f"마지막 오류: {last_err}")
            else:
                st.warning(status)

    _engine_card(_e1, "gemini", "🔷 Gemini (Flash/Pro)",
                 _engine.get("gemini", "알 수 없음"),
                 _engine.get("gemini_last_error"))
    _engine_card(_e2, "groq",   "⚡ Groq (크롤링 1순위)",
                 _engine.get("groq",   "알 수 없음"),
                 _engine.get("groq_last_error"))
    _engine_card(_e3, "ollama", "🖥️ Ollama (로컬)",
                 _engine.get("ollama", "알 수 없음"))

    st.divider()

    # ── 라우팅 정책 요약 ─────────────────────────────────────
    st.markdown("### 🗺️ 엔진 라우팅 정책")
    st.markdown(
        "| 작업 유형 | 1순위 | 2순위 | 3순위 |\n"
        "|-----------|-------|-------|-------|\n"
        "| 코드 수정 / 자가치유 | Gemini | — | — |\n"
        "| 크롤링 / 시딩 / PDF | Groq | Ollama | — |\n"
        "| 일반 챗봇 응답 | Gemini | Groq | Ollama |\n"
        "| 야간 검증 (Verifier) | Gemini | — | — |"
    )

    st.divider()

    # ── 오늘 엔진별 생성량 차트 ───────────────────────────────
    st.markdown("### 📊 오늘 엔진별 사용량 (추정)")
    _chart_data = {
        "엔진": ["Gemini", "Groq", "Ollama"],
        "생성량(K tokens)": [
            _daily.get("gemini", 0),
            _daily.get("groq",   0),
            _daily.get("ollama", 0),
        ],
    }
    try:
        import pandas as _pd_e
        _e_df = _pd_e.DataFrame(_chart_data).set_index("엔진")
        st.bar_chart(_e_df, use_container_width=True)
    except Exception:
        for nm, val in zip(_chart_data["엔진"], _chart_data["생성량(K tokens)"]):
            st.write(f"- {nm}: {val}K tokens")

    _pdf_today = _daily.get("_pdf_today", 0)
    st.info(f"오늘 시스템 이벤트 기준 PDF 발급: **{_pdf_today}건**")

    # ── 최근 API 에러 로그 ───────────────────────────────────
    st.divider()
    st.markdown("### 🔴 최근 API 오류 (최근 1시간)")
    try:
        from datetime import timedelta
        _err_cutoff = datetime.now() - timedelta(hours=1)
        _raw_errors = _du._load_ai_errors()
        _recent = [
            r for r in _raw_errors
            if datetime.fromisoformat((r.get("timestamp") or "")[:19]) >= _err_cutoff
        ] if _raw_errors else []
        if _recent:
            for _r in _recent[-5:]:
                _ts  = _r.get("timestamp", "")[:19]
                _typ = _r.get("error_type", "Error")
                _msg = str(_r.get("error_message", ""))[:120]
                st.warning(f"**[{_ts}] {_typ}**: {_msg}...")
        else:
            st.success("✅ 최근 1시간 내 API 오류 없음")
    except Exception as _ee:
        st.caption(f"오류 로그 파싱 실패: {_ee}")


# ═════════════════════════════════════════════════════════════
# PAGE 6 — 유저 이탈률 (Funnel)
# ═════════════════════════════════════════════════════════════
elif menu == "__퍼널__":  # 데이터 관제에 통합됨
    st.title("📉 유저 이탈률 트래커 (Funnel)")

    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        import dashboard_utils as _du
        _funnel = _du.get_user_funnel()
    except Exception as _fe:
        st.error(f"퍼널 데이터 로드 실패: {_fe}")
        _funnel = {"stage1": 1, "stage2": 0, "stage3": 0, "drop12": 0.0, "drop23": 0.0}

    _s1 = max(_funnel.get("stage1", 1), 1)
    _s2 = _funnel.get("stage2", 0)
    _s3 = _funnel.get("stage3", 0)
    _d12 = _funnel.get("drop12", 0.0)
    _d23 = _funnel.get("drop23", 0.0)

    # ── 핵심 지표 카드 ────────────────────────────────────────
    st.markdown("### 📊 전환율 요약")
    _fc1, _fc2, _fc3 = st.columns(3)
    _fc1.metric("👤 1단계: 프로필 시작",    f"{_s1}명",  delta="100%",         delta_color="off")
    _fc2.metric("📝 2단계: 학과/성적 입력", f"{_s2}명",
                delta=f"-{_d12:.1f}% 이탈", delta_color="inverse")
    _fc3.metric("📄 3단계: 처방전 발급",    f"{_s3}건",
                delta=f"-{_d23:.1f}% 이탈", delta_color="inverse")

    st.divider()

    # ── 퍼널 시각화 ──────────────────────────────────────────
    st.markdown("### 📉 퍼널 차트")
    _funnel_labels = ["1. 프로필 시작", "2. 학과/성적 입력", "3. 처방전 발급"]
    _funnel_values = [_s1, _s2, _s3]
    _funnel_pcts   = [
        100.0,
        round(_s2 / _s1 * 100, 1),
        round(_s3 / _s1 * 100, 1),
    ]

    try:
        import plotly.express as _px_f
        import pandas as _pd_f
        _f_df = _pd_f.DataFrame({
            "단계":  _funnel_labels,
            "사용자": _funnel_values,
            "전환율(%)": _funnel_pcts,
        })
        _fig_f = _px_f.funnel(
            _f_df, x="사용자", y="단계", color="단계",
            title="UnivAgent 유저 전환 퍼널",
            labels={"사용자": "유저 수"},
            color_discrete_sequence=["#3B82F6", "#10B981", "#F59E0B"],
        )
        _fig_f.update_layout(
            showlegend=False,
            margin=dict(l=0, r=0, t=40, b=0),
            height=320,
        )
        st.plotly_chart(_fig_f, use_container_width=True)
    except ImportError:
        # plotly 없을 때 bar_chart 폴백
        try:
            import pandas as _pd_f2
            _fb_df = _pd_f2.DataFrame(
                {"전환율(%)": _funnel_pcts}, index=_funnel_labels
            )
            st.bar_chart(_fb_df, use_container_width=True)
        except Exception:
            for lbl, val, pct in zip(_funnel_labels, _funnel_values, _funnel_pcts):
                st.write(f"- **{lbl}**: {val}명 ({pct}%)")

    st.divider()

    # ── 이탈률 분석 ──────────────────────────────────────────
    st.markdown("### 🔍 이탈률 분석")
    _a1, _a2 = st.columns(2)
    with _a1:
        st.markdown("#### 1단계 → 2단계 이탈")
        _lost12 = _s1 - _s2
        if _d12 == 0:
            st.success(f"이탈 없음 ✅ (전환율 100%)")
        elif _d12 < 30:
            st.info(f"**{_d12:.1f}%** 이탈 — {_lost12}명 미입력\n\n_학과·성적 입력 유도 메시지 강화 검토_")
        else:
            st.warning(f"**{_d12:.1f}%** 이탈 ⚠️ — {_lost12}명 미입력\n\n_프로필 위자드 UX 개선 필요_")

    with _a2:
        st.markdown("#### 2단계 → 3단계 이탈")
        _lost23 = _s2 - _s3
        if _d23 == 0:
            st.success(f"이탈 없음 ✅ (전환율 100%)")
        elif _d23 < 40:
            st.info(f"**{_d23:.1f}%** 이탈 — {_lost23}명 미발급\n\n_처방전 발급 버튼 UX 검토_")
        else:
            st.warning(f"**{_d23:.1f}%** 이탈 ⚠️ — {_lost23}명 미발급\n\n_AI 진단 CTA 강화 필요_")

    st.divider()
    st.caption(
        "데이터 출처: `students` 테이블 (프로필 시작·성적 입력) | "
        "`system_events.jsonl` PDF_GOLDEN_SAVED 이벤트 (처방전 발급)"
    )


# ═════════════════════════════════════════════════════════════
# PAGE 7 — 야간 빌드 및 무결성
# ═════════════════════════════════════════════════════════════
elif menu == "__야간빌드__":  # 시스템 로그에 통합됨
    st.title("💾 야간 빌드 및 무결성 체크")
    st.caption(f"기준 날짜: {datetime.now().strftime('%Y-%m-%d')} | 24시간 이내 완료 여부 확인")

    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        import dashboard_utils as _du
        _nightly = _du.get_nightly_build_status()
    except Exception as _ne:
        st.error(f"빌드 상태 로드 실패: {_ne}")
        _nightly = {"backup_ok": False, "backup_path": None, "golden_ok": False,
                    "golden_count_today": 0, "modelfile_ok": False,
                    "modelfile_path": None, "backup_age_h": None}

    _nb_ok    = _nightly.get("backup_ok",    False)
    _nb_path  = _nightly.get("backup_path",  None)
    _nb_age   = _nightly.get("backup_age_h", None)
    _gd_ok    = _nightly.get("golden_ok",    False)
    _gd_cnt   = _nightly.get("golden_count_today", 0)
    _mf_ok    = _nightly.get("modelfile_ok", False)
    _mf_path  = _nightly.get("modelfile_path", None)

    all_ok = _nb_ok and _gd_ok and _mf_ok

    if all_ok:
        st.success("✅ 오늘 모든 야간 빌드 항목이 정상 완료되었습니다!")
    else:
        missing = []
        if not _nb_ok: missing.append("DB 백업")
        if not _gd_ok: missing.append("시뮬레이터 포집")
        if not _mf_ok: missing.append("Ollama Modelfile")
        st.warning(f"⚠️ 미완료 항목: **{', '.join(missing)}** — 아래 체크리스트를 확인하세요.")

    st.divider()

    # ── 체크리스트 ────────────────────────────────────────────
    st.markdown("### ✅ 오늘 야간 빌드 체크리스트")

    # ── 항목 1: DB 백업 ────────────────────────────────────────
    with st.container():
        _nb_c1, _nb_c2 = st.columns([1, 4])
        with _nb_c1:
            if _nb_ok:
                st.success("✅ 완료")
            else:
                st.error("❌ 미완료")
        with _nb_c2:
            st.markdown("**💾 DB 백업 파일 최신화**")
            if _nb_ok and _nb_path:
                _age_str = f" ({_nb_age:.1f}시간 전)" if _nb_age is not None else ""
                st.caption(f"파일: `{_nb_path}`{_age_str}")
            else:
                st.caption("오늘 백업 파일 없음 — `python3 scripts/daily_backup.py` 실행 필요")
                st.warning("24시간 이내 백업 없음", icon="🔴")

    st.divider()

    # ── 항목 2: Golden Dataset ─────────────────────────────────
    with st.container():
        _gd_c1, _gd_c2 = st.columns([1, 4])
        with _gd_c1:
            if _gd_ok:
                st.success("✅ 완료")
            else:
                st.error("❌ 미완료")
        with _gd_c2:
            st.markdown("**🤖 시뮬레이터 (Golden Dataset) 자동 포집**")
            if _gd_ok:
                st.caption(f"오늘 신규 {_gd_cnt}건 포집 완료")
            else:
                st.caption("오늘 포집 데이터 없음 — `python3 scripts/auto_simulator.py` 실행 필요")
                st.warning("시뮬레이터 미실행 감지", icon="🔴")

    st.divider()

    # ── 항목 3: Ollama Modelfile ────────────────────────────────
    with st.container():
        _mf_c1, _mf_c2 = st.columns([1, 4])
        with _mf_c1:
            if _mf_ok:
                st.success("✅ 완료")
            else:
                st.error("❌ 미완료")
        with _mf_c2:
            st.markdown("**🧠 Ollama Modelfile 업데이트**")
            if _mf_ok and _mf_path:
                st.caption(f"파일: `{_mf_path}`")
            else:
                st.caption("오늘 Modelfile 없음 — `python3 scripts/nightly_train.py` 실행 필요")
                st.warning("Ollama 모델 미업데이트", icon="🔴")

    st.divider()

    # ── 수동 실행 안내 ────────────────────────────────────────
    st.markdown("### 🔧 수동 실행 명령어")
    st.code(
        "# 1. DB 백업\n"
        "python3 scripts/daily_backup.py\n\n"
        "# 2. 시뮬레이터 실행 (합성 데이터 10건 생성)\n"
        "python3 scripts/auto_simulator.py --count 10\n\n"
        "# 3. Ollama 모델 업데이트\n"
        "python3 scripts/nightly_train.py\n\n"
        "# 전체 재시작 (위 3개 자동 포함)\n"
        "./restart_agent.sh",
        language="bash",
    )
