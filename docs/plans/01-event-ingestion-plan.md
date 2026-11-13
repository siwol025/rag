# F1. 이벤트 수신 상세 구현 계획 (Slack → n8n → FastAPI)

> 작성일 2026-11-12 · 입력: `PLAN.md`, `docs/plans/00-overall-roadmap.md`(F1), `docs/researchs/plan-architecture.md`(3장 단계 1·2, 10장), `docs/researchs/external-api-specs.md`(8.1.1, 8.1.2, 8.1.6, 8.2.1~8.2.3, 8.3)
> 다음 단계: TDD 서브에이전트가 5장 순서대로 `/mattpocock-skills:tdd`(red → green)로 구현합니다. 시작 전에 3.6의 seam 목록을 사용자와 확인합니다.

---

## 1. 개요

### 1.1 목표
대상 채널에 사람이 새 최상위 메시지를 쓰면, 사람 개입 없이 FastAPI에 **작업 기록 1건**이 생기고 `202 {job_id}`가 반환됩니다(PLAN.md:3-6, 로드맵 F1).

### 1.2 범위
| 포함 | 제외(다음 단계 또는 범위 밖) |
|---|---|
| Slack 앱 manifest(최소 스코프, 이벤트 구독) | 큐 적재, 워커, 작업 상태 전이(F2) |
| n8n 워크플로: Slack Trigger(Signing Secret 필수) → HTTP Request(Header Auth, Retry On Fail) | LLM 호출(F3~) |
| FastAPI `POST /jobs`: 인증, 입력 검증, 필터, 멱등 저장, 즉시 202 | Slack 회신, 접수 이모지, Interactivity(F9) |
| PostgreSQL `jobs` 테이블 1개, `(channel_id, message_ts)` 유일 제약 | 누락분 대조(reconciliation), n8n 에러 워크플로(관리자 알림) |
| 프로젝트 골격(uv, Dockerfile, docker compose) | 메트릭, 트레이싱(OQ-23), 보존 기간(OQ-26) |
| 관측성 최소치: 응답과 로그의 job_id, JSON 로그 | 마이그레이션 도구, 페이로드 크기 제한, rate limit |

### 1.3 사용자가 확정한 결정
| ID | 결정 | 이 계획에 반영된 곳 |
|---|---|---|
| OQ-06 | 대상 채널의 **새 최상위 메시지 전부** 접수. 스레드 답글, 수정(`message_changed`), 삭제(`message_deleted`) 등은 무시 | 필터 `ignore_reason()`(5장 단계 2) |
| OQ-05 | **사람 메시지만** 접수. `bot_id`가 있거나 `subtype`이 있는 메시지 등 봇 메시지 전부 차단. 단 **사람이 파일을 첨부한 메시지(`subtype: file_share`)는 접수**(2026-11-13 사용자 결정) | 같은 필터 |
| OQ-07 | n8n **셀프호스팅 Docker**. compose로 FastAPI, PostgreSQL과 함께 실행. 로컬은 터널로 Slack → n8n 연결. F1에서 n8n은 이벤트 전달만 | 6장 compose, n8n 워크플로 |

### 1.4 가정 (미확정 사항을 대신함)
- **가정 A1 (OQ-24 대체)**: 사내 내부 Slack 앱, 워크스페이스 1개, 대상 채널은 설정값 `SLACK_TARGET_CHANNEL_IDS`(1개 이상).
- **가정 A2**: 대상 채널은 **공개 채널**입니다(`channels:history`, `message.channels`). 비공개 채널이면 `groups:history` 스코프와 `message.groups` 이벤트를 추가해야 합니다.
- **가정 A3**: 작업 기록 저장소는 PostgreSQL입니다(F2·F3 checkpoint, F4 pgvector 후보와 맞춤).
- **가정 A4**: Python 3.11 이상(`requires-python = ">=3.11"`, 컨테이너는 3.13), FastAPI, uv, pytest.
- **가정 A5**: 개발 PC에 Docker가 있습니다. compose 실행과 DB 테스트(testcontainers) 둘 다 Docker를 씁니다.
- **가정 A6**: n8n 내부 저장소는 기본 SQLite(`n8n_data` 볼륨)를 씁니다. 앱 PostgreSQL과 섞지 않아 설정이 가장 적습니다.
- **가정 A7**: 외부(터널)에 노출하는 것은 n8n(5678)뿐입니다. FastAPI와 PostgreSQL은 compose 내부망과 `127.0.0.1`에만 엽니다.

### 1.5 확정 규칙의 영향
- **파일을 첨부한 사람 메시지는 접수합니다.** Slack은 파일 첨부 메시지에 `subtype: "file_share"`를 붙입니다(external-api-specs 8.1.1). "subtype이 있으면 차단" 규칙에서 `file_share`만 예외로 둡니다(2026-11-13 사용자 결정). 저장하는 것은 메시지 본문(`text`)뿐이고, **첨부 파일 내용은 다루지 않습니다**(`files:read` 스코프도 넣지 않음). 텍스트 없이 파일만 올린 메시지는 빈 본문(`""`)으로 접수됩니다.
- `file_share`라도 봇이 올렸거나(`bot_id`) 스레드 답글이면 뒤따르는 `not_human`, `thread_reply` 판정에서 걸러집니다.
- 스레드 답글을 채널에도 보내는 경우(`thread_broadcast`)는 subtype이 있어 무시됩니다(스레드 답글 무시 결정과 일치).

### 1.6 확인한 버전 (2026-11-12 조회)
| 대상 | 버전 | 확인 출처 |
|---|---|---|
| fastapi | 0.162.5 (Python ≥3.10) | PyPI JSON API |
| starlette (fastapi 하위 의존) | 1.10.0 | PyPI. **TestClient가 `httpx2`를 요구**(없으면 `httpx`로 대체하며 경고). 소스 `starlette/testclient.py@1.10.0` 확인 |
| pydantic / pydantic-settings | 2.14.3 / 2.16.2 (pydantic-settings는 Python ≥3.11) | PyPI |
| psycopg / psycopg-binary / psycopg-pool | 3.3.8 / 3.3.8(cp310~cp315 휠) / 3.3.3 | PyPI |
| uvicorn | 0.54.0 | PyPI |
| pytest / httpx2 / testcontainers | 9.2.0 / 2.16.0 / 4.15.0 | PyPI. testcontainers 4.15는 `testcontainers.community.postgres` 경로(기존 `testcontainers.postgres`는 deprecated). 소스 확인 |
| uv / uv_build | 0.13.7 / 0.13.7 | PyPI, `ghcr.io/astral-sh/uv:0.13.7` 매니페스트 200 확인 |
| n8n | 2.47.4 (npm stable/latest), Docker `n8nio/n8n:2.47.4` | npm registry, Docker Hub |
| n8n Slack Trigger 노드 | `n8n-nodes-base.slackTrigger` v1 | 소스 `SlackTrigger.node.ts@n8n@2.47.4`(master와 동일) |
| n8n HTTP Request 노드 | `n8n-nodes-base.httpRequest` v4.5(기본 버전), Timeout 기본 10000ms | 소스 `HttpRequest/V3/Description.ts@n8n@2.47.4` |
| n8n Retry On Fail | Max Tries 기본 3(2~5로 제한), Wait Between Tries 기본 1000ms(0~5000ms로 제한), 고정 간격. **4xx 포함 모든 노드 오류에서 재시도** | 소스 `workflow-execute.ts@n8n@2.47.4` `getRetryParams()`와 재시도 루프 |
| PostgreSQL 이미지 | `postgres:18.6-alpine`. 18부터 볼륨 경로가 `/var/lib/postgresql` | Docker Hub, docker-library 문서 |
| Python 이미지 | `python:3.13-slim`(3.13.18) | Docker Hub |
| langgraph-checkpoint-postgres(참고, F3) | 3.2.0, `psycopg>=3.2` 의존 | PyPI |

