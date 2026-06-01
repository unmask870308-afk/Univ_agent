"""
system_tester.py — UnivAgent E2E 시스템 테스터 v1
==================================================
1. Gemini 로 합성 모의 학생 프로필 생성
2. RAG 파이프라인 시뮬레이션 (진단 리포트 초안)
3. 비평 에이전트 토론 (품질 개선 → 최종 리포트)
4. 총감독(Chief Director) JSON PASS/FAIL 판정
5. PASS → verified_golden_records 에 자동 주입
   (target_major 접두사: "[E2E-Synthetic]")
6. 모든 예외 → data/fix_error/test_errors.log (JSONL)
"""

import os
import sys
import json
import logging
import argparse
import traceback
import subprocess
import re
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# 의존성 자동 설치
# ─────────────────────────────────────────────────────────────

_REQUIRED = {
    "dotenv":       "python-dotenv",
    "google.genai": "google-genai",
    "groq":         "groq",
    "ollama":       "ollama",
}
for _mod, _pkg in _REQUIRED.items():
    try:
        __import__(_mod.split(".")[0])
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", _pkg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# 환경 설정
# ─────────────────────────────────────────────────────────────

프로젝트_루트 = Path(__file__).parent.parent
load_dotenv(프로젝트_루트 / ".env")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# db_manager import (scripts/ 디렉터리 경로 추가)
sys.path.insert(0, str(Path(__file__).parent))
import db_manager

# 전용 에러 로그 경로
_테스트_에러_로그 = 프로젝트_루트 / "data" / "fix_error" / "test_errors.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("system_tester")


# ─────────────────────────────────────────────────────────────
# 전용 에러 로거 (JSONL, LLM 최적화)
# ─────────────────────────────────────────────────────────────

def _테스트_에러_기록(
    error: Exception,
    단계: str,
    extra: dict | None = None,
) -> None:
    """
    E2E 테스트 예외를 test_errors.log 에 JSONL 한 줄로 기록합니다.

    형식 (LLM 디버깅 최적화):
      {"ts": ..., "task": "E2E Mock Test", "stage": ...,
       "error_type": ..., "error_msg": ..., "traceback": ...}
    """
    os.makedirs(str(_테스트_에러_로그.parent), exist_ok=True)
    항목: dict = {
        "ts":         datetime.now().isoformat(timespec="seconds"),
        "task":       "E2E Mock Test",
        "stage":      단계,
        "error_type": type(error).__name__,
        "error_msg":  str(error)[:500],
        "traceback":  traceback.format_exc()[:2000],
    }
    if extra:
        항목.update(extra)
    try:
        with open(_테스트_에러_로그, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(항목, ensure_ascii=False) + "\n")
    except Exception:
        pass
    logger.error(f"[테스트오류] 단계={단계} | {type(error).__name__}: {error}")


# ─────────────────────────────────────────────────────────────
# LLM 호출 헬퍼 — 3-Tier 라우터로 위임
# ─────────────────────────────────────────────────────────────

import token_manager as _tm

def _gemini_호출(
    프롬프트: str,
    모델: str = "gemini-2.5-flash-lite",
    temperature: float = 0.4,
    max_tokens: int = 2048,
) -> str:
    """3-Tier LLM 라우터를 통해 텍스트를 생성합니다 (Gemini→Groq→Ollama)."""
    result, _ = _tm.generate_text_sync(프롬프트)
    if not result:
        raise RuntimeError("모든 LLM 티어 실패 — .env의 API 키를 확인하세요")
    return result


