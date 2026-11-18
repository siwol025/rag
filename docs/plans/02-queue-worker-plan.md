# F2. 큐 적재와 워커 소비 상세 구현 계획 (FastAPI → Redis(arq) → AI Worker)

> 작성일 2026-11-13 · 입력: `PLAN.md`(8-11, 58), `docs/plans/00-overall-roadmap.md`(F2, 4장, 5장), `docs/plans/01-event-ingestion-plan.md`(3.5, 8.2), `docs/tradeOff.md`(TO-001~004), `docs/researchs/external-api-specs.md`(8.3, 8.4), `docs/researchs/plan-architecture.md`(3장 단계 3·4, 4.5), 현재 코드(`src/slack_rag/*`, `tests/*`, compose, Dockerfile)
> 다음 단계: TDD 서브에이전트가 5장 순서대로 `/mattpocock-skills:tdd`(red → green)로 구현합니다. 시작 전에 3.7의 seam 목록과 1.4의 숫자 가정(시도 횟수, 타임아웃)을 사용자와 확인합니다.

---

## 1. 개요

### 1.1 목표
요청이 몰려도 FastAPI는 작업을 큐에 넣고 바로 202로 답하고, 워커가 큐에서 하나씩 가져가 작업 상태를 **대기(queued) → 처리 중(running) → 완료(done)/실패(failed)** 로 바꿉니다(PLAN.md:8-11, 로드맵 F2).

### 1.2 범위
| 포함 | 제외(다음 단계 또는 범위 밖) |
|---|---|
| FastAPI 접수 경로에 큐 적재 연결(DB 커밋 뒤 적재, 실패 시 503) | LangGraph, LLM 호출(F3~) |
| 중복 요청이어도 아직 시작 전인 작업은 다시 적재 | Slack 회신(F9) |
| Redis 서비스, arq 워커 서비스(docker compose) | 배포, 클라우드(OQ-19, TO-003) |
| `jobs` 상태 컬럼과 상태 전이, 두 워커 동시 처리 방지 | 큐 교체 대비 추상 인터페이스(약속은 "모듈 두 곳에 모음"까지, TO-004) |
| 워커 장애 시 재처리, 반복 실패 작업 격리(`failed`) | 작업 상태 조회 API, 실패 작업 재처리 도구 |
| 워커 시작 시 미적재 작업 보정 | 주기적 보정(cron), 처리 중으로 멈춘 작업 자동 회수 |
| 관측성 최소치: 큐 대기 시간·처리 시간 로그, job_id 상관 ID | 메트릭, 대시보드(OQ-23) |
| **임시 대체**: 처리 함수 `handle_job()`은 아무것도 하지 않음 → 워커가 완료로 표시 | 처리 함수 추상화(F3가 이 함수 본문을 바꿈) |

### 1.3 확정된 결정 (바꾸지 않음)
| ID | 결정 | 이 계획에 반영된 곳 |
|---|---|---|
| TO-001 | A안: PostgreSQL `jobs`가 원본, 큐에는 `job_id`만. 적재 실패는 5xx로 n8n 재시도 유도, 중복 요청이어도 미적재면 다시 적재 | 3.1, 3.2, 2.3 |
| TO-002 | 워커당 동시 1건(arq `max_jobs=1`), 처리량은 워커 수로. 개발 중 워커 1개 | 6.3 `worker` 서비스, `create_worker()` |
| TO-003 | 로컬 docker compose, 클라우드 비종속. 큐 코드는 적재·소비 각 한 모듈 | 4장(`job_queue.py`, `worker.py`) |
| TO-004 | Redis + arq. 두 번 실행될 수 있음(감수), 나머지 코드는 arq를 직접 부르지 않음 | 3.4, 3.5, 8.1의 `grep` 확인 |

### 1.4 가정 (미확정 사항을 대신함, 사용자 확인 필요)
- **가정 B1 (숫자)**: 처리 시도 상한 `MAX_TRIES = 3`, 예외 뒤 재시도 간격 `RETRY_DELAY_S = 10`초, 처리 단계 상한 `JOB_TIMEOUT_S = 600`초. F2의 임시 처리는 수 ms라 의미가 없고, F3~F8에서 실제 작업 시간과 LLM 비용을 보고 다시 정합니다(3.5). **2026-11-13 사용자 승인: 이 값으로 시작하고 운영 상황에 맞춰 조정.** 3.7 seam 목록도 같은 날 승인.
- **가정 B2**: 개발 중 워커 1개. 늘릴 때는 `docker compose up -d --scale worker=N`(TO-002).
- **가정 B3**: Redis DB 0을 큐 전용으로 씁니다. F3 캐시가 같은 Redis를 쓸 때 키 접두어나 DB 번호를 그때 정합니다.
- **가정 B4**: Redis는 인증 없이 compose 내부망과 `127.0.0.1`에만 엽니다(F1 가정 A7과 같은 원칙).
- **가정 B5**: F1에서 이미 저장된 행(E2E 시험 메시지)은 스키마 변경 때 `queued`로 채워지고, 첫 워커 시작 때 보정(3.6)으로 적재되어 임시 처리로 `done`이 됩니다. 원치 않으면 F2 적용 전에 `TRUNCATE jobs`.
- **가정 B6**: 로그의 "큐 대기 시간(`queue_wait_ms`)"은 **접수 시각(`created_at`)부터 워커가 가져간 시각까지**입니다. 재시도라면 앞 시도에 쓴 시간이 포함됩니다.

### 1.5 확인한 버전 (2026-11-13 조회)
| 대상 | 버전 | 확인 출처 |
|---|---|---|
| arq | 0.28.0 (2026-04-16, Python ≥3.9, classifier 3.13·3.14 포함) | PyPI JSON. 의존성 `redis[hiredis]>=4.2.0,<6`. 휠 소스(`connections.py`, `worker.py`, `constants.py`) 직접 확인 |
| redis-py | 최신 8.1.0이지만 arq가 `<6`을 요구 → **5.3.1** (2025-07-25), hiredis 3.5.0(cp313 macOS arm64·manylinux·musllinux 휠 있음) | PyPI JSON. 5.3.1 소스: `close()`는 5.0.1부터 deprecated → `aclose()`. arq 기본 설정에서는 명령 실패 시 재시도 없음(`Retry(NoBackoff(), 0)`), 풀에서 꺼낼 때 끊긴 연결은 다시 연결 |
| testcontainers | 4.15.0 (이미 설치) | 설치본 소스. Redis 모듈은 `testcontainers.community.redis.RedisContainer`(`testcontainers.redis`는 deprecated). **`[redis]` extra는 `redis>=7`을 요구해 arq와 충돌하므로 쓰지 않음.** 모듈 자체는 `redis.Redis(...).ping()`만 써서 redis-py 5.3.1로 동작. `stop()`은 컨테이너를 삭제(`remove(force=True)`) |
| Redis 이미지 | `redis:8.10.3-alpine` (2026-11-13 기준 `alpine`, `8-alpine`과 같은 digest. 8.12는 RC) | Docker Hub API |
| anyio | 4.16.0 (fastapi 하위 의존으로 이미 설치) | 설치본 소스. pytest 플러그인 내장(`@pytest.mark.anyio`, `anyio_backend` fixture). `from_thread.run()`은 AnyIO 워커 스레드 안에서만 동작 |
| fastapi / starlette | 0.162.5 / 1.10.0 (변경 없음) | 설치본 `fastapi/routing.py`: 동기 `def` 엔드포인트를 `starlette.concurrency.run_in_threadpool` → `anyio.to_thread.run_sync`로 실행 → `from_thread.run()` 사용 가능 |
| psycopg / psycopg-pool | 3.3.9 / 3.3.3 (uv.lock 기준, 변경 없음) | 설치본 `_cursor_base.py`: 파라미터 없이 `execute()`하면 simple query 프로토콜 → `schema.sql`에 문장 여러 개 가능. `str` 파라미터는 OID unknown으로 보내 `uuid` 컬럼과 비교 가능 |
| pytest-asyncio | 1.5.0 (2026-11-12) | PyPI. **쓰지 않음**(anyio 플러그인으로 충분, 새 의존성 없음) |

### 1.6 arq 동작 확인 결과 (0.28.0 소스, 이 계획의 근거)
TO-004에서 "미확인"이던 항목을 포함합니다.

| 항목 | 동작 | 이 계획의 대응 |
|---|---|---|
| `enqueue_job(..., _job_id=X)` | `arq:job:X`(대기·실행 중, 기본 1일 뒤 만료) 또는 `arq:result:X`(결과, `keep_result` 기본 3600초)가 있으면 **`None`을 반환**하고 넣지 않음. 없으면 `Job` 반환 | 3.2 |
| 실행 중 표시 | 워커가 시작할 때 `arq:in-progress:X`를 TTL `job_timeout + 10초`로 둠. 이 키가 있는 동안 다른 워커는 X를 시작하지 않음(WATCH/MULTI) | 3.4 |
| 일반 예외 | **재시도 없이 바로 실패**로 끝남(`max_tries`와 무관). `Retry` 예외만 재시도 | 3.5: 모든 예외를 `Retry`로 바꿈 |
| 워커 SIGTERM | 실행 중 작업을 취소하고 큐에 남김(`retry_jobs=True` 기본), 실행 중 표시 삭제 → **곧바로** 다시 실행 가능 | 2.6 |
| 워커 SIGKILL·OOM | 실행 중 표시가 TTL로 만료된 뒤 다시 실행(**`job_timeout + 10초` 지연**) | 2.6, 3.5 |
| 시도 번호 `ctx["job_try"]` | 실행할 때마다 `INCR arq:retry:X`(종료·취소·Retry 모두 증가). 작업이 끝나면 삭제 | 3.4 펜싱 토큰 |
| `job_try > max_tries` | **작업 함수를 부르지 않고** 실패로 끝냄 | 3.5: arq `max_tries`를 우리 상한 + 1로 |
| `job_timeout` 초과 | `asyncio.wait_for`가 작업 코루틴을 **취소**하고 `TimeoutError` → 재시도 없이 실패. 작업 함수는 DB에 아무것도 남길 수 없음 | 3.5: 자체 타임아웃을 더 짧게 |
| DLQ | **없음**(소스 전체에 dead-letter 개념 없음). 실패 결과를 `keep_result` 동안 보관할 뿐 | 3.5: `jobs.status='failed'`가 격리 장소 |
| `on_job_start`/`on_job_end` 훅 | 결과(성공/실패/재시도)를 넘겨주지 않고, `max_tries` 초과 시에는 불리지도 않음 | 쓰지 않음 |
| 폴링 | `poll_delay` 기본 0.5초. `max_jobs=1`이면 한 번 폴링에 최대 1건 시작 → 짧은 작업은 워커당 초당 약 2건 | 7장 M2 예상 시간 |
| Redis 오류 시 워커 | 폴링 루프가 Redis 오류를 잡지 않음 → 워커 프로세스가 종료됨 | 3.6 재시작 보정 |
| 직렬화 | 기본 `pickle` | 8.2 범위 밖(배포 시 검토) |

---

## 2. 전체 흐름과 데이터 계약

### 2.1 흐름
```
[n8n HTTP Request] POST /jobs · Retry On Fail 5회/5초(4xx·5xx 모두 재시도, F1 6.7)
              ▼
[FastAPI POST /jobs]  동기 def(스레드풀) — 1)~3)은 F1 그대로
     1) X-API-Key → 401   2) 검증 → 422   3) ignore_reason() → 200 ignored
     4) INSERT … ON CONFLICT (+SELECT) → (job_id, created, status) ── 블록 끝에서 커밋
     5) status == 'queued'면 job_queue.enqueue(job_id)            ── F2, 커밋 뒤
          · 넣음 / 이미 대기 중  → 202 accepted | duplicate
          · Redis 오류          → 503 (행은 queued로 남음 → n8n 재시도가 5)를 다시 지남)
              ▼  ZSET arq:queue(member=job_id) + arq:job:<job_id>(함수 run_job, 인자 job_id)
[Redis 8.10.3, AOF]
              ▼  0.5초 폴링, 워커당 동시 1건
[worker.run_job(ctx, job_id)]
     0) job_try > 3        → failed('max_tries_exceeded')
     1) claim_job          → running(attempts=job_try) | 가져갈 수 없으면 job_skipped
     2) handle_job(job_id) → F2는 아무것도 안 함(F3에서 LangGraph). 자체 타임아웃 600초
     3) complete_job       → done
     예외·타임아웃 → Retry(10초 뒤 다시) · SIGTERM → 곧바로 다시 · SIGKILL → 640초 뒤 다시
[worker 시작 시] status='queued' 행을 전부 다시 적재(미적재 보정, 이미 있으면 arq가 무시)
```

