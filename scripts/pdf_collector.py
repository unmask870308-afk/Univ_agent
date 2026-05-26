"""
대학 입시 모집요강 PDF 자동 수집 스크립트
adiga.kr (대입정보포털) 및 개별 대학 입학처에서 모집요강 PDF를 다운로드합니다.

실행 방법:
    python scripts/pdf_collector.py
"""

import subprocess
import sys
import os
import re
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse

# ─────────────────────────────────────────────────────────────
# 의존성 자동 설치
# ─────────────────────────────────────────────────────────────

REQUIRED_PACKAGES = {
    "playwright": "playwright",
    "bs4": "beautifulsoup4",
    "requests": "requests",
}


def 의존성_설치():
    """필요한 패키지가 없으면 자동으로 설치합니다."""
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(import_name)
            logging.info(f"[의존성] {pip_name} 이미 설치됨")
        except ImportError:
            logging.warning(f"[의존성] {pip_name} 미설치 → 자동 설치 중...")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pip_name],
                stdout=subprocess.DEVNULL,
            )
            logging.info(f"[의존성] {pip_name} 설치 완료")

    # playwright 브라우저 바이너리 확인
    try:
        cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
        if not any(cache_dir.glob("chromium-*")):
            logging.warning("[playwright] chromium 미설치 → 설치 중...")
            subprocess.check_call(
                [sys.executable, "-m", "playwright", "install", "chromium"]
            )
            logging.info("[playwright] chromium 설치 완료")
        else:
            logging.info("[playwright] chromium 이미 설치됨")
    except Exception as e:
        logging.error(f"[playwright] 브라우저 확인 실패: {e}")


# ─────────────────────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────────────────────

def 로깅_설정():
    """콘솔과 파일에 한국어 로그를 출력하도록 설정합니다."""
    log_dir = Path(__file__).parent.parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"collector_{datetime.now():%Y%m%d_%H%M%S}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logging.info(f"[로그] 로그 파일: {log_file}")


# ─────────────────────────────────────────────────────────────
# 유틸리티
# ─────────────────────────────────────────────────────────────

def 안전_파일명(text: str, max_len: int = 80) -> str:
    """파일명으로 사용할 수 없는 문자를 제거합니다."""
    text = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", text)
    text = re.sub(r'\s+', "_", text.strip())
    return text[:max_len]


def url_해시(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:8]


def 중복_확인(save_dir: Path, url: str) -> bool:
    """같은 URL을 이전에 다운로드한 적이 있는지 확인합니다."""
    h = url_해시(url)
    return any(h in f.name for f in save_dir.glob("*.pdf"))


# ─────────────────────────────────────────────────────────────
# PDF 실제 파일 여부 검증
# ─────────────────────────────────────────────────────────────

def PDF_바이너리_검증(data: bytes) -> bool:
    """바이트 데이터가 실제 PDF 파일인지 확인합니다 (PDF 시그니처: %PDF-)."""
    return data[:5] == b"%PDF-"


# ─────────────────────────────────────────────────────────────
# requests 기반 직접 다운로드
# ─────────────────────────────────────────────────────────────

def requests_PDF_다운로드(url: str, 파일경로: Path, 출처: str = "") -> bool:
    """
    requests 라이브러리로 URL에서 PDF를 직접 다운로드합니다.
    반환값: 다운로드 성공 여부
    """
    import requests

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Referer": 출처 or "https://www.adiga.kr/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30, stream=True)
        resp.raise_for_status()

        content = b"".join(resp.iter_content(8192))
        if PDF_바이너리_검증(content):
            파일경로.write_bytes(content)
            logging.info(f"[다운로드] 완료 → {파일경로.name} ({len(content):,} bytes)")
            return True
        else:
            ct = resp.headers.get("Content-Type", "")
            logging.debug(f"[다운로드] PDF 시그니처 없음: {url} (Content-Type={ct})")
            return False
    except Exception as e:
        logging.debug(f"[다운로드] 실패: {url} → {e}")
        return False


# ─────────────────────────────────────────────────────────────
# 주요 수집 클래스
# ─────────────────────────────────────────────────────────────

