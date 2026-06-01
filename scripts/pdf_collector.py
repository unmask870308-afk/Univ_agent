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
import json
import hashlib
import logging
import time
import random
import traceback
import textwrap
import threading
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse

# Gemini API 키 (자가치유·코드 수정 전용 — 크롤링 파싱에는 미사용)
_GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

sys.path.insert(0, str(Path(__file__).parent))
import token_manager as _tm  # noqa: E402

# ─────────────────────────────────────────────────────────────
# 의존성 자동 설치
# ─────────────────────────────────────────────────────────────

REQUIRED_PACKAGES = {
    "playwright": "playwright",
    "bs4": "beautifulsoup4",
    "requests": "requests",
    "fake_useragent": "fake-useragent",
}


def 의존성_설치():
    """필요한 패키지가 없으면 자동으로 설치합니다."""
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(import_name)
            logging.info(f"[의존성] {pip_name} 이미 설치됨")
        except ImportError:
            logging.warning(f"[의존성] {pip_name} 미설치 → 자동 설치 중...")
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", pip_name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                logging.info(f"[의존성] {pip_name} 설치 완료")
            except Exception as ie:
                logging.warning(f"[의존성] {pip_name} 설치 실패 (계속 진행): {ie}")

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
    """콘솔, 앱 파일, HTTP 네트워크 파일 3채널 로깅을 구성합니다."""
    log_dir = Path(__file__).parent.parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"collector_{datetime.now():%Y%m%d_%H%M%S}.log"
    datefmt = "%Y-%m-%d %H:%M:%S"

    fmt_app = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt)
    fmt_net = logging.Formatter("%(asctime)s [%(name)-24s] %(levelname)-8s %(message)s", datefmt)

    # 앱 로그: 콘솔 + 타임스탬프 파일
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt_app)
    fh_app = logging.FileHandler(log_file, encoding="utf-8")
    fh_app.setFormatter(fmt_app)
    root.addHandler(sh)
    root.addHandler(fh_app)

    # HTTP 네트워크 로그 — requests/urllib3/playwright 네트워크 트래픽 분리
    net_log = log_dir / "http_network.log"
    fh_net = logging.FileHandler(net_log, encoding="utf-8", mode="a")
    fh_net.setFormatter(fmt_net)
    for lib in ("urllib3", "urllib3.connectionpool", "requests",
                "playwright", "asyncio"):
        lg = logging.getLogger(lib)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(fh_net)
        lg.propagate = False

    # 자가치유 로그 — Gemini 자가수정 이력 전용
    heal_log = log_dir / "self_healing.log"
    fh_heal = logging.FileHandler(heal_log, encoding="utf-8", mode="a")
    fh_heal.setFormatter(fmt_app)
    _자가치유_로거 = logging.getLogger("self_healing")
    _자가치유_로거.setLevel(logging.DEBUG)
    _자가치유_로거.handlers.clear()
    _자가치유_로거.addHandler(fh_heal)
    _자가치유_로거.addHandler(sh)
    _자가치유_로거.propagate = False

    # http_network 수집기 상태 로그 — 수집 진행 상태 한국어 기록 전용
    _네트워크_로거 = logging.getLogger("http_network")
    _네트워크_로거.setLevel(logging.DEBUG)
    _네트워크_로거.handlers.clear()
    _네트워크_로거.addHandler(fh_net)
    _네트워크_로거.addHandler(sh)
    _네트워크_로거.propagate = False

    logging.info(f"[로그] 앱: {log_file}")
    logging.info(f"[로그] 네트워크: {net_log}")
    logging.info(f"[로그] 자가치유: {heal_log}")


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

# ─────────────────────────────────────────────────────────────
# User-Agent 풀 및 헬퍼
# ─────────────────────────────────────────────────────────────

_UA_풀 = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]


def UA_가져오기() -> str:
    """fake_useragent 또는 내장 UA 풀에서 랜덤 User-Agent를 반환합니다."""
    try:
        from fake_useragent import UserAgent
        return UserAgent().random
    except Exception:
        return random.choice(_UA_풀)


