"""
대학 입시 모집요강 PDF 파싱 스크립트 (Groq/Ollama Crawl LLM)

수집된 모집요강 PDF를 PyPDF2로 텍스트 추출 후 Groq→Ollama로
아래 항목을 구조화된 JSON으로 추출합니다 (Gemini 토큰 미사용):
  - 수시 전형별 반영 비율
  - 수능 최저학력기준
  - 모집 인원

출력: data/student/parsed_admission_guide.json
"""

import subprocess
import sys
import os
import re
import json
import logging
import time
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Any

# ─────────────────────────────────────────────────────────────
# 의존성 자동 설치
# ─────────────────────────────────────────────────────────────

REQUIRED_PACKAGES = {
    "PyPDF2":         "PyPDF2",
    "dotenv":         "python-dotenv",
    "groq":           "groq",
    "ollama":         "ollama",
}

sys.path.insert(0, str(Path(__file__).parent))
import token_manager as _tm  # noqa: E402


def 의존성_설치():
    """필요한 패키지가 없으면 pip으로 자동 설치합니다."""
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        # 하위 모듈은 상위 패키지 이름으로 import 시도
        top_level = import_name.split(".")[0]
        try:
            __import__(top_level)
            logging.info(f"[의존성] {pip_name} 이미 설치됨")
        except ImportError:
            logging.warning(f"[의존성] {pip_name} 미설치 → 자동 설치 중...")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pip_name],
                stdout=subprocess.DEVNULL,
            )
            logging.info(f"[의존성] {pip_name} 설치 완료")


# ─────────────────────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────────────────────

def 로깅_설정():
    """콘솔 + 파일에 한국어 로그를 출력하도록 설정합니다."""
    log_dir = Path(__file__).parent.parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"parser_{datetime.now():%Y%m%d_%H%M%S}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logging.info(f"[로그] 로그 파일: {log_file}")


# ─────────────────────────────────────────────────────────────
# 환경 변수 로드
# ─────────────────────────────────────────────────────────────

def 환경_설정_로드() -> None:
    """Groq/Ollama 크롤링 LLM 환경을 로드하고 가용성을 확인합니다."""
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)
        logging.info(f"[설정] .env 파일 로드: {env_path}")
    else:
        logging.warning(f"[설정] .env 파일 없음: {env_path} (환경변수 직접 사용)")

    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        logging.info(f"[설정] GROQ_API_KEY 로드 완료 (앞 8자리: {groq_key[:8]}...)")
    else:
        logging.warning("[설정] GROQ_API_KEY 미설정 — Ollama 로컬 폴백만 사용")

    logging.info("[설정] PDF 파싱 엔진: Groq → Ollama (Gemini 미사용)")


# ─────────────────────────────────────────────────────────────
# PDF 텍스트 추출 (PyPDF2 폴백)
# ─────────────────────────────────────────────────────────────

def PDF_텍스트_추출(pdf_path: Path) -> str:
    """
    PyPDF2로 PDF에서 텍스트를 추출합니다.
    Gemini Files API가 실패하거나 파일이 너무 클 때 폴백으로 사용합니다.
    """
    try:
        import PyPDF2
        text_parts = []
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            총_페이지 = len(reader.pages)
            logging.info(f"[PyPDF2] {pdf_path.name}: {총_페이지}페이지")
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    text_parts.append(f"[{i+1}페이지]\n{text}")
        결과 = "\n\n".join(text_parts)
        logging.info(f"[PyPDF2] 추출 완료: {len(결과):,}자")
        return 결과
    except Exception as e:
        logging.warning(f"[PyPDF2] 텍스트 추출 실패: {e}")
        return ""


# ─────────────────────────────────────────────────────────────
# Gemini 추출 프롬프트
# ─────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """
당신은 대한민국 대학 입시 전문가입니다.
첨부된 대학 입시 모집요강 PDF를 분석하여 아래 항목을 추출해주세요.

## 추출 항목

