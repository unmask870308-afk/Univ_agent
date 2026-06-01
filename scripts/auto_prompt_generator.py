"""
auto_prompt_generator.py — UnivAgent 자동 수정 프롬프트 생성기
==============================================================
data/fix_error/ 의 모든 .log 파일을 집계 → Gemini 분석
→ [성공] claude_fix_prompt.txt 저장 + 소스 로그 초기화
→ [실패] manual_error_summary.txt 저장 (소스 로그 보존)

실행 방법:
    python scripts/auto_prompt_generator.py
"""

import os
import sys
import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# 의존성 자동 설치
# ─────────────────────────────────────────────────────────────
_REQUIRED = {
    "dotenv":       "python-dotenv",
    "google.genai": "google-genai",
    "groq":         "groq",
    "ollama":       "ollama",
}
_venv_py = Path(__file__).parent.parent / "venv" / "bin" / "python3"
_pip_exe = str(_venv_py) if _venv_py.exists() else sys.executable

for _mod, _pkg in _REQUIRED.items():
    try:
        __import__(_mod.split(".")[0])
    except ImportError:
        subprocess.check_call(
            [_pip_exe, "-m", "pip", "install", _pkg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# 환경 설정
# ─────────────────────────────────────────────────────────────
프로젝트_루트 = Path(__file__).parent.parent
load_dotenv(프로젝트_루트 / ".env")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

import token_manager as _tm

FIX_ERROR_DIR      = 프로젝트_루트 / "data" / "fix_error"
OUTPUT_FILE        = FIX_ERROR_DIR / "claude_fix_prompt.txt"
MANUAL_SUMMARY_FILE = FIX_ERROR_DIR / "manual_error_summary.txt"

# 집계 대상 소스 로그 파일 (성공 시 초기화됨)
LOG_FILES = [
    "crawler_errors.log",
    "telegram_errors.log",
    "test_errors.log",
    "devops_errors.log",
]

# 파일 당 최대 보존 라인 / 문자 수 (Gemini 토큰 초과 방지)
MAX_LINES_PER_FILE = 50
MAX_CHARS_PER_FILE = 10_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("auto_prompt_generator")


# ─────────────────────────────────────────────────────────────
# 1. 로그 파일 파싱 & 집계
# ─────────────────────────────────────────────────────────────

def _로그_섹션_읽기(log_path: Path) -> str:
    """
    단일 JSONL 로그 파일을 읽어 사람이 읽기 쉬운 텍스트 블록으로 변환합니다.
    파일이 없거나 비어 있으면 빈 문자열을 반환합니다.
    """
    if not log_path.exists():
        return ""

    try:
        raw = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[로그] {log_path.name} 읽기 실패: {e}")
        return ""

    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return ""

    lines = lines[-MAX_LINES_PER_FILE:]

    formatted: list[str] = []
    for line in lines:
        try:
            entry = json.loads(line)
            ts       = entry.get("ts", "")
            script   = entry.get("script", entry.get("task", "unknown"))
            err_type = entry.get("error_type", "")
            err_msg  = entry.get("error_msg", "")
            stage    = entry.get("stage", "")
            tb_raw   = entry.get("traceback", "")
            tb_lines = [l for l in tb_raw.splitlines() if l.strip()]
            tb_short = "\n    ".join(tb_lines[-3:]) if tb_lines else ""

            parts = [f"[{ts}] [{script}]"]
            if stage:
                parts.append(f"stage={stage}")
            if err_type:
                parts.append(f"{err_type}:")
            if err_msg:
                parts.append(err_msg[:300])
            if tb_short:
                parts.append(f"\n    Traceback (last 3 lines):\n    {tb_short}")
            formatted.append(" ".join(parts))
        except (json.JSONDecodeError, ValueError):
            formatted.append(line[:200])

    block = "\n".join(formatted)

    if len(block) > MAX_CHARS_PER_FILE:
        block = block[-MAX_CHARS_PER_FILE:]
        block = f"[앞부분 생략 — 최근 {MAX_CHARS_PER_FILE}자만 표시]\n" + block

    return block


def 로그_집계() -> tuple[str, dict[str, int]]:
    """
    모든 로그 파일을 읽어 통합 컨텍스트 문자열과 파일별 에러 건수를 반환합니다.
    반환값: (집계_컨텍스트_문자열, {"파일명": 에러_건수, ...})
    """
    FIX_ERROR_DIR.mkdir(parents=True, exist_ok=True)

    sections: list[str] = []
    건수_맵: dict[str, int] = {}

    # 명시된 파일 순서대로 처리
    for fname in LOG_FILES:
        path = FIX_ERROR_DIR / fname
        block = _로그_섹션_읽기(path)
        건수 = len([l for l in block.splitlines() if l and not l.startswith("[")])
        건수_맵[fname] = 건수

        if block:
            sections.append(f"=== From {fname} ({건수}건) ===\n{block}")
        else:
            sections.append(f"=== From {fname} (에러 없음 또는 파일 없음) ===")

    # fix_error 디렉토리의 추가 .log 파일 자동 포함
    known = set(LOG_FILES)
    for extra in sorted(FIX_ERROR_DIR.glob("*.log")):
        if extra.name not in known:
            block = _로그_섹션_읽기(extra)
            건수 = len([l for l in block.splitlines() if l and not l.startswith("[")])
            건수_맵[extra.name] = 건수
            if block:
                sections.append(
                    f"=== From {extra.name} ({건수}건) [자동 감지] ===\n{block}"
                )

    집계 = "\n\n".join(sections)
    총_건수 = sum(건수_맵.values())
    logger.info(f"[집계] 총 {총_건수}건 에러 집계 완료 (파일 {len(건수_맵)}개)")
    return 집계, 건수_맵


# ─────────────────────────────────────────────────────────────
# 2. Gemini 프롬프트 생성
#    반환값: (프롬프트_텍스트, 성공_여부: bool)
#    - True  = Gemini 가 실제 응답을 생성했거나 에러가 없는 정상 상태
#    - False = Gemini API 실패 (429, 키 없음, 모든 모델 소진 등)
# ─────────────────────────────────────────────────────────────

def Gemini_수정_프롬프트_생성(
    집계_컨텍스트: str,
    건수_맵: dict[str, int],
) -> tuple[str, bool]:
    """
    집계된 에러 컨텍스트를 Gemini 에 전송하여 Claude Code 자율 수정 프롬프트를 생성합니다.

    반환: (프롬프트_텍스트, 성공_여부)
    """
    총_건수 = sum(건수_맵.values())
    오늘_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 에러 없음 → Gemini 불필요, 정상 상태로 처리
    if 총_건수 == 0:
        msg = (
            f"Please fix the following issues based on the aggregated error logs: "
            f"No errors were detected in any log file as of {오늘_str}. "
            f"All systems appear to be operating normally. "
            f"No code changes are required at this time."
        )
        logger.info("[Gemini] 에러 없음 — 정상 상태 메시지 생성 (성공 처리)")
        return msg, True

    if not GEMINI_API_KEY:
        logger.warning("[Gemini] API 키 없음 — 실패 처리")
        return (
            f"Please fix the following issues based on the aggregated error logs:\n\n"
            f"[Gemini API key not configured]\n\n{집계_컨텍스트[:3000]}",
            False,
        )

    파일_목록_str = ", ".join(
        f"{k} ({v}건)" for k, v in 건수_맵.items() if v > 0
    ) or "없음"

    시스템_프롬프트 = (
        "You are a Senior DevOps & Scraping Engineer specializing in Python automation systems. "
        "Analyze the aggregated error logs from various system components (Crawler, Telegram bot, "
        "E2E Tests, DevOps Reporter). "
        "Identify root causes, especially cascading failures where a crawler error causes a test "
        "or Telegram bot error. "
        "Be specific about file names, line numbers, and function names when they appear in tracebacks. "
        "Prioritize the most critical and frequent errors first."
    )

    사용자_프롬프트 = f"""Below are aggregated error logs from the UnivAgent system collected on {오늘_str}.
Error files analyzed: {파일_목록_str}
Total errors: {총_건수}건

--- AGGREGATED ERROR LOGS ---
{집계_컨텍스트}
--- END OF LOGS ---

Based on the above error logs, write a comprehensive and powerful English prompt for Claude Code to autonomously fix ALL identified issues across the codebase.

Requirements for your output:
1. Start your output EXACTLY with: "Please fix the following issues based on the aggregated error logs:"
2. List every distinct root cause found (not just symptoms).
3. For cascading failures (e.g., crawler error → test failure → bot error), explain the chain clearly.
4. Explicitly name every Python file that needs modification (e.g., scripts/pdf_collector.py, scripts/telegram_agent.py).
5. For each file, describe exactly what needs to change (function name, the bug, the fix).
6. If the same error type appears repeatedly (e.g., 429 rate limit), propose a structural fix (retry strategy, backoff, quota guard) not just a one-time patch.
7. Keep the prompt self-contained so Claude Code can act on it without reading the raw logs.
8. End with a section "Files to modify:" listing all affected files as a bullet list.
"""

    try:
        결과, _ = _tm.generate_text_sync(사용자_프롬프트, system_prompt=시스템_프롬프트, force_engine="gemini")
        if not 결과:
            logger.error("[TokenManager] Gemini 코드 분석 실패 — 실패 처리")
            return (
                f"Please fix the following issues based on the aggregated error logs:\n\n"
                f"[LLM 모든 티어 소진 — raw aggregated context]\n\n{집계_컨텍스트[:4000]}",
                False,
            )
        logger.info(f"[TokenManager] 프롬프트 생성 완료 ({len(결과)}자)")
        _tm.save_error_fix_for_training(
            사용자_프롬프트,
            결과,
            system_prompt=시스템_프롬프트,
            source="auto_prompt_generator",
        )
        return 결과, True

    except Exception as e:
        logger.error(f"[TokenManager] 예외: {e}")
        return (
            f"Please fix the following issues based on the aggregated error logs:\n\n"
            f"[LLM 예외: {e}]\n\n{집계_컨텍스트[:4000]}",
            False,
        )


# ─────────────────────────────────────────────────────────────
# 3. 출력 파일 저장
# ─────────────────────────────────────────────────────────────

def 프롬프트_저장(프롬프트: str) -> Path:
    """생성된 프롬프트를 claude_fix_prompt.txt 에 덮어씁니다."""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(프롬프트, encoding="utf-8")
    logger.info(f"[저장] {OUTPUT_FILE} ({len(프롬프트)}자)")
    return OUTPUT_FILE


def 로그_초기화() -> None:
    """Gemini 성공 후 소스 로그 파일 4개를 안전하게 비웁니다 (삭제 아님)."""
    for fname in LOG_FILES:
        path = FIX_ERROR_DIR / fname
        if path.exists():
            try:
                path.open("w").close()
                logger.info(f"[초기화] {fname} 클리어 완료")
            except Exception as e:
                logger.warning(f"[초기화] {fname} 클리어 실패: {e}")


def 수동_요약_저장(집계_컨텍스트: str) -> Path:
    """
    Gemini 실패 시 raw 집계 내용을 manual_error_summary.txt 에 저장합니다.
    소스 로그 파일은 건드리지 않습니다.
    """
    경고_헤더 = (
        "WARNING: Gemini API Quota Exceeded. "
        "Claude could not generate a prompt. "
        "Please review these raw errors manually.\n"
        f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        + "=" * 60 + "\n\n"
    )
    내용 = 경고_헤더 + 집계_컨텍스트
    MANUAL_SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MANUAL_SUMMARY_FILE.write_text(내용, encoding="utf-8")
    logger.info(f"[수동요약] {MANUAL_SUMMARY_FILE} 저장 완료 ({len(내용)}자)")
    return MANUAL_SUMMARY_FILE


# ─────────────────────────────────────────────────────────────
# 4. 메인
# ─────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info("  UnivAgent 자동 수정 프롬프트 생성기 시작")
    logger.info("=" * 60)

    # Step 1: 모든 에러 로그 집계
    logger.info("[1/3] 에러 로그 집계 중...")
    집계_컨텍스트, 건수_맵 = 로그_집계()

    총_건수 = sum(건수_맵.values())
    for fname, cnt in 건수_맵.items():
        logger.info(f"       {fname}: {cnt}건")
    logger.info(f"       합계: {총_건수}건")

    # Step 2: Gemini 분석
    logger.info("[2/3] Gemini 분석 및 프롬프트 생성 중...")
    프롬프트, 성공 = Gemini_수정_프롬프트_생성(집계_컨텍스트, 건수_맵)

    # Step 3: 라이프사이클 처리
    logger.info("[3/3] 결과 저장 중...")

    if 성공:
        저장_경로 = 프롬프트_저장(프롬프트)
        # 소스 로그 파일 초기화 (재분석 방지)
        if 총_건수 > 0:
            로그_초기화()
        logger.info("=" * 60)
        logger.info("  ✅ 완료! (Gemini 성공 → 로그 초기화 완료)")
        logger.info(f"  저장 위치    : {저장_경로}")
        logger.info(f"  프롬프트 길이: {len(프롬프트)}자")
        logger.info(f"  분석 에러 건수: {총_건수}건")
        logger.info("=" * 60)
        print(f"\n✅ 자동 수정 프롬프트 생성 완료!")
        print(f"   저장 위치  : {저장_경로}")
        print(f"   총 에러 건수: {총_건수}건")
        print(f"   프롬프트 길이: {len(프롬프트)}자")
        print(f"\n[생성된 프롬프트 미리보기 — 앞 300자]\n")
        print(프롬프트[:300])
        if len(프롬프트) > 300:
            print(f"\n... (이하 {len(프롬프트) - 300}자 생략, 전체 내용은 파일 확인)")
    else:
        # Gemini 실패: 소스 로그 보존, 수동 요약 저장
        수동_경로 = 수동_요약_저장(집계_컨텍스트)
        logger.info("=" * 60)
        logger.info("  ⚠️  Gemini 실패 — 소스 로그 보존, 수동 요약 저장")
        logger.info(f"  수동 요약 위치: {수동_경로}")
        logger.info(f"  분석 에러 건수: {총_건수}건 (다음 실행까지 보존)")
        logger.info("=" * 60)
        print(f"\n⚠️  Gemini API 실패 — 소스 로그 파일은 보존됩니다.")
        print(f"   수동 요약 저장: {수동_경로}")
        print(f"   총 에러 건수  : {총_건수}건")
        print(f"   다음 실행 시 Gemini 쿼터가 회복되면 자동으로 처리됩니다.")
        sys.exit(1)  # subprocess 호출자가 실패를 감지할 수 있도록


if __name__ == "__main__":
    main()
