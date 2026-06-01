"""
e2e_tester.py — UnivAgent E2E 시스템 헬스체크 데몬
====================================================
가상의 대입 질문 페이로드를 생성하여 telegram_agent.py 라우터와
동일한 token_manager 3-Tier 라우팅 엔진(Gemini → Groq → Ollama)에
직접 전송합니다. 각 티어별 응답 여부와 레이턴시를 측정하고
결과를 system_events.jsonl 에 비동기 로깅합니다.

devops_reporter.py 는 system_events.jsonl 의 E2E_TEST_RESULT 이벤트를
읽어 대시보드의 E2E 결과를 갱신합니다.

실행:
    python e2e_tester.py            # 1회 즉시 실행
    python e2e_tester.py --daemon   # 30분 간격 반복
    python e2e_tester.py --once     # 명시적 1회 실행
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 경로 설정 ──────────────────────────────────────────────────
_루트 = Path(__file__).resolve().parent
sys.path.insert(0, str(_루트 / "scripts"))

from dotenv import load_dotenv
load_dotenv(_루트 / ".env")

import token_manager as _tm
import logger_factory as _lf
import db_manager as _db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
_logger = logging.getLogger("e2e_tester")

# ── E2E 테스트 페이로드 ────────────────────────────────────────
_TEST_QUESTIONS = [
    {
        "question": "서울대학교 컴퓨터공학과 수시 학생부종합전형 합격을 위해 어떤 세특 활동이 효과적인가요?",
        "profile": {
            "grade": "2등급",
            "major": "컴퓨터공학과",
            "school": "일반고",
            "mock_exam": "1등급",
        },
    },
    {
        "question": "연세대 의대 지역균형선발 전형 지원 시 내신 몇 등급이 필요하며, 비교과 활동에서 어떤 점을 중점으로 준비해야 하나요?",
        "profile": {
            "grade": "1.5등급",
            "major": "의학과",
            "school": "자율고",
            "mock_exam": "1등급",
        },
    },
    {
        "question": "환경공학과를 희망하는 고2 학생입니다. 세특에 환경 관련 탐구 활동을 기록하려면 어떤 주제가 좋을까요?",
        "profile": {
            "grade": "3등급",
            "major": "환경공학과",
            "school": "일반고",
            "mock_exam": "3등급",
        },
    },
]

_SYSTEM_PROMPT = (
    "당신은 UnivAgent 의 대한민국 대학 입시 전문 AI 어시스턴트입니다. "
    "학생의 질문에 정확하고 간결하게 한국어로 답변하세요. "
    "최대 3문장으로 핵심만 답변하세요."
)

_ENGINES = [
    ("gemini", "Tier-1 Gemini"),
    ("groq",   "Tier-2 Groq"),
    ("ollama", "Tier-3 Ollama"),
]

_DAEMON_INTERVAL_S = 1800   # 30분


# ─────────────────────────────────────────────────────────────
# 단일 엔진 테스트
# ─────────────────────────────────────────────────────────────

def _test_engine_sync(
    force_engine: str,
    question: str,
    profile: dict,
) -> dict:
    """지정된 엔진으로 질문을 전송하고 결과 딕셔너리를 반환합니다."""
    prompt = (
        f"[학생 프로필]\n"
        f"희망학과: {profile.get('major','미입력')}\n"
        f"내신: {profile.get('grade','미입력')}\n"
        f"고교유형: {profile.get('school','미입력')}\n"
        f"모의고사: {profile.get('mock_exam','미입력')}\n\n"
        f"[질문]\n{question}"
    )

    start = time.monotonic()
    result = {"engine": force_engine, "success": False, "latency_ms": 0,
              "response_len": 0, "error": ""}
    try:
        text, engine_name = _tm.generate_text_sync(
            prompt,
            system_prompt=_SYSTEM_PROMPT,
            force_engine=force_engine,
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        if text:
            result.update({
                "success":      True,
                "latency_ms":   latency_ms,
                "engine_name":  engine_name,
                "response_len": len(text),
            })
            _logger.info(
                f"[E2E] {force_engine} ✅  {latency_ms:,}ms  "
                f"응답 {len(text)}자  엔진={engine_name}"
            )
        else:
            result["error"] = f"빈 응답 (엔진: {engine_name})"
            _logger.warning(f"[E2E] {force_engine} ⚠️  빈 응답")
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        result.update({"latency_ms": latency_ms, "error": str(exc)[:300]})
        _logger.error(f"[E2E] {force_engine} ❌  {exc}")

    return result


# ─────────────────────────────────────────────────────────────
# 전체 E2E 라운드 실행
# ─────────────────────────────────────────────────────────────

async def _run_e2e_round() -> dict:
    """
    3개 질문 × 3개 엔진 티어에 걸쳐 E2E 라우팅을 검증합니다.

    - 각 질문은 Tier-1(Gemini) → Tier-2(Groq) → Tier-3(Ollama) 순으로 테스트
    - 전체 결과를 집계하여 PASS / PARTIAL / FAIL 판정
    - 결과를 system_events.jsonl + system_metrics DB 에 기록
    """
    loop = asyncio.get_event_loop()
    round_start = time.monotonic()
    test_ts = datetime.now().isoformat(timespec="seconds")

    all_engine_results: list[dict] = []
    question_results:   list[dict] = []

    for q_idx, payload in enumerate(_TEST_QUESTIONS):
        question = payload["question"]
        profile  = payload["profile"]
        q_label  = f"Q{q_idx + 1}"

        _logger.info(f"[E2E] {q_label}: {question[:60]}...")
        engine_rows: list[dict] = []

        for force_engine, tier_label in _ENGINES:
            row = await loop.run_in_executor(
                None,
                lambda e=force_engine, q=question, p=profile: _test_engine_sync(e, q, p),
            )
            row["tier"]    = tier_label
            row["q_label"] = q_label
            engine_rows.append(row)
            all_engine_results.append(row)

            # Tier-1 성공 시 Tier-2/3 건너뜀 (실제 라우팅과 동일)
            if row["success"] and force_engine == "gemini":
                _logger.info(f"[E2E] {q_label} Tier-1 성공 → 하위 티어 생략")
                break

        question_results.append({
            "q_label":  q_label,
            "question": question[:80],
            "engines":  engine_rows,
            "passed":   any(r["success"] for r in engine_rows),
        })

    # ── 판정 ──────────────────────────────────────────────────
    total_q  = len(question_results)
    passed_q = sum(1 for q in question_results if q["passed"])

    if passed_q == total_q:
        verdict = "PASS"
    elif passed_q >= total_q // 2:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    total_latency_ms = int((time.monotonic() - round_start) * 1000)
    successful_engines = [
        r["engine_name"] for r in all_engine_results
        if r.get("success") and r.get("engine_name")
    ]

    summary = {
        "ts":                  test_ts,
        "verdict":             verdict,
        "passed_questions":    passed_q,
        "total_questions":     total_q,
        "total_latency_ms":    total_latency_ms,
        "successful_engines":  list(set(successful_engines)),
        "question_results":    question_results,
    }

    _logger.info(
        f"[E2E] 라운드 완료 → {verdict}  "
        f"({passed_q}/{total_q} 질문 성공, {total_latency_ms:,}ms)"
    )

    # ── system_events.jsonl 기록 ───────────────────────────
    await _lf.async_log_event(
        "E2E_TEST_RESULT",
        "e2e_tester",
        f"E2E 헬스체크 {verdict}: {passed_q}/{total_q} 성공, {total_latency_ms:,}ms",
        level="INFO" if verdict == "PASS" else ("WARNING" if verdict == "PARTIAL" else "ERROR"),
        extra={
            "verdict":            verdict,
            "passed_questions":   passed_q,
            "total_questions":    total_q,
            "total_latency_ms":   total_latency_ms,
            "successful_engines": list(set(successful_engines)),
        },
    )

    # 개별 엔진 에러도 로깅
    for row in all_engine_results:
        if not row["success"] and row.get("error"):
            await _lf.async_log_event(
                "E2E_ENGINE_ERROR",
                "e2e_tester",
                f"{row['engine']} 응답 실패: {row['error'][:200]}",
                level="WARNING",
                extra={
                    "engine":      row["engine"],
                    "latency_ms":  row.get("latency_ms", 0),
                    "error":       row.get("error", ""),
                    "q_label":     row.get("q_label", ""),
                },
            )

    # ── system_metrics DB E2E 결과 갱신 ───────────────────
    try:
        await loop.run_in_executor(
            None,
            lambda: _db.시스템_스냅샷_저장(e2e_test_result=verdict),
        )
        _logger.info(f"[E2E] system_metrics e2e_test_result={verdict} 저장 완료")
    except Exception as e:
        _logger.warning(f"[E2E] system_metrics 저장 실패: {e}")
        await _lf.async_log_error("E2E_DB_UPDATE_FAIL", "e2e_tester", e)

    return summary


# ─────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────

async def _main_async(daemon: bool = False) -> None:
    if daemon:
        _logger.info(f"[E2E 데몬] 시작 — {_DAEMON_INTERVAL_S}초 간격으로 반복 실행")
        while True:
            try:
                summary = await _run_e2e_round()
                verdict = summary["verdict"]
                print(
                    f"\n{'=' * 60}\n"
                    f"[E2E] {summary['ts']}  판정: {verdict}  "
                    f"({summary['passed_questions']}/{summary['total_questions']})\n"
                    f"{'=' * 60}"
                )
            except Exception as e:
                _logger.error(f"[E2E 데몬] 라운드 실행 오류: {e}", exc_info=True)
                await _lf.async_log_error("E2E_ROUND_ERROR", "e2e_tester", e)
            _logger.info(f"[E2E 데몬] {_DAEMON_INTERVAL_S}초 대기 중...")
            await asyncio.sleep(_DAEMON_INTERVAL_S)
    else:
        summary = await _run_e2e_round()
        _print_summary(summary)


def _print_summary(summary: dict) -> None:
    verdict = summary["verdict"]
    icon = "✅" if verdict == "PASS" else ("⚠️" if verdict == "PARTIAL" else "❌")
    print(f"\n{'=' * 60}")
    print(f"  {icon}  E2E 헬스체크 결과: {verdict}")
    print(f"  시각: {summary['ts']}")
    print(f"  질문 성공: {summary['passed_questions']} / {summary['total_questions']}")
    print(f"  총 레이턴시: {summary['total_latency_ms']:,} ms")
    print(f"  성공 엔진: {', '.join(summary['successful_engines']) or '없음'}")
    print(f"{'=' * 60}")

    for qr in summary.get("question_results", []):
        status = "✅" if qr["passed"] else "❌"
        print(f"  {status} {qr['q_label']}: {qr['question']}")
        for er in qr.get("engines", []):
            ok = "✅" if er["success"] else "❌"
            lat = er.get("latency_ms", 0)
            eng = er.get("engine_name", er["engine"])
            err = er.get("error", "")
            detail = f"{lat:,}ms  응답 {er.get('response_len', 0)}자" if er["success"] else f"실패: {err[:60]}"
            print(f"       {ok} {er['tier']} ({eng}): {detail}")

    print(f"\n  로그: {_lf._EVENT_LOG}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="UnivAgent E2E 시스템 헬스체크 데몬"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help=f"{_DAEMON_INTERVAL_S}초 간격으로 반복 실행",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="1회 실행 후 종료 (기본값)",
    )
    args = parser.parse_args()
    asyncio.run(_main_async(daemon=args.daemon))


if __name__ == "__main__":
    main()