1. **수시 전형 목록** - 문서에 등장하는 모든 수시 전형명
2. **전형별 반영 비율** - 각 전형의 전형요소별 반영 비율 (예: 학생부교과 80% + 면접 20%)
3. **수능 최저학력기준** - 각 전형별 수능 최저학력기준 (없으면 "없음"으로 표기)
4. **모집 인원** - 각 전형별 모집 인원 (숫자)

## 출력 형식

반드시 아래 JSON 형식으로만 응답하세요. JSON 외의 다른 텍스트는 포함하지 마세요.

```json
{
  "대학명": "대학교 이름",
  "학년도": "20XX학년도",
  "문서_유형": "수시모집요강 | 정시모집요강 | 학생부종합전형안내서 | 입학전형시행계획 | 기타",
  "수시_전형목록": [
    {
      "전형명": "전형 이름",
      "전형_유형": "학생부교과 | 학생부종합 | 논술 | 실기/실적 | 기타",
      "모집인원": 숫자_또는_null,
      "전형요소_반영비율": {
        "학생부교과": 숫자_또는_null,
        "학생부종합(서류)": 숫자_또는_null,
        "논술": 숫자_또는_null,
        "면접": 숫자_또는_null,
        "실기": 숫자_또는_null,
        "수능": 숫자_또는_null,
        "기타": "기타 요소 설명 또는 null"
      },
      "수능최저학력기준": {
        "적용여부": true_또는_false,
        "기준_상세": "기준 내용 텍스트 (없으면 null)",
        "필수_과목수": 숫자_또는_null
      },
      "비고": "특이사항 또는 null"
    }
  ],
  "파싱_신뢰도": "높음 | 중간 | 낮음",
  "파싱_비고": "파싱 과정에서 발견한 특이사항이나 불명확한 부분"
}
```

