"""
대학 입시 정보 텔레그램 봇

data/student/parsed_admission_guide.json을 기반으로 학생 질문에 답합니다.
학생 프로필과 대화 이력은 data/student/user_profiles.json에 저장됩니다.

실행 방법:
    python scripts/telegram_agent.py

필요 환경변수 (.env):
    TELEGRAM_BOT_TOKEN=your_bot_token_here
    GEMINI_API_KEY=your_gemini_api_key_here  (AI 자연어 응답용)
"""

import asyncio
import subprocess
import sys
import os
import json
import logging
import re
import time
import random
import traceback
import threading
from pathlib import Path
from datetime import datetime
from typing import Any

# ─────────────────────────────────────────────────────────────
# 의존성 자동 설치
# ─────────────────────────────────────────────────────────────

REQUIRED = {
    "telegram": "python-telegram-bot[job-queue]>=20.0",
    "dotenv":   "python-dotenv",
    "google.genai": "google-genai",
    "fpdf":     "fpdf2",
    "reportlab": "reportlab",
    "cryptography": "cryptography",
    "pypdf":    "pypdf",
    "tenacity": "tenacity",
    "unidecode": "unidecode",
}

def 의존성_설치():
    for mod, pkg in REQUIRED.items():
        top = mod.split(".")[0]
        try:
            __import__(top)
        except ImportError:
            print(f"[설치] {pkg} 설치 중...")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pkg],
                stdout=subprocess.DEVNULL,
            )
            print(f"[설치] {pkg} 완료")

의존성_설치()

# db_manager는 의존성 설치 후 임포트 (cryptography/pypdf 필요)
sys.path.insert(0, str(Path(__file__).parent))
import db_manager          # noqa: E402
import storage_manager     # noqa: E402
import token_manager as _tm  # noqa: E402
import pdf_generator       # noqa: E402
import logger_factory as _lf  # noqa: E402
import vision_parser       # noqa: E402

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import Conflict, BadRequest, RetryAfter

# ─────────────────────────────────────────────────────────────
# 로거 선언 (핸들러는 main() → 로깅_설정() 에서 구성)
# ─────────────────────────────────────────────────────────────

logger              = logging.getLogger(__name__)
_활동_로거          = logging.getLogger("user_activity")
_데브옵스_에러_로거 = logging.getLogger("devops_errors")

# ─────────────────────────────────────────────────────────────
# 프로젝트 루트 — 환경변수·경로 참조 전에 반드시 먼저 정의
# ─────────────────────────────────────────────────────────────

프로젝트_루트 = Path(__file__).parent.parent

# ─────────────────────────────────────────────────────────────
# 환경변수 로드 (Graceful Boot Failure)
# ─────────────────────────────────────────────────────────────

def _boot_fatal(reason: str) -> None:
    """
    부팅 불가 상태를 system_events.jsonl 에 CRITICAL 로 기록한 뒤 종료합니다.
    web_dashboard.py 가 BOOT_FAILURE 이벤트를 읽어 관제센터에 표시합니다.
    """
    _msg = f"CRITICAL: 환경변수 로드 실패 - 봇 기동 불가 [{reason}]"
    logger.critical(_msg)
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import logger_factory as _lf
        _lf.log_event(
            "BOOT_FAILURE", "telegram_agent", _msg,
            level="CRITICAL",
            extra={"reason": reason},
        )
    except Exception as _le:
        logger.error(f"[BOOT] logger_factory 기록 실패 (무시): {_le}")
    sys.exit(1)


_boot_env_error: str = ""
try:
    _env_path = 프로젝트_루트 / ".env"
    if _env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(_env_path)
except Exception as _env_exc:
    _boot_env_error = f".env 파싱 오류: {_env_exc}"

# TELEGRAM_BOT_TOKEN 또는 TELEGRAM_TOKEN 둘 다 허용
TELEGRAM_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TELEGRAM_TOKEN")
    or ""
)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
try:
    ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0") or "0")
except ValueError:
    ADMIN_TELEGRAM_ID = 0

if _boot_env_error:
    _boot_fatal(_boot_env_error)

if not TELEGRAM_TOKEN:
    _boot_fatal(
        "TELEGRAM_BOT_TOKEN (또는 TELEGRAM_TOKEN) 미설정 — "
        ".env 파일에 TELEGRAM_BOT_TOKEN=your_token 추가 필요. "
        "봇 토큰 발급: https://t.me/BotFather"
    )

# ── 집중 학습 모드 판별 (system_config.json, 5초 TTL 캐시) ──────
_SYS_CONFIG_PATH  = 프로젝트_루트 / "system_config.json"
_TM_MODE_CACHE: dict = {"val": False, "ts": 0.0}
_TM_MODE_TTL    = 5.0


def _is_training_mode() -> bool:
    """system_config.json 에서 집중 학습 모드 여부를 읽습니다 (5초 TTL 캐시)."""
    import time as _t
    now = _t.monotonic()
    if now - _TM_MODE_CACHE["ts"] < _TM_MODE_TTL:
        return bool(_TM_MODE_CACHE["val"])
    val = False
    try:
        if _SYS_CONFIG_PATH.exists():
            cfg = json.loads(_SYS_CONFIG_PATH.read_text(encoding="utf-8"))
            val = bool(cfg.get("training_mode", False))
    except Exception:
        pass
    _TM_MODE_CACHE["val"] = val
    _TM_MODE_CACHE["ts"]  = now
    return val

# ─────────────────────────────────────────────────────────────
# 비평 엔진 설정 (환경변수 또는 코드로 토글)
#   "gemini"  → gemini-2.5-pro  (클라우드, 엄격 검증)
#   "ollama"  → localhost:11434  (로컬, 오프라인 가능)
# ─────────────────────────────────────────────────────────────

CRITIC_ENGINE: str = os.environ.get("CRITIC_ENGINE", "gemini").lower()
OLLAMA_URL:    str = os.environ.get("OLLAMA_URL",    "http://localhost:11434/api/generate")
OLLAMA_MODEL:  str = os.environ.get("OLLAMA_MODEL",  "gemma3")
CRITIC_PRO_MODEL = "gemini-2.5-pro"   # 비평 전용 고성능 모델

# ─────────────────────────────────────────────────────────────
# 데이터 경로  (프로젝트_루트 는 환경변수 로드 섹션에서 정의됨)
# ─────────────────────────────────────────────────────────────

입시_데이터_경로 = 프로젝트_루트 / "data" / "student" / "parsed_admission_guide.json"
프로필_경로     = 프로젝트_루트 / "data" / "student" / "user_profiles.json"
_LOCAL_AI_DATASET = 프로젝트_루트 / "data" / "logs" / "local_ai_dataset.jsonl"
프로필_경로.parent.mkdir(parents=True, exist_ok=True)

# ── 전용 에러 로그 경로 ────────────────────────────────────────
_텔레그램_에러_로그 = 프로젝트_루트 / "data" / "fix_error" / "telegram_errors.log"
_사용자_로그_루트   = 프로젝트_루트 / "data" / "logs" / "users"

# ── 주기적 백그라운드 크롤러 설정 ─────────────────────────────
_SEED_SCRIPT        = Path(__file__).parent / "seed_seteuk_db.py"
_ADMISSIONS_SCRIPT  = Path(__file__).parent / "seed_admissions_stats.py"
_CRAWLER_MAJORS = [
    "환경공학과",    # CRITICAL: 부팅 직후 우선 처리
    "전자공학과",
    "생명과학과",
    "화학공학과",
    "경제학과",
    "심리학과",
    "정치외교학과",
]
_CRAWLER_INTERVAL_H = 12   # 학과 간 대기 시간 (시간)

# ── asyncio 백그라운드 태스크 (Task destroyed 방지) ───────────
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background(coro, *, name: str = "bg") -> asyncio.Task:
    """백그라운드 코루틴을 추적·등록합니다 (종료 시 cancel, 예외 로깅)."""
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error(f"[백그라운드/{name}] {type(exc).__name__}: {exc}")

    task.add_done_callback(_on_done)
    return task


async def _cancel_background_tasks() -> None:
    """봇 종료 시 pending 백그라운드 태스크를 정리합니다."""
    if not _BACKGROUND_TASKS:
        return
    for t in list(_BACKGROUND_TASKS):
        t.cancel()
    await asyncio.gather(*list(_BACKGROUND_TASKS), return_exceptions=True)
    _BACKGROUND_TASKS.clear()


def _telegram_plain(text: str) -> str:
    """Markdown 파싱 실패 시 사용할 plain 텍스트."""
    return re.sub(r"[*_`\[\]]", "", str(text))[:4096]


def _esc(text: str) -> str:
    """MarkdownV2 특수문자 이스케이프 — 사용자 입력값을 안전하게 삽입할 때 사용."""
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\-])', r'\\\1', str(text))


def _cleanup_photo_temps(context: ContextTypes.DEFAULT_TYPE) -> None:
    """photo_cart 임시 파일을 디스크에서 삭제하고 관련 user_data 키를 제거합니다."""
    for path_str in context.user_data.pop("photo_cart", []):
        try:
            Path(path_str).unlink(missing_ok=True)
        except Exception:
            pass
    # 구형 키도 정리 (호환성)
    for path_str in context.user_data.pop("photo_paths", []):
        try:
            Path(path_str).unlink(missing_ok=True)
        except Exception:
            pass
    # 디바운스 태스크 및 상태 메시지 ID 정리
    task = context.user_data.pop("_debounce_task", None)
    if task and not task.done():
        task.cancel()
    context.user_data.pop("_cart_msg_id", None)
    context.user_data.pop("_processed_groups", None)


async def _safe_bot_send_message(bot, chat_id: int, text: str, **kwargs):
    """
    Telegram send_message — Can't parse entities 시 plain text 폴백.
    RetryAfter 시 대기 후 1회 재시도.
    """
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except BadRequest as e:
        err = str(e).lower()
        if "parse entities" in err or "can't parse" in err:
            logger.warning("[Telegram] Markdown 파싱 실패 → plain 재전송")
            kw = dict(kwargs)
            kw.pop("parse_mode", None)
            return await bot.send_message(
                chat_id=chat_id, text=_telegram_plain(text), **kw
            )
        raise
    except RetryAfter as e:
        logger.warning(f"[Telegram] Rate Limit - {e.retry_after}초 후 재시도")
        await asyncio.sleep(e.retry_after)
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)


# ─────────────────────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────────────────────

