"""
데이터셋 로그 로테이션 & 스토리지 관리 유틸리티

- UserProfileManager: JSON 파일 기반 사용자 프로필 CRUD
- RequestCounter: JSON 파일 기반 누적 요청 수 카운터
- rotate_training_dataset(): 학습 데이터 JSONL이 50MB 초과 시 gzip 압축 후 원본 초기화
- 최근 5개 압축 아카이브만 유지, 이전 파일 자동 삭제
"""

import gzip
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_루트 = Path(__file__).resolve().parent.parent
_TRAINING_DIR = _루트 / "data" / "training"
_TRAINING_FILE = _TRAINING_DIR / "ollama_finetune_dataset.jsonl"
_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50 MB
_MAX_ARCHIVES = 5


class UserProfileManager:
    """JSON 파일 기반 사용자 프로필 관리자 (telegram_agent.py 전용)."""

    def __init__(self, 경로: Path) -> None:
        self._경로 = 경로
        self._data: dict[str, dict] = {}

    def load(self) -> None:
        if self._경로.exists():
            try:
                self._data = json.loads(self._경로.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[Storage] 프로필 로드 실패 — 빈 상태로 시작: {e}")
                self._data = {}

    def save(self) -> None:
        self._경로.parent.mkdir(parents=True, exist_ok=True)
        self._경로.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_user(self, user_id: int) -> dict | None:
        return self._data.get(str(user_id))

    def add_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        key = str(user_id)
        if key not in self._data:
            self._data[key] = {
                "user_id": user_id,
                "username": username or "",
                "first_name": first_name or "",
                "target_major": "",
                "grade_raw": "",
                "mock_exam": "",
                "school_type": "일반고",
                "favorites": [],
                "report_preferences": [],
                "awaiting_profile_update": False,
            }

    def update_user_profile(self, user_id: int, key: str, value: str) -> bool:
        user = self._data.get(str(user_id))
        if user is None:
            return False
        user[key] = value
        return True

    def add_favorite(self, user_id: int, university_name: str, plan_name: str) -> bool:
        user = self._data.get(str(user_id))
        if user is None:
            return False
        entry = {"university": university_name, "plan": plan_name}
        if entry not in user.setdefault("favorites", []):
            user["favorites"].append(entry)
        return True

    def set_report_preference(self, user_id: int, university_name: str, plan_name: str) -> bool:
        user = self._data.get(str(user_id))
        if user is None:
            return False
        entry = {"university": university_name, "plan": plan_name}
        prefs = user.setdefault("report_preferences", [])
        if entry not in prefs:
            prefs.append(entry)
        return True

    def is_awaiting_profile_update(self, user_id: int) -> bool:
        user = self._data.get(str(user_id))
        return bool(user and user.get("awaiting_profile_update"))

    def set_awaiting_profile_update(self, user_id: int) -> None:
        user = self._data.get(str(user_id))
        if user is not None:
            user["awaiting_profile_update"] = True

    def clear_awaiting_profile_update(self, user_id: int) -> None:
        user = self._data.get(str(user_id))
        if user is not None:
            user["awaiting_profile_update"] = False

    @property
    def total_users(self) -> int:
        return len(self._data)

    @property
    def 총_사용자_수(self) -> int:
        return self.total_users


class RequestCounter:
    """JSON 파일 기반 누적 요청 수 카운터 (telegram_agent.py 전용)."""

    def __init__(self, 경로: Path) -> None:
        self._경로 = 경로
        self._count = 0

    def load(self) -> None:
        if self._경로.exists():
            try:
                obj = json.loads(self._경로.read_text(encoding="utf-8"))
                self._count = int(obj.get("total_requests", 0))
            except Exception as e:
                logger.warning(f"[Storage] 요청 카운터 로드 실패 — 0으로 시작: {e}")
                self._count = 0

    def increment(self) -> None:
        self._count += 1
        self._경로.parent.mkdir(parents=True, exist_ok=True)
        self._경로.write_text(
            json.dumps({"total_requests": self._count}, ensure_ascii=False),
            encoding="utf-8",
        )

    @property
    def total_requests(self) -> int:
        return self._count

    @property
    def 총_요청_수(self) -> int:
        return self._count


def rotate_training_dataset() -> bool:
    """
    학습 데이터셋이 50MB를 초과하면 gzip 압축 후 원본을 비웁니다.
    최근 5개 아카이브만 유지합니다.

    반환: True = 로테이션 실행됨, False = 임계값 미달 또는 파일 없음
    """
    if not _TRAINING_FILE.exists():
        logger.info("[Storage] 학습 데이터 파일 없음 — 로테이션 건너뜀")
        return False

    파일_크기 = _TRAINING_FILE.stat().st_size
    if 파일_크기 <= _THRESHOLD_BYTES:
        logger.info(
            f"[Storage] 학습 데이터 {파일_크기 / 1024 / 1024:.1f}MB < 50MB — 로테이션 불필요"
        )
        return False

    _TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    타임스탬프 = datetime.now().strftime("%Y%m%d_%H%M%S")
    아카이브_경로 = _TRAINING_DIR / f"ollama_finetune_dataset_{타임스탬프}.jsonl.gz"

    try:
        with _TRAINING_FILE.open("rb") as f_in, gzip.open(str(아카이브_경로), "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        # 원본 초기화 (삭제 대신 비움 — 파일 핸들 안전)
        _TRAINING_FILE.write_text("", encoding="utf-8")

        logger.info(
            f"[Storage] 로테이션 완료: {파일_크기 / 1024 / 1024:.1f}MB → {아카이브_경로.name}"
        )
    except Exception as e:
        logger.error(f"[Storage] 로테이션 실패: {e}")
        return False

    _오래된_아카이브_정리()
    return True


def _오래된_아카이브_정리() -> None:
    """최근 _MAX_ARCHIVES개만 남기고 이전 .gz 파일을 삭제합니다."""
    archives = sorted(
        _TRAINING_DIR.glob("ollama_finetune_dataset_*.jsonl.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in archives[_MAX_ARCHIVES:]:
        try:
            old.unlink()
            logger.info(f"[Storage] 오래된 아카이브 삭제: {old.name}")
        except Exception as e:
            logger.warning(f"[Storage] 아카이브 삭제 실패 {old.name}: {e}")


def training_dataset_info() -> dict:
    """현재 학습 데이터셋 상태를 반환합니다 (devops_reporter 용)."""
    크기_mb = 0.0
    라인_수 = 0
    archives = list(_TRAINING_DIR.glob("ollama_finetune_dataset_*.jsonl.gz")) if _TRAINING_DIR.exists() else []

    if _TRAINING_FILE.exists():
        크기_mb = _TRAINING_FILE.stat().st_size / 1024 / 1024
        try:
            with _TRAINING_FILE.open("r", encoding="utf-8") as f:
                라인_수 = sum(1 for line in f if line.strip())
        except Exception:
            pass

    return {
        "size_mb":   round(크기_mb, 2),
        "lines":     라인_수,
        "archives":  len(archives),
    }