데이터가 명확하지 않거나 PDF에 해당 정보가 없는 경우 해당 필드를 null로 설정하세요.
모집인원이 여러 전형에 나눠져 있으면 각각 별도 항목으로 작성하세요.
""".strip()


# ─────────────────────────────────────────────────────────────
# JSON 파싱 유틸리티
# ─────────────────────────────────────────────────────────────

def JSON_추출(text: str) -> dict | None:
    """
    Gemini 응답 텍스트에서 JSON 블록을 추출합니다.
    마크다운 코드 블록(```json ... ```) 또는 순수 JSON을 파싱합니다.
    """
    # 마크다운 코드 블록 제거
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE).strip()

    # 중괄호 블록 직접 탐색
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        return None

    json_str = cleaned[start:end]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logging.debug(f"[JSON파싱] 1차 실패: {e}")

    # JSON5-like: 후행 콤마, 단일 따옴표 정규화 후 재시도
    json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
    json_str = json_str.replace("'", '"')
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logging.warning(f"[JSON파싱] 최종 실패: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Gemini API 클라이언트
# ─────────────────────────────────────────────────────────────

class GeminiParser:
    """
    PDF에서 입시 정보를 추출하는 파서입니다.

    전략: PyPDF2 텍스트 추출 → Groq/Ollama (force_engine='crawl') JSON 추출
    """

    REQUEST_INTERVAL_SEC = 2

    def __init__(self, api_key: str = ""):
        self._last_request_time: float = 0
        logging.info("[파서] Crawl LLM 파서 초기화 (Groq→Ollama, Gemini 미사용)")

    def _속도_제한_대기(self):
        """API 속도 제한을 준수하기 위해 필요 시 대기합니다."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.REQUEST_INTERVAL_SEC:
            대기_시간 = self.REQUEST_INTERVAL_SEC - elapsed
            logging.info(f"[Crawl LLM] 속도 제한 대기: {대기_시간:.1f}초...")
            time.sleep(대기_시간)

    def PDF_파싱(self, pdf_path: Path) -> dict:
        """PDF 파일을 분석하여 입시 정보를 추출합니다 (PyPDF2 + Groq/Ollama)."""
        logging.info(f"[파서] 파싱 시작: {pdf_path.name}")
        return self._텍스트_기반_파싱(pdf_path)

    def _Files_API_파싱(self, pdf_path: Path) -> dict | None:
        """레거시 — Gemini Files API 비활성화 (크롤링은 Groq/Ollama 전용)."""
        return None

    # ── 텍스트 기반 파싱 (Groq/Ollama) ──────────────────────────

    def _텍스트_기반_파싱(self, pdf_path: Path) -> dict:
        """
        PyPDF2로 추출한 텍스트를 Groq/Ollama(Crawl LLM)로 분석합니다.
        """
        텍스트 = PDF_텍스트_추출(pdf_path)
        if not 텍스트:
            logging.error(f"[텍스트파싱] 텍스트 추출 실패: {pdf_path.name}")
            return {
                "대학명": pdf_path.stem.split("_")[0],
                "파싱_실패": True,
                "오류_메시지": "PyPDF2 텍스트 추출 실패 - 스캔 PDF이거나 암호화된 파일",
            }

        # 토큰 제한을 위해 앞쪽 40000자만 사용
        텍스트_제한 = 텍스트[:40_000]
        if len(텍스트) > 40_000:
            logging.warning(f"[텍스트파싱] 텍스트 길이 {len(텍스트):,}자 → 40,000자로 제한")

        프롬프트 = (
            f"아래는 대학 입시 모집요강 PDF에서 추출한 텍스트입니다:\n\n"
            f"파일명: {pdf_path.name}\n\n"
            f"--- 텍스트 시작 ---\n{텍스트_제한}\n--- 텍스트 끝 ---\n\n"
            f"{EXTRACTION_PROMPT}"
        )

        self._속도_제한_대기()
        self._last_request_time = time.time()

        logging.info(f"[텍스트파싱] Crawl LLM 분석 요청 ({len(프롬프트):,}자)...")
        try:
            응답_텍스트, 엔진명 = _tm.generate_text_sync(프롬프트, force_engine="crawl")
            if not 응답_텍스트:
                raise RuntimeError("Groq/Ollama Crawl LLM 모두 실패")
            logging.info(f"[텍스트파싱] 응답 수신 ({엔진명}): {len(응답_텍스트):,}자")

            결과 = JSON_추출(응답_텍스트)
            if 결과:
                logging.info(f"[텍스트파싱] JSON 파싱 성공: 전형 {len(결과.get('수시_전형목록', []))}개 추출")
                결과["_파싱_방식"] = f"텍스트 기반 (PyPDF2 + {엔진명})"
                return 결과
            else:
                logging.warning("[텍스트파싱] JSON 추출 실패")
                return {
                    "대학명": pdf_path.stem.split("_")[0],
                    "파싱_실패": True,
                    "_파싱_방식": f"텍스트 기반 (JSON 추출 실패, {엔진명})",
                    "_원본_응답": 응답_텍스트[:2000],
                }
        except Exception as e:
            logging.error(f"[텍스트파싱] API 오류: {e}")
            return {
                "대학명": pdf_path.stem.split("_")[0],
                "파싱_실패": True,
                "오류_메시지": str(e),
            }


# ─────────────────────────────────────────────────────────────
# 결과 후처리 및 저장
# ─────────────────────────────────────────────────────────────

def 결과_후처리(파싱_결과: dict, pdf_path: Path) -> dict:
    """
    Gemini 파싱 결과에 메타데이터를 추가합니다.
    파일명에서 대학명·학년도를 보조 추출합니다.
    """
    파일_메타 = {
        "_소스_파일": pdf_path.name,
        "_파일_크기_MB": round(pdf_path.stat().st_size / 1024 / 1024, 2),
        "_파싱_시각": datetime.now().isoformat(timespec="seconds"),
        "_파일_해시": hashlib.md5(open(pdf_path, "rb").read(4096)).hexdigest()[:8],
    }

    # 파일명에서 보조 정보 추출 (예: 서울대학교_2026학년도_수시모집요강_xxx.pdf)
    부분들 = pdf_path.stem.split("_")
    if "대학명" not in 파싱_결과 or not 파싱_결과["대학명"]:
        파싱_결과["대학명"] = 부분들[0] if 부분들 else "미상"
    if "학년도" not in 파싱_결과 or not 파싱_결과["학년도"]:
        학년도_후보 = next((p for p in 부분들 if re.match(r"20\d{2}학년도", p)), None)
        파싱_결과["학년도"] = 학년도_후보 or "미상"

    파싱_결과.update(파일_메타)
    return 파싱_결과