def 로깅_설정():
    """로거 핸들러 및 포맷을 설정합니다."""
    log_level = logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    formatter = logging.Formatter(log_format, date_format)

    # 콘솔 핸들러
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    _활동_로거.addHandler(console_handler)
    _데브옵스_에러_로거.addHandler(console_handler)

    # 파일 핸들러 (에러 로그) — mode='a' 명시하여 재시작 시 로그 보존
    _텔레그램_에러_로그.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(_텔레그램_에러_로그, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 사용자별 활동 로그 핸들러 (동적 생성)
    _사용자_로그_루트.mkdir(parents=True, exist_ok=True)
    _활동_로거.propagate = False  # 루트 로거에 중복 기록 방지

    # Gemini API 에러 로깅 (ClientError 429 처리)
    gemini_error_logger = logging.getLogger("gemini_api_errors")
    gemini_error_logger.setLevel(logging.ERROR)
    gemini_error_file_handler = logging.FileHandler(
        _텔레그램_에러_로그.parent / "gemini_api_errors.log",
        mode="a", encoding="utf-8"
    )
    gemini_error_file_handler.setFormatter(formatter)
    gemini_error_logger.addHandler(gemini_error_file_handler)

    # Devops 에러 로깅
    devops_error_file_handler = logging.FileHandler(
        _텔레그램_에러_로그.parent / "devops_errors.log",
        mode="a", encoding="utf-8"
    )
    devops_error_file_handler.setLevel(logging.ERROR)
    devops_error_file_handler.setFormatter(formatter)
    _데브옵스_에러_로거.addHandler(devops_error_file_handler)
    _데브옵스_에러_로거.propagate = False

    logger.setLevel(log_level)


# ─────────────────────────────────────────────────────────────
# AI 모델 및 서비스 초기화
# ─────────────────────────────────────────────────────────────

# Gemini API 클라이언트 (API 키 로드 후 초기화)
_AI = None
if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        _AI = genai.GenerativeModel("gemini-2.5-flash-lite")
        logger.info("[AI] Gemini API 클라이언트 초기화 완료 (gemini-2.5-flash-lite)")
    except Exception as e:
        logger.error(f"[AI] Gemini API 클라이언트 초기화 실패: {e}")
        _AI = None
else:
    logger.warning("[AI] GEMINI_API_KEY 미설정 → Gemini API 비활성화")


# ─────────────────────────────────────────────────────────────
# 데이터 관리자 초기화
# ─────────────────────────────────────────────────────────────

# 프로필 관리자
_프로필 = storage_manager.UserProfileManager(프로필_경로)
_프로필.load()

# 요청 카운터
_요청 = storage_manager.RequestCounter(프로젝트_루트 / "data" / "request_counter.json")
_요청.load()

# ─────────────────────────────────────────────────────────────
# 프로필 위자드 — ConversationHandler 상태 & 필드 맵
# ─────────────────────────────────────────────────────────────

(SELECT_ACTION, WAITING_FOR_MAJOR, WAITING_FOR_GPA, WAITING_FOR_MOCK,
 WAITING_FOR_PHOTO, WAITING_FOR_MORE_PHOTOS, CONFIRM_OCR_RESULT,
 WAITING_FOR_GRADE_LEVEL, WAITING_FOR_HS_TYPE, WAITING_FOR_KEYWORDS,
 WAITING_FOR_CSAT, WAITING_FOR_BULK_INPUT) = range(12)


def _build_profile_card(
    user_profile: dict | None, first_name: str
) -> tuple[str, InlineKeyboardMarkup]:
    """프리미엄 컨설팅 대시보드 스타일 프로필 카드와 버튼 키보드를 반환합니다."""
    p = user_profile or {}

    def _v(key: str, fallback: str = "미입력") -> str:
        """HTML escape + 미입력 처리."""
        val = p.get(key) or fallback
        return val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    major            = _v("target_major")
    gpa              = _v("grade_raw")
    mock             = _v("mock_exam")
    current_grade    = _v("current_grade")
    highschool_type  = _v("highschool_type")
    target_keywords  = _v("target_keywords")
    csat_subjects    = _v("csat_subjects")

    # 세부 성적표 상태
    detail_grade = p.get("grade_raw", "")
    grade_status = "입력됨 ✅" if detail_grade and len(detail_grade) > 10 else "미입력"

    # 필수 정보 완성도 계산
    required_filled = sum([
        bool(p.get("target_major")),
        bool(p.get("grade_raw")),
        bool(p.get("mock_exam")),
    ])
    optional_filled = sum([
        bool(p.get("current_grade")),
        bool(p.get("highschool_type")),
        bool(p.get("target_keywords")),
        bool(p.get("csat_subjects")),
    ])

    fn = first_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = (
        f"<b>📋 {fn}님의 AI 입시 상담 대시보드</b>\n"
        f"{'─' * 28}\n\n"
        f"<b>[필수 정보]</b>  ({required_filled}/3 입력)\n"
        f"🎯 희망 학과: <b>{major}</b>\n"
        f"📊 내신 등급: <b>{gpa}</b>\n"
        f"📝 모의고사:  <b>{mock}</b>\n\n"
        f"<b>[선택 정보 - 정밀 분석용]</b>  ({optional_filled}/4 입력)\n"
        f"🏫 현재 학년:       {current_grade}\n"
        f"🏛 고교 유형:       {highschool_type}\n"
        f"🔬 주력 세특 키워드: {target_keywords}\n"
        f"📚 수능 선택 과목:  {csat_subjects}\n"
        f"📷 세부 성적표:     {grade_status}\n\n"
        f"💡 선택 정보를 많이 입력할수록 대치동급 초정밀 AI 처방전이 발급됩니다."
    )
    keyboard = InlineKeyboardMarkup([
        # 필수 정보
        [
            InlineKeyboardButton("🎯 희망 학과",  callback_data="set_major"),
            InlineKeyboardButton("📊 내신 등급",  callback_data="set_gpa"),
        ],
        [
            InlineKeyboardButton("📝 모의고사",   callback_data="set_mock"),
            InlineKeyboardButton("📷 성적표 사진 자동입력", callback_data="upload_photo"),
        ],
        # 선택 정보
        [
            InlineKeyboardButton("🏫 현재 학년",  callback_data="set_grade_level"),
            InlineKeyboardButton("🏛 고교 유형",  callback_data="set_hs_type"),
        ],
        [
            InlineKeyboardButton("🔬 세특 키워드", callback_data="set_keywords"),
            InlineKeyboardButton("📚 수능 선택",  callback_data="set_csat"),
        ],
        [InlineKeyboardButton("💬 텍스트 또는 자연어로 한 번에 입력하기", callback_data="bulk_input")],
        [InlineKeyboardButton("✅ 입력 완료 (처방전 발급 가능)", callback_data="done")],
    ])
    return text, keyboard


# ─────────────────────────────────────────────────────────────
# 봇 핸들러 및 콜백
# ─────────────────────────────────────────────────────────────

async def _log_user_activity(
    user_id: int, username: str, chat_id: int, message: str, action: str = "message"
):
    """사용자 활동을 기록합니다."""
    user_log_dir = _사용자_로그_루트 / f"{username}_{user_id}"
    user_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = user_log_dir / "actions.log"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} [{action}] ChatID:{chat_id} | {message}\n"

    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        logger.error(f"사용자 활동 로그 기록 실패 ({username}): {e}")


async def _gemini_진단_리포트(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    error: Exception,
    user_input: str = "",
    chat_id: int = 0,
    extra_info: dict = None,
):
    """
    Gemini API 호출 중 발생한 에러를 진단하고 리포트합니다.
    특히 429 Resource Exhausted 에러에 대한 재시도 지연 시간을 파싱합니다.
    """
    from tenacity import retry, stop_after_attempt, wait_fixed, wait_random

    extra_info = extra_info or {}
    error_type = type(error).__name__
    error_message = str(error)
    traceback_str = traceback.format_exc()

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "error_type": error_type,
        "error_message": error_message,
        "traceback": traceback_str,
        "context": f"진단에이전트._Gemini_진단_리포트",
        "user_input": user_input,
        "chat_id": chat_id,
        **extra_info,
    }

    # ai_runtime_errors.json 에 기록
    error_log_path = 프로젝트_루트 / "data" / "fix_error" / "ai_runtime_errors.json"
    error_log_path.parent.mkdir(parents=True, exist_ok=True)

    existing_errors = []
    if error_log_path.exists():
        try:
            with open(error_log_path, "r", encoding="utf-8") as f:
                existing_errors = json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"기존 에러 로그 파일 파싱 실패: {error_log_path}")
        except Exception as e:
            logger.error(f"기존 에러 로그 파일 로드 실패: {e}")

    existing_errors.append(log_entry)

    try:
        with open(error_log_path, "w", encoding="utf-8") as f:
            json.dump(existing_errors, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"에러 로그 파일 저장 실패: {e}")

    # 429 Resource Exhausted 에러 처리
    if error_type == "ClientError" and "429 RESOURCE_EXHAUSTED" in error_message:
        retry_delay = 30  # 기본 재시도 지연 시간 (초)
        try:
            # 에러 메시지에서 재시도 지연 시간 파싱 시도
            match = re.search(r"Please retry in (\d+(\.\d+)?)s", error_message)
            if match:
                retry_delay = float(match.group(1))
                logger.warning(f"Gemini API 429 에러: {retry_delay:.2f}초 후 재시도 필요.")
            else:
                logger.warning("Gemini API 429 에러: 재시도 지연 시간 파싱 실패, 기본 30초 대기.")
        except Exception as e:
            logger.error(f"Gemini API 429 에러 파싱 중 오류: {e}")

        # Exponential Backoff 재시도 로직
        @retry(
            wait=wait_random(min=retry_delay, max=retry_delay + 10) + wait_fixed(retry_delay),
            stop=stop_after_attempt(3),
            reraise=True,
        )
        async def _retry_gemini_call():
            logger.info(f"Gemini API 재시도 중 (지연 시간: {retry_delay:.2f}초)...")
            # 실제 Gemini API 호출 로직 (이 함수 내에서 다시 호출)
            # 이 예시에서는 실제 API 호출 대신 에러를 다시 발생시켜 재시도 로직 테스트
            # 실제 구현 시에는 여기서 원래 호출하려던 Gemini API 함수를 호출해야 합니다.
            # 예: return await _call_gemini_api(...)
            raise ClientError(f"Simulated retry after {retry_delay}s") # 테스트용

        try:
            await _retry_gemini_call()
        except Exception as retry_exc:
            logger.error(f"Gemini API 재시도 실패: {retry_exc}")
            # 재시도 실패 시 사용자에게 알림
            if update and update.effective_chat:
                await _safe_bot_send_message(
                    context.bot,
                    update.effective_chat.id,
                    "죄송합니다. 현재 AI 응답에 문제가 발생했습니다. 잠시 후 다시 시도해주세요.",
                )
            return

    # 일반 에러 로깅 (Gemini API 에러가 아닌 경우)
    else:
        logger.error(
            f"[Gemini 진단] 에러 발생: {error_type} - {error_message}\n"
            f"  컨텍스트: {log_entry['context']}\n"
            f"  사용자 입력: {user_input}\n"
            f"  Traceback: {traceback_str}"
        )
        # 관리자에게 알림 (선택 사항)
        if ADMIN_TELEGRAM_ID:
            await _safe_bot_send_message(
                context.bot,
                ADMIN_TELEGRAM_ID,
                f"🚨 Gemini API 에러 발생!\n"
                f"  타입: {error_type}\n"
                f"  메시지: {error_message[:200]}...\n"
                f"  사용자 입력: {user_input[:100]}...",
            )


# ── 비동기 사후 검증 파이프라인 상수 ────────────────────────────
_OLLAMA_UX_CAPTION = (
    "⚠️ 현재 시스템 접속량 증가로 로컬 AI가 1차 진단을 수행했습니다. "
    "추후 메인 클라우드 AI의 정밀 검증이 완료되면 "
    "보완된 최종 리포트를 추가로 발송해 드립니다."
)

_입시_시스템_프롬프트 = (
    "SYSTEM: 너는 대한민국 최고의 대입 전문 AI 컨설턴트 'UnivAgent'야. "
    "사용자의 질문에 반드시 100% 한국어(Korean)로만 대답해야 해. "
    "영어는 절대 사용하지 마. 친절하고 전문적인 존댓말을 사용해. "
    "학생의 내신 등급, 모의고사 성적, 희망학과를 고려하여 "
    "최신 대입 전형 정보를 기반으로 정확하고 구체적인 입시 조언을 제공하세요. "
    "핵심 위주로 500자 이내로 답변하세요."
)

# ── 진단 처방전 PDF 생성 상수 ─────────────────────────────────
# Section D에서 Groq/Ollama가 영어로 출력하는 문제를 원천 차단하는 강제 규칙
_DRACONIAN_KOREAN_RULE = (
    "CRITICAL SYSTEM RULE: You are a Korean College Admission Expert. "
    "You MUST write the ENTIRE response in 100% Korean (한국어). "
    "ABSOLUTELY NO ENGLISH ALLOWED. "
    "DO NOT output introductory or filler phrases like "
    "'Here are two potential...', 'Here is...', 'Certainly!'. "
    "Output ONLY the Korean content directly."
)

