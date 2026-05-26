"""
대학 입시 모집요강 PDF 파싱 스크립트 (Google Gemini AI 활용)

수집된 모집요강 PDF를 Gemini API에 전송하여 아래 항목을 구조화된 JSON으로 추출합니다:
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
    "google.genai":   "google-genai",
    "PyPDF2":         "PyPDF2",
    "dotenv":         "python-dotenv",
}


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

def API_키_로드() -> str:
    """
    GEMINI_API_KEY를 환경변수 또는 .env 파일에서 읽습니다.
    키가 없으면 안내 메시지와 함께 종료합니다.
    """
    # .env 파일 로드 (있을 경우)
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)
        logging.info(f"[설정] .env 파일 로드: {env_path}")
    else:
        logging.warning(f"[설정] .env 파일 없음: {env_path} (환경변수 직접 사용)")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logging.error("=" * 60)
        logging.error("[오류] GEMINI_API_KEY가 설정되지 않았습니다.")
        logging.error("  방법 1: .env 파일 생성 후 GEMINI_API_KEY=키값 입력")
        logging.error("  방법 2: export GEMINI_API_KEY=키값 (터미널에서 직접 설정)")
        logging.error("  API 키 발급: https://aistudio.google.com/app/apikey")
        logging.error("=" * 60)
        sys.exit(1)

    logging.info(f"[설정] GEMINI_API_KEY 로드 완료 (앞 8자리: {api_key[:8]}...)")
    return api_key


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
    Gemini API를 사용하여 PDF에서 입시 정보를 추출하는 파서입니다.

    전략:
    1. PDF를 Gemini Files API에 업로드하여 직접 처리 (권장)
    2. Files API 실패 시 PyPDF2로 텍스트 추출 후 텍스트로 전송 (폴백)
    """

    # 무료 티어 속도 제한 대기 (초)
    REQUEST_INTERVAL_SEC = 4

    # 모델 우선순위: 쿼터/가용성에 따라 순서대로 시도
    MODEL_FALLBACK = [
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash",
    ]

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-lite"):
        from google import genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self._last_request_time: float = 0
        logging.info(f"[Gemini] 클라이언트 초기화 완료 (모델: {self.model})")

    def _속도_제한_대기(self):
        """API 속도 제한(분당 15회)을 준수하기 위해 필요 시 대기합니다."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.REQUEST_INTERVAL_SEC:
            대기_시간 = self.REQUEST_INTERVAL_SEC - elapsed
            logging.info(f"[Gemini] 속도 제한 대기: {대기_시간:.1f}초...")
            time.sleep(대기_시간)

    def PDF_파싱(self, pdf_path: Path) -> dict:
        """
        PDF 파일을 분석하여 입시 정보를 추출합니다.
        Gemini Files API → PyPDF2 폴백 순서로 시도합니다.
        """
        logging.info(f"[파서] 파싱 시작: {pdf_path.name}")
        크기_mb = pdf_path.stat().st_size / 1024 / 1024

        # Gemini Files API 방식 시도 (20MB 이하)
        if 크기_mb <= 20:
            결과 = self._Files_API_파싱(pdf_path)
            if 결과:
                return 결과

        # 폴백: PyPDF2 텍스트 추출 후 텍스트로 전송
        logging.warning(f"[파서] Files API 실패 → PyPDF2 폴백 전환: {pdf_path.name}")
        return self._텍스트_기반_파싱(pdf_path)

    # ── Files API 방식 ─────────────────────────────────────────

    def _Files_API_파싱(self, pdf_path: Path) -> dict | None:
        """
        Gemini Files API에 PDF를 업로드하고 분석을 요청합니다.
        파일 URI를 사용하므로 텍스트 추출 없이 PDF를 직접 처리합니다.
        """
        from google.genai import types

        logging.info(f"[Files API] 업로드 시작: {pdf_path.name} ({pdf_path.stat().st_size/1024/1024:.1f} MB)")

        업로드된_파일 = None
        try:
            with open(pdf_path, "rb") as f:
                업로드된_파일 = self.client.files.upload(
                    file=f,
                    config=types.UploadFileConfig(
                        mime_type="application/pdf",
                        display_name=pdf_path.stem,
                    ),
                )
            logging.info(f"[Files API] 업로드 완료: URI={업로드된_파일.uri}")

            # 파일 처리 완료 대기 (ACTIVE 상태)
            최대_대기 = 30
            for _ in range(최대_대기):
                상태 = self.client.files.get(name=업로드된_파일.name)
                if 상태.state.name == "ACTIVE":
                    break
                logging.info(f"[Files API] 파일 처리 중... (상태: {상태.state.name})")
                time.sleep(1)
            else:
                logging.warning("[Files API] 파일 처리 타임아웃")
                return None

            self._속도_제한_대기()
            self._last_request_time = time.time()

            logging.info("[Files API] Gemini 분석 요청 전송 중...")
            응답 = None
            for 시도_모델 in [self.model] + [m for m in self.MODEL_FALLBACK if m != self.model]:
                try:
                    응답 = self.client.models.generate_content(
                        model=시도_모델,
                        contents=[
                            # google-genai 2.x: 키워드 인자 필수
                            types.Part.from_uri(
                                file_uri=업로드된_파일.uri,
                                mime_type="application/pdf",
                            ),
                            types.Part.from_text(text=EXTRACTION_PROMPT),
                        ],
                        config=types.GenerateContentConfig(
                            temperature=0.1,
                            max_output_tokens=8192,
                        ),
                    )
                    if 시도_모델 != self.model:
                        logging.info(f"[Files API] 폴백 모델 사용: {시도_모델}")
                    break
                except Exception as e:
                    err = str(e)
                    if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                        logging.warning(f"[Files API] 모델 {시도_모델} 쿼터 초과 → 다음 모델 시도")
                        time.sleep(2)
                    else:
                        raise
            if 응답 is None:
                raise RuntimeError("모든 모델 쿼터 초과")

            응답_텍스트 = 응답.text
            logging.info(f"[Files API] 응답 수신: {len(응답_텍스트):,}자")

            결과 = JSON_추출(응답_텍스트)
            if 결과:
                logging.info(f"[Files API] JSON 파싱 성공: 전형 {len(결과.get('수시_전형목록', []))}개 추출")
                결과["_파싱_방식"] = "Gemini Files API"
                결과["_원본_응답_길이"] = len(응답_텍스트)
                return 결과
            else:
                logging.warning("[Files API] JSON 파싱 실패 - 원본 응답을 raw 필드에 저장")
                return {
                    "대학명": pdf_path.stem.split("_")[0],
                    "파싱_실패": True,
                    "_파싱_방식": "Gemini Files API (JSON 추출 실패)",
                    "_원본_응답": 응답_텍스트[:2000],
                }

        except Exception as e:
            logging.error(f"[Files API] 오류: {e}")
            return None
        finally:
            # 업로드한 파일 삭제 (API 저장소 정리)
            if 업로드된_파일:
                try:
                    self.client.files.delete(name=업로드된_파일.name)
                    logging.info(f"[Files API] 임시 파일 삭제 완료: {업로드된_파일.name}")
                except Exception:
                    pass

    # ── 텍스트 기반 폴백 방식 ──────────────────────────────────

    def _텍스트_기반_파싱(self, pdf_path: Path) -> dict:
        """
        PyPDF2로 추출한 텍스트를 Gemini에 직접 전송합니다.
        텍스트 추출 품질에 따라 결과 정확도가 달라집니다.
        """
        from google.genai import types

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

        logging.info(f"[텍스트파싱] Gemini 텍스트 분석 요청 ({len(프롬프트):,}자)...")
        try:
            응답 = None
            for 시도_모델 in [self.model] + [m for m in self.MODEL_FALLBACK if m != self.model]:
                try:
                    응답 = self.client.models.generate_content(
                        model=시도_모델,
                        contents=프롬프트,
                        config=types.GenerateContentConfig(
                            temperature=0.1,
                            max_output_tokens=8192,
                        ),
                    )
                    if 시도_모델 != self.model:
                        logging.info(f"[텍스트파싱] 폴백 모델 사용: {시도_모델}")
                    break
                except Exception as e:
                    err = str(e)
                    if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                        logging.warning(f"[텍스트파싱] 모델 {시도_모델} 쿼터 초과 → 다음 모델 시도")
                        time.sleep(2)
                    else:
                        raise
            if 응답 is None:
                raise RuntimeError("모든 모델 쿼터 초과")
            응답_텍스트 = 응답.text
            logging.info(f"[텍스트파싱] 응답 수신: {len(응답_텍스트):,}자")

            결과 = JSON_추출(응답_텍스트)
            if 결과:
                logging.info(f"[텍스트파싱] JSON 파싱 성공: 전형 {len(결과.get('수시_전형목록', []))}개 추출")
                결과["_파싱_방식"] = "텍스트 기반 (PyPDF2 + Gemini)"
                return 결과
            else:
                logging.warning("[텍스트파싱] JSON 추출 실패")
                return {
                    "대학명": pdf_path.stem.split("_")[0],
                    "파싱_실패": True,
                    "_파싱_방식": "텍스트 기반 (JSON 추출 실패)",
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
    로깅_설정()
    logging.info("━" * 62)
    logging.info("  대학 입시 모집요강 PDF 파서 (Gemini AI)")
    logging.info("━" * 62)

    # 1. 의존성 확인
    logging.info("[초기화] 의존성 확인 중...")
    의존성_설치()

    # 2. API 키 로드
    api_key = API_키_로드()

    # 3. 경로 설정
    프로젝트_루트 = Path(__file__).parent.parent
    PDF_디렉토리 = 프로젝트_루트 / "data" / "raw_pdf"
    출력_경로 = 프로젝트_루트 / "data" / "student" / "parsed_admission_guide.json"

    # 4. 파싱 대상 PDF 목록 수집
    PDF_목록 = sorted(PDF_디렉토리.glob("*.pdf"))
    if not PDF_목록:
        logging.error(f"[오류] PDF 파일 없음: {PDF_디렉토리}")
        logging.error("  먼저 scripts/pdf_collector.py 를 실행하여 PDF를 수집하세요.")
        return 1

    logging.info(f"[파서] 파싱 대상 PDF: {len(PDF_목록)}개")
    for p in PDF_목록:
        logging.info(f"  - {p.name} ({p.stat().st_size/1024/1024:.1f} MB)")

    # 5. Gemini 파서 초기화
    파서 = GeminiParser(api_key=api_key)

    # 6. 각 PDF 파싱
    파싱_결과_목록: list[dict] = []
    성공_수 = 0

    for i, pdf_path in enumerate(PDF_목록, 1):
        logging.info("")
        logging.info(f"[{i}/{len(PDF_목록)}] 파싱 중: {pdf_path.name}")
        logging.info("-" * 50)

        try:
            결과 = 파서.PDF_파싱(pdf_path)
            결과 = 결과_후처리(결과, pdf_path)
            파싱_결과_목록.append(결과)

            if not 결과.get("파싱_실패"):
                성공_수 += 1
                logging.info(f"  ✓ 파싱 성공: 전형 {len(결과.get('수시_전형목록', []))}개 추출")
            else:
                logging.warning(f"  ✗ 파싱 실패: {결과.get('오류_메시지', '원인 미상')}")

        except KeyboardInterrupt:
            logging.warning("[파서] 사용자 중단 - 지금까지의 결과를 저장합니다")
            break
        except Exception as e:
            logging.error(f"[파서] 예외 발생: {e}", exc_info=True)
            파싱_결과_목록.append({
                "대학명": pdf_path.stem.split("_")[0],
                "파싱_실패": True,
                "오류_메시지": str(e),
                "_소스_파일": pdf_path.name,
                "_파싱_시각": datetime.now().isoformat(timespec="seconds"),
            })

    # 7. 최종 JSON 저장
    최종_결과 = {
        "생성_일시": datetime.now().isoformat(timespec="seconds"),
        "총_PDF_수": len(PDF_목록),
        "파싱_성공_수": 성공_수,
        "파싱_실패_수": len(파싱_결과_목록) - 성공_수,
        "사용_모델": 파서.model,
        "대학_목록": 파싱_결과_목록,
    }

    JSON_저장(최종_결과, 출력_경로)

    # 8. 요약 출력
    결과_요약_출력(파싱_결과_목록)

    logging.info(f"[완료] 파싱 완료: {성공_수}/{len(PDF_목록)}개 성공")
    logging.info(f"[완료] 결과 파일: {출력_경로}")
    return 0 if 성공_수> 0 else 1


if __name__ == "__main__":
    sys.exit(main())