def _json_파싱_안전(텍스트: str) -> dict:
    """
    JSON 블록을 안전하게 파싱합니다.
    우선순위: ```json 코드블록 → ``` 코드블록 → 중괄호 직접 추출
    어떤 방법도 실패하면 빈 dict 반환.
    """
    # 1. ```json 코드 블록
    m = re.search(r"```json\s*(\{.*?\})\s*```", 텍스트, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 2. ``` 코드 블록 (언어 없음)
    m = re.search(r"```\s*(\{.*?\})\s*```", 텍스트, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3. 가장 큰 중괄호 블록 직접 추출
    s_pos = 텍스트.find("{")
    e_pos = 텍스트.rfind("}") + 1
    if s_pos != -1 and e_pos > s_pos:
        try:
            return json.loads(텍스트[s_pos:e_pos])
        except json.JSONDecodeError:
            pass
    return {}


# ─────────────────────────────────────────────────────────────
# Step 1: 합성 모의 프로필 생성
# ─────────────────────────────────────────────────────────────

def _합성_프로필_생성(전공: str) -> dict:
    """
    Gemini 로 현실적인 한국 수험생 합성 프로필을 생성합니다.
    반환 키: 희망학과, 고교_유형, 내신, 모의고사, 등급_체계,
             재수여부, 세특, 관심_대학
    """
    프롬프트 = f"""당신은 한국 고등학교 교사입니다.
아래 희망 전공을 가진 가상의 실제 수험생 프로필을 현실적으로 생성하세요.
구체적인 수치와 세부 내용을 포함해 실제 학생부처럼 작성하세요.

희망 전공: {전공}

아래 JSON 형식으로만 출력하세요 (다른 텍스트 없이):
```json
{{
  "희망학과": "{전공}",
  "고교_유형": "일반고",
  "내신": "2.85등급",
  "모의고사": "국2 수3 영2 탐2",
  "등급_체계": "9등급",
  "재수여부": "해당없음",
  "세특": "3~5문장 구체적 세부특기사항 (전공 관련 핵심 키워드 반드시 포함)",
  "관심_대학": ["대학명1", "대학명2", "대학명3"]
}}
```"""

    응답 = _gemini_호출(프롬프트, temperature=0.6, max_tokens=800)
    프로필 = _json_파싱_안전(응답)

    # 필수 필드 누락 시 기본값 보완
    프로필.setdefault("희망학과", 전공)
    프로필.setdefault("고교_유형", "일반고")
    프로필.setdefault("내신", "3.2등급")
    프로필.setdefault("모의고사", "국3 수3 영3 탐3")
    프로필.setdefault("등급_체계", "9등급")
    프로필.setdefault("재수여부", "해당없음")
    if not 프로필.get("세특"):
        프로필["세특"] = f"{전공} 관련 활동을 통해 기초 역량을 함양함"
    if not isinstance(프로필.get("관심_대학"), list) or not 프로필["관심_대학"]:
        프로필["관심_대학"] = ["서울대학교", "연세대학교", "고려대학교"]

    logger.info(
        f"[Step1] 합성 프로필 생성 완료: 내신={프로필['내신']} / "
        f"모의={프로필['모의고사']} / 대학={프로필['관심_대학'][:2]}"
    )
    return 프로필


# ─────────────────────────────────────────────────────────────
# Step 2: RAG 파이프라인 시뮬레이션 (진단 리포트 초안)
# ─────────────────────────────────────────────────────────────

def _진단_리포트_생성(프로필: dict) -> str:
    """
    합성 프로필을 기반으로 4차원 입시 진단 리포트 초안을 생성합니다.
    (telegram_agent.py 의 진단에이전트와 동일한 페르소나)
    """
    희망학과  = 프로필.get("희망학과", "")
    내신      = 프로필.get("내신", "")
    모의고사  = 프로필.get("모의고사", "")
    세특      = 프로필.get("세특", "")
    관심_대학 = ", ".join(프로필.get("관심_대학", []))
    재수여부  = 프로필.get("재수여부", "해당없음")

    프롬프트 = f"""당신은 대한민국 대학 입시 전문 진단 에이전트입니다.
아래 학생 프로필을 분석하여 4차원 입시 진단 리포트 초안을 작성하세요.

[학생 프로필]
- 희망학과: {희망학과}
- 내신: {내신}
- 모의고사: {모의고사}
- 세특: {세특}
- 관심 대학: {관심_대학}
- 재수여부: {재수여부}

[출력 형식: 4차원 진단]

## Dimension 1 — GPA Gap (내신 격차)
(목표 대학별 내신 기준선 대비 현재 격차를 구체적 수치로 기술)

## Dimension 2 — SE-TEUK Gap (세특 키워드 격차)
(희망학과 필수/권장 키워드 보유·누락 현황)

## Dimension 3 — CSAT Risk (수능 최저 위험도)
(목표 대학 전형별 수능최저 충족 여부)

## Dimension 4 — 정시 경쟁력
(모의고사 점수 기반 정시 가능성 분석)

## 종합 전략 권고
(실행 가능한 3가지 구체적 행동 권고사항)

각 섹션을 3~5 불릿 포인트로 작성하되, 수치와 구체적 대학명을 반드시 포함하세요."""

    리포트 = _gemini_호출(
        프롬프트, 모델="gemini-2.5-flash-lite", temperature=0.3, max_tokens=1500,
    )
    if not 리포트:
        raise RuntimeError("진단 리포트 초안 생성 실패 — Gemini 응답이 비어있음")
    logger.info(f"[Step2] 진단 리포트 초안 생성 완료 ({len(리포트)}자)")
    return 리포트


# ─────────────────────────────────────────────────────────────
# Step 3: 비평 에이전트 토론 (최종 리포트 도출)
# ─────────────────────────────────────────────────────────────

def _비평_에이전트_개선(프로필: dict, 초안_리포트: str) -> str:
    """
    비평 에이전트가 초안을 4가지 규칙으로 검토·개선하여 최종 리포트를 반환합니다.
    """
    프롬프트 = f"""당신은 대한민국 최고의 입시 컨설팅 비평 에이전트입니다.
아래 진단 리포트 초안을 엄격히 검토하고, 문제를 수정한 개선된 최종 리포트를 작성하세요.

[검토 규칙]
A. 모호한 표현 → 구체적 수치(등급·퍼센트·인원)로 대체
B. 잘못된 내신/수능 기준 → 실제 기준으로 교정
C. 전공과 무관한 세특 언급 → 전공 연관성 강화
D. 추상적 권고 → 실행 가능한 구체적 행동으로 교체

[학생 프로필]
희망학과: {프로필.get('희망학과','')}, 내신: {프로필.get('내신','')}, 모의: {프로필.get('모의고사','')}

[초안 리포트]
{초안_리포트[:1500]}

위 규칙을 모두 적용한 개선된 최종 리포트를 동일한 4차원 구조로 작성하세요:"""

    최종_리포트 = _gemini_호출(
        프롬프트, 모델="gemini-2.5-flash-lite", temperature=0.2, max_tokens=1500,
    )
    if not 최종_리포트:
        logger.warning("[Step3] 비평 에이전트 응답 없음 — 초안을 최종본으로 사용")
        return 초안_리포트
    logger.info(f"[Step3] 비평 에이전트 개선 완료 ({len(최종_리포트)}자)")
    return 최종_리포트


# ─────────────────────────────────────────────────────────────
# Step 4: 총감독(Chief Director) PASS/FAIL JSON 판정
# ─────────────────────────────────────────────────────────────

def _총감독_판정(프로필: dict, 최종_리포트: str) -> dict:
    """
    Chief Director 가 전체 파이프라인 결과물을 심사하여 JSON 판정을 반환합니다.

    반환 dict 필수 키:
      status        : "PASS" | "FAIL"
      quality_score : 0~100 정수
      reason        : 판정 이유 문자열
      verdict       : 한 줄 종합 평가
    """
    프롬프트 = f"""당신은 대한민국 최고 대입 컨설팅 회사의 총감독(Chief Director)입니다.
아래 E2E AI 진단 파이프라인의 결과물을 4가지 기준으로 엄격하게 심사하여 JSON으로 판정하세요.

[심사 기준]
1. 수치 정확성 (25점): 내신·수능 등 구체적 수치가 포함되어 있는가?
2. 전공 연관성 (25점): 세특 분석이 희망학과와 밀접하게 연결되어 있는가?
3. 실행 가능성 (25점): 권고사항이 구체적이고 즉시 실행 가능한가?
4. 4차원 완성도 (25점): 4가지 차원 모두 충실히 분석되었는가?

품질 점수 60점 이상 → "PASS", 미만 → "FAIL"

[학생 프로필 요약]
희망학과: {프로필.get('희망학과','')}, 내신: {프로필.get('내신','')}, 모의: {프로필.get('모의고사','')}

[최종 진단 리포트]
{최종_리포트[:2000]}

아래 JSON 형식으로만 출력하세요:
```json
{{
  "status": "PASS",
  "quality_score": 75,
  "reason": "판정 이유 (2~3문장)",
  "verdict": "한 줄 종합 평가"
}}
```"""

    응답 = _gemini_호출(
        프롬프트, 모델="gemini-2.5-flash", temperature=0.1, max_tokens=512,
    )
    판정 = _json_파싱_안전(응답)

    # 필수 필드 기본값 보완
    판정.setdefault("status", "FAIL")
    판정.setdefault("quality_score", 0)
    판정.setdefault("reason", "판정 정보 없음")
    판정.setdefault("verdict", "")
    판정["status"] = str(판정["status"]).upper()

    # quality_score 정수 변환 및 PASS/FAIL 재검증
    try:
        score = int(판정["quality_score"])
    except (ValueError, TypeError):
        score = 0
    판정["quality_score"] = score

    if score < 60 and 판정["status"] == "PASS":
        판정["status"] = "FAIL"
        판정["reason"] = f"품질 점수 {score}점으로 기준(60점) 미달 — 자동 FAIL 처리"

    logger.info(
        f"[Step4] 총감독 판정: {판정['status']} "
        f"(점수={판정['quality_score']}, 이유={판정['reason'][:60]})"
    )
    return 판정


# ─────────────────────────────────────────────────────────────
# Step 5: PASS 시 DB 주입
# ─────────────────────────────────────────────────────────────

def _골든_레코드_주입(전공: str, 프로필: dict, 최종_리포트: str, 판정: dict) -> int:
    """
    총감독 PASS 판정 시 verified_golden_records 에 레코드를 주입합니다.
    target_major 에 "[E2E-Synthetic]" 접두사를 붙여 합성 데이터임을 명시합니다.
    """
    target_major = f"[E2E-Synthetic] {전공}"
    record_id = db_manager.save_golden_record(
        target_major=target_major,
        mock_profile=프로필,
        final_optimized_text=최종_리포트,
        director_verdict=판정,
        source="E2E-Synthetic",
    )
    logger.info(
        f"[Step5] 골든 레코드 주입 완료: id={record_id}, major={target_major}"
    )
    return record_id


# ─────────────────────────────────────────────────────────────
# 메인 E2E 파이프라인
# ─────────────────────────────────────────────────────────────

def E2E_테스트_실행(전공: str) -> dict:
    """
    E2E 테스트 전체 파이프라인을 실행합니다.
    모든 단계에서 발생하는 예외는 test_errors.log 에 기록됩니다.

    반환 dict:
      status        : "PASS" | "FAIL" | "ERROR"
      quality_score : 총감독 품질 점수 (ERROR 시 0)
      reason        : 판정 이유
      record_id     : DB 삽입 id (PASS + 주입 성공 시)
      전공          : 입력 전공명
    """
    logger.info("=" * 60)
    logger.info("  UnivAgent E2E 시스템 테스터")
    logger.info(f"  대상 전공: {전공}")
    logger.info("=" * 60)

    try:
        # ── Step 1: 합성 프로필 생성 ──────────────────────────
        logger.info("[Step1/5] 합성 모의 프로필 생성 중...")
        mock_profile = _합성_프로필_생성(전공)

        # ── Step 2: RAG 파이프라인 (초안 리포트) ──────────────
        logger.info("[Step2/5] 진단 리포트 초안 생성 중 (RAG 시뮬레이션)...")
        초안_리포트 = _진단_리포트_생성(mock_profile)

        # ── Step 3: 멀티에이전트 토론 (비평 개선) ─────────────
        logger.info("[Step3/5] 비평 에이전트 품질 개선 중...")
        최종_리포트 = _비평_에이전트_개선(mock_profile, 초안_리포트)

        # ── Step 4: 총감독 PASS/FAIL 판정 ─────────────────────
        logger.info("[Step4/5] 총감독(Chief Director) JSON 판정 중...")
        판정 = _총감독_판정(mock_profile, 최종_리포트)

        # ── Step 5: PASS → DB 주입 ─────────────────────────────
        record_id = 0
        if 판정.get("status") == "PASS":
            logger.info("[Step5/5] PASS 확인 — verified_golden_records 주입 중...")
            record_id = _골든_레코드_주입(전공, mock_profile, 최종_리포트, 판정)
            logger.info(
                f"✅ E2E 테스트 PASS — "
                f"record_id={record_id}, score={판정['quality_score']}"
            )
        else:
            logger.warning(
                f"[Step5/5] FAIL — DB 주입 건너뜀 "
                f"(score={판정['quality_score']}, reason={판정.get('reason','')})"
            )

        return {
            "status":        판정.get("status", "FAIL"),
            "quality_score": 판정.get("quality_score", 0),
            "reason":        판정.get("reason", ""),
            "verdict":       판정.get("verdict", ""),
            "record_id":     record_id,
            "전공":          전공,
        }

    except Exception as e:
        _테스트_에러_기록(e, f"E2E_테스트_실행", extra={"전공": 전공})
        return {
            "status":        "ERROR",
            "quality_score": 0,
            "reason":        str(e),
            "verdict":       "",
            "record_id":     0,
            "전공":          전공,
        }


# ─────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UnivAgent E2E 시스템 테스터")
    parser.add_argument(
        "--major", "-m",
        default="컴퓨터공학과",
        help="테스트 대상 전공 (기본: 컴퓨터공학과)",
    )
    args = parser.parse_args()

    db_manager.DB_초기화()

    결과 = E2E_테스트_실행(args.major)

    print(f"\n{'='*60}")
    print(f"  E2E 테스트 결과 : {결과['status']}")
    print(f"  전공            : {결과['전공']}")
    print(f"  품질 점수       : {결과.get('quality_score', 'N/A')}")
    if 결과.get("record_id"):
        print(f"  DB 레코드 ID    : {결과['record_id']}")
    if 결과.get("verdict"):
        print(f"  종합 평가       : {결과['verdict']}")
    if 결과.get("reason"):
        print(f"  판정 이유       : {결과['reason']}")
    if 결과.get("status") == "ERROR":
        print(f"  오류 상세       : {결과.get('reason','')}")
        print(f"  에러 로그       : {_테스트_에러_로그}")
    print(f"{'='*60}")

    sys.exit(0 if 결과["status"] == "PASS" else 1)