_REPORT_SYSTEM_PROMPT = (
    _DRACONIAN_KOREAN_RULE + "\n\n"
    "당신은 대한민국 최고의 대학 입시 전문 컨설턴트입니다. "
    "학생 프로필을 분석하여 정확히 아래 5개의 섹션 태그를 사용한 완전한 입시 처방전을 작성하세요. "
    "각 태그는 반드시 독립된 줄의 맨 앞에 정확히 기재하며, 태그 외 서문·인사말은 절대 금지. "
    "모든 내용은 100% 한국어로만 작성하세요.\n\n"
    "필수 섹션 태그 규칙:\n"
    "[섹션 A] — 관심대학 지원 가능성 정밀 진단.\n"
    "  현황 분석 2~3문장 후, CSV 테이블 출력. 헤더: 대학명,전형명,기준등급,현재등급,판정\n"
    "  4개 대학. 판정은 '안정'/'적정'/'상향' 중 하나. 현재등급란에는 학생의 실제 내신 기재.\n"
    "[섹션 B] — 성적대별 대안 대학 및 전형 추천.\n"
    "  대안 전략 2~3문장 후, CSV 테이블 출력 (동일 헤더). 4개 대학. 안정·적정 위주 구성.\n"
    "[섹션 C] — 성적 향상 시나리오별 목표 확장 가이드.\n"
    "  '시나리오1:', '시나리오2:', '시나리오3:' 형식으로 3가지 제시.\n"
    "  각 시나리오는 내신 향상 폭(+0.3, +0.5, +1.0 등)과 달성 시 지원 가능 대학 변화 기술.\n"
    "[섹션 D] — 세특 공백 보완용 초정밀 탐구 보고서 레시피.\n"
    "  '주제1:', '주제2:' 형식으로 2가지 제시. 각 주제는 '키워드:', '1단계:', '2단계:', '3단계:' 포함.\n"
    "[팩트 체크] — 이 학생이 반드시 알아야 할 핵심 입시 규정 2~3가지. 구체적 전형명·수치 포함.\n"
)

_REPORT_PROMPT_TEMPLATE = """\
[학생 프로필]
- 희망학과: {major}
- 내신 등급: {grade}
- 모의고사 성적: {mock}
- 고교 유형: {school}

위 학생을 위한 완전한 입시 처방전을 아래 섹션 태그 형식으로 작성하세요.
각 태그는 반드시 독립된 줄 맨 앞에 작성하고, 태그 사이에 관련 내용만 기재하세요.
반드시 한국어로만 작성하고, 영어 단어·문장은 절대 사용하지 마세요.

[섹션 A]
(현재 내신 {grade}등급, 모의고사 {mock} 기준 지원 가능성 분석 — 2~3문장)
대학명,전형명,기준등급,현재등급,판정
(관심 대학 4곳 CSV 행. 현재등급={grade}. 판정은 안정/적정/상향 중 하나)

[섹션 B]
(성적대별 대안 대학 전략 설명 — 2~3문장)
대학명,전형명,기준등급,현재등급,판정
(대안 대학 4곳 CSV 행. 현재등급={grade}. 안정·적정 위주 구성)

[섹션 C]
시나리오1: (내신 {grade}등급 → 현재 유지 시 가능한 최선 전략과 목표 대학)
시나리오2: (내신 0.3~0.5등급 향상 시 새롭게 가능해지는 대학·전형 변화)
시나리오3: (내신 0.5~1.0등급 이상 향상 시 도달 가능한 최상위 목표 대학)

[섹션 D]
주제1: ({major} 연계 탐구 주제 제목)
키워드: (핵심 키워드 3가지)
1단계: (탐구 준비 및 배경 조사 방법)
2단계: (실험/조사/분석 방법)
3단계: (결론 도출 및 세특 기재 방향)

주제2: (두 번째 탐구 주제 제목)
키워드: (핵심 키워드 3가지)
1단계: (탐구 준비 방법)
2단계: (실험/분석 방법)
3단계: (결론 및 세특 기재 방향)

[팩트 체크]
(이 학생이 반드시 알아야 할 {major} 관련 입시 규정 핵심 사항 2~3가지. 구체적 전형명·수치 포함)
"""

# ── 역추천 모드 (희망학과 미입력 시) ──────────────────────────

_RECOMMENDATION_SYSTEM_PROMPT = (
    _DRACONIAN_KOREAN_RULE + "\n\n"
    "사용자가 희망 학과를 입력하지 않았습니다. "
    "주어진 '내신 성적(GPA)'을 바탕으로 지원 가능한 가장 유망한 학과 3개를 역으로 추천하세요. "
    "그리고 추천된 학과에 대해 다음 3가지를 반드시 포함하세요: "
    "1) 무엇을 배우는가(주요 과목), 2) 졸업 후 어떤 일을 하는가(직무), "
    "3) 주로 어떤 기업/기관에 취업하는가."
)

_RECOMMENDATION_PROMPT_TEMPLATE = """\
[학생 프로필]
- 희망학과: 미입력 (성적 기반 역추천 모드)
- 내신 등급: {grade}
- 모의고사 성적: {mock}
- 고교 유형: {school}

위 학생을 위해 내신 성적 기반으로 적합한 처방전을 아래 섹션 태그 형식으로 작성하세요.
반드시 한국어로만 작성하고, 영어 단어·문장은 절대 사용하지 마세요.

[섹션 A]
(내신 {grade}등급 기준 현황 분석 및 추천 가능 학과 범위 — 2~3문장)
대학명,전형명,기준등급,현재등급,판정
(내신 {grade}에 맞는 추천 대학 4곳 CSV. 현재등급={grade}. 판정은 안정/적정/상향)

[섹션 B]
(성적대별 대안 전략 및 하향·상향 지원 범위 설명 — 2~3문장)
대학명,전형명,기준등급,현재등급,판정
(대안 대학 4곳 CSV. 현재등급={grade}. 안정·적정 위주)

[섹션 C]
시나리오1: (현재 {grade}등급 유지 시 수시/정시 최선 전략)
시나리오2: (내신 0.3~0.5등급 향상 시 새롭게 가능한 대학·전형)
시나리오3: (내신 0.5등급 이상 향상 시 도달 가능한 최상위 목표)

[섹션 D]
주제1: (내신 {grade}등급 학생에게 적합한 추천 학과 연계 탐구 주제 1)
키워드: (핵심 키워드 3가지)
1단계: (탐구 준비 방법)
2단계: (분석 방법)
3단계: (결론 및 세특 기재 방향)

주제2: (두 번째 탐구 주제)
키워드: (핵심 키워드 3가지)
1단계: (탐구 준비 방법)
2단계: (분석 방법)
3단계: (결론 및 세특 기재 방향)

[팩트 체크]
(이 학생에게 중요한 수시/정시 핵심 입시 규정 2~3가지. 구체적 전형명·수치 포함)
"""


async def _백그라운드_학과_저장(major_name: str, report_text: str) -> None:
    """LLM 리포트 텍스트에서 학과 정보를 파싱해 major_knowledge DB에 저장합니다."""
    import re
    loop = asyncio.get_event_loop()

    def _extract(pattern: str) -> str:
        m = re.search(pattern, report_text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip()[:3000] if m else ""

    curriculum  = _extract(r"##\s*주요 교육과정\s*(.*?)(?:##|\Z)")
    career      = _extract(r"##\s*졸업 후 직무\s*(.*?)(?:##|\Z)")
    employment  = _extract(r"##\s*주요 취업 기업[··]?기관\s*(.*?)(?:#|\Z)")

    if not any([curriculum, career, employment]):
        return

    try:
        await loop.run_in_executor(
            None,
            lambda: db_manager.save_major_info(major_name, curriculum, career, employment),
        )
        logger.info(f"[학과지식DB] 백그라운드 저장 완료: {major_name}")
    except Exception as e:
        logger.warning(f"[학과지식DB] 저장 실패 (무시): {e}")


async def _질문_처리_및_큐잉(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    질문: str,
    chat_id: int,
    user_id: int,
    username: str,
) -> None:
    """
    3-Tier LLM 라우팅으로 입시 질문에 답변합니다.

    - 일반 모드: Gemini → Groq → Ollama 3-Tier
    - 집중 학습 모드: Ollama 전용
    - Ollama 응답 시 무조건: UX 경고 캡션 발송 + pending_verifications 큐잉
    """
    loop = asyncio.get_event_loop()
    await update.message.chat.send_action("typing")

    if _is_training_mode():
        # ── 학습 모드 경고 메시지 ────────────────────────────────
        await _safe_bot_send_message(
            context.bot, chat_id,
            "⚠️ *현재 AI 집중 학습 및 데이터 포집 기간입니다.*\n"
            "시스템 자원 할당으로 인해 로컬 엔진으로 답변을 생성하여 다소 느릴 수 있습니다.",
            parse_mode=ParseMode.MARKDOWN,
        )
        _prompt = f"{_입시_시스템_프롬프트}\n\n질문: {질문}"
        답변, engine = await loop.run_in_executor(
            None,
            lambda: _tm.generate_text_sync(_prompt, "", "ollama"),
        )
    else:
        # ── 일반 3-Tier 라우팅 ───────────────────────────────────
        _profile = _프로필.get_user(user_id) or {}
        _ctx = ""
        if _profile:
            _ctx = (
                f"학생 프로필: 희망학과={_profile.get('target_major', '미입력')}, "
                f"내신={_profile.get('grade_raw', '미입력')}, "
                f"고교유형={_profile.get('school_type', '미입력')}\n\n"
            )
        _prompt = f"{_입시_시스템_프롬프트}\n\n{_ctx}질문: {질문}"
        답변, engine = await loop.run_in_executor(
            None,
            lambda: _tm.generate_text_sync(_prompt, ""),
        )

    if not 답변:
        await _safe_bot_send_message(
            context.bot, chat_id,
            "죄송합니다. 현재 AI 엔진에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.",
        )
        return

    # ── 1차 답변 전송 ────────────────────────────────────────────
    await _safe_bot_send_message(
        context.bot, chat_id, 답변, parse_mode=ParseMode.MARKDOWN
    )
    _요청.increment()

    # ── Ollama 폴백 감지: UX 경고 캡션 + pending_verifications 큐잉 ──
    if "Ollama" in engine:
        await _safe_bot_send_message(context.bot, chat_id, _OLLAMA_UX_CAPTION)
        try:
            await loop.run_in_executor(
                None,
                lambda: db_manager.pending_verification_추가(user_id, 질문, 답변),
            )
            logger.info(
                f"[검증대기열] 큐잉 완료: user_id={user_id}, engine={engine}"
            )
        except Exception as _qe:
            logger.warning(f"[검증대기열] 큐잉 실패 (무시): {_qe}")

    await _log_user_activity(
        user_id, username, chat_id,
        f"AI 질문: {질문[:50]}", action="ai_question",
    )


async def _handle_gemini_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    chat_id: int,
    user_id: int,
    username: str,
    model_name: str = "gemini-2.5-flash-lite",
):
    """Gemini API 호출 및 응답 처리를 담당합니다."""
    global _AI
    if not _AI or not GEMINI_API_KEY:
        await _safe_bot_send_message(
            context.bot, chat_id, "죄송합니다. 현재 AI 기능이 비활성화되어 있습니다."
        )
        return

    start_time = time.monotonic()
    response_text = ""
    try:
        # Gemini API 호출
        # API 레이트 리밋 (429) 에러 처리를 위해 _Gemini_진단_리포트 함수 내에서 재시도 로직 구현
        # 여기서는 직접 API 호출
        gemini_model = genai.GenerativeModel(model_name)
        response = await gemini_model.generate_content_async(prompt)

        if response.candidates:
            response_text = response.text
        else:
            response_text = "죄송합니다. AI 응답을 생성하는 데 실패했습니다. 다시 시도해주세요."
            # Gemini API 에러 로깅 (응답이 없는 경우)
            await _gemini_진단_리포트(
                update, context,
                error=RuntimeError("Gemini API returned no candidates"),
                user_input=prompt,
                chat_id=chat_id,
                extra_info={"model": model_name, "response_parts": response.parts},
            )

    except Exception as e:
        # Gemini API 에러 로깅 (ClientError 429 포함)
        await _gemini_진단_리포트(
            update, context,
            error=e,
            user_input=prompt,
            chat_id=chat_id,
            extra_info={"model": model_name},
        )
        response_text = "죄송합니다. AI 응답 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    end_time = time.monotonic()
    duration = end_time - start_time

    # 응답이 비어있지 않으면 사용자에게 전송
    if response_text:
        await _safe_bot_send_message(
            context.bot, chat_id, response_text, parse_mode=ParseMode.MARKDOWN
        )
        _활동_로거.info(
            f"User: {user_id} ({username}) | ChatID: {chat_id} | Prompt: {prompt[:50]}... | "
            f"Response: {response_text[:50]}... | Model: {model_name} | Duration: {duration:.2f}s"
        )
        _요청.increment()
        await _log_user_activity(user_id, username, chat_id, f"Gemini Prompt: {prompt[:50]}...", action="gemini_query")
    else:
        # 에러 발생 시에도 사용자에게 알림
        await _safe_bot_send_message(
            context.bot, chat_id, "죄송합니다. AI 응답을 생성하는 데 실패했습니다. 다시 시도해주세요."
        )


