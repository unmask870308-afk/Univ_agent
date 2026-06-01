#!/usr/bin/env bash
# security_setup.sh — UnivAgent OS 레벨 보안 설정 스크립트
# 사용법: ./scripts/security_setup.sh
# 역할: 민감 파일 권한을 소유자 전용(600)으로 강제 잠금

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "========================================================"
echo "  UnivAgent 보안 설정 (Security Hardening)"
echo "========================================================"

# ── 1. .env 잠금 (소유자 읽기·쓰기만, 그룹/기타 접근 차단) ──
ENV_FILE="$PROJECT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    chmod 600 "$ENV_FILE"
    echo "  [✓] .env                → 600 (소유자 전용)"
else
    echo "  [!] .env 파일 없음 — 건너뜀"
fi

# ── 2. SQLite DB 잠금 ─────────────────────────────────────────
DB_FILE="$PROJECT_DIR/data/admissions_agent.db"
if [ -f "$DB_FILE" ]; then
    chmod 600 "$DB_FILE"
    echo "  [✓] admissions_agent.db → 600 (소유자 전용)"
fi

# WAL / SHM 보조 파일도 동시 잠금 (SQLite WAL 모드 파일)
for EXT in "-shm" "-wal"; do
    AUX="$DB_FILE$EXT"
    if [ -f "$AUX" ]; then
        chmod 600 "$AUX"
        echo "  [✓] admissions_agent.db$EXT → 600"
    fi
done

# ── 3. 에러 로그 디렉터리 잠금 (소유자만 접근) ───────────────
FIX_ERROR_DIR="$PROJECT_DIR/data/fix_error"
if [ -d "$FIX_ERROR_DIR" ]; then
    chmod 700 "$FIX_ERROR_DIR"
    # 개별 로그 파일도 600
    find "$FIX_ERROR_DIR" -maxdepth 1 -type f -name "*.log" \
        -exec chmod 600 {} \; 2>/dev/null || true
    echo "  [✓] data/fix_error/     → 700 + 내부 *.log 600"
fi

# ── 4. 유지보수 리포트 디렉터리 잠금 ────────────────────────
REPORTS_DIR="$PROJECT_DIR/data/maintenance_reports"
if [ -d "$REPORTS_DIR" ]; then
    chmod 700 "$REPORTS_DIR"
    find "$REPORTS_DIR" -type f -name "*.pdf" \
        -exec chmod 600 {} \; 2>/dev/null || true
    echo "  [✓] data/maintenance_reports/ → 700 + *.pdf 600"
fi

# ── 5. 스크립트 실행 권한 유지 확인 ─────────────────────────
for SH in "$PROJECT_DIR/restart_agent.sh" "$SCRIPT_DIR/security_setup.sh"; do
    if [ -f "$SH" ]; then
        chmod 700 "$SH"
        echo "  [✓] $(basename "$SH")       → 700 (소유자 실행)"
    fi
done

echo "========================================================"
echo "  보안 설정 완료"
echo "========================================================"
