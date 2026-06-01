"""
vision_parser.py — Gemini Vision 기반 성적표 OCR 분석기
=======================================================
Google `google-genai` SDK(v2+) 를 사용하여 성적표 이미지에서
내신 등급을 자동 추출합니다.

공개 함수:
    extract_grades_from_image(image_path) -> str   (비동기)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

# ── dotenv 로드 — 반드시 API 키 참조 전에 실행 ──────────────────
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv 미설치 환경에서는 기존 환경변수 사용

logger = logging.getLogger(__name__)

_OCR_PROMPT = (
    "이 이미지는 한국 고등학교의 생활기록부(NEIS) 또는 성적표입니다. "
    "이미지에 있는 1학년, 2학년(있는 경우)의 과목별 성적을 분석하여, "
    "1) 전체 평균 내신 등급(GPA)을 소수점 둘째 자리까지 계산하고, "
    "2) 학년별 성적 추이를 간략히 요약해 주세요. "
    "결과는 '전체 평균: X.XX등급 (1학년: X.X, 2학년: X.X)' 형태로 "
    "명확하게 텍스트로만 반환하세요."
)

_MIME_MAP: dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
    ".gif":  "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _infer_mime(path: Path) -> str:
    return _MIME_MAP.get(path.suffix.lower(), "image/jpeg")


def _call_gemini_vision_sync(image_path: Path, api_key: str) -> str:
    """동기 Gemini Vision 호출 — run_in_executor 에서 실행됩니다."""
    # ── Fail-Fast: API 키 검증 ───────────────────────────────────
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY가 .env 파일에 설정되지 않았거나 로드되지 않았습니다."
        )

    try:
        from google import genai          # google-genai v2+
        from google.genai import types
    except ImportError as ie:
        raise RuntimeError(
            f"google-genai 패키지가 설치되지 않았습니다. "
            f"'pip install google-genai'를 실행하세요. 원인: {ie}"
        ) from ie

    image_bytes = image_path.read_bytes()
    mime_type   = _infer_mime(image_path)

    client     = genai.Client(api_key=api_key)
    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    text_part  = types.Part.from_text(text=_OCR_PROMPT)

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=types.Content(parts=[image_part, text_part], role="user"),
    )
    return (response.text or "").strip()


def _classify_api_error(e: Exception) -> str:
    """API 예외를 사용자 친화적 한국어 메시지로 변환합니다."""
    err_str  = str(e).lower()
    err_type = type(e).__name__

    # google-genai v2 ClientError / google.api_core.exceptions 공통 처리
    status_code = (
        getattr(e, "status_code", None)
        or getattr(e, "code", None)
        or getattr(e, "http_status", None)
    )

    # 401 / 403 → 인증 실패
    if status_code in (401, 403) or any(
        kw in err_str for kw in ("api_key_invalid", "permission_denied",
                                  "invalid api key", "api key not valid",
                                  "unauthenticated", "forbidden")
    ):
        return "[OCR 오류] API 키가 만료되었거나 올바르지 않습니다. .env 파일의 GEMINI_API_KEY를 확인해주세요."

    # 429 → 할당량 초과
    if status_code == 429 or any(
        kw in err_str for kw in ("resource_exhausted", "quota", "rate limit",
                                  "too many requests")
    ):
        return "[OCR 오류] API 할당량이 초과되었습니다. 잠시 후 다시 시도해주세요."

    # 400 → 잘못된 요청 (이미지 포맷 등)
    if status_code == 400 or "invalid_argument" in err_str:
        return "[OCR 오류] 이미지 형식이 올바르지 않거나 지원되지 않습니다. JPG/PNG 파일을 사용해주세요."

    # 503 / 내부 서버 오류
    if status_code in (500, 502, 503) or "server_error" in err_str:
        return "[OCR 오류] Gemini 서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    # 그 외 API 관련 오류
    if "ClientError" in err_type or "APIError" in err_type or "GoogleAPIError" in err_type:
        return f"[OCR 오류] API 요청 실패 ({err_type}). 잠시 후 다시 시도해주세요."

    # 알 수 없는 오류 — 원본 메시지에서 민감 정보(API 키) 제거 후 반환
    safe_msg = str(e)
    if len(safe_msg) > 200:
        safe_msg = safe_msg[:200] + "..."
    return f"[OCR 오류] 성적표 분석 중 예기치 못한 오류가 발생했습니다: {safe_msg}"


async def extract_grades_from_image(image_path: str | Path) -> str:
    """
    성적표 이미지를 Gemini Vision으로 분석하여 내신 등급 요약 텍스트를 반환합니다.

    Parameters
    ----------
    image_path : 분석할 이미지 파일 경로 (jpg / png / webp / gif 지원)

    Returns
    -------
    str : OCR 분석 결과 텍스트 (실패 시 사용자 친화적 오류 메시지)
    """
    image_path = Path(image_path)
    if not image_path.exists():
        return f"[OCR 오류] 파일을 찾을 수 없습니다: {image_path.name}"

    # load_dotenv()가 모듈 임포트 시점에 이미 호출됐지만,
    # 런타임에 환경변수가 업데이트된 경우를 대비해 매 호출마다 재조회
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    if not api_key:
        return (
            "[OCR 오류] GEMINI_API_KEY 환경변수가 설정되지 않았습니다. "
            ".env 파일에 GEMINI_API_KEY=your_key 를 추가해주세요."
        )

    loop = asyncio.get_event_loop()
    try:
        result: str = await loop.run_in_executor(
            None, _call_gemini_vision_sync, image_path, api_key
        )
        logger.info(f"[VisionOCR] 분석 완료: {image_path.name} → {len(result)}자")
        return result or "[OCR] 이미지에서 성적 정보를 인식하지 못했습니다."

    except ValueError as e:
        # API 키 미설정 fail-fast
        logger.error(f"[VisionOCR] 설정 오류: {e}")
        return f"[OCR 오류] 설정 오류: {e}"

    except RuntimeError as e:
        # 패키지 미설치
        logger.error(f"[VisionOCR] 패키지 오류: {e}")
        return f"[OCR 오류] {e}"

    except Exception as e:
        # google.genai.errors.ClientError / google.api_core.exceptions 등
        logger.error(f"[VisionOCR] API 호출 실패: {type(e).__name__}: {e}")
        return _classify_api_error(e)