FastAPI 0.115 이후 F1에 영향 있는 변경: 0.132.0부터 JSON 요청에 `Content-Type: application/json`이 **필수**(strict content type), 0.128.0에서 `pydantic.v1` 지원 제거, 0.129.0에서 Python 3.9 지원 종료. n8n HTTP Request(Body Content Type JSON)는 이 헤더를 보냅니다.

---

## 2. 전체 흐름과 데이터 계약

### 2.1 흐름
```
[Slack 대상 채널] --Events API POST (X-Slack-Signature)--> [터널] --> [n8n Slack Trigger]
     · Signing Secret으로 서명 검증(실패 401) · url_verification 응답 · Slack에 즉시 200
     · Trigger On = message, Watch Whole Workspace = on (채널·봇 필터 없음)
              │ 내부 event 객체 그대로 (event_id, team_id는 여기서 사라짐)
              ▼
[n8n HTTP Request]  POST http://api:8000/jobs · X-API-Key · Retry On Fail 5회/5초
              ▼
[FastAPI POST /jobs]
     1) X-API-Key 상수 시간 비교          → 401
     2) SlackMessageEvent 검증            → 422
     3) ignore_reason(): 채널/subtype/봇/스레드 → 200 {"status":"ignored"}
     4) INSERT … ON CONFLICT DO NOTHING (+SELECT) → 202 accepted | 202 duplicate
              ▼
[PostgreSQL jobs]  ← F2가 여기서 이어받음(F1→F2 계약)
```

### 2.2 요청 (n8n → FastAPI)
본문은 **n8n Slack Trigger가 넘기는 Slack 내부 `event` 객체를 그대로** 보냅니다. n8n에서 필드를 다시 매핑하지 않습니다(소스 확인: `json: req.body.event`).

```http
POST /jobs HTTP/1.1
Host: api:8000
Content-Type: application/json
X-API-Key: <JOBS_API_KEY>
```
```json
{
  "type": "message",
  "channel": "C0TARGET01",
  "user": "U0HUMAN01",
  "text": "결제 API 5xx 급증, NullPointerException",
  "ts": "1794381600.000100",
  "event_ts": "1794381600.000100",
  "channel_type": "channel",
  "client_msg_id": "…",
  "team": "T…",
  "blocks": []
}
```
| 필드 | 필수 | 쓰임 |
|---|---|---|
| `type` | 예(`"message"`만) | 다른 이벤트 타입이면 422 |
| `channel` | 예 | 대상 채널 판정, 중복 키 |
| `ts` | 예(`^\d+\.\d+$`) | 중복 키(채널 안에서 유일, Slack 문서) |
| `user` | 아니오 | 없으면 사람 메시지가 아님(`message_changed`/`message_deleted`에는 없음) |
| `text` | 아니오(기본 `""`) | 저장 |
| `subtype`, `bot_id`, `thread_ts` | 아니오 | 필터 |
| 그 밖의 필드 | - | 무시(Pydantic 기본 `extra="ignore"`) |

### 2.3 응답과 상태 코드
```jsonc
// 202 새 작업
{"status": "accepted", "job_id": "6f1c2a7e-0b7d-4a43-9a52-3c1d0e8f9b21"}
// 202 같은 (channel, ts)가 다시 옴
{"status": "duplicate", "job_id": "6f1c2a7e-0b7d-4a43-9a52-3c1d0e8f9b21"}
// 200 작업 대상이 아님
{"status": "ignored", "reason": "thread_reply"}
// 401
{"detail": "invalid api key"}
```
| 상황 | 코드 | 본문 | n8n 동작 |
|---|---|---|---|
| 새 메시지 | 202 | `accepted` + job_id | 성공 |
| 중복 수신 | 202 | `duplicate` + 기존 job_id | 성공 |
| 필터 제외 | 200 | `ignored` + reason | 성공 |
| `X-API-Key` 없음/불일치 | 401 | `detail` | 오류 → 재시도 후 실패(설정 오류이므로 실패 실행으로 드러남) |
| 본문 검증 실패 | 422 | FastAPI 기본 | 오류 → 재시도 후 실패 |
| `Content-Type` 누락 | 4xx(정확한 코드 **미확인**) | FastAPI 기본 | n8n은 항상 JSON 헤더를 보냄 |
| DB 장애 등 | 500 | - | Retry On Fail로 재시도 |

| `reason` | 조건 | 예 |
|---|---|---|
| `channel_not_targeted` | `channel`이 `SLACK_TARGET_CHANNEL_IDS`에 없음 | 봇이 초대된 다른 채널 |
| `has_subtype` | `subtype`이 있음. 단 `file_share`는 제외(사람의 파일 첨부 메시지는 접수) | `message_changed`, `message_deleted`, `bot_message`, `channel_join`, `thread_broadcast` |
| `not_human` | `bot_id`가 있거나 `user`가 없음 | 앱이 `chat.postMessage`로 쓴 메시지(subtype 없이 `bot_id`) |
| `thread_reply` | `thread_ts`가 있고 `ts`와 다름 | 스레드 답글(Slack 문서: 같으면 부모, 다르면 답글) |

판정 순서는 위 표 순서입니다. 수정·삭제 이벤트는 `user`가 없어도 `has_subtype`으로 기록되도록 subtype을 봇 판정보다 먼저 봅니다.

---

## 3. 설계 결정

### 3.1 필터 위치 → **FastAPI의 순수 함수 `ignore_reason()`**. n8n에는 IF 노드를 두지 않습니다.
- **테스트**: 규칙 4개를 pytest로 red → green 할 수 있습니다. n8n IF 노드는 자동 테스트가 안 됩니다.
- **중복 구현 없음**: 규칙이 한 곳에만 있습니다. n8n Slack Trigger에는 봇·subtype 필터가 없으므로(소스 확인) 어차피 어딘가에 새로 만들어야 합니다.
- **n8n 단순화**: 노드 2개(Trigger → HTTP Request)로 끝납니다.
- **대상 채널도 FastAPI에서**: Slack Trigger의 Channel to Watch는 채널을 **1개만** 받고(소스: `channelId` 단일 값), Slack은 앱당 웹훅 URL을 1개만 허용합니다. 그래서 채널 1개 이상(가정 A1)을 지원하려면 Watch Whole Workspace를 켜고 FastAPI가 `SLACK_TARGET_CHANNEL_IDS`로 거르는 방식이 맞습니다. 봇은 초대된 채널의 이벤트만 받으므로(`channels:history`) 실제 트래픽은 대상 채널 수준입니다.
- **대가**: 봇·수정·삭제 이벤트도 n8n 실행 1건과 FastAPI 호출 1건을 만듭니다. 셀프호스팅이라 실행 과금이 없고, 채널 메시지 수준의 트래픽이라 무시할 만합니다.

