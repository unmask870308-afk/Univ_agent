"""
gemini_verifier.py — Ollama→Gemini 비동기 사후 검증 + 황금 QA 수집기
======================================================================
pending_verifications 테이블에 쌓인 Ollama 초안 답변을 Gemini로 개선하고,
(질문, Ollama 초안, Gemini 개선본) 삼중 쌍을 golden_dataset에 저장합니다.

실행 방법:
    python3 scripts/gemini_verifier.py              # 1회 배치 처리
    python3 scripts/gemini_verifier.py --loop       # 60초 간격 무한 루프
    python3 scripts/gemini_verifier.py --limit 20   # 처리 한도 지정 (기본 10)
"""

from __future__ import annotations

import argparse
import logging
import os
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
logger = logging.getLogger("gemini_verifier")

import db_manager
import token_manager as _tm

# ─────────────────────────────────────────────────────────────
# 시스템 프롬프트
# ─────────────────────────────────────────────────────────────

_VERIFIER_SYSTEM = (
    "CRITICAL SYSTEM RULE: You MUST write the ENTIRE response in 100% Korean (한국어). "
    "ABSOLUTELY NO ENGLISH ALLOWED.\n\n"
    "당신은 대한민국 최고의 대학 입시 전문 컨설턴트입니다. "
    "아래 학생 질문에 대해 정확하고 구체적이며 신뢰할 수 있는 입시 조언을 "
    "한국어로만 작성하세요. "
    "핵심 위주로 명확하게 답변하세요."
)

_QUALITY_SYSTEM = (
    "You are a quality evaluator for Korean college admission advice. "
    "Rate the quality of the answer on a scale of 0-100. "
    "Return ONLY a JSON object: {\"score\": <int>, \"reason\": \"<brief_reason>\"}"
)


def _call_gemini_upgrade(query: str, ollama_draft: str) -> tuple[str, int]:
    """
    Gemini로 질문에 대한 정답을 생성하고 (gemini_answer, quality_score)를 반환합니다.
    token_manager.generate_text_sync 의 3-Tier에서 Gemini를 직접 호출합니다.
    """
    prompt = (
        f"[학생 질문]\n{query}\n\n"
        f"[이전 AI 답변 (참고용, 개선 필요)]\n{ollama_draft[:500]}\n\n"
        "위 질문에 대해 더 정확하고 구체적인 입시 조언을 작성하세요."
    )

    answer, engine = _tm.generate_text_sync(prompt, _VERIFIER_SYSTEM, force_engine="gemini")

    if not answer or "Ollama" in engine:
        logger.warning(f"[Verifier] Gemini 호출 실패, 엔진={engine}")
        return "", 0

    # 간단한 품질 점수: 길이 기반 (실제 LLM 품질 평가는 비용 절감을 위해 생략)
    quality = min(100, max(10, len(answer) // 10))
    logger.info(f"[Verifier] 개선 답변 생성: {len(answer)}자, engine={engine}, score={quality}")
    return answer, quality


def run_verification_batch(limit: int = 10) -> int:
    """
    pending_verifications 에서 최대 limit 개를 처리합니다.
    처리된 건수를 반환합니다.
    """
    pending = db_manager.pending_verifications_조회(status="pending", limit=limit)
    if not pending:
        logger.info("[Verifier] 처리할 pending 항목 없음")
        return 0

    logger.info(f"[Verifier] {len(pending)}개 항목 처리 시작")
    processed = 0

    for row in pending:
        row_id       = row["id"]
        query        = row["query"]
        ollama_draft = row["ollama_answer"]

        try:
            gemini_answer, quality = _call_gemini_upgrade(query, ollama_draft)

            if gemini_answer:
                # pending_verifications 완료 처리
                db_manager.pending_verification_완료(row_id, gemini_answer)

                # ── 핵심: golden_dataset에 황금 QA 쌍 저장 ──────────────
                db_manager.save_golden_qa(
                    user_query=query,
                    ollama_response=ollama_draft,
                    gemini_response=gemini_answer,
                    source="verified_by_gemini",
                    quality_score=quality,
                )
                logger.info(
                    f"[Verifier] ✅ id={row_id} 완료 → golden_dataset 저장 (score={quality})"
                )
            else:
                db_manager.pending_verification_실패(row_id)
                logger.warning(f"[Verifier] id={row_id} Gemini 응답 없음 → 실패 처리")

        except Exception as e:
            logger.error(f"[Verifier] id={row_id} 처리 실패: {e}", exc_info=True)
            try:
                db_manager.pending_verification_실패(row_id)
            except Exception:
                pass

        processed += 1
        time.sleep(1.5)  # API rate limit 방지

    logger.info(f"[Verifier] 배치 완료: {processed}개 처리")
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini 사후 검증기")
    parser.add_argument("--loop",  action="store_true", help="60초 간격 무한 루프 모드")
    parser.add_argument("--limit", type=int, default=10, help="배치당 처리 한도 (기본 10)")
    args = parser.parse_args()

    db_manager.init_db()
    logger.info(f"[Verifier] 시작 (loop={args.loop}, limit={args.limit})")

    if args.loop:
        while True:
            try:
                run_verification_batch(args.limit)
            except KeyboardInterrupt:
                logger.info("[Verifier] 종료")
                break
            except Exception as e:
                logger.error(f"[Verifier] 루프 오류: {e}")
            time.sleep(60)
    else:
        count = run_verification_batch(args.limit)
        print(f"✅ 검증 완료: {count}개 처리")


if __name__ == "__main__":
    main()
