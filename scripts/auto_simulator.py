"""
auto_simulator.py — AI 자가 학습 시뮬레이터 (Self-Play Loop)
=============================================================
3단계 셀프플레이 파이프라인:
  1) Gemini 가 가상 고2 학생 프로필(전공·성적)을 JSON 으로 생성
  2) Ollama(univagent-expert) 가 해당 프로필로 입시 상담 초안 작성
  3) Gemini 가 Ollama 초안을 검토·보완하여 황금 정답 생성
  4) 델타(초안 vs 정답) 를 golden_dataset 에 source='synthetic' 으로 저장

일일 토큰 예산 DAILY_TOKEN_LIMIT = 50,000 초과 시 자동 중단.

실행 방법:
    python3 scripts/auto_simulator.py              # 기본 5건
    python3 scripts/auto_simulator.py --count 10   # 10건 생성
    python3 scripts/auto_simulator.py --dry-run    # DB 저장 없이 콘솔 출력만
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("auto_simulator")

import db_manager
import token_manager as _tm

# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────

DAILY_TOKEN_LIMIT = 50_000


# ─────────────────────────────────────────────────────────────
# 시스템 프롬프트
# ─────────────────────────────────────────────────────────────

_PROFILE_GEN_SYSTEM = (
    "당신은 대한민국 고등학교 2학년 학생 데이터를 생성하는 AI입니다. "
    "반드시 JSON 형식만 출력하고, 설명이나 마크다운 코드블록을 절대 포함하지 마세요. "
    "JSON 키: major(희망전공), gpa(내신등급 1.0~6.0 소수점1자리), "
    "mock_kor(국어 모의등급 1~9), mock_math(수학 모의등급 1~9), "
    "mock_eng(영어 모의등급 1~4 절대평가), activities(비교과 한 문장)."
)

_DRAFT_SYSTEM = (
    "당신은 한국 대학 입시를 안내하는 로컬 AI 상담사입니다. "
    "학생 프로필을 보고 수시·정시 지원 전략과 추천 대학을 한국어로 간결하게 답변하세요."
)

_REVIEW_SYSTEM = (
    "당신은 대한민국 최고의 대학 입시 전문 컨설턴트입니다. "
    "아래에 로컬 AI 초안이 제공됩니다. 오류를 수정하고, 누락된 전형 정보를 보완하며, "
    "구체적인 수치(기준 내신, 수능 백분위)와 실전 전략을 추가하여 "
    "완벽한 입시 처방전을 한국어로만 작성하세요. "
    "최종 답변만 출력하고, '수정 이유' 같은 메타 텍스트는 쓰지 마세요."
)


# ─────────────────────────────────────────────────────────────
# 토큰 추정
# ─────────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """한국어 기준 약 2~3자/token."""
    return max(1, len(text) // 3)


# ─────────────────────────────────────────────────────────────
# Step 1: Gemini → 가상 학생 프로필 생성
# ─────────────────────────────────────────────────────────────

_PROFILE_PROMPT = (
    "대한민국 고등학교 2학년 가상 학생 프로필 1건을 JSON 으로 생성하세요. "
    "매번 다른 전공·성적 조합을 사용하세요. "
    "출력 예시: "
    '{"major":"컴퓨터공학과","gpa":2.5,"mock_kor":2,"mock_math":3,"mock_eng":2,'
    '"activities":"교내 소프트웨어 동아리 회장, 앱 개발 프로젝트 수상"}'
)


def _generate_profile_via_gemini() -> dict | None:
    """Gemini 로 가상 학생 프로필 JSON 을 생성합니다."""
    try:
        raw, engine = _tm.generate_text_sync(
            _PROFILE_PROMPT, _PROFILE_GEN_SYSTEM, force_engine="gemini"
        )
        if not raw:
            logger.warning("[Simulator] Gemini 프로필 생성 — 빈 응답")
            return None

        # JSON 블록 추출 (마크다운 코드펜스 무시)
        clean = re.sub(r"```[a-z]*\n?", "", raw).strip().strip("`").strip()
        # JSON 객체만 파싱
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if not m:
            logger.warning(f"[Simulator] JSON 파싱 실패: {clean[:120]}")
            return None

        profile = json.loads(m.group(0))
        required = {"major", "gpa", "mock_kor", "mock_math", "mock_eng", "activities"}
        if not required.issubset(profile.keys()):
            logger.warning(f"[Simulator] 프로필 키 부족: {profile}")
            return None

        logger.info(
            f"[Simulator] 프로필 생성 완료 (engine={engine}): "
            f"{profile['major']} / 내신 {profile['gpa']} / "
            f"모의 국어{profile['mock_kor']} 수{profile['mock_math']} 영{profile['mock_eng']}"
        )
        return profile

    except json.JSONDecodeError as e:
        logger.warning(f"[Simulator] JSON 디코드 오류: {e}")
        return None
    except Exception as e:
        logger.warning(f"[Simulator] Gemini 프로필 호출 실패: {type(e).__name__}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Step 2: Ollama(univagent-expert) → 초안 상담 보고서
# ─────────────────────────────────────────────────────────────

def _build_student_question(profile: dict) -> str:
    return (
        f"희망 전공: {profile['major']}\n"
        f"내신 등급: {profile['gpa']}등급\n"
        f"모의고사: 국어 {profile['mock_kor']}등급 / 수학 {profile['mock_math']}등급 / "
        f"영어 {profile['mock_eng']}등급\n"
        f"비교과 활동: {profile['activities']}\n\n"
        "위 학생에게 맞는 수시·정시 지원 전략과 추천 대학을 알려주세요."
    )


def _generate_draft_via_ollama(question: str) -> tuple[str, int]:
    """Ollama(univagent-expert) 로 초안을 생성합니다. (text, tokens)"""
    try:
        text, engine = _tm.generate_text_sync(
            question, _DRAFT_SYSTEM, force_engine="ollama"
        )
        text = text or ""
        tokens = _estimate_tokens(question) + _estimate_tokens(text)
        logger.info(f"[Simulator] Ollama 초안 {len(text)}자 ~{tokens}tok (engine={engine})")
        return text, tokens
    except Exception as e:
        logger.warning(f"[Simulator] Ollama 초안 실패: {type(e).__name__}: {e}")
        return "", 0


# ─────────────────────────────────────────────────────────────
# Step 3: Gemini → 초안 검토 및 황금 정답 생성
# ─────────────────────────────────────────────────────────────

def _generate_perfect_via_gemini(profile: dict, question: str, draft: str) -> tuple[str, int]:
    """Gemini 가 Ollama 초안을 검토·보완한 황금 정답을 생성합니다. (text, tokens)"""
    review_prompt = (
        f"[학생 프로필]\n"
        f"- 희망 전공: {profile['major']}\n"
        f"- 내신: {profile['gpa']}등급 / 모의: 국어{profile['mock_kor']} "
        f"수{profile['mock_math']} 영{profile['mock_eng']}\n"
        f"- 비교과: {profile['activities']}\n\n"
        f"[원본 질문]\n{question}\n\n"
        f"[로컬 AI(Ollama) 초안]\n{draft or '(초안 없음)'}\n\n"
        "위 초안을 검토하고 오류 수정·보완하여 완성된 입시 처방전을 작성하세요."
    )
    try:
        text, engine = _tm.generate_text_sync(
            review_prompt, _REVIEW_SYSTEM, force_engine="gemini"
        )
        text = text or ""
        tokens = _estimate_tokens(review_prompt) + _estimate_tokens(text)
        logger.info(f"[Simulator] Gemini 황금 정답 {len(text)}자 ~{tokens}tok (engine={engine})")
        return text, tokens
    except Exception as e:
        logger.warning(f"[Simulator] Gemini 검토 실패: {type(e).__name__}: {e}")
        return "", 0


# ─────────────────────────────────────────────────────────────
# 메인 시뮬레이션 루프
# ─────────────────────────────────────────────────────────────

def run_simulation(count: int = 5, dry_run: bool = False) -> int:
    """
    Self-Play Loop 를 count 회 실행하고 저장 성공 건수를 반환합니다.
    dry_run=True 이면 DB 저장 없이 콘솔만 출력합니다.
    """
    db_manager.init_db()
    saved = 0

    for i in range(count):
        logger.info(f"[Simulator] ── 루프 {i+1}/{count} 시작 ──")

        # ── 일일 토큰 예산 확인 ───────────────────────────────
        if not dry_run:
            usage = db_manager.get_today_simulator_usage()
            if usage["tokens_used"] >= DAILY_TOKEN_LIMIT:
                logger.warning(
                    f"[Simulator] 일일 토큰 예산 초과 "
                    f"({usage['tokens_used']:,}/{DAILY_TOKEN_LIMIT:,}). 중단합니다."
                )
                break

        # ── Step 1: Gemini 로 프로필 생성 ────────────────────
        profile = _generate_profile_via_gemini()
        if profile is None:
            logger.warning(f"[Simulator] [{i+1}] 프로필 생성 실패 — 건너뜀")
            time.sleep(2)
            continue

        question = _build_student_question(profile)
        loop_tokens = _estimate_tokens(_PROFILE_PROMPT)  # 프로필 생성 토큰 포함

        # ── Step 2: Ollama 초안 ───────────────────────────────
        ollama_draft, tok_ollama = _generate_draft_via_ollama(question)
        loop_tokens += tok_ollama

        # ── Step 3: Gemini 황금 정답 ─────────────────────────
        gemini_perfect, tok_gemini = _generate_perfect_via_gemini(
            profile, question, ollama_draft
        )
        loop_tokens += tok_gemini

        if not gemini_perfect:
            logger.warning(f"[Simulator] [{i+1}] Gemini 황금 정답 없음 — 건너뜀")
            time.sleep(2)
            continue

        # 품질 점수: Gemini 응답 길이 기반 (20~100)
        quality_score = min(100, max(20, len(gemini_perfect) // 8))

        if dry_run:
            print(f"\n{'='*60}")
            print(f"[DRY-RUN #{i+1}] {profile['major']} | 내신 {profile['gpa']}")
            print(f"질문: {question[:80]}...")
            print(f"Ollama 초안 ({len(ollama_draft)}자): {ollama_draft[:120]}...")
            print(f"Gemini 황금 ({len(gemini_perfect)}자): {gemini_perfect[:120]}...")
            print(f"품질점수: {quality_score} | 루프 토큰: ~{loop_tokens}")
            saved += 1
        else:
            # ── Step 4: Delta 저장 ────────────────────────────
            row_id = db_manager.save_golden_qa(
                user_query=question,
                source="synthetic",
                quality_score=quality_score,
                fake_profile=json.dumps(profile, ensure_ascii=False),
                ollama_draft=ollama_draft,
                gemini_perfect=gemini_perfect,
                ollama_response=ollama_draft,
                gemini_response=gemini_perfect,
            )
            if row_id:
                logger.info(
                    f"[Simulator] [{i+1}/{count}] ✅ 저장 완료 id={row_id} "
                    f"score={quality_score} ~{loop_tokens}tok"
                )
                db_manager.add_simulator_usage(tokens=loop_tokens, runs=1)
                saved += 1
            else:
                logger.warning(f"[Simulator] [{i+1}] DB 저장 실패")

        time.sleep(1.5)  # API 레이트 리밋 방지

    return saved


# ─────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="UnivAgent 자가 학습 시뮬레이터 (Gemini→Ollama→Gemini Self-Play)"
    )
    parser.add_argument(
        "--count", type=int, default=5,
        help="생성할 가상 프로필 수 (기본 5)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="DB 저장 없이 콘솔 출력만 (토큰 예산 미차감)"
    )
    args = parser.parse_args()

    logger.info(f"[Simulator] 시작 (count={args.count}, dry_run={args.dry_run})")
    if not args.dry_run:
        usage = db_manager.get_today_simulator_usage()
        logger.info(
            f"[Simulator] 오늘 사용량: {usage['tokens_used']:,}/{DAILY_TOKEN_LIMIT:,} tokens "
            f"({usage['runs_completed']}회 완료)"
        )

    saved = run_simulation(count=args.count, dry_run=args.dry_run)

    print(f"\n{'='*60}")
    print(f"✅ 시뮬레이션 완료: {saved}/{args.count}건 저장 (source='synthetic')")
    if not args.dry_run and saved > 0:
        usage_after = db_manager.get_today_simulator_usage()
        print(
            f"   오늘 누적 토큰: {usage_after['tokens_used']:,}/{DAILY_TOKEN_LIMIT:,} "
            f"({usage_after['runs_completed']}회)"
        )
        print("   → `python3 scripts/nightly_train.py` 로 Ollama 모델을 업데이트하세요.")
    print("="*60)


if __name__ == "__main__":
    main()
