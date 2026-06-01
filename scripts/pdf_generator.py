"""
pdf_generator.py — UnivAgent 프리미엄 PDF 처방전 렌더러 v3
===========================================================
AI 응답의 [섹션 A~D] / [팩트 체크] 태그를 파싱하여
6-섹션 고품질 레이아웃으로 렌더링합니다.

섹션 구조:
    [섹션 A] 관심대학 지원 가능성 정밀 진단   (분석 텍스트 + 제브라 테이블)
    [섹션 B] 성적대별 대안 대학 및 전형 추천   (분석 텍스트 + 제브라 테이블)
    [섹션 C] 성적 향상 시나리오 3가지         (컬러 시나리오 박스)
    [섹션 D] 세특 탐구 보고서 레시피 2가지     (navy 사이드바 박스)
    [팩트 체크] 입시 규정 경고                (주황 테두리 박스)
    CTA 박스 (정적 — 상세 성적 요청)
    [부록] 2028+ 대입 트렌드 가이드           (정적 회색 박스)

공개 API (기존과 동일):
    generate_pdf_async(title, content_lines, output_path, *, doc_type, source, metadata)
    generate_pdf_sync (title, content_lines, output_path, *, doc_type, source, metadata)
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_루트    = Path(__file__).resolve().parent.parent
_DB_경로 = _루트 / "data" / "admissions_agent.db"

_FONT_CANDIDATES = [
    _루트 / "data" / "fonts" / "NanumGothic.ttf",
    Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
    Path("/Library/Fonts/NanumGothic.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
]
_KOREAN_TTF: Path | None = next((p for p in _FONT_CANDIDATES if p.exists()), None)

# ─────────────────────────────────────────────────────────────
# 색상 팔레트
# ─────────────────────────────────────────────────────────────
_NAVY     = (30,  50,  130)
_NAVY_DK  = (20,  35,   90)
_GRAY     = (80,  80,   80)
_GRAY_LT  = (150, 150, 150)
_LIGHT_BG = (245, 247, 252)
_ALT_ROW  = (237, 241, 251)
_WHITE    = (255, 255, 255)
_RED      = (200,  50,  50)
_GREEN    = ( 30, 140,  60)
_BLUE_TXT = ( 30,  90, 180)
_ORANGE   = (200, 100,  20)
_CTA_BG   = (235, 248, 255)
_CTA_BD   = ( 30, 100, 180)
_APP_BG   = (245, 245, 245)
_APP_BD   = (180, 180, 180)

_SCN_COLORS = [
    ( 30, 100, 200),   # 시나리오 1 — 파랑
    ( 30, 140,  60),   # 시나리오 2 — 초록
    (120,  50, 180),   # 시나리오 3 — 보라
]

# ─────────────────────────────────────────────────────────────
# 정적 부록 텍스트
# ─────────────────────────────────────────────────────────────
_APPENDIX_TEXT = """\
[제도 변화]
 - 2028 수능 개편: 선택과목 폐지, 통합형 출제 전환 (수학·탐구 구조 변화)
 - 학생부 간소화: 수상경력·독서활동 대입 미반영 확대 → 세특 중요성 상승
 - 의대·약대 증원: 이공계 경쟁률 일부 완화 예상, 의학계열 경쟁은 여전히 치열
 - 무전공 입학: 주요 대학 확대 추세 → 입학 후 전공 선택 가능 비율 증가

[고2 지금 해야 할 일]
 1) 내신 취약 과목 1개 선정 → 2학기 내 0.5등급 향상 목표 수립
 2) 이번 학기 세특 주제 1개 착수 (위 섹션 D 처방전 참고)
 3) 수능 기출 오답 노트 주 2회 작성 시작 (국어·수학 우선)

[고3 준비 로드맵]
 1) 수시 전략 확정: 내신 3.0 이상 → 교과전형 집중 / 이하 → 학종 병행
 2) 수능 최저 공략: 강점 과목 2개 집중, 영어 절대평가 2등급 사수
 3) 정시 비율 결정: 6월·9월 모의평가 결과 후 수시:정시 비율 최종 확정

[자주 하는 실수 TOP 3]
 1) 내신·수능 동시 방치 → 수능 최저 미충족으로 수시 전원 불합격
 2) 세특 주제 착수 시기 늦추기 → 고3 1학기 이전 주제 확정 필수
 3) 상향 지원에만 집중 → 안정 대학 1~2개를 반드시 포함해야 함\