### 3.2 중복 제거 키 → **`UNIQUE (channel_id, message_ts)` + `INSERT … ON CONFLICT DO NOTHING RETURNING id`, 행이 없으면 `SELECT`로 기존 id**
- `event_id`는 n8n이 넘기지 않으므로 쓸 수 없습니다. `ts`는 채널 안에서만 유일하므로 채널을 키에 포함합니다.
- 동시에 같은 키가 들어오면, 뒤 요청의 INSERT는 앞 트랜잭션 커밋을 기다렸다가 아무것도 넣지 않고, 이어지는 SELECT(READ COMMITTED, 새 스냅샷)가 커밋된 행을 찾습니다. 그래서 경쟁 상황에서도 1건입니다(단계 4의 동시성 테스트로 확인).
- **중복 응답은 202 + 같은 job_id + `"status": "duplicate"`** 입니다. 재시도한 호출자(n8n)가 첫 호출과 같은 의미("접수됨, 이 job_id")를 받는 멱등 응답이고, 구분은 상태 코드가 아니라 본문 `status`로 합니다.

### 3.3 필터에 걸린 메시지의 응답 → **200 `{"status":"ignored","reason":…}`**
- **2xx여야 합니다.** n8n HTTP Request는 2xx가 아니면 노드 오류를 내고, Retry On Fail은 상태 코드와 상관없이 모든 노드 오류를 재시도합니다(소스 확인). 4xx로 답하면 정상적으로 무시한 이벤트마다 재시도 5회와 실패 실행이 쌓입니다.
- 202가 아니라 200인 이유: 202는 "처리하려고 접수함"이라는 뜻인데, 무시한 이벤트는 처리하지 않습니다. 본문의 `reason`은 n8n 실행 기록과 로그에서 디버깅할 때 씁니다.

### 3.4 인증 → **`X-API-Key` 헤더 + `secrets.compare_digest`(bytes), 실패하면 401**
- 비교는 `secrets.compare_digest(given.encode(), expected.encode())`로 합니다. `str`끼리 비교하면 ASCII가 아닌 문자에서 `TypeError`가 납니다.
- 시크릿은 `.env`의 `JOBS_API_KEY`(예: `openssl rand -hex 32`)이고, n8n에서는 Header Auth 자격 증명(n8n 암호화 저장소)에 같은 값을 넣습니다.
- **빈 키를 금지합니다**(`Field(min_length=1)`). 키가 빈 문자열이면 빈 헤더와 비교해 참이 되어 인증이 뚫립니다.
- Slack Bot Token과 Signing Secret은 n8n 자격 증명에만 있습니다. FastAPI는 Slack 시크릿을 모릅니다.

### 3.5 DB 접근과 스키마 관리 → **psycopg 3(동기) + psycopg_pool, `schema.sql`(`CREATE TABLE IF NOT EXISTS`)을 앱 시작 때 적용. ORM과 마이그레이션 도구는 쓰지 않음**
- 테이블 1개, 쿼리 2개라 ORM이나 Alembic은 코드보다 설정이 더 많습니다.
- psycopg 3을 고른 이유: F3에서 쓸 LangGraph Postgres checkpointer(`langgraph-checkpoint-postgres` 3.2.0)가 `psycopg>=3.2`와 `psycopg-pool`에 의존하므로 드라이버가 하나로 유지됩니다.
- 동기 방식을 고른 이유: FastAPI가 `def` 엔드포인트를 스레드풀에서 돌리므로 충분하고, 테스트에 asyncio 플러그인이 필요 없습니다.
- 트랜잭션은 엔드포인트 안의 `with pool.connection() as conn:` 블록이 끝날 때 커밋됩니다. yield 의존성으로 커밋하면 응답을 보낸 뒤에 커밋될 수 있어 쓰지 않습니다.
- `schema.sql`을 앱 시작 때 적용하는 이유: compose의 `docker-entrypoint-initdb.d`는 빈 볼륨에서만 돌아 이후 변경이 반영되지 않고, 테스트도 같은 함수(`apply_schema`)로 스키마를 만들 수 있습니다.
- **F2 고려(미리 만들지 않음)**: 컬럼·테이블 추가는 같은 파일에 `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`로 이어 쓸 수 있습니다. 이름 변경이나 타입 변경처럼 되돌릴 수 없는 변경이 생기면 그때 마이그레이션 도구를 검토합니다. F1에는 `status` 컬럼도 넣지 않습니다(F2의 상태 전이 범위).

### 3.6 테스트 전략 (seam 목록, TDD 시작 전에 사용자와 확인)
| Seam(공개 경계) | 테스트 파일 | 실제 PostgreSQL | 잡는 것 | 못 잡는 것 |
|---|---|---|---|---|
| `SlackMessageEvent`(요청 스키마) | `test_schemas.py` | 아니오 | 필수 필드, ts 형식, 타입 제한, 수정·삭제 이벤트 수용 | n8n이 실제로 보내는 모양 |
| `ignore_reason()`(순수 함수) | `test_filters.py` | 아니오 | 채널, subtype, 봇, 스레드 규칙 | Slack이 실제로 그 필드를 붙이는지 |
| `Settings`(환경 변수 계약) | `test_config.py` | 아니오 | `.env` 형식(JSON 배열), compose와 공유한 `.env`의 남는 키 허용, 빈 키 거부 | - |
| `JsonFormatter`(로그 한 줄) | `test_logs.py` | 아니오 | JSON 한 줄, job_id 같은 필드 포함 | 수집 시스템 연동 |
| `create_job()` + `jobs` 테이블 | `test_db.py` | **예** | 유일 제약, 멱등 INSERT, 동시 중복 | HTTP |
| HTTP `POST /jobs` + `jobs` 테이블 | `test_api.py` | **예** | 인증, 상태 코드, 중복, 무시, 로그의 job_id | n8n, Slack, 터널 |
| Slack → n8n → FastAPI | 수동(7장) | 예(compose) | 서명 검증, 실제 페이로드, Retry On Fail | - |

- `jobs` 테이블은 F2 워커가 읽을 **F1→F2 계약**이므로, API 테스트에서 행 수를 세는 것은 내부 구현 확인이 아니라 다음 단계와의 계약 확인으로 봅니다.
- 실제 PostgreSQL은 **testcontainers**로 테스트 세션마다 `postgres:18.6-alpine`(compose와 같은 이미지)을 1개 띄웁니다. 세션 fixture라 첫 DB 테스트에서 한 번만 뜨고, 순수 테스트만 실행할 때는 뜨지 않습니다.
- 실행: Docker를 켠 상태에서 `uv run pytest`. Docker 없이 순수 테스트만 돌리려면 `uv run pytest tests/test_schemas.py tests/test_filters.py tests/test_config.py tests/test_logs.py`.
- Docker Desktop이 아닌 런타임(Colima 등)은 testcontainers에 `DOCKER_HOST` 등 설정이 더 필요할 수 있습니다(**미확인**, 환경에 따라 다름).

