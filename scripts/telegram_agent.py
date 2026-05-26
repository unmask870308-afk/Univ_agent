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
# 사용자 요청 관리
# ─────────────────────────────────────────────────────────────

요청_경로 = 프로젝트_루트 / "data" / "student" / "user_requests.json"


class 요청관리자:
    """사용자 대학 추가·기능 요청을 JSON 파일로 저장합니다."""

    def __init__(self):
        self._데이터: dict = {"requests": [], "총_요청_수": 0}
        self._로드()

    def _로드(self):
        if 요청_경로.exists():
            try:
                with open(요청_경로, encoding="utf-8") as f:
                    self._데이터 = json.load(f)
            except Exception:
                pass

    def _저장(self):
        self._데이터["총_요청_수"] = len(self._데이터["requests"])
        self._데이터["최종_업데이트"] = datetime.now().isoformat(timespec="seconds")
        요청_경로.parent.mkdir(parents=True, exist_ok=True)
        with open(요청_경로, "w", encoding="utf-8") as f:
            json.dump(self._데이터, f, ensure_ascii=False, indent=2)

    def 요청_저장(self, user: Any, 내용: str, 유형: str = "일반") -> int:
        """
        요청을 저장하고 부여된 요청 번호를 반환합니다.

        저장 필드:
          request_id  — 순번 (1부터 시작)
          timestamp   — ISO 8601 형식
          chat_id     — Telegram chat ID
          user_id     — Telegram user ID
          username    — Telegram 사용자명 (없으면 빈 문자열)
          full_name   — 표시 이름
          유형        — "대학추가" | "기능요청" | "일반"
          내용        — 요청 본문
        """
        request_id = len(self._데이터["requests"]) + 1
        항목 = {
            "request_id": request_id,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "chat_id": user.id,
            "user_id": user.id,
            "username": user.username or "",
            "full_name": user.full_name or "",
            "유형": 유형,
            "내용": 내용,
            "처리_상태": "접수",
        }
        self._데이터["requests"].append(항목)
        self._저장()
        logger.info(f"[요청#{request_id}] {user.full_name}: {내용[:60]}")
        return request_id

    @property
    def 총_요청_수(self) -> int:
        return len(self._데이터["requests"])


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

_DB      = 입시데이터()
_프로필  = 프로필관리자()
_요청    = 요청관리자()
_AI      = AI응답기(GEMINI_API_KEY, _DB)


