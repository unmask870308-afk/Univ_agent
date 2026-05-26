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

import subprocess
import sys
import os
import json
import logging
import re
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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────────────────────

log_dir = Path(__file__).parent.parent / "data" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / f"telegram_{datetime.now():%Y%m%d_%H%M%S}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 환경변수 로드
# ─────────────────────────────────────────────────────────────

env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path)

# TELEGRAM_BOT_TOKEN 또는 TELEGRAM_TOKEN 둘 다 허용
TELEGRAM_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TELEGRAM_TOKEN")
    or ""
)
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")

if not TELEGRAM_TOKEN:
    logger.error("=" * 60)
    logger.error("[오류] TELEGRAM_BOT_TOKEN (또는 TELEGRAM_TOKEN)이 설정되지 않았습니다.")
    logger.error("  .env 파일에 TELEGRAM_BOT_TOKEN=your_token 을 추가하세요.")
    logger.error("  봇 토큰 발급: https://t.me/BotFather")
    logger.error("=" * 60)
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
# 데이터 경로
# ─────────────────────────────────────────────────────────────

프로젝트_루트 = Path(__file__).parent.parent
입시_데이터_경로 = 프로젝트_루트 / "data" / "student" / "parsed_admission_guide.json"
프로필_경로     = 프로젝트_루트 / "data" / "student" / "user_profiles.json"
프로필_경로.parent.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# 입시 데이터 로더
# ─────────────────────────────────────────────────────────────

class 입시데이터:
    """parsed_admission_guide.json을 메모리에 로드하고 검색 기능을 제공합니다."""

    def __init__(self):
        self._원본: dict = {}
        self._대학_맵: dict[str, list[dict]] = {}  # 대학명 → 문서 목록
        self.로드()

    def 로드(self):
        if not 입시_데이터_경로.exists():
            logger.warning(f"[데이터] 입시 데이터 없음: {입시_데이터_경로}")
            return
        with open(입시_데이터_경로, encoding="utf-8") as f:
            self._원본 = json.load(f)

        self._대학_맵.clear()
        for 문서 in self._원본.get("대학_목록", []):
            대학명 = 문서.get("대학명", "")
            if 대학명:
                self._대학_맵.setdefault(대학명, []).append(문서)

        logger.info(f"[데이터] {len(self._대학_맵)}개 대학, "
                    f"{len(self._원본.get('대학_목록', []))}개 문서 로드 완료")

    def 대학_목록(self) -> list[str]:
        return sorted(self._대학_맵.keys())

    def 대학_검색(self, 키워드: str) -> list[str]:
        """키워드로 대학명을 검색합니다 (부분 일치)."""
        키워드 = 키워드.strip()
        return [n for n in self._대학_맵 if 키워드 in n]

    def 문서_목록(self, 대학명: str) -> list[dict]:
        return self._대학_맵.get(대학명, [])

    def 최신_수시_문서(self, 대학명: str) -> dict | None:
        """대학명으로 가장 최신 수시모집요강 문서를 반환합니다."""
        문서들 = self._대학_맵.get(대학명, [])
        수시들 = [d for d in 문서들 if "수시" in d.get("문서_유형", "")]
        if not 수시들:
            수시들 = 문서들
        # 학년도 내림차순 정렬
        수시들.sort(key=lambda d: d.get("학년도", ""), reverse=True)
        return 수시들[0] if 수시들 else None

    def 전형_검색(self, 대학명: str, 전형_키워드: str) -> list[dict]:
        """특정 대학의 전형 중 키워드가 포함된 전형들을 반환합니다."""
        문서 = self.최신_수시_문서(대학명)
        if not 문서:
            return []
        전형들 = 문서.get("수시_전형목록") or []
        if not 전형_키워드:
            return 전형들
        return [t for t in 전형들 if 전형_키워드 in t.get("전형명", "")]

    @property
    def 총_문서_수(self) -> int:
        return len(self._원본.get("대학_목록", []))

    @property
    def 갱신일시(self) -> str:
        return self._원본.get("생성_일시", "미상")