---

## 4. 디렉터리 구조 (F1에서 생기는 파일만)
```
slack_rag/
├── pyproject.toml
├── uv.lock                  # uv sync가 생성
├── .python-version          # uv python pin 3.13
├── .env.example             # .env는 이것을 복사해 만듦(커밋 금지)
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
├── slack/manifest.yaml
├── n8n/slack-to-jobs.json
├── src/slack_rag/
│   ├── __init__.py          # 빈 파일
│   ├── schemas.py           # SlackMessageEvent
│   ├── filters.py           # ignore_reason
│   ├── config.py            # Settings
│   ├── schema.sql           # jobs 테이블
│   ├── db.py                # apply_schema, create_job
│   ├── logs.py              # JsonFormatter, configure_logging
│   └── main.py              # create_app
└── tests/
    ├── conftest.py
    ├── test_schemas.py
    ├── test_filters.py
    ├── test_config.py
    ├── test_db.py
    ├── test_logs.py
    └── test_api.py
```
git 저장소로 만들 때는 `.env`와 `.venv`를 `.gitignore`에 넣습니다(지금은 git 저장소가 아니라서 만들지 않음).

---

## 5. 구현 단계 (TDD 순서)

공통 규칙: 각 단계의 테스트는 **나열한 순서대로 하나씩** red → green 합니다. 아래 "최소 구현"은 그 단계의 모든 테스트가 통과한 **끝 모습**입니다. 리팩터링은 이 루프에 넣지 않습니다(TDD 스킬 규칙).

순서 근거: 스키마(의존 없음) → 필터(스키마 타입을 입력으로 받음) → 설정 → DB → 로그 → API(앞의 전부를 조립) → 컨테이너 조립. 필터 함수가 `SlackMessageEvent`를 받으므로 스키마를 필터보다 먼저 둡니다.

### 단계 0. 프로젝트 골격 (테스트 없음)
`pyproject.toml`(6.1)과 빈 `src/slack_rag/__init__.py`를 만들고 의존성을 설치합니다.
```bash
uv python pin 3.13
uv sync
uv run pytest        # "no tests ran"(종료 코드 5)이면 정상
```

### 단계 1. 요청 스키마 `SlackMessageEvent`
① 실패 테스트. 먼저 `tests/conftest.py`에 이벤트 팩토리를 둡니다(이후 단계도 씀).
```python
# tests/conftest.py (단계 1 부분)
import pytest


@pytest.fixture
def make_event():
    def _make(**overrides):
        return {
            "type": "message",
            "channel": "C0TARGET01",
            "user": "U0HUMAN01",
            "text": "결제 API 5xx 급증",
            "ts": "1794381600.000100",
            "event_ts": "1794381600.000100",
            "channel_type": "channel",
        } | overrides

    return _make
```
```python
# tests/test_schemas.py
import pytest
from pydantic import ValidationError

from slack_rag.schemas import SlackMessageEvent


def test_parses_human_message_and_ignores_unknown_fields(make_event):
    event = SlackMessageEvent.model_validate(
        make_event(team="T0TEAM0001", blocks=[{"type": "rich_text"}])
    )
    assert (event.channel, event.ts, event.user) == ("C0TARGET01", "1794381600.000100", "U0HUMAN01")
    assert (event.subtype, event.bot_id, event.thread_ts) == (None, None, None)


def test_accepts_message_changed_event_without_user_and_text():
    # Slack 문서의 message_changed 내부 event 예시 모양
    event = SlackMessageEvent.model_validate({
        "type": "message",
        "subtype": "message_changed",
        "hidden": True,
        "channel": "C0TARGET01",
        "ts": "1358878755.000001",
        "message": {"type": "message", "user": "U0HUMAN01", "text": "수정됨"},
    })
    assert event.subtype == "message_changed"
    assert event.user is None


@pytest.mark.parametrize("missing", ["channel", "ts"])
def test_rejects_event_without_dedup_key(make_event, missing):
    payload = make_event()
    del payload[missing]
    with pytest.raises(ValidationError):
        SlackMessageEvent.model_validate(payload)


def test_rejects_malformed_ts(make_event):
    with pytest.raises(ValidationError):
        SlackMessageEvent.model_validate(make_event(ts="not-a-ts"))


def test_rejects_non_message_event(make_event):
    with pytest.raises(ValidationError):
        SlackMessageEvent.model_validate(make_event(type="reaction_added"))
```
② 최소 구현
```python
# src/slack_rag/schemas.py
from typing import Literal

from pydantic import BaseModel, Field


class SlackMessageEvent(BaseModel):
    """n8n Slack Trigger가 그대로 넘기는 Slack 내부 event 객체(message 타입)."""

    type: Literal["message"]
    channel: str = Field(min_length=1)
    ts: str = Field(pattern=r"^\d+\.\d+$")
    user: str | None = None
    text: str = ""
    subtype: str | None = None
    bot_id: str | None = None
    thread_ts: str | None = None
```
③ 검증: `uv run pytest tests/test_schemas.py`

### 단계 2. 필터 `ignore_reason()`
① 실패 테스트
```python
# tests/test_filters.py
import pytest

from slack_rag.filters import ignore_reason
from slack_rag.schemas import SlackMessageEvent

TARGETS = {"C0TARGET01"}


def reason(payload: dict) -> str | None:
    return ignore_reason(SlackMessageEvent.model_validate(payload), TARGETS)


def test_accepts_top_level_human_message(make_event):
    assert reason(make_event()) is None


def test_ignores_message_in_other_channel(make_event):
    assert reason(make_event(channel="C0OTHER001")) == "channel_not_targeted"


@pytest.mark.parametrize(
    "subtype",
    ["message_changed", "message_deleted", "bot_message", "channel_join", "thread_broadcast"],
)
def test_ignores_subtype_other_than_file_share(make_event, subtype):
    assert reason(make_event(subtype=subtype)) == "has_subtype"


def test_accepts_human_message_with_file_attachment(make_event):
    # 사람이 스크린샷 등을 첨부하면 Slack이 subtype: file_share를 붙임 → 접수(1.5)
    assert reason(make_event(subtype="file_share")) is None


def test_ignores_file_share_posted_by_bot(make_event):
    assert reason(make_event(subtype="file_share", bot_id="B0OURBOT01")) == "not_human"


def test_ignores_app_message_with_bot_id(make_event):
    # chat.postMessage로 앱이 올린 메시지: subtype 없이 bot_id와 봇 user가 옴
    assert reason(make_event(bot_id="B0OURBOT01", user="U0BOTUSER1")) == "not_human"


def test_ignores_message_without_user(make_event):
    payload = make_event()
    del payload["user"]
    assert reason(payload) == "not_human"


def test_ignores_thread_reply(make_event):
    assert reason(make_event(ts="1794381700.000200", thread_ts="1794381600.000100")) == "thread_reply"


def test_accepts_thread_parent_whose_thread_ts_equals_ts(make_event):
    assert reason(make_event(thread_ts="1794381600.000100")) is None
```
② 최소 구현
```python
# src/slack_rag/filters.py
from slack_rag.schemas import SlackMessageEvent

ACCEPTED_SUBTYPES = {"file_share"}  # 사람의 파일 첨부 메시지(1.5)


def ignore_reason(event: SlackMessageEvent, target_channel_ids: set[str]) -> str | None:
    """작업으로 접수하지 않을 이벤트면 사유를, 접수할 이벤트면 None을 돌려준다."""
    if event.channel not in target_channel_ids:
        return "channel_not_targeted"
    if event.subtype is not None and event.subtype not in ACCEPTED_SUBTYPES:
        return "has_subtype"
    if event.bot_id is not None or event.user is None:
        return "not_human"
    if event.thread_ts is not None and event.thread_ts != event.ts:
        return "thread_reply"
    return None
```
③ 검증: `uv run pytest tests/test_filters.py`