# ─────────────────────────────────────────────────────────────
# 텔레그램 커맨드 핸들러
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """봇 시작 — 환영 메시지 + 입력 가이드 안내."""
    user = update.effective_user
    _프로필.접속_기록(user)
    logger.info(f"[봇] /start: {user.full_name} ({user.id})")

    환영 = (
        f"안녕하세요, *{user.first_name}*님! 👋\n\n"
        "🎓 *고등학생 입시 AI 분석봇*에 오신 걸 환영합니다!\n"
        "2026학년도 수시 모집요강 데이터를 기반으로\n"
        "여러분의 *맞춤 전형·대학*을 AI가 분석해 드려요.\n\n"
        f"📚 수록 대학: *{len(_DB.대학_목록())}개교*"
        f"  |  📅 데이터 기준: {_DB.갱신일시[:10]}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 *AI 맞춤 분석*을 받으려면\n"
        "아래처럼 성적을 자유롭게 입력해 보세요!\n\n"
        "`희망학과: 컴퓨터공학`\n"
        "`내신: 1.8`\n"
        "`모의고사: 국2 수1 영2 탐1`\n"
        "`세특: 파이썬 데이터분석, 알고리즘 대회`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📖 */help* — 상세 입력 가이드 (복사용 양식 포함)\n"
        "🔍 */list* — 전체 대학 목록\n"
        "🏫 */search 대학명* — 전형 상세 조회\n"
        "👤 */profile* — 내 프로필 확인\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(환영, parse_mode=ParseMode.MARKDOWN)


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
            "사용법: `/search 대학명`\n예) `/search 연세대`",
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
            "사용법: `/detail 대학명`\n예) `/detail 서울대학교`",
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
        await update.message.reply_text("사용법: `/add 대학명`", parse_mode=ParseMode.MARKDOWN)
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

    성적 = 프로필.get("성적", {})
    성적_행 = "\n".join(f"  • *{k}*: {v}" for k, v in 성적.items()) if 성적 else "  (미입력)"

    text = (
        f"👤 *{user.full_name}* 프로필\n\n"
        f"  최초 접속: {최초}\n"
        f"  최근 접속: {최근}\n"
        f"  질문 횟수: {이력수}회\n\n"
        f"📊 *저장된 성적 데이터*\n{성적_행}\n\n"
        f"⭐ *관심 대학* ({len(관심)}개)\n"
        + ("\n".join(f"  • {d}" for d in 관심) if 관심 else "  (없음)")
    )

    markup = InlineKeyboardMarkup(관심_버튼) if 관심_버튼 else None
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def cmd_도움말(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """고2 학생 맞춤 데이터 입력 가이드 출력 (2개 메시지로 분리)."""

    # ── 메시지 1: 입력 항목 설명 ──────────────────────────────
    가이드1 = (
        "📌 *성적 입력 가이드* — 고2 학생 맞춤\n\n"
        "아래 4가지 항목을 입력하면 AI가\n"
        "*맞춤 대학·전형·수능최저 분석*을 해드립니다! 🎯\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "1️⃣ *희망학과* (필수)\n"
        "  진학하고 싶은 학과·전공을 적어주세요.\n"
        "  _복수 입력 가능 (쉼표로 구분)_\n"
        "  예) `희망학과: 컴퓨터공학, 소프트웨어학`\n\n"

        "2️⃣ *내신 등급* (필수)\n"
        "  전 과목 평균 등급을 소수점 1자리로 입력하세요.\n"
        "  _학생부교과전형 지원 가능 여부 판단에 사용됩니다._\n"
        "  예) `내신: 2.3`\n\n"

        "3️⃣ *모의고사 성적*\n"
        "  최근 모의고사의 과목별 등급을 입력하세요.\n"
        "  국어/수학/영어/탐구(1과목 기준)\n"
        "  _수능 최저학력기준 충족 여부를 확인합니다._\n"
        "  예) `모의고사: 국2 수1 영2 탐1`\n\n"

        "4️⃣ *생기부 핵심 키워드*\n"
        "  세특·동아리·수상·봉사 활동 중 강점을 적어주세요.\n"
        "  _학생부종합전형 적합 전형 분석에 활용됩니다._\n"
        "  예) `세특: 파이썬 데이터분석, 수학경시 수상, 물리탐구반`"
    )

    # ── 메시지 2: 복사용 양식 + 실제 예시 ───────────────────────
    가이드2 = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "✏️ *복사해서 쓰는 입력 양식*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "```\n"
        "희망학과: [학과명]\n"
        "내신: [X.X]\n"
        "모의고사: 국[등급] 수[등급] 영[등급] 탐[등급]\n"
        "세특: [키워드1, 키워드2, ...]\n"
        "```\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💬 *실제 입력 예시 (그대로 보내도 됩니다!)*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "```\n"
        "희망학과: 컴퓨터공학\n"
        "내신: 1.8\n"
        "모의고사: 국2 수1 영2 탐1\n"
        "세특: 파이썬 데이터분석, 알고리즘 대회 수상, 수학 심화탐구\n"
        "```\n\n"
        "위 형식 중 *일부만 입력해도 분석*이 가능해요.\n"
        "예) `내신 2.1 컴퓨터공학 지망`처럼 자유롭게 입력하셔도 됩니다. 🤖\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔧 *기타 명령어*\n"
        "  */list* — 전체 대학 목록\n"
        "  */search 대학명* — 대학 전형 조회\n"
        "  */detail 대학명* — 전형 상세 정보\n"
        "  */add 대학명* — 관심 대학 등록\n"
        "  */profile* — 내 프로필 및 저장된 성적\n\n"
        "  단축어: `대학목록` `내프로필` `도움말`"
    )

    # /help 안내에 /request 명령어도 포함
    가이드2 += "\n  */request 내용* — 대학 추가·기능 요청"
    await update.message.reply_text(가이드1, parse_mode=ParseMode.MARKDOWN)
    await update.message.reply_text(가이드2, parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────────────────────────────────────────
# 사용자 요청 접수 핸들러
# ─────────────────────────────────────────────────────────────

async def cmd_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/request 명령어: 대학 추가 또는 기능 요청을 접수합니다."""
    user = update.effective_user
    _프로필.접속_기록(user)

    내용 = " ".join(context.args).strip() if context.args else ""
    if not 내용:
        await update.message.reply_text(
            "📬 *요청 방법*\n\n"
            "*/request 요청 내용* 형식으로 입력하거나\n"
            "`[요청]`으로 시작하는 메시지를 보내주세요.\n\n"
            "예시:\n"
            "  `/request 부산대학교 데이터 추가 부탁드립니다`\n"
            "  `[요청] KAIST 수시 전형 정보가 필요해요`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await _요청_접수(update, user, 내용)


async def _요청_접수(update: Update, user: Any, 내용: str):
    """요청 내용을 저장하고 확인 메시지를 전송합니다."""
    # 요청 유형 자동 분류
    대학_키워드 = ["대학", "학교", "캠퍼스", "추가", "업데이트"]
    유형 = "대학추가" if any(kw in 내용 for kw in 대학_키워드) else "기능요청"

    request_id = _요청.요청_저장(user, 내용, 유형)

    확인_메시지 = (
        f"✅ *요청이 접수되었습니다!*\n\n"
        f"📋 접수 번호: `#{request_id}`\n"
        f"🏷 유형: {유형}\n"
        f"📝 내용: {내용[:100]}\n"
        f"🕐 접수 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        "에이전트가 곧 해당 대학 입시 데이터를 업데이트하겠습니다! 🤖\n"
        "처리 완료 시 별도로 안내드릴게요. 감사합니다 🙏"
    )
    await update.message.reply_text(확인_메시지, parse_mode=ParseMode.MARKDOWN)


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
# 학생 성적 데이터 파싱 유틸리티
# ─────────────────────────────────────────────────────────────

def _성적_파싱(텍스트: str) -> dict:
    """
    자유형식 텍스트에서 학생 성적 데이터를 추출합니다.
    반환 예시:
      {"희망학과": "컴퓨터공학", "내신": "1.8",
       "모의고사": "국2 수1 영2 탐1", "세특": "파이썬 데이터분석"}
    """
    결과: dict[str, str] = {}

    # 희망학과 / 희망학부 / 희망전공
    m = re.search(r"희망(?:학과|학부|전공)[:\s]+([^\n/,]+)", 텍스트)
    if m:
        결과["희망학과"] = m.group(1).strip()

    # 내신
    m = re.search(r"내신[:\s]+([0-9.]+)", 텍스트)
    if m:
        결과["내신"] = m.group(1).strip()

    # 모의고사 (국X 수X 영X 탐X 형태 또는 자유 텍스트)
    m = re.search(r"모의(?:고사)?[:\s]+([^\n]+)", 텍스트)
    if m:
        결과["모의고사"] = m.group(1).strip()
    else:
        # "국2 수1 영2 탐1" 패턴만 있는 경우
        m = re.search(r"국\d\s*수\d\s*영\d\s*탐\d", 텍스트)
        if m:
            결과["모의고사"] = m.group(0).strip()

    # 세특 / 생기부
    m = re.search(r"(?:세특|생기부)[:\s]+([^\n]+)", 텍스트)
    if m:
        결과["세특"] = m.group(1).strip()

    return 결과


async def _성적_입력_처리(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
    텍스트: str,
):
    """
    학생이 성적 데이터를 입력했을 때:
    1) 파싱 → 프로필에 저장
    2) AI에게 맞춤 분석 요청
    3) 결과를 친절하게 안내
    """
    파싱 = _성적_파싱(텍스트)
    if not 파싱:
        return  # 성적 데이터 없으면 일반 AI 흐름으로

    # 프로필 저장
    _프로필.프로필_업데이트(user.id, {"성적": 파싱})
    logger.info(f"[성적] {user.full_name} 데이터 저장: {파싱}")

    # 저장 확인 메시지
    저장_요약 = "\n".join(f"  • *{k}*: {v}" for k, v in 파싱.items())
    await update.message.reply_text(
        f"✅ *성적 데이터가 저장되었습니다!*\n\n{저장_요약}\n\n"
        "🤖 AI가 맞춤 전형을 분석 중입니다...",
        parse_mode=ParseMode.MARKDOWN,
    )

    if not _AI.사용가능:
        await update.message.reply_text(
            "AI 분석 기능이 비활성화되어 있습니다.\n"
            "*/list* 또는 */search 대학명*으로 직접 조회해 보세요.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.chat.send_action("typing")

    # AI 분석 프롬프트 구성
    희망학과 = 파싱.get("희망학과", "미입력")
    내신 = 파싱.get("내신", "미입력")
    모의 = 파싱.get("모의고사", "미입력")
    세특 = 파싱.get("세특", "미입력")

    분석_질문 = (
        f"고등학교 2학년 학생의 성적 데이터입니다.\n"
        f"희망학과: {희망학과}\n"
        f"내신: {내신}등급\n"
        f"모의고사: {모의}\n"
        f"생기부/세특: {세특}\n\n"
        f"이 학생에게 적합한 수시 전형 유형(학생부교과/학생부종합/논술 등)과 "
        f"수능최저학력기준 충족 가능성을 포함해서 맞춤 분석해 주세요."
    )
    답변 = _AI.질문_답변(분석_질문, None)
    await update.message.reply_text(답변, parse_mode=ParseMode.MARKDOWN)
    _프로필.질문_기록(user.id, 텍스트, 답변[:200])

    # 관련 대학 조회 버튼 제공
    대학들 = _DB.대학_검색(희망학과) or _DB.대학_목록()[:4]
    if 대학들:
        키보드 = [[InlineKeyboardButton(d, callback_data=f"대학:{d}")] for d in 대학들[:4]]
        await update.message.reply_text(
            "🏫 관련 대학의 전형 정보를 바로 확인해 보세요:",
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

    # ── 한국어 키워드 → 커맨드 함수 라우팅 ──────────────────────
    # 사용자가 /list 대신 "대학목록"처럼 입력해도 동작하도록 합니다.
    _한국어_단축어: dict[str, object] = {
        "도움말": cmd_도움말,
        "대학목록": cmd_대학목록,
        "내프로필": cmd_내프로필,
    }
    if 질문 in _한국어_단축어:
        await _한국어_단축어[질문](update, context)  # type: ignore[operator]
        return

    # "검색 연세대" / "검색:연세대" 패턴
    _검색_매치 = re.match(r"^검색[:\s]+(.+)$", 질문)
    if _검색_매치:
        context.args = _검색_매치.group(1).split()  # type: ignore[assignment]
        await cmd_검색(update, context)
        return

    # "전형 서울대학교" / "전형:고려대" 패턴
    _전형_매치 = re.match(r"^전형[:\s]+(.+)$", 질문)
    if _전형_매치:
        context.args = _전형_매치.group(1).split()  # type: ignore[assignment]
        await cmd_전형(update, context)
        return

    # "관심추가 한양대" / "관심 추가 한양대" 패턴
    _관심_매치 = re.match(r"^관심\s*추가[:\s]+(.+)$", 질문)
    if _관심_매치:
        context.args = _관심_매치.group(1).split()  # type: ignore[assignment]
        await cmd_관심추가(update, context)
        return

    # "[요청] ..." / "요청: ..." 패턴 → 요청 접수
    _요청_매치 = re.match(r"^\[요청\]\s*(.+)$|^요청[:\s]+(.+)$", 질문, re.DOTALL)
    if _요청_매치:
        내용 = (_요청_매치.group(1) or _요청_매치.group(2) or "").strip()
        if 내용:
            await _요청_접수(update, user, 내용)
            return
    # ─────────────────────────────────────────────────────────────

    # ── 학생 성적 데이터 입력 감지 ────────────────────────────────
    # "내신:", "희망학과:", "모의고사:", "세특:" 중 2개 이상 포함 → 성적 입력으로 판단
    성적_키워드 = ["내신", "희망학과", "희망학부", "모의고사", "모의", "세특", "생기부"]
    성적_히트 = sum(1 for kw in 성적_키워드 if kw in 질문)
    if 성적_히트 >= 2:
        await _성적_입력_처리(update, context, user, 질문)
        return
    # ─────────────────────────────────────────────────────────────

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
        if 감지된_대학:
            await _대학_전형_목록_표시(update, 감지된_대학)
        else:
            await update.message.reply_text(
                "AI 응답 기능이 비활성화되어 있습니다.\n"
                "*/search 대학명* 또는 */list* 명령어를 이용해주세요.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # AI 자유 질문 응답
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
    logger.info(f"  사용자 수: {_프로필.총_사용자_수}명  |  누적 요청: {_요청.총_요청_수}건")
    logger.info("━" * 60)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # 커맨드 핸들러 등록
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_도움말))
    app.add_handler(CommandHandler("list",    cmd_대학목록))
    app.add_handler(CommandHandler("search",  cmd_검색))
    app.add_handler(CommandHandler("detail",  cmd_전형))
    app.add_handler(CommandHandler("add",     cmd_관심추가))
    app.add_handler(CommandHandler("profile", cmd_내프로필))
    app.add_handler(CommandHandler("request", cmd_request))

    # 인라인 버튼 콜백
    app.add_handler(CallbackQueryHandler(callback_handler))

    # 자유 텍스트 질문 (커맨드 제외)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("[봇] 폴링 시작 (Ctrl+C로 종료)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
