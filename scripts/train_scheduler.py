"""
train_scheduler.py — UnivAgent 야간 자율 훈련 스케줄러
=======================================================
지정한 시간 구간 동안 auto_simulator.py 를 45초 간격으로 반복 실행합니다.
Gemini API 429 Rate Limit 방지를 위해 반드시 45초 이상 대기합니다.

사용법:
    python3 scripts/train_scheduler.py --start 00:00 --end 08:00
    python3 scripts/train_scheduler.py --start 22:30 --end 06:00  # 자정 넘김 지원
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path

_ROOT    = Path(__file__).resolve().parent.parent
_LOG_DIR = _ROOT / "data" / "logs"
_LOG_FILE = _LOG_DIR / "training_night.log"
_PID_FILE = _LOG_DIR / "train.pid"
_SLEEP_BETWEEN = 45          # Gemini 429 방지 최소 대기 (초)
_SIM_SCRIPT    = _ROOT / "scripts" / "auto_simulator.py"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(_LOG_FILE), mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger("train_scheduler")


def _parse_hhmm(s: str) -> dt_time:
    h, m = map(int, s.split(":"))
    return dt_time(h, m)


def _now_time() -> dt_time:
    n = datetime.now()
    return dt_time(n.hour, n.minute, n.second)


def _in_window(start: dt_time, end: dt_time) -> bool:
    """현재 시각이 [start, end) 범위 안인지 확인. 자정을 넘기는 구간도 지원."""
    now = _now_time()
    if start <= end:
        return start <= now < end
    # 자정을 넘기는 구간 (e.g. 22:00 → 06:00)
    return now >= start or now < end


def _seconds_until(target: dt_time) -> float:
    """target 시각까지 남은 초를 반환합니다. 최대 24시간."""
    now = datetime.now()
    t = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if t <= now:
        t = t.replace(day=t.day + 1)
    return (t - now).total_seconds()


def _run_one_simulation() -> int:
    """auto_simulator.py --count 1 을 실행하고 종료 코드를 반환합니다."""
    py = sys.executable
    result = subprocess.run(
        [py, str(_SIM_SCRIPT), "--count", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(_ROOT),
    )
    # stdout을 로그 파일에 그대로 기록
    if result.stdout:
        with open(str(_LOG_FILE), "a", encoding="utf-8") as lf:
            lf.write(result.stdout)
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="UnivAgent 야간 훈련 스케줄러")
    parser.add_argument("--start", default="00:00", metavar="HH:MM",
                        help="훈련 시작 시각 (기본: 00:00)")
    parser.add_argument("--end",   default="08:00", metavar="HH:MM",
                        help="훈련 종료 시각 (기본: 08:00)")
    args = parser.parse_args()

    start_t = _parse_hhmm(args.start)
    end_t   = _parse_hhmm(args.end)

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    # PID 파일 기록 (대시보드가 프로세스 상태를 확인)
    _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    logger.info(f"[스케줄러] 시작 — 훈련 창: {args.start} ~ {args.end}")

    try:
        # ── 시작 시각까지 대기 ───────────────────────────────
        if not _in_window(start_t, end_t):
            wait_sec = _seconds_until(start_t)
            logger.info(f"[스케줄러] 훈련 시작 시각 대기 중 ({wait_sec/60:.1f}분 남음)...")
            time.sleep(wait_sec)

        run_count = 0
        fail_count = 0

        # ── 메인 훈련 루프 ───────────────────────────────────
        while _in_window(start_t, end_t):
            run_count += 1
            logger.info(f"[스케줄러] 루프 #{run_count} 시작 — {datetime.now().strftime('%H:%M:%S')}")

            rc = _run_one_simulation()
            if rc == 0:
                logger.info(f"[스케줄러] 루프 #{run_count} 완료 ✅")
            else:
                fail_count += 1
                logger.warning(f"[스케줄러] 루프 #{run_count} 실패 (exit={rc}) — 계속 진행")

            # 종료 시각 재확인 후 대기
            if _in_window(start_t, end_t):
                logger.info(f"[스케줄러] {_SLEEP_BETWEEN}초 대기 (Gemini 429 방지)...")
                time.sleep(_SLEEP_BETWEEN)

        logger.info(
            f"[스케줄러] 훈련 창 종료. "
            f"총 {run_count}회 실행 / 성공 {run_count - fail_count}회 / 실패 {fail_count}회"
        )

    except KeyboardInterrupt:
        logger.info("[스케줄러] 강제 종료 신호 수신 — 정상 종료합니다.")
    finally:
        try:
            _PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info("[스케줄러] PID 파일 삭제 완료. 종료.")


if __name__ == "__main__":
    main()
