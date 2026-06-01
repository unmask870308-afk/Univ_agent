"""
gemini_verifier.py — UnivAgent 비동기 사후 검증 데몬
=====================================================
Ollama(3순위 엔진)가 생성한 1차 입시 답변을 5분 간격으로
Gemini(1순위 엔진)가 팩트체크 및 논리 고도화한 후 해당 유저에게
텔레그램으로 최종 리포트를 Push 발송합니다.

흐름:
  1. pending_verifications WHERE status='pending' LIMIT 5 조회
  2. Gemini API 호출 (429 → 다음 주기로 조용히 넘김)
  3. 성공 → DB 'verified' 업데이트 + 텔레그램 Push
  4. 기타 오류 → DB 'failed' 업데이트

실행:
    python gemini_verifier.py            # 5분 간격 무한 루프
    python gemini_verifier.py --once     # 1회만 실행 후 종료
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# ── 경로 설정 ──────────────────────────────────────────────────
_루트 = Path(__file__).resolve().parent
sys.path.insert(0, str(_루트 / "scripts"))

from dotenv import load_dotenv
load_dotenv(_루트 / ".env")

import db_manager
import token_manager as _tm
import logger_factory as _lf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
_logger = logging.getLogger("gemini_verifier")

# ── 설정 ───────────────────────────────────────────────────────
_LOOP_INTERVAL_S  = 300   # 5분
_BATCH_SIZE       = 5     # 1회 처리 최대 건수
_TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN", "")
_ADMIN_ID_RAW     = os.getenv("ADMIN_TELEGRAM_ID", "0")

try:
    _ADMIN_ID = int(_ADMIN_ID_RAW)
except ValueError:
    _ADMIN_ID = 0

# ── 검증 프롬프트 ───────────────────────────────────────────────
_VERIFY_SYSTEM = (
    "당신은 대한민국 대학 입시 전문 팩트체커 AI입니다. "
    "아래에 제공되는 학생의 질문과 AI의 1차 답변을 검토하여 "
    "입시 사실 오류를 수정하고 논리를 고도화한 최종 답변을 작성하세요. "
    "한국어로 작성하되 800자 이내로 핵심만 담아 주세요. "
    "오류가 없다면 더 구체적인 수치(경쟁률, 합격선 등)를 보완하세요."
)

def _build_verify_prompt(query: str, ollama_answer: str) -> str:
    return (
        f"[학생 질문]\n{query}\n\n"
        f"[AI 1차 답변 (검증 대상)]\n{ollama_answer}\n\n"
        "[지시사항]\n"
        "위 1차 답변의 입시 팩트를 검증하고, 부정확한 내용은 수정하며, "
        "논리적 빈틈을 보완한 최종 답변을 작성해 주세요."
    )


# ── 텔레그램 Push ───────────────────────────────────────────────

async def _push_verified_answer(user_id: int, verified_answer: str) -> bool:
    """검증 완료된 최종 답변을 해당 유저에게 텔레그램으로 발송합니다."""
    if not _TELEGRAM_TOKEN:
        _logger.warning("[Verifier] TELEGRAM_TOKEN 미설정 — Push 생략")
        return False

    try:
        from telegram import Bot
        from telegram.constants import ParseMode

        push_text = (
            "✨ *\\[정밀 검증 완료\\]* 메인 AI가 보완한 최종 대입 처방전이 도착했습니다\\.\n\n"
            + _escape_md(verified_answer)
        )

        bot = Bot(token=_TELEGRAM_TOKEN)
        async with bot:
            await bot.send_message(
                chat_id=user_id,
                text=push_text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        _logger.info(f"[Verifier] Push 발송 완료 → user_id={user_id}")
        return True

    except Exception as _e:
        _logger.warning(f"[Verifier] Push 발송 실패 (user_id={user_id}): {_e}")
        return False


def _escape_md(text: str) -> str:
    """MarkdownV2 특수문자 이스케이프."""
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)


# ── 단일 행 검증 ────────────────────────────────────────────────

async def _verify_one(row: dict, loop: asyncio.AbstractEventLoop) -> str:
    """
    하나의 pending 행을 Gemini로 검증합니다.

    반환값:
        "verified"  — 성공
        "quota"     — 429 한도 초과 (다음 주기로 넘김)
        "failed"    — 기타 오류
    """
    row_id      = row["id"]
    user_id     = row["user_id"]
    query       = row.get("query", "")
    ollama_ans  = row.get("ollama_answer", "")

    prompt = _build_verify_prompt(query, ollama_ans)

    _logger.info(f"[Verifier] 검증 시작 id={row_id}  user={user_id}")

    try:
        text, engine_name = await loop.run_in_executor(
            None,
            lambda: _tm.generate_text_sync(
                prompt,
                system_prompt=_VERIFY_SYSTEM,
                force_engine="gemini",
            ),
        )
    except Exception as _e:
        err_str = str(_e)
        # 429 Resource Exhausted → quota
        if "429" in err_str or "quota" in err_str.lower() or "RESOURCE_EXHAUSTED" in err_str:
            _logger.warning(f"[Verifier] id={row_id} Gemini 한도 초과(429) — 다음 주기로 연기")
            await _lf.async_log_event(
                "VERIFIER_QUOTA",
                "gemini_verifier",
                f"Gemini 한도 초과 — id={row_id} 연기",
                level="WARNING",
                extra={"row_id": row_id, "error": err_str[:200]},
            )
            return "quota"

        _logger.error(f"[Verifier] id={row_id} Gemini 호출 오류: {_e}")
        await _lf.async_log_event(
            "VERIFIER_ERROR",
            "gemini_verifier",
            f"Gemini 호출 오류 — id={row_id}: {err_str[:200]}",
            level="ERROR",
            extra={"row_id": row_id, "error": err_str[:300]},
        )
        await loop.run_in_executor(
            None, lambda: db_manager.pending_verification_실패(row_id)
        )
        return "failed"

    if not text:
        _logger.warning(f"[Verifier] id={row_id} Gemini 빈 응답 — failed 처리")
        await loop.run_in_executor(
            None, lambda: db_manager.pending_verification_실패(row_id)
        )
        return "failed"

    # ── 성공 경로 ───────────────────────────────────────────────
    await loop.run_in_executor(
        None,
        lambda: db_manager.pending_verification_완료(row_id, text),
    )
    _logger.info(
        f"[Verifier] id={row_id} 검증 완료 "
        f"({len(text)}자, 엔진={engine_name})"
    )

    await _lf.async_log_event(
        "VERIFIER_SUCCESS",
        "gemini_verifier",
        f"검증 완료 — id={row_id}  {len(text)}자  user={user_id}",
        level="INFO",
        extra={"row_id": row_id, "user_id": user_id, "answer_len": len(text)},
    )

    push_ok = await _push_verified_answer(user_id, text)
    if not push_ok:
        _logger.warning(f"[Verifier] id={row_id} Push 실패 — DB는 verified 유지")

    return "verified"


# ── 1회 배치 실행 ───────────────────────────────────────────────

async def _run_batch(loop: asyncio.AbstractEventLoop) -> dict:
    """pending 행 최대 _BATCH_SIZE 건을 처리합니다."""
    rows = await loop.run_in_executor(
        None,
        lambda: db_manager.pending_verifications_조회(status="pending", limit=_BATCH_SIZE),
    )

    if not rows:
        _logger.info("[Verifier] 대기 중인 검증 항목 없음")
        return {"total": 0, "verified": 0, "quota": 0, "failed": 0}

    _logger.info(f"[Verifier] 배치 시작 — {len(rows)}건 처리")
    counts = {"total": len(rows), "verified": 0, "quota": 0, "failed": 0}

    for row in rows:
        outcome = await _verify_one(row, loop)
        if outcome in counts:
            counts[outcome] += 1

        # 한도 초과 시 이번 배치 즉시 중단 (이후 행들도 quota일 가능성 높음)
        if outcome == "quota":
            _logger.info("[Verifier] Quota 감지 — 배치 조기 종료")
            break

    _logger.info(
        f"[Verifier] 배치 완료 — "
        f"verified={counts['verified']} / quota={counts['quota']} / failed={counts['failed']}"
    )
    return counts


# ── 메인 루프 ───────────────────────────────────────────────────

async def _main_async(once: bool = False) -> None:
    loop = asyncio.get_event_loop()

    if once:
        _logger.info("[Verifier] 1회 실행 모드")
        counts = await _run_batch(loop)
        _print_counts(counts)
        return

    _logger.info(
        f"[Verifier] 데몬 시작 — {_LOOP_INTERVAL_S}초({_LOOP_INTERVAL_S // 60}분) 간격 무한 루프"
    )
    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        _logger.info(f"[Verifier] === 배치 라운드 시작 {ts} ===")
        try:
            counts = await _run_batch(loop)
            _print_counts(counts)
        except Exception as _e:
            _logger.error(f"[Verifier] 배치 오류: {_e}", exc_info=True)
            await _lf.async_log_error("VERIFIER_BATCH_ERROR", "gemini_verifier", _e)

        _logger.info(f"[Verifier] {_LOOP_INTERVAL_S}초 대기 중...")
        await asyncio.sleep(_LOOP_INTERVAL_S)


def _print_counts(counts: dict) -> None:
    if counts["total"] == 0:
        print("  (대기 항목 없음)")
        return
    print(
        f"  처리: {counts['total']}건  "
        f"✅ verified={counts['verified']}  "
        f"⏳ quota={counts['quota']}  "
        f"❌ failed={counts['failed']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="UnivAgent Gemini 사후 검증 데몬"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="1회 배치 실행 후 종료 (기본: 무한 루프)",
    )
    args = parser.parse_args()
    asyncio.run(_main_async(once=args.once))


if __name__ == "__main__":
    main()