def _온보딩_키보드() -> InlineKeyboardMarkup:
    """온보딩 메시지용 3버튼 인라인 키보드."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📝 내 프로필(성적/희망학과) 설정하기", callback_data="menu_profile"
        )],
        [InlineKeyboardButton(
            "⚡ AI 진단 처방전 즉시 발급", callback_data="menu_analyze"
        )],
        [InlineKeyboardButton(
            "❓ 이용 방법 안내", callback_data="menu_help"
        )],
    ])


async def _send_온보딩_메시지(bot, chat_id: int, first_name: str) -> None:
    """신규·재방문 사용자 공통 온보딩 메시지를 전송합니다."""
    text = (
        f"🎓 안녕하세요\\! AI 맞춤형 대입 컨설턴트 *UnivAgent*입니다\\.\n\n"
        f"지원자님의 내신 성적과 희망 학과를 바탕으로, "
        f"빅데이터 기반의 가장 유리한 대입 전략과 "
        f"'세특 탐구 보고서 주제'를 추천해 드립니다\\.\n\n"
        f"👇 아래 버튼을 눌러 바로 시작해 보세요\\!"
    )
    await bot.send_message(
        chat_id, text,
        reply_markup=_온보딩_키보드(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    await _log_user_activity(user.id, username, chat_id, "/start", action="command")

    if not _프로필.get_user(user.id):
        _프로필.add_user(user.id, user.username, user.first_name)
        _프로필.save()

    await _send_온보딩_메시지(context.bot, chat_id, user.first_name)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/menu 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    await _log_user_activity(user.id, username, chat_id, "/menu", action="command")

    keyboard = [
        [
            InlineKeyboardButton("🎓 대학/학과 검색", callback_data="menu_search"),
            InlineKeyboardButton("📝 전형 상세 정보", callback_data="menu_detail"),
        ],
        [
            InlineKeyboardButton("📊 학생부 분석", callback_data="menu_analyze"),
            InlineKeyboardButton("👤 내 프로필", callback_data="menu_profile"),
        ],
        [
            InlineKeyboardButton("❓ 도움말", callback_data="menu_help"),
            InlineKeyboardButton("💡 기타 문의", callback_data="menu_query"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _safe_bot_send_message(
        context.bot, chat_id, "무엇을 도와드릴까요?", reply_markup=reply_markup
    )


async def cmd_도움말(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    await _log_user_activity(user.id, username, chat_id, "/help", action="command")

    help_text = (
        "**대학 입시 정보 봇 도움말**\n\n"
        "이 봇은 대학 입시 관련 정보를 제공합니다.\n\n"
        "**주요 명령어:**\n"
        "- `/start`: 봇 시작 및 환영 메시지\n"
        "- `/menu`: 주요 기능 메뉴 보기\n"
        "- `/help`: 이 도움말 보기\n"
        "- `/list`: 수록된 대학 목록 보기\n"
        "- `/search [학과명]`: 특정 학과를 개설한 대학 검색\n"
        "  예: `/search 신소재공학과`\n"
        "- `/detail [대학명] [전형명]`: 특정 대학의 전형 상세 정보 보기\n"
        "  예: `/detail 서울대학교 수시 일반전형`\n"
        "- `/add [대학명] [전형명]`: 관심 대학/전형 목록에 추가\n"
        "- `/profile`: 내 프로필 확인 및 수정 (희망 학과, 내신 등)\n"
        "- `/analyze`: 학생부 PDF 파일을 업로드하여 분석 요청\n"
        "- `/request [질문]`: AI에게 자유롭게 질문하기\n"
        "- `/setreport [대학명] [전형명]`: 맞춤 입시 리포트 설정\n"
        "- `/reportnow`: 설정된 맞춤 리포트 즉시 생성\n\n"
        "**AI 기능:**\n"
        "봇에게 자유롭게 질문하면 AI가 답변해 드립니다. (예: `내신 2.5에 신소재학과 가려면 어떤 대학이 좋을까?`)\n\n"
        "**주의사항:**\n"
        "- AI 답변은 참고용이며, 최종 결정은 본인의 판단으로 하시기 바랍니다.\n"
        "- 학생부 분석은 PDF 파일만 지원합니다.\n"
        "- 일부 기능은 Gemini API 키가 필요합니다."
    )
    await _safe_bot_send_message(context.bot, chat_id, help_text, parse_mode=ParseMode.MARKDOWN)


async def cmd_대학목록(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    await _log_user_activity(user.id, username, chat_id, "/list", action="command")

    universities = db_manager.입시_대학_목록()
    if not universities:
        await _safe_bot_send_message(context.bot, chat_id, "죄송합니다. 현재 등록된 대학 정보가 없습니다.")
        return

    message = "현재 봇에서 제공하는 대학 목록입니다:\n\n"
    for univ in sorted(universities):
        message += f"- {univ}\n"

    await _safe_bot_send_message(context.bot, chat_id, message)


async def cmd_검색(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/search 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    query = " ".join(context.args).strip()
    if not query:
        await _safe_bot_send_message(
            context.bot, chat_id, "검색할 학과명을 입력해주세요. 예: `/search 신소재공학과`"
        )
        return

    await _log_user_activity(user.id, username, chat_id, f"/search {query}", action="command")

    # 대학명 키워드 검색 + 전형 목록에서 학과/전형명 키워드 교차 검색
    univ_matches = db_manager.입시_대학_검색(query)
    # 전체 대학 목록을 돌며 전형명에 키워드가 포함된 대학도 수집
    all_univs = db_manager.입시_대학_목록()
    plan_matches: list[str] = []
    for u in all_univs:
        if u in univ_matches:
            continue
        plans = db_manager.입시_전형_검색(u, query)
        if plans:
            plan_matches.append(u)
    combined = univ_matches + plan_matches

    if not combined:
        await _safe_bot_send_message(
            context.bot, chat_id, f"'{query}'에 해당하는 대학/전형을 찾을 수 없습니다. 다른 키워드로 다시 시도해주세요."
        )
        return

    message = f"**'{query}' 관련 대학 검색 결과:**\n\n"
    for univ in sorted(combined):
        plans = db_manager.입시_전형_검색(univ, query) or db_manager.입시_전형_검색(univ, "")
        message += f"*{univ}*\n"
        for p in plans[:3]:
            message += f"  - {p.get('전형명', '전형명 없음')}\n"
        if len(plans) > 3:
            message += f"  - 외 {len(plans) - 3}개 전형\n"
        message += "\n"

    await _safe_bot_send_message(context.bot, chat_id, message, parse_mode=ParseMode.MARKDOWN)


async def cmd_전형(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/detail 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    args = context.args
    if len(args) < 2:
        await _safe_bot_send_message(
            context.bot,
            chat_id,
            "대학명과 전형명을 입력해주세요. 예: `/detail 서울대학교 수시 일반전형`",
        )
        return

    university_name = args[0]
    admission_plan_name = " ".join(args[1:])

    await _log_user_activity(
        user.id, username, chat_id, f"/detail {university_name} {admission_plan_name}", action="command"
    )

    plans = db_manager.입시_전형_검색(university_name, admission_plan_name)
    detail = plans[0] if plans else None

    if not detail:
        await _safe_bot_send_message(
            context.bot,
            chat_id,
            f"'{university_name}'의 '{admission_plan_name}' 전형 정보를 찾을 수 없습니다. "
            "대학명과 전형명을 정확히 입력했는지 확인해주세요.",
        )
        return

    message = f"**{university_name} - {detail.get('전형명', admission_plan_name)} 상세 정보**\n\n"
    message += f"**모집 단위:** {detail.get('모집단위', '정보 없음')}\n"
    message += f"**모집 인원:** {detail.get('모집인원', '정보 없음')}\n"
    message += f"**지원 자격:** {detail.get('지원자격', '정보 없음')}\n"
    message += f"**선발 방법:** {detail.get('선발방법', '정보 없음')}\n"
    message += f"**제출 서류:** {detail.get('제출서류', '정보 없음')}\n"
    message += f"**전형 일정:** {detail.get('전형일정', '정보 없음')}\n"
    message += f"**기타 사항:** {detail.get('기타사항', '정보 없음')}\n"

    await _safe_bot_send_message(context.bot, chat_id, message, parse_mode=ParseMode.MARKDOWN)


async def cmd_관심추가(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    args = context.args
    if len(args) < 2:
        await _safe_bot_send_message(
            context.bot,
            chat_id,
            "관심 대학과 전형명을 입력해주세요. 예: `/add 서울대학교 수시 일반전형`",
        )
        return

    university_name = args[0]
    admission_plan_name = " ".join(args[1:])

    await _log_user_activity(
        user.id, username, chat_id, f"/add {university_name} {admission_plan_name}", action="command"
    )

    if _프로필.add_favorite(user.id, university_name, admission_plan_name):
        _프로필.save()
        await _safe_bot_send_message(
            context.bot, chat_id, f"'{university_name} - {admission_plan_name}'을(를) 관심 목록에 추가했습니다."
        )
    else:
        await _safe_bot_send_message(
            context.bot, chat_id, f"'{university_name} - {admission_plan_name}'은(는) 이미 관심 목록에 있습니다."
        )


# ── 프로필 위자드 ConversationHandler 핸들러 ─────────────────

async def profile_wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/profile 커맨드 또는 menu_profile 버튼 → 프로필 카드 + 위자드 시작."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if update.callback_query:
        await update.callback_query.answer()

    await _log_user_activity(user.id, user.username or "unknown", chat_id, "/profile", action="command")

    if not _프로필.get_user(user.id):
        _프로필.add_user(user.id, user.username, user.first_name)
        _프로필.save()

    text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
    await _safe_bot_send_message(
        context.bot, chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    return SELECT_ACTION


async def profile_action_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """SELECT_ACTION 상태 — 버튼 클릭 → 각 WAITING 상태로 라우팅."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "done":
        return await profile_done(update, context)

    if data == "upload_photo":
        await query.message.reply_text(
            "📸 나이스(NEIS) 성적표나 학교에서 발급받은 성적표 사진을 채팅창에 전송해 주세요.\n\n"
            "⚠️ [주의] 안정적인 AI 판독을 위해 반드시 '한 번에 한 장씩' 올려주세요!",
        )
        return WAITING_FOR_PHOTO

    if data == "set_major":
        await query.message.reply_text(
            "🎯 희망 학과를 채팅으로 입력해주세요.\n예시: 신소재공학과"
        )
        return WAITING_FOR_MAJOR

    if data == "set_gpa":
        await query.message.reply_text(
            "📊 내신 등급을 채팅으로 입력해주세요.\n예시: 2.5등급"
        )
        return WAITING_FOR_GPA

    if data == "set_mock":
        await query.message.reply_text(
            "📝 모의고사 등급을 채팅으로 입력해주세요.\n예시: 3등급 (국어 2 / 수학 3 / 영어 2)"
        )
        return WAITING_FOR_MOCK

    if data == "set_grade_level":
        await query.message.reply_text(
            "🏫 현재 학년을 입력해주세요.\n예시: 고2 / 고3"
        )
        return WAITING_FOR_GRADE_LEVEL

    if data == "set_hs_type":
        await query.message.reply_text(
            "🏛 고교 유형을 입력해주세요.\n예시: 일반고 / 자사고 / 특목고 / 과학고 / 외고"
        )
        return WAITING_FOR_HS_TYPE

    if data == "set_keywords":
        await query.message.reply_text(
            "🔬 주력 세특 키워드를 입력해주세요.\n"
            "예시: 머신러닝, 데이터 분석, 알고리즘 설계"
        )
        return WAITING_FOR_KEYWORDS

    if data == "set_csat":
        await query.message.reply_text(
            "📚 수능 선택 과목을 입력해주세요.\n예시: 미적분, 화학I / 확률과통계, 생명과학I"
        )
        return WAITING_FOR_CSAT

    if data == "bulk_input":
        await query.message.reply_text(
            "아래 양식을 복사해서 쓰시거나, 그냥 편하게 말하듯 채팅을 쳐주시면 AI가 찰떡같이 알아듣고 프로필을 채워드립니다!\n\n"
            "💡 [자연어 입력 예시]\n"
            "\"저 고2 일반고 다니고, 수능은 미적분이랑 과탐 선택했어요. 희망학과는 환경공학이고 세특은 수질오염 썼어요!\"\n\n"
            "📝 [양식 복사해서 입력하기]\n"
            "희망 학과: \n"
            "모의고사: \n"
            "세특 키워드: \n"
            "수능 과목: \n"
            "현재 학년: \n"
            "고교 유형: ",
        )
        return WAITING_FOR_BULK_INPUT

    return SELECT_ACTION