# ─────────────────────────────────────────────────────────────
# 학생 프로필 관리
# ─────────────────────────────────────────────────────────────

class 프로필관리자:
    """학생 프로필을 JSON 파일로 저장·조회합니다."""

    def __init__(self):
        self._데이터: dict = {"users": {}, "최종_업데이트": ""}
        self._로드()

    def _로드(self):
        if 프로필_경로.exists():
            try:
                with open(프로필_경로, encoding="utf-8") as f:
                    self._데이터 = json.load(f)
            except Exception:
                pass

    def _저장(self):
        self._데이터["최종_업데이트"] = datetime.now().isoformat(timespec="seconds")
        with open(프로필_경로, "w", encoding="utf-8") as f:
            json.dump(self._데이터, f, ensure_ascii=False, indent=2)

    def 프로필_가져오기(self, user_id: int) -> dict:
        uid = str(user_id)
        return self._데이터["users"].get(uid, {})

    def 프로필_업데이트(self, user_id: int, update_data: dict):
        uid = str(user_id)
        if uid not in self._데이터["users"]:
            self._데이터["users"][uid] = {
                "user_id": user_id,
                "최초_접속": datetime.now().isoformat(timespec="seconds"),
                "관심_대학": [],
                "질문_이력": [],
            }
        self._데이터["users"][uid].update(update_data)
        self._데이터["users"][uid]["최근_접속"] = datetime.now().isoformat(timespec="seconds")
        self._저장()

    def 접속_기록(self, user: Any):
        """사용자 접속 기록을 업데이트합니다."""
        self.프로필_업데이트(user.id, {
            "username": user.username or "",
            "full_name": user.full_name or "",
        })

    def 질문_기록(self, user_id: int, 질문: str, 답변_요약: str):
        uid = str(user_id)
        프로필 = self._데이터["users"].get(uid)
        if not 프로필:
            return
        이력 = 프로필.setdefault("질문_이력", [])
        이력.append({
            "시각": datetime.now().isoformat(timespec="seconds"),
            "질문": 질문[:200],
            "답변": 답변_요약[:300],
        })
        # 최근 50개만 유지
        프로필["질문_이력"] = 이력[-50:]
        self._저장()

    def 관심_대학_추가(self, user_id: int, 대학명: str) -> bool:
        프로필 = self.프로필_가져오기(user_id)
        관심 = 프로필.get("관심_대학", [])
        if 대학명 in 관심:
            return False
        관심.append(대학명)
        self.프로필_업데이트(user_id, {"관심_대학": 관심})
        return True

    def 관심_대학_제거(self, user_id: int, 대학명: str) -> bool:
        프로필 = self.프로필_가져오기(user_id)
        관심 = 프로필.get("관심_대학", [])
        if 대학명 not in 관심:
            return False
        관심.remove(대학명)
        self.프로필_업데이트(user_id, {"관심_대학": 관심})
        return True

    @property
    def 총_사용자_수(self) -> int:
        return len(self._데이터["users"])


# ─────────────────────────────────────────────────────────────
# Gemini AI 자연어 응답
# ─────────────────────────────────────────────────────────────

