# UnivAgent 개발 히스토리 로그

최신 순으로 정렬합니다.

---

## [2026-05-31] Daily Backup System

**목적**: 시스템 재시작 시 자동 백업으로 데이터 손실 방지

**주요 변경:**
- `scripts/daily_backup.py` 신규 생성
  - `data/backups/UnivAgent_Backup_YYYYMMDD_HHMMSS.tar.gz` 생성
  - 포함 대상: `scripts/`, `.env`, `data/*.db`
  - 7일 초과 구백업 자동 삭제 (mtime + 파일명 타임스탬프 이중 기준)
- `restart_agent.sh` 업데이트: DB 최적화(6-B) 직후 백업(6-C) 단계 삽입

---

## [2026-05-31] DB Schema Centralization Hotfix

**목적**: 부팅 시 발생한 `AttributeError` 및 DB 스키마 분산 문제 해결

**근본 원인**: `telegram_agent.py`가 `db_manager`에 더 이상 존재하지 않는 레거시 함수 4개(`load_admission_data`, `get_all_universities`, `search_departments`, `get_admission_plan_detail`)를 호출

**주요 변경:**
- `db_manager.py`: 영문 표준 진입점 `init_db()` 추가 (docstring에 전체 스키마 명세 내장)
- `telegram_agent.py`: 레거시 4개 함수 → 현행 db_manager API로 교체
  - `get_all_universities()` → `입시_대학_목록()`
  - `search_departments()` → `입시_대학_검색()` + `입시_전형_검색()` 교차 검색
  - `get_admission_plan_detail()` → `입시_전형_검색()[0]`
  - `load_admission_data()` → `DB_초기화()`
- 스타트업 호출: `db_manager.DB_초기화()` → `db_manager.init_db()`

---

## [2026-05-30] Async Post-Verification Pipeline

**목적**: Ollama 1차 답변을 Gemini가 사후 검증해 품질 보장

**주요 변경:**
- `db_manager.py`: `pending_verifications` 테이블 + CRUD 4개 추가
- `telegram_agent.py`:
  - `_OLLAMA_UX_CAPTION` 상수: Ollama 폴백 시 사용자 안내 메시지
  - `_질문_처리_및_큐잉()` 함수: Ollama 감지 시 자동 큐잉 + 캡션 발송
  - `text_handler` 마지막 라인을 `_질문_처리_및_큐잉()` 호출로 교체
- `gemini_verifier.py` 신규 생성 (프로젝트 루트):
  - 5분 간격 무한 루프 데몬
  - `pending` 상태 최대 5건 배치 처리
  - 429 감지 시 배치 즉시 중단 + 다음 주기 연기
  - 검증 완료 시 MarkdownV2 텔레그램 Push

---

## [2026-05-30] MLOps Dashboard Enhancement (3차)

**목적**: 데이터 수집 현황 전용 탭 분리 + API 상태등 추가

**주요 변경:**
- `web_dashboard.py`:
  - 사이드바에 "📡 4. 데이터 수집 현황" 탭 신설
  - 1번 탭의 데이터 수집 지표를 4번 탭으로 이전
  - 7일간 일별 증가량 Plotly 시계열 차트 (`pd.DataFrame.diff().clip(lower=0)`)
  - 엔진별 토큰 사용량 라벨에 헬스 상태등(🟢/🔴) 실시간 표시
- `token_manager.py`:
  - `check_api_health()` 함수 추가 (60초 TTL 캐시)
  - Stage 1: 정규식 포맷 검증 (`^AIza...`, `^gsk_...`)
  - Stage 2: 모델 리스트 엔드포인트 HTTP 호출

---

## [2026-05-30] Boot Hardening & Ollama Health Check

**목적**: 부팅 실패 시 크래시 방지 + Ollama 연결 상태 자동 감지

**주요 변경:**
- `telegram_agent.py`:
  - NameError 버그 수정: `프로젝트_루트` 정의를 env 로드 이전으로 이동
  - `_boot_fatal(reason)` 함수 추가: CRITICAL 로깅 → `sys.exit(1)`
  - `.env` 로드 + `TELEGRAM_TOKEN` 체크를 try-except로 감싸 Graceful Boot Failure 구현
