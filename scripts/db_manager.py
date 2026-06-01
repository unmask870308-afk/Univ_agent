"""
UnivAgent SQLite 데이터베이스 관리자
암호화 지원 내장 (Fernet symmetric encryption)
"""

import sqlite3
import json
import os
import re
import logging
import subprocess
import sys
import threading
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

_프로젝트_루트 = Path(__file__).parent.parent
DB_경로  = _프로젝트_루트 / "data" / "admissions_agent.db"
ENV_경로 = _프로젝트_루트 / ".env"

# ─────────────────────────────────────────────────────────────
# 의존성 자동 설치
# ─────────────────────────────────────────────────────────────

def _의존성_확인():
    for 패키지, pip명 in [("cryptography", "cryptography"), ("pypdf", "pypdf")]:
        try:
            __import__(패키지)
        except ImportError:
            if 패키지 == "pypdf":
                try:
                    __import__("PyPDF2")
                    continue
                except ImportError:
                    pass
            logger.info(f"[DB] {pip명} 설치 중...")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pip명],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info(f"[DB] {pip명} 설치 완료")

_의존성_확인()

# ─────────────────────────────────────────────────────────────
# Fernet 암호화 키 관리
# ─────────────────────────────────────────────────────────────

_FERNET_키: bytes | None = None
_FERNET_잠금 = threading.Lock()


def _Fernet_키_로드() -> bytes:
    global _FERNET_키
    with _FERNET_잠금:
        if _FERNET_키:
            return _FERNET_키

        # 1. 환경변수 우선
        키_str = os.environ.get("FERNET_KEY", "").strip()
        if 키_str:
            _FERNET_키 = 키_str.encode()
            return _FERNET_키

        # 2. .env 파일 파싱
        if ENV_경로.exists():
            for 줄 in ENV_경로.read_text(encoding="utf-8").splitlines():
                if 줄.strip().startswith("FERNET_KEY="):
                    키_str = 줄.split("=", 1)[1].strip()
                    if 키_str:
                        os.environ["FERNET_KEY"] = 키_str
                        _FERNET_키 = 키_str.encode()
                        return _FERNET_키

        # 3. 신규 키 생성 후 .env 에 저장
        from cryptography.fernet import Fernet
        새_키 = Fernet.generate_key()
        새_키_str = 새_키.decode()
        os.environ["FERNET_KEY"] = 새_키_str
        try:
            기존 = ENV_경로.read_text(encoding="utf-8") if ENV_경로.exists() else ""
            if "FERNET_KEY=" not in 기존:
                with open(ENV_경로, "a", encoding="utf-8") as _f:
                    _f.write(f"\nFERNET_KEY={새_키_str}\n")
            logger.info("[DB] Fernet 암호화 키 신규 생성 → .env 저장 완료")
        except Exception as _e:
            logger.warning(f"[DB] .env 키 저장 실패 (환경변수에만 유지): {_e}")
        _FERNET_키 = 새_키
        return _FERNET_키


def 암호화(텍스트: str) -> str:
    """텍스트를 Fernet 대칭키로 암호화합니다. 빈 값은 그대로 반환."""
    if not 텍스트:
        return ""
    try:
        from cryptography.fernet import Fernet
        return Fernet(_Fernet_키_로드()).encrypt(텍스트.encode("utf-8")).decode("ascii")
    except Exception as _e:
        logger.warning(f"[DB] 암호화 실패 — 평문 저장: {_e}")
        return 텍스트


def 복호화(암호문: str) -> str:
    """Fernet 암호문을 복호화합니다. 실패 시 빈 문자열 반환."""
    if not 암호문:
        return ""
    try:
        from cryptography.fernet import Fernet
        return Fernet(_Fernet_키_로드()).decrypt(암호문.encode("ascii")).decode("utf-8")
    except Exception:
        # 마이그레이션 이전 평문 데이터는 그대로 반환
        return 암호문 if not 암호문.startswith("gAAAAA") else ""


# ─────────────────────────────────────────────────────────────
# 입력 소독 (보안 유틸리티)
# ─────────────────────────────────────────────────────────────

def 입력_소독(텍스트: str, 최대길이: int = 2000) -> str:
    """
    사용자 입력을 소독합니다.
    - 제어 문자 제거 (탭·줄바꿈 허용)
    - 경로 탐색 패턴 제거
    - NULL 바이트 제거
    """
    if not isinstance(텍스트, str):
        return ""
    텍스트 = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", 텍스트)
    텍스트 = 텍스트.replace("\x00", "")
    텍스트 = re.sub(r"\.\.[/\\]", "", 텍스트)
    return 텍스트[:최대길이]


def 파일명_안전(파일명: str) -> bool:
    """파일명 안전성을 검증합니다 (경로 탐색, 금지 문자 차단)."""
    if not 파일명 or len(파일명) > 255:
        return False
    return not any(c in 파일명 for c in ["..", "/", "\\", "\x00", "<", ">", "|", "?", "*"])


# ─────────────────────────────────────────────────────────────
# SQLite DDL
# ─────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS students (
    chat_id         INTEGER PRIMARY KEY,
    username        TEXT    NOT NULL DEFAULT '',
    full_name       TEXT    NOT NULL DEFAULT '',
    target_major    TEXT    NOT NULL DEFAULT '',
    school_type     TEXT    NOT NULL DEFAULT '',
    grade_raw       TEXT    NOT NULL DEFAULT '',
    mock_exam       TEXT    NOT NULL DEFAULT '',
    grade_trend     TEXT    NOT NULL DEFAULT '',
    elective_subj   TEXT    NOT NULL DEFAULT '',
    repeat_year     TEXT    NOT NULL DEFAULT '',
    grade_system    TEXT    NOT NULL DEFAULT '9등급',
    setech_enc      TEXT    NOT NULL DEFAULT '',
    subject_gpa_enc TEXT    NOT NULL DEFAULT '',
    school_rec_enc  TEXT    NOT NULL DEFAULT '',
    interest_univs  TEXT    NOT NULL DEFAULT '[]',
    grade_history   TEXT    NOT NULL DEFAULT '{}',
    question_hist   TEXT    NOT NULL DEFAULT '[]',
    created_at      TEXT    NOT NULL DEFAULT '',
    updated_at      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_students_uname ON students(username);

