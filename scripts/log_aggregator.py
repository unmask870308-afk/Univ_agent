"""
log_aggregator.py — UnivAgent 통합 디버그 로그 수집기
=====================================================
여러 로그 파일을 하나의 가독성 높은 텍스트 파일로 합쳐
Gemini/Claude에게 전달할 수 있도록 data/fix_error/unified_debug.txt 에 저장합니다.

사용법:
    python scripts/log_aggregator.py
"""

import os
import sys
from datetime import datetime
from pathlib import Path

_루트 = Path(__file__).resolve().parent.parent

_LOG_SOURCES = [
    ("TELEGRAM NOHUP LOG (최근 100줄)",     _루트 / "data" / "logs" / "telegram_nohup.log",          100),
    ("SYSTEM EVENTS (최근 50줄)",            _루트 / "data" / "logs" / "system_events.jsonl",          50),
    ("USER ACTIVITY LOG (최근 50줄)",        _루트 / "data" / "logs" / "user_activity.log",            50),
    ("TELEGRAM ERRORS (최근 30줄)",          _루트 / "data" / "fix_error" / "telegram_errors.log",     30),
    ("GEMINI API ERRORS (최근 20줄)",        _루트 / "data" / "fix_error" / "gemini_api_errors.log",   20),
    ("DEVOPS ERRORS (최근 20줄)",            _루트 / "data" / "fix_error" / "devops_errors.log",       20),
    ("CRAWLER ERRORS (최근 20줄)",           _루트 / "data" / "logs" / "crawler_errors.log",           20),
    ("AI RUNTIME ERRORS (최근 20줄)",        _루트 / "data" / "fix_error" / "ai_runtime_errors.json",  20),
]

_OUT_DIR  = _루트 / "data" / "fix_error"
_OUT_FILE = _OUT_DIR / "unified_debug.txt"


def _tail(path: Path, n: int) -> str:
    """파일의 마지막 n줄을 문자열로 반환합니다."""
    if not path.exists():
        return f"  (파일 없음: {path.relative_to(_루트)})\n"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return "  (파일이 비어 있습니다)\n"
        tail_lines = lines[-n:]
        return "\n".join(tail_lines) + "\n"
    except Exception as e:
        return f"  (읽기 실패: {e})\n"


def build_unified_debug() -> Path:
    """통합 디버그 파일을 생성하고 경로를 반환합니다."""
    os.makedirs(str(_OUT_DIR), exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sep = "=" * 60

    chunks = [
        f"UnivAgent 통합 디버그 리포트\n",
        f"생성 시각: {ts}\n",
        f"{sep}\n\n",
    ]

    for title, path, n in _LOG_SOURCES:
        chunks.append(f"\n{'='*3} {title} {'='*3}\n")
        chunks.append(_tail(path, n))

    output = "".join(chunks)
    _OUT_FILE.write_text(output, encoding="utf-8")
    return _OUT_FILE


if __name__ == "__main__":
    out = build_unified_debug()
    print(f"✅ 통합 디버그 파일이 {out.relative_to(_루트)} 에 생성되었습니다.")
    print("   이 파일의 내용을 복사해서 제미나이에게 전달하세요.")
    print()

    # 파일 크기 안내
    size_kb = out.stat().st_size / 1024
    print(f"   파일 크기: {size_kb:.1f} KB")
    print(f"   전체 경로: {out}")