async def save_major(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WAITING_FOR_MAJOR — 희망 학과 저장 후 SELECT_ACTION 복귀."""
    user    = update.effective_user
    chat_id = update.effective_chat.id
    value   = update.message.text.strip()

    _프로필.update_user_profile(user.id, "target_major", value)
    _프로필.save()
    db_manager.update_user_profile(user.id, "target_major", value)
    await _log_user_activity(user.id, user.username or "unknown", chat_id,
                             f"프로필 수정: target_major={value}", action="profile_update")

    await update.message.reply_text(f"✅ 희망 학과 '{value}'이(가) 저장되었습니다!")
    text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
    await _safe_bot_send_message(
        context.bot, chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    return SELECT_ACTION


async def save_gpa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WAITING_FOR_GPA — 내신 등급 저장 후 SELECT_ACTION 복귀."""
    user    = update.effective_user
    chat_id = update.effective_chat.id
    value   = update.message.text.strip()

    _프로필.update_user_profile(user.id, "grade_raw", value)
    _프로필.save()
    db_manager.update_user_profile(user.id, "grade_raw", value)
    await _log_user_activity(user.id, user.username or "unknown", chat_id,
                             f"프로필 수정: grade_raw={value}", action="profile_update")

    await update.message.reply_text(f"✅ 내신 등급 '{value}'이(가) 저장되었습니다!")
    text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
    await _safe_bot_send_message(
        context.bot, chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    return SELECT_ACTION


async def save_mock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WAITING_FOR_MOCK — 모의고사 등급 저장 후 SELECT_ACTION 복귀."""
    user    = update.effective_user
    chat_id = update.effective_chat.id
    value   = update.message.text.strip()

    _프로필.update_user_profile(user.id, "mock_exam", value)
    _프로필.save()
    db_manager.update_user_profile(user.id, "mock_exam", value)
    await _log_user_activity(user.id, user.username or "unknown", chat_id,
                             f"프로필 수정: mock_exam={value}", action="profile_update")

    await update.message.reply_text(f"✅ 모의고사 등급 '{value}'이(가) 저장되었습니다!")
    text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
    await _safe_bot_send_message(
        context.bot, chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    return SELECT_ACTION


async def save_grade_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WAITING_FOR_GRADE_LEVEL — 현재 학년 저장."""
    user, chat_id = update.effective_user, update.effective_chat.id
    value = update.message.text.strip()
    db_manager.update_user_profile(user.id, "current_grade", value)
    _프로필.update_user_profile(user.id, "current_grade", value)
    _프로필.save()
    await update.message.reply_text(f"✅ 현재 학년 '{value}'이(가) 저장되었습니다!")
    text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
    await _safe_bot_send_message(context.bot, chat_id, text, reply_markup=keyboard,
                                 parse_mode=ParseMode.HTML)
    return SELECT_ACTION


async def save_hs_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WAITING_FOR_HS_TYPE — 고교 유형 저장."""
    user, chat_id = update.effective_user, update.effective_chat.id
    value = update.message.text.strip()
    db_manager.update_user_profile(user.id, "highschool_type", value)
    _프로필.update_user_profile(user.id, "highschool_type", value)
    _프로필.save()
    await update.message.reply_text(f"✅ 고교 유형 '{value}'이(가) 저장되었습니다!")
    text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
    await _safe_bot_send_message(context.bot, chat_id, text, reply_markup=keyboard,
                                 parse_mode=ParseMode.HTML)
    return SELECT_ACTION


async def save_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WAITING_FOR_KEYWORDS — 주력 세특 키워드 저장."""
    user, chat_id = update.effective_user, update.effective_chat.id
    value = update.message.text.strip()
    db_manager.update_user_profile(user.id, "target_keywords", value)
    _프로필.update_user_profile(user.id, "target_keywords", value)
    _프로필.save()
    await update.message.reply_text(f"✅ 세특 키워드 '{value}'이(가) 저장되었습니다!")
    text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
    await _safe_bot_send_message(context.bot, chat_id, text, reply_markup=keyboard,
                                 parse_mode=ParseMode.HTML)
    return SELECT_ACTION


async def save_csat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WAITING_FOR_CSAT — 수능 선택 과목 저장."""
    user, chat_id = update.effective_user, update.effective_chat.id
    value = update.message.text.strip()
    db_manager.update_user_profile(user.id, "csat_subjects", value)
    _프로필.update_user_profile(user.id, "csat_subjects", value)
    _프로필.save()
    await update.message.reply_text(f"✅ 수능 선택 과목 '{value}'이(가) 저장되었습니다!")
    text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
    await _safe_bot_send_message(context.bot, chat_id, text, reply_markup=keyboard,
                                 parse_mode=ParseMode.HTML)
    return SELECT_ACTION


_BULK_NLP_SYSTEM = (
    "You are an expert NLP parser for Korean college admissions. "
    "Extract profile fields from the user's text, which may be unstructured natural language or a filled-out template. "
    "Return ONLY a strictly valid JSON object with no markdown, no explanation, no code block. "
    "If a field is not mentioned, set its value to null. "
    'Schema: {"major": "희망학과 (e.g. 환경공학)", "mock_exam": "모의고사 성적 (e.g. 올3등급)", '
    '"target_keywords": "세특 키워드 (e.g. 수질오염)", "csat_subjects": "수능 과목 (e.g. 미적분, 과탐)", '
    '"current_grade": "현재 학년 (e.g. 고2)", "highschool_type": "고교 유형 (e.g. 일반고)"}'
)

_BULK_FIELD_MAP = {
    "major":           "target_major",
    "mock_exam":       "mock_exam",
    "target_keywords": "target_keywords",
    "csat_subjects":   "csat_subjects",
    "current_grade":   "current_grade",
    "highschool_type": "highschool_type",
}


async def save_bulk_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WAITING_FOR_BULK_INPUT — Gemini NLP로 자연어/양식 텍스트를 파싱해 프로필 일괄 저장."""
    import re as _re
    user    = update.effective_user
    chat_id = update.effective_chat.id
    raw     = update.message.text.strip()

    thinking_msg = await update.message.reply_text("🤖 AI가 내용을 분석 중입니다... 잠시만요!")

    extracted: dict = {}
    try:
        result_text, _engine = _tm.generate_text_sync(
            raw, _BULK_NLP_SYSTEM, force_engine="gemini"
        )
        if not result_text:
            raise ValueError("Gemini 응답이 비어있습니다.")

        # 마크다운 코드펜스 제거 후 JSON 파싱
        cleaned = _re.sub(r"```[a-z]*\n?", "", result_text).strip().strip("`").strip()
        m = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
        if not m:
            raise ValueError(f"JSON 블록을 찾을 수 없습니다: {cleaned[:120]}")
        extracted = json.loads(m.group(0))

    except Exception as e:
        logger.warning(f"[BulkInput] Gemini 파싱 실패: {type(e).__name__}: {e}")
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=thinking_msg.message_id,
                text="⚠️ AI가 내용을 파악하지 못했습니다. 항목별로 직접 입력해주시거나, 좀 더 명확하게 다시 입력해주세요!",
            )
        except Exception:
            pass
        text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
        await _safe_bot_send_message(context.bot, chat_id, text, reply_markup=keyboard,
                                     parse_mode=ParseMode.HTML)
        return SELECT_ACTION

    # 추출된 값 중 null이 아닌 것만 DB + 메모리 저장
    updated_fields: list[str] = []
    for json_key, db_field in _BULK_FIELD_MAP.items():
        value = extracted.get(json_key)
        if value and str(value).strip() and str(value).strip().lower() != "null":
            v = str(value).strip()
            db_manager.update_user_profile(user.id, db_field, v)
            _프로필.update_user_profile(user.id, db_field, v)
            updated_fields.append(db_field)
    _프로필.save()

    await _log_user_activity(
        user.id, user.username or "unknown", chat_id,
        f"자연어 일괄입력: {len(updated_fields)}개 필드 업데이트 {updated_fields}",
        action="bulk_input_nlp",
    )

    if updated_fields:
        notice = f"✨ AI가 문맥을 파악하여 프로필을 자동 업데이트했습니다! ({len(updated_fields)}개 항목)"
    else:
        notice = "⚠️ 입력에서 프로필 항목을 찾지 못했습니다. 더 구체적으로 입력해주세요."

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=thinking_msg.message_id, text=notice,
        )
    except Exception:
        await update.message.reply_text(notice)

    text, keyboard = _build_profile_card(_프로필.get_user(user.id), user.first_name)
    await _safe_bot_send_message(context.bot, chat_id, text, reply_markup=keyboard,
                                 parse_mode=ParseMode.HTML)
    return SELECT_ACTION