"""

_CTA_BODY = (
    "현재 전체 평균 내신만으로 분석되었습니다. "
    "1학년 1학기/2학기, 2학년 1학기 등 세부 학기별 성적표 사진을 "
    "텔레그램 채팅창에 추가로 올려주시면, AI가 성적 상승/하락 추이를 파악하여 "
    "학업 발전성을 반영한 훨씬 더 정교한 2차 처방전을 발급해 드립니다."
)


# ─────────────────────────────────────────────────────────────
# DB DDL & 헬퍼
# ─────────────────────────────────────────────────────────────
_DDL_GOLDEN_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS golden_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL DEFAULT '',
    file_path   TEXT    NOT NULL UNIQUE,
    doc_type    TEXT    NOT NULL DEFAULT 'PDF',
    page_count  INTEGER NOT NULL DEFAULT 0,
    source      TEXT    NOT NULL DEFAULT '',
    metadata    TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_golden_docs_created
    ON golden_documents(created_at);
"""


def _db_conn() -> sqlite3.Connection:
    _DB_경로.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_경로), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL_GOLDEN_DOCUMENTS)
    conn.commit()
    return conn


def _insert_golden_document_sync(
    title: str, file_path: str, doc_type: str,
    page_count: int, source: str, metadata: dict,
) -> int:
    conn = _db_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO golden_documents "
            "(title, file_path, doc_type, page_count, source, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(title)[:500], str(file_path), str(doc_type)[:50],
             int(page_count), str(source)[:200],
             json.dumps(metadata, ensure_ascii=False)),
        )
        conn.commit()
        record_id = cur.lastrowid or 0
        logger.info(f"[PDF] 골든문서 DB 저장: id={record_id}, file={Path(file_path).name}")
        return record_id
    except Exception as e:
        logger.error(f"[PDF] DB 저장 실패: {e}")
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# 섹션 파서
# ─────────────────────────────────────────────────────────────

def _parse_sections(content_lines: list[str]) -> dict[str, str]:
    """
    content_lines 를 이어붙인 텍스트에서 섹션 태그로 분리합니다.

    신규 형식 (우선):  [섹션 A] ~ [섹션 D], [팩트 체크]
    구형 형식 (폴백):  [SECTION_OVERVIEW], [SECTION_TABLE], [SECTION_SETEUK], [SECTION_FACTCHECK]

    반환: {"A": "", "B": "", "C": "", "D": "", "FACTCHECK": ""}
    """
    sections: dict[str, str] = {"A": "", "B": "", "C": "", "D": "", "FACTCHECK": ""}
    text = "\n".join(content_lines)

    # 태그 내부 공백 정규화
    text = re.sub(r"\[섹션\s+([A-D])\]", r"[섹션\1]", text)
    text = re.sub(r"\[팩트\s*체크\]", "[팩트체크]", text)

    # ── 신규 Korean 태그 파싱 (라인 단위) ────────────────────
    _new_tag = re.compile(r"^\[(섹션([A-D])|팩트체크)\]", re.UNICODE)
    current_key: str | None = None
    buf: list[str] = []
    found_new = False

    for line in text.splitlines():
        m = _new_tag.match(line.strip())
        if m:
            found_new = True
            if current_key is not None:
                sections[current_key] = "\n".join(buf).strip()
            buf = []
            current_key = m.group(2) if m.group(2) else "FACTCHECK"
        elif current_key is not None:
            buf.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(buf).strip()

    if found_new:
        return sections

    # ── 구형 [SECTION_*] 폴백 ────────────────────────────────
    _old_tag = re.compile(
        r"\[(SECTION_OVERVIEW|SECTION_TABLE|SECTION_SETEUK|SECTION_FACTCHECK)\]"
    )
    old_parts = _old_tag.split(text)
    if len(old_parts) > 1:
        overview = table = seteuk = factcheck = ""
        i = 1
        while i < len(old_parts) - 1:
            tag, body = old_parts[i], old_parts[i + 1].strip()
            if tag == "SECTION_OVERVIEW":
                overview = body
            elif tag == "SECTION_TABLE":
                table = body
            elif tag == "SECTION_SETEUK":
                seteuk = body
            elif tag == "SECTION_FACTCHECK":
                factcheck = body
            i += 2
        # 구형 → 신규 매핑
        if overview or table:
            sections["A"] = (overview + "\n" + table).strip()
        sections["D"] = seteuk
        sections["FACTCHECK"] = factcheck
        return sections

    # ── 최후 폴백: 전체 텍스트 → 섹션 A ─────────────────────
    sections["A"] = text.strip()
    return sections


def _split_text_and_csv(raw: str) -> tuple[str, str]:
    """
    섹션 본문에서 분석 텍스트와 CSV 테이블 부분을 분리합니다.
    '대학명'으로 시작하는 줄 또는 쉼표 3개 이상인 줄부터 CSV 로 간주합니다.
    """
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        # CSV 헤더 행
        if s.startswith("대학명") and "," in s:
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
        # CSV 데이터 행 (쉼표 3개 이상, 괄호·점으로 끝나지 않는 행)
        if (s.count(",") >= 3 and len(s) > 8
                and not s.startswith("(") and not s.startswith("•")):
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
    return raw.strip(), ""