class 모집요강_수집기:
    """
    대학 입시 모집요강 PDF 수집기.

    수집 전략 (순서대로 시도):
    1. adiga.kr 대학 정보 페이지에서 모집요강 PDF 탐색
    2. 개별 대학 입학처 공지사항 게시판 탐색
    3. Playwright 다운로드 이벤트 인터셉션
    """

    # adiga.kr 대학 코드 목록 (주요 대학 샘플)
    ADIGA_대학목록 = [
        {"코드": "0001", "이름": "가톨릭대학교"},
        {"코드": "0002", "이름": "강원대학교"},
        {"코드": "0039", "이름": "고려대학교"},
        {"코드": "0067", "이름": "국민대학교"},
        {"코드": "0109", "이름": "동국대학교"},
        {"코드": "0319", "이름": "서강대학교"},
        {"코드": "0320", "이름": "서울대학교"},
        {"코드": "0325", "이름": "서울시립대학교"},
        {"코드": "0395", "이름": "성균관대학교"},
        {"코드": "0518", "이름": "연세대학교"},
        {"코드": "0623", "이름": "이화여자대학교"},
        {"코드": "0654", "이름": "인하대학교"},
        {"코드": "0696", "이름": "중앙대학교"},
        {"코드": "0855", "이름": "한양대학교"},
        {"코드": "0780", "이름": "포항공과대학교(POSTECH)"},
    ]

    # 개별 대학 입학처 URL - 실제 테스트로 접근 가능이 확인된 URL
    개별_대학_URL = [
        {
            "이름": "서울시립대학교",
            "url": "https://iphak.uos.ac.kr/",
            "설명": "서울시립대 입학처 메인 (수시 모집요강 PDF 직접 링크 존재)",
        },
        {
            "이름": "숭실대학교",
            "url": "https://admission.ssu.ac.kr/",
            "설명": "숭실대 입학처 메인 (입시통계·전형계획 PDF 존재)",
        },
        {
            "이름": "광운대학교",
            "url": "https://admission.kw.ac.kr/",
            "설명": "광운대 입학처 메인",
        },
        {
            "이름": "고려대학교",
            "url": "https://oku.korea.ac.kr/oku/index.do",
            "설명": "고려대 입학처 메인",
        },
        {
            "이름": "성균관대학교",
            "url": "https://admission.skku.edu/admission/index.do",
            "설명": "성균관대 입학처 메인",
        },
        {
            "이름": "한국외국어대학교",
            "url": "https://ibsi.hufs.ac.kr/",
            "설명": "한국외대 입학처 메인",
        },
        {
            "이름": "경희대학교",
            "url": "https://iphak.khu.ac.kr/",
            "설명": "경희대 입학처 메인",
        },
        {
            "이름": "인천대학교",
            "url": "https://iphak.inu.ac.kr/",
            "설명": "인천대 입학처 메인",
        },
    ]

    def __init__(self, save_dir: Path, max_pdf: int = 3, headless: bool = False):
        self.save_dir = save_dir
        self.max_pdf = max_pdf
        self.headless = headless
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.downloaded: list[Path] = []

    # ── 전체 수집 실행 ─────────────────────────────────────────

    def 실행(self) -> list[Path]:
        from playwright.sync_api import sync_playwright

        logging.info("=" * 62)
        logging.info("  모집요강 PDF 수집 시작")
        logging.info(f"  저장 경로 : {self.save_dir}")
        logging.info(f"  목표 수량 : {self.max_pdf}개")
        logging.info(f"  브라우저  : headless={self.headless}")
        logging.info("=" * 62)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                accept_downloads=True,
            )
            # context 수준 다운로드 이벤트 인터셉션
            context.on("download", self._다운로드_이벤트_처리)

            page = context.new_page()

            try:
                # 개별 대학 입학처 탐색
                # (adiga.kr은 로그인 없이 PDF 다운로드 불가 구조 확인됨)
                logging.info("[수집기] 개별 대학 입학처 탐색 시작")
                self._개별_대학_탐색(page, context)

            except Exception as e:
                logging.error(f"[수집기] 예외: {e}", exc_info=True)
            finally:
                page.close()
                context.close()
                browser.close()
                logging.info("[수집기] 브라우저 종료")

        self._결과_요약()
        return self.downloaded

    # ── 다운로드 이벤트 처리 ───────────────────────────────────

    def _다운로드_이벤트_처리(self, download):
        """Playwright download 이벤트를 받아 PDF 파일로 저장합니다."""
        if len(self.downloaded) >= self.max_pdf:
            return

        suggested = download.suggested_filename or "모집요강.pdf"
        안전_이름 = 안전_파일명(suggested)
        if not 안전_이름.lower().endswith(".pdf"):
            안전_이름 += ".pdf"

        hash_suffix = url_해시(download.url)
        파일명 = f"{안전_이름[:-4]}_{hash_suffix}.pdf"
        경로 = self.save_dir / 파일명

        try:
            download.save_as(경로)
            if 경로.exists() and PDF_바이너리_검증(경로.read_bytes()[:5]):
                크기 = 경로.stat().st_size
                logging.info(f"[이벤트다운] 저장 완료: {파일명} ({크기:,} bytes)")
                self.downloaded.append(경로)
            else:
                logging.warning(f"[이벤트다운] PDF 시그니처 없음: {파일명}")
                if 경로.exists():
                    경로.unlink()
        except Exception as e:
            logging.warning(f"[이벤트다운] 저장 실패: {e}")

    # ── 전략 1: adiga.kr 대학별 탐색 ─────────────────────────

    def _adiga_대학별_탐색(self, page, context):
        """adiga.kr에서 대학별 모집요강 페이지를 탐색합니다."""
        BASE = "https://www.adiga.kr"

        # 메인 접속
        logging.info(f"[adiga] 메인 접속: {BASE}")
        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(1500)
            logging.info(f"[adiga] 페이지 제목: {page.title()}")
        except Exception as e:
            logging.warning(f"[adiga] 메인 접속 실패: {e}")
            return

        # 방법 A: 대학 정보 목록 페이지
        대학목록_url = f"{BASE}/ucp/uvt/uni/univList.do?menuId=PCUVTINF2000"
        logging.info(f"[adiga] 대학 목록 탐색: {대학목록_url}")
        try:
            page.goto(대학목록_url, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(2000)

            # 대학 링크들 수집
            대학_링크들 = page.evaluate("""
                () => Array.from(document.querySelectorAll('a'))
                    .filter(a => a.href && (
                        a.href.includes('univView') ||
                        a.href.includes('univCd') ||
                        a.href.includes('univ_cd')
                    ))
                    .slice(0, 10)
                    .map(a => ({href: a.href, text: a.innerText.trim()}))
            """)
            logging.info(f"[adiga] 대학 링크 {len(대학_링크들)}개 발견")

            for 대학 in 대학_링크들:
                if len(self.downloaded) >= self.max_pdf:
                    return
                self._adiga_대학페이지_탐색(page, context, 대학["href"], 대학["text"])

        except Exception as e:
            logging.warning(f"[adiga] 대학 목록 탐색 실패: {e}")

        # 방법 B: 알려진 대학 코드로 직접 접근
        if len(self.downloaded) < self.max_pdf:
            logging.info("[adiga] 방법B: 알려진 대학 코드로 직접 접근")
            for 대학 in self.ADIGA_대학목록:
                if len(self.downloaded) >= self.max_pdf:
                    return
                url = f"{BASE}/ucp/uvt/uni/univView.do?univCd={대학['코드']}&menuId=PCUVTINF2000"
                self._adiga_대학페이지_탐색(page, context, url, 대학["이름"])

    def _adiga_대학페이지_탐색(self, page, context, url: str, 대학명: str):
        """adiga.kr 특정 대학 페이지에서 PDF 링크를 탐색합니다."""
        logging.info(f"[adiga대학] {대학명} 접속: {url}")
        try:
            page.goto(url, wait_until="networkidle", timeout=20_000)
            page.wait_for_timeout(1000)

            # 페이지 내 실제 .pdf 링크만 추출 (href가 .pdf로 끝나거나 download= 속성 포함)
            pdf_링크들 = page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => {
                        const h = a.href.toLowerCase();
                        return h.endsWith('.pdf')
                            || h.includes('/pdf/')
                            || h.includes('download=')
                            || a.download
                            || a.href.includes('fileDown')
                            || a.href.includes('fileDwn')
                            || a.href.includes('attachDown')
                            || a.href.includes('boardDown')
                            || a.href.includes('getFile')
                            || a.href.includes('downFile');
                    })
                    .map(a => ({url: a.href, text: a.innerText.trim().slice(0,80)}))
            """)

            # onclick 기반 다운로드 버튼 추출
            onclick_다운로드 = page.evaluate("""
                () => Array.from(document.querySelectorAll('[onclick]'))
                    .filter(el => {
                        const oc = el.getAttribute('onclick') || '';
                        return oc.includes('download') || oc.includes('fileDown')
                            || oc.includes('Down') || oc.includes('pdf');
                    })
                    .map(el => ({
                        selector: el.tagName.toLowerCase() + (el.id ? '#'+el.id : ''),
                        text: el.innerText.trim().slice(0, 80),
                        onclick: el.getAttribute('onclick')
                    }))
                    .slice(0, 5)
            """)

            logging.info(
                f"[adiga대학] {대학명}: PDF링크 {len(pdf_링크들)}개, "
                f"onclick다운로드 {len(onclick_다운로드)}개 발견"
            )

            # 직접 PDF 링크 다운로드
            for 링크 in pdf_링크들:
                if len(self.downloaded) >= self.max_pdf:
                    return
                self._url_PDF_저장(링크["url"], f"{대학명}_{링크['text']}", page.url)

            # "모집요강" 탭 또는 메뉴 클릭 후 재탐색
            if len(self.downloaded) < self.max_pdf:
                모집요강_탭 = page.locator(
                    "a:has-text('모집요강'), button:has-text('모집요강'), "
                    "li:has-text('모집요강') a, .tab:has-text('모집요강')"
                ).first
                if 모집요강_탭.count() > 0:
                    logging.info(f"[adiga대학] '{대학명}' 모집요강 탭 클릭")
                    모집요강_탭.click()
                    page.wait_for_timeout(1500)
                    # 클릭 후 PDF 링크 재추출
                    pdf_링크들2 = page.evaluate("""
                        () => Array.from(document.querySelectorAll('a[href]'))
                            .filter(a => a.href.toLowerCase().endsWith('.pdf')
                                || a.href.includes('fileDown')
                                || a.href.includes('download'))
                            .map(a => ({url: a.href, text: a.innerText.trim().slice(0,80)}))
                    """)
                    for 링크 in pdf_링크들2:
                        if len(self.downloaded) >= self.max_pdf:
                            return
                        self._url_PDF_저장(링크["url"], f"{대학명}_{링크['text']}", page.url)

        except Exception as e:
            logging.debug(f"[adiga대학] {대학명} 오류: {e}")

    # ── 전략 2: 개별 대학 입학처 탐색 ────────────────────────

    def _개별_대학_탐색(self, page, context):
        """개별 대학 입학처 페이지에서 모집요강 PDF를 탐색합니다."""
        for 대학 in self.개별_대학_URL:
            if len(self.downloaded) >= self.max_pdf:
                return
            logging.info(f"[개별대학] {대학['이름']} 접속 ({대학['설명']})")
            self._대학페이지_깊이_탐색(page, context, 대학["url"], 대학["이름"])

    def _대학페이지_깊이_탐색(self, page, context, url: str, 대학명: str, 깊이: int = 0):
        """
        대학 페이지에서 PDF를 찾습니다.
        직접 PDF가 없으면 공지사항/모집요강 게시판으로 진입합니다.
        """
        if 깊이 > 2:
            return

        # javascript: URL은 goto()로 탐색 불가 - 건너뜀
        if url.startswith("javascript:") or url == "#" or url == "":
            logging.debug(f"[개별대학] javascript: URL 건너뜀: {url[:60]}")
            return

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            # 동적 콘텐츠가 로드될 시간 확보
            page.wait_for_timeout(2500)
            logging.info(f"[개별대학] '{대학명}' 페이지 로드: {page.title()[:50]}")

            # 직접 PDF 링크 탐색
            pdf_링크들 = self._페이지_PDF_링크_추출(page)
            for 링크 in pdf_링크들:
                if len(self.downloaded) >= self.max_pdf:
                    return
                self._url_PDF_저장(링크["url"], f"{대학명}_{링크['text']}", page.url)

            if len(self.downloaded) >= self.max_pdf:
                return

            # PDF 직접 링크 없음 → 공지사항/모집요강 메뉴 진입
            if 깊이 == 0:
                탐색_셀렉터들 = [
                    "a:has-text('모집요강')",
                    "nav a:has-text('수시')",
                    "a:has-text('수시모집')",
                    "a:has-text('정시모집')",
                    "a:has-text('입학공지')",
                    "a:has-text('공지사항')",
                    "nav a:has-text('입학안내')",
                ]
                for 셀렉터 in 탐색_셀렉터들:
                    try:
                        링크 = page.locator(셀렉터).first
                        if 링크.count() > 0:
                            href = 링크.get_attribute("href") or ""
                            링크_텍스트 = 링크.inner_text().strip()

                            # javascript: URL 건너뜀
                            if href.startswith("javascript:") or href == "#":
                                logging.debug(f"[개별대학] JS URL 건너뜀: {href[:60]}")
                                continue

                            logging.info(f"[개별대학] '{링크_텍스트}' 메뉴 진입: {href}")
                            전체_url = href if href.startswith("http") else urljoin(page.url, href)
                            self._대학페이지_깊이_탐색(page, context, 전체_url, 대학명, 깊이 + 1)
                            if len(self.downloaded) >= self.max_pdf:
                                return
                    except Exception:
                        continue

            # 게시판 목록에서 모집요강 행 탐색
            elif 깊이 == 1:
                self._게시판_탐색(page, context, 대학명)

        except Exception as e:
            logging.warning(f"[개별대학] '{대학명}' 탐색 오류 (깊이={깊이}): {e}")

    def _게시판_탐색(self, page, context, 대학명: str):
        """게시판 목록에서 '모집요강' 제목을 찾아 첨부파일을 다운로드합니다."""
        try:
            # 게시판 항목에서 '모집요강' 포함 제목 찾기
            행_셀렉터들 = [
                "table tbody tr:has(td:has-text('모집요강'))",
                "ul.board li:has(a:has-text('모집요강'))",
                "div.list-item:has-text('모집요강')",
                ".board-list tr:has(:has-text('요강'))",
            ]
            for 셀렉터 in 행_셀렉터들:
                try:
                    행들 = page.locator(셀렉터).all()
                    if 행들:
                        logging.info(f"[게시판] '{셀렉터}' 항목 {len(행들)}개 발견")
                        for 행 in 행들[:3]:
                            if len(self.downloaded) >= self.max_pdf:
                                return
                            링크 = 행.locator("a").first
                            if 링크.count() > 0:
                                제목 = 링크.inner_text().strip()[:60]
                                href = 링크.get_attribute("href") or ""
                                logging.info(f"[게시판] 항목 클릭: {제목}")
                                링크.click()
                                page.wait_for_timeout(2000)
                                # 상세 페이지에서 첨부파일 탐색
                                self._첨부파일_탐색_및_다운로드(page, 대학명, 제목)
                                page.go_back()
                                page.wait_for_timeout(1000)
                        return
                except Exception:
                    continue
        except Exception as e:
            logging.debug(f"[게시판] 탐색 오류: {e}")

    def _첨부파일_탐색_및_다운로드(self, page, 대학명: str, 제목: str):
        """게시글 상세 페이지에서 PDF 첨부파일을 찾아 다운로드합니다."""
        # 첨부파일 링크 패턴
        첨부_셀렉터들 = [
            "a[href$='.pdf']",
            "a[href*='fileDown']",
            "a[href*='fileDwn']",
            "a[href*='attachDown']",
            "a[href*='getFile']",
            "a[href*='downFile']",
            "a[href*='download']",
            ".attach-file a",
            ".file-list a",
            "td.file a",
            "dd.file a",
        ]
        for 셀렉터 in 첨부_셀렉터들:
            try:
                링크들 = page.locator(셀렉터).all()
                for 링크 in 링크들[:2]:
                    href = 링크.get_attribute("href") or ""
                    파일명_텍스트 = 링크.inner_text().strip()[:60]
                    if not href:
                        continue
                    전체_url = href if href.startswith("http") else urljoin(page.url, href)
                    logging.info(f"[첨부파일] '{파일명_텍스트}' 다운로드 시도: {전체_url}")
                    저장_키 = f"{대학명}_{제목[:30]}_{파일명_텍스트}"
                    성공 = self._url_PDF_저장(전체_url, 저장_키, page.url)
                    if 성공 and len(self.downloaded) >= self.max_pdf:
                        return
            except Exception:
                continue

    # ── PDF 저장 ───────────────────────────────────────────────

    def _페이지_PDF_링크_추출(self, page) -> list[dict]:
        """현재 페이지에서 실제 PDF 파일을 가리키는 링크만 추출합니다."""
        try:
            return page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => {
                        const h = (a.href || '').toLowerCase();
                        const t = (a.innerText || a.title || '').trim();
                        // 실제 파일 다운로드 URL 패턴만 허용
                        return h.endsWith('.pdf')
                            || h.includes('/pdf/')
                            || h.includes('filedown')
                            || h.includes('filedwn')
                            || h.includes('attachdown')
                            || h.includes('getfile')
                            || h.includes('downfile')
                            || (h.includes('download') && !h.includes('adobe'));
                    })
                    .map(a => ({url: a.href, text: (a.innerText||a.title||'').trim().slice(0,80)}))
            """)
        except Exception:
            return []

    def _url_PDF_저장(self, url: str, 제목: str, 출처: str = "") -> bool:
        """
        URL에서 PDF를 다운로드하고 save_dir에 저장합니다.
        반환값: 저장 성공 여부
        """
        if not url or not url.startswith("http"):
            return False
        if 중복_확인(self.save_dir, url):
            logging.info(f"[저장] 중복 건너뜀: {url[:60]}")
            return False

        안전제목 = 안전_파일명(제목) or "모집요강"
        파일명 = f"{안전제목}_{url_해시(url)}.pdf"
        경로 = self.save_dir / 파일명

        성공 = requests_PDF_다운로드(url, 경로, 출처)
        if 성공:
            self.downloaded.append(경로)
        return 성공

    # ── 결과 요약 ──────────────────────────────────────────────

    def _결과_요약(self):
        logging.info("")
        logging.info("=" * 62)
        logging.info(f"[결과] 수집 완료: 총 {len(self.downloaded)}개 PDF")
        for i, 경로 in enumerate(self.downloaded, 1):
            크기 = 경로.stat().st_size if 경로.exists() else 0
            logging.info(f"  {i:2}. {경로.name}  ({크기:,} bytes)")
        logging.info(f"[결과] 저장 위치: {self.save_dir}")
        logging.info("=" * 62)