async def profile_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """입력 완료 버튼 → 필수 필드 검증 후 대화 종료 + PDF 생성 유도."""
    query = update.callback_query
    if query:
        await query.answer()
    chat_id = (query.message if query else update.message).chat_id
    user = query.from_user if query else update.effective_user

    # ── 필수 필드 검증 ───────────────────────────────────────────
    profile = _프로필.get_user(user.id) or {}
    major = profile.get("target_major") or profile.get("희망학과") or ""
    gpa   = profile.get("grade_raw")    or profile.get("내신")     or ""

    if not major and not gpa:
        alert = (
            "⚠️ 아직 필수 정보(내신 등급 또는 희망 학과)가 입력되지 않았습니다.\n\n"
            f"현재 입력된 정보:\n"
            f"- 희망 학과: {major or '미입력'}\n"
            f"- 내신 등급: {gpa or '미입력'}\n\n"
            "위 버튼을 눌러 정보를 보완해 주세요."
        )
        text, keyboard = _build_profile_card(profile, user.first_name)
        await context.bot.send_message(chat_id, alert)
        await _safe_bot_send_message(
            context.bot, chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
        return SELECT_ACTION

    # ── 검증 통과 → CTA 전송 후 종료 ───────────────────────────
    # 희망 학과 미입력(성적만 있는) 사용자에게 역추천 안내 토스트
    if not major and gpa:
        await context.bot.send_message(
            chat_id,
            "💡 희망 학과가 미입력되어, AI가 성적(GPA) 기반 최적의 유망 학과를 분석하여 역으로 추천해 드립니다!",
        )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ AI 진단 처방전 생성 시작", callback_data="menu_analyze"),
    ]])
    await _safe_bot_send_message(
        context.bot,
        chat_id,
        "🎉 프로필 설정이 완료되었습니다\\!\n"
        "입력해주신 데이터를 바탕으로 AI 맞춤형 대입 진단 처방전을 즉시 생성해볼까요?",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END


async def collect_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WAITING_FOR_PHOTO / WAITING_FOR_MORE_PHOTOS — 한 장씩 핑퐁 UX 장바구니 적재."""
    user           = update.effective_user
    media_group_id = update.message.media_group_id

    # ── 1. MediaGroup 감지: 두 번째 이후 사진은 무시 ────────────
    if media_group_id:
        processed_groups: set = context.user_data.setdefault("_processed_groups", set())
        if media_group_id in processed_groups:
            return WAITING_FOR_MORE_PHOTOS  # 중복 전송 무시
        processed_groups.add(media_group_id)

    # ── 2. 사진 다운로드 ─────────────────────────────────────────
    photo    = update.message.photo[-1]
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    tmp_path = 프로젝트_루트 / "data" / f"temp_{user.id}_{ts}.jpg"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(str(tmp_path))
    except Exception as e:
        logger.error(f"[OCR] 사진 다운로드 실패: {e}", exc_info=True)
        await update.message.reply_text("❌ 사진 수신에 실패했습니다. 다시 시도해주세요.")
        return WAITING_FOR_PHOTO

    # ── 3. photo_cart 에 적재 ─────────────────────────────────────
    cart: list = context.user_data.setdefault("photo_cart", [])
    cart.append(str(tmp_path))
    logger.info(
        f"[OCR] 장바구니 {len(cart)}장 적재: user_id={user.id}, "
        f"media_group={media_group_id}"
    )

    # ── 4. 즉시 핑퐁 피드백 (버튼 1개만) ────────────────────────
    _keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔍 모든 사진 제출 완료 (AI 분석 시작)",
            callback_data="start_ocr",
        )
    ]])

    if media_group_id:
        reply_text = (
            "⚠️ 여러 장이 동시에 전송되었습니다. 일부 사진이 누락될 수 있으니, "
            "다음부터는 한 장씩 올려주시길 권장합니다.\n\n"
            f"📸 현재 {len(cart)}장이 장바구니에 담겼습니다.\n"
            "모두 올리셨다면 아래 버튼을 눌러주세요."
        )
    else:
        reply_text = (
            f"✅ {len(cart)}번째 사진이 장바구니에 담겼습니다!\n\n"
            "추가할 성적표가 있다면 다음 사진을 한 장 올려주시고, "
            "모두 올리셨다면 아래 버튼을 눌러주세요."
        )

    await update.message.reply_text(reply_text, reply_markup=_keyboard)
    return WAITING_FOR_MORE_PHOTOS


async def process_ocr_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """start_ocr 콜백 — 수집된 사진들을 일괄 OCR 처리 후 확인 요청."""
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user    = query.from_user

    # photo_cart (신규) 우선, 없으면 photo_paths (구형) 폴백
    paths: list[str] = context.user_data.get("photo_cart") or context.user_data.get("photo_paths", [])
    if not paths:
        await query.message.reply_text(
            "⚠️ 분석할 사진이 없습니다. 사진을 먼저 업로드해주세요."
        )
        return WAITING_FOR_PHOTO

    await query.message.reply_text(
        f"⏳ AI가 수신된 사진 {len(paths)}장을 종합하여 성적을 분석 중입니다."
        " 잠시만 기다려주세요..."
    )

    # 각 사진 OCR — vision_parser는 내부적으로 run_in_executor 사용하므로 비동기 안전
    ocr_parts: list[str] = []
    for i, path_str in enumerate(paths):
        result = await vision_parser.extract_grades_from_image(Path(path_str))
        label  = f"[사진 {i + 1}]\n" if len(paths) > 1 else ""
        ocr_parts.append(f"{label}{result}")
        logger.info(f"[OCR] 사진 {i+1}/{len(paths)} 완료: user_id={user.id}")

    # 임시 파일 즉시 삭제 및 장바구니 초기화
    for path_str in paths:
        try:
            Path(path_str).unlink(missing_ok=True)
        except Exception:
            pass
    context.user_data["photo_cart"]  = []
    context.user_data["photo_paths"] = []
    context.user_data.pop("_cart_msg_id", None)

    combined = "\n\n".join(ocr_parts)
    context.user_data["temp_ocr_result"] = combined

    await _log_user_activity(
        user.id, user.username or "unknown", chat_id,
        f"배치 OCR 완료: {len(paths)}장 → {len(combined)}자", action="ocr_batch",
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭕ 맞습니다", callback_data="ocr_yes")],
        [InlineKeyboardButton("❌ 다시 입력", callback_data="ocr_no")],
    ])
    await query.message.reply_text(
        f"📊 [AI 판독 결과]\n\n{combined}\n\n이대로 프로필에 저장할까요?",
        reply_markup=keyboard,
    )
    return CONFIRM_OCR_RESULT


async def confirm_ocr_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ocr_yes 콜백 — OCR 결과를 grade_raw 로 저장 후 위자드 종료."""
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user    = query.from_user

    ocr_result = context.user_data.pop("temp_ocr_result", "")
    _cleanup_photo_temps(context)

    if ocr_result:
        _프로필.update_user_profile(user.id, "grade_raw", ocr_result)
        _프로필.save()
        db_manager.update_user_profile(user.id, "grade_raw", ocr_result)
        await _log_user_activity(
            user.id, user.username or "unknown", chat_id,
            f"OCR 확정 저장: {ocr_result[:60]}", action="ocr_confirm_yes",
        )
        logger.info(f"[OCR] 확정 저장 완료: user_id={user.id}")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ AI 진단 처방전 발급", callback_data="menu_analyze")
    ]])
    await query.message.reply_text(
        "🎉 성적 정보가 성공적으로 저장되었습니다!\n"
        "이제 AI 진단 처방전을 발급받으실 수 있습니다.",
        reply_markup=keyboard,
    )
    return ConversationHandler.END


async def confirm_ocr_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ocr_no 콜백 — OCR 결과 폐기 후 사진 재업로드 상태로 복귀."""
    query = update.callback_query
    await query.answer()

    context.user_data.pop("temp_ocr_result", None)
    _cleanup_photo_temps(context)

    await _log_user_activity(
        query.from_user.id, query.from_user.username or "unknown",
        query.message.chat_id, "OCR 결과 거부", action="ocr_confirm_no",
    )

    await query.message.reply_text(
        "🔄 정보가 초기화되었습니다.\n"
        "사진을 다시 올려주시거나, 평균 등급을 텍스트로 직접 입력해주세요."
    )
    return WAITING_FOR_PHOTO


async def cancel_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel — 프로필 위자드 강제 종료 (임시 파일 정리 포함)."""
    _cleanup_photo_temps(context)
    context.user_data.pop("temp_ocr_result", None)
    await update.message.reply_text(
        "프로필 설정이 취소되었습니다. /profile 로 다시 시작할 수 있습니다."
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────
# /debug 커맨드 — 통합 디버그 로그 생성 & Telegram 발송
# ─────────────────────────────────────────────────────────────

def _build_unified_debug() -> Path:
    """통합 디버그 파일을 생성하고 경로를 반환합니다 (동기, run_in_executor용)."""

    def _tail(path: Path, n: int) -> str:
        if not path.exists():
            return f"(파일 없음: {path})\n"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:]) + "\n"
        except Exception as e:
            return f"(읽기 실패: {e})\n"

    logs_dir  = 프로젝트_루트 / "data" / "logs"
    out_dir   = 프로젝트_루트 / "data" / "fix_error"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path  = out_dir / "unified_debug.txt"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sections = [
        f"UnivAgent 통합 디버그 리포트 — {ts}\n{'='*60}\n\n",

        "=== TELEGRAM NOHUP LOG (최근 100줄) ===\n",
        _tail(logs_dir / "telegram_nohup.log", 100),

        "\n=== SYSTEM EVENTS (최근 50줄) ===\n",
        _tail(logs_dir / "system_events.jsonl", 50),

        "\n=== USER ACTIVITY LOG (최근 50줄) ===\n",
        _tail(logs_dir / "user_activity.log", 50),

        "\n=== TELEGRAM ERRORS (최근 30줄) ===\n",
        _tail(out_dir / "telegram_errors.log", 30),

        "\n=== GEMINI API ERRORS (최근 20줄) ===\n",
        _tail(out_dir / "gemini_api_errors.log", 20),
    ]

    out_path.write_text("".join(sections), encoding="utf-8")
    return out_path


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/debug — 통합 디버그 로그를 생성 후 Telegram 파일로 발송."""
    user    = update.effective_user
    chat_id = update.effective_chat.id

    await update.message.reply_text("🔧 통합 디버그 파일 생성 중...")

    loop = asyncio.get_event_loop()
    try:
        out_path: Path = await loop.run_in_executor(None, _build_unified_debug)
        with open(out_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename="unified_debug.txt",
                caption=(
                    f"✅ UnivAgent 통합 디버그 리포트\n"
                    f"생성: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"이 파일을 Gemini/Claude에게 전달해 오류 분석을 요청하세요."
                ),
            )
        logger.info(f"[Debug] 통합 디버그 파일 발송: {out_path}")
    except Exception as e:
        logger.error(f"[Debug] 통합 디버그 생성 실패: {e}", exc_info=True)
        await _safe_bot_send_message(
            context.bot, chat_id, f"❌ 디버그 파일 생성 실패: {e}"
        )


async def cmd_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/request 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    prompt = " ".join(context.args).strip()
    if not prompt:
        await _safe_bot_send_message(
            context.bot, chat_id, "질문 내용을 입력해주세요. 예: `/request 서울대학교 신소재공학과 경쟁률은?`"
        )
        return

    await _log_user_activity(user.id, username, chat_id, f"/request {prompt}", action="command")

    await _handle_gemini_response(
        update, context, prompt, chat_id, user.id, username, model_name="gemini-2.5-flash-lite"
    )


async def _analyze_report_생성_및_발송(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    username: str,
) -> None:
    """
    학생 프로필 기반 입시 진단 처방전 PDF를 생성하고 즉시 발송합니다.

    흐름 (순서 엄수):
      1. 프로필 조회 → 미설정 시 안내 후 종료
      2. LLM 콘텐츠 생성 (run_in_executor, Section D DRACONIAN 한국어 강제)
      3. PDF 렌더링 (generate_pdf_async 내부에서 run_in_executor)
      4. send_document ← 반드시 이 줄이 실행돼야 함
      5. Ollama 사용 시 pending_verifications 큐잉 (PDF 발송 완료 후)
    에러 발생 시: logger_factory 로깅 + 사용자에게 오류 메시지 전송 (무음 실패 금지)
    """
    loop = asyncio.get_event_loop()

    try:
        # ── 1. 프로필 조회 ────────────────────────────────────────
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        profile = _프로필.get_user(user_id) or {}
        major  = profile.get("target_major") or ""
        grade  = profile.get("grade_raw")    or ""
        mock   = profile.get("mock_exam")    or "미입력"
        school = profile.get("school_type")  or "일반고"

        # 둘 다 미입력 → 프로필 설정 유도
        if not major and not grade:
            await _safe_bot_send_message(
                context.bot, chat_id,
                "📋 진단 처방전을 생성하려면 먼저 프로필을 설정해주세요.\n"
                "`/profile` 명령어로 희망학과·내신·모의고사 성적을 입력하면\n"
                "맞춤 처방전을 즉시 생성해 드립니다.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # ── 역추천 모드 vs 일반 모드 분기 ────────────────────────
        is_rec_mode = not major  # 희망학과 미입력 시 역추천

        if is_rec_mode:
            await _safe_bot_send_message(
                context.bot, chat_id,
                "💡 희망 학과가 미입력되어, AI가 성적(GPA) 기반 최적의 유망 학과를 분석하여 역으로 추천해 드립니다!",
            )
            await _safe_bot_send_message(
                context.bot, chat_id,
                f"⏳ *입시 역추천 분석 중...*\n"
                f"내신: {grade or '미입력'} | 모의고사: {mock}\n"
                f"AI가 성적에 맞는 최적 학과를 추천합니다. 잠시만 기다려주세요.",
                parse_mode=ParseMode.MARKDOWN,
            )
            report_label = "AI 역추천"
            active_system_prompt = _RECOMMENDATION_SYSTEM_PROMPT
            prompt = _RECOMMENDATION_PROMPT_TEMPLATE.format(
                grade=grade or "미입력", mock=mock, school=school
            )
        else:
            await _safe_bot_send_message(
                context.bot, chat_id,
                f"⏳ *입시 처방전 분석 중...*\n"
                f"희망학과: {major} | 내신: {grade or '미입력'} | 모의고사: {mock}\n"
                f"AI가 맞춤 리포트를 생성합니다. 잠시만 기다려주세요.",
                parse_mode=ParseMode.MARKDOWN,
            )
            report_label = major
            active_system_prompt = _REPORT_SYSTEM_PROMPT

            # DB에 기존 학과 지식이 있으면 프롬프트에 주입 (토큰 절감 + 일관성)
            major_db_data = await loop.run_in_executor(
                None, lambda: db_manager.get_major_info(major)
            )
            db_prefix = ""
            if major_db_data:
                db_prefix = (
                    f"[검증된 학과 데이터 — 반드시 아래 정보를 리포트에 활용하세요]\n"
                    f"주요 교육과정: {major_db_data.get('curriculum', '')}\n"
                    f"졸업 후 직무: {major_db_data.get('career_paths', '')}\n"
                    f"주요 취업처: {major_db_data.get('employment_companies', '')}\n\n"
                )
                logger.info(f"[학과지식DB] 기존 데이터 프롬프트 주입: {major}")
            prompt = db_prefix + _REPORT_PROMPT_TEMPLATE.format(
                major=major, grade=grade or "미입력", mock=mock, school=school
            )

        # ── 2. LLM 콘텐츠 생성 (DRACONIAN 한국어 규칙 적용) ──────
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        raw_content, engine = await loop.run_in_executor(
            None,
            lambda: _tm.generate_text_sync(prompt, active_system_prompt),
        )

        if not raw_content:
            await _safe_bot_send_message(
                context.bot, chat_id,
                "❌ 리포트 생성 중 치명적 오류가 발생했습니다: "
                "AI 엔진이 응답하지 않습니다. 잠시 후 다시 시도해주세요.",
            )
            return

        # ── 2-B. 학과 지식 백그라운드 저장 ──────────────────────
        save_key = major if not is_rec_mode else f"역추천_{(grade or '')[:10]}"
        asyncio.create_task(_백그라운드_학과_저장(save_key, raw_content))

        # ── 3. PDF 렌더링 ─────────────────────────────────────────
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_path = 프로젝트_루트 / "data" / "reports" / f"report_{user_id}_{ts}.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
        await pdf_generator.generate_pdf_async(
            title=f"UnivAgent 입시 처방전 — {report_label}",
            content_lines=raw_content.strip().splitlines(),
            output_path=pdf_path,
            doc_type="DIAGNOSIS_REPORT",
            source=f"telegram_analyze:{user_id}",
            metadata={
                "user_id": user_id,
                "major": report_label,
                "grade": grade,
                "mock": mock,
                "engine": engine,
                "rec_mode": is_rec_mode,
            },
        )

        # ── 4. PDF 즉시 발송 (이 라인이 반드시 실행돼야 함) ───────
        with open(pdf_path, "rb") as _pdf_file:
            await context.bot.send_document(
                chat_id=chat_id,
                document=_pdf_file,
                filename=f"UnivAgent_처방전_{report_label}_{ts}.pdf",
                caption=(
                    f"✅ *{report_label}* 맞춤 입시 처방전 완성!\n"
                    f"_(분석 엔진: {engine})_"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        _요청.increment()
        logger.info(f"[처방전] PDF 발송 완료: user_id={user_id}, engine={engine}, path={pdf_path.name}")

        # ── 5. Ollama 폴백 → UX 캡션 + 큐잉 (PDF 발송 완료 후) ───
        if "Ollama" in engine:
            await _safe_bot_send_message(context.bot, chat_id, _OLLAMA_UX_CAPTION)
            try:
                await loop.run_in_executor(
                    None,
                    lambda: db_manager.pending_verification_추가(
                        user_id,
                        f"입시 처방전 요청 (희망학과={report_label}, 내신={grade}, 모의={mock})",
                        raw_content,
                    ),
                )
            except Exception as _qe:
                logger.warning(f"[검증대기열] 큐잉 실패 (무시): {_qe}")

        await _log_user_activity(
            user_id, username, chat_id,
            f"처방전 PDF 생성: {major}", action="analyze_report",
        )

    except Exception as e:
        # 에러 로깅 (무음 실패 절대 금지)
        try:
            await _lf.async_log_error("ANALYZE_REPORT_FAIL", "telegram_agent", e,
                                      extra={"user_id": user_id, "username": username})
        except Exception:
            pass
        logger.error(f"[처방전] 생성 실패: {e}", exc_info=True)
        await _safe_bot_send_message(
            context.bot, chat_id,
            f"❌ 리포트 생성 중 치명적 오류가 발생했습니다: {str(e)[:200]}",
        )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/analyze 커맨드 핸들러 — 즉시 입시 처방전 PDF 생성·발송."""
    user     = update.effective_user
    chat_id  = update.effective_chat.id
    username = user.username or "unknown"

    await _log_user_activity(user.id, username, chat_id, "/analyze", action="command")
    await _analyze_report_생성_및_발송(context, chat_id, user.id, username)