def _parse_csv_table(csv_text: str) -> tuple[list[str], list[list[str]]]:
    """CSV 텍스트를 (헤더, 데이터 행) 으로 파싱합니다."""
    lines = [ln.strip() for ln in csv_text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if "," in ln]
    if not lines:
        return [], []
    reader = csv.reader(io.StringIO("\n".join(lines)))
    rows   = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []
    header = [c.strip() for c in rows[0]]
    data   = [[c.strip() for c in row] for row in rows[1:] if row]
    return header, data


# ─────────────────────────────────────────────────────────────
# PDF 렌더러
# ─────────────────────────────────────────────────────────────

def _render_pdf_sync(
    title: str,
    content_lines: list[str],
    output_path: Path,
    metadata: dict | None = None,
) -> int:
    """구조적 섹션 태그를 파싱하여 6-섹션 프리미엄 PDF 를 렌더링합니다."""
    from fpdf import FPDF

    meta   = metadata or {}
    major  = str(meta.get("major",  "미입력"))
    grade  = str(meta.get("grade",  "미입력"))
    mock   = str(meta.get("mock",   "미입력"))
    engine = str(meta.get("engine", "AI"))

    sections = _parse_sections(content_lines)

    # ──────────────────────────────────────────────────────────
    # FPDF 서브클래스
    # ──────────────────────────────────────────────────────────
    class _PDF(FPDF):
        def __init__(self):
            super().__init__()
            self._kr = False
            if _KOREAN_TTF and _KOREAN_TTF.exists():
                try:
                    self.add_font("KR",  "",  str(_KOREAN_TTF))
                    self.add_font("KR", "B",  str(_KOREAN_TTF))
                    self._kr = True
                except Exception as fe:
                    logger.warning(f"[PDF] 폰트 로드 실패: {fe}")

        # ── 폰트 설정 ─────────────────────────────────────────
        def F(self, size: int = 11, bold: bool = False):
            style = "B" if bold else ""
            if self._kr:
                self.set_font("KR", style=style, size=size)
            else:
                self.set_font("Helvetica", style=style, size=size)

        # ── 헤더·푸터 ─────────────────────────────────────────
        def header(self):
            if self.page_no() == 1:
                return
            self.F(8)
            self.set_text_color(160, 160, 160)
            self.cell(0, 6, "UnivAgent 입시 처방전", align="L",
                      new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(200, 210, 230)
            self.line(self.l_margin, self.get_y(),
                      self.w - self.r_margin, self.get_y())
            self.ln(3)

        def footer(self):
            if self.page_no() == 1:
                return
            self.set_y(-13)
            self.F(8)
            self.set_text_color(160, 160, 160)
            self.cell(0, 8, f"- {self.page_no() - 1} -", align="C")

        # ── 유효 너비 ─────────────────────────────────────────
        @property
        def W(self) -> float:
            return self.w - self.l_margin - self.r_margin

        # ── 페이지 여유 확인 ──────────────────────────────────
        def _page_ok(self, need_h: float) -> bool:
            return self.get_y() + need_h <= self.h - self.b_margin

        def _ensure_space(self, need_h: float):
            if not self._page_ok(need_h):
                self.add_page()

        # ── 리본 섹션 헤더 ────────────────────────────────────
        def ribbon(self, text: str):
            self._ensure_space(16)
            self.ln(4)
            self.set_fill_color(*_NAVY)
            self.set_text_color(*_WHITE)
            self.F(11, bold=True)
            self.rect(self.l_margin, self.get_y(), self.W, 9, style="F")
            self.set_xy(self.l_margin + 3, self.get_y() + 0.5)
            self.cell(self.W - 3, 8, text, align="L")
            self.ln(10)
            self.set_text_color(*_GRAY)
            self.F(10)

        # ── 본문 멀티셀 ───────────────────────────────────────
        def body(self, text: str, indent: float = 0):
            if not text.strip():
                return
            self.set_x(self.l_margin + indent)
            self.set_text_color(*_GRAY)
            self.F(10)
            self.multi_cell(
                w=self.W - indent, h=6, txt=text.strip(),
                align="L", new_x="LMARGIN", new_y="NEXT",
            )

        # ── 제브라 테이블 (4-컬럼 표시: 30/30/20/20) ─────────
        def draw_table(self, csv_text: str):
            header, rows = _parse_csv_table(csv_text)
            if not header:
                self.body("(테이블 데이터 없음)")
                return

            usable   = self.W
            # 4 display columns: 대학명 30% | 전형명 30% | 격차 20% | 판정 20%
            DISP_H   = ["대학명", "전형명", "기준→현재", "판정"]
            DISP_W   = [usable * 0.30, usable * 0.30, usable * 0.20, usable * 0.20]
            row_h    = 7.5
            col_count = len(header)

            self._ensure_space(row_h * (min(len(rows), 5) + 2) + 6)

            y0 = self.get_y()

            # 헤더 행 (navy 배경, white 텍스트)
            self.set_fill_color(*_NAVY)
            self.rect(self.l_margin, y0, usable, row_h, style="F")
            self.set_text_color(*_WHITE)
            self.F(9, bold=True)
            x = self.l_margin
            for i, hdr in enumerate(DISP_H):
                self.set_xy(x, y0)
                self.cell(DISP_W[i], row_h, hdr, align="C")
                x += DISP_W[i]
            self.ln(row_h)

            # 데이터 행 (흰색 / 연회색 교대)
            for ri, row in enumerate(rows):
                # 5컬럼 CSV → 4 display 컬럼 변환
                row = row + [""] * max(0, 5 - len(row))
                if col_count >= 5:
                    g_base   = row[2].strip()
                    g_cur    = row[3].strip()
                    gap_text = f"{g_base}→{g_cur}" if (g_base and g_cur) else (g_base or g_cur)
                    verdict  = row[4].strip()
                elif col_count == 4:
                    gap_text = row[2].strip()
                    verdict  = row[3].strip()
                else:
                    gap_text = ""
                    verdict  = row[-1].strip() if row else ""

                disp_row = [row[0].strip(), row[1].strip(), gap_text, verdict]

                bg = _WHITE if ri % 2 == 0 else _ALT_ROW
                self.set_fill_color(*bg)
                y_row = self.get_y()
                self.rect(self.l_margin, y_row, usable, row_h, style="F")

                x = self.l_margin
                for ci, cell_val in enumerate(disp_row):
                    if ci == 3:                      # 판정 컬럼 색상
                        if "상향" in cell_val:
                            self.set_text_color(*_RED)
                        elif "적정" in cell_val:
                            self.set_text_color(*_GREEN)
                        else:
                            self.set_text_color(*_BLUE_TXT)
                        self.F(9, bold=True)
                        align = "C"
                    else:
                        self.set_text_color(*_GRAY)
                        self.F(9)
                        align = "L" if ci == 0 else "C"
                    self.set_xy(x, y_row)
                    self.cell(DISP_W[ci], row_h, cell_val, align=align)
                    x += DISP_W[ci]
                self.ln(row_h)

            # 테이블 테두리
            self.set_draw_color(180, 190, 220)
            self.rect(self.l_margin, y0, usable, row_h * (len(rows) + 1), style="D")
            self.ln(3)
            self.set_text_color(*_GRAY)

        # ── 시나리오 박스 (3색 왼쪽 라인 + 번호 뱃지) ──────────
        def scenario_box(self, num: int, text: str):
            text = text.strip()
            if not text:
                return
            color   = _SCN_COLORS[(num - 1) % 3]
            line_h  = 5.5
            n_lines = max(len(text.splitlines()), 2)
            box_h   = line_h * n_lines + 10

            self._ensure_space(box_h + 4)
            x0, y0 = self.l_margin, self.get_y()

            # 배경
            self.set_fill_color(248, 250, 255)
            self.rect(x0, y0, self.W, box_h, style="F")

            # 왼쪽 컬러 라인
            self.set_fill_color(*color)
            self.rect(x0, y0, 4, box_h, style="F")

            # 번호 뱃지
            self.set_fill_color(*color)
            self.rect(x0 + 7, y0 + 2.5, 9, 9, style="F")
            self.set_text_color(*_WHITE)
            self.F(8, bold=True)
            self.set_xy(x0 + 7, y0 + 3)
            self.cell(9, 8, str(num), align="C")

            # 텍스트
            self.set_text_color(*_GRAY)
            self.F(9)
            self.set_xy(x0 + 19, y0 + 3)
            self.multi_cell(
                w=self.W - 21, h=line_h, txt=text,
                align="L", new_x="LMARGIN", new_y="NEXT",
            )
            self.ln(3)

        # ── 세특 박스 (navy 왼쪽 라인 + 연회색 배경) ──────────
        def seteuk_box(self, text: str):
            text = text.strip()
            if not text:
                return
            line_h  = 5.5
            n_lines = max(len(text.splitlines()), 2)
            box_h   = line_h * n_lines + 8

            self._ensure_space(box_h + 4)
            x0, y0 = self.l_margin, self.get_y()

            self.set_fill_color(*_LIGHT_BG)
            self.rect(x0, y0, self.W, box_h, style="F")

            self.set_fill_color(*_NAVY)
            self.rect(x0, y0, 3, box_h, style="F")

            self.set_text_color(*_GRAY)
            self.F(9)
            self.set_xy(x0 + 5, y0 + 3)
            self.multi_cell(
                w=self.W - 7, h=line_h, txt=text,
                align="L", new_x="LMARGIN", new_y="NEXT",
            )
            self.ln(4)

        # ── 팩트체크 박스 (주황 테두리 + 연노랑 배경) ──────────
        def factcheck_box(self, text: str):
            text = text.strip()
            if not text:
                return
            self._ensure_space(20)
            self.ln(2)
            self.F(10, bold=True)
            self.set_text_color(*_ORANGE)
            self.cell(0, 7, "[!] 입시 팩트체크", new_x="LMARGIN", new_y="NEXT")

            self.F(9)
            self.set_text_color(*_GRAY)
            x0, y0 = self.l_margin, self.get_y()
            line_h  = 5.5
            n_lines = max(len(text.splitlines()), 2)
            box_h   = line_h * n_lines + 8

            self._ensure_space(box_h + 4)
            y0 = self.get_y()
            self.set_draw_color(*_ORANGE)
            self.set_fill_color(255, 248, 235)
            self.rect(x0, y0, self.W, box_h, style="FD")
            self.set_xy(x0 + 4, y0 + 3)
            self.multi_cell(
                w=self.W - 6, h=line_h, txt=text,
                align="L", new_x="LMARGIN", new_y="NEXT",
            )
            self.set_draw_color(0, 0, 0)
            self.ln(3)

        # ── CTA 박스 (파랑 테두리 + 연파랑 배경) ───────────────
        def cta_box(self, text: str):
            text = text.strip()
            if not text:
                return
            self._ensure_space(28)
            self.ln(4)
            x0, y0 = self.l_margin, self.get_y()
            line_h  = 5.5
            n_lines = max(len(text.splitlines()), 3)
            box_h   = line_h * n_lines + 14

            self.set_fill_color(*_CTA_BG)
            self.set_draw_color(*_CTA_BD)
            self.rect(x0, y0, self.W, box_h, style="FD")

            # 제목 라인
            self.set_xy(x0 + 4, y0 + 4)
            self.F(10, bold=True)
            self.set_text_color(*_CTA_BD)
            self.cell(self.W - 8, 7, "[AI 컨설턴트의 팁]",
                      new_x="LMARGIN", new_y="NEXT")

            self.set_xy(x0 + 4, self.get_y())
            self.F(9)
            self.set_text_color(*_GRAY)
            self.multi_cell(
                w=self.W - 8, h=line_h, txt=text,
                align="L", new_x="LMARGIN", new_y="NEXT",
            )
            self.set_draw_color(0, 0, 0)
            self.ln(5)

        # ── 부록 박스 (연회색 배경) ───────────────────────────
        def appendix_box(self, text: str):
            text = text.strip()
            if not text:
                return
            self._ensure_space(20)
            x0, y0 = self.l_margin, self.get_y()
            line_h  = 5.5
            n_lines = max(len(text.splitlines()), 4)
            box_h   = line_h * n_lines + 10

            self._ensure_space(box_h)
            y0 = self.get_y()
            self.set_fill_color(*_APP_BG)
            self.set_draw_color(*_APP_BD)
            self.rect(x0, y0, self.W, box_h, style="FD")
            self.set_xy(x0 + 5, y0 + 4)
            self.F(9)
            self.set_text_color(*_GRAY)
            self.multi_cell(
                w=self.W - 10, h=line_h, txt=text,
                align="L", new_x="LMARGIN", new_y="NEXT",
            )
            self.set_draw_color(0, 0, 0)
            self.ln(3)

    # ──────────────────────────────────────────────────────────
    # PDF 초기화
    # ──────────────────────────────────────────────────────────
    pdf = _PDF()
    pdf.set_margins(left=15, top=15, right=15)
    pdf.set_auto_page_break(auto=True, margin=15)

    PAGE_W = pdf.w
    PAGE_H = pdf.h

    # ══════════════════════════════════════════════════════════
    # 표지 (Page 1)
    # ══════════════════════════════════════════════════════════
    pdf.add_page()

    # 상단 네이비 블록
    pdf.set_fill_color(*_NAVY_DK)
    pdf.rect(0, 0, PAGE_W, 90, style="F")

    pdf.set_xy(0, 16)
    pdf.F(10)
    pdf.set_text_color(180, 200, 240)
    pdf.cell(PAGE_W, 8, "UnivAgent AI 입시 처방전", align="C",
             new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(0, 28)
    pdf.F(21, bold=True)
    pdf.set_text_color(*_WHITE)
    safe_title = title.replace("UnivAgent 입시 처방전 — ", "").replace("UnivAgent 입시 처방전 - ", "")
    pdf.cell(PAGE_W, 13, safe_title, align="C",
             new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(0, 48)
    pdf.F(9)
    pdf.set_text_color(160, 185, 230)
    pdf.cell(PAGE_W, 7, f"분석 엔진: {engine}", align="C",
             new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(0, 60)
    pdf.F(9)
    pdf.set_text_color(140, 165, 215)
    ts_str = datetime.now().strftime("%Y년 %m월 %d일 %H:%M 기준")
    pdf.cell(PAGE_W, 7, ts_str, align="C",
             new_x="LMARGIN", new_y="NEXT")

    # 프로필 카드
    card_x = 25
    card_y = 100
    card_w = PAGE_W - 50
    card_h = 68

    pdf.set_fill_color(*_WHITE)
    pdf.set_draw_color(*_NAVY)
    pdf.rect(card_x, card_y, card_w, card_h, style="FD")

    pdf.set_fill_color(*_NAVY)
    pdf.rect(card_x, card_y, card_w, 10, style="F")
    pdf.set_xy(card_x, card_y + 1)
    pdf.F(10, bold=True)
    pdf.set_text_color(*_WHITE)
    pdf.cell(card_w, 8, "학생 프로필", align="C")

    profile_items = [
        ("[ ] 희망 학과", major),
        ("[+] 내신 등급", f"{grade} 등급"),
        ("[M] 모의고사",  mock),
    ]
    row_y = card_y + 16
    for label, value in profile_items:
        pdf.set_xy(card_x + 8, row_y)
        pdf.F(9, bold=True)
        pdf.set_text_color(*_NAVY)
        pdf.cell(38, 7, label)

        pdf.set_xy(card_x + 46, row_y)
        pdf.F(9)
        pdf.set_text_color(*_GRAY)
        pdf.cell(4, 7, ":")

        pdf.set_xy(card_x + 50, row_y)
        pdf.F(9, bold=True)
        pdf.set_text_color(20, 20, 60)
        pdf.cell(card_w - 58, 7, value)
        row_y += 16

    pdf.set_xy(0, 185)
    pdf.F(8)
    pdf.set_text_color(140, 140, 160)
    pdf.cell(PAGE_W, 6,
             "본 처방전은 AI가 생성한 참고용 정보입니다. 최종 지원 결정은 입시 전문가와 상담하세요.",
             align="C", new_x="LMARGIN", new_y="NEXT")

    # ══════════════════════════════════════════════════════════
    # 본문 (Page 2+)
    # ══════════════════════════════════════════════════════════
    pdf.add_page()

    # ── 섹션 A: 관심대학 지원 가능성 정밀 진단 ─────────────────
    pdf.ribbon("섹션 A.  관심대학 지원 가능성 정밀 진단")

    sec_a = sections.get("A", "").strip()
    if sec_a:
        text_a, csv_a = _split_text_and_csv(sec_a)
        if text_a:
            pdf.body(text_a)
            pdf.ln(2)
        pdf.draw_table(csv_a if csv_a else sec_a)
    else:
        pdf.body("(AI 응답에서 지원 가능성 분석 내용을 찾지 못했습니다.)")

    pdf.ln(3)

    # ── 섹션 B: 성적대별 대안 대학 및 전형 추천 ─────────────────
    pdf.ribbon("섹션 B.  성적대별 대안 대학 및 전형 추천")

    sec_b = sections.get("B", "").strip()
    if sec_b:
        text_b, csv_b = _split_text_and_csv(sec_b)
        if text_b:
            pdf.body(text_b)
            pdf.ln(2)
        pdf.draw_table(csv_b if csv_b else sec_b)
    else:
        pdf.body("(AI 응답에서 대안 대학 추천 내용을 찾지 못했습니다.)")

    pdf.ln(3)

    # ── 섹션 C: 성적 향상 시나리오 ────────────────────────────
    pdf.ribbon("섹션 C.  성적 향상 시나리오별 목표 확장 가이드")

    sec_c = sections.get("C", "").strip()
    if sec_c:
        scenario_blocks = re.split(r"(?m)^시나리오\d+\s*:", sec_c)
        scenario_hdrs   = re.findall(r"(?m)^(시나리오\d+)\s*:", sec_c)
        if len(scenario_hdrs) >= 1:
            for idx, (hdr, blk) in enumerate(zip(scenario_hdrs, scenario_blocks[1:])):
                blk = blk.strip()
                if not blk:
                    continue
                first_line = blk.splitlines()[0].strip()
                rest = "\n".join(blk.splitlines()[1:]).strip()
                full_text = f"{hdr}: {first_line}" + (f"\n{rest}" if rest else "")
                pdf.scenario_box(idx + 1, full_text)
        else:
            pdf.body(sec_c)
    else:
        pdf.body("(AI 응답에서 시나리오 분석 내용을 찾지 못했습니다.)")

    pdf.ln(3)

    # ── 섹션 D: 세특 탐구 보고서 레시피 ──────────────────────
    pdf.ribbon("섹션 D.  세특 공백 보완용 초정밀 탐구 보고서 레시피")

    sec_d = sections.get("D", "").strip()
    if sec_d:
        topic_blocks = re.split(r"(?m)^주제\d+\s*:", sec_d)
        topic_hdrs   = re.findall(r"(?m)^(주제\d+)\s*:", sec_d)
        if len(topic_hdrs) >= 1:
            for idx, (hdr, blk) in enumerate(zip(topic_hdrs, topic_blocks[1:])):
                blk = blk.strip()
                if not blk:
                    continue
                blk_lines   = blk.splitlines()
                topic_title = blk_lines[0].strip()
                rest        = "\n".join(blk_lines[1:]).strip()

                pdf.F(10, bold=True)
                pdf.set_text_color(*_NAVY)
                pdf._ensure_space(12)
                pdf.cell(0, 7, f"탐구 주제 {idx + 1}: {topic_title}",
                         new_x="LMARGIN", new_y="NEXT")
                pdf.ln(1)
                if rest:
                    pdf.seteuk_box(rest)
                pdf.ln(1)
        else:
            pdf.seteuk_box(sec_d)
    else:
        pdf.body("(AI 응답에서 세특 탐구 주제를 찾지 못했습니다.)")

    pdf.ln(2)

    # ── 팩트체크 ──────────────────────────────────────────────
    sec_fc = sections.get("FACTCHECK", "").strip()
    if sec_fc:
        pdf.factcheck_box(sec_fc)

    # ── CTA 박스 ──────────────────────────────────────────────
    pdf.cta_box(_CTA_BODY)

    # ── 부록 ──────────────────────────────────────────────────
    pdf._ensure_space(20)
    pdf.ribbon("부록.  2028+ 대입 트렌드 및 학년별 맞춤 준비 가이드")
    pdf.appendix_box(_APPENDIX_TEXT)

    # ── 최하단 안내 ───────────────────────────────────────────
    pdf.ln(4)
    pdf.F(8)
    pdf.set_text_color(170, 170, 190)
    pdf.multi_cell(
        w=0, h=5,
        txt=(
            "※ 본 처방전은 AI 기반 참고 자료입니다. "
            "실제 입학 기준은 각 대학 입학처를 반드시 확인하세요."
        ),
        align="C", new_x="LMARGIN", new_y="NEXT",
    )

    # ── 저장 ──────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    page_count = pdf.page
    logger.info(f"[PDF] 렌더링 완료: {output_path.name} ({page_count}페이지)")
    return page_count


# ─────────────────────────────────────────────────────────────
# 공개 비동기 API
# ─────────────────────────────────────────────────────────────

async def generate_pdf_async(
    title: str,
    content_lines: list[str],
    output_path: Path,
    *,
    doc_type: str = "PDF",
    source: str = "",
    metadata: dict | None = None,
) -> Path:
    """비동기 PDF 생성 + 골든문서 DB 저장."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import logger_factory

    loop = asyncio.get_event_loop()
    meta = metadata or {}

    try:
        page_count: int = await loop.run_in_executor(
            None,
            lambda: _render_pdf_sync(title, content_lines, output_path, meta),
        )
    except Exception as e:
        await logger_factory.async_log_error(
            "PDF_RENDER_FAIL", "pdf_generator", e,
            extra={"title": title, "output_path": str(output_path)},
        )
        raise

    try:
        record_id: int = await loop.run_in_executor(
            None,
            lambda: _insert_golden_document_sync(
                title, str(output_path), doc_type, page_count, source, meta
            ),
        )
        await logger_factory.async_log_event(
            "PDF_GOLDEN_SAVED", "pdf_generator",
            f"골든문서 저장 완료: {output_path.name}",
            extra={
                "record_id":  record_id,
                "file_path":  str(output_path),
                "page_count": page_count,
                "doc_type":   doc_type,
            },
        )
    except Exception as e:
        await logger_factory.async_log_error(
            "PDF_DB_INSERT_FAIL", "pdf_generator", e,
            extra={"title": title, "file_path": str(output_path)},
        )

    return output_path


# ─────────────────────────────────────────────────────────────
# 공개 동기 API
# ─────────────────────────────────────────────────────────────

def generate_pdf_sync(
    title: str,
    content_lines: list[str],
    output_path: Path,
    *,
    doc_type: str = "PDF",
    source: str = "",
    metadata: dict | None = None,
) -> Path:
    """동기 PDF 생성 + 골든문서 DB 저장 (CLI / cron 용)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import logger_factory

    meta = metadata or {}

    try:
        page_count = _render_pdf_sync(title, content_lines, output_path, meta)
    except Exception as e:
        logger_factory.log_error(
            "PDF_RENDER_FAIL", "pdf_generator", e,
            extra={"title": title, "output_path": str(output_path)},
        )
        raise

    try:
        _insert_golden_document_sync(
            title, str(output_path), doc_type, page_count, source, meta
        )
        logger_factory.log_event(
            "PDF_GOLDEN_SAVED", "pdf_generator",
            f"골든문서 저장 완료: {output_path.name}",
            extra={"file_path": str(output_path), "page_count": page_count,
                   "doc_type": doc_type},
        )
    except Exception as e:
        logger_factory.log_error(
            "PDF_DB_INSERT_FAIL", "pdf_generator", e,
            extra={"title": title, "file_path": str(output_path)},
        )

    return output_path


# ─────────────────────────────────────────────────────────────
# CLI 테스트 (6-섹션 샘플)
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging as _log
    _log.basicConfig(level=_log.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    _SAMPLE_CONTENT = """\
[섹션 A]
현재 내신 2.5등급, 모의고사 국어 2/수학 3/영어 2 기준으로 수도권 중위권 대학 지원이 가능한 수준입니다.
수학 계열 성적이 안정적으로 유지되고 있어 이공계 학과 진학에 유리한 위치에 있습니다.
대학명,전형명,기준등급,현재등급,판정
서울시립대학교,학생부교과,2.0,2.5,상향
경희대학교,학생부종합,2.5,2.5,적정
아주대학교,학생부교과,2.8,2.5,안정
인하대학교,학생부교과,3.0,2.5,안정

[섹션 B]
현재 내신 기준으로 지방 거점 국립대 및 수도권 사립대 중위권이 현실적인 대안입니다.
안정 지원 대학을 1~2개 반드시 포함하여 전략적으로 분산 지원하세요.
대학명,전형명,기준등급,현재등급,판정
충남대학교,학생부교과,3.0,2.5,안정
세종대학교,학생부종합,3.2,2.5,안정
국민대학교,학생부교과,3.5,2.5,안정
한국항공대학교,학생부교과,2.7,2.5,적정

[섹션 C]
시나리오1: 현재 2.5등급 유지 시 — 경희대 학종 + 아주대·인하대 교과전형 동시 지원. 안정 2곳 확보 전략.
시나리오2: 내신 2.2등급(+0.3) 달성 시 — 서울시립대 교과전형 적정권 진입. 성균관대 학종 도전 가능.
시나리오3: 내신 1.8등급(+0.7) 달성 시 — 연세대·고려대 학종 상향 도전. 한양대 교과전형 적정 진입.

[섹션 D]
주제1: 반도체 소자의 전류-전압 특성 탐구
키워드: 반도체, PN접합, 전류-전압 특성곡선
1단계: 반도체 기초 이론 학습 및 실험 설계 (교과서 + 논문 탐독)
2단계: 다이오드 실험 장치로 I-V 특성 직접 측정 및 그래프 분석
3단계: 결과를 물리·화학 세특에 '실험적 탐구를 통한 반도체 원리 이해'로 기재

주제2: 인공지능 알고리즘의 효율성 비교 연구
키워드: 머신러닝, 알고리즘, 시간복잡도
1단계: 주요 정렬·탐색 알고리즘 이론 정리 및 구현 계획 수립
2단계: Python으로 버블정렬·퀵정렬·병합정렬 구현 후 대용량 데이터 처리 속도 비교
3단계: 시간복잡도 분석 결과를 정보 세특에 '알고리즘 비교 실험을 통한 효율성 탐구'로 기재

[팩트 체크]
컴퓨터공학과 학생부교과전형의 경우 수능 최저학력기준을 요구하는 대학이 많습니다.
경희대 SW특기자전형은 수능 최저 없음이지만, 학생부교과전형은 국/수/영/탐 2개 합 5등급 이내를 요구합니다.
아주대 학생부교과전형 역시 국수영탐 3개 합 10등급 이내를 요구하므로, 수능 대비를 병행하세요.
"""

    _out = _루트 / "data" / "reports" / "test_premium_pdf.pdf"
    _result = generate_pdf_sync(
        title="UnivAgent 입시 처방전 — 컴퓨터공학과",
        content_lines=_SAMPLE_CONTENT.splitlines(),
        output_path=_out,
        doc_type="DIAGNOSIS_REPORT",
        source="cli_test",
        metadata={
            "major":  "컴퓨터공학과",
            "grade":  "2.5",
            "mock":   "국어 2 / 수학 3 / 영어 2",
            "engine": "Gemini Flash",
        },
    )
    print(f"✅ PDF 생성 완료: {_result}")
