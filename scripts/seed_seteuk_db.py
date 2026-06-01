"""
seed_seteuk_db.py — 서울대 합격생 세특 데이터 시딩 에이전트 v3
=============================================================
1단계: Wikipedia 공개 REST API 크롤러 — source='crawler'
2단계: Groq/Ollama 합성 데이터 생성 (Gemini 제외, 학과별 즉시 저장) — source='llm_crawl'
3단계: 부족분 내장 고품질 세특 폴백 — source='builtin'
"""

import argparse
import subprocess
import sys
import os
import sqlite3
import json
import time
import random
import re
import logging
import traceback as _tb
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# 의존성 자동 설치 (venv 우선)
# ─────────────────────────────────────────────────────────────
_REQUIRED = {
    "requests":     "requests",
    "bs4":          "beautifulsoup4",
    "dotenv":       "python-dotenv",
    "google.genai": "google-genai",
    "groq":         "groq",
    "ollama":       "ollama",
}

_venv_py = Path(__file__).parent.parent / "venv" / "bin" / "python3"
_pip_exe = str(_venv_py) if _venv_py.exists() else sys.executable

for _mod, _pkg in _REQUIRED.items():
    try:
        __import__(_mod.split(".")[0])
    except ImportError:
        print(f"[설치] {_pkg} 설치 중...")
        try:
            subprocess.check_call(
                [_pip_exe, "-m", "pip", "install", _pkg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            subprocess.check_call(
                [_pip_exe, "-m", "pip", "install", "--user", _pkg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        print(f"[설치] {_pkg} 완료")

import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# 환경 설정
# ─────────────────────────────────────────────────────────────
프로젝트_루트 = Path(__file__).parent.parent
load_dotenv(프로젝트_루트 / ".env")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

import token_manager as _tm
DB_경로        = 프로젝트_루트 / "data" / "admissions_agent.db"
_크롤러_에러_로그 = 프로젝트_루트 / "data" / "fix_error" / "crawler_errors.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("seed_seteuk")

# ─────────────────────────────────────────────────────────────
# DB 초기화
# ─────────────────────────────────────────────────────────────

def DB_테이블_초기화():
    conn = sqlite3.connect(str(DB_경로))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS successful_seteuks (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            major_category TEXT    NOT NULL,
            subject        TEXT    NOT NULL,
            raw_text       TEXT    NOT NULL,
            source         TEXT    NOT NULL DEFAULT 'unknown',
            created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    cur = conn.execute("PRAGMA table_info(successful_seteuks)")
    columns = {row[1] for row in cur.fetchall()}
    if "source" not in columns:
        conn.execute(
            "ALTER TABLE successful_seteuks ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'"
        )
    conn.commit()
    conn.close()
    logger.info("[DB] successful_seteuks 테이블 준비 완료")


def DB_삽입(레코드들: list[dict]) -> int:
    if not 레코드들:
        return 0
    conn = sqlite3.connect(str(DB_경로))
    성공 = 0
    for 행 in 레코드들:
        try:
            conn.execute(
                "INSERT INTO successful_seteuks "
                "(major_category, subject, raw_text, source) VALUES (?,?,?,?)",
                (
                    str(행.get("major_category", "미분류"))[:100],
                    str(행.get("subject", "미분류"))[:100],
                    str(행.get("raw_text", ""))[:4000],
                    str(행.get("source", "unknown"))[:50],
                ),
            )
            성공 += 1
        except Exception as e:
            logger.warning(f"[DB] 삽입 실패: {e}")
    conn.commit()
    conn.close()
    return 성공


def DB_현재_건수() -> int:
    conn = sqlite3.connect(str(DB_경로))
    n = conn.execute("SELECT COUNT(*) FROM successful_seteuks").fetchone()[0]
    conn.close()
    return n


# ─────────────────────────────────────────────────────────────
# 크롤러 전용 구조화 에러 로거 (LLM 프롬프트 최적화 JSONL)
# ─────────────────────────────────────────────────────────────

def _크롤러_에러_기록(
    task: str,
    error: Exception,
    major: str = "",
    topic: str = "",
    url: str = "",
    model: str = "",
    extra: dict | None = None,
) -> None:
    """
    크롤링·시딩 오류를 crawler_errors.log 에 JSONL 한 줄로 기록합니다.

    형식 (LLM 프롬프트 최적화):
      {"ts":..., "task":..., "major":..., "topic":..., "model":...,
       "url":..., "error_type":..., "error_msg":..., "traceback":...}

    - ts         : ISO-8601 타임스탬프 (시간 상관 분석용)
    - task       : 발생 단계 (예: "Wikipedia 크롤링", "Gemini 생성")
    - major      : 처리 중이던 학과명
    - topic      : 검색 키워드 / 주제
    - model      : Gemini 모델명 (해당 시)
    - url        : 요청 URL (해당 시)
    - error_type : 예외 클래스명 (패턴 분류용)
    - error_msg  : 예외 메시지 (500자 한도)
    - traceback  : 전체 스택트레이스 (2000자 한도, LLM 디버깅용)
    """
    os.makedirs(str(_크롤러_에러_로그.parent), exist_ok=True)
    항목: dict = {
        "ts":         datetime.now().isoformat(timespec="seconds"),
        "task":       task,
        "major":      major or "N/A",
        "topic":      topic or "",
        "model":      model or "",
        "url":        url[:200] if url else "",
        "error_type": type(error).__name__,
        "error_msg":  str(error)[:500],
        "traceback":  _tb.format_exc()[:2000],
    }
    if extra:
        항목.update(extra)
    try:
        with open(_크롤러_에러_로그, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(항목, ensure_ascii=False) + "\n")
        logger.debug(f"[에러로그] crawler_errors.log 기록: {type(error).__name__}")
    except Exception:
        pass  # 로그 실패는 무시


# ─────────────────────────────────────────────────────────────
# 1단계: Wikipedia 공개 REST API 크롤러
# ─────────────────────────────────────────────────────────────

# 학과별 Wikipedia 주제 맵 (기존 5 + 크롤러 대상 7 추가)
_학과_주제_맵: dict[str, tuple[str, list[str]]] = {
    "물리학과":      ("물리학Ⅱ",   ["양자역학",    "상대성이론",  "통계역학"]),
    "컴퓨터공학부":  ("정보",       ["알고리즘",    "기계학습",    "운영체제"]),
    "의과대학":      ("생명과학Ⅱ", ["유전학",      "면역학",      "세포생물학"]),
    "경영대학":      ("경제",       ["게임이론",    "행동경제학",  "미시경제학"]),
    "기계공학부":    ("물리학Ⅰ",   ["유체역학",    "열역학",      "재료역학"]),
    # 추가 (주기적 크롤러 대상)
    "환경공학과":    ("과학",       ["환경공학",    "수질오염",    "대기오염"]),
    "전자공학과":    ("물리학Ⅱ",   ["반도체",      "트랜지스터",  "디지털회로"]),
    "생명과학과":    ("생명과학Ⅱ", ["세포생물학",  "유전공학",    "생태계"]),
    "화학공학과":    ("화학Ⅱ",     ["고분자화학",  "화학반응",    "촉매반응"]),
    "경제학과":      ("경제",       ["거시경제학",  "통화정책",    "국제경제"]),
    "심리학과":      ("통합사회",   ["인지심리학",  "사회심리학",  "발달심리학"]),
    "정치외교학과":  ("통합사회",   ["국제관계론",  "외교정책",    "정치학"]),
}

# 기본 크롤링 대상 (인수 없을 때 전체 5개 학과)
_기본_위키_주제 = [
    (_학과, _과목, _주제들)
    for _학과, (_과목, _주제들) in _학과_주제_맵.items()
    if _학과 in ("물리학과", "컴퓨터공학부", "의과대학", "경영대학", "기계공학부")
]

_위키_API = "https://ko.wikipedia.org/api/rest_v1/page/summary/{}"
_헤더 = {"User-Agent": "UnivAgent-Seeder/3.0 (educational; open source)",
          "Accept-Language": "ko-KR"}


def _위키_세특_변환(학과: str, 과목: str, 주제: str, 요약: str) -> str:
    요약_정제 = re.sub(r"\s+", " ", 요약).strip()[:300]
    도입 = random.choice([
        f"{학과} 지망생으로서",
        "이 학생은",
        "수업 중 자기주도적 탐구를 통해",
        "관련 분야에 높은 지적 호기심을 발휘하여",
    ])
    탐구 = random.choice([
        f"'{주제}'에 관한 심층 탐구 보고서를 작성하여",
        f"'{주제}'의 핵심 원리를 독자적으로 연구하고",
        f"'{주제}'을 주제로 학문적 탐구를 수행한 후",
    ])
    결론 = random.choice([
        f"이를 {과목} 교과 내용과 연계하여 발표함으로써 학문적 탐구 역량을 입증하였으며, {학과} 진학 적합성이 매우 높음.",
        f"{과목} 심화 학습을 전개하였고, 논리적 사고력과 분석력이 최상위 수준임.",
        f"학급 세미나에서 발표하여 동료 학생들의 이해를 도왔으며, 탁월한 학문적 잠재력을 보임.",
    ])
    return f"{도입} {탐구} 다음과 같이 탐구함: {요약_정제} {결론}"


def 위키피디아_크롤링(특정_학과: str | None = None) -> list[dict]:
    """
    특정_학과가 None이면 기본 5개 학과 전체를 크롤링합니다.
    특정_학과가 지정되면 해당 학과만 크롤링합니다.
    """
    if 특정_학과:
        if 특정_학과 in _학과_주제_맵:
            과목, 주제들 = _학과_주제_맵[특정_학과]
            크롤링_대상 = [(특정_학과, 과목, 주제들)]
        else:
            # 맵에 없는 학과 → 학과명 자체를 Wikipedia 검색어로 사용
            크롤링_대상 = [(특정_학과, "통합", [특정_학과])]
        logger.info(f"[크롤러] Wikipedia 크롤링 시작 (학과 지정: {특정_학과})...")
    else:
        크롤링_대상 = _기본_위키_주제
        logger.info("[크롤러] Wikipedia 공개 API 크롤링 시작...")

    결과: list[dict] = []

    for 학과, 과목, 주제_목록 in 크롤링_대상:
        for 주제 in 주제_목록:
            url = _위키_API.format(requests.utils.quote(주제))
            try:
                resp = requests.get(url, headers=_헤더, timeout=5)
                if resp.status_code == 404:
                    logger.warning(f"  [위키] '{주제}' 항목 없음")
                    continue
                resp.raise_for_status()
                요약 = resp.json().get("extract", "").strip()
                if len(요약) < 50:
                    continue
                세특 = _위키_세특_변환(학과, 과목, 주제, 요약)
                결과.append({"major_category": 학과, "subject": 과목,
                              "raw_text": 세특, "source": "crawler"})
                logger.info(f"  ✅ [{학과}] '{주제}' 크롤링 성공 ({len(세특)}자)")
                time.sleep(random.uniform(0.3, 0.7))
            except requests.exceptions.Timeout as e:
                logger.warning(f"  [타임아웃] '{주제}'")
                _크롤러_에러_기록(
                    "Wikipedia 크롤링", e,
                    major=학과, topic=주제, url=url,
                )
            except Exception as e:
                logger.warning(f"  [오류] '{주제}': {e}")
                _크롤러_에러_기록(
                    "Wikipedia 크롤링", e,
                    major=학과, topic=주제, url=url,
                )

    logger.info(f"[크롤러] 완료: {len(결과)}건")
    return 결과


# ─────────────────────────────────────────────────────────────
# 2단계: Gemini 합성 데이터 (학과별 즉시 저장, 빠른 모델 전환)
# ─────────────────────────────────────────────────────────────

_합성_계획 = [
    {"major_category": "물리학과",      "subject": "물리학Ⅱ",   "count": 4},
    {"major_category": "컴퓨터공학부",  "subject": "정보",       "count": 4},
    {"major_category": "의과대학",      "subject": "생명과학Ⅱ", "count": 4},
    {"major_category": "경영대학",      "subject": "경제",       "count": 4},
    {"major_category": "기계공학부",    "subject": "물리학Ⅰ",   "count": 4},
]

_GEMINI_모델_순서 = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

_MAX_429_RETRIES  = 3   # 동일 모델 최대 재시도 횟수
_QUOTA_SLEEP_S    = 60  # 1분 쿼터 리셋 대기 (최소 슬립)
_SUCCESS_PACE_S   = 10  # 성공 후 RPM 페이싱 대기 (분당 요청 수 제한 방지)

# NOTE: 이 스크립트는 telegram_agent.py 에서 asyncio.create_subprocess_exec 으로
# 별도 프로세스로 실행됩니다. 따라서 time.sleep() 호출이 봇 이벤트 루프를 절대
# 블로킹하지 않습니다. 비동기 환경에서 직접 임포트될 경우를 대비한 asyncio.sleep
# 사용은 불필요합니다.


class QuotaExhaustedError(RuntimeError):
    """
    모든 Gemini 모델의 할당량이 소진됐을 때 발생합니다.

    _Gemini_학과_생성 에서 raise → Gemini_합성_데이터_생성 에서 catch →
    현재 사이클을 즉시 중단하고 누적 저장 건수를 반환합니다.
    봇의 메인 루프는 절대 블로킹되지 않습니다.
    """


def _retryDelay_파싱(err_str: str) -> int:
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+)s", err_str)
    if m:
        return int(m.group(1))
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
    if m:
        return int(float(m.group(1))) + 1
    return 0


def _Gemini_학과_생성(major_category: str, subject: str, count: int,
                      남은_모델: list[str]) -> tuple[list[dict], list[str]]:
    """
    단일 학과에 대해 세특을 생성합니다 (크롤링 전용: Groq → Ollama).

    남은_모델 파라미터는 하위 호환성 유지를 위해 보존하나 내부 라우팅은
    token_manager force_engine='crawl' 이 담당합니다.
    """
    프롬프트 = (
        f"당신은 서울대학교 입학처 수석 컨설턴트입니다.\n"
        f"아래 조건에 맞는 100% 실제 서울대 합격생 수준의 세부능력 및 특기사항(세특) 사례를 "
        f"{count}개 작성하세요.\n\n"
        f"조건: 학과={major_category}, 과목={subject}, 수준=상위 1% 고등학생\n"
        f"문체: 교사 서술 형식(3인칭, 객관적, 학문적), 길이: 250~350자(한국어 기준)\n"
        f"필수요소: (1)심화 탐구 주제 (2)방법론 (3)학문적 개념·이론명 (영문 병기 허용) "
        f"(4)한계점·후속 연구 방향 (5){major_category} 진학 적합성\n\n"
        f"출력: 아래 JSON 배열만, 코드블록(```) 없이\n"
        f'[{{"major_category":"{major_category}","subject":"{subject}",'
        f'"raw_text":"교사 서술 세특 전문..."}}]'
    )

    try:
        txt, engine = _tm.generate_text_sync(프롬프트, force_engine="crawl")
        if not txt:
            _msg = (
                f"Critical: Crawl LLM tiers (Groq/Ollama) exhausted. "
                f"Aborting current target major seeding. (major={major_category})"
            )
            logger.error(f"  [쿼터소진] {_msg}")
            _크롤러_에러_기록(
                "전체 티어 소진", QuotaExhaustedError(_msg),
                major=major_category,
                extra={"abort_reason": "all_tiers_exhausted"},
            )
            raise QuotaExhaustedError(_msg)

        txt = re.sub(r"```(?:json)?", "", txt).strip().rstrip("`").strip()
        s, e_pos = txt.find("["), txt.rfind("]") + 1
        if s < 0 or e_pos <= 0:
            logger.warning(f"  [TokenManager] JSON 배열 없음 — {major_category} 건너뜀")
            return [], 남은_모델

        parsed = json.loads(txt[s:e_pos])
        결과 = [
            {
                "major_category": str(it.get("major_category", major_category))[:100],
                "subject":        str(it.get("subject", subject))[:100],
                "raw_text":       str(it.get("raw_text", "")).strip()[:4000],
                "source":         f"crawl_{engine.split()[0].lower()}",
            }
            for it in parsed
            if isinstance(it, dict) and len(str(it.get("raw_text", ""))) >= 100
        ]
        logger.info(f"  ✅ [Crawl/{engine}] {major_category}: {len(결과)}건 생성")
        time.sleep(_SUCCESS_PACE_S)
        return 결과, 남은_모델

    except QuotaExhaustedError:
        raise
    except json.JSONDecodeError as je:
        _크롤러_에러_기록("LLM JSON 파싱", je, major=major_category)
        return [], 남은_모델
    except Exception as exc:
        _크롤러_에러_기록("LLM 생성 예외", exc, major=major_category)
        return [], 남은_모델




def _합성_계획_생성(특정_학과: str | None = None) -> list[dict]:
    """합성 생성 계획 목록을 반환합니다."""
    if 특정_학과:
        if 특정_학과 in _학과_주제_맵:
            과목, _ = _학과_주제_맵[특정_학과]
        else:
            과목 = "통합"
        return [{"major_category": 특정_학과, "subject": 과목, "count": 4}]
    return _합성_계획


def Gemini_합성_데이터_생성(특정_학과: str | None = None) -> int:
    """
    각 학과별로 즉시 DB에 저장하고 총 저장 건수를 반환합니다.

    QuotaExhaustedError 발생 시 현재까지 저장된 건수를 반환하며 조용히 종료합니다.
    봇의 메인 루프는 절대 블로킹되지 않습니다.
    """
    계획_목록 = _합성_계획_생성(특정_학과)
    목표_건수 = sum(c["count"] for c in 계획_목록)
    logger.info(f"[Crawl] 합성 세특 생성 시작 (목표 {목표_건수}건, Groq→Ollama)...")
    남은_모델 = list(_GEMINI_모델_순서)
    총_저장 = 0

    for 계획 in 계획_목록:
        if not 남은_모델:
            # _Gemini_학과_생성 이 QuotaExhaustedError 를 먼저 raise 하므로
            # 정상 흐름에서는 도달하지 않지만 방어 코드로 유지
            logger.warning(f"  [Crawl] 사용 가능 LLM 없음 — {계획['major_category']} 건너뜀")
            break

        try:
            레코드들, 남은_모델 = _Gemini_학과_생성(
                major_category=계획["major_category"],
                subject=계획["subject"],
                count=계획["count"],
                남은_모델=남은_모델,
            )
        except QuotaExhaustedError as qe:
            # 일일 할당량 완전 소진 → 현재 사이클 즉시 중단
            logger.error(
                f"[Crawl] Groq/Ollama 소진 — 현재 사이클 중단 "
                f"(저장 완료: {총_저장}건 / 목표: {목표_건수}건)"
            )
            logger.error(f"  ↳ {qe}")
            # crawler_errors.log 에는 _Gemini_학과_생성 내부에서 이미 기록됨
            break

        # 생성 즉시 DB 저장 (프로세스 중단 시 손실 방지)
        저장수 = DB_삽입(레코드들)
        총_저장 += 저장수
        if 저장수:
            logger.info(f"  💾 [{계획['major_category']}] {저장수}건 즉시 저장")

    logger.info(f"[Crawl] 합성 완료: {총_저장}건 DB 저장")
    return 총_저장


# ─────────────────────────────────────────────────────────────
# 3단계: 내장 고품질 세특 폴백 (20건)
# ─────────────────────────────────────────────────────────────

_내장_세특 = [
    # ── 물리학과 ──────────────────────────────────────────────
    {
        "major_category": "물리학과", "subject": "물리학Ⅱ", "source": "builtin",
        "raw_text": (
            "파동함수의 확률적 해석(Born interpretation)에 흥미를 느껴 슈뢰딩거 방정식을 자기주도적으로 학습함. "
            "무한 사각 우물 모형(Infinite Square Well)에서 에너지 고유값을 직접 유도하고, 양자 터널링(Quantum Tunneling) "
            "현상을 WKB 근사법으로 분석한 탐구 보고서를 제출함. 양자 컴퓨터의 기본 원리인 큐비트(qubit)의 중첩 상태와 "
            "얽힘(entanglement)이 기존 고전 컴퓨팅과 다른 계산 복잡도를 갖는다는 점을 명확히 서술하였으며, 측정 문제의 "
            "철학적 함의(코펜하겐 해석 vs 다세계 해석)에 대한 비판적 분석도 포함함. 물리학과 진학 후 응집물질물리 분야 "
            "연구를 지향하는 명확한 목표와 탁월한 수식 전개 능력을 보임."
        ),
    },
    {
        "major_category": "물리학과", "subject": "물리학Ⅱ", "source": "builtin",
        "raw_text": (
            "특수 상대성이론(Special Relativity)의 로런츠 변환(Lorentz Transformation)을 교과 범위를 넘어 독자적으로 "
            "유도하고, 시공간 도표(Minkowski Diagram)를 직접 작성하여 동시성의 상대성과 쌍둥이 역설(Twin Paradox)을 "
            "시각적으로 분석함. GPS 위성 시스템에서 일반 상대론적 효과와 특수 상대론적 효과가 반대 방향으로 작용하며 "
            "시계 보정이 필요하다는 실질적 응용 사례를 조사하고, 보정값(+45.9μs/day − 7.2μs/day)을 계산하여 발표함. "
            "블랙홀 사건 지평선 근처에서의 시간 지연 효과를 슈바르츠실트 계량(Schwarzschild metric)으로 서술하며, "
            "중력파 검출(LIGO) 원리를 이해 수준 이상으로 탐구한 매우 뛰어난 학생임."
        ),
    },
    {
        "major_category": "물리학과", "subject": "물리학Ⅱ", "source": "builtin",
        "raw_text": (
            "통계역학(Statistical Mechanics)의 볼츠만 엔트로피(Boltzmann Entropy) S = k_B ln Ω 를 미시 상태 수로부터 "
            "직접 유도하고, 이상기체의 맥스웰-볼츠만 속도 분포(Maxwell–Boltzmann Distribution)를 도출하여 온도와 분자 "
            "평균 속력의 관계를 수학적으로 증명함. 반데르발스 방정식(Van der Waals Equation)과 이상기체 방정식의 차이를 "
            "임계점(critical point) 근방에서 비교 분석하였으며, 상전이(Phase Transition)의 란다우 이론을 질서 변수 "
            "(order parameter) 개념으로 간략히 소개한 탐구 보고서를 작성함. 열역학 제2법칙의 통계적 기원을 명확히 "
            "설명하는 논리력이 돋보이며, 물리학과 진학 적성이 최우수로 판단됨."
        ),
    },
    {
        "major_category": "물리학과", "subject": "물리학Ⅱ", "source": "builtin",
        "raw_text": (
            "회절(Diffraction)과 간섭(Interference) 현상을 이중슬릿 실험으로 직접 진행하고, 파장-격자 간격-밝은 무늬 "
            "간격 간의 정량적 관계를 유도하여 레이저 파장을 1.2% 오차 내로 측정함. 단일 슬릿 회절 패턴의 포락선이 "
            "이중 슬릿 간섭 패턴을 조절한다는 사실을 푸리에 변환(Fourier Transform) 관점에서 설명하였으며, 홀로그래피 "
            "(Holography)의 원리가 파면 복원에 기반한다는 점을 시각 자료를 제작하여 발표함. 실험 계획 수립부터 데이터 "
            "분석·오차 처리까지 독립적으로 수행한 탁월한 실험 능력을 보유하며 물리학 연구자 잠재력이 매우 뛰어남."
        ),
    },
    # ── 컴퓨터공학부 ─────────────────────────────────────────
    {
        "major_category": "컴퓨터공학부", "subject": "정보", "source": "builtin",
        "raw_text": (
            "정렬 알고리즘의 시간복잡도(Time Complexity) 비교 분석 프로젝트에서 버블정렬 O(n²), 병합정렬 O(n log n), "
            "팀소트(Timsort)를 Python으로 구현하여 벤치마킹함. 이론적 빅오 표기법이 상수 인자(constant factor)와 "
            "캐시 효율성(cache locality)에 의해 실제 성능과 괴리될 수 있음을 실증하고, NP-완전 문제인 외판원 문제(TSP)를 "
            "동적 계획법(Dynamic Programming)으로 해결하는 Held–Karp 알고리즘을 직접 구현하여 휴리스틱 근사(2-opt)와 "
            "성능 비교 분석한 보고서를 제출함. 알고리즘 분석 능력과 코드 구현력이 대학원 수준에 근접하는 탁월한 학생임."
        ),
    },
    {
        "major_category": "컴퓨터공학부", "subject": "정보", "source": "builtin",
        "raw_text": (
            "합성곱 신경망(CNN)의 역전파(Backpropagation) 알고리즘 수식을 직접 유도하고, PyTorch를 활용하여 MNIST 손글씨 "
            "분류 모델을 구현하여 98.7% 정확도를 달성함. 합성곱 층에서 필터가 특징 맵(feature map)을 추출하는 원리를 "
            "Grad-CAM으로 시각화하였으며, 과적합(Overfitting) 방지를 위한 드롭아웃(Dropout)과 배치 정규화(Batch "
            "Normalization)의 수학적 원리를 비교 분석하여 학급 AI 세미나에서 발표함. 트랜스포머(Transformer)의 "
            "셀프-어텐션(Self-Attention) 메커니즘을 행렬 연산으로 서술한 추가 탐구 보고서도 제출하며 AI·ML 역량이 탁월함."
        ),
    },
    {
        "major_category": "컴퓨터공학부", "subject": "정보", "source": "builtin",
        "raw_text": (
            "운영체제(OS) 프로세스 스케줄링 알고리즘 비교 탐구에서 라운드 로빈(Round Robin), SRTF, 다단계 피드백 큐를 "
            "Python 시뮬레이터로 구현하고, 평균 대기 시간(AWT)·반환 시간(ATT)·기아 현상(Starvation)을 비교하여 리눅스 "
            "커널이 CFS(Completely Fair Scheduler)를 채택한 이유를 레드-블랙 트리(Red-Black Tree)와 vruntime 개념으로 "
            "설명함. 페이지 교체 알고리즘(LRU, CLOCK, Optimal) 성능 비교 실험도 병행하였으며, 메모리 계층 구조에 대한 "
            "깊이 있는 이해를 바탕으로 한 탁월한 보고서를 제출함."
        ),
    },
    {
        "major_category": "컴퓨터공학부", "subject": "정보", "source": "builtin",
        "raw_text": (
            "RSA 공개키 암호화의 수학적 기반인 오일러 피 함수(Euler's Totient Function)와 중국인의 나머지 정리(CRT)를 "
            "직접 유도하고, Python으로 2048비트 RSA 암복호화를 구현함. 소인수분해 문제의 계산 복잡도가 RSA 안전성의 "
            "근거임을 P vs NP 문제와 연계하여 설명하고, 쇼어 알고리즘(Shor's Algorithm)이 RSA를 깰 수 있음을 증명하며 "
            "격자 기반 암호(CRYSTALS-Kyber)를 차세대 표준으로 제안한 보고서를 작성함. 수학과 컴퓨터과학을 융합하는 "
            "탐구 역량이 매우 뛰어나며 정보보안 분야 연구자로서의 자질이 충분히 검증됨."
        ),
    },
    # ── 의과대학 ──────────────────────────────────────────────
    {
        "major_category": "의과대학", "subject": "생명과학Ⅱ", "source": "builtin",
        "raw_text": (
            "CRISPR-Cas9 시스템의 분자생물학적 원리를 세포 내 DNA 이중가닥 절단(DSB)과 비상동 말단 결합(NHEJ) 및 "
            "상동 재조합(HDR) 수복 경로로 설명하고, 유전성 망막 이영양증 치료 임상 시험 사례를 분석하여 발표함. "
            "오프 타겟 편집(off-target editing) 위험성을 전장유전체 서열분석(WGS)으로 평가하는 방법론을 조사하고, "
            "생식세포(germline) 편집의 윤리적 문제를 헬싱키 선언과 ISSCR 가이드라인을 인용하여 다각도로 분석함. "
            "기초과학 지식과 생명윤리에 대한 균형 잡힌 시각이 돋보이며 의학 연구자로서의 비판적 사고 능력이 탁월함."
        ),
    },
    {
        "major_category": "의과대학", "subject": "생명과학Ⅱ", "source": "builtin",
        "raw_text": (
            "면역관문 억제제(Immune Checkpoint Inhibitor)의 항암 기전을 PD-1/PD-L1 신호 경로 차단과 T세포 활성화 회복으로 "
            "설명하고, 니볼루맙과 펨브롤리주맙의 임상 3상 데이터를 비교 분석하여 비소세포 폐암 전체 생존율(OS) 개선 효과를 "
            "정량적으로 제시함. 종양 미세환경(TME) 내 조절 T세포(Treg)와 종양 관련 대식세포(TAM)의 면역 억제 기전을 탐구하고, "
            "CAR-T 세포 치료와 병용 요법의 시너지 가능성을 최신 리뷰 논문을 인용하여 논의함. 종양 면역학 연구에 대한 "
            "명확한 학문적 방향성을 가진 탁월한 학생임."
        ),
    },
    {
        "major_category": "의과대학", "subject": "생명과학Ⅱ", "source": "builtin",
        "raw_text": (
            "신경가소성(Neuroplasticity)과 장기 강화(LTP)의 분자 기전을 AMPA 수용체 트래피킹과 NMDA 수용체의 코인시던스 "
            "검출 기능으로 탐구하고, 알츠하이머병에서 아밀로이드 베타 플라크와 타우 단백질 과인산화가 시냅스 기능 저하를 "
            "유발하는 기전을 최신 PET 연구와 연계하여 분석함. 기억의 신경생물학적 기반을 해마(Hippocampus) 회로 모형으로 "
            "설명하고, 공간 기억에서 격자 세포(Grid Cells)와 장소 세포(Place Cells)의 역할까지 탐구한 보고서를 제출함. "
            "신경과학과 의학의 교차점에 대한 깊이 있는 탐구로 의학자로서의 연구 잠재력이 높게 평가됨."
        ),
    },
    {
        "major_category": "의과대학", "subject": "생명과학Ⅱ", "source": "builtin",
        "raw_text": (
            "mRNA 백신(BNT162b2)의 작용 기전을 리포솜 나노입자(LNP) 전달 시스템, 5' 캡 구조, 유사우리딘(N1-methylpseudouridine) "
            "치환을 통한 면역 회피 기전까지 분자 수준으로 탐구함. 세포성 면역(CTL 활성화)과 체액성 면역(중화항체 생성)의 "
            "상보적 역할을 비교하고, 오미크론 변이의 RBD 변이(E484A, K417N)로 인한 중화능 감소 원인을 분석함. "
            "백신 효능 지속성과 부스터 전략에 관한 임상 데이터를 Kaplan-Meier 곡선으로 해석하는 과정에서 통계적 "
            "방법론까지 겸비한 매우 우수한 역량을 보임."
        ),
    },
    # ── 경영대학 ──────────────────────────────────────────────
    {
        "major_category": "경영대학", "subject": "경제", "source": "builtin",
        "raw_text": (
            "내쉬 균형(Nash Equilibrium) 개념을 이해하고 죄수의 딜레마, 치킨 게임, 스태그헌트(Stag Hunt) 등에서 균형 해를 "
            "계산하여 과점 시장의 기업 전략을 분석함. 반복 게임(Repeated Game)에서 민간 처벌(folk theorem) 조건 하에 "
            "협력 균형이 성립할 수 있음을 역진귀납법으로 증명하고, 한국 통신 3사 과점 구조를 용의자 딜레마로 모델링함. "
            "경매 이론(Auction Theory)에서 영국식 경매와 비크리 경매(Vickrey Auction)의 수익 동등성 정리(Revenue "
            "Equivalence Theorem)를 증명하여 제출한 보고서는 학문적 완성도가 매우 높게 평가됨."
        ),
    },
    {
        "major_category": "경영대학", "subject": "경제", "source": "builtin",
        "raw_text": (
            "행동경제학(Behavioral Economics)의 전망이론(Prospect Theory)을 통해 손실 회피 성향(Loss Aversion)이 "
            "기대효용이론(Expected Utility Theory)과 괴리되는 방식을 카너먼·트버스키 연구로 분석하고, 앵커링 효과와 "
            "가용성 편향(Availability Heuristic)이 주식 시장 과잉 반응을 유발하는 매커니즘을 실증 데이터로 제시함. "
            "넛지(Nudge) 정책의 한국 적용 사례를 연금 자동 가입 디폴트 변경 효과로 분석하고, 자유주의적 간섭주의 "
            "(Libertarian Paternalism)의 윤리적 쟁점을 탐구함. 행동공공정책 분야에 대한 깊은 이해를 입증함."
        ),
    },
    {
        "major_category": "경영대학", "subject": "경제", "source": "builtin",
        "raw_text": (
            "삼성전자와 TSMC 재무제표(손익계산서·대차대조표·현금흐름표)를 5개년 비교 분석하고, ROE 듀폰 분해 "
            "(DuPont Analysis: ROE = 순이익률 × 자산회전율 × 재무레버리지)로 수익성 차이의 구조적 원인을 파악함. "
            "반도체 산업의 CAPEX/매출 비율이 WACC(가중평균자본비용)에 미치는 영향을 분석하고, DCF 모형으로 양사의 "
            "내재가치를 추정하여 시장가격 대비 할인/프리미엄 여부를 판단한 투자 의견 보고서를 작성함. 회계·재무이론·"
            "산업분석을 통합하는 역량이 매우 뛰어나며 경영 분야 연구자로서의 자질이 검증됨."
        ),
    },
    {
        "major_category": "경영대학", "subject": "경제", "source": "builtin",
        "raw_text": (
            "플랫폼 경제(Platform Economy)에서 네트워크 외부성(Network Externality)의 직접·간접 효과가 승자독식 시장 구조를 "
            "형성하는 메커니즘을 이론적으로 분석하고, 카카오톡·유튜브의 멀티 사이디드 마켓 구조를 록인 효과(Lock-in)와 "
            "전환 비용(Switching Cost) 관점에서 탐구함. EU 디지털시장법(DMA)과 미국 플랫폼 경쟁법안을 비교하고, "
            "데이터 독점(Data Monopoly)이 진입 장벽 형성에 미치는 영향을 실증 연구로 분석함. 경제 이론과 현실 정책을 "
            "연계하는 사고력이 탁월하며 경영학·경제학 융합 역량이 뛰어남."
        ),
    },
    # ── 기계공학부 ────────────────────────────────────────────
    {
        "major_category": "기계공학부", "subject": "물리학Ⅰ", "source": "builtin",
        "raw_text": (
            "나비에-스토크스 방정식(Navier-Stokes Equations)의 물리적 의미를 선형 운동량 보존 법칙에서 유도하고, "
            "레이놀즈 수(Re = ρvL/μ)가 층류·난류를 구분하는 무차원수임을 실험으로 확인함. 비행기 날개의 양력 발생 "
            "원리를 쿠타-주코프스키 정리(Kutta–Joukowski Theorem)로 설명하고, OpenFOAM을 활용하여 NACA 에어포일의 "
            "압력 분포를 CFD 시뮬레이션으로 분석한 결과를 발표함. 단순 베르누이 방정식을 넘어 실제 점성 유동의 복잡성을 "
            "탐구하는 깊이 있는 학문적 태도가 인상적이며 항공우주 분야 진학 적합성이 매우 높음."
        ),
    },
    {
        "major_category": "기계공학부", "subject": "물리학Ⅰ", "source": "builtin",
        "raw_text": (
            "카르노 효율(η = 1 − T_C/T_H)이 이상 기관의 이론적 한계임을 엔트로피 증가 법칙으로 증명하고, 오토 사이클과 "
            "디젤 사이클의 열효율을 압축비(compression ratio)의 함수로 비교 분석함. 열전 발전기에서 제벡 효과(Seebeck "
            "Effect)를 활용한 폐열 회수 기술을 조사하고, ZT 값(무차원 성능 지수) 최적화 방향을 최신 소재 연구와 연계하여 "
            "탐구함. 열역학과 실공학 사이의 교량 역할을 하는 탐구 능력이 돋보이며, 에너지 시스템 공학자로서의 "
            "잠재력이 높게 평가됨."
        ),
    },
    {
        "major_category": "기계공학부", "subject": "물리학Ⅰ", "source": "builtin",
        "raw_text": (
            "응력-변형률(Stress-Strain) 선도를 분석하고, 후크의 법칙(Hooke's Law)이 탄성 한계 내에서만 성립하며 "
            "넥킹(necking) 이후 소성 변형(plastic deformation)에서 비선형 거동을 보임을 실험 데이터로 확인함. 보(Beam)의 "
            "휨(Bending) 해석에서 중립면(neutral axis)을 적용하여 최대 굽힘 응력 위치를 결정하고, I형강이 동일 무게 대비 "
            "높은 굽힘 저항을 갖는 원리를 단면 2차 모멘트(Second Moment of Area) 계산으로 증명함. FEM 소프트웨어 Abaqus로 "
            "브리지 구조 응력 분포 시뮬레이션을 수행하여 최적 설계 방향을 제안한 보고서를 제출함."
        ),
    },
    {
        "major_category": "기계공학부", "subject": "물리학Ⅰ", "source": "builtin",
        "raw_text": (
            "2자유도(2-DOF) 평면 로봇 팔의 순기구학(Forward Kinematics)을 데나빗-하텐베르크(DH) 표현법으로 유도하고, "
            "역기구학(Inverse Kinematics) 해를 기하학적 방법으로 도출함. 라그랑주 역학(Lagrangian Mechanics)으로 "
            "동역학 방정식을 수립하고, PID 제어기(Proportional-Integral-Derivative Controller)를 설계하여 Arduino 기반 "
            "실제 로봇 팔로 목표 궤적 추적 성능을 검증함. 이론 역학·제어공학·하드웨어 구현을 통합하는 역량이 공과대학 "
            "최상위 수준이며 지능 로봇 연구에 높은 적합성을 보임."
        ),
    },
]  # 총 20건


def 내장_폴백_삽입(목표_건수: int = 20) -> int:
    """현재 DB의 gemini+builtin 건수를 확인하여 부족분만큼 내장 세특을 삽입합니다."""
    conn = sqlite3.connect(str(DB_경로))
    현재 = conn.execute(
        "SELECT COUNT(*) FROM successful_seteuks WHERE source IN ('gemini','builtin')"
    ).fetchone()[0]
    conn.close()

    부족 = max(0, 목표_건수 - 현재)
    if 부족 == 0:
        return 0

    logger.info(f"[폴백] 내장 세특 {부족}건 삽입 (현재 합성 {현재}건, 목표 {목표_건수}건)")
    삽입_목록 = _내장_세특[:부족]
    return DB_삽입(삽입_목록)


# ─────────────────────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────────────────────

def main():
    # ── CLI 인수 파싱 ─────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="서울대 합격생 세특 DB 시딩 에이전트 v3"
    )
    parser.add_argument(
        "--major", type=str, default=None,
        metavar="학과명",
        help="특정 학과만 크롤링·생성 (예: --major 환경공학과). "
             "미지정 시 기본 5개 학과 전체 처리.",
    )
    args = parser.parse_args()
    특정_학과: str | None = args.major

    print("=" * 62)
    if 특정_학과:
        print(f"  서울대 합격생 세특 DB 시딩 에이전트 v3 [학과: {특정_학과}]")
    else:
        print("  서울대 합격생 세특 DB 시딩 에이전트 v3")
    print(f"  실행 시각: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 62)

    DB_테이블_초기화()

    # ── 1단계: Wikipedia 크롤링 ──────────────────────────────
    모드_라벨 = f"학과 지정: {특정_학과}" if 특정_학과 else "전체 5개 학과"
    print(f"\n[1단계] Wikipedia 공개 REST API 크롤링 중... ({모드_라벨})")
    크롤링_결과 = 위키피디아_크롤링(특정_학과)
    크롤링_저장수 = DB_삽입(크롤링_결과)
    print(f"  → 크롤링 수집: {len(크롤링_결과)}건 / DB 저장: {크롤링_저장수}건")

    # ── 2단계: Gemini 합성 (학과별 즉시 저장) ────────────────
    목표_설명 = f"4건 ({특정_학과})" if 특정_학과 else "20건 (전체)"
    print(f"\n[2단계] Groq/Ollama 합성 데이터 생성 중 (목표 {목표_설명})...")
    합성_저장수 = Gemini_합성_데이터_생성(특정_학과)
    print(f"  → Crawl LLM 저장: {합성_저장수}건")

    # ── 3단계: 내장 폴백 (특정 학과 모드에서는 건너뜀) ──────────
    폴백_저장수 = 0
    if not 특정_학과:
        폴백_저장수 = 내장_폴백_삽입(목표_건수=20)
        if 폴백_저장수:
            print(f"\n[3단계] 내장 고품질 세특 폴백: {폴백_저장수}건 추가 저장")

    # ── 최종 집계 ────────────────────────────────────────────
    conn = sqlite3.connect(str(DB_경로))
    source_집계 = dict(conn.execute(
        "SELECT source, COUNT(*) FROM successful_seteuks GROUP BY source"
    ).fetchall())
    총_건수 = conn.execute("SELECT COUNT(*) FROM successful_seteuks").fetchone()[0]
    학과_분포 = conn.execute(
        "SELECT major_category, COUNT(*) FROM successful_seteuks "
        "GROUP BY major_category ORDER BY COUNT(*) DESC"
    ).fetchall()
    conn.close()

    print("\n" + "=" * 62)
    print("  ✅ DB 시딩 완료 — 최종 결과 요약")
    print("=" * 62)
    print(f"  🌐 실제 크롤링  (source='crawler') : {source_집계.get('crawler', 0):>4}건")
    print(f"  🤖 Crawl LLM 합성 (Groq/Ollama)  : {source_집계.get('llm_crawl', 0) + sum(v for k,v in source_집계.items() if str(k).startswith('crawl_')):>4}건")
    print(f"  📚 내장 폴백    (source='builtin') : {source_집계.get('builtin', 0):>4}건")
    print(f"  {'─'*43}")
    print(f"  🗄  successful_seteuks 누적 총계   : {총_건수:>4}건")
    print(f"  📁 DB 경로: {DB_경로}")
    print("=" * 62)
    print("\n  학과별 분포:")
    for 학과, 건수 in 학과_분포:
        print(f"    {학과:<20} {건수}건")

    return 총_건수


if __name__ == "__main__":
    try:
        main()
    except Exception as _main_err:
        _크롤러_에러_기록("main() 미처리 예외", _main_err)
        raise