### 단계 3. 설정 `Settings`
① 실패 테스트
```python
# tests/test_config.py
import pytest
from pydantic import ValidationError

from slack_rag.config import Settings


def test_reads_dotenv_shared_with_compose(tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "JOBS_API_KEY", "SLACK_TARGET_CHANNEL_IDS"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=unrelated\n"
        "DATABASE_URL=postgresql://u:p@localhost:5432/db\n"
        "JOBS_API_KEY=secret\n"
        "SLACK_TARGET_CHANNEL_IDS='[\"C0TARGET01\", \"C0TARGET02\"]'\n"
    )
    settings = Settings(_env_file=env_file)
    assert settings.slack_target_channel_ids == {"C0TARGET01", "C0TARGET02"}


def test_rejects_empty_api_key():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql://u:p@localhost:5432/db",
            jobs_api_key="",
            slack_target_channel_ids={"C0TARGET01"},
        )
```
② 최소 구현. pydantic-settings의 기본 `extra="forbid"`는 `.env`에 필드가 아닌 키(`POSTGRES_PASSWORD` 등)가 있으면 오류를 내므로 `extra="ignore"`가 필요합니다(공식 문서 확인). 목록 타입은 환경 변수 값을 JSON으로 읽습니다.
```python
# src/slack_rag/config.py
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jobs_api_key: str = Field(min_length=1)
    slack_target_channel_ids: set[str] = Field(min_length=1)
```
③ 검증: `uv run pytest tests/test_config.py`

### 단계 4. 저장소 `create_job()`와 `jobs` 테이블 (실제 PostgreSQL)
① 실패 테스트. `conftest.py`에 DB fixture를 더합니다.
```python
# tests/conftest.py (단계 4에서 추가)
import psycopg
from testcontainers.community.postgres import PostgresContainer

from slack_rag.db import apply_schema

POSTGRES_IMAGE = "postgres:18.6-alpine"  # docker-compose.yml과 같은 이미지


@pytest.fixture(scope="session")
def database_url():
    with PostgresContainer(POSTGRES_IMAGE, driver=None) as postgres:
        url = postgres.get_connection_url()  # driver=None → "postgresql://…"
        with psycopg.connect(url, autocommit=True) as conn:
            apply_schema(conn)
        yield url


@pytest.fixture
def conn(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE jobs")
        yield conn
```
```python
# tests/test_db.py
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import psycopg

from slack_rag.db import create_job

JOB = {
    "channel_id": "C0TARGET01",
    "message_ts": "1794381600.000100",
    "user_id": "U0HUMAN01",
    "message_text": "결제 API 5xx 급증",
}


def test_first_message_creates_job(conn):
    job_id, created = create_job(conn, **JOB)
    assert created is True
    assert isinstance(job_id, UUID)


def test_same_channel_and_ts_returns_existing_job(conn):
    first_id, _ = create_job(conn, **JOB)
    second_id, created = create_job(conn, **JOB | {"message_text": "재전송"})
    assert (second_id, created) == (first_id, False)


def test_same_ts_in_other_channel_is_another_job(conn):
    first_id, _ = create_job(conn, **JOB)
    other_id, created = create_job(conn, **JOB | {"channel_id": "C0TARGET02"})
    assert created is True
    assert other_id != first_id


def test_concurrent_duplicates_create_exactly_one_job(database_url, conn):
    def insert(_):
        with psycopg.connect(database_url) as c:  # 블록이 끝날 때 커밋
            return create_job(c, **JOB)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(insert, range(8)))

    assert len({job_id for job_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
```
② 최소 구현
```sql
-- src/slack_rag/schema.sql
CREATE TABLE IF NOT EXISTS jobs (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id   text        NOT NULL,
    message_ts   text        NOT NULL,
    user_id      text        NOT NULL,
    message_text text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_channel_id_message_ts_key UNIQUE (channel_id, message_ts)
);
```
```python
# src/slack_rag/db.py
from pathlib import Path
from uuid import UUID

import psycopg

SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_SQL)


def create_job(
    conn: psycopg.Connection,
    *,
    channel_id: str,
    message_ts: str,
    user_id: str,
    message_text: str,
) -> tuple[UUID, bool]:
    """작업을 만들고 (job_id, 새로 만들었는지)를 돌려준다. 같은 (channel_id, message_ts)면 기존 작업."""
    row = conn.execute(
        """
        INSERT INTO jobs (channel_id, message_ts, user_id, message_text)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (channel_id, message_ts) DO NOTHING
        RETURNING id
        """,
        (channel_id, message_ts, user_id, message_text),
    ).fetchone()
    if row is not None:
        return row[0], True
    row = conn.execute(
        "SELECT id FROM jobs WHERE channel_id = %s AND message_ts = %s",
        (channel_id, message_ts),
    ).fetchone()
    return row[0], False
```
③ 검증: Docker 실행 중에 `uv run pytest tests/test_db.py`

### 단계 5. JSON 로그 `JsonFormatter`
① 실패 테스트
```python
# tests/test_logs.py
import json
import logging
from uuid import UUID

from slack_rag.logs import JsonFormatter


def test_formats_record_as_one_json_line_with_fields():
    record = logging.makeLogRecord({
        "name": "slack_rag.api",
        "levelname": "INFO",
        "msg": "job_accepted",
        "fields": {"job_id": UUID("6f1c2a7e-0b7d-4a43-9a52-3c1d0e8f9b21"), "channel_id": "C0TARGET01"},
    })
    line = JsonFormatter().format(record)

    assert "\n" not in line
    data = json.loads(line)
    assert data["event"] == "job_accepted"
    assert data["level"] == "INFO"
    assert data["job_id"] == "6f1c2a7e-0b7d-4a43-9a52-3c1d0e8f9b21"
    assert data["channel_id"] == "C0TARGET01"
```
② 최소 구현. 로그에는 메시지 본문(`text`)을 넣지 않습니다(개인정보, OQ-26).
```python
# src/slack_rag/logs.py
import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    logger = logging.getLogger("slack_rag")
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
```
③ 검증: `uv run pytest tests/test_logs.py`