class AI응답기:
    """Gemini API로 학생의 자연어 질문에 답합니다."""

    def __init__(self, api_key: str, 데이터: 입시데이터):
        if not api_key:
            self._client = None
            return
        from google import genai
        self._client = genai.Client(api_key=api_key)
        self._데이터 = 데이터
        self._model = "gemini-2.5-flash-lite"
        logger.info(f"[AI] Gemini 응답기 초기화 완료 (모델: {self._model})")

    @property
    def 사용가능(self) -> bool:
        return self._client is not None

    def 질문_답변(self, 질문: str, 대학명: str | None = None) -> str:
        """학생 질문에 입시 데이터를 기반으로 답합니다."""
        if not self._client:
            return "AI 응답 기능이 비활성화되어 있습니다. (GEMINI_API_KEY 미설정)"

        from google.genai import types

        # 관련 데이터 추출
        컨텍스트_블록 = []
        검색_대상 = [대학명] if 대학명 else self._데이터.대학_목록()[:5]

        for 대학 in 검색_대상:
            문서 = self._데이터.최신_수시_문서(대학)
            if not 문서:
                continue
            전형들 = 문서.get("수시_전형목록") or []
            줄들 = [f"## {대학} {문서.get('학년도','')} {문서.get('문서_유형','')}"]
            for t in 전형들[:10]:
                전형명 = t.get("전형명", "?")
                모집인원 = t.get("모집인원")
                비율 = t.get("전형요소_반영비율") or {}
                수능 = t.get("수능최저학력기준") or {}
                비율_요약 = ", ".join(
                    f"{k}:{v}%" for k, v in 비율.items()
                    if v is not None and str(v).replace(".", "").isdigit()
                )
                수능_적용 = "있음" if 수능.get("적용여부") else "없음"
                수능_기준 = 수능.get("기준_상세") or ""
                줄들.append(
                    f"- [{전형명}] 모집인원:{모집인원}명, "
                    f"반영비율:{비율_요약 or '미확인'}, "
                    f"수능최저:{수능_적용}"
                    + (f"({수능_기준[:60]})" if 수능_기준 else "")
                )
            컨텍스트_블록.append("\n".join(줄들))

        컨텍스트 = "\n\n".join(컨텍스트_블록) if 컨텍스트_블록 else "관련 데이터 없음"

        프롬프트 = f"""당신은 대한민국 대학 입시 전문 상담사입니다.
아래 입시 데이터를 바탕으로 학생의 질문에 친절하고 간결하게 답변해주세요.
텔레그램 메시지이므로 200자 이내로 답변하세요. 마크다운은 **굵게**와 줄바꿈만 사용하세요.

[입시 데이터]
{컨텍스트}

[학생 질문]
{질문}

답변:"""

        try:
            응답 = self._client.models.generate_content(
                model=self._model,
                contents=프롬프트,
                config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=512),
            )
            return 응답.text.strip()
        except Exception as e:
            logger.warning(f"[AI] 응답 실패: {e}")
            return "AI 응답 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."


# ─────────────────────────────────────────────────────────────
# 메시지 포매터
# ─────────────────────────────────────────────────────────────

def 전형_카드(전형: dict) -> str:
    """전형 정보를 텔레그램 메시지 형식으로 포매팅합니다."""
    전형명 = 전형.get("전형명", "미상")
    전형유형 = 전형.get("전형_유형", "")
    모집인원 = 전형.get("모집인원")
    비율 = 전형.get("전형요소_반영비율") or {}
    수능 = 전형.get("수능최저학력기준") or {}
    비고 = 전형.get("비고") or ""

    비율_행 = "\n".join(
        f"  • {k}: {v}%"
        for k, v in 비율.items()
        if v is not None and str(v).replace(".", "").isdigit()
    ) or "  • 미확인"

    수능_적용 = 수능.get("적용여부", False)
    수능_기준 = 수능.get("기준_상세") or ""

    lines = [
        f"📋 *{전형명}*",
        f"  유형: {전형유형}" if 전형유형 else "",
        f"  모집인원: {모집인원}명" if 모집인원 else "  모집인원: 미확인",
        "",
        "📊 *전형요소 반영비율*",
        비율_행,
        "",
        f"📌 *수능 최저학력기준*",
        f"  {'✅ 있음' if 수능_적용 else '❌ 없음'}",
        f"  {수능_기준[:100]}" if 수능_기준 and 수능_적용 else "",
        f"\n💬 {비고[:80]}" if 비고 else "",
    ]
    return "\n".join(l for l in lines if l is not None)