### 2.2 FastAPI 응답 변화
본문 형식과 기존 상태 코드는 그대로이고 **503 한 줄이 늘어납니다.**

| 상황 | 코드 | 본문 | 적재 | n8n 동작 |
|---|---|---|---|---|
| 새 메시지 | 202 | `{"status":"accepted","job_id":…}` | 함 | 성공 |
| 중복, 작업이 `queued` | 202 | `{"status":"duplicate","job_id":…}` | **다시 함**(이미 대기 중이면 arq가 무시) | 성공 |
| 중복, 작업이 `running`/`done`/`failed` | 202 | `{"status":"duplicate","job_id":…}` | 안 함 | 성공 |
| 필터 제외 | 200 | `{"status":"ignored","reason":…}` | 안 함 | 성공 |
| **큐(Redis)에 적재 실패** | **503** | `{"detail":"queue unavailable"}` | 실패(행은 `queued`) | 오류 → Retry On Fail |
| 401, 422, DB 장애 500 | F1과 같음 | F1과 같음 | - | F1과 같음 |

### 2.3 큐 메시지
| 항목 | 값 |
|---|---|
| arq 작업 ID(`_job_id`) | `str(job_id)` (UUID 문자열) |
| 함수 이름 | `"run_job"` (`job_queue.JOB_FUNCTION`, `worker.run_job`의 이름과 같아야 함) |
| 인자 | `(str(job_id),)` 하나. **메시지 본문·사용자 정보 없음**(TO-001, 개인정보를 큐에 남기지 않음) |
| 저장 형태(arq 내부) | `arq:queue` 정렬 집합(점수 = 적재 시각 ms), `arq:job:<id>`(pickle, 기본 1일 뒤 만료) |
| 결과 | 보관하지 않음(`keep_result=0`, 3.2). 결과·상태는 `jobs`가 원본 |

### 2.4 `jobs` 테이블 변화
| 컬럼 | 타입 | 기본값(기존 행 포함) | 쓰는 곳 |
|---|---|---|---|
| `status` | `text NOT NULL` | `'queued'` | API(INSERT 기본값), 워커. 값: `queued`, `running`, `done`, `failed` |
| `attempts` | `integer NOT NULL` | `0` | 워커가 가져갈 때 그 실행의 arq `job_try`를 기록(가장 최근 시도 번호) |
| `started_at` | `timestamptz` | `NULL` | 워커가 가져간 시각 |
| `finished_at` | `timestamptz` | `NULL` | `done`/`failed`가 된 시각 |
| `error` | `text` | `NULL` | `failed` 사유(`max_tries_exceeded`). 개별 예외 내용은 로그 `job_retry`에 |

### 2.5 상태 전이표
| 현재 | 사건 | 다음 | 누가(함수) | 조건 |
|---|---|---|---|---|
| (없음) | 새 요청 | `queued` | API `create_job` | INSERT 기본값 |
| `queued` | 워커가 가져감(시도 n) | `running`, attempts=n | 워커 `claim_job` | `status='queued'` |
| `running`(attempts=k) | arq가 다시 실행한 시도 n이 가져감(시도 k의 워커가 죽었거나 종료됨) | `running`, attempts=n | 워커 `claim_job` | `attempts < n` |
| `running`(attempts=n) | 시도 n의 처리 성공 | `done` | 워커 `complete_job` | `attempts = n` (더 나중 시도가 가져갔으면 바꾸지 않음 → `job_superseded` 로그) |
| `running` | 처리 중 예외·자체 타임아웃 | 그대로 → 10초 뒤 시도 n+1 | 워커 `run_job` | n ≤ 3 |
| `queued`/`running` | 시도 4번째 도달 | `failed`, error=`max_tries_exceeded` | 워커 `fail_job` | `job_try > MAX_TRIES` |
| `done`/`failed` | 어떤 시도든 다시 옴 | 그대로(`job_skipped`) | 워커 `claim_job` | 조건 불일치 |
| `queued` | 중복 요청 | 그대로 + 다시 적재 | API | - |
| `running`/`done`/`failed` | 중복 요청 | 그대로, 적재 안 함 | API | - |

`running` → `queued`로 되돌리는 전이는 없습니다. 죽은 시도의 `running`은 다음 시도가 `attempts < n` 조건으로 이어받습니다.

### 2.6 장애 시나리오별 결과
| 상황 | arq 동작(1.6) | `jobs` 결과 |
|---|---|---|
| 정상 | 1회 실행, 큐에서 제거 | `done`, attempts 1 |
| 처리 중 예외 / 자체 타임아웃(600초) | 우리가 `Retry(10초)` → 시도 n+1 | 3회 모두 실패하면 4번째 실행이 `failed` 기록 |
| 워커 SIGTERM(`docker compose stop worker`) | 실행 중 작업 취소 → 큐에 남김 → 다음 워커가 곧바로 시도 n+1 | `running`(n) → `running`(n+1) → `done` |
| 워커 SIGKILL(`docker compose kill`, OOM) | 실행 중 표시가 640초 뒤 만료 → 시도 n+1 | 같음(640초 지연). **`docker compose kill`로 죽인 워커는 `restart: unless-stopped`여도 자동으로 다시 뜨지 않음(`Exited (137)`) → `docker compose start worker` 필요**(2026-11-15 구현 중 확인). Redis 오류로 스스로 종료한 경우는 1~5초 간격으로 자동 재시작됨. 실제 OOM 시 동작은 미확인 |
| arq `job_timeout`(630초) 초과 | 재시도 없이 실패 + 코루틴 취소 | 자체 타임아웃(600초)이 먼저 걸리므로 생기지 않게 함 |
| 끝난 작업이 다시 큐에 들어옴(경합, 수동 적재) | 실행됨 | `claim_job`이 거부 → `job_skipped`, 상태 그대로 |
| Redis 중단 | API: 적재 실패 503 / 워커: 폴링 오류로 종료 → compose가 재시작 반복 | `queued`로 남음 → n8n 재시도 요청이나 워커 재시작 보정이 다시 적재 |
| PostgreSQL 중단 | 워커: 예외 → Retry, API: 500(F1) | 30초 안에 복구되면 정상 처리. 넘으면 3.5의 "남는 틈" |

---

## 3. 설계 결정

### 3.1 이중 쓰기와 재적재 → **커밋 뒤 적재, 실패하면 503. 중복 요청이어도 `status='queued'`면 다시 적재**
- **커밋 뒤에 적재합니다.** 트랜잭션 안에서 적재하면 워커가 아직 커밋되지 않은 행을 찾지 못해 `job_skipped`로 끝내고 작업이 사라질 수 있습니다. F1의 `with pool.connection()` 블록이 끝난 뒤(커밋 뒤) 적재합니다.
- **적재 실패는 503입니다.** n8n Retry On Fail(5회, 5초 간격)이 다시 보냅니다(TO-001). 행은 이미 커밋되어 `queued`로 남습니다.
- **재시도 요청은 `duplicate`지만 다시 적재합니다.** F1 `create_job()`이 기존 행의 `status`도 돌려주게 바꾸고(쿼리 수는 그대로), `queued`면 다시 적재합니다. `queued`는 "아직 어느 워커도 가져가지 않음"이라 적재됐는지 모르는 상태이고, 이미 대기 중이면 arq가 같은 `_job_id`를 무시하므로 다시 넣어도 안전합니다(3.2).
- **응답 본문은 바꾸지 않습니다.** 재적재 여부는 로그 필드 `enqueued`로만 남깁니다(호출자 n8n에게는 의미가 같음: "접수됨, 이 job_id").
- 대가: n8n이 끝내 포기하면(큐가 약 20초 넘게 내려감) DB에만 있는 작업이 남습니다 → 3.6.

### 3.2 arq `_job_id` 멱등의 한계 → **arq 멱등은 "대기·실행 중"에만 쓰고, 그 밖의 판단은 `jobs.status`가 함(두 겹)**
- 확인한 사실(1.6): `enqueue_job()`은 `arq:job:<id>` 또는 `arq:result:<id>`가 있을 때만 `None`을 돌려줍니다. 작업이 끝나면 `job` 키는 지워지고, 결과 키도 `keep_result`(기본 3600초) 뒤 사라집니다. 즉 **영구적이지 않습니다.**
- 결론:
  1. `_job_id = str(job_id)`로 넣어, 같은 작업이 큐에 둘 이상 동시에 존재하지 않게 합니다.
  2. 워커에 `keep_result=0`을 줍니다. 결과는 `jobs`에 있으므로 arq 결과는 필요 없고, "1시간 안의 중복은 막히고 그 뒤엔 안 막힌다" 같은 시간 경계가 사라져 규칙이 하나가 됩니다.
  3. **API는 `queued`일 때만 적재**하고(3.1), **워커는 `claim_job` 조건으로 `done`/`failed`를 다시 처리하지 않습니다**(3.4). 오래된 중복 요청이나 경합으로 끝난 작업이 큐에 다시 들어와도 `job_skipped`로 끝납니다.
- `_job_id`를 `채널:ts`가 아닌 `job_id`로 쓰는 이유: 큐 메시지에는 `job_id`만 둡니다(TO-001).

### 3.3 동기/비동기 불일치 → **F1 동기 엔드포인트를 그대로 두고 `anyio.from_thread.run(job_queue.enqueue, …)`로 적재**
| 안 | 내용 | 판단 |
|---|---|---|
| **A. 동기 유지 + `from_thread.run`** | `def post_job`은 스레드풀에서 돌고(F1 그대로), 비동기 적재만 이벤트 루프로 보내 끝날 때까지 기다림 | **채택.** F1 코드 변경이 가장 적음(언패킹 1줄 + 적재 블록). DB는 지금처럼 스레드풀, Redis I/O는 루프에서 비동기 → 루프를 막는 호출 없음 |
| B. `async def` + DB를 `run_in_threadpool` | 엔드포인트를 async로 바꾸고 DB 블록을 함수로 빼서 스레드풀로 | 동작은 같지만 F1 엔드포인트 구조를 더 많이 바꿈 |
| C. API도 psycopg async | `create_job`과 F1 DB 테스트를 비동기로 | F1 코드·테스트 전반 변경. TO-004 "F1 코드는 동기 그대로" |
| D. 동기 Redis 클라이언트로 직접 적재 | arq 직렬화·키 규칙을 손으로 재구현 | arq 내부에 묶이고 "arq는 두 모듈에서만" 약속도 깸 |
- 근거: FastAPI 0.162.5는 동기 엔드포인트를 `run_in_threadpool`(= `anyio.to_thread.run_sync`)로 실행하므로(1.5) 그 스레드에서 `anyio.from_thread.run()`을 쓸 수 있습니다. `ArqRedis`는 lifespan(같은 이벤트 루프)에서 만들어 루프가 일치합니다. TestClient도 같은 구조(포털 루프 + 워커 스레드)라 테스트에서도 동작합니다.
- 대가: 요청 스레드가 Redis 응답까지 기다립니다(스레드풀 기본 40개). 이 규모(100건)에서는 문제 없습니다.
- `anyio`는 fastapi 하위 의존이지만 `main.py`가 직접 import하므로 `pyproject.toml`에 명시합니다.

### 3.4 상태 모델과 동시 처리 방지 → **`status` + `attempts`(arq `job_try`를 펜싱 토큰으로)**
- **조건부 UPDATE로 가져갑니다.**
  ```sql
  UPDATE jobs SET status = 'running', attempts = :job_try, started_at = now()
  WHERE id = :job_id
    AND (status = 'queued' OR (status = 'running' AND attempts < :job_try))
  ```
  - 같은 시도 번호로 두 번(두 워커가 같은 메시지를 받은 경우) 가져갈 수 없습니다. 동시에 실행되면 PostgreSQL이 뒤 UPDATE를 앞 커밋 뒤에 다시 평가해 0행이 됩니다(READ COMMITTED).
  - **arq는 같은 작업을 다시 실행할 때마다 `job_try`를 1씩 올리고, 같은 작업이 실행 중일 때는 다른 워커가 시작하지 못하게 막습니다**(1.6). 그래서 "더 큰 `job_try`가 실행됐다 = 앞 시도의 워커는 죽었거나 종료됐다"로 볼 수 있고, 그 시도가 `running` 행을 이어받습니다.
  - `done`/`failed`는 어떤 시도도 가져가지 못합니다(3.2의 두 번째 겹).