### 단계 6. API `POST /jobs` (인증, 202, 중복, 무시, 로그)
① 실패 테스트(순서: 202 → 중복 → 무시 → 401 → 422 → 로그)
```python
# tests/test_api.py
import logging

import pytest
from fastapi.testclient import TestClient

from slack_rag.config import Settings
from slack_rag.main import create_app

API_KEY = "test-api-key"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture
def client(database_url, conn):  # conn이 jobs를 비움
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
    )
    with TestClient(create_app(settings)) as client:  # with로 lifespan(풀, 스키마) 실행
        yield client


def count_jobs(conn) -> int:
    return conn.execute("SELECT count(*) FROM jobs").fetchone()[0]


def test_new_message_returns_202_with_job_id_and_saves_job(client, conn, make_event):
    res = client.post("/jobs", json=make_event(), headers=AUTH)
    assert res.status_code == 202
    assert res.json()["status"] == "accepted"
    assert res.json()["job_id"]
    assert count_jobs(conn) == 1


def test_same_message_again_returns_same_job_without_new_record(client, conn, make_event):
    first = client.post("/jobs", json=make_event(), headers=AUTH).json()
    res = client.post("/jobs", json=make_event(), headers=AUTH)
    assert res.status_code == 202
    assert res.json() == {"status": "duplicate", "job_id": first["job_id"]}
    assert count_jobs(conn) == 1


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"ts": "1794381700.000200", "thread_ts": "1794381600.000100"}, "thread_reply"),
        ({"subtype": "message_changed"}, "has_subtype"),
        ({"subtype": "message_deleted"}, "has_subtype"),
        ({"bot_id": "B0OURBOT01"}, "not_human"),
        ({"channel": "C0OTHER001"}, "channel_not_targeted"),
    ],
)
def test_filtered_event_returns_200_ignored_without_record(client, conn, make_event, overrides, reason):
    res = client.post("/jobs", json=make_event(**overrides), headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"status": "ignored", "reason": reason}
    assert count_jobs(conn) == 0


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key"}])
def test_rejects_missing_or_wrong_api_key(client, conn, make_event, headers):
    res = client.post("/jobs", json=make_event(), headers=headers)
    assert res.status_code == 401
    assert count_jobs(conn) == 0


def test_invalid_payload_returns_422(client, make_event):
    payload = make_event()
    del payload["ts"]
    assert client.post("/jobs", json=payload, headers=AUTH).status_code == 422


def test_accepted_job_is_logged_with_its_job_id(client, make_event, caplog):
    caplog.set_level(logging.INFO, logger="slack_rag")
    job_id = client.post("/jobs", json=make_event(), headers=AUTH).json()["job_id"]
    logged = [getattr(r, "fields", {}) for r in caplog.records]
    assert any(
        str(f.get("job_id")) == job_id and f.get("message_ts") == "1794381600.000100" for f in logged
    )
```
② 최소 구현. 설정과 풀은 `create_app()` 안의 클로저로 넘깁니다(전역 상태 없음, 테스트가 `Settings`를 직접 주입).
```python
# src/slack_rag/main.py
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from psycopg_pool import ConnectionPool

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
        yield
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

        with pool.connection() as conn:  # 블록이 끝날 때 커밋 → 그 뒤에 응답
            job_id, created = create_job(
                conn,
                channel_id=event.channel,
                message_ts=event.ts,
                user_id=event.user,
                message_text=event.text,
            )
        result = "accepted" if created else "duplicate"
        logger.info(f"job_{result}", extra={"fields": {**ref, "job_id": job_id}})
        return {"status": result, "job_id": str(job_id)}

    return app
```
③ 검증: `uv run pytest` (전체, Docker 실행 중)

참고: API 테스트마다 lifespan이 `apply_schema`를 다시 실행하므로 `schema.sql`이 여러 번 적용돼도 안전한지(`IF NOT EXISTS`)가 함께 확인됩니다.

### 단계 7. 컨테이너 조립 (자동 테스트 없음, 명령으로 확인)
6장의 `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`을 만들고 `.env`를 채웁니다(`JOBS_API_KEY`는 `openssl rand -hex 32`, `SLACK_TARGET_CHANNEL_IDS`는 실제 채널 ID를 모르면 임시값).
```bash
docker compose up -d --build postgres api
set -a; source .env; set +a
CH=$(python3 -c "import json,os; print(next(iter(json.loads(os.environ['SLACK_TARGET_CHANNEL_IDS']))))")
EVENT="{\"type\":\"message\",\"channel\":\"$CH\",\"user\":\"U0HUMAN01\",\"text\":\"수동 확인\",\"ts\":\"1794381600.000100\"}"

curl -s -w ' %{http_code}\n' -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' -H "X-API-Key: $JOBS_API_KEY" -d "$EVENT"   # {"status":"accepted",…} 202
curl -s -w ' %{http_code}\n' -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' -H "X-API-Key: $JOBS_API_KEY" -d "$EVENT"   # {"status":"duplicate",같은 job_id} 202
curl -s -w ' %{http_code}\n' -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' -d "$EVENT"                                  # 401
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'SELECT count(*) FROM jobs'   # 1
docker compose logs api | grep '"event": "job_accepted"'                                                  # job_id가 든 JSON 한 줄
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'TRUNCATE jobs'               # 확인 후 정리
```

### 단계 8. Slack 앱과 n8n 연결 (수동, 7장 체크리스트)

---

## 6. 인프라 스니펫

### 6.1 `pyproject.toml`
```toml
[project]
name = "slack-rag"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.162.5",
    "pydantic>=2.14.3",
    "pydantic-settings>=2.16.2",
    "psycopg[binary,pool]>=3.3.8",
    "uvicorn>=0.54.0",
]

[dependency-groups]
dev = [
    "httpx2>=2.16.0",          # Starlette 1.10 TestClient가 요구(httpx 아님)
    "pytest>=9.2.0",
    "testcontainers>=4.15.0",  # postgres 모듈은 extra 없이 포함
]

[build-system]
requires = ["uv_build>=0.13.7,<0.14"]
build-backend = "uv_build"

[tool.pytest.ini_options]
testpaths = ["tests"]
```
`uv_build`는 `src/slack_rag/` 아래 파일(`schema.sql` 포함)을 패키지에 넣습니다. `readme` 필드는 넣지 않습니다(Docker 빌드에 README 복사가 필요해짐).

### 6.2 `Dockerfile`, `.dockerignore`
```dockerfile
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project
COPY src ./src
RUN uv sync --locked
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "slack_rag.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```
```
.venv
.env
```

### 6.3 `docker-compose.yml`
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

  api:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
    ports:
      - "127.0.0.1:8000:8000"
    depends_on:
      postgres:
        condition: service_healthy

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
  n8n_data:
```
- 로컬에 PostgreSQL이 이미 5432를 쓰고 있으면 포트를 바꿉니다.
- n8n의 SSRF 보호(`N8N_SSRF_PROTECTION_ENABLED`)는 기본으로 꺼져 있습니다. 켠다면 `N8N_SSRF_ALLOWED_HOSTNAMES=api`를 함께 넣어야 `http://api:8000`을 부를 수 있습니다(n8n 문서 확인).

### 6.4 `.env.example`
```dotenv
# PostgreSQL (docker compose)
POSTGRES_USER=slack_rag
POSTGRES_PASSWORD=change-me
POSTGRES_DB=slack_rag

# FastAPI
# 로컬에서 uv run으로 띄울 때 쓰는 값. compose의 api 컨테이너는 docker-compose.yml에서 덮어씀
DATABASE_URL=postgresql://slack_rag:change-me@localhost:5432/slack_rag
# openssl rand -hex 32 로 생성. n8n Header Auth 자격 증명의 Value에도 같은 값
JOBS_API_KEY=
# 대상 채널 ID JSON 배열(작은따옴표로 감쌈)
SLACK_TARGET_CHANNEL_IDS='["C0123456789"]'

# n8n: 터널 주소(끝에 / 포함)
N8N_WEBHOOK_URL=https://example.trycloudflare.com/
```
Slack Bot Token과 Signing Secret은 `.env`에 두지 않고 n8n 자격 증명에만 넣습니다.