async def cmd_pdf_업로드(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PDF 파일 업로드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"
    document = update.message.document

    if not document or not document.file_name.lower().endswith(".pdf"):
        await _safe_bot_send_message(
            context.bot, chat_id, "PDF 파일만 업로드해주세요."
        )
        return

    await _log_user_activity(user.id, username, chat_id, f"PDF 업로드: {document.file_name}", action="pdf_upload")

    try:
        file_path = await document.get_file().download_to_drive(
            custom_path=프로젝트_루트 / "data" / "uploads" / f"{user.id}_{document.file_name}"
        )
        logger.info(f"PDF 파일 다운로드 완료: {file_path}")

        # 학생부 분석 로직 (Gemini API 사용)
        # TODO: PDF 파싱 및 내용 추출 로직 구현
        # TODO: 추출된 내용을 바탕으로 Gemini API 호출하여 분석 결과 생성
        # TODO: 분석 결과 메시지 전송

        # 임시 응답
        await _safe_bot_send_message(
            context.bot, chat_id, "학생부 분석 요청이 접수되었습니다. 분석 후 결과를 보내드리겠습니다. (구현 중)"
        )

        # 실제 분석 로직 구현 시 아래 코드 활용
        # from pypdf import PdfReader
        # reader = PdfReader(file_path)
        # text = ""
        # for page in reader.pages:
        #     text += page.extract_text() or ""
        #
        # if not text:
        #     await _safe_bot_send_message(context.bot, chat_id, "PDF 파일에서 내용을 추출하지 못했습니다.")
        #     return
        #
        # prompt = f"다음은 학생부 내용입니다. 이를 바탕으로 대학 입시 관점에서 분석하고 조언해주세요:\n\n{text}"
        # await _handle_gemini_response(update, context, prompt, chat_id, user.id, username)

    except Exception as e:
        logger.error(f"PDF 파일 처리 중 오류 발생: {e}", exc_info=True)
        await _safe_bot_send_message(
            context.bot, chat_id, "학생부 처리 중 오류가 발생했습니다. 다시 시도해주세요."
        )


async def cmd_setreport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setreport 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    args = context.args
    if len(args) < 2:
        await _safe_bot_send_message(
            context.bot,
            chat_id,
            "맞춤 입시 리포트 설정을 위해 대학명과 전형명을 입력해주세요. 예: `/setreport 서울대학교 수시 일반전형`",
        )
        return

    university_name = args[0]
    admission_plan_name = " ".join(args[1:])

    await _log_user_activity(
        user.id, username, chat_id, f"/setreport {university_name} {admission_plan_name}", action="command"
    )

    if _프로필.set_report_preference(user.id, university_name, admission_plan_name):
        _프로필.save()
        await _safe_bot_send_message(
            context.bot, chat_id, f"'{university_name} - {admission_plan_name}'에 대한 맞춤 입시 리포트 설정을 저장했습니다."
        )
    else:
        await _safe_bot_send_message(
            context.bot, chat_id, "맞춤 입시 리포트 설정 저장에 실패했습니다."
        )


async def cmd_reportnow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reportnow 커맨드 핸들러."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"

    await _log_user_activity(user.id, username, chat_id, "/reportnow", action="command")

    report_prefs = _프로필.get_user(user.id).get("report_preferences", [])
    if not report_prefs:
        await _safe_bot_send_message(
            context.bot, chat_id, "맞춤 입시 리포트 설정이 없습니다. `/setreport` 명령어로 먼저 설정해주세요."
        )
        return

    await _safe_bot_send_message(context.bot, chat_id, "맞춤 입시 리포트를 생성 중입니다...")

    # TODO: 실제 리포트 생성 로직 구현
    # Gemini API를 사용하여 설정된 대학/전형 정보 기반으로 리포트 생성
    # 예:
    # report_text = "## 맞춤 입시 리포트\n\n"
    # for pref in report_prefs:
    #     univ = pref['university']
    #     plan = pref['plan']
    #     detail = db_manager.get_admission_plan_detail(univ, plan)
    #     if detail:
    #         report_text += f"### {univ} - {plan}\n"
    #         report_text += f"- 모집 단위: {detail.get('모집단위', '정보 없음')}\n"
    #         # ... 기타 상세 정보 추가 ...
    #     else:
    #         report_text += f"### {univ} - {plan}\n정보를 찾을 수 없습니다.\n"
    #
    # prompt = f"다음은 사용자가 설정한 대학/전형 정보입니다. 이를 바탕으로 입시 전략에 도움이 되는 맞춤 리포트를 생성해주세요:\n\n{report_text}"
    # await _handle_gemini_response(update, context, prompt, chat_id, user.id, username, model_name="gemini-2.5-pro") # 고성능 모델 사용 고려

    await _safe_bot_send_message(
        context.bot, chat_id, "맞춤 입시 리포트 생성 완료! (구현 중)"
    )


_온보딩_키워드 = frozenset({
    "메뉴", "시작", "start", "help", "안내", "도움", "도움말",
    "처음", "처음으로", "홈", "home", "menu",
})


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """텍스트 메시지 핸들러 (커맨드 외)."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or "unknown"
    text = update.message.text

    await _log_user_activity(user.id, username, chat_id, text, action="text_message")

    # 온보딩 키워드 → LLM 미전달, 온보딩 메시지로 유도
    if text.strip().lower() in _온보딩_키워드:
        await _send_온보딩_메시지(context.bot, chat_id, user.first_name)
        return

    # 일반 질문 — 3-Tier 라우팅 + Ollama 폴백 시 사후 검증 큐잉
    await _질문_처리_및_큐잉(update, context, text, chat_id, user.id, username)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """인라인 버튼 콜백 핸들러."""
    query = update.callback_query
    await query.answer()  # 콜백 쿼리에 대한 응답 (로딩 표시 해제)

    user = query.from_user
    chat_id = query.message.chat_id
    username = user.username or "unknown"
    data = query.data

    await _log_user_activity(user.id, username, chat_id, f"Callback: {data}", action="callback")

    if data.startswith("menu_"):
        menu_action = data.split("_")[1]
        if menu_action == "search":
            await _safe_bot_send_message(
                context.bot, chat_id, "검색하고 싶은 학과명을 입력해주세요. 예: `/search 신소재공학과`"
            )
        elif menu_action == "detail":
            await _safe_bot_send_message(
                context.bot, chat_id, "상세 정보를 알고 싶은 대학과 전형명을 입력해주세요. 예: `/detail 서울대학교 수시 일반전형`"
            )
        elif menu_action == "analyze":
            await _safe_bot_send_message(
                context.bot, chat_id,
                "⏳ 리포트를 생성 중입니다\\.\\.\\. 잠시만 기다려주세요\\!",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await _analyze_report_생성_및_발송(context, chat_id, user.id, username)
        elif menu_action == "help":
            await cmd_도움말(query, context) # query 객체를 update로 전달
        elif menu_action == "query":
            await _safe_bot_send_message(
                context.bot, chat_id, "무엇이든 물어보세요. AI가 답변해 드립니다."
            )
        else:
            await _safe_bot_send_message(context.bot, chat_id, "알 수 없는 메뉴입니다.")
    elif data in {"set_major", "set_gpa", "set_mock", "upload_photo", "done",
                  "start_ocr", "ocr_yes", "ocr_no"} or data.startswith("pf_"):
        # ConversationHandler 세션 만료 — 위자드 재시작 안내
        await _safe_bot_send_message(
            context.bot, chat_id,
            "⏰ 세션이 만료되었습니다. /profile 을 다시 입력하여 프로필 설정을 재시작해주세요.",
        )
    else:
        await _safe_bot_send_message(context.bot, chat_id, "알 수 없는 콜백 데이터입니다.")


async def _bot_post_init(application: Application) -> None:
    """봇 시작 후 실행되는 함수."""
    logger.info("[봇] 봇 초기화 완료. 백그라운드 작업 시작...")

    # 백그라운드 작업 스케줄링
    # _spawn_background(_run_crawler_periodically()) # 크롤러는 별도 스크립트에서 실행
    # _spawn_background(_run_admission_stats_updater()) # 크롤러는 별도 스크립트에서 실행

    # AI 기능 사용 가능 여부 확인
    if not GEMINI_API_KEY:
        logger.warning("[봇] GEMINI_API_KEY 미설정으로 AI 기능이 비활성화됩니다.")
        global _AI
        _AI = None

    # DB 스키마 초기화 — 모든 DDL은 db_manager 단독 관리
    try:
        db_manager.init_db()
        logger.info(f"[DB] 초기화 완료 ({db_manager.get_covered_universities_count()}개 대학)")
    except Exception as e:
        logger.error(f"[DB] 초기화 중 오류 발생: {e}")

    try:
        _프로필.load()
        logger.info(f"[프로필] 사용자 프로필 로드 완료 ({_프로필.total_users}명)")
    except Exception as e:
        logger.error(f"[프로필] 사용자 프로필 로드 중 오류 발생: {e}")

    try:
        _요청.load()
        logger.info(f"[요청] 요청 카운터 로드 완료 (총 {_요청.total_requests}건)")
    except Exception as e:
        logger.error(f"[요청] 요청 카운터 로드 중 오류 발생: {e}")


async def _bot_post_shutdown(application: Application) -> None:
    """봇 종료 시 실행되는 함수."""
    logger.info("[봇] 봇 종료 중... 백그라운드 작업 정리...")
    await _cancel_background_tasks()
    logger.info("[봇] 백그라운드 작업 정리 완료.")
    logger.info("[봇] 봇 종료.")


def _텔레그램_에러_기록(
    context_str: str,
    error: Exception,
    chat_id: int | None = None,
    update_str: str | None = None,
    extra: dict | None = None,
    tb_str: str | None = None,
):
    """텔레그램 관련 에러를 파일로 영구 기록합니다 (mode='a' — 재시작 후도 보존)."""
    extra = extra or {}
    tb = tb_str or traceback.format_exc()

    err_msg = f"[{context_str}] 에러 발생: {type(error).__name__} - {error}"
    if chat_id:
        err_msg += f" (ChatID: {chat_id})"
    if update_str:
        err_msg += f"\n  Update: {update_str}"

    logger.error(err_msg, exc_info=True)

    log_entry = {
        "timestamp":    datetime.now().isoformat(),
        "context":      context_str,
        "error_type":   type(error).__name__,
        "error_message": str(error),
        "traceback":    tb if tb and tb.strip() != "NoneType: None" else None,
        "chat_id":      chat_id,
        "update_str":   update_str,
        "extra":        extra,
    }

    error_log_path = _텔레그램_에러_로그
    error_log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(error_log_path, "a", encoding="utf-8") as f:
            json.dump(log_entry, f, ensure_ascii=False)
            f.write("\n")
    except Exception as e:
        logger.error(f"텔레그램 에러 로그 파일 저장 실패: {e}")


async def _global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    모든 미처리 예외를 잡아서 로깅하고, 필요한 경우 사용자에게 알립니다.
    특히 텔레그램 API 관련 에러 (Conflict, BadRequest, RetryAfter) 를 처리합니다.
    """
    error = context.error
    chat_id = None
    update_str = None

    if isinstance(error, (Conflict, BadRequest, RetryAfter)):
        # 텔레그램 API 관련 에러는 이미 _safe_bot_send_message 에서 처리될 수 있으나,
        # 여기서도 기록하여 추적합니다.
        pass
    else:
        # 일반적인 예외 처리
        if isinstance(update, Update):
            chat_id = update.effective_chat.id if update.effective_chat else None
            update_str = str(update)
        elif hasattr(context, "update") and isinstance(context.update, Update):
            chat_id = context.update.effective_chat.id if context.update.effective_chat else None
            update_str = str(context.update)

        if chat_id:
            await _safe_bot_send_message(
                context.bot,
                chat_id,
                "죄송합니다. 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
            )

    ctx_name = "global_error_handler"
    err_str = str(error)

    # 봇 인스턴스 중복 (getUpdates Conflict)
    if isinstance(error, Conflict) or "Conflict" in err_str:
        ctx_name = "global_error_handler | Telegram Conflict (중복 봇 인스턴스)"
        logger.error(
            "[봇전역오류] Telegram Conflict — 동일 봇이 다른 프로세스에서도 실행 중입니다. "
            "터미널/Streamlit 중복 기동을 종료하고 `./restart_agent.sh` 로 하나만 띄우세요."
        )
    elif isinstance(error, RetryAfter):
        ctx_name = "global_error_handler | Telegram Rate Limit"
        logger.warning(
            f"[봇전역오류] Telegram Rate Limit — {getattr(error, 'retry_after', '?')}초 후 재시도 필요"
        )
    elif isinstance(error, BadRequest) and (
        "parse entities" in err_str.lower() or "can't parse" in err_str.lower()
    ):
        ctx_name = "global_error_handler | Markdown parse entities"
        logger.warning(
            "[봇전역오류] Telegram Markdown 파싱 실패 — 메시지 특수문자 이스케이프 필요 "
            "(리포트 전송은 _safe_bot_send_message 가 plain 폴백 처리)"
        )
    else:
        ctx_name = f"global_error_handler | {type(error).__name__}"
        logger.error(
            f"[봇전역오류] 잡히지 않은 예외 (chat_id={chat_id}): "
            f"{type(error).__name__}: {error}",
            exc_info=True,
        )

    # traceback.format_exception 을 사용해 실제 스택 트레이스 캡처
    import traceback as _tb_mod
    tb_lines = _tb_mod.format_exception(type(error), error, error.__traceback__)
    tb_captured = "".join(tb_lines)

    # 파일에 직접 영구 기록
    try:
        _텔레그램_에러_로그.parent.mkdir(parents=True, exist_ok=True)
        with open(_텔레그램_에러_로그, "a", encoding="utf-8") as _f:
            _f.write(
                f"\n===== {datetime.now().isoformat()} [{ctx_name}] =====\n"
                f"{tb_captured}\n"
            )
    except Exception:
        pass

    _텔레그램_에러_기록(
        context_str=ctx_name,
        error=error,
        chat_id=chat_id,
        update_str=update_str,
        extra={"handler": "PTB_global"},
        tb_str=tb_captured,
    )


# ─────────────────────────────────────────────────────────────
# 봇 실행
# ─────────────────────────────────────────────────────────────

def main():
    로깅_설정()
    총_문서수, 최신ts = db_manager.입시_총수()
    # 학습 데이터셋 누적 현황
    데이터셋_건수 = 0
    if _LOCAL_AI_DATASET.exists():
        try:
            데이터셋_건수 = sum(1 for _ in open(_LOCAL_AI_DATASET, encoding="utf-8"))
        except Exception:
            pass

    logger.info("━" * 60)
    logger.info("  대학 입시 정보 텔레그램 봇 시작 (SQLite + 하이브리드 비평 모드)")
    logger.info(f"  수록 대학: {db_manager.get_covered_universities_count()}개  |  입시 문서: {총_문서수}건  |  갱신: {(최신ts or '미상')[:16]}")
    logger.info(f"  생성 엔진: gemini-2.5-flash-lite  |  비평 엔진: {CRITIC_ENGINE.upper()}"
                + (f" ({CRITIC_PRO_MODEL})" if CRITIC_ENGINE == "gemini" else f" ({OLLAMA_URL} / {OLLAMA_MODEL})"))
    logger.info(f"  AI 응답: {'활성화 (Gemini)' if _AI and GEMINI_API_KEY else '비활성화'}")
    logger.info(f"  사용자 수: {_프로필.총_사용자_수}명  |  누적 요청: {_요청.총_요청_수}건")
    logger.info(f"  DB 경로: {db_manager.DB_경로}")
    logger.info(f"  학습 데이터셋: {_LOCAL_AI_DATASET.name}  ({데이터셋_건수}건 누적)")
    logger.info("━" * 60)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_bot_post_init)
        .post_shutdown(_bot_post_shutdown)
        .build()
    )

    # 커맨드 핸들러 등록
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("help",      cmd_도움말))
    app.add_handler(CommandHandler("list",      cmd_대학목록))
    app.add_handler(CommandHandler("search",    cmd_검색))
    app.add_handler(CommandHandler("detail",    cmd_전형))
    app.add_handler(CommandHandler("add",       cmd_관심추가))
    app.add_handler(CommandHandler("request",   cmd_request))
    app.add_handler(CommandHandler("analyze",   cmd_analyze))
    app.add_handler(CommandHandler("setreport", cmd_setreport))
    app.add_handler(CommandHandler("reportnow", cmd_reportnow))
    app.add_handler(CommandHandler("debug",     cmd_debug))

    # 프로필 위자드 — 반드시 일반 CallbackQueryHandler 보다 먼저 등록
    _profile_conv = ConversationHandler(
        entry_points=[
            CommandHandler("profile", profile_wizard_start),
            CallbackQueryHandler(profile_wizard_start, pattern="^menu_profile$"),
        ],
        states={
            SELECT_ACTION: [
                CallbackQueryHandler(
                    profile_action_chosen,
                    pattern="^(set_major|set_gpa|set_mock|upload_photo|done|set_grade_level|set_hs_type|set_keywords|set_csat|bulk_input)$",
                ),
            ],
            WAITING_FOR_MAJOR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_major),
            ],
            WAITING_FOR_GPA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_gpa),
            ],
            WAITING_FOR_MOCK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_mock),
            ],
            WAITING_FOR_GRADE_LEVEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_grade_level),
            ],
            WAITING_FOR_HS_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_hs_type),
            ],
            WAITING_FOR_KEYWORDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_keywords),
            ],
            WAITING_FOR_CSAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_csat),
            ],
            WAITING_FOR_BULK_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_bulk_input),
            ],
            WAITING_FOR_PHOTO: [
                MessageHandler(filters.PHOTO, collect_photo),
            ],
            WAITING_FOR_MORE_PHOTOS: [
                MessageHandler(filters.PHOTO, collect_photo),
                CallbackQueryHandler(process_ocr_batch, pattern="^start_ocr$"),
            ],
            CONFIRM_OCR_RESULT: [
                CallbackQueryHandler(confirm_ocr_yes, pattern="^ocr_yes$"),
                CallbackQueryHandler(confirm_ocr_no,  pattern="^ocr_no$"),
            ],
        },
        fallbacks=[
            CommandHandler("profile", profile_wizard_start),
            CommandHandler("cancel",  cancel_profile),
        ],
        per_message=False,
        name="profile_wizard",
    )
    app.add_handler(_profile_conv)

    # 인라인 버튼 콜백 (프로필 위자드에서 처리되지 않은 나머지)
    app.add_handler(CallbackQueryHandler(callback_handler))

    # 학생부 PDF 업로드 핸들러 (텍스트 핸들러보다 먼저 등록)
    app.add_handler(MessageHandler(filters.Document.PDF, cmd_pdf_업로드))

    # 자유 텍스트 질문 (커맨드 제외, 프로필 위자드 TYPING 상태 제외)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # 전역 에러 핸들러 — 모든 미처리 Telegram 예외를 telegram_errors.log 에 기록
    app.add_error_handler(_global_error_handler)

    logger.info("[봇] 폴링 시작 (Ctrl+C로 종료)")
    logger.info(
        f"[로그] 에러 로그: data/fix_error/telegram_errors.log / crawler_errors.log"
        f" | 사용자별: data/logs/users/{{username}}/actions.log"
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()