- **완료도 같은 시도 번호일 때만 기록합니다**(`WHERE status='running' AND attempts=:job_try`). 죽은 줄 알았던 앞 시도가 늦게 끝나도 더 나중 시도의 상태를 덮어쓰지 않습니다. F2 임시 처리에서는 거의 생기지 않지만, F3에서 결과를 저장할 때 같은 규칙을 씁니다.
- **시간 기반 lease를 쓰지 않은 이유**: SIGTERM 뒤 arq는 곧바로 다시 실행하므로(1.6), "시작 후 N초가 지난 `running`만 다시 집기"로는 그 재실행이 거부되어 작업이 `running`에 멈춥니다. 취소 시 `queued`로 되돌리는 처리도 SIGKILL에서는 실행되지 않습니다. `job_try` 비교 하나로 두 경우를 모두 다룹니다.
- **처리 중에 트랜잭션을 열어 두지 않습니다.** 가져가기와 완료는 각각 짧은 트랜잭션이라, F3의 수 분짜리 처리 동안 행 잠금이 없습니다.
- **기존 행의 기본값**: `status='queued'`, `attempts=0`(가정 B5). `ADD COLUMN … DEFAULT 상수`는 PostgreSQL 11부터 테이블을 다시 쓰지 않습니다.
- **스키마 변경 방식**: F1 3.5 그대로 `schema.sql`에 `ALTER TABLE … ADD COLUMN IF NOT EXISTS`를 이어 씁니다. 값 목록 `CHECK` 제약은 넣지 않습니다(값을 쓰는 SQL은 `db.py` 네 곳뿐이고 테스트로 확인, `ADD COLUMN IF NOT EXISTS`와 제약을 섞은 재실행 동작을 따로 확인하는 부담을 피함).

### 3.5 재시도·격리·타임아웃 → **모든 예외(자체 타임아웃 포함)를 `Retry`로, 3회 넘으면 4번째 실행이 `failed`를 기록**
- arq는 DLQ가 없고, 일반 예외는 재시도 없이 실패로 끝나며, `job_try > max_tries`나 `job_timeout` 초과면 작업 함수가 DB에 아무것도 남기지 못합니다(1.6). 그대로 두면 작업이 `running`에 영원히 남습니다.
- 그래서 아래처럼 정합니다.

| 설정 | 값 | 이유 |
|---|---|---|
| `MAX_TRIES`(우리 상한) | 3 | 처리 시도 3회. 가정 B1 |
| arq `max_tries` | `MAX_TRIES + 1` = 4 | 4번째 실행은 처리하지 않고 `failed`만 기록. arq가 함수를 부르지 않는 상황(`job_try > max_tries`)을 피함 |
| 예외 처리 | `except Exception` → `raise Retry(defer=RETRY_DELAY_S)` | DB 일시 오류, F3 이후의 처리 오류를 같은 규칙으로 재시도. 취소(`CancelledError`)는 `Exception`이 아니라 그대로 arq에 넘어가 재실행됨 |
| `RETRY_DELAY_S` | 10초 | 짧은 DB 재시작을 넘길 정도. 외부 API 재시도는 F3의 LangGraph `RetryPolicy`가 맡음(plan-architecture 4.5 ①) |
| `JOB_TIMEOUT_S`(자체 타임아웃) | 600초 | `asyncio.timeout()`으로 처리 단계만 감쌈 → `TimeoutError`(예외)로 위 재시도 규칙을 탐. F3 이후 실측으로 조정 |
| arq `job_timeout` | `JOB_TIMEOUT_S + 30` = 630초 | 자체 타임아웃이 먼저 걸리고 DB 기록·`Retry`까지 마칠 여유. arq가 먼저 끊으면 재시도 없이 끝남 |
| arq `keep_result` | 0 | 3.2 |
| 격리 장소 | `jobs.status='failed'`, `error='max_tries_exceeded'` | DLQ 대신. 조회·재처리는 7장 운영 메모의 SQL |
- **대가**
  - SIGKILL 뒤 재처리까지 최대 `630 + 10 = 640`초 걸립니다. `job_timeout`을 늘리면 이 지연도 늘어납니다(F3 이후 다시 볼 값).
  - **워커 재시작도 시도 횟수를 씁니다.** SIGTERM(배포·재시작)으로 실행 중 작업이 취소돼도 arq가 `job_try`를 올립니다(1.6). F2 임시 처리는 즉시 끝나 문제가 없지만, F3 이후 수 분짜리 작업이 도는 동안 워커가 3번 재시작되면 처리 실패가 없었어도 `failed`로 격리될 수 있습니다. F3 계획에서 다시 봅니다(리뷰 중 추가).
  - **남는 틈**: 4번째(기록용) 실행에서 DB까지 장애면 `fail_job`이 실패하고 arq가 작업을 끝내 버려, 행이 `queued`(워커 재시작 보정이 다시 적재) 또는 `running`(7장 운영 메모 쿼리로 확인)으로 남습니다. PostgreSQL이 약 30초(10초 × 3) 넘게 내려간 경우에만 생깁니다.

### 3.6 미적재 작업 보정 → **워커 시작 시 1회, `queued` 행을 전부 다시 적재(주기 실행은 넣지 않음)**
- **이 형태로 충분한 이유**: 큐가 n8n 재시도 시간(약 20초)보다 오래 내려가 있으면 워커도 Redis를 못 써서 종료됩니다(arq 폴링 루프가 Redis 오류를 잡지 않음, 1.6). compose `restart: unless-stopped`가 워커를 계속 다시 띄우고, Redis가 돌아와 워커가 시작하는 순간 보정이 돕니다. 즉 "큐 장애 → 복구" 경로에 보정이 자연히 걸립니다.
- 이미 대기 중인 작업은 arq가 같은 `_job_id`를 무시하므로, 워커 여러 개가 동시에 시작해도 안전합니다. 끝난 작업은 `queued`가 아니라 대상이 아닙니다.
- **주기 실행(arq cron)을 넣지 않은 이유**: cron 작업도 같은 큐의 작업이라 `max_jobs=1` 슬롯을 차지하고, F3 이후 긴 작업이 도는 동안 분마다 cron 작업이 큐에 쌓입니다. 이를 피하려면 별도 백그라운드 태스크를 만들어야 해 코드가 늘어납니다.
- **남는 틈**: 워커는 Redis를 정상으로 쓰는데 API만 20초 넘게 적재에 실패한 경우(같은 호스트 compose에서는 드묾)에는 `docker compose restart worker`로 보정합니다(7장 M5b). 이런 일이 잦아지면 TO-001 "다시 볼 조건"(DB 큐 또는 아웃박스)을 검토합니다.

### 3.7 테스트 전략 (seam 목록, TDD 시작 전에 사용자와 확인)
| Seam(공개 경계) | 테스트 파일 | 실제 PostgreSQL | 실제 Redis | 잡는 것 | 못 잡는 것 |
|---|---|---|---|---|---|
| `Settings.redis_url` | `test_config.py` | 아니오 | 아니오 | 필수 설정, `.env` 읽기 | - |
| `create_job()` 상태 반환 + 스키마 변경 | `test_db.py` | 예 | 아니오 | 새 행 `queued`, 중복 시 현재 상태, F1 행 업그레이드 | - |
| `claim_job`/`complete_job`/`fail_job`/`queued_job_ids` | `test_db.py`(async) | 예 | 아니오 | 조건부 전이, 펜싱, 격리 | arq 재실행 타이밍 |
| `job_queue.enqueue()` | `test_queue.py`(async) | 아니오 | 예 | 메시지 계약(`run_job`, 인자 `job_id`만), 중복 무시 | - |
| HTTP `POST /jobs` + 큐 | `test_api.py` | 예 | 예 | 적재, 재적재, 시작된 작업 미적재, Redis 중단 503 | n8n 재시도 |
| arq 워커(`create_worker`, `run_job`) | `test_worker.py`(async) | 예 | 예 | 완료, 보정, 건너뛰기, 재시도→격리, 두 워커, 로그 | compose 재시작 정책 |
| 강제 종료(SIGKILL) 재처리 | `test_worker.py` + `hanging_worker.py` | 예 | 예 | 처리 중에 죽은 워커의 작업을 다음 시도가 완료 | 실제 컨테이너 kill |
| 100건 E2E | `test_worker.py` | 예 | 예 | 모두 202 후 모두 `done`(로드맵 완료 기준) | 동시 부하 |
| compose 전체 | 수동(7장) | 예 | 예 | 실제 프로세스·재시작·SIGTERM·Redis 중단 | - |

- **비동기 테스트는 anyio pytest 플러그인**(`@pytest.mark.anyio`)으로 씁니다. anyio는 이미 설치돼 있어 새 의존성이 없습니다. `conftest.py`의 `anyio_backend` fixture로 asyncio만 씁니다(arq와 psycopg 비동기 풀은 asyncio 전용).
- **Redis는 testcontainers**로 세션마다 `redis:8.10.3-alpine`(compose와 같은 이미지)을 1개 띄우고, 테스트마다 `FLUSHDB`로 비웁니다. Ryuk 비활성은 F1 `conftest.py` 설정을 그대로 씁니다. 503 테스트만 멈춰도 되는 Redis를 따로 하나 띄웁니다.
- **워커는 arq burst 모드**(`burst=True`: 큐가 비면 종료)로 테스트 안에서 돌립니다. `handle_signals=False`(테스트 루프에 시그널 핸들러를 걸지 않음), `poll_delay=0.01`(테스트 속도. 운영은 기본 0.5초)을 `create_worker(settings, **overrides)`로 넘깁니다.
- **처리 실패·타임아웃**은 `monkeypatch`로 `worker.handle_job`(F3가 바꿀 바로 그 자리)과 `RETRY_DELAY_S`·`JOB_TIMEOUT_S`를 바꿔 만듭니다.
- **SIGKILL 재처리는 자동 테스트로 합니다.** `tests/hanging_worker.py`를 별도 프로세스로 띄워 작업을 `running`으로 만든 뒤 이벤트 루프째 멈추게 하고, `proc.kill()`(SIGKILL) 후 테스트 안의 burst 워커가 실행 중 표시 만료(`job_timeout=1` → 11초)를 기다려 완료하는지 봅니다. 이 테스트 하나가 약 15초 걸립니다.
- **F1 테스트 중 바뀌는 것**(동작이 아니라 호출 모양만): `test_db.py`의 `create_job` 언패킹(2값 → 3값), `test_config.py`에 `REDIS_URL`, `test_api.py` `client` fixture에 `redis_url`. F1 응답 동작(`duplicate` 등)은 바뀌지 않아 기존 단언은 그대로 통과해야 합니다.
- 실행: Docker를 켠 상태에서 `uv run pytest`. 순수 테스트만: `uv run pytest tests/test_schemas.py tests/test_filters.py tests/test_config.py tests/test_logs.py`.