def 대학_요약(문서: dict) -> str:
    """대학 문서의 핵심 정보를 한 줄로 요약합니다."""
    대학명 = 문서.get("대학명", "미상")
    학년도 = 문서.get("학년도", "")
    문서유형 = 문서.get("문서_유형", "")
    전형수 = len(문서.get("수시_전형목록") or [])
    총모집 = sum(
        t.get("모집인원") or 0
        for t in (문서.get("수시_전형목록") or [])
        if isinstance(t.get("모집인원"), (int, float))
    )
    return (
        f"🏫 *{대학명}* {학년도} {문서유형}\n"
        f"  전형 수: {전형수}개"
        + (f"  |  총 모집인원: {총모집}명" if 총모집 else "")
    )


# ─────────────────────────────────────────────────────────────
# 전역 객체 (핸들러에서 공유)
# ─────────────────────────────────────────────────────────────

_DB   = 입시데이터()
_프로필 = 프로필관리자()
_AI   = AI응답기(GEMINI_API_KEY, _DB)


# ─────────────────────────────────────────────────────────────
# 텔레그램 커맨드 핸들러
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """봇 시작 및 소개."""
    user = update.effective_user
    _프로필.접속_기록(user)
    logger.info(f"[봇] /start: {user.full_name} ({user.id})")

    text = (
        f"안녕하세요, *{user.first_name}*님! 👋\n\n"
        f"🎓 *대학 입시 정보 봇*입니다.\n"
        f"2026학년도 수시 모집요강을 쉽게 조회할 수 있어요.\n\n"
        f"📚 *현재 수록 대학*: {len(_DB.대학_목록())}개교\n"
        f"📅 *데이터 기준*: {_DB.갱신일시[:10]}\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔍 */대학목록* — 전체 대학 목록\n"
        "🏫 */검색 [대학명]* — 대학 정보 조회\n"
        "📋 */전형 [대학명]* — 수시 전형 목록\n"
        "⭐ */관심추가 [대학명]* — 관심 대학 등록\n"
        "👤 */내프로필* — 내 프로필 조회\n"
        "❓ *자유 질문* — AI에게 무엇이든 물어보세요\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "예) `연세대 수능최저 알려줘`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_대학목록(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """전체 대학 목록 버튼 표시."""
    _프로필.접속_기록(update.effective_user)
    대학들 = _DB.대학_목록()
    if not 대학들:
        await update.message.reply_text("❌ 저장된 입시 데이터가 없습니다.")
        return

    # 2열 인라인 키보드
    키보드 = []
    행 = []
    for 대학 in 대학들:
        행.append(InlineKeyboardButton(대학, callback_data=f"대학:{대학}"))
        if len(행) == 2:
            키보드.append(행)
            행 = []
    if 행:
        키보드.append(행)

    markup = InlineKeyboardMarkup(키보드)
    await update.message.reply_text(
        f"📚 수록 대학 목록 ({len(대학들)}개교)\n버튼을 눌러 조회하세요:",
        reply_markup=markup,
    )


async def cmd_검색(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """대학명 키워드로 검색."""
    _프로필.접속_기록(update.effective_user)
    키워드 = " ".join(context.args).strip() if context.args else ""
    if not 키워드:
        await update.message.reply_text(
            "사용법: `/검색 대학명`\n예) `/검색 연세대`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    결과 = _DB.대학_검색(키워드)
    if not 결과:
        await update.message.reply_text(f"❌ '{키워드}' 관련 대학 데이터가 없습니다.")
        return

    if len(결과) == 1:
        # 정확히 1개 → 바로 전형 목록 표시
        await _대학_전형_목록_표시(update, 결과[0])
    else:
        키보드 = [[InlineKeyboardButton(n, callback_data=f"대학:{n}")] for n in 결과]
        await update.message.reply_text(
            f"🔍 '{키워드}' 검색 결과 ({len(결과)}개):",
            reply_markup=InlineKeyboardMarkup(키보드),
        )


async def cmd_전형(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """특정 대학의 전형 목록 표시."""
    _프로필.접속_기록(update.effective_user)
    대학명_입력 = " ".join(context.args).strip() if context.args else ""
    if not 대학명_입력:
        await update.message.reply_text(
            "사용법: `/전형 대학명`\n예) `/전형 서울대학교`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    결과 = _DB.대학_검색(대학명_입력)
    if not 결과:
        await update.message.reply_text(f"❌ '{대학명_입력}' 데이터가 없습니다.")
        return

    await _대학_전형_목록_표시(update, 결과[0])


async def _대학_전형_목록_표시(update: Update, 대학명: str):
    """대학의 수시 전형 목록을 인라인 버튼으로 표시합니다."""
    문서 = _DB.최신_수시_문서(대학명)
    if not 문서:
        await update.message.reply_text(f"❌ {대학명} 데이터가 없습니다.")
        return

    전형들 = 문서.get("수시_전형목록") or []
    if not 전형들:
        await update.message.reply_text(f"❌ {대학명}의 전형 정보가 없습니다.")
        return

    요약 = 대학_요약(문서)
    키보드 = []
    for i, 전형 in enumerate(전형들):
        전형명 = 전형.get("전형명", f"전형{i+1}")
        단축명 = 전형명[:20]  # 버튼 텍스트 제한
        키보드.append([InlineKeyboardButton(단축명, callback_data=f"전형:{대학명}:{i}")])

    # 관심 대학 추가 버튼
    키보드.append([InlineKeyboardButton("⭐ 관심 대학 추가", callback_data=f"관심추가:{대학명}")])

    await update.message.reply_text(
        f"{요약}\n\n전형을 선택하세요:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(키보드),
    )


async def cmd_관심추가(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """관심 대학 추가."""
    user = update.effective_user
    _프로필.접속_기록(user)
    대학명 = " ".join(context.args).strip() if context.args else ""
    if not 대학명:
        await update.message.reply_text("사용법: `/관심추가 대학명`", parse_mode=ParseMode.MARKDOWN)
        return

    결과 = _DB.대학_검색(대학명)
    타깃 = 결과[0] if 결과 else 대학명
    추가됨 = _프로필.관심_대학_추가(user.id, 타깃)
    if 추가됨:
        await update.message.reply_text(f"⭐ *{타깃}*을(를) 관심 대학에 추가했습니다.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"이미 관심 대학에 등록되어 있습니다: {타깃}")


async def cmd_내프로필(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """학생 프로필 조회."""
    user = update.effective_user
    _프로필.접속_기록(user)
    프로필 = _프로필.프로필_가져오기(user.id)

    관심 = 프로필.get("관심_대학", [])
    이력수 = len(프로필.get("질문_이력", []))
    최초 = 프로필.get("최초_접속", "")[:10]
    최근 = 프로필.get("최근_접속", "")[:16].replace("T", " ")

    관심_버튼 = []
    for 대학 in 관심:
        관심_버튼.append([
            InlineKeyboardButton(f"🏫 {대학}", callback_data=f"대학:{대학}"),
            InlineKeyboardButton(f"🗑 제거", callback_data=f"관심제거:{대학}"),
        ])

    text = (
        f"👤 *{user.full_name}* 프로필\n\n"
        f"  최초 접속: {최초}\n"
        f"  최근 접속: {최근}\n"
        f"  질문 횟수: {이력수}회\n\n"
        f"⭐ *관심 대학* ({len(관심)}개)\n"
        + ("\n".join(f"  • {d}" for d in 관심) if 관심 else "  (없음)")
    )

    markup = InlineKeyboardMarkup(관심_버튼) if 관심_버튼 else None
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def cmd_도움말(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """도움말 출력."""
    await update.message.reply_text(
        "📖 *도움말*\n\n"
        "*/대학목록* — 수록된 전체 대학 목록\n"
        "*/검색 [키워드]* — 대학명 키워드 검색\n"
        "*/전형 [대학명]* — 수시 전형 목록 조회\n"
        "*/관심추가 [대학명]* — 관심 대학 등록\n"
        "*/내프로필* — 내 프로필 및 관심 대학\n\n"
        "💬 *자유 질문 예시*\n"
        "  `서강대 논술전형 수능최저 알려줘`\n"
        "  `고려대 학생부종합 반영비율은?`\n"
        "  `수능최저 없는 대학 알려줘`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────────────────────────────────────────────────────
# 인라인 버튼 콜백 핸들러
# ─────────────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """인라인 키보드 버튼 클릭 처리."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = query.from_user
    _프로필.접속_기록(user)
    logger.info(f"[버튼] {user.full_name}: {data}")

    # 대학 선택 → 전형 목록
    if data.startswith("대학:"):
        대학명 = data[3:]
        문서 = _DB.최신_수시_문서(대학명)
        if not 문서:
            await query.edit_message_text(f"❌ {대학명} 데이터가 없습니다.")
            return

        전형들 = 문서.get("수시_전형목록") or []
        요약 = 대학_요약(문서)
        키보드 = []
        for i, 전형 in enumerate(전형들):
            전형명 = 전형.get("전형명", f"전형{i+1}")[:20]
            키보드.append([InlineKeyboardButton(전형명, callback_data=f"전형:{대학명}:{i}")])
        키보드.append([InlineKeyboardButton("⭐ 관심 추가", callback_data=f"관심추가:{대학명}")])
        키보드.append([InlineKeyboardButton("« 목록으로", callback_data="뒤로:목록")])

        await query.edit_message_text(
            f"{요약}\n\n전형을 선택하세요:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(키보드),
        )

    # 전형 상세 조회
    elif data.startswith("전형:"):
        _, 대학명, idx_str = data.split(":", 2)
        idx = int(idx_str)
        문서 = _DB.최신_수시_문서(대학명)
        전형들 = (문서.get("수시_전형목록") or []) if 문서 else []
        if idx >= len(전형들):
            await query.edit_message_text("❌ 해당 전형 정보를 찾을 수 없습니다.")
            return

        전형 = 전형들[idx]
        카드 = 전형_카드(전형)
        키보드 = [
            [InlineKeyboardButton("« 전형 목록으로", callback_data=f"대학:{대학명}")],
        ]
        await query.edit_message_text(
            카드,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(키보드),
        )
        _프로필.질문_기록(user.id, f"{대학명} {전형.get('전형명','')} 조회", 카드[:100])

    # 관심 대학 추가
    elif data.startswith("관심추가:"):
        대학명 = data[5:]
        추가됨 = _프로필.관심_대학_추가(user.id, 대학명)
        msg = f"⭐ *{대학명}* 관심 대학에 추가됐습니다!" if 추가됨 else f"이미 등록된 대학입니다: {대학명}"
        await query.answer(msg, show_alert=True)

    # 관심 대학 제거
    elif data.startswith("관심제거:"):
        대학명 = data[5:]
        제거됨 = _프로필.관심_대학_제거(user.id, 대학명)
        msg = f"🗑 *{대학명}* 관심 대학에서 제거됐습니다." if 제거됨 else "이미 제거된 대학입니다."
        await query.answer(msg, show_alert=True)
        # 프로필 화면 갱신
        프로필 = _프로필.프로필_가져오기(user.id)
        관심 = 프로필.get("관심_대학", [])
        관심_버튼 = [
            [InlineKeyboardButton(f"🏫 {d}", callback_data=f"대학:{d}"),
             InlineKeyboardButton("🗑 제거", callback_data=f"관심제거:{d}")]
            for d in 관심
        ]
        await query.edit_message_text(
            f"⭐ *관심 대학* ({len(관심)}개)\n"
            + ("\n".join(f"  • {d}" for d in 관심) if 관심 else "  (없음)"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(관심_버튼) if 관심_버튼 else None,
        )

    # 뒤로가기
    elif data.startswith("뒤로:"):
        대학들 = _DB.대학_목록()
        키보드 = []
        행 = []
        for 대학 in 대학들:
            행.append(InlineKeyboardButton(대학, callback_data=f"대학:{대학}"))
            if len(행) == 2:
                키보드.append(행)
                행 = []
        if 행:
            키보드.append(행)
        await query.edit_message_text(
            f"📚 수록 대학 목록 ({len(대학들)}개교)\n버튼을 눌러 조회하세요:",
            reply_markup=InlineKeyboardMarkup(키보드),
        )


# ─────────────────────────────────────────────────────────────
# 자유 텍스트 → AI 응답
# ─────────────────────────────────────────────────────────────

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """커맨드가 아닌 일반 텍스트 메시지를 AI로 처리합니다."""
    user = update.effective_user
    _프로필.접속_기록(user)
    질문 = update.message.text.strip()
    logger.info(f"[질문] {user.full_name}: {질문[:80]}")

    # 대학명 자동 감지
    감지된_대학 = None
    for 대학 in _DB.대학_목록():
        # 줄임말도 허용 (예: 연세대 → 연세대학교)
        if any(kw in 질문 for kw in [대학, 대학[:-2]]):
            감지된_대학 = 대학
            break

    # "~전형 목록"처럼 명시적 데이터 요청은 버튼으로 유도
    전형_요청 = any(kw in 질문 for kw in ["전형", "전형목록", "수시전형", "전형 목록"])
    if 감지된_대학 and 전형_요청:
        await _대학_전형_목록_표시(update, 감지된_대학)
        return

    if not _AI.사용가능:
        # AI 없으면 대학명 감지 시 버튼 제공
        if 감지된_대학:
            await _대학_전형_목록_표시(update, 감지된_대학)
        else:
            await update.message.reply_text(
                "AI 응답 기능이 비활성화되어 있습니다.\n"
                "*/검색 대학명* 또는 */대학목록* 명령어를 이용해주세요.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # AI 응답
    await update.message.chat.send_action("typing")
    답변 = _AI.질문_답변(질문, 감지된_대학)
    await update.message.reply_text(답변, parse_mode=ParseMode.MARKDOWN)
    _프로필.질문_기록(user.id, 질문, 답변[:200])

    # 관련 대학 버튼 제공
    if 감지된_대학:
        await update.message.reply_text(
            f"📋 {감지된_대학} 전형 목록도 보시겠어요?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"{감지된_대학} 전형 보기", callback_data=f"대학:{감지된_대학}")
            ]]),
        )


# ─────────────────────────────────────────────────────────────
# 봇 실행
# ─────────────────────────────────────────────────────────────

def main():
    logger.info("━" * 60)
    logger.info("  대학 입시 정보 텔레그램 봇 시작")
    logger.info(f"  수록 대학: {len(_DB.대학_목록())}개  |  데이터: {_DB.갱신일시[:10]}")
    logger.info(f"  AI 응답: {'활성화 (Gemini)' if _AI.사용가능 else '비활성화'}")
    logger.info(f"  사용자 수: {_프로필.총_사용자_수}명")
    logger.info("━" * 60)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # 커맨드 핸들러 등록
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_도움말))
    app.add_handler(CommandHandler("도움말",   cmd_도움말))
    app.add_handler(CommandHandler("대학목록", cmd_대학목록))
    app.add_handler(CommandHandler("검색",     cmd_검색))
    app.add_handler(CommandHandler("전형",     cmd_전형))
    app.add_handler(CommandHandler("관심추가", cmd_관심추가))
    app.add_handler(CommandHandler("내프로필", cmd_내프로필))

    # 인라인 버튼 콜백
    app.add_handler(CallbackQueryHandler(callback_handler))

    # 자유 텍스트 질문 (커맨드 제외)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("[봇] 폴링 시작 (Ctrl+C로 종료)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