### 6.5 DB 스키마
5장 단계 4의 `src/slack_rag/schema.sql`이 전부입니다. 앱 시작 때 `apply_schema()`가 적용합니다.

### 6.6 Slack 앱 manifest (`slack/manifest.yaml`)
```yaml
display_information:
  name: Slack RAG
features:
  bot_user:
    display_name: slack-rag
    always_online: false
oauth_config:
  scopes:
    bot:
      - channels:history        # 공개 채널 message.channels 수신(가정 A2)
settings:
  event_subscriptions:          # 처음 만들 때는 이 블록을 빼고 만든다(7장 E3, E6)
    request_url: https://<터널 도메인>/webhook/<webhookId>/webhook
    bot_events:
      - message.channels
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false # n8n 문서: 로테이션을 켜면 12시간마다 토큰 만료, 한번 켜면 못 끔
```
- 필드 이름과 구조는 Slack App Manifest 레퍼런스에서 확인했습니다.
- `chat:write`는 F9(회신)에서 추가합니다.
- n8n Slack Trigger 문서는 `conversations.list`, `users.list`용 스코프가 "최소한" 필요하다고 안내합니다. 그러나 소스(2.47.4)에서 이 두 API는 노드 화면의 채널 목록(Channel to Watch → From List)과 무시할 사용자 목록 옵션에만 쓰입니다. 이 계획은 둘 다 쓰지 않으므로(Watch Whole Workspace, 무시 목록 없음) 스코프를 넣지 않습니다. E2E에서 문제가 생기면 `channels:read`, `users:read`를 추가합니다(**미확인**: 실제 화면 동작).
- Slack API 자격 증명 연결 테스트(`users.profile.get`)는 `invalid_auth`일 때만 실패로 보므로 스코프가 없어도 통과합니다(소스 확인).

### 6.7 n8n 워크플로 (`n8n/slack-to-jobs.json`)
노드 이름, 타입, 버전, 파라미터 이름은 n8n 2.47.4 소스에서 확인했습니다. 다만 이 JSON을 실제로 import해 보지는 않았습니다(**미확인**). import가 안 되면 아래 표대로 화면에서 구성합니다. 자격 증명은 import 뒤에 고릅니다.
```json
{
  "name": "Slack message → FastAPI /jobs",
  "nodes": [
    {
      "id": "d903efda-45ef-4518-ba20-f3206d7e202a",
      "name": "Slack Trigger",
      "type": "n8n-nodes-base.slackTrigger",
      "typeVersion": 1,
      "position": [0, 0],
      "webhookId": "dab0fa5e-fbf1-4ada-91b8-a5dd7a85d0c6",
      "parameters": {
        "trigger": ["message"],
        "watchWorkspace": true,
        "options": {}
      }
    },
    {
      "id": "360cec65-a60e-40c0-a38b-299f593cf75b",
      "name": "POST /jobs",
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 4.5,
      "position": [260, 0],
      "retryOnFail": true,
      "maxTries": 5,
      "waitBetweenTries": 5000,
      "parameters": {
        "method": "POST",
        "url": "http://api:8000/jobs",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "sendBody": true,
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify($json) }}",
        "options": {}
      }
    }
  ],
  "connections": {
    "Slack Trigger": {
      "main": [[{ "node": "POST /jobs", "type": "main", "index": 0 }]]
    }
  },
  "settings": { "executionOrder": "v1" }
}
```
| 노드 | 설정 | 값 | 이유 |
|---|---|---|---|
| Slack Trigger | Credential | Slack API: Access Token = Bot Token(`xoxb-`), **Signature Secret = Signing Secret(필수)** | 없으면 서명 검증을 건너뜀(소스 확인) |
| | Trigger On | New Message Posted to Channel(`message`) | 다른 이벤트 타입은 노드에서 버림 |
| | Watch Whole Workspace | on | 채널 필터는 FastAPI가 함(3.1) |
| | Options | 모두 기본(Resolve IDs off, 무시할 사용자 없음) | 이벤트를 바꾸지 않고 그대로 넘김 |
| POST /jobs (HTTP Request) | Method, URL | POST, `http://api:8000/jobs` | compose 내부망 |
| | Authentication | Generic Credential Type → Header Auth: Name `X-API-Key`, Value = `JOBS_API_KEY` | 3.4 |
| | Send Body | JSON, Using JSON, `={{ JSON.stringify($json) }}` | 트리거 출력(event) 그대로 |
| | Options → Timeout | 기본 10000ms 유지 | FastAPI는 DB INSERT만 함 |
| | Settings → Retry On Fail | on, Max Tries 5, Wait Between Tries 5000ms | n8n 허용 최댓값. Slack은 n8n에게서 이미 200을 받았으므로 재시도하지 않음 → 이 재시도가 유일한 안전망(약 20초 + 타임아웃) |
| | Settings → On Error | Stop Workflow(기본) | 끝내 실패하면 실행 목록에 실패로 남음 |

---

## 7. 수동 E2E 체크리스트

### 7.1 준비 (사용자)
- [ ] **E1 터널**: `cloudflared tunnel --url http://localhost:5678` 실행 → 출력된 `https://<무작위>.trycloudflare.com`을 `.env`의 `N8N_WEBHOOK_URL`에 넣습니다(끝에 `/`). Quick Tunnel 주소는 실행할 때마다 바뀌므로, 바뀌면 `N8N_WEBHOOK_URL`, n8n 재시작, Slack Request URL을 모두 다시 맞춥니다. ngrok(`ngrok http 5678`)도 됩니다(고정 도메인 옵션은 **미확인**).
- [ ] **E2 기동**: `docker compose up -d --build` → `http://localhost:5678`에서 n8n 소유자 계정을 만듭니다.
- [ ] **E3 Slack 앱 생성**: https://api.slack.com/apps → Create New App → From a manifest → 워크스페이스 선택 → 6.6 manifest에서 **`settings.event_subscriptions` 블록을 뺀 것**을 붙여 넣고 생성 → Install to Workspace. OAuth & Permissions의 Bot User OAuth Token(`xoxb-`)과 Basic Information → App Credentials의 Signing Secret을 복사합니다. (이벤트 구독을 처음부터 넣으면 n8n이 준비되기 전에 Slack이 URL 검증을 시도하므로 나중에 넣습니다.)
- [ ] **E4 n8n 자격 증명**: Slack API(Access Token, Signature Secret), Header Auth(Name `X-API-Key`, Value `JOBS_API_KEY`).
- [ ] **E5 워크플로**: Import from File로 `n8n/slack-to-jobs.json`을 가져와 두 노드에 자격 증명을 고르고 저장 → **Publish**. Slack Trigger 노드의 **Production URL**을 복사합니다(`https://<터널>/webhook/<id>/webhook` 형태).
- [ ] **E6 이벤트 구독**: Slack 앱 설정 → Event Subscriptions → Enable → Request URL에 Production URL → "Verified" 확인 → Subscribe to bot events에 `message.channels` 추가 → Save Changes. (테스트 URL과 운영 URL을 동시에 등록할 수 없습니다. 앱당 웹훅 1개.)
- [ ] **E7 채널**: 대상 채널 이름 → 채널 정보 하단의 Channel ID를 `.env`의 `SLACK_TARGET_CHANNEL_IDS`에 넣고 `docker compose up -d api`(재생성으로 반영). 그다음 채널에서 `/invite @slack-rag`.