def requests_PDF_다운로드(url: str, 파일경로: Path, 출처: str = "") -> bool:
    """
    requests 라이브러리로 URL에서 PDF를 직접 다운로드합니다.
    fake-useragent로 UA를 무작위 교체해 봇 탐지를 우회합니다.
    반환값: 다운로드 성공 여부
    """
    import requests

    headers = {
        "User-Agent": UA_가져오기(),
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Referer": 출처 or "https://www.adiga.kr/",
        "Connection": "keep-alive",
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
# BS4 폴백 스캐너 (Playwright 실패 시 사용)
# ─────────────────────────────────────────────────────────────

def BS4_PDF_스캔(url: str, 대학명: str, 출처: str = "", _깊이: int = 0) -> list[dict]:
    """
    requests + BeautifulSoup4 로 대학 페이지를 스캔해 PDF 링크를 추출합니다.
    Playwright 클릭 실패·봇 차단 시 자동 폴백으로 호출됩니다.

    반환: [{"url": "...", "text": "..."}, ...] 목록
    """
    import requests
    from bs4 import BeautifulSoup

    if not url or not url.startswith("http"):
        return []

    headers = {
        "User-Agent": UA_가져오기(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": 출처 or url,
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    # PDF 관련 URL 패턴
    _PDF_패턴 = re.compile(
        r'(\.pdf(\?|$))|(/pdf/)'
        r'|filedown|filedwn|attachdown|getfile|downfile'
        r'|boarddown|boardfile|atch_file|file_down'
        r'|pdfdown|pdfview|pdf_down|fileSave|fileView'
        r'|download(?!=\.)|attach(?=Down|File)',
        re.IGNORECASE,
    )
    # 모집요강/수시 메뉴 텍스트 패턴
    _메뉴_패턴 = re.compile(
        r'모집요강|수시모집|수시안내|입학요강|입학안내|모집안내'
        r'|전형안내|입시안내|입학정보|모집_요강|요강_다운',
        re.IGNORECASE,
    )

    결과: list[dict] = []

    try:
        sess = requests.Session()
        sess.headers.update(headers)
        # GET 전 HEAD 없이 바로 GET (일부 대학은 HEAD 차단)
        resp = sess.get(url, timeout=25, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        base_url = resp.url  # 리다이렉트 후 실제 URL

        # ── 1. 직접 PDF 링크 추출 (<a href>) ───────────────
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith("javascript:") or href in ("#", ""):
                continue
            텍스트 = (a.get_text(strip=True) or a.get("title") or "모집요강")[:80]
            if _PDF_패턴.search(href):
                전체_url = href if href.startswith("http") else urljoin(base_url, href)
                결과.append({"url": 전체_url, "text": f"{대학명}_{텍스트}"})

        # ── 2. onclick 속성 내 파일 경로 추출 ───────────────
        _onclick_패턴 = re.compile(
            r"(?:fileDown|fileDwn|download|getFile|boardFile|fnDown|pdfDown)"
            r"\s*\(\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )
        for el in soup.find_all(onclick=True):
            oc = el.get("onclick", "")
            for m in _onclick_패턴.finditer(oc):
                path = m.group(1).strip()
                if not path:
                    continue
                전체_url = path if path.startswith("http") else urljoin(base_url, "/" + path.lstrip("/"))
                텍스트 = (el.get_text(strip=True) or "onclick다운로드")[:60]
                결과.append({"url": 전체_url, "text": f"{대학명}_{텍스트}"})

        # ── 3. 깊이=0이고 직접 PDF 미발견 → 모집요강 메뉴 링크 추적 ──
        if _깊이 == 0 and not 결과:
            방문한: set[str] = set()
            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                텍스트 = a.get_text(strip=True)
                if not href or href.startswith("javascript:") or href == "#":
                    continue
                if _메뉴_패턴.search(텍스트) and href not in 방문한:
                    방문한.add(href)
                    하위_url = href if href.startswith("http") else urljoin(base_url, href)
                    logging.debug(f"[BS4] '{대학명}' 메뉴 추적: {텍스트!r} → {하위_url[:60]}")
                    하위결과 = BS4_PDF_스캔(하위_url, 대학명, url, _깊이=1)
                    결과.extend(하위결과)
                    if 결과:
                        break  # 첫 성공 시 중단

        # ── 4. 공지사항 게시판 목록에서 '모집요강' 행 → 상세 페이지 추적 ─
        if _깊이 == 0 and not 결과:
            게시판_패턴 = re.compile(r'모집요강|수시모집|입학요강', re.IGNORECASE)
            for a in soup.find_all("a", href=True, string=게시판_패턴):
                href = (a.get("href") or "").strip()
                if not href or href.startswith("javascript:"):
                    continue
                상세_url = href if href.startswith("http") else urljoin(base_url, href)
                logging.debug(f"[BS4] '{대학명}' 게시글 추적: {a.get_text(strip=True)!r}")
                하위결과 = BS4_PDF_스캔(상세_url, 대학명, url, _깊이=1)
                결과.extend(하위결과)
                if 결과:
                    break

        logging.info(f"[BS4폴백] '{대학명}': {len(결과)}개 PDF 링크 발견")

    except Exception as e:
        logging.debug(f"[BS4폴백] '{대학명}' 오류 (url={url[:60]}): {e}")

    return 결과


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

    # 개별 대학 입학처 URL — university_list.txt의 전체 대학 커버
    개별_대학_URL = [
        # ── SKY ─────────────────────────────────────────────────
        {"이름": "서울대학교",       "url": "https://admission.snu.ac.kr/",                    "설명": "서울대 입학본부 메인"},
        {"이름": "연세대학교",       "url": "https://admission.yonsei.ac.kr/",                 "설명": "연세대 입학처 메인"},
        {"이름": "고려대학교",       "url": "https://oku.korea.ac.kr/oku/index.do",            "설명": "고려대 입학처 메인"},
        # ── 서성한중경이 ───────────────────────────────────────
        {"이름": "서강대학교",       "url": "https://admission.sogang.ac.kr/",                 "설명": "서강대 입학처 메인"},
        {"이름": "성균관대학교",     "url": "https://admission.skku.edu/admission/index.do",   "설명": "성균관대 입학처 메인"},
        {"이름": "한양대학교",       "url": "https://go.hanyang.ac.kr/",                       "설명": "한양대 입학처 메인"},
        {"이름": "중앙대학교",       "url": "https://admission.cau.ac.kr/",                    "설명": "중앙대 입학처 메인"},
        {"이름": "경희대학교",       "url": "https://iphak.khu.ac.kr/",                        "설명": "경희대 입학처 메인"},
        {"이름": "이화여자대학교",   "url": "https://admission.ewha.ac.kr/",                   "설명": "이화여대 입학처 메인"},
        # ── 서울권 ─────────────────────────────────────────────
        {"이름": "서울시립대학교",   "url": "https://iphak.uos.ac.kr/",                        "설명": "서울시립대 입학처 메인"},
        {"이름": "한국외국어대학교", "url": "https://ibsi.hufs.ac.kr/",                        "설명": "한국외대 입학처 메인"},
        {"이름": "숭실대학교",       "url": "https://admission.ssu.ac.kr/",                    "설명": "숭실대 입학처 메인"},
        {"이름": "광운대학교",       "url": "https://admission.kw.ac.kr/",                     "설명": "광운대 입학처 메인"},
        {"이름": "국민대학교",       "url": "https://iphak.kookmin.ac.kr/",                    "설명": "국민대 입학처 메인"},
        {"이름": "동국대학교",       "url": "https://ipsi.dongguk.edu/",                       "설명": "동국대 입학처 메인"},
        {"이름": "건국대학교",       "url": "https://ipsi.konkuk.ac.kr/",                      "설명": "건국대 입학처 메인"},
        {"이름": "홍익대학교",       "url": "https://ipsi.hongik.ac.kr/",                      "설명": "홍익대 입학처 메인"},
        {"이름": "세종대학교",       "url": "https://ipsi.sejong.ac.kr/",                      "설명": "세종대 입학처 메인"},
        {"이름": "가천대학교",       "url": "https://ipsi.gachon.ac.kr/",                      "설명": "가천대 입학처 메인"},
        # ── 여자대학교 ─────────────────────────────────────────
        {"이름": "숙명여자대학교",   "url": "https://iphak.sookmyung.ac.kr/",                  "설명": "숙명여대 입학처 메인"},
        {"이름": "성신여자대학교",   "url": "https://ipsi.sungshin.ac.kr/",                    "설명": "성신여대 입학처 메인"},
        {"이름": "덕성여자대학교",   "url": "https://www.duksung.ac.kr/iphak/",                "설명": "덕성여대 입학처 메인"},
        # ── 수도권 ─────────────────────────────────────────────
        {"이름": "인천대학교",       "url": "https://iphak.inu.ac.kr/",                        "설명": "인천대 입학처 메인"},
        {"이름": "인하대학교",       "url": "https://admission.inha.ac.kr/",                   "설명": "인하대 입학처 메인"},
        {"이름": "아주대학교",       "url": "https://ipsi.ajou.ac.kr/",                        "설명": "아주대 입학처 메인"},
        {"이름": "단국대학교",       "url": "https://iphak.dankook.ac.kr/",                    "설명": "단국대 입학처 메인"},
        # ── 지방 국립대 ───────────────────────────────────────
        {"이름": "부산대학교",       "url": "https://ipsi.pusan.ac.kr/",                       "설명": "부산대 입학처 메인"},
        {"이름": "경북대학교",       "url": "https://admission.knu.ac.kr/",                    "설명": "경북대 입학처 메인"},
        {"이름": "충남대학교",       "url": "https://admission.cnu.ac.kr/",                    "설명": "충남대 입학처 메인"},
        {"이름": "전남대학교",       "url": "https://iphak.jnu.ac.kr/",                        "설명": "전남대 입학처 메인"},
        # ── 이공계 특성화 ─────────────────────────────────────
        {"이름": "포항공과대학교",   "url": "https://admission.postech.ac.kr/",                "설명": "포항공대 입학처 메인"},
        {"이름": "DGIST",            "url": "https://admission.dgist.ac.kr/",                  "설명": "DGIST 입학처 메인"},
        {"이름": "GIST",             "url": "https://admission.gist.ac.kr/",                   "설명": "GIST 입학처 메인"},
        {"이름": "UNIST",            "url": "https://admission.unist.ac.kr/",                  "설명": "UNIST 입학처 메인"},
    ]

    def __init__(self, save_dir: Path, max_pdf: int = 3, headless: bool = False,
                 _다운로드_콜백=None):
        self.save_dir = save_dir
        self.max_pdf = max_pdf
        self.headless = headless
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.downloaded: list[Path] = []
        self._다운로드_콜백 = _다운로드_콜백

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
                if self._다운로드_콜백:
                    try:
                        self._다운로드_콜백(경로)
                    except Exception as _ce:
                        logging.warning(f"[콜백] 이벤트다운 콜백 오류: {_ce}")
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
        for idx, 대학 in enumerate(self.개별_대학_URL):
            if len(self.downloaded) >= self.max_pdf:
                return
            logging.info(f"[개별대학] {대학['이름']} 접속 ({대학['설명']})")
            self._대학페이지_깊이_탐색(page, context, 대학["url"], 대학["이름"])
            # 탐지 회피용 랜덤 딜레이 (마지막 항목 제외)
            if idx < len(self.개별_대학_URL) - 1:
                딜레이 = random.uniform(3.0, 7.0)
                logging.info(f"[딜레이] 다음 대학까지 {딜레이:.1f}초 대기...")
                time.sleep(딜레이)

    def _대학페이지_깊이_탐색(self, page, context, url: str, 대학명: str, 깊이: int = 0):
        """
        대학 페이지에서 PDF를 찾습니다.
        직접 PDF가 없으면 공지사항/모집요강 게시판으로 진입합니다.
        모든 Playwright 시도 실패 시 BS4 폴백으로 재탐색합니다.
        """
        if 깊이 > 2:
            return

        if url.startswith("javascript:") or url == "#" or url == "":
            logging.debug(f"[개별대학] javascript: URL 건너뜀: {url[:60]}")
            return

        시작_다운로드_수 = len(self.downloaded)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            page.wait_for_timeout(2500)
            logging.info(f"[개별대학] '{대학명}' 페이지 로드: {page.title()[:50]}")

            # ── 직접 PDF 링크 탐색 ────────────────────────────
            pdf_링크들 = self._페이지_PDF_링크_추출(page)
            for 링크 in pdf_링크들:
                if len(self.downloaded) >= self.max_pdf:
                    return
                self._url_PDF_저장(링크["url"], f"{대학명}_{링크['text']}", page.url)

            if len(self.downloaded) >= self.max_pdf:
                return

            # ── 모집요강/수시 메뉴 진입 (깊이=0) ──────────────
            if 깊이 == 0:
                탐색_셀렉터들 = [
                    "a:has-text('모집요강')",
                    "a:has-text('입학요강')",
                    "a:has-text('모집안내')",
                    "a:has-text('전형안내')",
                    "a:has-text('입시안내')",
                    "a:has-text('입학정보')",
                    "nav a:has-text('수시')",
                    "a:has-text('수시모집')",
                    "a:has-text('수시안내')",
                    "a:has-text('정시모집')",
                    "a:has-text('입학공지')",
                    "a:has-text('공지사항')",
                    "nav a:has-text('입학안내')",
                    "header a:has-text('입학')",
                    ".gnb a:has-text('입학')",
                    ".nav a:has-text('모집')",
                    ".lnb a:has-text('요강')",
                    ".menu a:has-text('모집요강')",
                    "[class*='nav'] a:has-text('모집')",
                    "[class*='menu'] a:has-text('요강')",
                ]
                for 셀렉터 in 탐색_셀렉터들:
                    if len(self.downloaded) > 시작_다운로드_수:
                        break
                    try:
                        링크 = page.locator(셀렉터).first
                        if 링크.count() > 0:
                            href = 링크.get_attribute("href") or ""
                            링크_텍스트 = 링크.inner_text().strip()
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

            # ── 게시판 탐색 (깊이=1) ──────────────────────────
            elif 깊이 == 1:
                self._게시판_탐색(page, context, 대학명)

        except Exception as e:
            logging.warning(f"[개별대학] '{대학명}' Playwright 탐색 오류 (깊이={깊이}): {e}")

        # ── BS4 폴백: 깊이=0에서 새 PDF 미발견 시 ─────────────
        if 깊이 == 0 and len(self.downloaded) == 시작_다운로드_수:
            logging.info(f"[BS4폴백] '{대학명}' Playwright 탐색 미발견 → BeautifulSoup 재시도")
            try:
                bs4_링크들 = BS4_PDF_스캔(url, 대학명, url)
                for 링크 in bs4_링크들:
                    if len(self.downloaded) >= self.max_pdf:
                        break
                    self._url_PDF_저장(링크["url"], 링크["text"], url)
            except Exception as be:
                logging.debug(f"[BS4폴백] '{대학명}' 오류: {be}")

    def _게시판_탐색(self, page, context, 대학명: str):
        """게시판 목록에서 '모집요강' 제목을 찾아 첨부파일을 다운로드합니다."""
        try:
            # 다양한 대학 게시판 HTML 구조 커버
            행_셀렉터들 = [
                "table tbody tr:has(td:has-text('모집요강'))",
                "table tbody tr:has(td:has-text('수시모집요강'))",
                "table tbody tr:has(td:has-text('입학요강'))",
                "table tbody tr:has(td:has-text('요강'))",
                "ul.board li:has(a:has-text('모집요강'))",
                "ul.board li:has(a:has-text('요강'))",
                "div.list-item:has-text('모집요강')",
                ".board-list tr:has(:has-text('요강'))",
                ".bbs-list tr:has(:has-text('요강'))",
                ".notice-list tr:has(:has-text('요강'))",
                ".board-wrap tr:has(:has-text('모집요강'))",
                "li:has(a:has-text('수시'))",
                "tr:has(td:has-text('수시모집'))",
                ".list_cont li:has-text('모집요강')",
                ".board_list li:has-text('요강')",
                "[class*='board'] tr:has(:has-text('요강'))",
                "[class*='list'] tr:has(:has-text('모집요강'))",
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
            if self._다운로드_콜백:
                try:
                    self._다운로드_콜백(경로)
                except Exception as _ce:
                    logging.warning(f"[콜백] URL저장 콜백 오류: {_ce}")
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
    # HTTP 200 + %PDF- 바이너리 시그니처 확인된 URL 목록 (매년 갱신 필요)
    KNOWN_PDFS: list[dict] = [
        # ── SKY ────────────────────────────────────────────────
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
        {
            "title": "고려대학교_2026학년도_수시모집요강",
            "url": "https://oku.korea.ac.kr/attach/202508/1756457523764_0.pdf",
            "출처": "https://oku.korea.ac.kr/",
        },
        # ── 서성한중경이 ────────────────────────────────────────
        {
            "title": "서강대학교_2026학년도_수시모집요강",
            "url": "https://admission.sogang.ac.kr/upload/GUIDES/202509011514258V5BHK.pdf",
            "출처": "https://admission.sogang.ac.kr/enter/html/rolling/guide.asp",
        },
        {
            "title": "성균관대학교_2026학년도_수시모집요강",
            "url": "https://admission.skku.edu/upload/guide/20251015112648Z86375.pdf",
            "출처": "https://admission.skku.edu/admission/html/rolling/guide.html",
        },
        {
            "title": "한양대학교_2026학년도_수시모집요강",
            "url": "https://go.hanyang.ac.kr/file/download.do?menu=mojib&file_no=421&type=pdf",
            "출처": "https://go.hanyang.ac.kr/web/mojib/mojib.do?m_type=SUSI&m_year=2026",
        },
        {
            "title": "중앙대학교_2026학년도_수시모집요강",
            "url": "https://admission.cau.ac.kr/file/pdfDown.pdf?sfn=20250609045351746_5c67320918194f48aad6bd9a77ffa626.pdf&ofn=2026%ED%95%99%EB%85%84%EB%8F%84+%EC%A4%91%EC%95%99%EB%8C%80%ED%95%99%EA%B5%90+%EC%88%98%EC%8B%9C%EB%AA%A8%EC%A7%91%EC%9A%94%EA%B0%95_%EA%B3%B5%EA%B3%A0%EC%9A%A9(%EC%B5%9C%EC%A2%85).pdf",
            "출처": "https://admission.cau.ac.kr/",
        },
        {
            "title": "경희대학교_2026학년도_수시모집요강",
            "url": "https://iphak.khu.ac.kr/file/download.do?sfn=20250714101033873_2026%ed%95%99%eb%85%84%eb%8f%84+%ea%b2%bd%ed%9d%ac%eb%8c%80%ed%95%99%ea%b5%90+%ec%88%98%ec%8b%9c+%eb%aa%a8%ec%a7%91%ec%9a%94%ea%b0%95_202507011519_fv.pdf&ofn=2026%ed%95%99%eb%85%84%eb%8f%84+%ea%b2%bd%ed%9d%ac%eb%8c%80%ed%95%99%ea%b5%90+%ec%88%98%ec%8b%9c+%eb%aa%a8%ec%a7%91%ec%9a%94%ea%b0%95_202507011519_fv.pdf",
            "출처": "https://iphak.khu.ac.kr/",
        },
        {
            "title": "이화여자대학교_2026학년도_수시모집요강",
            "url": "https://admission.ewha.ac.kr/upload/GUIDES/202505291554567L7HWF.pdf",
            "출처": "https://admission.ewha.ac.kr/admission/html/rolling/guide.asp",
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
# 자가치유 엔진 (Gemini 기반 동적 스크래핑 전략 생성)
# ─────────────────────────────────────────────────────────────

class 자가치유_엔진:
    """
    대학 PDF 수집 실패 시 Gemini API 로 오류를 분석하고
    대안 스크래핑 코드를 생성·실행하여 복구를 시도합니다.

    성공/실패 이력은 data/logs/self_healing.log 에 기록됩니다.
    """

    _로거 = logging.getLogger("self_healing")

    def __init__(self, api_key: str, save_dir: Path):
        self._api_key = api_key
        self._save_dir = save_dir

    # ── 퍼블릭 진입점 ─────────────────────────────────────────

    def 치유_시도(
        self,
        대학명: str,
        url: str,
        에러_메시지: str,
        스택트레이스: str = "",
    ) -> list[Path]:
        """
        Gemini 에게 에러 분석 + 대안 코드 생성을 요청하고
        생성된 코드를 실행해 PDF 수집을 재시도합니다.

        반환: 복구된 PDF Path 목록 (실패 시 빈 리스트)
        """
        if not self._api_key:
            self._로거.warning(f"[SELF-HEALING SKIP] {대학명}: GEMINI_API_KEY 미설정")
            return []

        self._로거.info(
            f"[SELF-HEALING START] {대학명}\n"
            f"  URL : {url[:100]}\n"
            f"  오류: {에러_메시지[:200]}"
        )

        try:
            코드 = self._Gemini_코드_생성(대학명, url, 에러_메시지, 스택트레이스)
        except Exception as e:
            self._로거.error(f"[SELF-HEALING FAIL] {대학명}: Gemini 호출 실패 — {e}")
            return []

        if not 코드:
            self._로거.warning(f"[SELF-HEALING FAIL] {대학명}: 유효한 코드 미생성")
            return []

        self._로거.info(f"[SELF-HEALING] {대학명}: 생성 코드 실행 중 ({len(코드)}자)")
        결과 = self._코드_실행(코드, 대학명, url)

        if 결과:
            self._로거.info(
                f"[SELF-HEALING SUCCESS] {대학명}: {len(결과)}개 PDF 복구\n"
                + "\n".join(f"  ✓ {p.name}" for p in 결과)
            )
        else:
            self._로거.warning(f"[SELF-HEALING FAIL] {대학명}: 복구 코드 실행 후 결과 없음")

        return 결과

    # ── Gemini 코드 생성 ──────────────────────────────────────

    def _Gemini_코드_생성(
        self, 대학명: str, url: str, 에러: str, 트레이스: str
    ) -> str:
        from google import genai
        from google.genai import types

        프롬프트 = textwrap.dedent(f"""
            한국 대학 입학처 웹사이트에서 모집요강 PDF 자동 수집 중 오류가 발생했습니다.

            ## 실패 정보
            - 대학명: {대학명}
            - 시도한 URL: {url}
            - 오류 메시지: {에러[:500]}
            - 스택트레이스:
            {트레이스[:1500]}

            ## 요청
            아래 함수 시그니처를 **정확히** 사용하는 대안 스크래핑 Python 함수를 작성하세요.
            Playwright 없이 requests + BeautifulSoup4 만 사용하세요.

            ```python
            def 자가치유_수집(
                url: str,
                대학명: str,
                save_dir,          # pathlib.Path
                url_해시_fn,       # url_해시(url) -> str
                안전_파일명_fn,    # 안전_파일명(text) -> str
                PDF검증_fn,        # PDF_바이너리_검증(data) -> bool
            ) -> list:             # 저장 성공한 pathlib.Path 목록
                import requests
                from bs4 import BeautifulSoup
                from pathlib import Path
                from urllib.parse import urljoin
                # ... 구현 ...
            ```

            ## 전략 힌트 (하나 이상 시도)
            1. 다른 셀렉터 (CSS class, id, aria-label)로 PDF 링크 찾기
            2. onclick 속성, data-* 속성에서 파일 경로 추출
            3. iframe src 에서 PDF URL 추출
            4. 대학 공지사항/입학안내 API 엔드포인트 (/api/, /json/ 등) 직접 호출
            5. Referer 헤더를 '{url}' 로 설정해 쿠키 우회

            ## 규칙
            - 코드 블록(```python ... ```)으로만 응답
            - 설명은 코드 주석으로
            - 함수명은 반드시 '자가치유_수집'
            - PDF 검증: data[:5] == b"%PDF-"
        """).strip()

        client = genai.Client(api_key=self._api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=프롬프트,
            config=types.GenerateContentConfig(temperature=0.15, max_output_tokens=2048),
        )

        raw = resp.text.strip()
        self._로거.debug(f"[SELF-HEALING] Gemini 응답 ({len(raw)}자):\n{raw[:300]}...")

        # 코드 블록 추출
        m = re.search(r"```python\s*(.*?)```", raw, re.DOTALL)
        if m:
            코드 = m.group(1).strip()
        elif "def 자가치유_수집" in raw:
            코드 = raw
        else:
            return ""

        # 최소 검증: 함수 선언과 return 포함 여부
        if "def 자가치유_수집" not in 코드 or "return" not in 코드:
            self._로거.warning("[SELF-HEALING] 생성된 코드가 유효하지 않음 (함수/return 없음)")
            return ""

        return 코드

    # ── 코드 실행 ─────────────────────────────────────────────

    def _코드_실행(self, 코드: str, 대학명: str, url: str) -> list[Path]:
        """생성된 코드를 제한된 네임스페이스에서 안전하게 실행합니다."""
        try:
            import requests as _requests
            from bs4 import BeautifulSoup as _BS
        except ImportError as e:
            self._로거.error(f"[SELF-HEALING] 의존성 없음: {e}")
            return []

        # 허용된 builtins 만 노출
        _allowed = (
            "print", "len", "range", "str", "int", "float", "list",
            "dict", "set", "tuple", "bool", "bytes", "bytearray",
            "isinstance", "enumerate", "zip", "sorted", "min", "max",
            "open", "hasattr", "getattr", "repr", "type",
            "Exception", "ValueError", "KeyError", "TypeError",
            "StopIteration", "RuntimeError",
        )
        _builtins_dict = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
        _safe_builtins = {k: _builtins_dict[k] for k in _allowed if k in _builtins_dict}

        exec_globals: dict = {
            "__builtins__": _safe_builtins,
            "requests": _requests,
            "BeautifulSoup": _BS,
            "re": re,
            "time": time,
            "random": random,
            "Path": Path,
            "logging": logging,
            "urljoin": urljoin,
            "hashlib": hashlib,
        }
        exec_locals: dict = {}

        try:
            exec(코드, exec_globals, exec_locals)   # noqa: S102
        except Exception as e:
            self._로거.error(f"[SELF-HEALING] exec 실패: {e}\n{traceback.format_exc()}")
            return []

        함수 = exec_locals.get("자가치유_수집")
        if not callable(함수):
            self._로거.warning("[SELF-HEALING] 자가치유_수집 함수를 찾을 수 없음")
            return []

        try:
            결과 = 함수(url, 대학명, self._save_dir, url_해시, 안전_파일명, PDF_바이너리_검증)
            return [p for p in (결과 or []) if isinstance(p, Path) and p.exists()]
        except Exception as e:
            self._로거.error(
                f"[SELF-HEALING] 자가치유_수집 실행 오류: {e}\n{traceback.format_exc()}"
            )
            return []


# ─────────────────────────────────────────────────────────────
# 스크래퍼 에러 로거 (scraper_runtime_errors.json)
# ─────────────────────────────────────────────────────────────

class 스크래퍼_에러_로거:
    """스크래핑 오류를 JSON 파일에 기록합니다."""
    _경로 = Path(__file__).parent.parent / "data" / "logs" / "scraper_runtime_errors.json"
    _net = logging.getLogger("http_network")

    @classmethod
    def 기록(cls, 에러: Exception, 대학명: str, url: str = "") -> dict:
        오류_항목 = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "university": 대학명,
            "url": url[:200],
            "error_type": type(에러).__name__,
            "error_message": str(에러)[:500],
            "traceback": traceback.format_exc(),
        }
        cls._경로.parent.mkdir(parents=True, exist_ok=True)
        목록: list = []
        if cls._경로.exists():
            try:
                with open(cls._경로, encoding="utf-8") as _f:
                    목록 = json.load(_f)
            except Exception:
                목록 = []
        목록.append(오류_항목)
        목록 = 목록[-50:]
        with open(cls._경로, "w", encoding="utf-8") as _f:
            json.dump(목록, _f, ensure_ascii=False, indent=2)
        cls._net.info(
            f"[에러기록] {대학명} 오류 저장 → {cls._경로.name}: "
            f"{type(에러).__name__}: {str(에러)[:120]}"
        )
        return 오류_항목


# ─────────────────────────────────────────────────────────────
# 스크래퍼 핫픽스 엔진 (pdf_collector.py 자가 수정 + 재시작)
# ─────────────────────────────────────────────────────────────

class 스크래퍼_핫픽스_엔진:
    """
    스크래핑 치명 오류 발생 시 Gemini 에게 pdf_collector.py 전체 수정을 요청하고
    백업 후 파일을 덮어쓴 다음 restart_agent.sh 로 시스템을 재시작합니다.
    """
    _스크립트 = Path(__file__)
    _재시작_sh = Path(__file__).parent.parent / "restart_agent.sh"
    _heal_log = logging.getLogger("self_healing")

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._치유_진행 = False

    def 치유_실행(self, 오류_항목: dict):
        if self._치유_진행 or not self._api_key:
            return
        t = threading.Thread(target=self._핫픽스_루프, args=(오류_항목,), daemon=True)
        t.start()

    def _핫픽스_루프(self, 오류_항목: dict):
        import ast as _ast
        self._치유_진행 = True
        self._heal_log.info(
            f"[스크래퍼_핫픽스] 핫픽스 루프 시작 — 대학: {오류_항목.get('university', '?')}"
        )
        try:
            소스코드 = self._스크립트.read_text(encoding="utf-8")
            수정코드 = self._Gemini_핫픽스_요청(소스코드, 오류_항목)
            if not 수정코드:
                self._heal_log.warning("[스크래퍼_핫픽스] Gemini가 유효한 수정 코드를 반환하지 않음")
                return
            try:
                _ast.parse(수정코드)
            except SyntaxError as _se:
                self._heal_log.error(f"[스크래퍼_핫픽스] 수정 코드 문법 오류 — 적용 취소: {_se}")
                return
            백업_경로 = self._스크립트.with_suffix(".py.bak")
            백업_경로.write_text(소스코드, encoding="utf-8")
            self._스크립트.write_text(수정코드, encoding="utf-8")
            self._heal_log.info(
                "[스크래퍼_핫픽스] 핫픽스 적용 완료. 시스템 재시작 중..."
            )
            subprocess.Popen(
                ["bash", str(self._재시작_sh)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as _e:
            self._heal_log.error(
                f"[스크래퍼_핫픽스] 핫픽스 루프 예외: {_e}\n{traceback.format_exc()}"
            )
        finally:
            self._치유_진행 = False

    def _Gemini_핫픽스_요청(self, 소스코드: str, 오류_항목: dict) -> str | None:
        try:
            from google import genai
            from google.genai import types as _types
            소스_요약 = 소스코드[:8000] + "\n...[중략]...\n" + 소스코드[-4000:]
            프롬프트 = (
                "당신은 시니어 Python 디버거입니다. 아래 스크래퍼 오류를 분석하고 수정된 전체 소스코드를 반환하세요.\n\n"
                f"## 오류 정보\n"
                f"- 대학명: {오류_항목.get('university', '?')}\n"
                f"- URL: {오류_항목.get('url', '?')}\n"
                f"- 에러 타입: {오류_항목.get('error_type', '?')}\n"
                f"- 에러 메시지: {오류_항목.get('error_message', '?')}\n"
                f"- 트레이스백:\n{오류_항목.get('traceback', '')[:2000]}\n\n"
                f"## 현재 소스코드 (앞 8000자 + 뒤 4000자)\n```python\n{소스_요약}\n```\n\n"
                "## 요구사항\n"
                "1. 오류를 수정한 완전한 Python 소스코드를 반환하세요\n"
                "2. 기존 아키텍처(클래스/함수/로직)를 최대한 유지하세요\n"
                "3. 반드시 ```python ... ``` 코드 블록으로만 응답하세요\n"
                "4. 모든 주석과 로그 메시지는 한국어로 작성하세요"
            )
            client = genai.Client(api_key=self._api_key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=프롬프트,
                config=_types.GenerateContentConfig(temperature=0.1, max_output_tokens=16384),
            )
            raw = resp.text.strip()
            m = re.search(r"```python\s*(.*?)```", raw, re.DOTALL)
            if m:
                return m.group(1).strip()
            if raw.startswith("import ") or "def " in raw[:200]:
                return raw
            return None
        except Exception as _e:
            self._heal_log.error(f"[스크래퍼_핫픽스] Gemini 요청 실패: {_e}")
            return None


# ─────────────────────────────────────────────────────────────
# 경로 상수 및 기본 대학 목록
# ─────────────────────────────────────────────────────────────

_프로젝트_루트     = Path(__file__).parent.parent
_저장경로          = _프로젝트_루트 / "data" / "raw_pdf"
_RAW_TEXT_DIR      = _프로젝트_루트 / "data" / "raw_text"
_UNIVERSITY_LIST   = _프로젝트_루트 / "data" / "university_list.txt"
_PARSED_JSON       = _프로젝트_루트 / "data" / "student" / "parsed_admission_guide.json"
_PDF_PARSER_SCRIPT = _프로젝트_루트 / "scripts" / "pdf_parser.py"

CHECK_INTERVAL_SEC = 6 * 60 * 60  # 6시간

# 스크래퍼 핫픽스 엔진 싱글턴 — 치명 오류 발생 시 자가 수정 트리거
_스크래퍼_핫픽스 = 스크래퍼_핫픽스_엔진(_GEMINI_API_KEY)

_DEFAULT_UNIV_LIST = [
    "서울대학교", "연세대학교", "고려대학교",
    "서강대학교", "성균관대학교", "한양대학교", "중앙대학교",
    "경희대학교", "이화여자대학교", "서울시립대학교",
    "숭실대학교", "광운대학교", "한국외국어대학교", "인천대학교",
    "건국대학교", "홍익대학교", "숙명여자대학교", "세종대학교",
    "단국대학교", "부산대학교", "경북대학교",
    "아주대학교", "충남대학교", "전남대학교",
    "성신여자대학교", "덕성여자대학교", "가천대학교",
    "국민대학교", "동국대학교", "인하대학교", "포항공과대학교",
]


# ─────────────────────────────────────────────────────────────
# 대학 목록 로더
# ─────────────────────────────────────────────────────────────

def 대학_목록_로드() -> list[str]:
    """
    data/university_list.txt 에서 대학 목록을 읽습니다.
    파일이 없으면 기본 31개 대학으로 자동 생성합니다.
    # 로 시작하는 줄은 주석으로 무시합니다.
    """
    if not _UNIVERSITY_LIST.exists():
        _UNIVERSITY_LIST.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 수집 대상 대학 목록 — 한 줄에 대학 이름 하나\n"
            "# 추가·삭제 후 저장하면 다음 사이클부터 자동 반영됩니다.\n\n"
        )
        _UNIVERSITY_LIST.write_text(
            header + "\n".join(_DEFAULT_UNIV_LIST) + "\n", encoding="utf-8"
        )
        logging.info(f"[목록] university_list.txt 없음 → 기본 {len(_DEFAULT_UNIV_LIST)}개 대학으로 생성")

    lines = _UNIVERSITY_LIST.read_text(encoding="utf-8").splitlines()
    목록 = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    logging.info(f"[목록] {_UNIVERSITY_LIST.name} 로드: {len(목록)}개 대학")
    return 목록


# ─────────────────────────────────────────────────────────────
# 증분 전략 결정기
# ─────────────────────────────────────────────────────────────

def 증분_대상_결정(
    전체_대학_목록: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """
    university_list.txt 의 대학 목록을 아래 두 소스와 교차 확인합니다.
      1) data/raw_pdf/   — 파일명 접두사로 PDF 존재 여부 판단
      2) parsed_admission_guide.json — 파싱 성공/실패 이력 파악

    Returns:
        신규_수집  : PDF 없고 파싱 이력 없는 대학  → 이번 사이클 수집 필요
        재수집_실패: 파싱 실패 이력 있는 대학       → 재수집 필요
        스킵       : 파싱 성공 기록 있는 대학       → 건너뜀 (Gemini 쿼터 절약)
    """
    import json as _json

    # 1. raw_pdf/ 파일명 접두사로 PDF 보유 대학 파악
    PDF보유: set[str] = set()
    _저장경로.mkdir(parents=True, exist_ok=True)
    for pdf in _저장경로.glob("*.pdf"):
        for 대학 in 전체_대학_목록:
            if pdf.name.startswith(대학):
                PDF보유.add(대학)
                break

    # 2. parsed_admission_guide.json 파싱 이력 파악
    파싱_성공: set[str] = set()
    파싱_실패: set[str] = set()
    if _PARSED_JSON.exists():
        try:
            with open(_PARSED_JSON, encoding="utf-8") as f:
                data = _json.load(f)
            for 항목 in data.get("대학_목록", []):
                raw_name = (항목.get("대학명") or "").strip()
                # university_list 항목과 부분 매칭 (예: "숭실대학교" ↔ "숭실대학교")
                매칭 = next(
                    (u for u in 전체_대학_목록 if u in raw_name or raw_name in u),
                    None,
                )
                if not 매칭:
                    continue
                if 항목.get("파싱_실패"):
                    파싱_실패.add(매칭)
                else:
                    파싱_성공.add(매칭)
        except Exception as e:
            logging.warning(f"[증분] JSON 로드 실패: {e}")

    신규_수집:   list[str] = []
    재수집_실패: list[str] = []
    스킵:        list[str] = []

    for 대학 in 전체_대학_목록:
        if 대학 in 파싱_성공 and 대학 not in 파싱_실패:
            스킵.append(대학)
        elif 대학 in 파싱_실패:
            재수집_실패.append(대학)
        else:
            신규_수집.append(대학)

    logging.info(
        f"[증분] 전체 {len(전체_대학_목록)}개 | "
        f"✓ 완료(스킵) {len(스킵)}개 | "
        f"★ 신규 {len(신규_수집)}개 | "
        f"↩ 재수집(실패) {len(재수집_실패)}개"
    )
    if 스킵:
        logging.info(f"[증분] 스킵: {', '.join(스킵)}")
    if 신규_수집:
        logging.info(f"[증분] 신규 수집 대상: {', '.join(신규_수집)}")
    if 재수집_실패:
        logging.info(f"[증분] 재수집 대상(실패): {', '.join(재수집_실패)}")

    return 신규_수집, 재수집_실패, 스킵


# ─────────────────────────────────────────────────────────────
# 증분 PDF 수집 실행기
# ─────────────────────────────────────────────────────────────

def 증분_수집_실행(수집_대상: list[str]) -> list[Path]:
    """
    수집_대상 대학에 해당하는 PDF만 선별하여 수집합니다.
    직접 URL → Playwright 순서로 시도하며 대학 간 3~7초 랜덤 딜레이를 적용합니다.
    """
    if not 수집_대상:
        return []

    대상_세트 = set(수집_대상)
    수집_결과: list[Path] = []

    # ── 1. 직접 URL 수집 (대상 대학 해당 항목만) ──────────────
    필터_직접 = [
        항목 for 항목 in 직접URL_수집기.KNOWN_PDFS
        if any(대학 in 항목.get("title", "") for 대학 in 대상_세트)
    ]

    net_log = logging.getLogger("http_network")
    if 필터_직접:
        net_log.info(f"[직접URL수집] {len(필터_직접)}개 URL 다운로드 시작")
        for i, 항목 in enumerate(필터_직접):
            url   = 항목.get("url", "")
            제목  = 항목.get("title", "모집요강")
            출처  = 항목.get("출처", "")
            파일명 = f"{안전_파일명(제목)}_{url_해시(url)}.pdf"
            경로  = _저장경로 / 파일명

            if 중복_확인(_저장경로, url):
                net_log.info(f"[직접URL수집] 중복 건너뜀: {제목}")
                continue

            net_log.info(f"[직접URL수집] ▶ 다운로드: {제목}")
            if requests_PDF_다운로드(url, 경로, 출처):
                수집_결과.append(경로)
                net_log.info(f"[직접URL수집] ✓ 완료: {제목} → 즉시 파싱 시작")
                파서_즉시_실행(경로)

            if i < len(필터_직접) - 1:
                딜레이 = random.uniform(3.0, 7.0)
                net_log.info(f"[딜레이] 다음 대학까지 {딜레이:.1f}초 대기...")
                time.sleep(딜레이)

    # ── 2. Playwright 탐색 (직접 URL로 수집 안 된 대학) ───────
    직접_수집된: set[str] = set()
    for pdf in 수집_결과:
        for 대학 in 수집_대상:
            if pdf.name.startswith(대학):
                직접_수집된.add(대학)
                break

    playwright_대상 = [d for d in 수집_대상 if d not in 직접_수집된]
    필터_개별 = [
        항목 for 항목 in 모집요강_수집기.개별_대학_URL
        if 항목.get("이름", "") in playwright_대상
    ]

    if 필터_개별:
        net_log.info(f"[Playwright수집] Playwright 탐색 대상 {len(필터_개별)}개 대학 — 다운로드 즉시 파싱 활성화")
        수집기 = 모집요강_수집기(
            save_dir=_저장경로,
            max_pdf=len(playwright_대상) * 2,
            headless=True,
            _다운로드_콜백=파서_즉시_실행,
        )
        수집기.개별_대학_URL = 필터_개별  # 인스턴스 변수로 클래스 변수 가림
        수집_결과 += 수집기.실행()

    # ── 3. 자가치유 엔진 — 결과 없는 대학 Gemini 재시도 ─────────
    if _GEMINI_API_KEY:
        # 수집 완료된 대학 세트 (파일명 접두사로 판별)
        수집된_대학들: set[str] = set()
        for pdf in 수집_결과:
            for 대학 in 수집_대상:
                if 대학[:4] in pdf.name:
                    수집된_대학들.add(대학)

        실패한_대학들 = [u for u in 수집_대상 if u not in 수집된_대학들]

        if 실패한_대학들:
            logging.info(
                f"[자가치유] 실패 대학 {len(실패한_대학들)}개 → Gemini 치유 엔진 실행: "
                + ", ".join(실패한_대학들[:5])
            )
            치유기 = 자가치유_엔진(_GEMINI_API_KEY, _저장경로)

            # URL 조회용 맵: 개별_대학_URL 리스트에서 대학명 → url
            url_맵: dict[str, str] = {
                항목.get("이름", ""): 항목.get("url", "")
                for 항목 in 모집요강_수집기.개별_대학_URL
            }
            # KNOWN_PDFS 에서도 추가
            for 항목 in 직접URL_수집기.KNOWN_PDFS:
                for 대학 in 실패한_대학들:
                    if 대학 in 항목.get("title", ""):
                        url_맵.setdefault(대학, 항목.get("url", ""))

            for 대학 in 실패한_대학들:
                url = url_맵.get(대학, "")
                오류_msg = f"{대학}: Playwright + BS4 + 직접URL 모두 시도했으나 PDF 0개 수집"
                복구_결과 = 치유기.치유_시도(대학, url, 오류_msg)
                for 복구파일 in 복구_결과:
                    수집_결과.append(복구파일)
                    net_log.info(f"[자가치유복구] {대학} → 즉시 파싱: {복구파일.name}")
                    파서_즉시_실행(복구파일)

    return 수집_결과


# ─────────────────────────────────────────────────────────────
# PDF 파서 서브프로세스 실행기
# ─────────────────────────────────────────────────────────────

def 파서_실행(새_PDF: list | None = None) -> bool:
    """
    pdf_parser.py를 서브프로세스로 실행합니다.

    새_PDF 목록이 주어지면 --files / --delta-only 플래그를 붙여
    해당 파일만 Groq/Ollama(Crawl LLM)로 파싱하고 기존 JSON에 병합합니다.
    목록이 없으면 전체 미파싱 스캔 모드로 실행합니다.
    """
    if not _PDF_PARSER_SCRIPT.exists():
        logging.warning(f"[파서] 스크립트 없음: {_PDF_PARSER_SCRIPT}")
        return False

    cmd = [sys.executable, str(_PDF_PARSER_SCRIPT)]
    if 새_PDF:
        파일명_목록 = ",".join(p.name for p in 새_PDF)
        cmd += ["--files", 파일명_목록, "--delta-only"]
        logging.info(
            f"[파서] 증분 모드 — 신규 {len(새_PDF)}개 파일만 파싱:\n"
            + "\n".join(f"  • {p.name}" for p in 새_PDF)
        )
    else:
        logging.info("[파서] 전체 모드 — 미파싱 PDF 전체 처리")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(_프로젝트_루트),
            timeout=3600,
        )
        if result.returncode == 0:
            logging.info("[파서] 완료 (성공)")
            return True
        logging.warning(f"[파서] 종료 코드: {result.returncode}")
        return False
    except subprocess.TimeoutExpired:
        logging.error("[파서] 타임아웃 (1시간 초과)")
        return False
    except Exception as e:
        logging.error(f"[파서] 실행 오류: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# 단일 PDF 즉시 파싱 (다운로드 직후 호출)
# ─────────────────────────────────────────────────────────────

def 파서_즉시_실행(pdf_path: Path) -> bool:
    """
    PDF 1개를 즉시 파싱하여 parsed_admission_guide.json 에 실시간 반영합니다.
    다운로드 완료 콜백에서 호출되므로 블로킹 방식으로 순차 실행합니다.
    """
    net_log = logging.getLogger("http_network")
    net_log.info(f"[즉시파싱] ▶ 시작: {pdf_path.name}")
    try:
        결과 = 파서_실행([pdf_path])
        if 결과:
            net_log.info(f"[즉시파싱] ✓ 완료: {pdf_path.name} → parsed_admission_guide.json 실시간 업데이트")
        else:
            net_log.warning(f"[즉시파싱] ✗ 파싱 실패: {pdf_path.name}")
        return 결과
    except Exception as _e:
        net_log.error(f"[즉시파싱] 예외: {pdf_path.name}: {_e}")
        return False


# ─────────────────────────────────────────────────────────────
# adiga.kr 원문 텍스트 수집
# ─────────────────────────────────────────────────────────────

def adiga_텍스트_수집(대학코드: str, 대학명: str) -> str:
    """
    adiga.kr 대학 정보 페이지에서 입시 관련 원문 텍스트를 스크랩합니다.
    수집된 텍스트를 data/raw_text/{대학명}_adiga_{날짜}.txt 에 저장하고 반환합니다.
    """
    import requests
    from bs4 import BeautifulSoup

    net_log = logging.getLogger("http_network")
    _RAW_TEXT_DIR.mkdir(parents=True, exist_ok=True)

    BASE = "https://www.adiga.kr"
    url = f"{BASE}/ucp/uvt/uni/univView.do?univCd={대학코드}&menuId=PCUVTINF2000"

    net_log.info(f"[adiga텍스트] {대학명} ({대학코드}) 텍스트 수집 시작: {url}")

    headers = {
        "User-Agent": UA_가져오기(),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": BASE,
    }

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 불필요한 태그 제거
        for 태그 in soup.find_all(["script", "style", "nav", "header", "footer"]):
            태그.decompose()

        # 본문 텍스트 추출 (입시 관련 콘텐츠 우선)
        본문_텍스트: list[str] = []
        # 입시 정보 영역 우선 추출
        for sel in [".board-content", ".view-content", ".cont-area", "#content", "main", "article"]:
            영역 = soup.select_one(sel)
            if 영역:
                본문_텍스트.append(영역.get_text(separator="\n", strip=True))
                break

        if not 본문_텍스트:
            본문_텍스트.append(soup.get_text(separator="\n", strip=True))

        전체_텍스트 = "\n".join(본문_텍스트)
        # 연속 공백/빈줄 정리
        전체_텍스트 = re.sub(r"\n{3,}", "\n\n", 전체_텍스트)
        전체_텍스트 = re.sub(r"[ \t]{2,}", " ", 전체_텍스트)

        if len(전체_텍스트.strip()) < 100:
            net_log.warning(f"[adiga텍스트] {대학명}: 추출된 텍스트 너무 짧음 ({len(전체_텍스트)}자) — 건너뜀")
            return ""

        # 파일 저장
        날짜_태그 = datetime.now().strftime("%Y%m%d")
        저장_경로 = _RAW_TEXT_DIR / f"{안전_파일명(대학명)}_adiga_{날짜_태그}.txt"
        저장_경로.write_text(전체_텍스트, encoding="utf-8")
        net_log.info(f"[adiga텍스트] {대학명}: {len(전체_텍스트):,}자 저장 → {저장_경로.name}")
        return 전체_텍스트

    except Exception as _e:
        net_log.warning(f"[adiga텍스트] {대학명} 수집 실패: {_e}")
        return ""


# ─────────────────────────────────────────────────────────────
# 텍스트 데이터 즉시 파싱 (Groq/Ollama Crawl LLM → JSON 병합)
# ─────────────────────────────────────────────────────────────

def 텍스트_즉시_파싱(텍스트: str, 대학명: str) -> bool:
    """
    원문 텍스트를 Groq/Ollama(Crawl LLM)로 직접 분석하여 parsed_admission_guide.json 에 실시간 병합합니다.
    pdf_parser.py 를 거치지 않고 인라인으로 처리합니다 (Gemini 토큰 미사용).
    """
    if not 텍스트:
        return False

    net_log = logging.getLogger("http_network")
    net_log.info(f"[텍스트파싱] {대학명}: Crawl LLM 인라인 파싱 시작 ({len(텍스트):,}자)")

    try:
        # 텍스트 길이 제한 (토큰 절약)
        텍스트_제한 = 텍스트[:30_000]

        EXTRACTION_PROMPT_TEXT = """
당신은 대한민국 대학 입시 전문가입니다.
아래 대학 입시 안내 텍스트에서 입시 정보를 추출하여 JSON으로 반환하세요.

반드시 아래 JSON 형식으로만 응답하세요:
```json
{
  "대학명": "대학교 이름",
  "학년도": "20XX학년도",
  "문서_유형": "adiga.kr 텍스트 데이터",
  "수시_전형목록": [
    {
      "전형명": "전형명",
      "전형_유형": "학생부교과|학생부종합|논술|기타",
      "모집인원": null,
      "전형요소_반영비율": {},
      "수능최저학력기준": {"적용여부": false, "기준_상세": null},
      "비고": null
    }
  ],
  "파싱_신뢰도": "높음|중간|낮음",
  "파싱_비고": "특이사항"
}
```
""".strip()

        프롬프트 = f"대학명: {대학명}\n\n--- 텍스트 시작 ---\n{텍스트_제한}\n--- 텍스트 끝 ---\n\n{EXTRACTION_PROMPT_TEXT}"

        응답_텍스트, 엔진명 = _tm.generate_text_sync(프롬프트, force_engine="crawl")
        if not 응답_텍스트:
            net_log.warning(f"[텍스트파싱] {대학명}: Groq/Ollama 모두 실패")
            return False

        # JSON 추출
        cleaned = re.sub(r"```(?:json)?\s*", "", 응답_텍스트)
        cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start == -1 or end == 0:
            net_log.warning(f"[텍스트파싱] {대학명}: JSON 추출 실패")
            return False

        파싱_결과: dict = json.loads(cleaned[start:end])
        파싱_결과.setdefault("대학명", 대학명)
        파싱_결과["_소스_파일"] = f"{안전_파일명(대학명)}_adiga_text"
        파싱_결과["_파싱_방식"] = f"adiga.kr 텍스트 인라인 ({엔진명})"
        파싱_결과["_파싱_시각"] = datetime.now().isoformat(timespec="seconds")

        # parsed_admission_guide.json 에 델타 병합
        _PARSED_JSON.parent.mkdir(parents=True, exist_ok=True)
        기존: dict = {}
        if _PARSED_JSON.exists():
            try:
                with open(_PARSED_JSON, encoding="utf-8") as _f:
                    기존 = json.load(_f)
            except Exception:
                기존 = {}

        대학_목록: list = 기존.get("대학_목록", [])
        소스키 = 파싱_결과["_소스_파일"]
        # 동일 소스 파일 항목 교체
        대학_목록 = [x for x in 대학_목록 if x.get("_소스_파일") != 소스키]
        대학_목록.append(파싱_결과)
        대학_목록.sort(key=lambda x: x.get("_소스_파일", ""))

        성공_수 = sum(1 for x in 대학_목록 if not x.get("파싱_실패"))
        기존.update({
            "생성_일시": datetime.now().isoformat(timespec="seconds"),
            "총_PDF_수": len(대학_목록),
            "파싱_성공_수": 성공_수,
            "파싱_실패_수": len(대학_목록) - 성공_수,
            "대학_목록": 대학_목록,
        })

        with open(_PARSED_JSON, "w", encoding="utf-8") as _f:
            json.dump(기존, _f, ensure_ascii=False, indent=2)

        net_log.info(f"[텍스트파싱] ✓ {대학명}: parsed_admission_guide.json 실시간 업데이트 완료")
        return True

    except Exception as _e:
        net_log.error(f"[텍스트파싱] {대학명} 오류: {_e}")
        return False


# ─────────────────────────────────────────────────────────────
# 단일 수집 사이클
# ─────────────────────────────────────────────────────────────

def 단일_사이클_실행() -> bool:
    """
    university_list.txt 파싱 → adiga 텍스트 수집 → 증분 PDF 수집
    → 즉시 파싱(다운로드 콜백) → 안전망 배치 파싱 → 로그 알림

    각 단계에서 예외 발생 시 scraper_runtime_errors.json 에 기록하고
    스크래퍼 핫픽스 엔진을 트리거합니다.
    """
    net_log = logging.getLogger("http_network")
    net_log.info("=" * 62)
    net_log.info(f"  [수집사이클 시작] {datetime.now():%Y-%m-%d %H:%M:%S}")
    net_log.info("=" * 62)

    try:
        # Step 1: university_list.txt 읽기
        전체_목록 = 대학_목록_로드()

        # Step 2: raw_pdf/ + parsed_json 교차 확인 → 수집 대상 결정
        신규, 재수집, 스킵 = 증분_대상_결정(전체_목록)
        수집_대상 = 신규 + 재수집

        if not 수집_대상:
            net_log.info("[사이클] 모든 대학 파싱 완료 상태 — 이번 사이클 수집 없음")
            return False

        # Step 3: adiga.kr 텍스트 수집 (PDF 없이 즉시 파싱 가능한 대학만)
        net_log.info(f"[adiga텍스트단계] 수집 대상 {len(수집_대상)}개 대학 adiga.kr 텍스트 수집 시도")
        # ADIGA 대학 코드 맵 (모집요강_수집기.ADIGA_대학목록 활용)
        _코드_맵: dict[str, str] = {
            d["이름"]: d["코드"]
            for d in 모집요강_수집기.ADIGA_대학목록
        }
        for 대학 in 수집_대상:
            코드 = _코드_맵.get(대학)
            if not 코드:
                continue
            try:
                텍스트 = adiga_텍스트_수집(코드, 대학)
                if 텍스트:
                    net_log.info(f"[adiga텍스트단계] {대학} 텍스트 수집 완료 → 즉시 파싱")
                    텍스트_즉시_파싱(텍스트, 대학)
            except Exception as _te:
                net_log.warning(f"[adiga텍스트단계] {대학} 처리 실패 (비치명): {_te}")

        # Step 4: 대상 대학만 PDF 수집 (다운로드 직후 파서_즉시_실행 콜백 실행)
        새_PDF = 증분_수집_실행(수집_대상)

        if not 새_PDF:
            net_log.warning("[사이클] 새로 수집된 PDF 없음 — 안전망 파싱 건너뜀")
            # adiga 텍스트 파싱만 완료된 경우도 성공으로 간주
            return True

        net_log.info(f"[사이클] 새 PDF {len(새_PDF)}개 수집 완료:")
        for _p in 새_PDF:
            net_log.info(f"  ✓ {_p.name}")

        # Step 5: 안전망 배치 파싱 — 즉시 파싱 실패한 파일 캐치
        # (pdf_parser.py 내부에서 기존 성공 항목은 자동 스킵됨)
        net_log.info("[사이클] 안전망 배치 파싱 실행 (미파싱 파일 전체 검사)...")
        파싱_성공 = 파서_실행(새_PDF)

        if 파싱_성공:
            net_log.info("")
            net_log.info("━" * 62)
            net_log.info("[UPDATE] New university data merged successfully.")
            net_log.info("━" * 62)
            net_log.info("")
            return True

        net_log.warning("[사이클] 안전망 배치 파싱 실패 — 즉시 파싱 결과는 이미 반영됨")
        return False

    except Exception as _사이클_에러:
        net_log.error(f"[사이클오류] 치명 예외 발생: {_사이클_에러}\n{traceback.format_exc()}")
        에러_항목 = 스크래퍼_에러_로거.기록(_사이클_에러, "단일_사이클_실행", "")
        _스크래퍼_핫픽스.치유_실행(에러_항목)
        return False


# ─────────────────────────────────────────────────────────────
# 스케줄러 (6시간 인터벌 무한 루프)
# ─────────────────────────────────────────────────────────────

def 스케줄_루프(interval_sec: int = CHECK_INTERVAL_SEC) -> None:
    """
    interval_sec 마다 단일_사이클_실행()을 반복합니다 (기본 6시간).
    Ctrl+C 로 안전하게 종료됩니다.
    """
    logging.info("━" * 62)
    logging.info("  모집요강 PDF 자동 수집기 — 스케줄 모드 시작")
    logging.info(f"  수집 주기 : {interval_sec // 3600}시간 ({interval_sec:,}초)")
    logging.info(f"  university_list: {_UNIVERSITY_LIST}")
    logging.info(f"  PDF 저장 경로  : {_저장경로}")
    logging.info("━" * 62)

    사이클_번호 = 0
    logging.info("[스케줄] 즉시 첫 번째 수집 사이클 시작 (기동 직후 지연 없음)")
    while True:
        사이클_번호 += 1
        logging.info(f"\n[스케줄] ━━━ 사이클 #{사이클_번호} ━━━")

        try:
            단일_사이클_실행()
        except KeyboardInterrupt:
            logging.info("[스케줄] Ctrl+C 감지 — 정상 종료")
            break
        except Exception as e:
            logging.error(f"[스케줄] 사이클 #{사이클_번호} 오류: {e}", exc_info=True)
            logging.info("[스케줄] 30초 후 재시도...")
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                logging.info("[스케줄] Ctrl+C 감지 — 종료")
                break
            continue

        다음_예정 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(
            f"[스케줄] 사이클 #{사이클_번호} 완료 "
            f"→ {interval_sec // 3600}시간 대기 후 재실행 "
            f"(현재 {다음_예정})"
        )
        try:
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            logging.info("[스케줄] Ctrl+C 감지 — 종료")
            break


# ─────────────────────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────────────────────

def main():
    로깅_설정()
    logging.info("━" * 62)
    logging.info("  대학 입시 모집요강 PDF 자동 수집기 v3 (스케줄+증분)")
    logging.info("━" * 62)

    의존성_설치()
    _저장경로.mkdir(parents=True, exist_ok=True)

    import argparse
    ap = argparse.ArgumentParser(description="모집요강 PDF 자동 수집기")
    ap.add_argument(
        "--once", action="store_true",
        help="1회만 실행 후 종료 (스케줄 없음)",
    )
    ap.add_argument(
        "--interval", type=int, default=CHECK_INTERVAL_SEC,
        metavar="SEC",
        help=f"수집 주기 (초, 기본 {CHECK_INTERVAL_SEC} = 6시간)",
    )
    args = ap.parse_args()

    if args.once:
        logging.info("[메인] --once 모드: 1회 실행 후 종료")
        결과 = 단일_사이클_실행()
        return 0 if 결과 else 1

    스케줄_루프(interval_sec=args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
