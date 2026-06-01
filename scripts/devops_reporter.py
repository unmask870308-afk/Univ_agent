"""
devops_reporter.py — UnivAgent DevOps PDF 리포터 v2
====================================================
1. system_metrics DB에서 일/주/월 성장 + 쉴드/E2E/토큰 수집
2. 통합 대시보드 차트 (Bar 데이터 성장 + Line 에러/방어, 보조 Y축)
   + ax.table 수치 요약 테이블 (오늘 / 어제 / 이번주 / 이번달)
3. Gemini 5-섹션 CEO 직언 분석
   (경영진 요약 / 데이터 성장 / 토큰 효율 / E2E 평가 / 건강도 등급)
4. fpdf2 로 전문가 PDF 조립 (Gemini 분석 → 차트+테이블)
5. (pdf_경로, 텍스트_요약) 튜플 반환
"""

import asyncio
import os
import sys
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# MUST come before any matplotlib.pyplot import — prevents macOS GUI thread hang
import matplotlib
matplotlib.use("Agg")

# ─────────────────────────────────────────────────────────────
# 의존성 자동 설치
# ─────────────────────────────────────────────────────────────
_REQUIRED = {
    "matplotlib":   "matplotlib",
    "fpdf":         "fpdf2",
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
        subprocess.check_call(
            [_pip_exe, "-m", "pip", "install", _pkg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# 환경 설정
# ─────────────────────────────────────────────────────────────
프로젝트_루트 = Path(__file__).parent.parent
load_dotenv(프로젝트_루트 / ".env")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
ADMIN_CHAT_ID  = os.getenv("ADMIN_TELEGRAM_ID") or os.getenv("ADMIN_CHAT_ID", "")

import token_manager as _tm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("devops_reporter")

_DEVOPS_에러_로그 = 프로젝트_루트 / "data" / "fix_error" / "devops_errors.log"


def _devops_에러_기록(error: Exception, stage: str = "리포트_생성") -> None:
    """예외를 devops_errors.log에 JSONL 형식으로 추가합니다."""
    import traceback as _tb
    entry = {
        "ts":         datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "task":       "DevOps Report",
        "stage":      stage,
        "error_type": type(error).__name__,
        "error_msg":  str(error)[:500],
        "traceback":  _tb.format_exc()[:2000],
    }
    try:
        _DEVOPS_에러_로그.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEVOPS_에러_로그, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# 한국어 폰트 탐색 (Matplotlib + fpdf2 공용)
# ─────────────────────────────────────────────────────────────
_FONT_CANDIDATES = [
    프로젝트_루트 / "data" / "fonts" / "NanumGothic.ttf",
    Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
    Path("/Library/Fonts/NanumGothic.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
]
_KOREAN_TTF: Path | None = next(
    (p for p in _FONT_CANDIDATES if p.exists()), None
)
_MPLRC_FONT = "AppleGothic"   # matplotlib rcParams 폴백 (macOS 기본)


# ─────────────────────────────────────────────────────────────
# 1. DB 데이터 수집 & 집계
# ─────────────────────────────────────────────────────────────

def _메트릭_로드() -> list[dict]:
    """db_manager.시스템_메트릭_조회()로 최근 30일 데이터를 가져옵니다."""
    sys.path.insert(0, str(Path(__file__).parent))
    import db_manager
    rows = db_manager.시스템_메트릭_조회(days=30)
    if not rows:
        db_manager.시스템_스냅샷_저장()
        rows = db_manager.시스템_메트릭_조회(days=30)
    return rows


def _기간별_집계(rows: list[dict]) -> dict:
    """일/주/월 집계 및 최신 현황 딕셔너리를 반환합니다."""
    오늘 = datetime.now().date()
    어제 = 오늘 - timedelta(days=1)

    def _날짜(r: dict):
        try:
            return datetime.strptime(r["date_str"], "%Y-%m-%d").date()
        except Exception:
            return None

    일간 = [r for r in rows if _날짜(r) == 오늘]
    어제_행 = [r for r in rows if _날짜(r) == 어제]
    주간 = [r for r in rows if _날짜(r) and (오늘 - _날짜(r)).days < 7]
    월간 = rows[:]

    def _합산(lst, key):
        return sum(int(r.get(key, 0) or 0) for r in lst)

    def _델타(lst, key) -> int:
        """
        누적 스냅샷 컬럼(seteuk/stats/golden)의 기간 순증가분을 반환합니다.
        각 행이 그날의 총 누적값이므로 합산하면 중복 계산됩니다.
        올바른 방법: 기간 내 마지막 스냅샷 - 첫 스냅샷 (음수면 0으로 보정)
        """
        유효 = [int(r.get(key, 0) or 0) for r in lst if _날짜(r) is not None]
        if not 유효:
            return 0
        if len(유효) == 1:
            return 유효[0]
        return max(유효[-1] - 유효[0], 0)

    def _최신(key):
        return int(rows[-1].get(key, 0) or 0) if rows else 0

    def _최신_str(key):
        return str(rows[-1].get(key, "") or "") if rows else ""

    # 직전 7일 (전주 비교용)
    이전_7일 = [r for r in rows if _날짜(r) and 7 <= (오늘 - _날짜(r)).days < 14]

    return {
        "일간": {
            # 일간은 해당일 스냅샷 1행 → 합산 = 그날의 누적 현황값 (정상)
            "seteuk":       _합산(일간, "seteuk_count"),
            "stats":        _합산(일간, "univ_stats_count"),
            "golden":       _합산(일간, "golden_count"),
            "errors":       _합산(일간, "crawler_errors"),
            "error_count":  _합산(일간, "error_count"),
            "shield":       _합산(일간, "shield_defenses"),
            "tokens":       _합산(일간, "total_tokens"),
        },
        "어제": {
            "seteuk":       _합산(어제_행, "seteuk_count"),
            "stats":        _합산(어제_행, "univ_stats_count"),
            "golden":       _합산(어제_행, "golden_count"),
            "errors":       _합산(어제_행, "crawler_errors"),
            "error_count":  _합산(어제_행, "error_count"),
            "shield":       _합산(어제_행, "shield_defenses"),
            "tokens":       _합산(어제_행, "total_tokens"),
        },
        "주간": {
            # 누적 스냅샷 컬럼(seteuk/stats/golden) → 델타(순증가분)로 표시
            # 이벤트 카운터(errors/shield/tokens) → 합산 유지 (일별 발생량)
            "seteuk":       _델타(주간, "seteuk_count"),
            "stats":        _델타(주간, "univ_stats_count"),
            "golden":       _델타(주간, "golden_count"),
            "errors":       _합산(주간, "crawler_errors"),
            "error_count":  _합산(주간, "error_count"),
            "shield":       _합산(주간, "shield_defenses"),
            "tokens":       _합산(주간, "total_tokens"),
        },
        "이전_주간": {
            "seteuk":   _델타(이전_7일, "seteuk_count"),
        },
        "월간": {
            # 누적 스냅샷 컬럼 → 델타
            "seteuk":       _델타(월간, "seteuk_count"),
            "stats":        _델타(월간, "univ_stats_count"),
            "golden":       _델타(월간, "golden_count"),
            # 이벤트 카운터 → 합산
            "errors":       _합산(월간, "crawler_errors"),
            "error_count":  _합산(월간, "error_count"),
            "shield":       _합산(월간, "shield_defenses"),
            "tokens":       _합산(월간, "total_tokens"),
        },
        "최신_현황": {
            "seteuk":       _최신("seteuk_count"),
            "stats":        _최신("univ_stats_count"),
            "golden":       _최신("golden_count"),
            "errors":       _최신("crawler_errors"),
            "error_count":  _최신("error_count"),
            "shield":       _최신("shield_defenses"),
            "tokens":       _최신("total_tokens"),
            "e2e":          _최신_str("e2e_test_result"),
            "rows":         len(rows),
        },
    }


# ─────────────────────────────────────────────────────────────
# 2. Git 커밋 로그 수집 (최근 24시간)
# ─────────────────────────────────────────────────────────────

def _git_로그_수집() -> str:
    """
    최근 커밋 5건을 '--oneline' 형식으로 수집합니다.
    git 미설치·레포 없음·타임아웃 시 scripts/ 폴더의 최근 수정 파일 3개로 폴백합니다.
    """
    import glob as _glob
    try:
        result = subprocess.run(
            ["git", "log", "-n", "5", "--oneline"],
            cwd=str(프로젝트_루트),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            return "\n".join(f"  • {line.strip()}" for line in lines[:5])
        # git은 성공했지만 커밋 없음 (빈 레포)
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass

    # 폴백: scripts/ 의 최근 수정 .py 파일 3개
    try:
        py_files = sorted(
            _glob.glob(str(프로젝트_루트 / "scripts" / "*.py")),
            key=os.path.getmtime,
            reverse=True,
        )[:3]
        if py_files:
            줄들 = []
            for fp in py_files:
                mtime = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%m/%d %H:%M")
                줄들.append(f"  • [최근 수정] {Path(fp).name}  ({mtime})")
            return "\n".join(줄들)
    except Exception:
        pass
    return "  • 업데이트 내역 없음"


def _git_건수(git_로그: str) -> int:
    """git 로그 문자열에서 커밋 건수를 셉니다."""
    return sum(1 for line in git_로그.split("\n") if line.strip().startswith("•"))


# ─────────────────────────────────────────────────────────────
# 3. 통합 대시보드 차트 + 수치 테이블 생성
# ─────────────────────────────────────────────────────────────

def _차트_생성(rows: list[dict], 집계: dict, 저장_경로: Path) -> Path:
    """
    통합 대시보드 PNG를 생성합니다.

    Layout (GridSpec 2 rows):
      [상단 75%] 그룹 막대(세특/입시통계/골든) + 보조 Y축 라인(에러/쉴드방어)
      [하단 25%] ax.table 수치 요약 (오늘 / 어제 / 이번주 / 이번달)

    Y축 정수 고정: MaxNLocator(integer=True)
    """
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    from matplotlib.ticker import MaxNLocator

    # ── 한국어 폰트 설정 ──────────────────────────────────────
    if _KOREAN_TTF:
        fm.fontManager.addfont(str(_KOREAN_TTF))
        _fname = fm.FontProperties(fname=str(_KOREAN_TTF)).get_name()
        plt.rcParams["font.family"] = _fname
    else:
        plt.rcParams["font.family"] = _MPLRC_FONT
    plt.rcParams["axes.unicode_minus"] = False

    오늘 = datetime.now().date()
    어제 = 오늘 - timedelta(days=1)

    # ── 차트용 데이터 (최근 14일 또는 전체) ───────────────────
    차트_rows = rows[-14:] if len(rows) >= 14 else rows

    def _parse_date(r):
        try:
            return datetime.strptime(r["date_str"], "%Y-%m-%d").date()
        except Exception:
            return None

    유효_rows = [(i, r) for i, r in enumerate(차트_rows) if _parse_date(r)]
    x_idx    = [i for i, _ in enumerate(유효_rows)]
    x_labels = [r["date_str"][5:] for _, (_, r) in enumerate(유효_rows)]  # MM-DD

    def _val(r, key): return int(r.get(key, 0) or 0)

    세특들   = [_val(r, "seteuk_count")    for _, r in 유효_rows]
    통계들   = [_val(r, "univ_stats_count") for _, r in 유효_rows]
    골든들   = [_val(r, "golden_count")     for _, r in 유효_rows]
    에러들   = [_val(r, "crawler_errors")   for _, r in 유효_rows]
    방어들   = [_val(r, "shield_defenses")  for _, r in 유효_rows]

    # ── 색상 팔레트 ───────────────────────────────────────────
    C = {
        "세특":    "#1976D2",
        "통계":    "#388E3C",
        "골든":    "#F57C00",
        "에러":    "#D32F2F",
        "방어":    "#7B1FA2",
        "헤더_bg": "#1565C0",
    }

    # ── Figure & GridSpec ─────────────────────────────────────
    fig = plt.figure(figsize=(14, 11))
    gs  = fig.add_gridspec(2, 1, height_ratios=[3, 1.1], hspace=0.45)
    ax_chart = fig.add_subplot(gs[0])
    ax_tbl   = fig.add_subplot(gs[1])

    fig.suptitle(
        f"UnivAgent 시스템 현황 대시보드  —  {오늘.strftime('%Y년 %m월 %d일')} 기준",
        fontsize=13, fontweight="bold", y=0.99,
    )

    # ── 막대 차트 (세특 / 입시통계 / 골든문서) ────────────────
    n = max(len(x_idx), 1)
    bar_w = max(0.18, min(0.28, 6.0 / n))

    if not x_idx:
        ax_chart.text(0.5, 0.5, "수집 데이터 없음",
                      ha="center", va="center", transform=ax_chart.transAxes, fontsize=13)
    else:
        xs = list(range(len(x_idx)))
        ax_chart.bar([x - bar_w   for x in xs], 세특들, width=bar_w,
                     color=C["세특"], alpha=0.85, label="세특 데이터")
        ax_chart.bar(xs,             통계들, width=bar_w,
                     color=C["통계"], alpha=0.85, label="입시통계")
        ax_chart.bar([x + bar_w   for x in xs], 골든들, width=bar_w,
                     color=C["골든"], alpha=0.85, label="골든문서(PDF)")

        ax_chart.set_xticks(xs)
        ax_chart.set_xticklabels(x_labels, rotation=35, ha="right", fontsize=8)
        ax_chart.set_ylabel("데이터 건수", fontsize=10)
        ax_chart.yaxis.set_major_locator(MaxNLocator(integer=True))

        # 값이 모두 0이면 Y범위 고정 (부동소수 눈금 방지)
        if max(세특들 + 통계들 + 골든들, default=0) == 0:
            ax_chart.set_ylim(0, 5)

        # ── 보조 Y축: 에러(선) + 방어(선) ─────────────────────
        ax2 = ax_chart.twinx()
        ax2.plot(xs, 에러들, "o-",  color=C["에러"], linewidth=2,
                 label="크롤러 에러", markersize=5, zorder=5)
        ax2.plot(xs, 방어들, "s--", color=C["방어"], linewidth=2,
                 label="쉴드방어(429)", markersize=5, zorder=5)
        ax2.set_ylabel("에러 / 방어 횟수", fontsize=10, color=C["에러"])
        ax2.tick_params(axis="y", labelcolor=C["에러"])
        ax2.yaxis.set_major_locator(MaxNLocator(integer=True))
        if max(에러들 + 방어들, default=0) == 0:
            ax2.set_ylim(0, 5)
        ax2.legend(loc="upper right", fontsize=8, framealpha=0.8)

    ax_chart.set_title("데이터 성장(막대)  &  에러/쉴드방어 추이(선)", fontsize=11, fontweight="bold")
    ax_chart.legend(loc="upper left", fontsize=9, framealpha=0.8)
    ax_chart.grid(axis="y", linestyle="--", alpha=0.3)
    ax_chart.spines["top"].set_visible(False)

    # ── 수치 요약 테이블 ──────────────────────────────────────
    일간 = 집계["일간"]
    어제_집계 = 집계["어제"]
    주간 = 집계["주간"]
    월간 = 집계["월간"]

    def _fmt_tok(v): return f"{v//1000:,}K" if v >= 1000 else str(v)

    col_labels = ["지표", "오늘", "어제", "이번주(7일)", "이번달(30일)"]
    e2e_val = 집계["최신_현황"].get("e2e", "") or "미실행"

    # 디스크 사용량
    _disk = shutil.disk_usage("/")
    _disk_used_gb  = _disk.used  / 1024 ** 3
    _disk_total_gb = _disk.total / 1024 ** 3
    _disk_free_gb  = _disk.free  / 1024 ** 3
    _disk_pct      = _disk.used  / _disk.total * 100
    _disk_val = f"{_disk_used_gb:.0f}/{_disk_total_gb:.0f}GB ({_disk_pct:.1f}%)"

    table_rows = [
        ["세특 데이터",    str(일간["seteuk"]),         str(어제_집계["seteuk"]),         str(주간["seteuk"]),         str(월간["seteuk"])],
        ["입시통계",       str(일간["stats"]),          str(어제_집계["stats"]),          str(주간["stats"]),          str(월간["stats"])],
        ["골든문서(PDF)",  str(일간["golden"]),         str(어제_집계["golden"]),         str(주간["golden"]),         str(월간["golden"])],
        ["크롤러 에러",    str(일간["errors"]),         str(어제_집계["errors"]),         str(주간["errors"]),         str(월간["errors"])],
        ["총 에러(전체)",  str(일간["error_count"]),    str(어제_집계["error_count"]),    str(주간["error_count"]),    str(월간["error_count"])],
        ["쉴드방어(429)",  str(일간["shield"]),         str(어제_집계["shield"]),         str(주간["shield"]),         str(월간["shield"])],
        ["토큰 사용량",    _fmt_tok(일간["tokens"]),    _fmt_tok(어제_집계["tokens"]),
                           _fmt_tok(주간["tokens"]),    _fmt_tok(월간["tokens"])],
        ["E2E 결과",       e2e_val, "-", "-", "-"],
        ["디스크 사용량",  _disk_val, "-", "-", f"{_disk_free_gb:.0f}GB 여유"],
    ]

    ax_tbl.axis("off")
    tbl = ax_tbl.table(
        cellText=table_rows,
        colLabels=col_labels,
        cellLoc="center",
        loc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1.0, 1.55)

    # 헤더 행 스타일
    for j in range(len(col_labels)):
        cell = tbl[0, j]
        cell.set_facecolor(C["헤더_bg"])
        cell.set_text_props(color="white", fontweight="bold")

    # 데이터 행 교대 색상 + 첫 열 강조
    for i in range(1, len(table_rows) + 1):
        for j in range(len(col_labels)):
            cell = tbl[i, j]
            if i % 2 == 0:
                cell.set_facecolor("#F0F4FF")
            else:
                cell.set_facecolor("#FFFFFF")
            if j == 0:
                cell.set_text_props(fontweight="bold")
            # 에러/방어 행 강조: 0이 아닌 값은 빨간색
            # 행 인덱스 4=크롤러에러, 5=총에러, 6=쉴드방어 (1-based, 헤더 제외)
            if j > 0 and i in (4, 5, 6):
                try:
                    if int(table_rows[i - 1][j].replace("K", "000").replace(",", "") or "0") > 0:
                        cell.set_text_props(color=C["에러"])
                except (ValueError, AttributeError):
                    pass
            # E2E PASS → 초록, FAIL → 빨강 (E2E는 뒤에서 두 번째 행)
            if i == len(table_rows) - 1 and j == 1:
                if e2e_val == "PASS":
                    cell.set_text_props(color="#2E7D32", fontweight="bold")
                elif e2e_val == "FAIL":
                    cell.set_text_props(color=C["에러"], fontweight="bold")
            # 디스크 사용량 행: 85% 이상이면 경고색
            if i == len(table_rows) and j == 1:
                if _disk_pct >= 85:
                    cell.set_text_props(color=C["에러"], fontweight="bold")

    ax_tbl.set_title("수치 요약 (기간별)", fontsize=10, fontweight="bold", pad=6)

    # ── 저장 ──────────────────────────────────────────────────
    저장_경로.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(str(저장_경로), dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    logger.info(f"[차트] 통합 대시보드 저장 완료: {저장_경로}")
    return 저장_경로


# ─────────────────────────────────────────────────────────────
# 4. Gemini 5-섹션 CEO 직언 분석
# ─────────────────────────────────────────────────────────────

def _Gemini_분석(집계: dict, rows: list[dict]) -> tuple[str, str, str]:
    """
    냉철한 수석 데이터 분석가 페르소나로 6-섹션 CEO 직언 리포트를 생성합니다.

    섹션: 경영진 요약 / 데이터 성장 & 품질 / API 토큰 효율 /
          인프라 & 스토리지 / E2E 테스트 평가 / 종합 건강도 (A~F)

    반환: (pdf용_상세_분석, telegram용_텍스트_요약, 엔진명)
    """
    _실패_응답 = ("[API 키 없음 — 분석 생략]", "분석 데이터 없음", "없음")
    if not GEMINI_API_KEY:
        return _실패_응답

    오늘_str = datetime.now().strftime("%Y년 %m월 %d일")
    최신  = 집계["최신_현황"]
    일간  = 집계["일간"]
    어제  = 집계["어제"]
    주간  = 집계["주간"]
    이전  = 집계["이전_주간"]
    월간  = 집계["월간"]

    def _rate(cur, prev):
        if prev == 0:
            return "데이터 없음 (전주 0건)"
        return f"{((cur - prev) / prev * 100):+.1f}%"

    주간_증감 = _rate(주간["seteuk"], 이전["seteuk"])

    # git 로그
    git_로그 = _git_로그_수집()
    git_건수 = _git_건수(git_로그)

    # 토큰 비용 추정 (Gemini 2.5 Flash 기준: input $0.15/1M, output $0.60/1M)
    총_토큰  = 월간["tokens"]
    추정_비용 = f"${총_토큰 / 1_000_000 * 0.3:.4f}"  # 혼합 단가 근사

    e2e_결과 = 최신.get("e2e", "") or "미실행"

    # 디스크 사용량
    _dsk = shutil.disk_usage("/")
    _dsk_used_gb  = _dsk.used  / 1024 ** 3
    _dsk_total_gb = _dsk.total / 1024 ** 3
    _dsk_free_gb  = _dsk.free  / 1024 ** 3
    _dsk_pct      = _dsk.used  / _dsk.total * 100

    # ── 오늘 vs 어제 순증감 (누적값 기준 정확한 델타) ────────
    _Δ세특   = 일간['seteuk']       - 어제['seteuk']
    _Δ통계   = 일간['stats']        - 어제['stats']
    _Δ골든   = 일간['golden']       - 어제['golden']
    _Δ에러   = 일간['errors']       - 어제['errors']
    _Δ총에러 = 일간['error_count']  - 어제['error_count']
    _Δ쉴드   = 일간['shield']       - 어제['shield']

    데이터_블록 = f"""
=== UnivAgent 운영 현황 ({오늘_str}) ===

[DB 누적 현황 — db_manager.시스템_메트릭_조회() 직접 조회]
- 세특 데이터:     {최신['seteuk']:,}건
- 입시통계:        {최신['stats']:,}건
- 골든문서(PDF):   {최신['golden']:,}건
- 분석 기간:       최근 {최신['rows']}일

[일간 증감 (오늘 누적 vs 어제 누적 → 순증감)]
- 세특:        오늘 {일간['seteuk']:,}건 / 어제 {어제['seteuk']:,}건  →  순증감 {_Δ세특:+,}건
- 입시통계:    오늘 {일간['stats']:,}건  / 어제 {어제['stats']:,}건   →  순증감 {_Δ통계:+,}건
- 골든문서:    오늘 {일간['golden']:,}건 / 어제 {어제['golden']:,}건  →  순증감 {_Δ골든:+,}건
- 크롤러 에러: 오늘 {일간['errors']}건  / 어제 {어제['errors']}건   →  변화 {_Δ에러:+}건
- 총 에러:     오늘 {일간['error_count']}건 / 어제 {어제['error_count']}건 →  변화 {_Δ총에러:+}건
- 쉴드방어:    오늘 {일간['shield']}회  / 어제 {어제['shield']}회   →  변화 {_Δ쉴드:+}회

[주간 집계 (최근 7일, 전주 대비)]
- 세특:          {주간['seteuk']:,}건 ({주간_증감})
- 입시통계:      {주간['stats']:,}건
- 골든문서:      {주간['golden']:,}건
- 크롤러 에러:   {주간['errors']}건 누적
- 총 에러(전체): {주간['error_count']}건 누적
- 쉴드방어:      {주간['shield']}회 누적

[API 사용량 & 비용 효율]
- 이번달 토큰 사용량: {총_토큰:,} 토큰
- 추정 API 비용:      {추정_비용} USD (Gemini 2.5 Flash 혼합 단가 기준)
- 일평균 토큰:        {총_토큰 // max(최신['rows'], 1):,} 토큰/일

[쉴드방어 (429 Rate Limit 방어)]
- 이번달 총 방어: {월간['shield']}회
- 이번주:        {주간['shield']}회
- 오늘:          {일간['shield']}회

[디스크 사용량]
- 총 용량:   {_dsk_total_gb:.0f} GB
- 사용 중:   {_dsk_used_gb:.1f} GB ({_dsk_pct:.1f}%)
- 여유 공간: {_dsk_free_gb:.1f} GB

[E2E 합성 테스트 결과]
- 최신 결과: {e2e_결과}

[최근 소프트웨어 업데이트 (최근 5커밋 / 파일수정 폴백)]
{git_로그}

[최근 7일 에러 추이]
{chr(10).join(f"  {r['date_str']}: 크롤러에러 {int(r.get('crawler_errors',0) or 0)}건 / 총에러 {int(r.get('error_count',0) or 0)}건 / 방어 {int(r.get('shield_defenses',0) or 0)}회" for r in rows[-7:])}
"""

    프롬프트 = f"""CRITICAL: 반드시 전체 리포트를 한국어(한국어로만 작성할 것)로 작성하세요. 영어 문장은 절대 출력하지 마세요.
CRITICAL: 가독성을 극대화하기 위해 적절한 이모지(🚀, 📊, ⚠️ 등)를 사용하고, 줄바꿈을 넉넉히 하며, 핵심 내용은 글머리 기호(-, •)를 사용하여 깔끔하게 정리하세요. 문단이 너무 길지 않게 핵심만 요약하세요.

당신은 감정 없이 팩트와 수치만으로 시스템을 평가하는 냉철하고 날카로운 수석 데이터 분석가입니다.
운영 데이터를 분석하여 CEO에게 직언하는 6-섹션 리포트를 한국어로 작성하세요.

{데이터_블록}

[출력 형식 — 반드시 아래 두 구분자 섹션 포함]

===PDF_ANALYSIS===
## 1. 경영진 요약 (Executive Summary)
(3~4줄, 핵심 수치 포함, 전체 시스템 상태를 한눈에)

## 2. 데이터 성장 & 품질
(불릿 포인트 4~5개: 세특/입시통계/골든문서 증감, 전주 대비 트렌드, 품질 이슈)

## 3. API 토큰 & 비용 효율성
(불릿 포인트 3~4개: 토큰 소비량, 추정 비용, 쉴드방어 빈도가 시사하는 쿼터 압박)

## 4. 인프라 & 스토리지 상태
(불릿 포인트 2~3개: 디스크 사용률, 여유 공간, 85% 초과 시 구체적 경고 및 조치 권고)

## 5. E2E 합성 테스트 평가
(불릿 포인트 3~4개: 최신 결과 해석, AI 파이프라인 품질 신호, 개선 필요 여부)

## 6. 종합 시스템 건강도 등급
(A / B / C / D / F 중 하나, 판정 근거 3줄)
마지막에 반드시 한 줄: "종합 등급: [등급] — [한 줄 평가]"
(디스크 85% 이상이면 등급에 불이익 반영)

===TEXT_SUMMARY===
(Telegram 전송용 초간결 요약 — 6줄 이내, 이모지 포함)
📊 누적: 세특 {최신['seteuk']:,}건 | 입시통계 {최신['stats']:,}건 | PDF {최신['golden']:,}건
📈 오늘 순증감: 세특 {_Δ세특:+,}건 / 통계 {_Δ통계:+,}건 / 에러 {_Δ에러:+}건
🛡 쉴드방어(이번주): {주간['shield']}회 | 토큰(이번달): {총_토큰:,}
🤖 E2E 테스트: (결과)
🔧 최근 업데이트: {git_건수}건
🏆 건강도 등급: (등급) — (한 줄 평)
"""

    try:
        전체, 엔진명 = _tm.generate_text_sync(프롬프트)
        if not 전체:
            return "[LLM 분석 실패 — 모든 티어 소진]", "분석 생성 실패", "없음"

        logger.info(f"[TokenManager] DevOps 분석 생성 완료 ({len(전체)}자, 엔진: {엔진명})")

        if "===TEXT_SUMMARY===" in 전체:
            parts    = 전체.split("===TEXT_SUMMARY===", 1)
            pdf_부분 = parts[0].replace("===PDF_ANALYSIS===", "").strip()
            요약_부분 = parts[1].strip()
        else:
            pdf_부분 = 전체
            요약_부분 = "\n".join(전체.split("\n")[:6])

        return pdf_부분, 요약_부분, 엔진명
    except Exception as e:
        logger.error(f"[TokenManager] DevOps 분석 예외: {e}")
        return f"[LLM 오류: {e}]", "분석 오류", "오류"


# ─────────────────────────────────────────────────────────────
# 5. PDF 조립 (fpdf2 + 한국어 TTF)
# ─────────────────────────────────────────────────────────────

def _PDF_생성(분석_텍스트: str, 차트_경로: Path, pdf_경로: Path, engine_name: str = "AI") -> Path:
    """fpdf2 로 전문가 DevOps 리포트 PDF를 조립합니다 (분석 → 차트+테이블)."""
    from fpdf import FPDF

    오늘 = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")

    class _리포트PDF(FPDF):
        def __init__(self):
            super().__init__()
            self._한글_가능 = False
            if _KOREAN_TTF and _KOREAN_TTF.exists():
                try:
                    self.add_font("Korean", "",  str(_KOREAN_TTF))
                    self.add_font("Korean", "B", str(_KOREAN_TTF))
                    self._한글_가능 = True
                    logger.debug(f"[PDF] 한국어 폰트 로드: {_KOREAN_TTF.name}")
                except Exception as _fe:
                    logger.warning(f"[PDF] 한국어 폰트 로드 실패: {_fe}")

        def _폰트(self, size=11, bold=False):
            style = "B" if bold else ""
            if self._한글_가능:
                self.set_font("Korean", style=style, size=size)
            else:
                self.set_font("Helvetica", style=style, size=size)

        def header(self):
            self._폰트(9)
            self.set_text_color(120, 120, 120)
            self.cell(0, 8, f"UnivAgent DevOps Report v2 — {오늘}",
                      align="R", new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(200, 200, 200)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(3)

        def footer(self):
            self.set_y(-15)
            self._폰트(8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 10, f"Page {self.page_no()} | Confidential — Internal Use Only",
                      align="C")

    pdf = _리포트PDF()
    pdf.set_margins(left=18, top=18, right=18)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ── 타이틀 ───────────────────────────────────────────────
    pdf._폰트(18, bold=True)
    pdf.set_text_color(30, 50, 100)
    pdf.cell(0, 12, "UnivAgent DevOps Report",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf._폰트(11)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 8, f"생성일: {오늘}  |  기밀 (관리자 전용)",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_draw_color(50, 80, 180)
    pdf.set_line_width(0.8)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.set_line_width(0.2)
    pdf.ln(6)

    # ── AI 6-섹션 분석 ───────────────────────────────────────
    pdf._폰트(13, bold=True)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 8, f"AI 시스템 분석 — 6섹션 CEO 직언 리포트 (엔진: {engine_name})",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    _분석_시작_y = pdf.get_y()
    pdf._폰트(11)
    pdf.set_text_color(40, 40, 40)

    _유효폭 = pdf.w - pdf.l_margin - pdf.r_margin

    for 줄 in 분석_텍스트.split("\n"):
        줄 = 줄.rstrip()
        if not 줄:
            pdf.ln(5)
            continue
        # 섹션 헤더 (## 또는 # 단독)
        if 줄.lstrip().startswith("#"):
            pdf.ln(4)
            pdf._폰트(12, bold=True)
            pdf.set_text_color(30, 50, 130)
            pdf.multi_cell(
                _유효폭, 7,
                줄.lstrip("# ").strip(), align="L",
                new_x="LMARGIN", new_y="NEXT",
            )
            pdf.ln(2)
            pdf._폰트(11)
            pdf.set_text_color(40, 40, 40)
            continue
        # 강조 줄 (▶ 또는 이모지로 시작)
        stripped = 줄.strip()
        _이모지_시작 = len(stripped) > 0 and ord(stripped[0]) > 127
        is_arrow  = stripped.startswith("▶")
        is_bullet = stripped.startswith(("•", "-", "*", "·"))
        if is_arrow:
            pdf.ln(3)
            pdf._폰트(11, bold=True)
            pdf.set_text_color(20, 70, 150)
            pdf.multi_cell(_유효폭, 6.5, stripped, align="L",
                           new_x="LMARGIN", new_y="NEXT")
            pdf._폰트(11)
            pdf.set_text_color(40, 40, 40)
        elif is_bullet:
            pdf.set_x(pdf.l_margin + 5)
            pdf.multi_cell(_유효폭- 5, 6.5, stripped, align="L",
                           new_x="LMARGIN", new_y="NEXT")
        elif _이모지_시작:
            pdf.ln(1)
            pdf.multi_cell(_유효폭, 6.5, stripped, align="L",
                           new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.multi_cell(_유효폭, 6.5, stripped, align="L",
                           new_x="LMARGIN", new_y="NEXT")

    # 분석 박스 테두리
    pdf.set_draw_color(180, 190, 220)
    pdf.rect(pdf.l_margin, _분석_시작_y - 2,
             pdf.w - pdf.l_margin - pdf.r_margin,
             pdf.get_y() - _분석_시작_y + 4)
    pdf.ln(8)

    # ── 대시보드 차트 + 테이블 이미지 (새 페이지에서 시작) ──
    if 차트_경로.exists():
        pdf.add_page()
        pdf._폰트(13, bold=True)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 8, "시스템 현황 대시보드 차트 & 수치 요약 테이블",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.image(str(차트_경로), x=pdf.l_margin, y=pdf.get_y(), w=pdf.epw)
        logger.info("[PDF] 대시보드 차트 이미지 삽입 완료")
    else:
        pdf._폰트(10)
        pdf.set_text_color(150, 50, 50)
        pdf.cell(0, 8, "[차트 이미지 생성 실패]", new_x="LMARGIN", new_y="NEXT")

    # ── 저장 ─────────────────────────────────────────────────
    pdf_경로.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(pdf_경로))
    logger.info(f"[PDF] 저장 완료: {pdf_경로}")
    return pdf_경로


# ─────────────────────────────────────────────────────────────
# 6. 메인 실행 함수 (telegram_agent.py 에서 import 후 호출)
# ─────────────────────────────────────────────────────────────

def 리포트_생성() -> tuple[Path | None, str]:
    """
    DevOps PDF 리포트를 생성하고 (pdf_경로, 텍스트_요약) 튜플을 반환합니다.
    실패 시 (None, 오류메시지) 반환 — 봇을 블로킹하지 않음.
    """
    오늘 = datetime.now()
    날짜_폴더 = 오늘.strftime("%Y-%m")
    날짜_파일 = 오늘.strftime("%Y-%m-%d")
    pdf_경로 = (
        프로젝트_루트
        / "data" / "maintenance_reports" / 날짜_폴더
        / f"{날짜_파일}_DevOps_Report.pdf"
    )

    logger.info(f"[리포터] DevOps 리포트 v2 생성 시작 → {pdf_경로.name}")

    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import db_manager

        # ── 차트 직전: snapshot_daily_metrics() 로 최신 DB 현황 반영 ──
        logger.info("[리포터] snapshot_daily_metrics() 호출 — 최신 카운트 반영 중...")
        db_manager.snapshot_daily_metrics()

        # ── 스냅샷 후 데이터 재조회 (차트에 최신 값 반영) ────
        rows = db_manager.시스템_메트릭_조회(days=30)
        집계  = _기간별_집계(rows)
        logger.info(f"[리포터] 메트릭 {len(rows)}일치 로드 완료")

        # ── 차트 + 테이블 생성 ───────────────────────────────
        차트_경로 = Path(tempfile.mkdtemp()) / "devops_dashboard.png"
        _차트_생성(rows, 집계, 차트_경로)

        # ── AI 6-섹션 분석 ───────────────────────────────────
        logger.info("[리포터] AI 6-섹션 분석 생성 중...")
        pdf_분석, 텍스트_요약, 엔진명 = _Gemini_분석(집계, rows)

        # ── PDF 조립 ────────────────────────────────────────
        logger.info(f"[리포터] PDF 조립 중... (엔진: {엔진명})")
        _PDF_생성(pdf_분석, 차트_경로, pdf_경로, engine_name=엔진명)

        try:
            차트_경로.unlink(missing_ok=True)
        except Exception:
            pass

        logger.info(f"[리포터] 완료 ✅ → {pdf_경로}")
        return pdf_경로, 텍스트_요약

    except Exception as e:
        logger.error(f"[리포터] 리포트 생성 실패: {e}", exc_info=True)
        _devops_에러_기록(e, stage="리포트_생성")
        return None, f"리포트 생성 실패: {str(e)[:100]}"


# ─────────────────────────────────────────────────────────────
# 7. Telegram 발송 함수
# ─────────────────────────────────────────────────────────────

async def send_telegram_report(pdf_path: Path, bot_token: str, chat_id: str) -> bool:
    """
    완성된 DevOps PDF를 관리자 Telegram 채팅으로 비동기 발송합니다.
    asyncio.run()으로 호출하여 동기 컨텍스트와 브리징합니다.
    반환: 성공 여부 (bool)
    """
    from telegram import Bot

    if not bot_token:
        print("[WARN] TELEGRAM_TOKEN이 설정되지 않았습니다. 발송을 건너뜁니다.")
        logger.warning("[Telegram] TELEGRAM_TOKEN 미설정 — 발송 건너뜀")
        return False

    if not chat_id:
        print("[WARN] ADMIN_CHAT_ID가 설정되지 않았습니다. 발송을 건너뜁니다.")
        logger.warning("[Telegram] ADMIN_CHAT_ID 미설정 — 발송 건너뜀")
        return False

    try:
        bot = Bot(token=bot_token)
        with open(pdf_path, "rb") as doc:
            await bot.send_document(
                chat_id=int(chat_id),
                document=doc,
                caption="📊 [UnivAgent] 재부팅 완료: 시스템 데브옵스 리포트가 발급되었습니다.",
            )
        print("[INFO] 텔레그램 발송 성공!")
        logger.info(f"[Telegram] DevOps PDF 발송 완료: chat_id={chat_id}")
        return True
    except Exception as e:
        print(f"[ERROR] 텔레그램 발송 실패: {e}")
        logger.error(f"[Telegram] 발송 예외: {type(e).__name__}: {e}")
        _devops_에러_기록(e, stage="telegram_발송")
        return False


# ─────────────────────────────────────────────────────────────
# CLI 직접 실행 지원
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse as _argparse
    _parser = _argparse.ArgumentParser(description="UnivAgent DevOps 리포터")
    _parser.add_argument(
        "--send-telegram", action="store_true",
        help="리포트 생성 후 관리자 Telegram 채팅으로 PDF 자동 발송",
    )
    _args = _parser.parse_args()

    try:
        pdf_경로, 텍스트_요약 = 리포트_생성()
    except Exception as _e:
        _devops_에러_기록(_e, stage="__main__")
        pdf_경로, 텍스트_요약 = None, f"치명적 오류: {_e}"

    # subprocess 호출자(telegram_agent.py)가 파싱할 수 있는 JSON 마커를 stdout에 출력
    _result_line = "DEVOPS_RESULT:" + json.dumps(
        {"pdf": str(pdf_경로) if pdf_경로 else None, "summary": 텍스트_요약},
        ensure_ascii=False,
    )
    print(_result_line, flush=True)

    if pdf_경로:
        print(f"\n✅ 리포트 저장 완료:\n  {pdf_경로}")
        print(f"\n[텍스트 요약]\n{텍스트_요약}")

        if _args.send_telegram:
            print("\n📨 Telegram 발송 중...")
            if not TELEGRAM_TOKEN:
                print("[WARN] TELEGRAM_TOKEN이 설정되지 않았습니다. 발송을 건너뜁니다.")
            elif not ADMIN_CHAT_ID:
                print("[WARN] ADMIN_CHAT_ID가 설정되지 않았습니다. 발송을 건너뜁니다.")
            else:
                ok = asyncio.run(send_telegram_report(pdf_경로, TELEGRAM_TOKEN, ADMIN_CHAT_ID))
                if ok:
                    print("✅ Telegram 발송 완료")
                else:
                    print("⚠️  Telegram 발송 실패 (devops_errors.log 확인)")
    else:
        print(f"\n❌ 리포트 생성 실패: {텍스트_요약}")
        sys.exit(1)