### 7.2 확인 (각 항목: n8n Executions, `docker compose logs api`, `psql`로 확인)
`psql` 명령: `docker compose exec postgres psql -U slack_rag -d slack_rag -c 'SELECT id, channel_id, message_ts, created_at FROM jobs ORDER BY created_at'`

| # | 행동 | 기대 결과 |
|---|---|---|
| 1 | 봇 초대 직후(E7) | `channel_join` 이벤트가 오면 200 `ignored`/`has_subtype`, jobs 0건 |
| 2 | 대상 채널에 새 메시지 | n8n 실행 성공, POST /jobs 출력 `{"status":"accepted","job_id":…}`, 로그 `job_accepted`에 같은 job_id, jobs 1건 |
| 3 | 2번 메시지에 스레드 답글 | `ignored`/`thread_reply`, jobs 증가 없음. Slack Trigger 출력에 `thread_ts`가 있는지도 확인(공식 이벤트 문서에 명시 문구 **미확인**) |
| 4 | 2번 메시지 수정 | `ignored`/`has_subtype`(로그 `subtype: message_changed`) |
| 5 | 2번 메시지 삭제 | `ignored`/`has_subtype`(로그 `subtype: message_deleted`) |
| 6 | 봇 메시지: Slack Workflow Builder로 대상 채널에 메시지를 보내는 워크플로 실행(또는 이미 있는 다른 앱의 메시지) | `ignored`/`has_subtype` 또는 `not_human`. 쓸 수 있는 봇이 없으면 자동 테스트로 갈음하고 F9에서 자기 봇 답글로 다시 확인 |
| 7 | 중복: 2번 실행의 Slack Trigger 출력 JSON을 복사해 단계 7의 curl로 두 번 POST | 둘 다 202 `duplicate` + 2번과 같은 job_id, jobs 그대로 |
| 8 | Retry On Fail: `docker compose stop api` → 메시지 작성 → 10초 안에 `docker compose start api` | 실행이 재시도 끝에 성공, jobs 1건 증가. 20초 넘게 멈춰 두면 실행이 실패로 남고 그 이벤트는 유실됨(대조는 범위 밖) |
| 9 | 서명 검증: `curl -i -X POST <Production URL> -H 'Content-Type: application/json' -d '{"type":"event_callback","event":{"type":"message"}}'` | n8n이 401 Unauthorized, n8n 실행과 FastAPI 로그 없음 |
| 10 | (선택) 봇이 초대된 비대상 채널에 메시지 | `ignored`/`channel_not_targeted` |
| 11 | 대상 채널에 스크린샷을 첨부한 새 메시지 | `accepted`, jobs 1건 증가. Slack Trigger 출력에 `subtype: file_share`가 있는지 확인 |

n8n 실행 기록에는 메시지 본문이 남습니다. 보존 기간은 OQ-26에서 정합니다.

---

## 8. 완료 기준과 범위 밖

### 8.1 완료 기준
- [ ] `uv run pytest` 전체 통과(Docker 실행 중).
- [ ] **메시지 → 작업 기록 + 202**: `test_api.py::test_new_message_returns_202_with_job_id_and_saves_job`, 단계 7 curl, E2E #2.
- [ ] **같은 요청을 다시 보내도 새 작업 없음**: `test_api.py::test_same_message_again_returns_same_job_without_new_record`, `test_db.py::test_concurrent_duplicates_create_exactly_one_job`, E2E #7.
- [ ] **스레드 답글, 수정, 삭제, 봇 메시지는 작업 없음**: `test_filters.py` 전체, `test_api.py::test_filtered_event_returns_200_ignored_without_record`, E2E #3~#6.
- [ ] **파일을 첨부한 사람 메시지는 작업 접수**: `test_filters.py::test_accepts_human_message_with_file_attachment`, E2E #11.
- [ ] **n8n 호출 인증**: `test_api.py::test_rejects_missing_or_wrong_api_key`, 단계 7의 401 curl.
- [ ] **Slack 서명 검증(n8n Signing Secret)**: E2E #9.
- [ ] **Retry On Fail**: E2E #8.
- [ ] **job_id 상관 ID와 구조화 로그**: `test_logs.py`, `test_api.py::test_accepted_job_is_logged_with_its_job_id`, `docker compose logs api`.

### 8.2 범위 밖 (다음 단계로 넘김)
| 항목 | 넘기는 곳 |
|---|---|
| 큐 적재, `status` 컬럼과 상태 전이, DB 기록 후 큐 적재 실패(이중 쓰기) 처리 | F2(OQ-08, OQ-09) |
| 끝내 실패한 n8n 실행의 유실 대비: 에러 워크플로(관리자 알림), `conversations.history` 대조 | 별도 결정 필요(OQ-07 확장) |
| 접수 표시(이모지), 결과 회신, 승인 버튼(Interactivity), `chat:write` 스코프 | F9(OQ-14, OQ-15) |
| 첨부 파일 내용 처리(다운로드, 이미지 분석, `files:read` 스코프) | 필요해지는 단계(F3 이후). F1은 `file_share` 메시지의 본문만 접수(1.5) |
| 비공개 채널(`groups:history`, `message.groups`) | 가정 A2가 바뀔 때 |
| 페이로드 크기 제한, rate limit, 메트릭·트레이싱 | 필요해지는 단계(OQ-23) |
| 되돌릴 수 없는 스키마 변경용 마이그레이션 도구 | 그런 변경이 처음 생기는 단계 |
| pgvector 이미지 | F4(OQ-18) |
| 운영 배포(고정 도메인, 시크릿 매니저) | OQ-19 |
| 작업·실행 기록 보존 기간 | OQ-26 |

### 8.3 미확인 항목 모음
1. n8n 워크플로 JSON의 실제 import 동작(파라미터 이름은 소스로 확인).
2. Slack 공식 이벤트 문서에 스레드 답글 이벤트가 `thread_ts`를 포함한다는 명시 문구(부모/답글 판별 규칙은 Slack 문서로 확인). E2E #3에서 확인.
3. `channels:read`, `users:read` 없이 n8n 노드 화면이 문제없이 동작하는지(소스상 쓰지 않음). E2E E5에서 확인.
4. FastAPI가 `Content-Type` 누락 JSON 요청에 돌려주는 정확한 상태 코드.
5. ngrok 고정 도메인 옵션 이름.
6. Workflow Builder 메시지의 정확한 형태(`bot_id`/`subtype`). 어느 쪽이든 필터에 걸림.
7. Docker Desktop이 아닌 런타임에서 testcontainers 추가 설정.