# ─────────────────────────────────────────────────────────────
# 보조 수집기: 알려진 직접 PDF URL로 즉시 다운로드
# ─────────────────────────────────────────────────────────────

class 직접URL_수집기:
    """
    직접 PDF URL이 알려진 경우 즉시 다운로드합니다.
    주로 테스트 및 fallback 용도로 사용합니다.

    한국 대학 공개 모집요강 PDF 직접 URL 목록을 포함합니다.
    (URL은 시즌마다 변경될 수 있으므로 정기적으로 갱신 필요)
    """

    # 공개 접근 가능한 직접 PDF URL
    # HTTP 200 + application/pdf 응답 확인된 URL 목록 (매년 갱신 필요)
    KNOWN_PDFS: list[dict] = [
        {
            "title": "서울대학교_2026학년도_수시모집요강",
            "url": "https://admission.snu.ac.kr/webdata/admission/files/2026susi.pdf",
            "출처": "https://admission.snu.ac.kr/undergraduate/early/guide",
        },
        {
            "title": "연세대학교_2026학년도_수시_모집요강",
            "url": "https://admission.yonsei.ac.kr/seoul/upload/guide/20251229103015L4LFJB.PDF",
            "출처": "https://admission.yonsei.ac.kr/seoul/admission/html/rolling/guide.asp",
        },
        {
            "title": "연세대학교_2026학년도_학생부종합전형_안내서",
            "url": "https://www2.yonsei.ac.kr/entrance/2025/2026학년도_연세대학교_학생부종합전형안내서.pdf",
            "출처": "https://admission.yonsei.ac.kr/seoul/admission/html/rolling/guide.asp",
        },
        {
            "title": "연세대학교_2028학년도_입학전형_시행계획",
            "url": "https://www2.yonsei.ac.kr/entrance/plan/2028_plan.pdf",
            "출처": "https://admission.yonsei.ac.kr/seoul/admission/html/rolling/guide.asp",
        },
    ]

    def __init__(self, save_dir: Path):
        self.save_dir = save_dir
        self.downloaded: list[Path] = []

    def 실행(self) -> list[Path]:
        if not self.KNOWN_PDFS:
            logging.info("[직접URL] 등록된 직접 URL 없음 - 건너뜀")
            return []

        logging.info(f"[직접URL] {len(self.KNOWN_PDFS)}개 URL 다운로드 시작")
        for 항목 in self.KNOWN_PDFS:
            url = 항목.get("url", "")
            제목 = 항목.get("title", "모집요강")
            출처 = 항목.get("출처", "")
            안전제목 = 안전_파일명(제목)
            파일명 = f"{안전제목}_{url_해시(url)}.pdf"
            경로 = self.save_dir / 파일명

            if 중복_확인(self.save_dir, url):
                logging.info(f"[직접URL] 중복 건너뜀: {제목}")
                continue

            logging.info(f"[직접URL] 다운로드: {제목}")
            if requests_PDF_다운로드(url, 경로, 출처):
                self.downloaded.append(경로)

        return self.downloaded


