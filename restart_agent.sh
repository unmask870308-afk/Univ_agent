#!/usr/bin/env bash
# restart_agent.sh — UnivAgent 전체 시스템 재시작
#
# 기동 순서: 보안 설정 → 기존 프로세스 종료 → .env 로드 → DB 정리
#            → pdf_collector → telegram_agent → web_dashboard (Streamlit)
#
# 사용법:
#   ./restart_agent.sh              # 전체 재시작 (기본)
#   ./restart_agent.sh --keep-logs  # fix_error 로그 유지
#   ./restart_agent.sh --no-dashboard
#   ./restart_agent.sh --no-collector
#   ./restart_agent.sh --help
#
# Ollama: 이 스크립트는 ollama serve 를 종료/기동하지 않습니다.
#         별도 터미널에서 `ollama serve` 가 떠 있어야 크롤링·대시보드 로컬 AI가 동작합니다.

set -euo pipefail

# macOS에서 `sh restart_agent.sh` 는 bash 기능(${!var} 등)이 깨짐 → bash로 재실행
if [ -z "${BASH_VERSION:-}" ]; then
    exec /usr/bin/env bash "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"
ENV_FILE="$SCRIPT_DIR/.env"
LOG_DIR="$SCRIPT_DIR/data/logs"
FIX_ERROR_DIR="$SCRIPT_DIR/data/fix_error"
PID_DIR="$SCRIPT_DIR/data/pids"
SECURITY_SCRIPT="$SCRIPT_DIR/scripts/security_setup.sh"
DB_FILE="$SCRIPT_DIR/data/admissions_agent.db"

STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
START_COLLECTOR=1
START_DASHBOARD=1
KEEP_LOGS=0

usage() {
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --keep-logs)     KEEP_LOGS=1 ;;
        --no-dashboard)  START_DASHBOARD=0 ;;
        --no-collector)  START_COLLECTOR=0 ;;
        -h|--help)       usage ;;
        *)
            echo "[오류] 알 수 없는 옵션: $1  (--help 참고)"
            exit 1
            ;;
    esac
    shift
done

mask_key() {
    local v="${1:-}"
    if [ -z "$v" ]; then
        echo "(미설정)"
    else
        echo "${v:0:12}..."
    fi
}

# .env 안전 로드 (따옴표·= 포함 값 지원, API 키는 항상 덮어씀)
load_env_file() {
    local overwrite_keys=" GEMINI_API_KEY GROQ_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_TOKEN ADMIN_TELEGRAM_ID "
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}}"
        line="${line%"${line##*[![:space:]]}"}}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" != *"="* ]] && continue
        local key="${line%%=*}"
        local value="${line#*=}"
        key="${key#"${key%%[![:space:]]*}"}}"
        key="${key%"${key##*[![:space:]]}"}}"
        value="${value#"${value%%[![:space:]]*}"}}"
        value="${value%"${value##*[![:space:]]}"}}"
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        [[ -z "$key" ]] && continue
        # 변수명은 영문·숫자·_ 만 허용 (잘못된 .env 줄 방어)
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
            echo "  [!] .env 무시 (잘못된 변수명): ${key:0:40}"
            continue
        }
        local do_export=0
        if [[ "$overwrite_keys" == *" $key "* ]]; then
            do_export=1
        elif [ -z "$(printenv "$key" 2>/dev/null || true)" ]; then
            do_export=1
        fi
        if [ "$do_export" -eq 1 ]; then
            printf -v "$key" '%s' "$value"
            export "$key"
        fi
    done < "$ENV_FILE"
}

stop_pidfile() {
    local pidfile="$1"
    local label="$2"
    if [ -f "$pidfile" ]; then
        local old_pid
        old_pid=$(cat "$pidfile" 2>/dev/null || true)
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            kill "$old_pid" 2>/dev/null && echo "  PID $old_pid 종료 ($label)" || true
            sleep 1
            kill -9 "$old_pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
    fi
}

check_process() {
    local pid="$1"
    local name="$2"
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        echo "  ✅ $name 기동 확인 (PID=$pid)"
        return 0
    fi
    echo "  ❌ $name 기동 실패"
    return 1
}

echo "========================================================"
echo "  UnivAgent 전체 시스템 재시작"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================================"

# ── 0. 보안 설정 ─────────────────────────────────────────────
echo "[0/8] 보안 설정 적용 중..."
if [ -f "$SECURITY_SCRIPT" ]; then
    bash "$SECURITY_SCRIPT"
else
    echo "  [!] security_setup.sh 없음"
    [ -f "$ENV_FILE" ] && chmod 600 "$ENV_FILE" && echo "  .env → 600"
fi
[ -f "$DB_FILE" ] && chmod 600 "$DB_FILE"

# ── 1. 기존 프로세스 종료 ────────────────────────────────────
echo "[1/8] 기존 UnivAgent 프로세스 종료 중..."

stop_pidfile "$PID_DIR/pdf_collector.pid"   "pdf_collector"
stop_pidfile "$PID_DIR/telegram_agent.pid"  "telegram_agent"
stop_pidfile "$PID_DIR/web_dashboard.pid"   "web_dashboard"

pkill -f "scripts/telegram_agent.py" 2>/dev/null && echo "  telegram_agent 잔여 종료" || true
pkill -f "scripts/pdf_collector.py"  2>/dev/null && echo "  pdf_collector 잔여 종료"  || true
pkill -f "streamlit run scripts/web_dashboard.py" 2>/dev/null && echo "  web_dashboard 잔여 종료" || true
pkill -f "streamlit run.*web_dashboard" 2>/dev/null || true
sleep 2

# ── 2. 포트 정리 ─────────────────────────────────────────────
echo "[2/8] 포트 정리 (Telegram webhook / Streamlit)..."
for PORT in 8443 8080 "$STREAMLIT_PORT"; do
    PIDS=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "  포트 $PORT → PID $PIDS 종료"
        kill -9 $PIDS 2>/dev/null || true
    else
        echo "  포트 $PORT 여유"
    fi
done

# ── 3. 가상환경 ──────────────────────────────────────────────
echo "[3/8] 가상환경 활성화..."
if [ ! -f "$VENV/bin/activate" ]; then
    echo "  [오류] $VENV 없음 — python3 -m venv venv && pip install -r requirements.txt"
    exit 1
fi
# shellcheck source=/dev/null
source "$VENV/bin/activate"
PY="$VENV/bin/python3"
echo "  Python: $($PY --version 2>&1) ($PY)"

# ── 4. .env 로드 ─────────────────────────────────────────────
echo "[4/8] .env 환경변수 로드..."
if [ ! -f "$ENV_FILE" ]; then
    echo "  [오류] $ENV_FILE 없음"
    exit 1
fi
load_env_file

export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-${TELEGRAM_TOKEN:-}}"
export TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"

