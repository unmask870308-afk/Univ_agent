"""
nightly_train.py — Ollama 자율 진화 야간 트레이너
==================================================
golden_dataset 에서 품질 상위 QA 쌍을 추출하여 Ollama Modelfile을 동적으로
생성하고, 'univagent-expert' 모델을 새로 빌드합니다.

실행 방법:
    python3 scripts/nightly_train.py              # 기본 (top-30 사례)
    python3 scripts/nightly_train.py --limit 50   # 사례 수 지정
    python3 scripts/nightly_train.py --dry-run    # Modelfile 생성만, ollama create 생략
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
from datetime import datetime
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
logger = logging.getLogger("nightly_train")

import db_manager

# ─────────────────────────────────────────────────────────────
# Modelfile 상수
# ─────────────────────────────────────────────────────────────

_MODELFILE_BASE_MODEL = "gemma2:2b"   # 기반 모델 (ollama pull gemma2:2b 필요)
_TARGET_MODEL_NAME   = "univagent-expert"

_SYSTEM_PERSONA = (
    "당신은 대한민국 최고의 대학 입시 전문 컨설턴트입니다. "
    "수시·정시·학종·교과·논술·실기 전형에 통달하였으며, "
    "서울대·연세대·고려대 등 최상위권부터 지방 국립대까지 모든 대학의 "
    "입시 전략을 구체적으로 조언할 수 있습니다. "
    "학생의 내신 등급, 모의고사 성적, 수상·봉사·세특 활동을 종합하여 "
    "최적의 지원 전략을 한국어로만 답변합니다. "
    "항상 구체적인 수치와 전형명을 명시하고, "
    "모르는 정보는 솔직히 모른다고 말합니다."
)

_PARAMETER_BLOCK = """
PARAMETER temperature 0.7
PARAMETER top_p 0.9
PARAMETER top_k 40
PARAMETER repeat_penalty 1.1
PARAMETER num_ctx 4096
""".strip()


def _build_modelfile(examples: list[dict]) -> str:
    """황금 QA 쌍을 few-shot MESSAGE 블록으로 주입한 Modelfile을 반환합니다."""
    lines: list[str] = []
    lines.append(f"FROM {_MODELFILE_BASE_MODEL}")
    lines.append("")
    lines.append(f'SYSTEM """{_SYSTEM_PERSONA}"""')
    lines.append("")
    lines.append(_PARAMETER_BLOCK)
    lines.append("")

    for ex in examples:
        user_q  = ex.get("user_query", "").strip()
        gemini_a = ex.get("gemini_response", ex.get("ollama_response", "")).strip()
        if not user_q or not gemini_a:
            continue
        # Modelfile MESSAGE 형식
        lines.append(f'MESSAGE user """{user_q}"""')
        lines.append(f'MESSAGE assistant """{gemini_a}"""')
        lines.append("")

    return "\n".join(lines)


def _run_ollama_create(modelfile_text: str, dry_run: bool) -> bool:
    """
    임시 Modelfile을 작성하고 `ollama create` 를 실행합니다.
    성공 여부(bool)를 반환합니다.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".Modelfile", delete=False,
        dir=_ROOT, encoding="utf-8"
    ) as tmp:
        tmp.write(modelfile_text)
        tmp_path = Path(tmp.name)

    archive_dir  = _ROOT / "data" / "training" / "modelfiles"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts           = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = archive_dir / f"Modelfile_{ts}"
    tmp_path.rename(archive_path)
    logger.info(f"[NightlyTrain] Modelfile 저장: {archive_path.relative_to(_ROOT)}")

    if dry_run:
        logger.info(f"[NightlyTrain] --dry-run 모드: ollama create 생략")
        return True

    # Ollama 서버 alive 확인
    import urllib.request, urllib.error
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5)
    except Exception:
        logger.error("[NightlyTrain] Ollama 서버 미응답. 'ollama serve'를 먼저 실행하세요.")
        return False

    logger.info(f"[NightlyTrain] ollama create {_TARGET_MODEL_NAME} 실행 중...")
    try:
        result = subprocess.run(
            ["ollama", "create", _TARGET_MODEL_NAME, "-f", str(archive_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            logger.info(f"[NightlyTrain] ✅ '{_TARGET_MODEL_NAME}' 모델 빌드 성공")
            return True
        else:
            logger.error(
                f"[NightlyTrain] ollama create 실패 (rc={result.returncode})\n"
                f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
            )
            return False
    except subprocess.TimeoutExpired:
        logger.error("[NightlyTrain] ollama create 타임아웃 (300초 초과)")
        return False
    except FileNotFoundError:
        logger.error("[NightlyTrain] 'ollama' 명령어를 찾을 수 없습니다. Ollama가 설치되어 있는지 확인하세요.")
        return False


def run_nightly_train(limit: int = 30, dry_run: bool = False) -> bool:
    """
    golden_dataset 에서 top-N 예시를 추출해 Ollama 모델을 재학습합니다.
    성공 여부를 반환합니다.
    """
    db_manager.init_db()

    examples = db_manager.get_golden_qa_for_training(limit=limit)
    if not examples:
        logger.info("[NightlyTrain] 학습할 golden_dataset 없음 — 종료")
        return True   # 에러가 아님, 그냥 데이터가 없는 것

    logger.info(f"[NightlyTrain] {len(examples)}개 황금 QA 쌍으로 Modelfile 생성")
    modelfile_text = _build_modelfile(examples)

    success = _run_ollama_create(modelfile_text, dry_run)

    if success and not dry_run:
        ids = [ex["id"] for ex in examples if "id" in ex]
        if ids:
            db_manager.mark_golden_qa_trained(ids)
            logger.info(f"[NightlyTrain] {len(ids)}개 레코드 used_in_train=1 마킹")

    return success


def main() -> None:
    parser = argparse.ArgumentParser(description="Ollama 야간 자율 트레이너")
    parser.add_argument("--limit",   type=int, default=30, help="사용할 황금 QA 쌍 수 (기본 30)")
    parser.add_argument("--dry-run", action="store_true",  help="Modelfile 생성만, ollama create 생략")
    args = parser.parse_args()

    logger.info(f"[NightlyTrain] 시작 (limit={args.limit}, dry_run={args.dry_run})")
    start = time.time()

    success = run_nightly_train(limit=args.limit, dry_run=args.dry_run)

    elapsed = time.time() - start
    if success:
        print(f"🧠 Nightly Build Complete: Ollama has evolved using today's feedback! ({elapsed:.1f}s)")
        sys.exit(0)
    else:
        print(f"❌ Nightly Build Failed — Ollama 모델 업데이트 실패. 로그를 확인하세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
