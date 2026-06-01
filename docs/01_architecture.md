# UnivAgent 시스템 아키텍처

## 1. 프로젝트 개요

UnivAgent는 대한민국 대학 입시 정보를 제공하는 AI 기반 텔레그램 봇 + MLOps 대시보드 통합 시스템입니다.

- **언어**: Python 3.11+
- **프로젝트 루트**: `/Users/jwlee/UnivAgent/`
- **DB**: SQLite (`data/admissions_agent.db`) — `scripts/db_manager.py` 단독 관리
- **로그**: `data/logs/system_events.jsonl` — `scripts/logger_factory.py` 단독 기록

---

## 2. 3-Tier LLM 라우팅 엔진

`scripts/token_manager.py`의 `generate_text_sync()` 함수가 담당합니다.

```
사용자 요청
    │
    ▼
[Tier 1] Gemini (Google Generative AI)
    │  실패 또는 quota 초과 시
    ▼
[Tier 2] Groq (Llama 계열 고속 추론)
    │  실패 시
    ▼
[Tier 3] Ollama (로컬 univagent-expert 모델, 포트 11434)
    │
    ▼
응답 반환 + 사용 엔진명 반환 (튜플)
```

### force_engine 파라미터 바이패스 규칙

| force_engine 값 | 동작 |
|---|---|
| `None` (기본) | Tier 1→2→3 전체 폴백 |
| `"gemini"` / `"code"` | Tier 1(Gemini)만 시도 |
| `"crawl"` | Tier 2(Groq) → Tier 3(Ollama) |
| `"groq"` | Tier 2(Groq)만 시도 |
| `"ollama"` | Tier 3(Ollama)만 시도 |

### 훈련 모드 잠금 (Training Mode Lock)

`system_config.json`의 `training_mode: true` 시, `force_engine=None` 경로는 강제로 Ollama로만 라우팅됩니다. `force_engine="gemini"/"crawl"/"code"` 바이패스는 훈련 모드에서도 허용됩니다.

---

## 3. 비동기 사후 검증 파이프라인 (Async Post-Verification Pipeline)

Ollama(Tier 3)가 응답한 경우 자동으로 Gemini의 사후 팩트체크를 예약하는 2단계 파이프라인입니다.

```
텔레그램 사용자 질문
        │
        ▼
  token_manager.generate_text_sync()
        │
   Ollama로 폴백됨?
        │ YES
        ├─► 사용자에게 1차 답변 전송
        │
        ├─► UX 경고 캡션 발송:
        │     "⚠️ 현재 시스템 접속량 증가로 로컬 AI가 1차 진단을 수행했습니다..."
        │
        └─► db_manager.pending_verification_추가()
                │
                │  (비동기, 별도 프로세스)
                ▼
         gemini_verifier.py 데몬 (5분 간격)
                │
                ├─► pending_verifications WHERE status='pending' LIMIT 5 조회
                │
                ├─► Gemini API 호출 → 팩트체크 + 논리 고도화
                │     429 한도 초과 → 조용히 다음 주기로 연기
                │
                ├─► DB status='verified' + verified_answer 저장
                │
                └─► 텔레그램 Push:
                      "✨ [정밀 검증 완료] 메인 AI가 보완한 최종 대입 처방전이 도착했습니다."
```

### 관련 DB 테이블: `pending_verifications`

| 컬럼 | 설명 |
|---|---|
| `id` | PK (AUTOINCREMENT) |
| `user_id` | 텔레그램 chat_id |
| `query` | 원본 질문 |
| `ollama_answer` | Ollama 1차 답변 |
| `status` | `pending` → `verified` / `failed` |
| `verified_answer` | Gemini 검증 완료 최종 답변 |
| `created_at` / `updated_at` | 타임스탬프 |

---

## 4. 데이터베이스 스키마 (db_manager.py 단독 관리)

| 테이블 | PK | 역할 |
|---|---|---|
| `students` | `chat_id` | 사용자 프로필 (암호화 포함) |
| `admissions_guide` | `id` | 파싱된 입시요강 문서 |
| `user_requests` | `id` | 사용자 문의 큐 |
| `system_metrics` | `id` | 일별 운영 스냅샷 |
| `admin_settings` | `setting_key` | 관리자 설정 키-값 |
| `verified_golden_records` | `id` | 고품질 QA 훈련 예시 |
| `pending_verifications` | `id` | Ollama→Gemini 재검증 대기열 |