def JSON_저장(데이터: dict, 저장경로: Path):
    """결과를 들여쓰기된 한국어 친화적 JSON 파일로 저장합니다."""
    저장경로.parent.mkdir(parents=True, exist_ok=True)
    with open(저장경로, "w", encoding="utf-8") as f:
        json.dump(데이터, f, ensure_ascii=False, indent=2)
    크기_kb = 저장경로.stat().st_size / 1024
    logging.info(f"[저장] JSON 저장 완료: {저장경로} ({크기_kb:.1f} KB)")


# ─────────────────────────────────────────────────────────────
# 결과 요약 출력
# ─────────────────────────────────────────────────────────────

def 결과_요약_출력(결과_목록: list[dict]):
    """파싱 결과를 사람이 읽기 쉬운 형태로 콘솔에 출력합니다."""
    logging.info("")
    logging.info("━" * 62)
    logging.info("  파싱 결과 요약")
    logging.info("━" * 62)

    for 결과 in 결과_목록:
        대학명 = 결과.get("대학명", "미상")
        학년도 = 결과.get("학년도", "미상")
        문서유형 = 결과.get("문서_유형", "미상")
        파싱방식 = 결과.get("_파싱_방식", "미상")
        파싱실패 = 결과.get("파싱_실패", False)

        logging.info(f"  ▶ {대학명} {학년도} ({문서유형})")
        logging.info(f"    파싱 방식: {파싱방식}")

        if 파싱실패:
            logging.warning(f"    ⚠ 파싱 실패: {결과.get('오류_메시지', '원인 미상')}")
            continue

        전형들 = 결과.get("수시_전형목록", [])
        logging.info(f"    전형 수: {len(전형들)}개")

        for 전형 in 전형들:
            전형명 = 전형.get("전형명", "미상")
            모집인원 = 전형.get("모집인원")
            수능최저 = 전형.get("수능최저학력기준", {})
            적용여부 = 수능최저.get("적용여부") if isinstance(수능최저, dict) else None
            기준 = 수능최저.get("기준_상세") if isinstance(수능최저, dict) else None

            반영비율 = 전형.get("전형요소_반영비율", {}) or {}
            비율_요약 = ", ".join(
                f"{k}:{v}%" for k, v in 반영비율.items()
                if v is not None and str(v).replace(".", "").isdigit()
            )

            logging.info(f"      [{전형명}]")
            logging.info(f"        모집인원: {모집인원 if 모집인원 is not None else '미확인'}명")
            logging.info(f"        반영비율: {비율_요약 if 비율_요약 else '미확인'}")
            logging.info(f"        수능최저: {'있음' if 적용여부 else '없음'} "
                         f"{'→ ' + 기준[:50] if 기준 else ''}")

        신뢰도 = 결과.get("파싱_신뢰도", "미상")
        logging.info(f"    파싱 신뢰도: {신뢰도}")
        logging.info("")

    logging.info("━" * 62)


# ─────────────────────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────────────────────