CREATE TABLE IF NOT EXISTS admissions_guide (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    university      TEXT    NOT NULL DEFAULT '',
    year            TEXT    NOT NULL DEFAULT '',
    doc_type        TEXT    NOT NULL DEFAULT '',
    source_file     TEXT    UNIQUE,
    entrance_types  TEXT    NOT NULL DEFAULT '[]',
    parse_method    TEXT    NOT NULL DEFAULT '',
    reliability     TEXT    NOT NULL DEFAULT '',
    parse_ts        TEXT    NOT NULL DEFAULT '',
    parse_failed    INTEGER NOT NULL DEFAULT 0,
    error_msg       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_guide_univ ON admissions_guide(university);
CREATE INDEX IF NOT EXISTS idx_guide_year ON admissions_guide(year);

CREATE TABLE IF NOT EXISTS user_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL DEFAULT 0,
    username     TEXT    NOT NULL DEFAULT '',
    full_name    TEXT    NOT NULL DEFAULT '',
    req_type     TEXT    NOT NULL DEFAULT '일반',
    content      TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT '접수',
    created_at   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS system_metrics (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    date_str             TEXT    NOT NULL UNIQUE,
    seteuk_count         INTEGER NOT NULL DEFAULT 0,
    univ_stats_count     INTEGER NOT NULL DEFAULT 0,
    golden_count         INTEGER NOT NULL DEFAULT 0,
    crawler_errors       INTEGER NOT NULL DEFAULT 0,
    error_count          INTEGER NOT NULL DEFAULT 0,
    shield_defenses      INTEGER NOT NULL DEFAULT 0,
    e2e_test_result      TEXT    NOT NULL DEFAULT '',
    total_tokens         INTEGER NOT NULL DEFAULT 0,
    gemini_daily_tokens  INTEGER NOT NULL DEFAULT 0,
    groq_daily_tokens    INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_metrics_date ON system_metrics(date_str);

CREATE TABLE IF NOT EXISTS admin_settings (
    setting_key   TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS verified_golden_records (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    target_major         TEXT    NOT NULL DEFAULT '',
    mock_profile         TEXT    NOT NULL DEFAULT '{}',
    final_optimized_text TEXT    NOT NULL DEFAULT '',
    source               TEXT    NOT NULL DEFAULT 'E2E-Synthetic',
    director_verdict     TEXT    NOT NULL DEFAULT '{}',
    quality_score        INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_golden_major ON verified_golden_records(target_major);

CREATE TABLE IF NOT EXISTS pending_verifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL DEFAULT 0,
    query           TEXT    NOT NULL DEFAULT '',
    ollama_answer   TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending',
    verified_answer TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_pv_status ON pending_verifications(status);
CREATE INDEX IF NOT EXISTS idx_pv_user   ON pending_verifications(user_id);

CREATE TABLE IF NOT EXISTS major_knowledge (
    major_name           TEXT PRIMARY KEY,
    curriculum           TEXT NOT NULL DEFAULT '',
    career_paths         TEXT NOT NULL DEFAULT '',
    employment_companies TEXT NOT NULL DEFAULT '',
    updated_at           TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_major_knowledge_name ON major_knowledge(major_name);

CREATE TABLE IF NOT EXISTS golden_dataset (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_query      TEXT    NOT NULL,
    ollama_response TEXT    NOT NULL DEFAULT '',
    gemini_response TEXT    NOT NULL DEFAULT '',
    source          TEXT    NOT NULL DEFAULT 'verified',
    quality_score   INTEGER NOT NULL DEFAULT 0,
    used_in_train   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_golden_qa_used   ON golden_dataset(used_in_train);
CREATE INDEX IF NOT EXISTS idx_golden_qa_score  ON golden_dataset(quality_score DESC);

CREATE TABLE IF NOT EXISTS simulator_quota (
    date            TEXT    NOT NULL PRIMARY KEY,   -- YYYY-MM-DD
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    runs_completed  INTEGER NOT NULL DEFAULT 0
);
"""

# 관리자 설정 기본값 (테이블 생성 직후 최초 1회만 삽입)
_ADMIN_SETTINGS_기본값 = {
    "report_time":        "06:00",   # 일일 리포트 전송 시각 (HH:MM)
    "report_last_sent":   "",        # 마지막으로 리포트가 전송된 날짜 (YYYY-MM-DD)
}

_연결: sqlite3.Connection | None = None
_연결_잠금 = threading.Lock()


def _system_metrics_마이그레이션(conn: sqlite3.Connection) -> None:
    """기존 system_metrics 테이블에 신규 컬럼을 추가합니다 (이미 존재하면 무시)."""
    새_컬럼들 = [
        ("shield_defenses",     "INTEGER NOT NULL DEFAULT 0"),
        ("e2e_test_result",     "TEXT    NOT NULL DEFAULT ''"),
        ("error_count",         "INTEGER NOT NULL DEFAULT 0"),
        ("gemini_daily_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("groq_daily_tokens",   "INTEGER NOT NULL DEFAULT 0"),
    ]
    for 컬럼명, 정의 in 새_컬럼들:
        try:
            conn.execute(f"ALTER TABLE system_metrics ADD COLUMN {컬럼명} {정의}")
            conn.commit()
            logger.info(f"[DB마이그레이션] system_metrics.{컬럼명} 컬럼 추가됨")
        except Exception:
            pass  # 이미 존재하는 컬럼이면 무시


def _golden_dataset_마이그레이션(conn: sqlite3.Connection) -> None:
    """golden_dataset 테이블에 시뮬레이터 컬럼을 추가합니다 (이미 존재하면 무시)."""
    new_cols = [
        ("fake_profile",   "TEXT NOT NULL DEFAULT ''"),
        ("ollama_draft",   "TEXT NOT NULL DEFAULT ''"),
        ("gemini_perfect", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col, definition in new_cols:
        try:
            conn.execute(f"ALTER TABLE golden_dataset ADD COLUMN {col} {definition}")
            conn.commit()
            logger.info(f"[DB마이그레이션] golden_dataset.{col} 컬럼 추가됨")
        except Exception:
            pass  # 이미 존재하는 컬럼이면 무시


def _students_마이그레이션(conn: sqlite3.Connection) -> None:
    """students 테이블에 신규 프로필 컬럼을 추가합니다 (이미 존재하면 무시)."""
    new_cols = [
        ("current_grade",   "TEXT NOT NULL DEFAULT ''"),
        ("highschool_type", "TEXT NOT NULL DEFAULT ''"),
        ("target_keywords", "TEXT NOT NULL DEFAULT ''"),
        ("csat_subjects",   "TEXT NOT NULL DEFAULT ''"),
    ]
    for col, definition in new_cols:
        try:
            conn.execute(f"ALTER TABLE students ADD COLUMN {col} {definition}")
            conn.commit()
            logger.info(f"[DB마이그레이션] students.{col} 컬럼 추가됨")
        except Exception:
            pass  # 이미 존재하는 컬럼이면 무시


def _외부_테이블_인덱스_생성(conn: sqlite3.Connection) -> None:
    """successful_seteuks, admissions_stats 인덱스 (테이블 존재 시에만)."""
    for 테이블, 컬럼, 인덱스명 in [
        ("successful_seteuks", "major_category", "idx_seteuks_major"),
        ("admissions_stats",   "university_name", "idx_stats_univ"),
        ("system_metrics",     "date_str",        "idx_metrics_date"),
    ]:
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {인덱스명} ON {테이블}({컬럼})"
            )
            conn.commit()
        except Exception:
            pass  # 테이블 미존재 시 무시


def optimize_database() -> None:
    """VACUUM + PRAGMA optimize — WAL 싱글턴과 별도 연결 필요."""
    try:
        with sqlite3.connect(str(DB_경로)) as _conn:
            _conn.isolation_level = None  # autocommit (VACUUM 요구)
            _conn.execute("VACUUM")
            _conn.execute("PRAGMA optimize")
        logger.info("[DB] optimize_database 완료")
    except Exception as _e:
        logger.warning(f"[DB] optimize_database 실패: {_e}")


def DB() -> sqlite3.Connection:
    """WAL 모드 스레드 안전 싱글턴 SQLite 연결을 반환합니다."""
    global _연결
    with _연결_잠금:
        if _연결 is None:
            DB_경로.parent.mkdir(parents=True, exist_ok=True)
            _연결 = sqlite3.connect(str(DB_경로), check_same_thread=False)
            _연결.row_factory = sqlite3.Row
            _연결.execute("PRAGMA journal_mode=WAL")
            _연결.execute("PRAGMA foreign_keys=ON")
            _연결.executescript(_DDL)
            _연결.commit()
            _system_metrics_마이그레이션(_연결)
            _golden_dataset_마이그레이션(_연결)
            _students_마이그레이션(_연결)
            _외부_테이블_인덱스_생성(_연결)
            logger.info(f"[DB] SQLite 초기화 완료: {DB_경로}")
    return _연결


def _admin_settings_기본값_초기화() -> None:
    """admin_settings 에 기본값이 없을 때만 삽입합니다 (최초 1회)."""
    conn = DB()
    for key, value in _ADMIN_SETTINGS_기본값.items():
        conn.execute(
            "INSERT OR IGNORE INTO admin_settings (setting_key, setting_value) VALUES (?,?)",
            (key, value),
        )
    conn.commit()


def DB_초기화():
    """데이터베이스를 초기화하고 기존 JSON 데이터를 마이그레이션합니다."""
    DB()
    _JSON_마이그레이션()
    _admin_settings_기본값_초기화()
    logger.info("[DB] 초기화 및 마이그레이션 완료")


def init_db() -> None:
    """
    Public English entry-point for DB schema initialisation.

    Schema summary (all DDL owned exclusively by this module):
      students(chat_id PK)          — user profiles; chat_id is the user key, NOT id
      admissions_guide(id PK)       — parsed university admission documents
      user_requests(id PK)          — user inquiry queue
      system_metrics(id PK)         — daily operational snapshots
      admin_settings(setting_key PK)— key/value config store
      verified_golden_records(id PK)— high-quality QA training examples
      pending_verifications(id PK)  — Ollama→Gemini async re-verification queue

    NOTE: there is no 'users' table.  The primary user table is 'students'
    with 'chat_id' (INTEGER PRIMARY KEY).  Any index on the user identifier
    must reference students(chat_id), NOT users(id).

    Called by telegram_agent.py at startup via:
        db_manager.init_db()
    """
    DB_초기화()


# ─────────────────────────────────────────────────────────────
# JSON → SQLite 마이그레이션 (최초 1회)
# ─────────────────────────────────────────────────────────────

def _JSON_마이그레이션():
    conn = DB()
    now = datetime.now().isoformat(timespec="seconds")

    # admissions_guide 마이그레이션
    if conn.execute("SELECT COUNT(*) FROM admissions_guide").fetchone()[0] == 0:
        json_경로 = _프로젝트_루트 / "data" / "student" / "parsed_admission_guide.json"
        if json_경로.exists():
            try:
                data = json.loads(json_경로.read_text(encoding="utf-8"))
                cnt = 0
                for 항목 in data.get("대학_목록", []):
                    try:
                        conn.execute("""
                            INSERT OR IGNORE INTO admissions_guide
                            (university, year, doc_type, source_file, entrance_types,
                             parse_method, reliability, parse_ts, parse_failed, error_msg)
                            VALUES (?,?,?,?,?,?,?,?,?,?)
                        """, (
                            항목.get("대학명", ""),
                            항목.get("학년도", ""),
                            항목.get("문서_유형", ""),
                            항목.get("_소스_파일", ""),
                            json.dumps(항목.get("수시_전형목록", []), ensure_ascii=False),
                            항목.get("_파싱_방식", ""),
                            항목.get("파싱_신뢰도", ""),
                            항목.get("_파싱_시각", now),
                            1 if 항목.get("파싱_실패") else 0,
                            항목.get("오류_메시지", ""),
                        ))
                        cnt += 1
                    except Exception:
                        pass
                conn.commit()
                logger.info(f"[DB마이그레이션] 입시데이터 {cnt}건 → admissions_guide")
            except Exception as _e:
                logger.warning(f"[DB마이그레이션] 입시데이터 실패: {_e}")

    # students 마이그레이션
    if conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0:
        json_경로 = _프로젝트_루트 / "data" / "student" / "user_profiles.json"
        if json_경로.exists():
            try:
                data = json.loads(json_경로.read_text(encoding="utf-8"))
                cnt = 0
                for uid, p in data.get("users", {}).items():
                    try:
                        chat_id = int(uid)
                        s = p.get("성적", {})
                        setech_raw   = s.get("세특", "")
                        sub_gpa_raw  = json.dumps(s.get("과목별_내신", {}), ensure_ascii=False)
                        rec_raw      = s.get("생기부_원문", "")
                        conn.execute("""
                            INSERT OR IGNORE INTO students
                            (chat_id, username, full_name, target_major, school_type,
                             grade_raw, mock_exam, grade_trend, elective_subj, repeat_year,
                             grade_system, setech_enc, subject_gpa_enc, school_rec_enc,
                             interest_univs, grade_history, question_hist,
                             created_at, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (
                            chat_id,
                            p.get("username", ""),
                            p.get("full_name", ""),
                            s.get("희망학과", ""),
                            s.get("고교_유형", ""),
                            s.get("내신", ""),
                            s.get("모의고사", ""),
                            s.get("성적_추이", ""),
                            s.get("선택과목", ""),
                            s.get("재수여부", ""),
                            s.get("등급_체계", "9등급"),
                            암호화(setech_raw),
                            암호화(sub_gpa_raw),
                            암호화(rec_raw),
                            json.dumps(p.get("관심_대학", []), ensure_ascii=False),
                            json.dumps(p.get("성적_이력", {}), ensure_ascii=False),
                            json.dumps(p.get("질문_이력", []), ensure_ascii=False),
                            p.get("최초_접속", now),
                            p.get("최근_접속", now),
                        ))
                        cnt += 1
                    except Exception:
                        pass
                conn.commit()
                logger.info(f"[DB마이그레이션] 학생 프로필 {cnt}명 → students")
            except Exception as _e:
                logger.warning(f"[DB마이그레이션] 프로필 실패: {_e}")


# ─────────────────────────────────────────────────────────────
# 학생 CRUD
# ─────────────────────────────────────────────────────────────

def 학생_조회(chat_id: int) -> sqlite3.Row | None:
    try:
        return DB().execute("SELECT * FROM students WHERE chat_id=?", (chat_id,)).fetchone()
    except Exception:
        return None


def 학생_업서트(chat_id: int, 필드: dict):
    """INSERT OR UPDATE 방식으로 학생 데이터를 저장합니다."""
    now = datetime.now().isoformat(timespec="seconds")
    row = 학생_조회(chat_id)
    기존 = dict(row) if row else {}

    def _get(키, 기본=""):
        v = 필드.get(키)
        if v is None:
            v = 기존.get(키, 기본)
        return 입력_소독(str(v)) if v else 기본

    def _get_json(키, 기본):
        v = 필드.get(키)
        if v is None:
            raw = 기존.get(키)
            if raw:
                return raw
            return json.dumps(기본, ensure_ascii=False)
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return str(v)

    # 민감 필드 암호화
    def _enc_get(enc_키, 평문_키):
        평문 = 필드.get(평문_키)
        if 평문 is not None:
            if isinstance(평문, dict):
                평문 = json.dumps(평문, ensure_ascii=False)
            return 암호화(입력_소독(str(평문)))
        return 기존.get(enc_키, "")

    try:
        DB().execute("""
            INSERT INTO students
            (chat_id, username, full_name, target_major, school_type,
             grade_raw, mock_exam, grade_trend, elective_subj, repeat_year,
             grade_system, setech_enc, subject_gpa_enc, school_rec_enc,
             interest_univs, grade_history, question_hist,
             created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username        = excluded.username,
                full_name       = excluded.full_name,
                target_major    = excluded.target_major,
                school_type     = excluded.school_type,
                grade_raw       = excluded.grade_raw,
                mock_exam       = excluded.mock_exam,
                grade_trend     = excluded.grade_trend,
                elective_subj   = excluded.elective_subj,
                repeat_year     = excluded.repeat_year,
                grade_system    = excluded.grade_system,
                setech_enc      = excluded.setech_enc,
                subject_gpa_enc = excluded.subject_gpa_enc,
                school_rec_enc  = excluded.school_rec_enc,
                interest_univs  = excluded.interest_univs,
                grade_history   = excluded.grade_history,
                question_hist   = excluded.question_hist,
                updated_at      = excluded.updated_at
        """, (
            chat_id,
            _get("username"), _get("full_name"),
            _get("target_major"), _get("school_type"),
            _get("grade_raw"), _get("mock_exam"),
            _get("grade_trend"), _get("elective_subj"), _get("repeat_year"),
            _get("grade_system", "9등급"),
            _enc_get("setech_enc", "setech"),
            _enc_get("subject_gpa_enc", "subject_gpa"),
            _enc_get("school_rec_enc", "school_record"),
            _get_json("interest_univs", []),
            _get_json("grade_history", {}),
            _get_json("question_hist", []),
            기존.get("created_at", now),
            now,
        ))
        DB().commit()
    except Exception as _e:
        logger.error(f"[DB] 학생 업서트 오류 chat_id={chat_id}: {_e}")


def 학생_프로필_재구성(row: sqlite3.Row | None) -> dict:
    """SQLite Row를 기존 중첩 프로필 형식으로 재구성합니다."""
    if not row:
        return {"관심_대학": [], "질문_이력": [], "성적": {}, "성적_이력": {}}
    d = dict(row)

    setech      = 복호화(d.get("setech_enc", ""))
    sub_gpa_str = 복호화(d.get("subject_gpa_enc", ""))
    school_rec  = 복호화(d.get("school_rec_enc", ""))

    try:
        sub_gpa = json.loads(sub_gpa_str) if sub_gpa_str else {}
    except Exception:
        sub_gpa = {}

    성적: dict = {}
    if d.get("target_major"):  성적["희망학과"]   = d["target_major"]
    if d.get("school_type"):   성적["고교_유형"]  = d["school_type"]
    if d.get("grade_raw"):     성적["내신"]       = d["grade_raw"]
    if d.get("mock_exam"):     성적["모의고사"]   = d["mock_exam"]
    if d.get("grade_trend"):   성적["성적_추이"]  = d["grade_trend"]
    if d.get("elective_subj"): 성적["선택과목"]   = d["elective_subj"]
    if d.get("repeat_year"):   성적["재수여부"]   = d["repeat_year"]
    if d.get("grade_system") and d["grade_system"] != "9등급":
        성적["등급_체계"] = d["grade_system"]
    if setech:                 성적["세특"]       = setech
    if sub_gpa:                성적["과목별_내신"] = sub_gpa
    if school_rec:             성적["생기부_원문"] = school_rec

    try:
        관심 = json.loads(d.get("interest_univs", "[]") or "[]")
    except Exception:
        관심 = []
    try:
        이력 = json.loads(d.get("grade_history", "{}") or "{}")
    except Exception:
        이력 = {}
    try:
        질문 = json.loads(d.get("question_hist", "[]") or "[]")
    except Exception:
        질문 = []

    return {
        "chat_id":   d.get("chat_id"),
        "username":  d.get("username", ""),
        "full_name": d.get("full_name", ""),
        "최초_접속": d.get("created_at", ""),
        "최근_접속": d.get("updated_at", ""),
        "관심_대학": 관심,
        "성적":      성적,
        "성적_이력": 이력,
        "질문_이력": 질문,
    }


def 학생_수() -> int:
    try:
        return DB().execute("SELECT COUNT(*) FROM students").fetchone()[0]
    except Exception:
        return 0


_PROFILE_FIELD_WHITELIST: frozenset[str] = frozenset({
    "target_major", "grade_raw", "mock_exam",
    "school_type", "elective_subj", "grade_trend",
    "repeat_year", "grade_system",
    # 신규 프로필 필드
    "current_grade", "highschool_type", "target_keywords", "csat_subjects",
})


def update_user_profile(user_id: int, field_name: str, value: str) -> bool:
    """
    students 테이블의 단일 필드를 업데이트합니다.

    허용 필드: target_major, grade_raw, mock_exam, school_type,
               elective_subj, grade_trend, repeat_year, grade_system

    반환: True = 성공, False = 허용되지 않은 필드 또는 DB 오류
    """
    if field_name not in _PROFILE_FIELD_WHITELIST:
        logger.warning(f"[DB] update_user_profile: 허용되지 않은 필드 '{field_name}'")
        return False
    try:
        학생_업서트(user_id, {field_name: 입력_소독(str(value))})
        logger.info(f"[DB] 프로필 업데이트: chat_id={user_id}, {field_name}='{value[:30]}'")
        return True
    except Exception as e:
        logger.error(f"[DB] update_user_profile 오류 chat_id={user_id}: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# 입시 데이터 CRUD
# ─────────────────────────────────────────────────────────────
# 학과 지식 DB CRUD
# ─────────────────────────────────────────────────────────────

def get_major_info(major_name: str) -> dict | None:
    """
    major_knowledge 테이블에서 학과 정보를 조회합니다.

    반환: {"major_name", "curriculum", "career_paths", "employment_companies", "updated_at"}
          또는 존재하지 않으면 None
    """
    try:
        row = DB().execute(
            "SELECT * FROM major_knowledge WHERE major_name=?",
            (major_name.strip()[:100],),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"[DB] get_major_info 오류: {e}")
        return None


def save_major_info(
    major_name: str,
    curriculum: str,
    career: str,
    employment: str,
) -> bool:
    """
    학과 지식 DB에 저장 또는 업데이트합니다 (UPSERT).

    Parameters
    ----------
    major_name  : 학과명 (PRIMARY KEY)
    curriculum  : 주요 교육과정 텍스트
    career      : 졸업 후 직무/커리어 텍스트
    employment  : 주요 취업 기업·기관 텍스트

    반환: True = 성공, False = 오류
    """
    now = datetime.now().isoformat(timespec="seconds")
    try:
        DB().execute(
            """
            INSERT INTO major_knowledge
                (major_name, curriculum, career_paths, employment_companies, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(major_name) DO UPDATE SET
                curriculum           = excluded.curriculum,
                career_paths         = excluded.career_paths,
                employment_companies = excluded.employment_companies,
                updated_at           = excluded.updated_at
            """,
            (
                입력_소독(major_name)[:100],
                입력_소독(curriculum)[:3000],
                입력_소독(career)[:3000],
                입력_소독(employment)[:3000],
                now,
            ),
        )
        DB().commit()
        logger.info(f"[DB] 학과 지식 저장 완료: {major_name}")
        return True
    except Exception as e:
        logger.error(f"[DB] save_major_info 오류: {e}")
        return False


# ─────────────────────────────────────────────────────────────

def 입시_저장(항목: dict):
    """파싱된 입시 데이터를 admissions_guide 테이블에 저장합니다."""
    now = datetime.now().isoformat(timespec="seconds")
    소스 = 항목.get("_소스_파일") or (항목.get("대학명", "?") + "_" + 항목.get("학년도", "?"))
    try:
        DB().execute("""
            INSERT INTO admissions_guide
            (university, year, doc_type, source_file, entrance_types,
             parse_method, reliability, parse_ts, parse_failed, error_msg)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_file) DO UPDATE SET
                university     = excluded.university,
                year           = excluded.year,
                doc_type       = excluded.doc_type,
                entrance_types = excluded.entrance_types,
                parse_method   = excluded.parse_method,
                reliability    = excluded.reliability,
                parse_ts       = excluded.parse_ts,
                parse_failed   = excluded.parse_failed,
                error_msg      = excluded.error_msg
        """, (
            항목.get("대학명", ""),
            항목.get("학년도", ""),
            항목.get("문서_유형", ""),
            소스,
            json.dumps(항목.get("수시_전형목록", []), ensure_ascii=False),
            항목.get("_파싱_방식", ""),
            항목.get("파싱_신뢰도", ""),
            항목.get("_파싱_시각", now),
            1 if 항목.get("파싱_실패") else 0,
            항목.get("오류_메시지", ""),
        ))
        DB().commit()
    except Exception as _e:
        logger.error(f"[DB] 입시_저장 오류: {_e}")


def 입시_대학_목록() -> list[str]:
    try:
        rows = DB().execute(
            "SELECT DISTINCT university FROM admissions_guide "
            "WHERE parse_failed=0 AND university!='' ORDER BY university"
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def 입시_대학_검색(키워드: str) -> list[str]:
    키워드 = 입력_소독(키워드)
    try:
        rows = DB().execute(
            "SELECT DISTINCT university FROM admissions_guide "
            "WHERE university LIKE ? AND parse_failed=0",
            (f"%{키워드}%",),
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def 입시_최신_수시(대학명: str) -> dict | None:
    대학명 = 입력_소독(대학명)
    try:
        row = DB().execute("""
            SELECT * FROM admissions_guide
            WHERE university=? AND parse_failed=0
            ORDER BY year DESC, parse_ts DESC LIMIT 1
        """, (대학명,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            전형들 = json.loads(d.get("entrance_types", "[]") or "[]")
        except Exception:
            전형들 = []
        return {
            "대학명":       d["university"],
            "학년도":       d["year"],
            "문서_유형":    d["doc_type"],
            "_소스_파일":   d["source_file"],
            "_파싱_방식":   d["parse_method"],
            "파싱_신뢰도":  d["reliability"],
            "_파싱_시각":   d["parse_ts"],
            "수시_전형목록": 전형들,
        }
    except Exception as _e:
        logger.error(f"[DB] 입시_최신_수시 오류 {대학명}: {_e}")
        return None


def 입시_문서_목록(대학명: str) -> list[dict]:
    대학명 = 입력_소독(대학명)
    try:
        rows = DB().execute(
            "SELECT * FROM admissions_guide WHERE university=? ORDER BY year DESC", (대학명,)
        ).fetchall()
        결과 = []
        for row in rows:
            d = dict(row)
            try:
                전형들 = json.loads(d.pop("entrance_types", "[]") or "[]")
            except Exception:
                전형들 = []
            결과.append({
                "대학명": d.pop("university", ""),
                "학년도": d.pop("year", ""),
                "문서_유형": d.pop("doc_type", ""),
                "수시_전형목록": 전형들,
                **d,
            })
        return 결과
    except Exception:
        return []


def 입시_전형_검색(대학명: str, 전형_키워드: str) -> list[dict]:
    문서 = 입시_최신_수시(대학명)
    if not 문서:
        return []
    전형들 = 문서.get("수시_전형목록", [])
    if not 전형_키워드:
        return 전형들
    return [t for t in 전형들 if 전형_키워드 in t.get("전형명", "")]


def 입시_총수() -> tuple[int, str]:
    """(파싱 성공 문서 수, 최근 파싱 시각) 반환."""
    try:
        cnt = DB().execute(
            "SELECT COUNT(*) FROM admissions_guide WHERE parse_failed=0"
        ).fetchone()[0]
        row = DB().execute(
            "SELECT parse_ts FROM admissions_guide WHERE parse_failed=0 "
            "ORDER BY parse_ts DESC LIMIT 1"
        ).fetchone()
        return cnt, (row[0] if row else "미상")
    except Exception:
        return 0, "미상"


# ─────────────────────────────────────────────────────────────
# 전체 수록 대학 동적 조회 (모든 테이블 UNION)
# ─────────────────────────────────────────────────────────────

_COVERED_UNIV_SQL = """
    SELECT university   AS name FROM admissions_guide
    WHERE  parse_failed = 0 AND university != ''
    UNION
    SELECT university_name AS name FROM admissions_stats
    WHERE  university_name != ''
    ORDER  BY name
"""

def get_covered_universities_list() -> list[str]:
    """
    모든 관련 테이블에서 고유 대학 이름 목록을 반환합니다.

    포함 테이블:
      - admissions_guide.university       (모집요강 수록 대학)
      - admissions_stats.university_name  (입시통계 보유 대학)

    반환: 가나다순 정렬된 고유 대학명 리스트
    """
    try:
        rows = DB().execute(_COVERED_UNIV_SQL).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception as _e:
        logger.warning(f"[DB] get_covered_universities_list 오류: {_e}")
        return []


def get_covered_universities_count() -> int:
    """
    모든 관련 테이블에서 고유 대학 수를 반환합니다.
    최적화된 서브쿼리 COUNT — 인덱스 활용.
    """
    try:
        row = DB().execute(
            f"SELECT COUNT(*) FROM ({_COVERED_UNIV_SQL})"
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception as _e:
        logger.warning(f"[DB] get_covered_universities_count 오류: {_e}")
        return 0


# ─────────────────────────────────────────────────────────────
# 요청 CRUD
# ─────────────────────────────────────────────────────────────

def 요청_저장(chat_id: int, username: str, full_name: str,
              req_type: str, content: str) -> int:
    content = 입력_소독(content, 최대길이=1000)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        cur = DB().execute("""
            INSERT INTO user_requests
            (chat_id, username, full_name, req_type, content, status, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (chat_id, username, full_name, req_type, content, "접수", now))
        DB().commit()
        return cur.lastrowid or 0
    except Exception as _e:
        logger.error(f"[DB] 요청_저장 오류: {_e}")
        return 0


def 요청_총수() -> int:
    try:
        return DB().execute("SELECT COUNT(*) FROM user_requests").fetchone()[0]
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────
# PDF 텍스트 추출 유틸리티
# ─────────────────────────────────────────────────────────────

def PDF_텍스트_추출_바이트(데이터: bytes) -> str:
    """PDF 바이트에서 텍스트를 추출합니다 (pypdf → PyPDF2 폴백)."""
    import io
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(데이터))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except ImportError:
        pass
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(데이터))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as _e:
        logger.warning(f"[PDF추출] 실패: {_e}")
        return ""


# ─────────────────────────────────────────────────────────────
# 시스템 메트릭 스냅샷 (DevOps 리포터용)
# ─────────────────────────────────────────────────────────────

def _에러_로그_건수(로그_경로: "Path | str") -> int:
    """JSONL 에러 로그 파일의 오늘치 행 수를 셉니다."""
    import json as _json
    오늘 = datetime.now().strftime("%Y-%m-%d")
    try:
        count = 0
        with open(str(로그_경로), encoding="utf-8") as _f:
            for line in _f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                    if str(obj.get("ts", "")).startswith(오늘):
                        count += 1
                except Exception:
                    pass
        return count
    except FileNotFoundError:
        return 0
    except Exception:
        return 0


def 시스템_스냅샷_저장(
    총_토큰: int = 0,
    shield_defenses: int = 0,
    e2e_test_result: str = "",
) -> dict:
    """
    현재 DB 상태를 system_metrics 에 스냅샷으로 저장(UPSERT)합니다.

    Parameters
    ----------
    총_토큰          : 오늘 누적 Gemini 입출력 토큰 수
    shield_defenses  : 오늘 누적 429 Rate Limit 방어 횟수
    e2e_test_result  : 가장 최근 E2E 테스트 결과 ("PASS"/"FAIL"/"", 미제공 시 DB 자동 조회)

    반환값: 저장된 메트릭 딕셔너리
    """
    오늘 = datetime.now().strftime("%Y-%m-%d")
    conn = DB()

    # ── 각 테이블 현황 카운트 ──────────────────────────────────
    def _count(table: str) -> int:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            return 0

    세특_수       = _count("successful_seteuks")
    입시통계_수   = _count("admissions_stats")
    황금문서_수   = _count("admissions_guide")

    # ── 오늘 에러 로그 건수 ────────────────────────────────────
    _에러_로그_경로 = DB_경로.parent / "fix_error" / "crawler_errors.log"
    에러_수 = _에러_로그_건수(_에러_로그_경로)
    # error_count = 모든 에러 로그 합산 (crawler + telegram + test)
    _에러_로그_목록 = [
        DB_경로.parent / "fix_error" / "crawler_errors.log",
        DB_경로.parent / "fix_error" / "telegram_errors.log",
        DB_경로.parent / "fix_error" / "test_errors.log",
    ]
    총_에러_수 = sum(_에러_로그_건수(p) for p in _에러_로그_목록)

    # ── E2E 테스트 결과 자동 조회 (미제공 시) ─────────────────
    if not e2e_test_result:
        try:
            row = conn.execute(
                "SELECT director_verdict FROM verified_golden_records "
                "WHERE date(created_at) = ? ORDER BY created_at DESC LIMIT 1",
                (오늘,),
            ).fetchone()
            if row and row[0]:
                verdict = json.loads(row[0])
                e2e_test_result = str(verdict.get("status", ""))
        except Exception:
            pass

    메트릭 = {
        "date_str":         오늘,
        "seteuk_count":     세특_수,
        "univ_stats_count": 입시통계_수,
        "golden_count":     황금문서_수,
        "crawler_errors":   에러_수,
        "error_count":      총_에러_수,
        "shield_defenses":  shield_defenses,
        "e2e_test_result":  e2e_test_result,
        "total_tokens":     총_토큰,
    }

    try:
        conn.execute("""
            INSERT INTO system_metrics
                (date_str, seteuk_count, univ_stats_count,
                 golden_count, crawler_errors, error_count,
                 shield_defenses, e2e_test_result, total_tokens)
            VALUES (:date_str, :seteuk_count, :univ_stats_count,
                    :golden_count, :crawler_errors, :error_count,
                    :shield_defenses, :e2e_test_result, :total_tokens)
            ON CONFLICT(date_str) DO UPDATE SET
                seteuk_count     = excluded.seteuk_count,
                univ_stats_count = excluded.univ_stats_count,
                golden_count     = excluded.golden_count,
                crawler_errors   = excluded.crawler_errors,
                error_count      = excluded.error_count,
                shield_defenses  = MAX(system_metrics.shield_defenses,
                                       excluded.shield_defenses),
                e2e_test_result  = CASE WHEN excluded.e2e_test_result != ''
                                        THEN excluded.e2e_test_result
                                        ELSE system_metrics.e2e_test_result END,
                total_tokens     = MAX(system_metrics.total_tokens, excluded.total_tokens)
        """, 메트릭)
        conn.commit()
        logger.info(f"[메트릭] {오늘} 스냅샷 저장 완료: {메트릭}")
    except Exception as _e:
        logger.warning(f"[메트릭] 스냅샷 저장 실패: {_e}")

    return 메트릭


def snapshot_daily_metrics() -> dict:
    """
    현재 DB 행 수를 동적으로 집계하여 오늘 날짜의 system_metrics 행을
    INSERT OR REPLACE 로 갱신합니다.

    집계 테이블
    -----------
    seteuk_count     ← successful_seteuks   전체 행 수
    univ_stats_count ← admissions_stats     전체 행 수
    golden_count     ← verified_golden_records 전체 행 수
    crawler_errors   ← crawler_errors.log  오늘치 JSONL 행 수
    error_count      ← 4개 에러 로그 파일 합산

    누적 값 보존 (INSERT OR REPLACE 로 대체되지 않도록 기존 행에서 읽어 그대로 씀)
    -----------
    shield_defenses  ← 오늘 기존 값 유지 (없으면 0)
    e2e_test_result  ← 오늘 기존 값 유지 (없으면 '')
    total_tokens     ← 오늘 기존 값 유지 (없으면 0)

    반환값: 저장된 메트릭 딕셔너리
    """
    오늘 = datetime.now().strftime("%Y-%m-%d")

    # DB()는 내부적으로 _연결_잠금 을 사용하므로 여기서 중복 취득하면 데드락 발생.
    # 연결 싱글턴 초기화는 DB() 에 위임하고, 쓰기 직렬화는 SQLite WAL 모드에 의존.
    conn = DB()

    # ── 동적 테이블 카운트 ─────────────────────────────────
    def _count(table: str) -> int:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            return 0

    seteuk_count     = _count("successful_seteuks")
    univ_stats_count = _count("admissions_stats")
    golden_count     = _count("verified_golden_records")

    # ── 에러 로그 집계 ─────────────────────────────────────
    _fix_dir = DB_경로.parent / "fix_error"
    crawler_errors = _에러_로그_건수(_fix_dir / "crawler_errors.log")
    error_count    = sum(
        _에러_로그_건수(_fix_dir / fname)
        for fname in (
            "crawler_errors.log", "telegram_errors.log",
            "test_errors.log",    "devops_errors.log",
        )
    )

    # ── 오늘 누적 값 읽기 (INSERT OR REPLACE 시 덮어쓰지 않도록 보존) ──
    _existing = conn.execute(
        "SELECT shield_defenses, e2e_test_result, total_tokens, "
        "       gemini_daily_tokens, groq_daily_tokens "
        "FROM system_metrics WHERE date_str = ?",
        (오늘,),
    ).fetchone()
    shield_defenses     = int(_existing[0] or 0) if _existing else 0
    e2e_test_result     = str(_existing[1] or "")  if _existing else ""
    total_tokens        = int(_existing[2] or 0) if _existing else 0
    gemini_daily_tokens = int(_existing[3] or 0) if _existing else 0
    groq_daily_tokens   = int(_existing[4] or 0) if _existing else 0

    # ── E2E 결과 자동 조회 (기존 값이 없을 때만) ──────────
    if not e2e_test_result:
        try:
            _row = conn.execute(
                "SELECT director_verdict FROM verified_golden_records "
                "WHERE date(created_at) = ? ORDER BY created_at DESC LIMIT 1",
                (오늘,),
            ).fetchone()
            if _row and _row[0]:
                _verdict = json.loads(_row[0])
                e2e_test_result = str(_verdict.get("status", ""))
        except Exception:
            pass

    메트릭 = {
        "date_str":             오늘,
        "seteuk_count":         seteuk_count,
        "univ_stats_count":     univ_stats_count,
        "golden_count":         golden_count,
        "crawler_errors":       crawler_errors,
        "error_count":          error_count,
        "shield_defenses":      shield_defenses,
        "e2e_test_result":      e2e_test_result,
        "total_tokens":         total_tokens,
        "gemini_daily_tokens":  gemini_daily_tokens,
        "groq_daily_tokens":    groq_daily_tokens,
    }

    try:
        conn.execute("""
            INSERT OR REPLACE INTO system_metrics
                (date_str, seteuk_count, univ_stats_count, golden_count,
                 crawler_errors, error_count,
                 shield_defenses, e2e_test_result, total_tokens,
                 gemini_daily_tokens, groq_daily_tokens)
            VALUES
                (:date_str, :seteuk_count, :univ_stats_count, :golden_count,
                 :crawler_errors, :error_count,
                 :shield_defenses, :e2e_test_result, :total_tokens,
                 :gemini_daily_tokens, :groq_daily_tokens)
        """, 메트릭)
        conn.commit()
        logger.info(
            f"[snapshot_daily_metrics] {오늘} 저장 완료: "
            f"seteuk={seteuk_count} stats={univ_stats_count} "
            f"golden={golden_count} errors={error_count}"
        )
    except Exception as _e:
        logger.warning(f"[snapshot_daily_metrics] 저장 실패: {_e}")

    return 메트릭


def 토큰_사용량_추가(engine: str, tokens: int) -> None:
    """오늘 날짜의 엔진별 토큰 사용량을 누적합니다. engine은 'gemini' 또는 'groq'."""
    if engine not in ("gemini", "groq") or tokens <= 0:
        return
    오늘 = datetime.now().strftime("%Y-%m-%d")
    컬럼 = "gemini_daily_tokens" if engine == "gemini" else "groq_daily_tokens"
    conn = DB()
    try:
        conn.execute(
            f"INSERT INTO system_metrics (date_str, {컬럼}) VALUES (?, ?) "
            f"ON CONFLICT(date_str) DO UPDATE SET {컬럼} = system_metrics.{컬럼} + excluded.{컬럼}",
            (오늘, tokens),
        )
        conn.commit()
    except Exception as _e:
        logger.warning(f"[토큰추적] {engine} 토큰 저장 실패: {_e}")


def 쉴드방어_기록(건수: int = 1) -> None:
    """오늘 날짜의 429 Rate Limit 방어(쉴드) 횟수를 누적합니다."""
    if 건수 <= 0:
        return
    오늘 = datetime.now().strftime("%Y-%m-%d")
    try:
        DB().execute(
            "INSERT INTO system_metrics (date_str, shield_defenses) VALUES (?, ?) "
            "ON CONFLICT(date_str) DO UPDATE SET "
            "shield_defenses = system_metrics.shield_defenses + excluded.shield_defenses",
            (오늘, 건수),
        )
        DB().commit()
    except Exception as _e:
        logger.warning(f"[쉴드방어] 기록 실패: {_e}")


def 오늘_토큰_사용량() -> dict[str, int]:
    """오늘 날짜의 엔진별 누적 토큰 사용량을 반환합니다."""
    오늘 = datetime.now().strftime("%Y-%m-%d")
    try:
        row = DB().execute(
            "SELECT gemini_daily_tokens, groq_daily_tokens "
            "FROM system_metrics WHERE date_str = ?",
            (오늘,),
        ).fetchone()
        if row:
            return {"gemini": int(row[0] or 0), "groq": int(row[1] or 0)}
    except Exception as _e:
        logger.warning(f"[토큰추적] 조회 실패: {_e}")
    return {"gemini": 0, "groq": 0}


def 시스템_메트릭_조회(days: int = 30) -> list[dict]:
    """
    최근 N일간의 system_metrics 행을 날짜 오름차순으로 반환합니다.
    각 행은 dict 형태입니다.
    """
    try:
        rows = DB().execute(
            "SELECT date_str, seteuk_count, univ_stats_count, "
            "       golden_count, crawler_errors, error_count, "
            "       shield_defenses, e2e_test_result, total_tokens "
            "FROM system_metrics "
            "ORDER BY date_str DESC LIMIT ?",
            (days,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    except Exception as _e:
        logger.warning(f"[메트릭] 조회 실패: {_e}")
        return []


# ─────────────────────────────────────────────────────────────
# 관리자 설정 CRUD
# ─────────────────────────────────────────────────────────────

def admin_설정_조회(key: str, 기본값: str = "") -> str:
    """
    admin_settings 에서 setting_key 에 해당하는 값을 반환합니다.
    키가 없거나 오류 시 기본값을 반환합니다.
    """
    try:
        row = DB().execute(
            "SELECT setting_value FROM admin_settings WHERE setting_key=?",
            (key,),
        ).fetchone()
        if row is not None:
            return str(row[0])
        # 키가 없으면 기본값 레코드를 자동 삽입 후 반환
        if 기본값:
            DB().execute(
                "INSERT OR IGNORE INTO admin_settings (setting_key, setting_value) VALUES (?,?)",
                (key, 기본값),
            )
            DB().commit()
        return 기본값
    except Exception as _e:
        logger.warning(f"[DB] admin_설정_조회({key}) 오류: {_e}")
        return 기본값


def admin_설정_저장(key: str, value: str) -> bool:
    """
    admin_settings 에 key=value 를 UPSERT 합니다.
    성공 시 True, 실패 시 False 반환.
    """
    try:
        DB().execute(
            "INSERT INTO admin_settings (setting_key, setting_value) VALUES (?,?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
            (key, str(value)),
        )
        DB().commit()
        logger.info(f"[DB] admin_설정_저장: {key}={value}")
        return True
    except Exception as _e:
        logger.warning(f"[DB] admin_설정_저장({key}) 오류: {_e}")
        return False


# ─────────────────────────────────────────────────────────────
# 검증된 골든 레코드 CRUD (E2E 테스트 → DB 주입)
# ─────────────────────────────────────────────────────────────

def save_golden_record(
    target_major: str,
    mock_profile: dict,
    final_optimized_text: str,
    director_verdict: dict,
    source: str = "E2E-Synthetic",
) -> int:
    """
    E2E 테스트에서 PASS 판정된 골든 레코드를 verified_golden_records 에 저장합니다.

    Parameters
    ----------
    target_major          : "[E2E-Synthetic] 환경공학과" 형식의 전공명
    mock_profile          : 합성 학생 프로필 딕셔너리
    final_optimized_text  : 비평 에이전트가 개선한 최종 진단 리포트 텍스트
    director_verdict      : 총감독 JSON 판정 딕셔너리 (status, quality_score, reason …)
    source                : 데이터 출처 태그 (기본: "E2E-Synthetic")

    Returns
    -------
    삽입된 레코드의 id (실패 시 0)
    """
    now = datetime.now().isoformat(timespec="seconds")
    try:
        quality_score = int(director_verdict.get("quality_score", 0))
    except (ValueError, TypeError):
        quality_score = 0

    try:
        with _연결_잠금:
            conn = DB()
            cur = conn.execute(
                """
                INSERT INTO verified_golden_records
                    (target_major, mock_profile, final_optimized_text,
                     source, director_verdict, quality_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    입력_소독(target_major, 최대길이=200),
                    json.dumps(mock_profile, ensure_ascii=False),
                    입력_소독(final_optimized_text, 최대길이=10000),
                    source,
                    json.dumps(director_verdict, ensure_ascii=False),
                    quality_score,
                    now,
                ),
            )
            conn.commit()
        logger.info(
            f"[DB] 골든 레코드 저장 완료: id={cur.lastrowid}, "
            f"major={target_major}, score={quality_score}"
        )
        return cur.lastrowid or 0
    except Exception as _e:
        logger.error(f"[DB] save_golden_record 오류: {_e}")
        return 0


def golden_record_조회(major_prefix: str = "", limit: int = 50) -> list[dict]:
    """
    verified_golden_records 를 조회합니다.
    major_prefix 가 주어지면 LIKE 필터 적용.
    """
    try:
        if major_prefix:
            rows = DB().execute(
                "SELECT id, target_major, source, quality_score, created_at "
                "FROM verified_golden_records WHERE target_major LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (f"%{major_prefix}%", limit),
            ).fetchall()
        else:
            rows = DB().execute(
                "SELECT id, target_major, source, quality_score, created_at "
                "FROM verified_golden_records ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as _e:
        logger.warning(f"[DB] golden_record_조회 오류: {_e}")
        return []


# ─────────────────────────────────────────────────────────────
# pending_verifications — 비동기 사후 검증 파이프라인 CRUD
# ─────────────────────────────────────────────────────────────

def pending_verification_추가(user_id: int, query: str, ollama_answer: str) -> int:
    """Ollama 1차 답변을 검증 대기열에 삽입합니다. 생성된 row id 반환."""
    try:
        cur = DB().execute(
            """
            INSERT INTO pending_verifications (user_id, query, ollama_answer)
            VALUES (?, ?, ?)
            """,
            (int(user_id), str(query)[:5000], str(ollama_answer)[:10000]),
        )
        DB().commit()
        row_id = cur.lastrowid or 0
        logger.info(f"[PV] 큐잉 완료: id={row_id}, user_id={user_id}")
        return row_id
    except Exception as _e:
        logger.warning(f"[PV] 큐잉 실패: {_e}")
        return 0


def pending_verifications_조회(
    status: str = "pending",
    limit: int = 10,
) -> list[dict]:
    """지정 상태의 pending_verifications 행을 오래된 순으로 반환합니다."""
    try:
        rows = DB().execute(
            """
            SELECT id, user_id, query, ollama_answer, created_at
            FROM pending_verifications
            WHERE status = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as _e:
        logger.warning(f"[PV] 조회 실패: {_e}")
        return []


def pending_verification_완료(row_id: int, verified_answer: str) -> None:
    """검증 완료 — status='verified', verified_answer 업데이트."""
    try:
        DB().execute(
            """
            UPDATE pending_verifications
            SET status = 'verified',
                verified_answer = ?,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (str(verified_answer)[:10000], int(row_id)),
        )
        DB().commit()
        logger.info(f"[PV] 검증 완료: id={row_id}")
    except Exception as _e:
        logger.warning(f"[PV] 완료 업데이트 실패: {_e}")


def pending_verification_실패(row_id: int) -> None:
    """검증 실패 — status='failed' 업데이트 (재시도 방지)."""
    try:
        DB().execute(
            """
            UPDATE pending_verifications
            SET status = 'failed',
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (int(row_id),),
        )
        DB().commit()
        logger.info(f"[PV] 실패 처리: id={row_id}")
    except Exception as _e:
        logger.warning(f"[PV] 실패 업데이트 실패: {_e}")


# ─────────────────────────────────────────────────────────────
# golden_dataset — Ollama 자율 진화 파이프라인용 황금 QA 저장소
# ─────────────────────────────────────────────────────────────

_GOLDEN_JSONL = Path(__file__).resolve().parent.parent / "data" / "golden_dataset.jsonl"


def save_golden_qa(
    user_query: str,
    ollama_response: str = "",
    gemini_response: str = "",
    source: str = "verified",
    quality_score: int = 0,
    fake_profile: str = "",
    ollama_draft: str = "",
    gemini_perfect: str = "",
) -> int:
    """
    Ollama 초안과 Gemini 개선본의 QA 쌍을 golden_dataset 테이블과
    data/golden_dataset.jsonl 파일에 동시에 저장합니다.

    Parameters
    ----------
    user_query      : 사용자 원본 질문 또는 프롬프트
    ollama_response : Ollama 1차 답변 (이전 호환용, ollama_draft 와 동일)
    gemini_response : Gemini 개선 답변 (이전 호환용, gemini_perfect 와 동일)
    source          : 데이터 출처 태그 (기본: "verified", 시뮬레이터: "synthetic")
    quality_score   : 0–100 품질 점수
    fake_profile    : 시뮬레이터가 생성한 가상 학생 프로필 JSON 문자열
    ollama_draft    : Ollama 1차 초안 (simulator 파이프라인 전용)
    gemini_perfect  : Gemini 황금 정답 (simulator 파이프라인 전용)

    Returns
    -------
    삽입된 row id (실패 시 0)
    """
    # 신규 필드가 주어지면 이전 호환 필드를 덮어씀
    _ollama  = ollama_draft   or ollama_response
    _gemini  = gemini_perfect or gemini_response

    now = datetime.now().isoformat(timespec="seconds")
    record = {
        "timestamp":       now,
        "user_query":      str(user_query)[:3000],
        "ollama_response": str(_ollama)[:5000],
        "gemini_response": str(_gemini)[:10000],
        "source":          str(source)[:50],
        "quality_score":   int(quality_score),
        "fake_profile":    str(fake_profile)[:2000],
        "used_in_train":   False,
    }

    # ── JSONL append ──────────────────────────────────────────
    try:
        _GOLDEN_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with _GOLDEN_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as _e:
        logger.warning(f"[GoldenQA] JSONL 저장 실패: {_e}")

    # ── SQLite INSERT ─────────────────────────────────────────
    try:
        cur = DB().execute(
            """
            INSERT INTO golden_dataset
                (user_query, ollama_response, gemini_response,
                 source, quality_score, fake_profile, ollama_draft, gemini_perfect,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(user_query)[:3000],
                str(_ollama)[:5000],
                str(_gemini)[:10000],
                str(source)[:50],
                int(quality_score),
                str(fake_profile)[:2000],
                str(ollama_draft)[:5000],
                str(gemini_perfect)[:10000],
                now,
            ),
        )
        DB().commit()
        row_id = cur.lastrowid or 0
        logger.info(
            f"[GoldenQA] 저장 완료: id={row_id}, source={source}, "
            f"score={quality_score}, query={str(user_query)[:40]!r}"
        )
        return row_id
    except Exception as _e:
        logger.error(f"[GoldenQA] DB 저장 실패: {_e}")
        return 0


def get_golden_qa_for_training(limit: int = 50) -> list[dict]:
    """
    미사용(used_in_train=0) golden_dataset 행을 quality_score 내림차순으로 반환합니다.
    nightly_train.py 에서 Modelfile few-shot 예시 생성에 사용합니다.
    """
    try:
        rows = DB().execute(
            """
            SELECT id, user_query, ollama_response, gemini_response,
                   source, quality_score, created_at
            FROM   golden_dataset
            WHERE  used_in_train = 0
            ORDER  BY quality_score DESC, created_at DESC
            LIMIT  ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as _e:
        logger.warning(f"[GoldenQA] 조회 실패: {_e}")
        return []


def mark_golden_qa_trained(ids: list[int]) -> None:
    """
    학습에 사용된 row id 목록을 used_in_train=1 로 표시합니다.
    중복 학습 방지 목적.
    """
    if not ids:
        return
    try:
        placeholders = ",".join("?" * len(ids))
        DB().execute(
            f"UPDATE golden_dataset SET used_in_train = 1 WHERE id IN ({placeholders})",
            [int(i) for i in ids],
        )
        DB().commit()
        logger.info(f"[GoldenQA] {len(ids)}개 학습 완료 표시")
    except Exception as _e:
        logger.warning(f"[GoldenQA] 학습 표시 실패: {_e}")


def get_golden_dataset_stats() -> dict:
    """
    golden_dataset 의 통계를 반환합니다.

    Returns
    -------
    {
        "total":       전체 건수,
        "by_source":   {"synthetic": N, "verified": M, ...},
        "trained":     학습에 사용된 건수,
        "untrained":   미사용 건수,
    }
    """
    try:
        conn = DB()
        total = conn.execute("SELECT COUNT(*) FROM golden_dataset").fetchone()[0]
        trained = conn.execute(
            "SELECT COUNT(*) FROM golden_dataset WHERE used_in_train = 1"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM golden_dataset GROUP BY source"
        ).fetchall()
        by_source = {r["source"]: r["cnt"] for r in rows}
        return {
            "total":     total,
            "by_source": by_source,
            "trained":   trained,
            "untrained": total - trained,
        }
    except Exception as _e:
        logger.warning(f"[GoldenQA] 통계 조회 실패: {_e}")
        return {"total": 0, "by_source": {}, "trained": 0, "untrained": 0}


# ─────────────────────────────────────────────────────────────
# simulator_quota — 일일 토큰 예산 추적
# ─────────────────────────────────────────────────────────────

def get_today_simulator_usage() -> dict:
    """
    오늘 날짜의 시뮬레이터 토큰 사용량을 반환합니다.
    데이터가 없으면 {"date": today, "tokens_used": 0, "runs_completed": 0} 반환.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        row = DB().execute(
            "SELECT tokens_used, runs_completed FROM simulator_quota WHERE date = ?",
            (today,),
        ).fetchone()
        if row:
            return {
                "date":           today,
                "tokens_used":    int(row["tokens_used"]),
                "runs_completed": int(row["runs_completed"]),
            }
    except Exception as _e:
        logger.warning(f"[SimQuota] 조회 실패: {_e}")
    return {"date": today, "tokens_used": 0, "runs_completed": 0}


def add_simulator_usage(tokens: int, runs: int = 1) -> None:
    """
    오늘 날짜의 시뮬레이터 토큰 사용량을 누적합니다.
    행이 없으면 INSERT, 있으면 UPDATE (UPSERT).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        DB().execute(
            """
            INSERT INTO simulator_quota (date, tokens_used, runs_completed)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                tokens_used    = tokens_used    + excluded.tokens_used,
                runs_completed = runs_completed + excluded.runs_completed
            """,
            (today, int(tokens), int(runs)),
        )
        DB().commit()
        logger.debug(f"[SimQuota] +{tokens}tokens +{runs}runs → {today}")
    except Exception as _e:
        logger.warning(f"[SimQuota] 누적 실패: {_e}")