echo "  TELEGRAM_TOKEN   : $(mask_key "${TELEGRAM_TOKEN:-}")"
echo "  GEMINI_API_KEY   : $(mask_key "${GEMINI_API_KEY:-}")  (코드 수정·자가치유)"
echo "  GROQ_API_KEY     : $(mask_key "${GROQ_API_KEY:-}")    (크롤링 1순위)"
echo "  엔진 정책        : crawl=Groq→Ollama | code=Gemini"

# ── 5. Ollama 연결 점검 (기동하지 않음) ──────────────────────
echo "[5/8] Ollama 로컬 서버 점검..."
if curl -sf "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; then
    echo "  ✅ Ollama 응답 OK (http://localhost:11434)"
else
    echo "  ⚠️  Ollama 미응답 — 별도 터미널에서 실행하세요:"
    echo "      ollama serve"
    echo "  (Groq 실패 시 크롤링·대시보드 로컬 AI는 Ollama에 의존합니다)"
fi

# ── 6. 디렉터리·로그·DB ──────────────────────────────────────
echo "[6/8] 디렉터리 및 DB 준비..."
mkdir -p "$LOG_DIR" "$FIX_ERROR_DIR" "$PID_DIR" "$LOG_DIR/users"

if [ "$KEEP_LOGS" -eq 0 ]; then
    : > "$FIX_ERROR_DIR/crawler_errors.log"
    : > "$FIX_ERROR_DIR/telegram_errors.log"
    : > "$FIX_ERROR_DIR/test_errors.log"
    : > "$FIX_ERROR_DIR/devops_errors.log"
    echo "  fix_error/*.log 초기화 (--keep-logs 로 유지 가능)"
else
    echo "  fix_error 로그 유지 (--keep-logs)"
fi

# 에러 분석 체크포인트 — 재시작 이후 신규 ERROR만 대시보드 LLM 분석
echo "  에러 분석 기준점 갱신 (재시작 이후만 집계)..."
"$PY" -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/scripts')
import error_checkpoint
p = error_checkpoint.mark_restart('restart_agent.sh')
print('  →', p.name)
" 2>/dev/null || echo "  [!] 체크포인트 기록 실패 (무시하고 계속)"

