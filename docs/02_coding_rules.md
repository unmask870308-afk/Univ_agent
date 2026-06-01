# UnivAgent 코딩 규칙 & 절대 원칙

## 규칙 1 — YOLO 모드 (자율 실행)

사용자가 "yolo mode" 또는 "execute autonomously"를 명시하면:
- 확인 질문 없이 즉시 실행
- 모든 파일 생성·수정·삭제를 직접 수행
- 실행 후 한국어로 결과 보고

단, 다음은 예외적으로 확인 필요:
- `git push` (원격 저장소 영향)
- 프로덕션 DB `DROP TABLE` / `DELETE` (비가역적 파괴)
- `.env` 파일에 실제 API 키 직접 기입

---

## 규칙 2 — 비동기 논블로킹 엄수

텔레그램 봇(`telegram_agent.py`)은 `asyncio` 기반입니다.

**금지 패턴:**
```python
# ❌ 절대 금지 — 봇 이벤트 루프 블로킹
time.sleep(5)
requests.get(url)                  # 동기 HTTP
subprocess.run(cmd)                # 동기 서브프로세스
token_manager.generate_text_sync() # 동기 LLM 호출 (직접 호출 금지)
```

**올바른 패턴:**
```python
# ✅ 반드시 executor로 감싸기
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(
    None,
    lambda: token_manager.generate_text_sync(prompt, system)
)

# ✅ 비블로킹 대기
await asyncio.sleep(1)

# ✅ 백그라운드 태스크
threading.Thread(target=fn, daemon=True).start()
```

**이유**: 동기 블로킹 호출이 `asyncio` 이벤트 루프에 직접 들어가면 전체 봇이 멈춥니다. Gemini API 호출은 최대 30초가 걸릴 수 있습니다.

---

## 규칙 3 — Graceful Error Handling (JSONL 로깅)

모든 에러는 `scripts/logger_factory.py`를 통해 `data/logs/system_events.jsonl`에 기록해야 합니다. `print()`나 `raise`로만 끝내는 것은 금지합니다.

**표준 패턴:**
```python
import logger_factory as _lf

# 동기 컨텍스트
try:
    do_something()
except Exception as e:
    _lf.log_error("EVENT_TYPE", "module_name", e)

# 비동기 컨텍스트
try:
    await do_something_async()
except Exception as e:
    await _lf.async_log_error("EVENT_TYPE", "module_name", e)
```

**이벤트 타입 네이밍 컨벤션:**
- `BOOT_FAILURE` — 부팅 불가 치명적 오류
- `OLLAMA_HEALTH_FAIL` — Ollama 포트 미응답
- `E2E_TEST_RESULT` — E2E 헬스체크 결과
- `VERIFIER_SUCCESS` / `VERIFIER_ERROR` / `VERIFIER_QUOTA` — 사후 검증 결과
- `SHIELD_DEFENSE` — 프롬프트 인젝션 방어

**부팅 실패 특별 규칙:**
`telegram_agent.py` 부팅 시 `.env` 로드 실패 또는 `TELEGRAM_TOKEN` 미설정 시:
1. `_boot_fatal(reason)` 호출
2. `logger_factory.log_event("BOOT_FAILURE", ...)` 기록
3. `sys.exit(1)` 종료
순서를 반드시 지킵니다.

---

## 규칙 4 — DB Single Source of Truth

**`scripts/db_manager.py`가 SQLite 스키마의 유일한 소유자입니다.**

| 허용 | 금지 |
|---|---|
| `db_manager.init_db()` 호출 | 타 모듈에서 `CREATE TABLE` 직접 실행 |
| `db_manager.*` 함수 사용 | `sqlite3.connect()` 직접 호출 (db_manager 외부) |
| `_DDL` 문자열 수정 | `ALTER TABLE` 직접 실행 (마이그레이션 함수 사용) |

**테이블 추가 절차:**
1. `db_manager.py`의 `_DDL` 문자열에 `CREATE TABLE IF NOT EXISTS` 추가
2. 필요 시 `_system_metrics_마이그레이션()` 패턴으로 마이그레이션 함수 추가
3. CRUD 함수를 같은 파일 내에 추가
4. 다른 모듈은 해당 CRUD 함수만 호출

**절대 존재해서는 안 되는 패턴:**
```python
# ❌ telegram_agent.py, web_dashboard.py 등에서 절대 금지
import sqlite3
conn = sqlite3.connect("data/admissions_agent.db")
conn.execute("CREATE TABLE IF NOT EXISTS ...")

# ❌ 잘못된 컬럼명 예시 (실제 PK는 chat_id)
CREATE INDEX IF NOT EXISTS idx_users_id ON users(id)  # users 테이블 없음!
```

---

## 규칙 5 — 훈련 모드 격리

`system_config.json`의 `training_mode: true` 활성화 시:

- `telegram_agent.py`: 사용자 질문을 Ollama로만 처리, 상단 경고 메시지 표시
- `token_manager.py`: `force_engine=None` 경로를 Ollama로 강제 락
- 바이패스 허용: `force_engine="gemini"/"code"/"crawl"` (자가치유·수집 작업)

**5초 TTL 캐시로 파일 읽기 부하 최소화:**
```python
_TM_MODE_CACHE = {"ts": 0.0, "val": False}
_TM_MODE_TTL   = 5.0

def _is_training_mode() -> bool:
    now = time.monotonic()
    if now - _TM_MODE_CACHE["ts"] < _TM_MODE_TTL:
        return _TM_MODE_CACHE["val"]
    # 파일 읽기 후 캐시 갱신
```

---

## 규칙 6 — 경로 해석 기준

모든 경로는 **프로젝트 루트 절대경로 기준**으로 작성합니다.

```python
# ✅ 올바른 패턴 (scripts/ 내부 파일 기준)
_ROOT = Path(__file__).resolve().parent.parent

# ✅ 올바른 패턴 (루트 파일 기준)
_ROOT = Path(__file__).resolve().parent
```

`os.getcwd()`나 상대경로(`"./data/..."`)는 사용하지 않습니다. 실행 디렉터리에 따라 경로가 달라지기 때문입니다.

---

## 규칙 7 — 보안

- `.env` 파일은 절대 git에 커밋하지 않습니다 (`.gitignore` 등록됨)
- API 키는 환경변수로만 주입 (`os.getenv()`)
- 사용자 입력은 `db_manager.입력_소독()` 으로 소독 후 DB 저장
- 학생 세특·생기부 원문은 Fernet 대칭키 암호화 후 저장 (`db_manager.암호화()`)
- 암호화 키는 `.env`의 `FERNET_KEY` 환경변수에서 로드, 없으면 자동 생성 후 `.env`에 기록