def main():
    import argparse

    로깅_설정()
    logging.info("━" * 62)
    logging.info("  대학 입시 모집요강 PDF 파서 (Groq/Ollama Crawl LLM)")
    logging.info("━" * 62)

    # ── CLI 인자 ──────────────────────────────────────────────
    ap = argparse.ArgumentParser(description="모집요강 PDF 파서")
    ap.add_argument(
        "--files", type=str, default="",
        metavar="파일명1,파일명2,...",
        help="파싱할 PDF 파일명 목록 (쉼표 구분). 미지정 시 전체 스캔.",
    )
    ap.add_argument(
        "--delta-only", action="store_true",
        help="지정 파일만 파싱하고 기존 JSON에 병합 (기존 항목 유지).",
    )
    args = ap.parse_args()

    델타_모드 = args.delta_only and bool(args.files)
    지정_파일명 = {f.strip() for f in args.files.split(",") if f.strip()} if args.files else set()

    if 델타_모드:
        logging.info(f"[모드] 델타 병합 모드 — 신규 {len(지정_파일명)}개 파일만 처리")
    else:
        logging.info("[모드] 전체 스캔 모드 — 미파싱 PDF 전체 처리")

    # 1. 의존성 확인
    logging.info("[초기화] 의존성 확인 중...")
    의존성_설치()

    # 2. 환경 설정 (Groq/Ollama)
    환경_설정_로드()

    # 3. 경로 설정
    프로젝트_루트 = Path(__file__).parent.parent
    PDF_디렉토리 = 프로젝트_루트 / "data" / "raw_pdf"
    출력_경로 = 프로젝트_루트 / "data" / "student" / "parsed_admission_guide.json"

    # 4. 기존 JSON 전체 로드 (성공·실패 모두 키로 보존)
    기존_전체_맵: dict[str, dict] = {}   # 소스파일명 → 항목 (모든 기존 결과)
    기존_성공_맵: dict[str, dict] = {}   # 성공 항목만 (전체 스캔 모드의 스킵 판단용)
    기존_메타: dict = {}
    if 출력_경로.exists():
        try:
            with open(출력_경로, encoding="utf-8") as f:
                기존_json = json.load(f)
            기존_메타 = {k: v for k, v in 기존_json.items() if k != "대학_목록"}
            for 항목 in 기존_json.get("대학_목록", []):
                소스 = 항목.get("_소스_파일", "")
                if 소스:
                    기존_전체_맵[소스] = 항목
                    if not 항목.get("파싱_실패"):
                        기존_성공_맵[소스] = 항목
            logging.info(
                f"[기존JSON] 전체 {len(기존_전체_맵)}개 항목 로드 "
                f"(성공 {len(기존_성공_맵)}개 / 실패 {len(기존_전체_맵)-len(기존_성공_맵)}개)"
            )
        except Exception as e:
            logging.warning(f"[기존JSON] 로드 실패: {e} → 새로 시작")

    # 5. 파싱 대상 결정
    if 델타_모드:
        # 지정된 파일명만 — raw_pdf 디렉토리에서 존재 확인
        신규_PDF_목록 = []
        for 파일명 in 지정_파일명:
            경로 = PDF_디렉토리 / 파일명
            if 경로.exists():
                신규_PDF_목록.append(경로)
            else:
                logging.warning(f"[델타] 파일 없음 (스킵): {파일명}")
        logging.info(f"[델타] 실제 파싱 대상: {len(신규_PDF_목록)}개")
    else:
        # 전체 스캔: raw_pdf 내 전체 PDF 중 기존 성공 결과 없는 것만
        PDF_목록 = sorted(PDF_디렉토리.glob("*.pdf"))
        if not PDF_목록:
            logging.error(f"[오류] PDF 파일 없음: {PDF_디렉토리}")
            return 1
        신규_PDF_목록 = [p for p in PDF_목록 if p.name not in 기존_성공_맵]
        스킵_수 = len(PDF_목록) - len(신규_PDF_목록)
        logging.info(
            f"[전체스캔] 전체 PDF: {len(PDF_목록)}개  |  "
            f"스킵(기존성공): {스킵_수}개  |  신규파싱: {len(신규_PDF_목록)}개"
        )
        for p in PDF_목록:
            태그 = "  [스킵]" if p.name in 기존_성공_맵 else "  [신규]"
            logging.info(f"{태그} {p.name} ({p.stat().st_size/1024/1024:.1f} MB)")

    if not 신규_PDF_목록:
        logging.info("[파서] 파싱할 신규 파일 없음 — 정상 종료")
        return 0

    # 6. Crawl LLM 파서 초기화
    파서 = GeminiParser()

    # 7. 신규 PDF 파싱
    신규_결과_목록: list[dict] = []
    성공_수_신규 = 0

    for i, pdf_path in enumerate(신규_PDF_목록, 1):
        logging.info("")
        logging.info(f"[신규 {i}/{len(신규_PDF_목록)}] 파싱 중: {pdf_path.name}")
        logging.info("-" * 50)
        try:
            결과 = 파서.PDF_파싱(pdf_path)
            결과 = 결과_후처리(결과, pdf_path)
            신규_결과_목록.append(결과)
            if not 결과.get("파싱_실패"):
                성공_수_신규 += 1
                logging.info(f"  ✓ 파싱 성공: 전형 {len(결과.get('수시_전형목록', []))}개 추출")
            else:
                logging.warning(f"  ✗ 파싱 실패: {결과.get('오류_메시지', '원인 미상')}")
        except KeyboardInterrupt:
            logging.warning("[파서] 사용자 중단 — 지금까지의 결과를 저장합니다")
            break
        except Exception as e:
            logging.error(f"[파서] 예외 발생: {e}", exc_info=True)
            신규_결과_목록.append({
                "대학명": pdf_path.stem.split("_")[0],
                "파싱_실패": True,
                "오류_메시지": str(e),
                "_소스_파일": pdf_path.name,
                "_파싱_시각": datetime.now().isoformat(timespec="seconds"),
            })

    # 8. JSON 저장 — 델타 병합 vs 전체 재기록
    if 델타_모드:
        # 기존 전체 맵에 신규 결과만 덮어쓰기/추가 (기존 미변경 항목 유지)
        for 결과 in 신규_결과_목록:
            소스 = 결과.get("_소스_파일", "")
            if 소스:
                기존_전체_맵[소스] = 결과
        최종_목록 = sorted(기존_전체_맵.values(), key=lambda x: x.get("_소스_파일", ""))
        기존_총_성공 = sum(1 for v in 기존_전체_맵.values()
                          if not v.get("파싱_실패") and v.get("_소스_파일", "") not in {r.get("_소스_파일") for r in 신규_결과_목록})
        총_성공 = 기존_총_성공 + 성공_수_신규
        logging.info(
            f"[델타병합] 기존 {len(기존_전체_맵) - len(신규_결과_목록)}개 유지 + "
            f"신규 {len(신규_결과_목록)}개 병합 → 총 {len(최종_목록)}개"
        )
    else:
        # 전체 스캔 모드: 기존 성공 + 신규 결과 합산
        기존_목록 = [기존_성공_맵[p.name] for p in PDF_목록 if p.name in 기존_성공_맵]
        최종_목록 = sorted(기존_목록 + 신규_결과_목록, key=lambda x: x.get("_소스_파일", ""))
        총_성공 = len(기존_목록) + 성공_수_신규
        PDF_총수 = len(PDF_목록)

    최종_결과 = {
        "생성_일시": datetime.now().isoformat(timespec="seconds"),
        "총_PDF_수": len(최종_목록),
        "파싱_성공_수": 총_성공,
        "파싱_실패_수": len(최종_목록) - 총_성공,
        "사용_모델": 파서.model,
        "대학_목록": 최종_목록,
    }
    JSON_저장(최종_결과, 출력_경로)

    # 9-a. SQLite 이중 저장 (db_manager 위임)
    try:
        _scripts_dir = Path(__file__).parent
        if str(_scripts_dir) not in sys.path:
            sys.path.insert(0, str(_scripts_dir))
        import db_manager as _db_mgr  # noqa: PLC0415
        _db_mgr.DB_초기화()
        _저장_수 = 0
        for 항목 in 최종_목록:
            try:
                _db_mgr.입시_저장(항목)
                _저장_수 += 1
            except Exception as _e:
                logging.warning(f"[DB저장] {항목.get('_소스_파일','?')} 저장 실패: {_e}")
        logging.info(f"[DB저장] SQLite 저장 완료: {_저장_수}/{len(최종_목록)}개")
    except Exception as _db_e:
        logging.warning(f"[DB저장] db_manager 로드 실패, JSON만 저장: {_db_e}")

    # 9-b. 요약 출력 (신규 파싱 결과만)
    결과_요약_출력(신규_결과_목록)

    logging.info(f"[완료] 신규 파싱: {성공_수_신규}/{len(신규_PDF_목록)}개 성공")
    logging.info(f"[완료] 누적 JSON 항목: {len(최종_목록)}개")
    logging.info(f"[완료] 결과 파일: {출력_경로}")
    return 0 if 성공_수_신규 > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
