"""
generate_handover.py — UnivAgent AI 컨텍스트 핸드오버 생성기
=============================================================
docs/ 폴더의 모든 .md 파일을 알파벳 순으로 읽어
마스터 프롬프트 래퍼로 감싼 뒤
docs/CURRENT_HANDOVER_PROMPT.txt 에 저장합니다.

실행:
    python3 scripts/generate_handover.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# ── 경로 설정 ──────────────────────────────────────────────────
_ROOT     = Path(__file__).resolve().parent.parent
_DOCS_DIR = _ROOT / "docs"
_OUTPUT   = _DOCS_DIR / "CURRENT_HANDOVER_PROMPT.txt"

_PROMPT_HEADER = """\
당신은 지금부터 'UnivAgent'의 수석 AI 엔지니어입니다.
아래의 프로젝트 아키텍처, 규칙, 히스토리를 완벽히 숙지하고 '숙지 완료'라고 대답하세요.

[생성 시각: {generated_at}]

[PROJECT CONTEXT]
"""

_PROMPT_FOOTER = """\

[END OF PROJECT CONTEXT]

위 내용을 모두 숙지했으면 '숙지 완료'라고만 답하세요.
이후 사용자의 지시에 따라 UnivAgent 수석 엔지니어로서 작업을 수행하세요.
"""


def generate_handover() -> Path:
    """
    docs/*.md 를 알파벳 순으로 읽어 핸드오버 프롬프트를 생성합니다.

    반환값: 생성된 파일 경로
    """
    if not _DOCS_DIR.is_dir():
        print(f"[오류] docs/ 디렉터리가 없습니다: {_DOCS_DIR}")
        sys.exit(1)

    md_files = sorted(_DOCS_DIR.glob("*.md"))
    if not md_files:
        print(f"[오류] docs/ 에 .md 파일이 없습니다.")
        sys.exit(1)

    sections: list[str] = []
    for md_path in md_files:
        content = md_path.read_text(encoding="utf-8").strip()
        sections.append(f"{'=' * 60}\n## 파일: {md_path.name}\n{'=' * 60}\n\n{content}")
        print(f"  [+] {md_path.name} ({len(content):,}자)")

    combined_body = "\n\n".join(sections)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = _PROMPT_HEADER.format(generated_at=generated_at)

    full_prompt = header + combined_body + _PROMPT_FOOTER

    _OUTPUT.write_text(full_prompt, encoding="utf-8")
    return _OUTPUT


def main() -> None:
    print("=" * 55)
    print(" UnivAgent Handover Prompt Generator")
    print(f" docs 경로: {_DOCS_DIR}")
    print("=" * 55)

    output_path = generate_handover()

    size_kb = output_path.stat().st_size / 1024
    print()
    print(f"✅ Handover prompt generated at docs/CURRENT_HANDOVER_PROMPT.txt. "
          f"Copy its contents into a new Gemini session.")
    print(f"   파일 크기: {size_kb:.1f} KB  |  경로: {output_path}")


if __name__ == "__main__":
    main()
