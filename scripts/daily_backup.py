"""
daily_backup.py — UnivAgent 일일 백업 스크립트
================================================
실행 시:
  1. generate_handover.py 실행 → docs/CURRENT_HANDOVER_PROMPT.txt 최신화
  2. data/backups/UnivAgent_Backup_YYYYMMDD_HHMMSS.tar.gz 생성
     포함 대상: scripts/, docs/, .env, data/*.db
  3. 7일 초과 구백업 자동 삭제

실행:
    python3 scripts/daily_backup.py
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import tarfile
import time
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
_logger = logging.getLogger("daily_backup")

# ── 경로 설정 (이 파일은 scripts/ 에 위치 → 부모가 프로젝트 루트) ──────────
_ROOT     = Path(__file__).resolve().parent.parent
_BACKUP_DIR = _ROOT / "data" / "backups"
_RETENTION_DAYS = 7


# ─────────────────────────────────────────────────────────────
# 백업 생성
# ─────────────────────────────────────────────────────────────

def create_backup() -> Path:
    """
    타임스탬프 tar.gz 아카이브를 생성합니다.

    포함 대상:
      - scripts/        전체 디렉터리
      - .env            (존재하는 경우)
      - data/*.db       SQLite DB 파일 전체

    반환값: 생성된 아카이브 경로
    """
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive  = _BACKUP_DIR / f"UnivAgent_Backup_{ts}.tar.gz"

    _logger.info(f"[Backup] 아카이브 생성 시작: {archive.name}")

    with tarfile.open(archive, "w:gz") as tar:

        # 1) scripts/ 디렉터리
        scripts_dir = _ROOT / "scripts"
        if scripts_dir.is_dir():
            tar.add(scripts_dir, arcname="scripts")
            _logger.info(f"[Backup]   + scripts/ ({_dir_size_mb(scripts_dir):.1f} MB)")
        else:
            _logger.warning("[Backup]   scripts/ 없음 — 건너뜀")

        # 2) .env 파일
        env_file = _ROOT / ".env"
        if env_file.is_file():
            tar.add(env_file, arcname=".env")
            _logger.info(f"[Backup]   + .env ({env_file.stat().st_size:,} bytes)")
        else:
            _logger.info("[Backup]   .env 없음 — 건너뜀")

        # 3) docs/ 디렉터리 (핸드오버 프롬프트 포함)
        docs_dir = _ROOT / "docs"
        if docs_dir.is_dir():
            tar.add(docs_dir, arcname="docs")
            _logger.info(f"[Backup]   + docs/ ({_dir_size_mb(docs_dir):.2f} MB)")
        else:
            _logger.info("[Backup]   docs/ 없음 — 건너뜀")

        # 4) data/*.db SQLite 파일
        db_files = list(_ROOT.glob("data/*.db"))
        for db_path in db_files:
            tar.add(db_path, arcname=f"data/{db_path.name}")
            _logger.info(f"[Backup]   + data/{db_path.name} ({db_path.stat().st_size / 1024:.1f} KB)")

        if not db_files:
            _logger.info("[Backup]   data/*.db 없음 — 건너뜀")

    size_mb = archive.stat().st_size / (1024 * 1024)
    _logger.info(f"[Backup] ✅ 완료: {archive.name}  ({size_mb:.2f} MB)")
    return archive


# ─────────────────────────────────────────────────────────────
# 구백업 삭제
# ─────────────────────────────────────────────────────────────

def cleanup_old_backups(retention_days: int = _RETENTION_DAYS) -> int:
    """
    data/backups/ 에서 retention_days 일을 초과한 아카이브를 삭제합니다.

    삭제 기준: 파일 수정 시각(mtime) 또는 파일명 타임스탬프 파싱 — 둘 중 더 오래된 쪽.
    반환값: 삭제된 파일 수
    """
    if not _BACKUP_DIR.is_dir():
        return 0

    cutoff   = time.time() - retention_days * 86400
    patterns = ["*.tar.gz", "*.zip", "*.tgz"]
    deleted  = 0

    for pattern in patterns:
        for path in _BACKUP_DIR.glob(pattern):
            age_by_mtime = path.stat().st_mtime
            age_by_name  = _parse_ts_from_name(path.name)
            # 둘 중 더 오래된 시각 기준
            effective_ts = min(age_by_mtime, age_by_name) if age_by_name else age_by_mtime

            if effective_ts < cutoff:
                try:
                    path.unlink()
                    _logger.info(f"[Backup] 🗑  삭제: {path.name}")
                    deleted += 1
                except Exception as e:
                    _logger.warning(f"[Backup] 삭제 실패 {path.name}: {e}")

    if deleted:
        _logger.info(f"[Backup] 구백업 {deleted}개 삭제 완료")
    else:
        _logger.info("[Backup] 삭제할 구백업 없음")

    return deleted


# ─────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────

def _dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def _parse_ts_from_name(filename: str) -> float | None:
    """
    'UnivAgent_Backup_20260531_143000.tar.gz' 패턴에서 Unix timestamp 파싱.
    파싱 실패 시 None 반환.
    """
    import re
    m = re.search(r"(\d{8})_(\d{6})", filename)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
        return dt.timestamp()
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────

def _refresh_handover() -> None:
    """백업 전 핸드오버 프롬프트를 최신 상태로 갱신합니다."""
    try:
        import importlib.util, sys as _sys
        spec = importlib.util.spec_from_file_location(
            "generate_handover",
            _ROOT / "scripts" / "generate_handover.py",
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        out = mod.generate_handover()
        _logger.info(f"[Backup] 핸드오버 프롬프트 갱신 완료: {out.name}")
    except Exception as e:
        _logger.warning(f"[Backup] 핸드오버 생성 실패 (백업은 계속): {e}")


def main() -> None:
    _logger.info("=" * 50)
    _logger.info(" UnivAgent Daily Backup")
    _logger.info(f" 루트: {_ROOT}")
    _logger.info(f" 저장: {_BACKUP_DIR}")
    _logger.info("=" * 50)

    # 1. 핸드오버 프롬프트 최신화 (압축 전)
    _logger.info("[Backup] 핸드오버 프롬프트 갱신 중...")
    _refresh_handover()

    # 2. 아카이브 생성 (docs/ 포함)
    archive = create_backup()

    # 3. 구백업 정리
    cleanup_old_backups()

    _logger.info(f"[Backup] 최신 백업: {archive}")


if __name__ == "__main__":
    main()