### 3.8 compose와 설정 → **`redis`, `worker` 서비스 추가, 설정 키는 `REDIS_URL` 하나**
- `redis`: `redis:8.10.3-alpine`, `--appendonly yes`(재시작해도 대기 작업 유지, 기본 `appendfsync everysec`로 최대 약 1초 손실), 볼륨 `redis_data:/data`, `127.0.0.1:6380`(호스트 6379는 다른 프로젝트 `flight-redis`가 사용 중, 컨테이너 안은 6379 그대로), `redis-cli ping` 헬스체크.
- `worker`: API와 같은 이미지, `command: ["python", "-m", "slack_rag.worker"]`, `env_file: .env` + `DATABASE_URL`/`REDIS_URL`을 compose 내부 주소로 덮어씀, `restart: unless-stopped`(3.6), `depends_on: api(started), redis(healthy)`. 포트 없음(`--scale` 가능).
- **주의(리뷰 지적 #2, 2026-11-15): `jobs`에 트랜잭션을 열어 둔 채 API를 재시작하지 마세요.** `schema.sql`의 `ALTER TABLE … ADD COLUMN IF NOT EXISTS`는 바꿀 컬럼이 없어도 테이블에 가장 강한 잠금(ACCESS EXCLUSIVE)을 먼저 잡습니다. psql 등으로 `jobs`에 트랜잭션을 열어 두면(`SELECT`만 해도) API 시작이 그 트랜잭션이 끝날 때까지 멈추고, 그 뒤의 워커 `UPDATE`도 줄을 섭니다. psql에서 `COMMIT`/`ROLLBACK`하면 풀립니다. 앱의 트랜잭션은 모두 짧아(처리 중에는 트랜잭션을 열지 않음, 3.4) 자동 운영에서는 생기지 않습니다. 결정 기록은 `docs/tradeOff.md` TO-005.
- 워커는 스키마를 적용하지 않습니다. API가 시작하며 적용하고(F1), 빈 DB에서 워커가 먼저 뜨면 보정 쿼리가 실패해 종료 → 재시작으로 해결됩니다. 두 프로세스가 동시에 `CREATE TABLE IF NOT EXISTS`를 실행하는 경합을 피합니다.
- arq CLI(`arq 모듈.WorkerSettings`) 대신 `python -m slack_rag.worker`를 씁니다. arq CLI는 설정 클래스 속성을 import 시점에 읽어 테스트에서 환경 변수 없이 import할 수 없고, 하위 클래스로 값을 바꾸면 arq가 `__dict__`만 읽어 상속 속성을 잃습니다(소스 확인). F1 `create_app(settings)`와 같은 팩토리 `create_worker(settings)`로 맞춥니다.
- `Settings.redis_url`은 기본값 없는 필수입니다(`DATABASE_URL`과 같은 규칙). compose는 `environment:`로 넣으므로 `.env`에 없어도 컨테이너는 뜨지만, 로컬 `uv run`에는 `.env`에 필요합니다.
- **사용자의 `.env`는 덮어쓰지 않습니다.** 사용자가 직접 추가할 키: `REDIS_URL=redis://localhost:6380/0`(6.4). 2026-11-13에 사용자 승인으로 추가 완료.
- 모듈 이름은 `job_queue.py`입니다. `queue.py`는 표준 라이브러리 `queue`를 가릴 수 있어 피합니다.

---

## 4. 디렉터리 구조 (F2에서 새로 생기거나 바뀌는 파일만)
```
slack_rag/
├── pyproject.toml            # 변경: anyio, arq, redis
├── uv.lock                   # 변경: uv lock
├── .env.example              # 변경: REDIS_URL
├── docker-compose.yml        # 변경: redis, worker 서비스, api 환경 변수·의존
├── src/slack_rag/
│   ├── config.py             # 변경: redis_url
│   ├── schema.sql            # 변경: status 등 5개 컬럼
│   ├── db.py                 # 변경: create_job이 status도 반환, 워커용 비동기 함수 4개
│   ├── job_queue.py          # 새 파일: 큐 적재(connect, enqueue, QueueUnavailable) — arq 사용 ①
│   ├── worker.py             # 새 파일: 큐 소비(handle_job, run_job, create_worker, main) — arq 사용 ②
│   └── main.py               # 변경: lifespan 큐 연결, 커밋 뒤 적재, 503
└── tests/
    ├── conftest.py           # 변경: anyio_backend, aconn, Redis 컨테이너 fixture
    ├── test_config.py        # 변경: REDIS_URL
    ├── test_db.py            # 변경: create_job 언패킹 + 새 테스트
    ├── test_api.py           # 변경: client fixture + 새 테스트
    ├── test_queue.py         # 새 파일
    ├── test_worker.py        # 새 파일
    └── hanging_worker.py     # 새 파일: 강제 종료 테스트가 띄우는 워커 프로세스(test_*가 아니라 수집 안 됨)
```
바뀌지 않는 파일: `schemas.py`, `filters.py`, `logs.py`, `Dockerfile`, `.dockerignore`, `n8n/slack-to-jobs.json`(Retry On Fail이 503도 재시도), `slack/manifest.yaml`.

**F1 코드에서 바뀌는 부분**
| 파일 | 바뀌는 부분 | 이유 |
|---|---|---|
| `config.py` | `redis_url: str` 한 줄 | 큐 주소(3.8) |
| `schema.sql` | `ALTER TABLE … ADD COLUMN IF NOT EXISTS` 5개 | 상태 전이(3.4) |
| `db.py` `create_job` | `RETURNING id` → `RETURNING id, status`, `SELECT id` → `SELECT id, status`, 반환 3값 | 중복 요청 재적재 판단(3.1). 쿼리 수 그대로 |
| `main.py` lifespan | 큐 연결·닫기 2줄 | 3.3 |
| `main.py` `post_job` | 커밋 뒤 `queued`면 적재, 실패 시 503, 로그에 `enqueued` | 3.1 |
| F1 테스트 3개 파일 | 호출 모양(언패킹, 설정 키, fixture 인자) | 3.7 |

---

## 5. 구현 단계 (TDD 순서)

공통 규칙: 각 단계의 테스트는 **나열한 순서대로 하나씩** red → green 합니다. 아래 "최소 구현"은 그 단계의 모든 테스트가 통과한 **끝 모습**입니다. 리팩터링은 이 루프에 넣지 않습니다(TDD 스킬 규칙). 단계마다 끝에 `uv run pytest` 전체가 통과해야 다음으로 갑니다(F1 37개 포함).

순서 근거: DB(동기, 의존 없음) → DB(비동기, 워커용) → 큐 적재 모듈(Redis만) → 설정 → API(앞의 것을 조립) → 워커(앞의 것을 조립) → 장애 재처리 → 100건 완료 기준 → 컨테이너 조립. `Settings.redis_url`을 필수로 만들면 F1 API 테스트 fixture가 Redis URL을 줘야 하므로, Redis fixture가 생기는 단계 3 뒤에 둡니다.

### 단계 0. 의존성 (테스트 없음)
6.1처럼 `pyproject.toml`을 고치고 설치합니다.
```bash
uv lock
uv sync
uv run python -c "import arq, redis; print(arq.VERSION, redis.__version__)"   # 0.28.0 5.3.1
uv run pytest                                                                   # F1 37개 통과(변화 없음)
```

### 단계 1. 스키마 변경과 `create_job()` 상태 반환 (실제 PostgreSQL)
① 실패 테스트(순서: 상태 반환 → F1 행 업그레이드)
```python
# tests/test_db.py (단계 1에서 추가)
from slack_rag.db import apply_schema, create_job


def test_new_job_is_queued_and_duplicate_reports_current_status(conn):
    job_id, created, status = create_job(conn, **JOB)
    assert (created, status) == (True, "queued")
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (job_id,))
    assert create_job(conn, **JOB) == (job_id, False, "done")


JOBS_TABLE_BEFORE_STATUS = """
CREATE TABLE jobs (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id   text        NOT NULL,
    message_ts   text        NOT NULL,
    user_id      text        NOT NULL,
    message_text text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_channel_id_message_ts_key UNIQUE (channel_id, message_ts)
)
"""


def test_schema_upgrade_marks_rows_saved_before_status_columns_as_queued(conn):
    conn.execute("DROP TABLE jobs")
    conn.execute(JOBS_TABLE_BEFORE_STATUS)  # F1 시절 테이블과 행
    conn.execute(
        "INSERT INTO jobs (channel_id, message_ts, user_id, message_text)"
        " VALUES ('C0TARGET01', '1794381600.000100', 'U0HUMAN01', '예전 행')"
    )
    apply_schema(conn)
    row = conn.execute("SELECT status, attempts, started_at, finished_at, error FROM jobs").fetchone()
    assert row == ("queued", 0, None, None, None)
```
반환값이 3개가 되면 F1 테스트의 언패킹이 깨집니다. 아래처럼 맞춥니다(단언 내용은 그대로).
```python
# tests/test_db.py (F1 테스트 수정분)
def test_first_message_creates_job(conn):
    job_id, created, _ = create_job(conn, **JOB)
    ...

def test_same_channel_and_ts_returns_existing_job(conn):
    first_id, _, _ = create_job(conn, **JOB)
    second_id, created, _ = create_job(conn, **JOB | {"message_text": "재전송"})
    assert (second_id, created) == (first_id, False)

def test_same_ts_in_other_channel_is_another_job(conn):
    first_id, _, _ = create_job(conn, **JOB)
    other_id, created, _ = create_job(conn, **JOB | {"channel_id": "C0TARGET02"})
    ...

def test_concurrent_duplicates_create_exactly_one_job(database_url, conn):
    ...
    assert len({job_id for job_id, _, _ in results}) == 1
    assert sum(created for _, created, _ in results) == 1
```
② 최소 구현
```sql
-- src/slack_rag/schema.sql (F1 CREATE TABLE 뒤에 이어 씀)

-- F2: 큐 소비 상태. 이미 있는 행(F1)은 queued, attempts 0으로 채워진다.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS status      text        NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    ADD COLUMN IF NOT EXISTS attempts    integer     NOT NULL DEFAULT 0,         -- 가장 최근에 가져간 시도 번호(arq job_try)
    ADD COLUMN IF NOT EXISTS started_at  timestamptz,
    ADD COLUMN IF NOT EXISTS finished_at timestamptz,
    ADD COLUMN IF NOT EXISTS error       text;
```
```python
# src/slack_rag/db.py (create_job만 변경)
def create_job(
    conn: psycopg.Connection,
    *,
    channel_id: str,
    message_ts: str,
    user_id: str,
    message_text: str,
) -> tuple[UUID, bool, str]:
    """작업을 만들고 (job_id, 새로 만들었는지, 현재 상태)를 돌려준다. 같은 (channel_id, message_ts)면 기존 작업."""
    row = conn.execute(
        """
        INSERT INTO jobs (channel_id, message_ts, user_id, message_text)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (channel_id, message_ts) DO NOTHING
        RETURNING id, status
        """,
        (channel_id, message_ts, user_id, message_text),
    ).fetchone()
    if row is not None:
        return row[0], True, row[1]
    row = conn.execute(
        "SELECT id, status FROM jobs WHERE channel_id = %s AND message_ts = %s",
        (channel_id, message_ts),
    ).fetchone()
    return row[0], False, row[1]
```
`main.py`는 `post_job` 안의 언패킹 한 줄만 맞춥니다(상태 사용은 단계 5).
```diff
-            job_id, created = create_job(
+            job_id, created, _ = create_job(
```
③ 검증: `uv run pytest tests/test_db.py`, 이어서 `uv run pytest`(F1 API 테스트가 여전히 통과)

`schema.sql`은 문장이 2개가 되지만 `apply_schema()`는 파라미터 없이 실행하므로 psycopg가 simple query 프로토콜로 한 번에 보냅니다(1.5). F1 API 테스트가 테스트마다 lifespan에서 `apply_schema`를 다시 실행하므로 `ALTER … IF NOT EXISTS`의 반복 적용도 함께 확인됩니다.

### 단계 2. 워커용 DB 함수 (비동기, 실제 PostgreSQL)
① 실패 테스트. `conftest.py`에 비동기 fixture를 더합니다.
```python
# tests/conftest.py (단계 2에서 추가)
@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"  # arq와 psycopg 비동기 풀은 asyncio에서 돈다


@pytest.fixture
async def aconn(database_url, conn):  # conn이 jobs를 비움
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as aconn:
        yield aconn
```
```python
# tests/test_db.py (단계 2에서 추가, 순서: claim → 이어받기·펜싱 → 끝난 작업 → 격리 → 대기 목록)
from datetime import timedelta

import pytest

from slack_rag.db import claim_job, complete_job, fail_job, queued_job_ids


def job_row(conn, job_id):
    return conn.execute("SELECT status, attempts, error FROM jobs WHERE id = %s", (job_id,)).fetchone()


@pytest.mark.anyio
async def test_claim_takes_queued_job_only_once_per_try(conn, aconn):
    job_id, _, _ = create_job(conn, **JOB)
    waited = await claim_job(aconn, job_id, 1)
    assert waited is not None and waited >= timedelta(0)  # 접수 후 기다린 시간
    assert await claim_job(aconn, job_id, 1) is None      # 같은 시도로는 두 번(두 워커) 못 가져감
    assert job_row(conn, job_id) == ("running", 1, None)


@pytest.mark.anyio
async def test_later_try_takes_over_running_job_and_earlier_try_cannot_complete(conn, aconn):
    job_id, _, _ = create_job(conn, **JOB)
    await claim_job(aconn, job_id, 1)                      # 1번째 시도의 워커가 처리 중에 죽음
    assert await claim_job(aconn, job_id, 2) is not None   # arq가 다시 실행한 2번째 시도
    assert await complete_job(aconn, job_id, 1) is False   # 1번째가 늦게 끝나도 덮어쓰지 못함
    assert await complete_job(aconn, job_id, 2) is True
    assert job_row(conn, job_id) == ("done", 2, None)


@pytest.mark.anyio
@pytest.mark.parametrize("final_status", ["done", "failed"])
async def test_finished_job_cannot_be_claimed(conn, aconn, final_status):
    job_id, _, _ = create_job(conn, **JOB)
    conn.execute("UPDATE jobs SET status = %s WHERE id = %s", (final_status, job_id))
    assert await claim_job(aconn, job_id, 1) is None


@pytest.mark.anyio
async def test_fail_job_isolates_only_unfinished_job(conn, aconn):
    running_id, _, _ = create_job(conn, **JOB)
    done_id, _, _ = create_job(conn, **JOB | {"message_ts": "1794381700.000200"})
    await claim_job(aconn, running_id, 1)
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (done_id,))

    await fail_job(aconn, running_id, "max_tries_exceeded")
    await fail_job(aconn, done_id, "max_tries_exceeded")

    assert job_row(conn, running_id) == ("failed", 1, "max_tries_exceeded")
    assert job_row(conn, done_id) == ("done", 0, None)


@pytest.mark.anyio
async def test_queued_job_ids_lists_waiting_jobs_oldest_first(conn, aconn):
    first, _, _ = create_job(conn, **JOB)
    second, _, _ = create_job(conn, **JOB | {"message_ts": "1794381700.000200"})
    started, _, _ = create_job(conn, **JOB | {"message_ts": "1794381800.000300"})
    await claim_job(aconn, started, 1)
    assert await queued_job_ids(aconn) == [first, second]
```
② 최소 구현(같은 `db.py`에 추가. 워커 쪽은 비동기라 `psycopg.AsyncConnection`을 받음)
```python
# src/slack_rag/db.py (단계 2에서 추가)
from datetime import timedelta


async def claim_job(conn: psycopg.AsyncConnection, job_id: UUID, job_try: int) -> timedelta | None:
    """작업을 처리 중으로 바꾸고 접수 후 기다린 시간을 돌려준다. 가져갈 수 없으면 None.

    대기 중인 작업, 또는 더 앞선 시도(죽었거나 종료된 워커)가 처리 중으로 둔 작업만 가져간다(3.4).
    """
    cur = await conn.execute(
        """
        UPDATE jobs
        SET status = 'running', attempts = %(job_try)s, started_at = now()
        WHERE id = %(job_id)s
          AND (status = 'queued' OR (status = 'running' AND attempts < %(job_try)s))
        RETURNING now() - created_at
        """,
        {"job_id": job_id, "job_try": job_try},
    )
    row = await cur.fetchone()
    return None if row is None else row[0]


async def complete_job(conn: psycopg.AsyncConnection, job_id: UUID, job_try: int) -> bool:
    """처리 중인 작업을 완료로 바꾼다. 그 사이 더 나중 시도가 가져갔으면 바꾸지 않고 False."""
    cur = await conn.execute(
        """
        UPDATE jobs SET status = 'done', finished_at = now()
        WHERE id = %s AND status = 'running' AND attempts = %s
        """,
        (job_id, job_try),
    )
    return cur.rowcount == 1


async def fail_job(conn: psycopg.AsyncConnection, job_id: UUID, error: str) -> None:
    """끝나지 않은 작업을 실패로 격리한다. 이미 끝난 작업은 그대로 둔다."""
    await conn.execute(
        """
        UPDATE jobs SET status = 'failed', error = %s, finished_at = now()
        WHERE id = %s AND status IN ('queued', 'running')
        """,
        (error, job_id),
    )


async def queued_job_ids(conn: psycopg.AsyncConnection) -> list[UUID]:
    """아직 어느 워커도 가져가지 않은 작업(오래된 순)."""
    cur = await conn.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at")
    return [row[0] for row in await cur.fetchall()]
```
③ 검증: `uv run pytest tests/test_db.py`

### 단계 3. 큐 적재 모듈 `job_queue` (실제 Redis)
① 실패 테스트. `conftest.py`에 Redis fixture를 더합니다.
```python
# tests/conftest.py (단계 3에서 추가)
from testcontainers.community.redis import RedisContainer  # noqa: E402  (다른 testcontainers import 옆)

REDIS_IMAGE = "redis:8.10.3-alpine"  # docker-compose.yml과 같은 이미지


def redis_url_of(container: RedisContainer) -> str:
    return f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def redis_container():
    with RedisContainer(REDIS_IMAGE) as container:
        yield container


@pytest.fixture
def redis_url(redis_container):
    client = redis_container.get_client()
    client.flushdb()
    client.close()
    return redis_url_of(redis_container)


@pytest.fixture
def stoppable_redis():
    """테스트 중에 멈출 Redis. 세션 Redis와 따로 띄운다. (컨테이너, URL)."""
    with RedisContainer(REDIS_IMAGE) as container:
        yield container, redis_url_of(container)
```
```python
# tests/test_queue.py
from uuid import uuid4

import pytest

from slack_rag import job_queue

pytestmark = pytest.mark.anyio


async def test_enqueue_puts_only_job_id_on_queue_once(redis_url):
    redis = await job_queue.connect(redis_url)
    try:
        job_id = uuid4()
        assert await job_queue.enqueue(redis, job_id) is True
        assert await job_queue.enqueue(redis, job_id) is False  # 대기 중인 같은 job_id는 넣지 않음

        [queued] = await redis.queued_jobs()  # arq가 테스트용으로 제공하는 조회
        assert (queued.job_id, queued.function, queued.args, queued.kwargs) == (
            str(job_id), "run_job", (str(job_id),), {},
        )
    finally:
        await redis.aclose()
```
② 최소 구현
```python
# src/slack_rag/job_queue.py
"""큐 적재. arq를 직접 쓰는 곳은 이 모듈(적재)과 worker.py(소비)뿐이다(TO-004)."""
from uuid import UUID

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

JOB_FUNCTION = "run_job"  # worker.run_job의 이름. arq는 함수 이름으로 작업을 찾는다


async def connect(redis_url: str) -> ArqRedis:
    return await create_pool(RedisSettings.from_dsn(redis_url))


async def enqueue(redis: ArqRedis, job_id: UUID) -> bool:
    """job_id만 큐에 넣는다. 같은 job_id가 이미 대기·실행 중이면 넣지 않고 False."""
    job = await redis.enqueue_job(JOB_FUNCTION, str(job_id), _job_id=str(job_id))
    return job is not None
```
③ 검증: `uv run pytest tests/test_queue.py`

### 단계 4. 설정 `redis_url`
① 실패 테스트(F1 테스트 수정 + 1개 추가)
```python
# tests/test_config.py
def test_reads_dotenv_shared_with_compose(tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "REDIS_URL", "JOBS_API_KEY", "SLACK_TARGET_CHANNEL_IDS"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=unrelated\n"
        "DATABASE_URL=postgresql://u:p@localhost:5432/db\n"
        "REDIS_URL=redis://localhost:6379/0\n"
        "JOBS_API_KEY=secret\n"
        "SLACK_TARGET_CHANNEL_IDS='[\"C0TARGET01\", \"C0TARGET02\"]'\n"
    )
    settings = Settings(_env_file=env_file)
    assert settings.slack_target_channel_ids == {"C0TARGET01", "C0TARGET02"}
    assert settings.redis_url == "redis://localhost:6379/0"


def test_rejects_empty_api_key():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql://u:p@localhost:5432/db",
            redis_url="redis://localhost:6379/0",  # 빈 키 때문에만 실패하도록
            jobs_api_key="",
            slack_target_channel_ids={"C0TARGET01"},
        )


def test_requires_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql://u:p@localhost:5432/db",
            jobs_api_key="secret",
            slack_target_channel_ids={"C0TARGET01"},
        )
```
② 최소 구현
```python
# src/slack_rag/config.py
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    jobs_api_key: str = Field(min_length=1)
    slack_target_channel_ids: set[str] = Field(min_length=1)
```
필수 키가 늘어 F1 API 테스트의 `client` fixture가 깨지므로 URL을 넘깁니다(API는 아직 쓰지 않음).
```python
# tests/test_api.py (client fixture만 수정)
@pytest.fixture
def client(database_url, redis_url, conn):  # conn이 jobs를, redis_url이 Redis를 비움
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        redis_url=redis_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
    )
    with TestClient(create_app(settings)) as client:
        yield client
```
③ 검증: `uv run pytest tests/test_config.py tests/test_api.py`

### 단계 5. API 적재 연결 (적재, 재적재, 시작된 작업 미적재, 503)
① 실패 테스트(순서: 새 작업 적재 → 미적재 작업 재적재 → 시작된 작업 미적재 → 503)
```python
# tests/test_api.py (단계 5에서 추가)
from arq.constants import default_queue_name

from slack_rag.db import create_job


def arq_queue_ids(redis_container) -> list[str]:
    """arq 기본 큐(정렬 집합)에 있는 작업 ID."""
    client = redis_container.get_client()
    try:
        return [member.decode() for member in client.zrange(default_queue_name, 0, -1)]
    finally:
        client.close()


def save_job(conn, event) -> str:
    """앞선 요청이 DB 저장 뒤 적재에 실패(503)한 상황을 만든다."""
    job_id, _, _ = create_job(
        conn,
        channel_id=event["channel"],
        message_ts=event["ts"],
        user_id=event["user"],
        message_text=event["text"],
    )
    return str(job_id)


def test_new_job_is_enqueued_by_its_job_id(client, redis_container, make_event):
    job_id = client.post("/jobs", json=make_event(), headers=AUTH).json()["job_id"]
    assert arq_queue_ids(redis_container) == [job_id]


def test_retry_of_saved_but_not_enqueued_job_enqueues_it(client, conn, redis_container, make_event):
    job_id = save_job(conn, make_event())
    res = client.post("/jobs", json=make_event(), headers=AUTH)  # n8n 재시도
    assert res.status_code == 202
    assert res.json() == {"status": "duplicate", "job_id": job_id}
    assert arq_queue_ids(redis_container) == [job_id]


@pytest.mark.parametrize("job_status", ["running", "done", "failed"])
def test_duplicate_of_started_job_is_not_enqueued(client, conn, redis_container, make_event, job_status):
    job_id = save_job(conn, make_event())
    conn.execute("UPDATE jobs SET status = %s WHERE id = %s", (job_status, job_id))
    res = client.post("/jobs", json=make_event(), headers=AUTH)
    assert res.status_code == 202
    assert res.json() == {"status": "duplicate", "job_id": job_id}
    assert arq_queue_ids(redis_container) == []


def test_returns_503_and_keeps_job_queued_when_queue_is_down(database_url, conn, stoppable_redis, make_event):
    container, redis_url = stoppable_redis
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        redis_url=redis_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
    )
    with TestClient(create_app(settings)) as client:
        container.get_wrapped_container().stop()  # 앱이 뜬 뒤 Redis 중단
        res = client.post("/jobs", json=make_event(), headers=AUTH)
    assert res.status_code == 503
    assert res.json() == {"detail": "queue unavailable"}
    assert conn.execute("SELECT status FROM jobs").fetchall() == [("queued",)]
```
F1의 `test_same_message_again_returns_same_job_without_new_record`는 그대로 통과해야 합니다(두 번째 요청은 `queued`라 다시 적재를 시도하고 arq가 무시 → 202 `duplicate`).

② 최소 구현
```python
# src/slack_rag/job_queue.py (단계 5에서 추가·변경)
from redis.exceptions import RedisError


class QueueUnavailable(Exception):
    """큐(Redis)에 적재하지 못했다."""


async def enqueue(redis: ArqRedis, job_id: UUID) -> bool:
    """job_id만 큐에 넣는다. 같은 job_id가 이미 대기·실행 중이면 넣지 않고 False."""
    try:
        job = await redis.enqueue_job(JOB_FUNCTION, str(job_id), _job_id=str(job_id))
    except (RedisError, OSError) as exc:  # redis-py는 연결 실패를 ConnectionError(RedisError)로 감쌈
        raise QueueUnavailable(repr(exc)) from exc
    return job is not None
```
```python
# src/slack_rag/main.py (끝 모습. 표시 없는 부분은 F1 그대로)
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from anyio import from_thread
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from psycopg_pool import ConnectionPool

from slack_rag import job_queue
from slack_rag.config import Settings
from slack_rag.db import apply_schema, create_job
from slack_rag.filters import ignore_reason
from slack_rag.logs import configure_logging
from slack_rag.schemas import SlackMessageEvent

logger = logging.getLogger("slack_rag.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging()
    pool = ConnectionPool(settings.database_url, open=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open(wait=True)  # DB에 못 붙으면 기본 30초 뒤 시작 실패
        with pool.connection() as conn:
            apply_schema(conn)
        app.state.queue = await job_queue.connect(settings.redis_url)  # 못 붙으면 약 5초 재시도 뒤 시작 실패
        yield
        await app.state.queue.aclose()
        pool.close()

    def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
        if x_api_key is None or not secrets.compare_digest(
            x_api_key.encode(), settings.jobs_api_key.encode()
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid api key")

    app = FastAPI(lifespan=lifespan)

    @app.post("/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
    def post_job(event: SlackMessageEvent, response: Response) -> dict:
        ref = {"channel_id": event.channel, "message_ts": event.ts}
        reason = ignore_reason(event, settings.slack_target_channel_ids)
        if reason is not None:
            logger.info("event_ignored", extra={"fields": {**ref, "reason": reason, "subtype": event.subtype}})
            response.status_code = status.HTTP_200_OK
            return {"status": "ignored", "reason": reason}

        with pool.connection() as conn:  # 블록이 끝날 때 커밋 → 그 뒤에 적재·응답
            job_id, created, job_status = create_job(
                conn,
                channel_id=event.channel,
                message_ts=event.ts,
                user_id=event.user,
                message_text=event.text,
            )
        result = "accepted" if created else "duplicate"
        fields = {**ref, "job_id": job_id}
        enqueued = False
        if job_status == "queued":  # 새 작업이거나, 앞선 요청이 적재에 실패했을 수 있는 작업(3.1)
            try:
                # 이 함수는 스레드풀에서 돈다. 비동기 적재는 이벤트 루프에서 돌리고 끝까지 기다린다(3.3)
                enqueued = from_thread.run(job_queue.enqueue, app.state.queue, job_id)
            except job_queue.QueueUnavailable as exc:
                logger.warning("job_enqueue_failed", extra={"fields": {**fields, "error": str(exc)}})
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="queue unavailable") from exc
        logger.info(f"job_{result}", extra={"fields": {**fields, "enqueued": enqueued}})
        return {"status": result, "job_id": str(job_id)}

    return app
```
`job_status`라는 이름은 `fastapi.status` import와 겹치지 않게 고른 것입니다.

③ 검증: `uv run pytest tests/test_api.py`, 이어서 `uv run pytest`

### 단계 6. 워커 `run_job`, `create_worker` (실제 PostgreSQL + Redis)
① 실패 테스트(순서: 적재된 작업 완료 → 시작 시 보정 → 끝난 작업 건너뛰기 → 로그 → 실패 격리 → 두 워커)
```python
# tests/test_worker.py
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from slack_rag import job_queue, worker
from slack_rag.config import Settings
from slack_rag.db import create_job
from slack_rag.main import create_app

pytestmark = pytest.mark.anyio

API_KEY = "test-api-key"
AUTH = {"X-API-Key": API_KEY}
HANGING_WORKER = Path(__file__).with_name("hanging_worker.py")


@pytest.fixture
def settings(database_url, redis_url, conn):  # conn이 jobs를, redis_url이 Redis를 비움
    return Settings(
        _env_file=None,
        database_url=database_url,
        redis_url=redis_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
    )


def make_job(conn, ts="1794381600.000100"):
    job_id, _, _ = create_job(
        conn, channel_id="C0TARGET01", message_ts=ts, user_id="U0HUMAN01", message_text="결제 API 5xx 급증"
    )
    return job_id


def job_row(conn, job_id):
    return conn.execute("SELECT status, attempts FROM jobs WHERE id = %s", (job_id,)).fetchone()


async def enqueue(settings, job_id):
    redis = await job_queue.connect(settings.redis_url)
    try:
        await job_queue.enqueue(redis, job_id)
    finally:
        await redis.aclose()


async def run_burst(settings, **overrides):
    """대기 작업이 없어질 때까지 워커를 돌린다(arq burst 모드). poll_delay는 테스트 속도용."""
    w = worker.create_worker(settings, burst=True, poll_delay=0.01, handle_signals=False, **overrides)
    try:
        await w.async_run()
    finally:
        await w.close()


async def test_enqueued_job_is_marked_done(settings, conn):
    job_id = make_job(conn)
    await enqueue(settings, job_id)
    await run_burst(settings)
    status, attempts, started_at, finished_at = conn.execute(
        "SELECT status, attempts, started_at, finished_at FROM jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert (status, attempts) == ("done", 1)
    assert started_at <= finished_at


async def test_worker_start_enqueues_jobs_left_only_in_db(settings, conn):
    job_ids = [make_job(conn, ts=f"1794381600.{i:06d}") for i in range(3)]  # 적재 실패로 DB에만 남음
    await run_burst(settings)
    assert [job_row(conn, job_id) for job_id in job_ids] == [("done", 1)] * 3


async def test_worker_does_not_process_finished_job_again(settings, conn, monkeypatch):
    handled = []

    async def record(job_id):
        handled.append(job_id)

    monkeypatch.setattr(worker, "handle_job", record)
    job_id = make_job(conn)
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (job_id,))
    await enqueue(settings, job_id)  # 오래된 중복이나 경합으로 끝난 작업이 큐에 다시 들어온 상황
    await run_burst(settings)
    assert handled == []
    assert job_row(conn, job_id) == ("done", 0)


async def test_worker_logs_queue_wait_and_processing_time_with_job_id(settings, conn, caplog):
    caplog.set_level(logging.INFO, logger="slack_rag")
    job_id = make_job(conn)
    await run_burst(settings)
    logged = {r.getMessage(): getattr(r, "fields", {}) for r in caplog.records if r.name == "slack_rag.worker"}
    assert logged["job_started"]["job_id"] == str(job_id)
    assert logged["job_started"]["queue_wait_ms"] >= 0
    assert logged["job_done"]["job_id"] == str(job_id)
    assert logged["job_done"]["duration_ms"] >= 0


async def raise_error(job_id):
    raise RuntimeError("처리 실패")


async def hang(job_id):
    await anyio.sleep(3600)


@pytest.mark.parametrize("handler", [raise_error, hang], ids=["exception", "timeout"])
async def test_job_failing_every_try_is_isolated_as_failed(settings, conn, monkeypatch, handler):
    monkeypatch.setattr(worker, "handle_job", handler)
    monkeypatch.setattr(worker, "RETRY_DELAY_S", 0)
    monkeypatch.setattr(worker, "JOB_TIMEOUT_S", 0.2)
    job_id = make_job(conn)
    await run_burst(settings)
    row = conn.execute("SELECT status, attempts, error FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == ("failed", worker.MAX_TRIES, "max_tries_exceeded")


async def test_two_workers_process_each_job_exactly_once(settings, conn, monkeypatch):
    handled = []

    async def record(job_id):
        handled.append(job_id)
        await anyio.sleep(0.05)  # 두 워커가 겹쳐 돌게 함

    monkeypatch.setattr(worker, "handle_job", record)
    job_ids = [make_job(conn, ts=f"1794381600.{i:06d}") for i in range(10)]
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_burst, settings)
        tg.start_soon(run_burst, settings)
    assert sorted(handled) == sorted(job_ids)
    assert conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall() == [("done", 10)]
```
② 최소 구현(끝 모습). 테스트별로 생기는 부분: 완료 → `run_job`의 claim·complete와 `create_worker`, 보정 → `startup`의 재적재, 건너뛰기 → `waited is None` 분기, 로그 → `job_started`/`job_done`, 실패 격리 → `try/except` + `Retry` + `job_try > MAX_TRIES` 분기 + `asyncio.timeout`, 두 워커 → 추가 구현 없이 통과(arq 실행 중 표시 + `claim_job`).
```python
# src/slack_rag/worker.py
"""큐 소비(arq 워커). arq를 직접 쓰는 곳은 이 모듈(소비)과 job_queue.py(적재)뿐이다(TO-004)."""
import asyncio
import logging
import time
from typing import Any
from uuid import UUID

from arq import Retry, Worker
from arq.connections import RedisSettings
from psycopg_pool import AsyncConnectionPool

from slack_rag import job_queue
from slack_rag.config import Settings
from slack_rag.db import claim_job, complete_job, fail_job, queued_job_ids
from slack_rag.logs import configure_logging

logger = logging.getLogger("slack_rag.worker")

MAX_TRIES = 3        # 처리 시도 상한. 넘으면 failed로 격리(3.5)
RETRY_DELAY_S = 10   # 처리 중 예외가 나면 이만큼 뒤에 다시 시도
JOB_TIMEOUT_S = 600  # 처리 단계 상한. 넘으면 예외로 보고 재시도(F3 이후 실측으로 조정)


async def handle_job(job_id: UUID) -> None:
    """작업의 실제 처리. F2에서는 아무것도 하지 않는다(F3에서 LangGraph 실행으로 바꾼다)."""


async def run_job(ctx: dict[str, Any], job_id: str) -> None:
    """arq가 부르는 작업 함수. 큐 메시지에는 job_id만 있다(TO-001)."""
    uid = UUID(job_id)
    job_try: int = ctx["job_try"]
    db: AsyncConnectionPool = ctx["db"]
    fields = {"job_id": job_id, "job_try": job_try}

    if job_try > MAX_TRIES:  # 앞선 시도가 모두 예외·타임아웃·워커 종료로 끝남
        async with db.connection() as conn:
            await fail_job(conn, uid, "max_tries_exceeded")
        logger.error("job_failed", extra={"fields": fields})
        return

    try:
        async with db.connection() as conn:
            waited = await claim_job(conn, uid, job_try)
        if waited is None:  # 이미 끝났거나 실패로 격리된 작업
            logger.info("job_skipped", extra={"fields": fields})
            return
        queue_wait_ms = round(waited.total_seconds() * 1000)
        logger.info("job_started", extra={"fields": {**fields, "queue_wait_ms": queue_wait_ms}})
        started = time.monotonic()
        async with asyncio.timeout(JOB_TIMEOUT_S):
            await handle_job(uid)
        async with db.connection() as conn:
            completed = await complete_job(conn, uid, job_try)
    except Exception as exc:  # 취소(CancelledError)는 Exception이 아니라 그대로 arq로 → 재실행
        logger.warning("job_retry", extra={"fields": {**fields, "error": repr(exc)}})
        raise Retry(defer=RETRY_DELAY_S) from exc

    duration_ms = round((time.monotonic() - started) * 1000)
    # completed가 False면 그 사이 더 나중 시도가 가져갔다(완료는 그 시도가 기록)
    event = "job_done" if completed else "job_superseded"
    logger.info(event, extra={"fields": {**fields, "duration_ms": duration_ms}})


def create_worker(settings: Settings, **overrides: Any) -> Worker:
    """arq Worker를 만든다. overrides는 테스트가 burst, poll_delay 등 arq 인자를 바꿀 때 쓴다."""

    async def startup(ctx: dict[str, Any]) -> None:
        db = AsyncConnectionPool(settings.database_url, open=False)
        await db.open(wait=True)
        ctx["db"] = db
        # 미적재 보정(3.6): DB에만 있고 큐에 없을 수 있는 대기 작업을 다시 넣는다. 이미 있으면 arq가 무시
        async with db.connection() as conn:
            job_ids = await queued_job_ids(conn)
        for job_id in job_ids:
            await job_queue.enqueue(ctx["redis"], job_id)
        logger.info("worker_started", extra={"fields": {"queued_jobs": len(job_ids)}})

    async def shutdown(ctx: dict[str, Any]) -> None:
        await ctx["db"].close()

    options: dict[str, Any] = {
        "functions": [run_job],             # 이름 "run_job" = job_queue.JOB_FUNCTION
        "redis_settings": RedisSettings.from_dsn(settings.redis_url),
        "on_startup": startup,
        "on_shutdown": shutdown,
        "max_jobs": 1,                      # 워커당 동시 1건(TO-002)
        "max_tries": MAX_TRIES + 1,         # 마지막 1회는 failed 기록용(3.5)
        "job_timeout": JOB_TIMEOUT_S + 30,  # arq가 먼저 끊으면 재시도 없이 끝나고 DB에 남길 수 없음
        "keep_result": 0,                   # 결과는 jobs에. 끝난 job_id를 arq가 막지 않게(3.2)
    }
    return Worker(**(options | overrides))


def main() -> None:
    configure_logging()
    create_worker(Settings()).run()  # SIGTERM이면 실행 중 작업을 취소하고 큐에 남긴 뒤 종료


if __name__ == "__main__":
    main()
```
참고
- `Worker.run()`은 Python 3.13에서 `asyncio.get_event_loop()` 경고(DeprecationWarning, 기본 숨김)를 낼 수 있지만 동작은 같습니다(arq가 3.14의 RuntimeError도 처리, 소스 확인).
- arq 자체 로그(`arq.worker`)는 설정하지 않아 WARNING 이상만 표준 오류로 나옵니다. 관측에 필요한 값은 위 JSON 로그(`slack_rag.worker`)에 있습니다.
- 테스트 종료 시 arq가 redis-py의 deprecated `close()`를 불러 경고가 보일 수 있습니다(실패 아님).

③ 검증: `uv run pytest tests/test_worker.py -k "not killed and not hundred"`

### 단계 7. 처리 중 강제 종료(SIGKILL) 재처리
① 실패 테스트
```python
# tests/hanging_worker.py
"""test_worker.py의 강제 종료 테스트가 띄우는 워커 프로세스.

작업을 처리 중(running)으로 바꾼 뒤 이벤트 루프째 멈춰서, 테스트가 SIGKILL로 죽일 때까지 기다린다.
"""
import time

from slack_rag import worker
from slack_rag.config import Settings


async def block_event_loop(job_id):
    time.sleep(3600)  # await가 아닌 sleep: 타임아웃·취소가 끼어들 수 없음


if __name__ == "__main__":
    worker.handle_job = block_event_loop
    # job_timeout=1 → arq의 실행 중 표시(in-progress 키)가 1 + 10초 뒤 만료된다
    worker.create_worker(Settings(), job_timeout=1).run()
```
```python
# tests/test_worker.py (단계 7에서 추가)
async def wait_for_status(conn, job_id, expected, timeout=30):
    with anyio.fail_after(timeout):
        while job_row(conn, job_id)[0] != expected:
            await anyio.sleep(0.1)


async def test_job_of_killed_worker_is_processed_again(settings, conn, tmp_path):
    job_id = make_job(conn)  # 큐 적재는 띄운 워커의 시작 보정이 함
    env = os.environ | {
        "DATABASE_URL": settings.database_url,
        "REDIS_URL": settings.redis_url,
        "JOBS_API_KEY": settings.jobs_api_key,
        "SLACK_TARGET_CHANNEL_IDS": json.dumps(sorted(settings.slack_target_channel_ids)),
    }
    # cwd=tmp_path: 사용자의 .env를 읽지 않게
    proc = subprocess.Popen([sys.executable, str(HANGING_WORKER)], env=env, cwd=tmp_path)
    try:
        await wait_for_status(conn, job_id, "running")
    finally:
        proc.kill()  # SIGKILL: arq가 정리할 틈 없이 죽음
        proc.wait()

    await run_burst(settings)  # 실행 중 표시가 만료(약 11초)되면 2번째 시도로 가져가 완료

    assert job_row(conn, job_id) == ("done", 2)
```
② 최소 구현: 없음(단계 6의 `claim_job` 조건 `attempts < job_try`가 이 경우를 위한 것). 이 단계에서 실패하면 원인은 단계 2·6 구현에 있습니다.

③ 검증: `uv run pytest tests/test_worker.py -k killed` (약 15초)

### 단계 8. 로드맵 완료 기준: 100건 → 모두 202 → 모두 완료
① 실패 테스트(단계 5·6이 끝났다면 바로 통과해야 함. 통과하지 않으면 그 단계로 돌아감)
```python
# tests/test_worker.py (단계 8에서 추가)
async def test_hundred_requests_get_202_at_once_and_all_become_done(settings, conn, make_event):
    with TestClient(create_app(settings)) as client:
        codes = [
            client.post("/jobs", json=make_event(ts=f"1794381600.{i:06d}"), headers=AUTH).status_code
            for i in range(100)
        ]
    assert codes == [202] * 100  # 워커가 돌기 전에 모두 202 → 응답이 처리를 기다리지 않음

    await run_burst(settings)

    assert conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall() == [("done", 100)]
```
TestClient는 자체 스레드·이벤트 루프에서 앱을 돌리므로 비동기 테스트 안에서 동기로 불러도 됩니다(앱과 테스트가 객체를 공유하지 않음). 문제가 생기면 이 테스트만 동기 함수로 바꾸고 워커를 `asyncio.run(run_burst(settings))`로 돌립니다.

③ 검증: `uv run pytest tests/test_worker.py -k hundred`, 이어서 **`uv run pytest` 전체**

### 단계 9. 컨테이너 조립 (자동 테스트 없음, 명령으로 확인)
6.3의 `docker-compose.yml`, 6.4의 `.env.example`을 반영하고, 사용자가 `.env`에 `REDIS_URL`을 추가했는지 확인합니다(`.env`는 덮어쓰지 않음).
```bash
docker compose up -d --build postgres redis api worker
docker compose ps                                   # redis healthy, api·worker running
docker compose logs worker | grep '"event": "worker_started"'   # {"queued_jobs": N} (F1 행 수, 가정 B5)
set -a; source .env; set +a
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c 'SELECT status, count(*) FROM jobs GROUP BY status'        # F1 행이 모두 done
```
첫 기동에서 워커가 API보다 먼저 스키마 없는 DB를 만나면 한 번 종료되고 다시 뜹니다(3.8, `docker compose logs worker`에 오류 1회는 정상).

### 단계 10. 수동 확인 (7장 체크리스트)

---

## 6. 인프라 스니펫

### 6.1 `pyproject.toml` 변경분
```toml
[project]
name = "slack-rag"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "anyio>=4.16.0",                # main.py의 from_thread.run (fastapi 하위 의존을 직접 씀)
    "arq>=0.28.0",                  # 큐(TO-004). redis[hiredis]>=4.2,<6 을 함께 설치
    "fastapi>=0.162.5",
    "pydantic>=2.14.3",
    "pydantic-settings>=2.16.2",
    "psycopg[binary,pool]>=3.3.8",
    "redis>=5.3.1,<6",              # job_queue.py가 예외 클래스를 직접 씀. arq 0.28이 6 미만만 허용
    "uvicorn>=0.54.0",
]

[dependency-groups]
dev = [
    "httpx2>=2.16.0",          # Starlette 1.10 TestClient가 요구(httpx 아님)
    "pytest>=9.2.0",
    "testcontainers>=4.15.0",  # postgres·redis 모듈 모두 extra 없이 포함. [redis] extra는 redis>=7을 요구해 arq와 충돌
]
```
`[build-system]`, `[tool.pytest.ini_options]`는 그대로입니다. 비동기 테스트는 anyio 플러그인(설치된 anyio에 내장, 자동 등록)을 씁니다.

### 6.2 `Dockerfile`
변경 없습니다. 워커는 같은 이미지를 compose `command`로 다르게 실행합니다.

### 6.3 `docker-compose.yml` (끝 모습)
```yaml
services:
  postgres:
    image: postgres:18.6-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports:
      - "127.0.0.1:5432:5432"     # 로컬 uv run, psql 확인용
    volumes:
      - postgres_data:/var/lib/postgresql   # 18부터 이 경로(…/data 아님)
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 5s
      timeout: 3s
      retries: 10

  redis:
    image: redis:8.10.3-alpine
    command: ["redis-server", "--appendonly", "yes"]   # 재시작해도 대기 중인 작업이 남게(AOF)
    ports:
      - "127.0.0.1:6380:6379"     # 로컬 uv run, redis-cli 확인용(호스트 6379는 다른 프로젝트가 사용 중)
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  api:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      REDIS_URL: redis://redis:6379/0
    ports:
      - "127.0.0.1:8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  worker:
    build: .
    command: ["python", "-m", "slack_rag.worker"]
    env_file: .env
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      REDIS_URL: redis://redis:6379/0
    depends_on:
      api:
        condition: service_started   # 스키마는 api가 시작하며 적용(워커가 먼저 뜨면 실패 후 재시작)
      redis:
        condition: service_healthy
    restart: unless-stopped          # Redis가 끊기면 워커가 종료됨 → 다시 떠서 대기 작업을 보정(3.6)

  n8n:
    image: n8nio/n8n:2.47.4
    environment:
      WEBHOOK_URL: ${N8N_WEBHOOK_URL}         # 터널 주소. Slack Trigger의 Production URL에 쓰임
      N8N_PROXY_HOPS: "1"                     # 터널(프록시) 1단 뒤에 있음
      N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS: "true"
    ports:
      - "127.0.0.1:5678:5678"     # 터널이 이 포트로 연결
    volumes:
      - n8n_data:/home/node/.n8n  # 워크플로, 자격 증명, 암호화 키(SQLite)
    depends_on:
      - api

volumes:
  postgres_data:
  redis_data:
  n8n_data:
```
- 호스트 포트는 6380입니다. 이 개발 PC의 6379는 다른 프로젝트(`flight-monitor`의 `flight-redis`)가 쓰고 있습니다(2026-11-13 확인). 컨테이너끼리는 `redis:6379`로 통신하므로 영향이 없습니다.
- 워커를 늘릴 때: `docker compose up -d --scale worker=2`(워커에는 포트·`container_name`이 없어 가능).

### 6.4 `.env.example` 변경분
```dotenv
# PostgreSQL (docker compose)
POSTGRES_USER=slack_rag
POSTGRES_PASSWORD=change-me
POSTGRES_DB=slack_rag

# FastAPI · 워커
# 로컬에서 uv run으로 띄울 때 쓰는 값. compose의 api·worker 컨테이너는 docker-compose.yml에서 덮어씀
DATABASE_URL=postgresql://slack_rag:change-me@localhost:5432/slack_rag
REDIS_URL=redis://localhost:6380/0
# openssl rand -hex 32 로 생성. n8n Header Auth 자격 증명의 Value에도 같은 값
JOBS_API_KEY=
# 대상 채널 ID JSON 배열(작은따옴표로 감쌈)
SLACK_TARGET_CHANNEL_IDS='["C0123456789"]'

# n8n: 터널 주소(끝에 / 포함)
N8N_WEBHOOK_URL=https://example.trycloudflare.com/
```
**사용자가 자기 `.env`에 직접 추가할 키**: `REDIS_URL=redis://localhost:6380/0` 한 줄(2026-11-13 추가 완료). 워커도 같은 `.env`를 읽으므로 `JOBS_API_KEY`, `SLACK_TARGET_CHANNEL_IDS`가 비어 있지 않아야 시작합니다(이미 F1에서 채움).

### 6.5 DB 스키마 변경 SQL
5장 단계 1의 `ALTER TABLE` 문이 전부입니다. `schema.sql`의 F1 `CREATE TABLE` 뒤에 이어 쓰고, 앱 시작 때 `apply_schema()`가 적용합니다. 되돌릴 일이 생기면 `ALTER TABLE jobs DROP COLUMN …`로 지울 수 있는 추가 변경이라 마이그레이션 도구는 아직 넣지 않습니다(F1 3.5).

---

## 7. 수동 확인 체크리스트

공통 준비(zsh·bash 공통. zsh는 변수에 담은 명령을 단어로 나누지 않으므로 함수로 정의):
```bash
docker compose up -d --build
set -a; source .env; set +a
CH=$(python3 -c "import json,os; print(next(iter(json.loads(os.environ['SLACK_TARGET_CHANNEL_IDS']))))")
pg() { docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"; }
```

한 건 보내기(아래에서 `post <ts>`로 부름):
```bash
post() {
  curl -s -w ' %{http_code}\n' -X POST http://localhost:8000/jobs \
    -H 'Content-Type: application/json' -H "X-API-Key: $JOBS_API_KEY" \
    -d "{\"type\":\"message\",\"channel\":\"$CH\",\"user\":\"U0HUMAN01\",\"text\":\"수동 확인\",\"ts\":\"$1\"}"
}
```

### M1. 기동과 F1 행 처리
- [ ] `docker compose ps`: `redis`(healthy), `api`, `worker` 실행 중.
- [ ] `docker compose logs worker | grep worker_started` → `queued_jobs`가 F1 행 수.
- [ ] `pg -c 'SELECT status, count(*) FROM jobs GROUP BY status'` → 모두 `done`(가정 B5).

### M2. 100건 부하 (로드맵 완료 기준 ①)
```bash
BASE=$(date +%s)
seq 1 100 | xargs -P 20 -I{} curl -s -o /dev/null -w '%{http_code} %{time_total}\n' -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' -H "X-API-Key: $JOBS_API_KEY" \
  -d "{\"type\":\"message\",\"channel\":\"$CH\",\"user\":\"U0HUMAN01\",\"text\":\"부하 {}\",\"ts\":\"$BASE.{}\"}" \
  > /tmp/f2-load.txt
cut -d' ' -f1 /tmp/f2-load.txt | sort | uniq -c      # 100 202
sort -k2 -n /tmp/f2-load.txt | tail -1                # 가장 느린 응답(처리를 기다리지 않으므로 짧아야 함)
pg -c "SELECT status, count(*) FROM jobs WHERE message_ts LIKE '$BASE.%' GROUP BY status"   # 반복 실행
```
- [ ] 100건 모두 202.
- [ ] 워커 1개 기준 약 50초 안에 `done | 100`(0.5초 폴링마다 1건, 1.6. 계산값).
- [ ] `docker compose logs worker | grep '"event": "job_started"' | tail -3` → `queue_wait_ms`가 점점 커짐, `job_done`에 `duration_ms`, 둘 다 `job_id` 포함.
- [ ] (선택) `docker compose up -d --scale worker=2` 후 다시 실행 → 더 빨리 끝나고, `SELECT count(*) FROM jobs WHERE message_ts LIKE '<새 BASE>.%' AND attempts <> 1` → 0(중복 처리 없음).

### M3. 워커 정상 종료(SIGTERM) 중 유실 없음 (완료 기준 ②의 SIGTERM 경로)
- [ ] M2 명령을 새 `BASE`로 보내고 몇 초 뒤 `docker compose stop worker`.
- [ ] `pg -c "SELECT status, count(*) FROM jobs WHERE message_ts LIKE '$BASE.%' GROUP BY status"` → `done` 일부 + `queued` 나머지(+ 드물게 `running` 1건).
- [ ] `docker compose start worker` → 모두 `done`. `running`이던 작업이 있었다면 그 행은 `attempts = 2`.

### M4. (선택, 약 11분) 처리 중 강제 종료(SIGKILL) — 자동 테스트 `test_job_of_killed_worker_is_processed_again`의 실제 환경 판
임시 처리는 수 ms라 "처리 중"인 틈이 없으므로 행 잠금으로 워커를 가져가기 직후 상태에 붙잡아 둡니다.
**주의:** 2번에서 트랜잭션을 열어 둔 동안에는 API를 재시작하지 마세요(`up -d --build api` 등). API 시작이 스키마 적용에서 멈춥니다(3.8 주의).
1. `docker compose stop worker` → `post $(date +%s).1` → 응답의 `job_id`를 `JOB`에 저장.
2. 터미널 A: `docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"` → `BEGIN; SELECT id FROM jobs WHERE id = '<JOB>' FOR UPDATE;`(열어 둠).
3. 터미널 B: `docker compose start worker` → `pg -c "SELECT pid, wait_event_type, left(query, 50) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"` → 워커의 `UPDATE jobs SET status = 'running'…`이 잠금 대기(= 큐에서 꺼내 처리를 시작한 상태).
4. `docker compose kill worker`(SIGKILL) → 터미널 A에서 `ROLLBACK;`.
5. `docker compose start worker` → `docker compose exec redis redis-cli TTL arq:in-progress:<JOB>` → 남은 초(최대 640).
- [ ] TTL이 끝난 뒤 `pg -c "SELECT status, attempts FROM jobs WHERE id = '<JOB>'"` → `done | 2`.

### M5. Redis 중단 → 503 → 재시도로 복구 (이중 쓰기 대응)
- [ ] `docker compose stop redis` → `post $(date +%s).2` (ts를 `TS`로 저장) → `{"detail":"queue unavailable"} 503`.
- [ ] `docker compose logs api | grep job_enqueue_failed` → 같은 `job_id`. `pg -c "SELECT status FROM jobs WHERE message_ts = '$TS'"` → `queued`.
- [ ] `docker compose ps worker` → 재시작 반복(Restarting) 상태.
- [ ] `docker compose start redis` → `post $TS`(n8n 재시도 흉내) → `{"status":"duplicate",…} 202`, `docker compose logs api | grep job_duplicate | tail -1` → `"enqueued": true`.
- [ ] 워커가 다시 뜬 뒤 그 행이 `done`.

### M5b. 긴 큐 장애(n8n이 포기한 경우) → 워커 재시작 보정
- [ ] `docker compose stop redis` → `post $(date +%s).3` → 503(다시 보내지 않음).
- [ ] `docker compose start redis` → 워커가 다시 뜨면 `docker compose logs worker | grep worker_started | tail -1` → `queued_jobs` ≥ 1 → 그 행이 `done`.
- [ ] 워커가 계속 떠 있었는데 `queued`가 남는 경우(3.6의 남는 틈): `docker compose restart worker`로 보정.

### M6. (선택) 실제 Slack 경로
- [ ] `docker compose stop redis` → 대상 채널에 메시지 → 15초 안에 `docker compose start redis` → n8n 실행이 재시도 끝에 성공, 그 작업이 `done`.
- [ ] 평소 상태에서 메시지 1건 → n8n 성공, `job_accepted`(`enqueued: true`) → `done`.

### 운영 메모 (문서화만, 자동화는 범위 밖)
```sql
-- 처리 중으로 오래 남은 작업(3.5의 남는 틈). 정상이면 0건
SELECT id, attempts, started_at FROM jobs WHERE status = 'running' AND started_at < now() - interval '15 minutes';
-- 격리된 실패 작업
SELECT id, attempts, error, finished_at FROM jobs WHERE status = 'failed' ORDER BY finished_at DESC;
-- 원인을 고친 뒤 다시 처리: queued로 되돌리고 워커를 재시작(시작 보정이 적재)
UPDATE jobs SET status = 'queued', error = NULL WHERE id = '<job_id>';
```
그다음 `docker compose restart worker`. 큐 길이 확인: `docker compose exec redis redis-cli ZCARD arq:queue`.

---

## 8. 완료 기준과 범위 밖

### 8.1 완료 기준
- [ ] `uv run pytest` 전체 통과(Docker 실행 중, F1 37개 포함).
- [ ] **100건 연속 → 모두 즉시 202 → 워커가 모두 완료**(PLAN.md:8): `test_worker.py::test_hundred_requests_get_202_at_once_and_all_become_done`, M2.
- [ ] **처리 중 워커를 강제 종료해도 다시 처리**: `test_worker.py::test_job_of_killed_worker_is_processed_again`(SIGKILL), M3(SIGTERM), 선택 M4.
- [ ] **같은 작업을 두 워커가 동시에 처리하지 않음**: `test_db.py::test_claim_takes_queued_job_only_once_per_try`, `test_db.py::test_later_try_takes_over_running_job_and_earlier_try_cannot_complete`, `test_worker.py::test_two_workers_process_each_job_exactly_once`.
- [ ] **반복 실패 작업 격리**: `test_worker.py::test_job_failing_every_try_is_isolated_as_failed[exception|timeout]`.
- [ ] **이중 쓰기 대응(503, 재적재, 시작된 작업 미적재)**: `test_api.py::test_returns_503_and_keeps_job_queued_when_queue_is_down`, `::test_retry_of_saved_but_not_enqueued_job_enqueues_it`, `::test_duplicate_of_started_job_is_not_enqueued`, M5.
- [ ] **끝난 작업은 다시 처리하지 않음**: `test_worker.py::test_worker_does_not_process_finished_job_again`, `test_db.py::test_finished_job_cannot_be_claimed`.
- [ ] **미적재 작업 보정**: `test_worker.py::test_worker_start_enqueues_jobs_left_only_in_db`, M5b.
- [ ] **관측성(큐 대기·처리 시간, job_id)**: `test_worker.py::test_worker_logs_queue_wait_and_processing_time_with_job_id`, M2 로그.
- [ ] **arq·redis를 직접 쓰는 곳은 두 모듈뿐**(TO-004): `grep -rlE '^(from|import) (arq|redis)' src/slack_rag` → `job_queue.py`, `worker.py`만.
- [ ] 큐 메시지에 `job_id`만: `test_queue.py::test_enqueue_puts_only_job_id_on_queue_once`.

### 8.2 범위 밖 (다음 단계로 넘김)
| 항목 | 넘기는 곳 |
|---|---|
| `handle_job()` 본문: 작업 로드, LangGraph 실행, 체크포인트로 재실행 시 이어 하기 | F3 |
| `MAX_TRIES`, `JOB_TIMEOUT_S`(= SIGKILL 재처리 지연) 재조정, 비용을 고려한 재시도 정책 | F3~F8(OQ-10, OQ-21) |
| Redis 캐시(LLM 응답, 임베딩) | F3, F4 |
| 주기적 미적재 보정, `running`으로 멈춘 작업 자동 회수, 실패 작업 재처리 도구 | 필요해지면(잦아지면 TO-001 다시 볼 조건: DB 큐·아웃박스) |
| 작업 상태 조회 API | 필요해지는 단계(F9 이전에는 DB로 확인) |
| Redis가 응답 없이 멈춘 경우의 요청 타임아웃(현재는 연결 실패만 503) | 필요해지면 |
| arq 기본 직렬화(pickle): Redis에 쓸 수 있으면 워커에서 코드 실행 가능 → Redis 인증·TLS, JSON 직렬화 | 배포 결정 시(OQ-19, TO-003) |
| arq 자체 로그의 JSON화, 메트릭·대시보드 | OQ-23 |
| 큐 교체(Pub/Sub, Streams 등) | TO-004 다시 볼 조건 |
| 서비스·저장소별 직렬 처리 | F5 이후(TO-002 다시 볼 조건) |
| 4번째(기록용) 실행에서 `fail_job`이 행을 바꾸지 않았는데도 `job_failed` ERROR 로그를 남기는 문제(3번째 시도가 `done` 커밋 직후 SIGKILL된 드문 경우. DB는 `done`으로 올바름) → `fail_job`의 `rowcount`로 실제로 바꿨을 때만 `job_failed`, 아니면 `job_skipped` | F3(실패 처리와 로그를 다시 설계할 때, 리뷰 지적 #4) |
| SIGTERM 재시작도 `job_try`를 써서 긴 작업이 처리 실패 없이 `failed`가 될 수 있음(3.5) | F3 |

### 8.3 미확인 항목 모음
구현 중(2026-11-15) 확인한 결과를 `→`로 덧붙였습니다.
1. Docker Desktop(macOS)에서 컨테이너를 멈춘 직후 포트 연결이 곧바로 거부되는지(503 테스트가 이를 기대. 연결이 매달리면 테스트가 멈춤). → **확인.** 테스트에서는 `ConnectionError`(Errno 61), compose에서는 DNS 실패(`Name or service not known`)로 곧바로 503.
2. TestClient를 anyio 비동기 테스트 안에서 동기로 부르는 방식이 이 환경에서 문제없는지(단계 8에 대안 적음). → **확인.** 문제없음(100건 테스트 6.5초).
3. `docker compose kill` 뒤 `restart: unless-stopped`가 컨테이너를 자동으로 다시 띄우는지(체크리스트는 명시적으로 `start`), Docker 재시작 지연의 상한. → **자동으로 다시 뜨지 않음**(`Exited (137)`, `start` 필요, 2.6 반영). Redis 오류로 종료된 경우는 1~5초 간격 자동 재시작. 실제 OOM 동작과 재시작 지연 상한은 여전히 미확인.
4. M4에서 강제 종료된 워커의 잠금 대기 `UPDATE`가 잠금이 풀린 뒤 롤백되는지(PostgreSQL이 끊긴 클라이언트를 감지하는 시점). → **확인.** 롤백됨. 행은 `queued|0`으로 남고 잠금 대기도 남지 않음.
5. AOF 로딩 중 `redis-cli ping` 헬스체크의 종료 코드. → 여전히 미확인(AOF가 작아 로딩 중 상태를 재현하지 못함).
6. 100건 처리 시간(워커 1개 약 50초)은 arq 소스로 계산한 값이며 실측 전. → **확인.** 워커 1개 49.9초, 워커 2개 25.0초(중복 처리 0건).

TO-004의 "DLQ 미확인"은 이번에 소스로 확인했습니다(없음, 1.6). `docs/tradeOff.md` TO-004의 감수한 비용에 "DLQ 없음", "일반 예외와 `job_timeout` 초과는 재시도 없이 실패"를 **반영함(2026-11-15)**.