> **주의**: `users(id)` 테이블은 존재하지 않습니다. 사용자 키는 `students.chat_id`입니다.

---

## 5. Ollama 로컬 AI 훈련 파이프라인

`scripts/web_dashboard.py`의 "3. 로컬 AI(Ollama) 훈련소" 탭에서 관리합니다.

### Zero-Downtime Hot Swap
```
ollama create univagent-expert:temp -f Modelfile
        │ (백그라운드 스레드)
        ▼
ollama cp univagent-expert:temp univagent-expert:latest
        │
        ▼
ollama rm univagent-expert:temp
```
Streamlit은 `time.sleep(2) + st.rerun()` 폴링으로 진행 상태를 표시합니다.

### Modelfile 핵심 파라미터
```
PARAMETER temperature 0.3    # 팩트 기반 낮은 창의성
PARAMETER num_ctx 8192        # 컨텍스트 윈도우
PARAMETER stop "User:"        # 할루시네이션 방지 스톱 토큰
```

### Context Window 컷오프 방어
- SYSTEM 프롬프트 최대 4,000자 (`_SYSTEM_CONTENT_MAX`)
- 최신 데이터 우선 (newest-first) 슬라이싱

---

## 6. Streamlit MLOps 대시보드

`scripts/web_dashboard.py` — `streamlit run` 으로 포트 8501에서 실행

| 탭 | 내용 |
|---|---|
| 📊 1. 시스템 요약 | 엔진별 토큰 사용량, API 헬스 상태등(🟢/🔴), 이벤트 피드 |
| 🤖 2. AI 자가치유 | LLM 기반 에러 분석·자동 코드 수정 |
| 🏋️ 3. 로컬 AI 훈련소 | Ollama 모델 빌드 (Hot Swap), few-shot 데이터 관리 |
| 📡 4. 데이터 수집 현황 | 7일간 일별 수집량 시계열 차트, 파이프라인 요약 |

### API 헬스체크 (저부하)
`token_manager.check_api_health()` — 60초 TTL 캐시
- Stage 1: 정규식 포맷 검증 (`^AIza...`, `^gsk_...`)
- Stage 2: 모델 리스트 엔드포인트 HTTP 호출 (`/v1beta/models`, `/openai/v1/models`)
- Ollama: `http://localhost:11434` ping

---

## 7. 시스템 구성 파일 맵

```
UnivAgent/
├── scripts/
│   ├── telegram_agent.py      # 텔레그램 봇 메인
│   ├── token_manager.py       # 3-Tier LLM 라우터
│   ├── db_manager.py          # SQLite 스키마 단독 소유자
│   ├── web_dashboard.py       # Streamlit MLOps UI
│   ├── logger_factory.py      # JSONL 이벤트 로거
│   ├── pdf_collector.py       # 입시요강 PDF 수집
│   ├── pdf_parser.py          # PDF → DB 파싱
│   ├── storage_manager.py     # JSON 기반 프로필 관리
│   ├── devops_reporter.py     # 운영 리포트 생성
│   ├── error_checkpoint.py    # 재시작 기준점 관리
│   ├── daily_backup.py        # 일일 백업 + 핸드오버 생성
│   └── generate_handover.py   # AI 컨텍스트 핸드오버 생성
├── gemini_verifier.py         # 사후 검증 데몬 (5분 루프)
├── e2e_tester.py              # E2E 헬스체크 데몬
├── restart_agent.sh           # 전체 시스템 재시작
├── Modelfile                  # Ollama 모델 정의
├── system_config.json         # 런타임 상태 (훈련모드 토글)
├── data/
│   ├── admissions_agent.db    # 메인 SQLite DB
│   ├── logs/system_events.jsonl  # 구조화 이벤트 로그
│   └── backups/               # 일일 tar.gz 백업
└── docs/                      # AI 컨텍스트 문서 (이 디렉터리)
```