# ─────────────────────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────────────────────

def main():
    로깅_설정()
    logging.info("━" * 62)
    logging.info("  대학 입시 모집요강 PDF 자동 수집기 v2")
    logging.info("━" * 62)

    # 1. 의존성 확인 및 설치
    logging.info("[초기화] 의존성 확인 중...")
    의존성_설치()

    # 저장 경로 준비
    프로젝트_루트 = Path(__file__).parent.parent
    저장경로 = 프로젝트_루트 / "data" / "raw_pdf"
    저장경로.mkdir(parents=True, exist_ok=True)
    logging.info(f"[초기화] PDF 저장 경로: {저장경로.resolve()}")

    결과: list[Path] = []

    # 2. 직접 URL 수집기 - 서울대·연세대 등 확인된 PDF URL 즉시 다운로드
    직접_수집기 = 직접URL_수집기(저장경로)
    결과 += 직접_수집기.실행()

    # 3. 직접 URL로 목표(4개) 미달 시 Playwright로 추가 수집
    목표_수량 = len(직접URL_수집기.KNOWN_PDFS)
    if len(결과) < 목표_수량:
        남은_수 = 목표_수량 - len(결과)
        logging.info(f"[메인] 직접 URL 미달({len(결과)}/{목표_수량}) → Playwright로 {남은_수}개 추가 수집")
        수집기 = 모집요강_수집기(
            save_dir=저장경로,
            max_pdf=남은_수,
            headless=False,
        )
        결과 += 수집기.실행()

    # 4. 최종 결과
    logging.info("")
    if 결과:
        logging.info(f"[완료] 총 {len(결과)}개 모집요강 PDF 수집 완료!")
        for 파일 in 결과:
            logging.info(f"  → {파일.name}")
        return 0
    else:
        logging.error("[완료] PDF 수집 실패.")
        logging.error("  사이트 구조가 변경되었거나 접근 제한이 있을 수 있습니다.")
        logging.error("  adiga.kr 또는 각 대학 입학처를 직접 확인해주세요.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