- `token_manager.py`:
  - `_check_ollama_health()`: 모듈 임포트 시 백그라운드 스레드로 포트 11434 ping
  - 실패 시 `system_events.jsonl`에 `OLLAMA_HEALTH_FAIL` WARNING 기록

---

## [2026-05-29] Ollama Training Tab 엔터프라이즈화

**목적**: 무중단 모델 교체 + 컨텍스트 초과 방어 + 추론 파라미터 고정

**주요 변경:**
- `web_dashboard.py`:
  - `_hotswap_pipeline()`: `threading.Thread(daemon=True)` 백그라운드 빌드 (블로킹 제거)
  - `_build_modelfile_system()`: 4,000자 컷오프 방어 (newest-first 슬라이싱)
  - Modelfile에 `PARAMETER temperature 0.3 / num_ctx 8192 / stop "User:"` 자동 삽입
  - 빌드 흐름: `:temp` 빌드 → `:latest`로 hot-swap → `:temp` 삭제

---

## [2026-05-28] Streamlit MLOps 대시보드 초기 구축

**목적**: 시스템 운영 상태를 실시간으로 시각화

**주요 변경:**
- `scripts/web_dashboard.py` 신규 생성
  - 1번 탭: 시스템 요약 (토큰·에러·E2E 결과)
  - 2번 탭: AI 자가치유 (LLM 에러 분석·코드 수정)
  - 3번 탭: 로컬 AI 훈련소 (Ollama 모델 관리)
- `scripts/logger_factory.py` 신규 생성: JSONL 구조화 로깅
- `scripts/devops_reporter.py` 신규 생성: 운영 리포트 자동화
- `scripts/error_checkpoint.py` 신규 생성: 재시작 기준점 관리

---

## [2026-05-27] 텔레그램 봇 UX 전면 개편

**목적**: 고2 맞춤 입력 가이드 + 성적 분석 기능 강화

**주요 변경:**
- `telegram_agent.py`:
  - 고2 프로필 입력 가이드 (/profile)
  - 내신·모의고사 분석 기능
  - 커맨드명 영문화 (`/시작` → `/start` 등)
  - 한국어 키워드 라우팅 (텍스트 → 적절한 핸들러)
  - 사용자 요청 접수 기능 (/request)
- `scripts/storage_manager.py` 신규 생성: JSON 기반 프로필 관리

---

## [2026-05-26] 텔레그램 입시정보 봇 초기 구현

**목적**: 대한민국 대입 정보를 AI로 제공하는 텔레그램 봇 MVP

**주요 변경:**
- `scripts/telegram_agent.py` 초기 구현
  - 3-Tier LLM 라우팅 (Gemini → Groq → Ollama)
  - 입시요강 PDF 파싱 데이터 기반 답변
  - 사용자 프로필 저장 (JSON)
- `scripts/token_manager.py`: LLM 라우팅 엔진
- `scripts/pdf_collector.py`: 대학 입시요강 자동 수집
- `scripts/pdf_parser.py`: PDF → 구조화 JSON 파싱
- `scripts/db_manager.py`: SQLite DB 스키마 관리 시작

---

## 알려진 기술 부채 및 주의사항

| 항목 | 상태 | 설명 |
|---|---|---|
| `storage_manager.py` UserProfileManager | 레거시 | JSON 파일 기반, 향후 `students` 테이블로 완전 이전 예정 |
| `_AI` 전역변수 | 병존 | `token_manager`와 별개로 `genai.GenerativeModel` 인스턴스 유지 중 |
| `__pycache__` 바이너리 | 백업 포함 | `daily_backup.py`가 `scripts/__pycache__/`도 함께 압축 (의도적) |
| Ollama 모델 부재 | 런타임 주의 | `univagent-expert` 모델이 없으면 Tier 3 폴백 실패 → E2E FAIL |