UNIV_COUNT=$("$PY" -c "
import sys; sys.path.insert(0,'$SCRIPT_DIR/scripts')
import db_manager
print(db_manager.get_covered_universities_count())
" 2>/dev/null || echo "?")
SETEUK_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM successful_seteuks;" 2>/dev/null || echo "0")
GOLDEN_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM verified_golden_records;" 2>/dev/null || echo "0")

echo "[6-B] DB 최적화..."
"$PY" -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/scripts')
import db_manager
db_manager.optimize_database()
" && echo "  DB VACUUM/optimize 완료" || echo "  [!] DB 최적화 실패 (계속 진행)"

# ── 6-C. Ollama 야간 자율 트레이닝 ──────────────────────────────
echo "[6-C] Ollama 야간 자율 트레이닝 (golden_dataset → univagent-expert)..."
"$PY" "$SCRIPT_DIR/scripts/nightly_train.py" --limit 30 \
    && echo "🧠 Nightly Build Complete: Ollama has evolved using today's feedback!" \
    || echo "  [!] 야간 트레이닝 실패 — 계속 진행 (재시작은 차단하지 않음)"

# ── 6-D. 일일 백업 ────────────────────────────────────────────
echo "[6-D] 일일 백업 생성 중..."
python3 "$SCRIPT_DIR/scripts/daily_backup.py" \
    && echo "✅ Daily Backup completed." \
    || echo "  [!] 백업 실패 — 계속 진행 (재시작은 차단하지 않음)"

echo ""
echo "========================================="
echo "  UnivAgent System Boot Sequence"
echo "========================================="
echo "  DB: 대학 ${UNIV_COUNT}개 | 세특 ${SETEUK_COUNT} | Golden ${GOLDEN_COUNT}"
echo "========================================="
echo ""

FAILED=0

# ── 7-A. pdf_collector ───────────────────────────────────────
if [ "$START_COLLECTOR" -eq 1 ]; then
    echo "[7/8] pdf_collector.py 기동..."
    nohup "$PY" "$SCRIPT_DIR/scripts/pdf_collector.py" \
        >> "$LOG_DIR/collector_nohup.log" 2>&1 &
    PDF_PID=$!
    echo "$PDF_PID" > "$PID_DIR/pdf_collector.pid"
    check_process "$PDF_PID" "pdf_collector" || { FAILED=1; echo "     tail -f $LOG_DIR/collector_nohup.log"; }
else
    echo "[7/8] pdf_collector 건너뜀 (--no-collector)"
    PDF_PID="-"
fi

# ── 7-B. telegram_agent ──────────────────────────────────────
echo "[7/8] telegram_agent.py 기동..."
nohup "$PY" "$SCRIPT_DIR/scripts/telegram_agent.py" \
    >> "$LOG_DIR/telegram_nohup.log" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$PID_DIR/telegram_agent.pid"
check_process "$BOT_PID" "telegram_agent" || { FAILED=1; echo "     tail -f $LOG_DIR/telegram_nohup.log"; }

# ── 8. Streamlit MLOps 대시보드 ──────────────────────────────
DASH_PID="-"
if [ "$START_DASHBOARD" -eq 1 ]; then
    echo "[8/8] web_dashboard.py (Streamlit) 기동 — 포트 $STREAMLIT_PORT ..."
    nohup "$VENV/bin/streamlit" run "$SCRIPT_DIR/scripts/web_dashboard.py" \
        --server.port "$STREAMLIT_PORT" \
        --server.headless true \
        >> "$LOG_DIR/dashboard_nohup.log" 2>&1 &
    DASH_PID=$!
    echo "$DASH_PID" > "$PID_DIR/web_dashboard.pid"
    sleep 4
    if kill -0 "$DASH_PID" 2>/dev/null; then
        echo "  ✅ web_dashboard 기동 확인 (PID=$DASH_PID)"
        echo "     http://localhost:$STREAMLIT_PORT"
    else
        echo "  ❌ web_dashboard 기동 실패"
        echo "     tail -f $LOG_DIR/dashboard_nohup.log"
        FAILED=1
    fi
else
    echo "[8/8] web_dashboard 건너뜀 (--no-dashboard)"
fi

echo ""
echo "========================================================"
if [ "$FAILED" -eq 0 ]; then
    echo "  ✅ 재시작 완료"
else
    echo "  ⚠️  일부 서비스 기동 실패 — 위 로그 확인"
fi
echo ""
echo "  프로세스:"
[ "$START_COLLECTOR" -eq 1 ] && echo "    pdf_collector   PID=$PDF_PID"
echo "    telegram_agent  PID=$BOT_PID"
[ "$START_DASHBOARD" -eq 1 ] && echo "    web_dashboard   PID=$DASH_PID  (http://localhost:$STREAMLIT_PORT)"
echo ""
echo "  PID 파일: $PID_DIR/"
echo ""
echo "  로그:"
echo "    tail -f $LOG_DIR/telegram_nohup.log"
echo "    tail -f $LOG_DIR/collector_nohup.log"
echo "    tail -f $LOG_DIR/dashboard_nohup.log"
echo "    tail -f $LOG_DIR/http_network.log"
echo ""
echo "  재시작:"
echo "    cd $SCRIPT_DIR && sh restart_agent.sh"
echo "========================================================"

# ── DevOps 리포트 생성 & Telegram 발송 (백그라운드) ─────────
echo ""
echo "📊 DevOps 리포트를 생성하고 텔레그램으로 발송합니다..."
mkdir -p "$LOG_DIR"
# Run in background so it doesn't block the terminal
nohup "$PY" "$SCRIPT_DIR/scripts/devops_reporter.py" --send-telegram \
    > "$LOG_DIR/devops_nohup.log" 2>&1 &
echo "  DevOps 리포터 PID=$! (백그라운드 실행)"
echo "  tail -f $LOG_DIR/devops_nohup.log"

exit "$FAILED"
