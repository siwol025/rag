# 외부 API / 기술 스펙 상세 분석

- 조사 기준일: **2026-11-11**. 모든 항목은 이 날짜에 공식 문서(1차 출처)를 직접 열어 확인했습니다. 기억에 의존한 내용은 넣지 않았습니다.
- 상위 문서: [plan-architecture.md](./plan-architecture.md) (이 문서는 그 문서의 8장 상세입니다)
- 표기 규칙
  - `[조사]`: 1차 출처에서 확인한 사실입니다. 바로 뒤에 출처 링크를 붙였습니다.
  - `[해석]`: 확인한 사실을 바탕으로 한 추론이나 이 프로젝트에 대한 시사점입니다.
  - `[미확인]`: 확인하지 못했습니다. 추측으로 채우지 않았습니다.
  - `PLAN.md:N`: 기획서 N번째 줄입니다.

---

## 8.1 Slack

### 8.1.1 Events API (HTTP Request URL 방식)

| 항목 | 내용 | 출처 |
|---|---|---|
| 응답 시한 | `[조사]` 이벤트를 받으면 **3초 안에** HTTP 2xx를 반환해야 합니다. 넘기면 전달 실패로 처리됩니다. | [Events API](https://docs.slack.dev/apis/events-api/) |
| 재시도 | `[조사]` 실패하면 **최대 3회** 재시도합니다. 1회차는 거의 즉시, 2회차는 1분 뒤, 3회차는 5분 뒤입니다(지수 백오프). 앱 설정에서 Delayed Events를 켜면 이후 24시간 동안 매시간 추가로 재시도합니다. 기본 설정에서는 2시간 넘게 늦은 이벤트를 전달하지 않습니다. | 같은 문서 |
| 재시도 헤더 | `[조사]` `x-slack-retry-num`은 `1`/`2`/`3`, `x-slack-retry-reason`은 `http_timeout`, `too_many_redirects`, `connection_failed`, `ssl_error`, `http_error`, `unknown_error` 중 하나입니다. | 같은 문서 |
| 재시도 중단 | `[조사]` 200이 아닌 응답에 `x-slack-no-retry: 1` 헤더를 넣으면 해당 이벤트만 재전송하지 않습니다. | 같은 문서 |
| 외부 래퍼 필드 | `[조사]` `type`(`event_callback`), `token`(폐기됨, 서명 검증으로 대체), `team_id`, `api_app_id`, `event`(내부 이벤트), `event_id`(워크스페이스 전체에서 전역 유일), `event_time`(epoch 초), `event_context`, `authorizations`, `is_ext_shared_channel`, `context_team_id`, `context_enterprise_id` | 같은 문서 |
| 전달량 제한 | `[조사]` 워크스페이스·앱마다 **60분당 30,000건**까지 전달합니다. 넘으면 `app_rate_limited` 이벤트가 분 단위로 옵니다. | 같은 문서, [Rate limits](https://docs.slack.dev/apis/web-api/rate-limits/) |
| 자동 비활성화 | `[조사]` 60분 동안 전달 시도의 **95% 넘게** 실패하면(SSL 오류, 3초 초과, 리디렉션 2회 초과, 2xx 아님) 구독이 임시로 비활성화되고 앱 소유자에게 메일이 갑니다. 시간당 1,000건 미만인 앱은 자동 비활성화 대상이 아닙니다. | [Events API](https://docs.slack.dev/apis/events-api/) |

**URL verification**
- `[조사]` 페이로드는 `{"type":"url_verification","challenge":"...","token":"..."}` 형태입니다(token은 무시해도 됨). 요청의 진위를 확인한 뒤 HTTP 200으로 `challenge` 값을 돌려줍니다. text/plain 본문, form-encoded `challenge=...`, JSON `{"challenge":"..."}` 중 어느 형식이든 받아들입니다. ([url_verification](https://docs.slack.dev/reference/events/url_verification/))

**메시지 이벤트**
- `[조사]` `message.channels`(공개 채널)에는 `channels:history` 스코프가 필요합니다. 내부 `event` 필드는 `type`(`message`), `channel`, `user`, `text`, `ts`, `event_ts`, `channel_type`(`channel`)입니다. ([message.channels](https://docs.slack.dev/reference/events/message.channels/))
- `[조사]` 대화 유형별 이벤트와 스코프: `message.groups`는 비공개 채널(`groups:history`), `message.im`은 DM(`im:history`), `message.mpim`은 그룹 DM(`mpim:history`)입니다. 서브타입에는 `bot_message`, `message_changed`, `message_deleted`, `thread_broadcast`, `message_replied`, `channel_join`, `channel_leave`, `file_share` 등이 있고, `message_deleted` 같은 일부는 `hidden: true`입니다. 수정된 메시지에는 `edited` 속성이 붙고 원문은 제공되지 않습니다. ([message](https://docs.slack.dev/reference/events/message/))
- `[조사]` `channels:history`는 "앱이 **추가된** 공개 채널의 메시지를 보는" 스코프입니다. 따라서 봇을 대상 채널에 초대해야 이벤트를 받습니다. ([channels:history](https://docs.slack.dev/reference/scopes/channels.history/))
- `[조사]` `app_mention` 이벤트에는 `app_mentions:read` 스코프가 필요합니다. 페이로드 필드는 `user`, `text`, `ts`, `channel`, `event_ts`입니다. ([app_mention](https://docs.slack.dev/reference/events/app_mention/))
- `[미확인]` 스레드 답글의 `thread_ts`/`parent_user_id` 필드 정의, `client_msg_id` 필드가 항상 존재하는지는 공식 페이지에서 확인하지 못했습니다.

**봇 메시지 루프 방지**
- `[조사]` granular bot permission 앱(현행 앱)에서는 페이로드에 `bot_id`와 `bot_profile`이 있는지로 봇 메시지를 판별합니다. `bot_id`는 메시지를 보낸 봇을 식별합니다. ([bot_message](https://docs.slack.dev/reference/events/message/bot_message/))
- `[조사]` Bolt for Python의 `ignoring_self_events` 미들웨어는 이벤트의 `bot_id`가 앱 자신의 `bot_id`와 같거나 user ID가 봇 user ID와 같으면 이벤트를 버립니다. ([Bolt ignoring_self_events](https://docs.slack.dev/tools/bolt-python/reference/middleware/ignoring_self_events/index.html))
- `[해석]` 이 시스템은 같은 채널(스레드)에 답글을 쓰므로, 필터가 없으면 자기 메시지에 다시 반응하는 무한 루프가 생길 수 있습니다. 최소한 다음을 걸러야 합니다. (1) `bot_id`가 있는 메시지, (2) `subtype`이 있는 메시지(`message_changed`, `message_deleted`, `channel_join` 등. 단 `file_share`를 처리할지는 결정 필요), (3) 스레드 답글(`thread_ts`가 있고 `ts`와 다른 경우)을 새 요청으로 볼지 여부.

### 8.1.2 요청 서명 검증
- `[조사]` 절차는 다음과 같습니다. ([Verifying requests](https://docs.slack.dev/authentication/verifying-requests-from-slack/))
  1. Signing Secret을 준비합니다(UTF-8 문자열).
  2. JSON 파싱 **전의 원본 본문(raw body)** 을 읽습니다.
  3. `X-Slack-Request-Timestamp`가 서버 시각과 **5분 넘게** 차이 나면 거부합니다(재전송 공격 방지).
  4. basestring `v0:<timestamp>:<raw body>`를 만듭니다.
  5. Signing Secret을 키로 HMAC-SHA256을 계산하고 hex 앞에 `v0=`을 붙입니다.
  6. `X-Slack-Signature` 헤더와 **상수 시간 비교**를 합니다. 헤더 이름은 대소문자를 구분하지 않습니다.
- `[조사]` verification token 방식은 폐기되었고 Signing Secret만 쓰도록 권고합니다.
- `[해석]` Interactivity 요청(버튼 클릭)도 Slack이 보내는 요청이므로 같은 방식으로 검증해야 합니다. 다만 interactivity 문서 페이지에는 서명 검증이 명시돼 있지 않았습니다(`[미확인]`: 해당 페이지의 명시 문구).

### 8.1.3 Socket Mode 비교
| 항목 | HTTP Request URL | Socket Mode | 출처 |
|---|---|---|---|
| 연결 | Slack이 공개 URL로 POST | `[조사]` 앱 레벨 토큰(`xapp-`)으로 `apps.connections.open`을 호출해 받은 WebSocket URL에 연결합니다. URL은 주기적으로 갱신됩니다. | [Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/) |
| 공개 엔드포인트 | 필요 | 불필요. 방화벽 안에서 운영할 수 있습니다. | 같은 문서 |
| ack | HTTP 2xx (3초) | `[조사]` 각 이벤트의 `envelope_id`를 돌려보내 ack합니다. `[미확인]` Socket Mode ack 시한이 3초인지는 문서에서 확인하지 못했습니다. | 같은 문서 |
| 동시 연결 | - | `[조사]` 최대 10개. 연결이 여러 개면 페이로드가 아무 연결로나 갈 수 있습니다. | 같은 문서 |
| 제약 | - | `[조사]` 공개 Slack Marketplace에 올릴 수 없습니다. granular permission 앱 전용입니다. 재연결을 직접 처리해야 하고(끊기기 약 10초 전에 경고, `refresh_requested`), 모드를 전환하는 동안 이벤트를 잃을 수 있습니다. | 같은 문서 |
| 이 프로젝트 적합성 | `[해석]` n8n Slack Trigger는 웹훅 노드(`webhook()` 메서드, `responseMode: 'onReceived'`)이므로 HTTP 방식을 씁니다(8.2절 소스 확인). 기획대로 n8n을 쓰면 HTTP 방식이 됩니다. | Socket Mode는 n8n 없이 Worker나 별도 수신기가 직접 연결하는 구조에서만 의미가 있습니다. | [SlackTrigger 소스](https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Slack/SlackTrigger.node.ts) |

### 8.1.4 `chat.postMessage` (결과 회신)
- `[조사]` 스코프는 `chat:write`입니다. 봇이 참여하지 않은 공개 채널에 쓰려면 `chat:write.public`, 사용자 이름이나 아이콘을 바꾸려면 `chat:write.customize`가 필요합니다. ([chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/))
- `[조사]` `thread_ts`를 주면 답글이 됩니다. 이때 답글의 ts가 아니라 **부모 메시지의 `ts`** 를 써야 합니다. `reply_broadcast`는 채널에도 함께 보이게 하는 옵션이며 아껴 쓰라고 안내합니다.
- `[조사]` `blocks`를 쓸 때는 알림용 대체 텍스트로 `text`를 함께 넣습니다. `markdown_text`는 `blocks`/`text`와 함께 쓸 수 없습니다.
- `[조사]` 길이: `text`는 약 4,000자 이하를 권장합니다. 40,000자를 넘으면 잘리고 `message_truncated` 경고가 붙습니다. 긴 내용은 파일 업로드를 권장합니다.
- `[조사]` 속도: 채널당 대략 **초당 1건**이고 짧은 버스트는 허용됩니다. 워크스페이스 전체로는 분당 수백 건이 상한입니다. 초과하면 HTTP 429와 `Retry-After` 헤더가 옵니다. ([Rate limits](https://docs.slack.dev/apis/web-api/rate-limits/))
- `[조사]` 응답 필드는 `ok`, `channel`, `ts`, `message`입니다. 주요 오류는 `not_in_channel`, `channel_not_found`, `ratelimited`, `rate_limited`, `msg_blocks_too_long`입니다.
- `[조사]` `metadata` 인자(`event_type`, `event_payload`)로 메시지에 구조화 데이터를 붙일 수 있습니다. 단, 메시지에 접근할 수 있는 워크스페이스 구성원 누구나 이 값을 볼 수 있습니다.
- `[해석]` diff 전문은 4,000자를 쉽게 넘습니다. 그래서 Slack에는 요약(변경 파일, 함수, 테스트 결과)만 올리고 전문은 PR 링크나 파일로 넘기는 방식이 현실적입니다. 진행 상황을 여러 번 알리면 채널당 초당 1건 제한에 걸릴 수 있으니 업데이트 횟수를 줄여야 합니다. `chat.update`로 한 메시지를 고쳐 쓰는 방식도 고려할 만합니다(`[미확인]`: `chat.update` 스펙은 이번에 확인하지 않음).

### 8.1.5 Block Kit 버튼 + Interactivity (사람 승인)
- `[조사]` 버튼 요소의 필드는 `type: "button"`, `text`(plain_text, 최대 75자), `action_id`(최대 255자, 블록 안에서 유일), `value`(최대 2,000자), `url`(최대 3,000자), `style`(`primary`/`danger`), `confirm`(확인 다이얼로그), `accessibility_label`입니다. 버튼은 `section`의 accessory나 `actions` 블록 안에 둡니다. 최근에는 `visible_to_user_ids`(버튼을 볼 수 있는 사용자 제한)와 `agent_prompt` 필드도 추가되었습니다. ([Button element](https://docs.slack.dev/reference/block-kit/block-elements/button-element/))
- `[조사]` 인터랙션 페이로드는 앱의 Interactivity Request URL로 `application/x-www-form-urlencoded` POST가 오고, `payload` 파라미터에 JSON이 들어 있습니다. **3초 안에 HTTP 200**을 반환해야 합니다. `response_url`은 페이로드마다 고유하며 **30분 안에 최대 5회**까지 쓸 수 있습니다. `trigger_id`는 3초 뒤 만료되고 한 번만 쓸 수 있습니다. ([Handling user interaction](https://docs.slack.dev/interactivity/handling-user-interaction/))
- `[조사]` `block_actions` 페이로드의 주요 필드는 `type`, `trigger_id`, `response_url`, `team`, `user{id,username,team_id}`, `api_app_id`, `container{type,message_ts,channel_id,...}`, `channel`, `message`, `state`, `actions[]{action_id, block_id, value, type, action_ts}`입니다. ([block_actions payload](https://docs.slack.dev/reference/interaction-payloads/block_actions-payload/))
- `[해석]` 승인 처리 시 주의할 점
  - 버튼 `value`에는 `job_id` 같은 식별자만 넣고, 승인 여부는 서버가 `user.id`를 승인자 허용 목록과 대조해 판단합니다. `value`는 클라이언트가 보내는 데이터라 신뢰할 수 없습니다.
  - `visible_to_user_ids`는 UI에서만 숨기는 기능이므로 서버 측 권한 검사를 대신할 수 없습니다.
  - 버튼을 두 번 누르거나 여러 사람이 동시에 누를 수 있으므로, 상태 전이(`awaiting_approval` → `approved`)를 원자적으로 처리해 한 번만 반영되게 합니다.

### 8.1.6 OAuth 스코프 (예상 최소 집합)
| 스코프 | 용도 | 확인 |
|---|---|---|
| `channels:history` | 공개 채널 `message.channels` 이벤트 수신, `conversations.history/replies` 호출 | `[조사]` [scope](https://docs.slack.dev/reference/scopes/channels.history/) |
| `groups:history` | 대상이 비공개 채널일 때 | `[조사]` [message](https://docs.slack.dev/reference/events/message/) |
| `chat:write` | 결과·승인 요청 게시 | `[조사]` [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/) |
| `app_mentions:read` | 멘션을 트리거로 쓸 때 | `[조사]` [app_mention](https://docs.slack.dev/reference/events/app_mention/) |
| `reactions:write` | 접수 표시(예: 눈 이모지)를 달 때(선택) | `[조사]` [reactions.add](https://docs.slack.dev/reference/methods/reactions.add/) (Tier 3) |
| n8n이 요구하는 추가 스코프 | n8n Slack Trigger 문서는 최소한 `conversations.list`, `users.list`용 스코프가 필요하다고 안내합니다. | `[조사]` [n8n Slack Trigger](https://docs.n8n.io/integrations/builtin/trigger-nodes/n8n-nodes-base.slacktrigger/). 정확한 스코프 이름(`channels:read`, `users:read` 추정)은 `[미확인]` |

### 8.1.7 Web API rate limit (RAG에 Slack 히스토리를 넣을 경우)
- `[조사]` 등급별 한도는 Tier 1이 분당 1회 이상, Tier 2가 20회 이상, Tier 3가 50회 이상, Tier 4가 100회 이상입니다(메서드·워크스페이스·앱 단위). ([Rate limits](https://docs.slack.dev/apis/web-api/rate-limits/))
- `[조사]` 2025-05-29 변경으로, **Marketplace 밖에서 상업 배포되는 앱**의 신규 설치는 `conversations.history`/`conversations.replies`가 **분당 1회, `limit` 최대·기본 15건**으로 제한됩니다. **사내에서 직접 만든 내부 앱(internal customer-built apps)은 영향을 받지 않습니다.** ([Changelog 2025-05-29](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps))
- `[해석]` 이 서비스를 사내 전용 앱으로 만들면 Slack 히스토리 수집에 Tier 3 수준 한도를 쓸 수 있습니다. 외부 배포를 고려한다면 히스토리 수집량이 크게 제한됩니다. 이것도 결정 사항에 영향을 줍니다.

---

## 8.2 n8n

- `[조사]` 현재 버전: npm `n8n` stable/latest가 **2.47.3**입니다(2026-11-11 레지스트리 조회, https://registry.npmjs.org/n8n).

### 8.2.1 Slack Trigger 노드
- `[조사]` 지원 이벤트는 Any Event, App Home Opened, Bot/App Mention, File Made Public, File Shared, New Message Posted to Channel, New Public Channel Created, New User, Reaction Added입니다. 파라미터로 Watch Whole Workspace(기본 off), Channel to Watch, Download Files가 있고, 옵션으로 Resolve IDs, Usernames or IDs to ignore, Emoji Names to Filter가 있습니다. ([n8n Slack Trigger](https://docs.n8n.io/integrations/builtin/trigger-nodes/n8n-nodes-base.slacktrigger/))
- `[조사]` Signing Secret은 "선택이지만 권장"이며 n8n **1.106.0**부터 지원합니다. 문서에는 다음 주의 사항도 있습니다.
  - "Slack은 앱당 웹훅을 하나만 등록할 수 있다." 그래서 테스트 URL과 운영 URL을 동시에 쓸 수 없습니다.
  - 토큰 로테이션을 켜면 토큰이 12시간마다 만료됩니다. 운영 환경에서는 로테이션을 꺼야 하고, 한 번 켜면 다시 끌 수 없습니다.
  - Watch Whole Workspace를 켜면 이벤트마다 실행이 1건씩 소모됩니다.
- `[조사]` **소스 코드 직접 확인** 결과입니다. ([SlackTrigger.node.ts](https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Slack/SlackTrigger.node.ts), [SlackTriggerHelpers.ts](https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Slack/SlackTriggerHelpers.ts), [webhook-signature-verification.ts](https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/utils/webhook-signature-verification.ts))
  - 웹훅 설정이 `responseMode: 'onReceived'`이므로 **워크플로를 끝까지 실행하기 전에 Slack에 응답**합니다. Slack의 3초 제한은 지켜집니다.
  - 처리 순서는 서명 검증 → `url_verification`이면 challenge 응답 → 이벤트 타입 필터 → 채널 필터 → 무시할 사용자 목록 → (리액션 이모지 필터) → ID 해석 → 파일 다운로드입니다.
  - **Signing Secret을 설정하지 않으면 서명 검증을 건너뜁니다**(`skipIfNoExpectedSignature: !signatureSecret`). 타임스탬프 허용 범위는 300초입니다.
  - **`bot_id`나 `subtype`으로 거르는 로직이 없습니다.** 봇 메시지는 무시 목록에 user ID를 넣었을 때만 빠집니다.
  - **`event_id` 기반 중복 제거가 없습니다.**
  - 다음 노드로 넘기는 데이터는 `json: req.body.event`, 즉 **내부 `event` 객체뿐**입니다. 바깥 래퍼의 `event_id`, `team_id`, `event_time`, `authorizations`는 전달되지 않습니다.
- `[해석]` 이 프로젝트에 주는 시사점
  1. n8n에 Signing Secret을 반드시 설정해야 합니다. 설정하지 않으면 위조 요청을 막지 못합니다.
  2. 봇 메시지 필터(`bot_id`, `subtype`)를 n8n IF 노드나 FastAPI에서 직접 구현해야 합니다.
  3. `event_id`가 사라지므로 **중복 제거 키는 `(channel, ts)`** 처럼 내부 이벤트 필드로 만들어야 합니다. 그렇지 않으면 Slack Trigger 대신 일반 Webhook 노드로 원본 본문을 받아야 합니다.
  4. n8n이 먼저 Slack에 200을 보내므로, 이후 n8n에서 FastAPI로 가는 호출이 실패하면 **Slack은 재시도하지 않습니다.** 이벤트가 그대로 유실될 수 있으니 n8n 쪽 재시도와 에러 워크플로가 필요합니다.

### 8.2.2 Webhook 노드
- `[조사]` 메서드는 DELETE, GET, HEAD, PATCH, POST, PUT입니다. 인증 방식은 Basic, Header, JWT, n8n User Auth, None입니다. 응답 방식은 Immediately / When Last Node Finishes / Using 'Respond to Webhook' Node / Streaming 중에서 고릅니다. 옵션으로 Raw Body와 IP(s) Allowlist(그 외 IP는 403)가 있습니다. 최대 페이로드는 16MB이며 셀프호스팅이면 `N8N_PAYLOAD_SIZE_MAX`로 바꿀 수 있습니다. 테스트 URL과 운영 URL이 따로 있고, 운영 URL은 워크플로를 publish하면 등록됩니다. ([n8n Webhook](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/))
- `[해석]` Webhook 노드의 내장 인증에는 Slack 서명 방식(HMAC)이 없습니다. Slack Interactivity를 n8n Webhook으로 받으려면 Code 노드 등으로 HMAC 검증을 직접 구현해야 합니다(`[미확인]`: n8n에 Slack 서명 검증 전용 옵션이 있는지).

### 8.2.3 HTTP Request 노드 / 노드 공통 설정
- `[조사]` 메서드, URL, 인증(Predefined Credential Type 권장, 그 외 Header Auth 같은 Generic), Query/Headers/Body(JSON)를 설정합니다. 옵션은 Batching, Ignore SSL Issues(기본 off), Response(기본은 2xx일 때만 성공, Never Error 선택 가능), Pagination, Timeout(ms), Redirects입니다. ([n8n HTTP Request](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/))
- `[조사]` 노드 Settings 탭에는 Always Output Data, Execute Once, **Retry On Fail**, On Error(Stop Workflow / Continue / Continue using error output)가 있습니다. ([Node settings](https://docs.n8n.io/workflows/components/nodes/))
- `[미확인]` Retry On Fail의 Max Tries와 Wait Between Tries 기본값, HTTP Request Timeout 기본값은 문서에서 확인하지 못했습니다.

### 8.2.4 Slack 노드: Send and Wait for Response
- `[조사]` "메시지를 보내고 수신자 응답을 기다린 뒤 진행"하는 오퍼레이션이며, 승인자가 Slack 안에서 바로 응답할 수 있습니다. 내부적으로 `chat.postMessage`를 씁니다. ([n8n Slack node](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.slack/))
- `[미확인]` 승인 타입(Approve only, Approve/Disapprove), 대기 시간 제한, 재개 메커니즘의 세부 사항은 별도 페이지("Approvals in Slack")에 있어 확인하지 못했습니다.
- `[해석]` 이 기능으로 승인을 받으면 승인 상태가 n8n 실행 안에 머무릅니다. 그러면 LangGraph `interrupt` 상태와 따로 관리해야 하는 이중 상태가 생깁니다. 승인 판단을 LangGraph 그래프 안에 둘지 n8n에 둘지 정해야 합니다(메인 문서 OQ 참고).

### 8.2.5 n8n 확장(Queue mode)
- `[조사]` main 인스턴스는 타이머와 웹훅을 받아 실행을 만들고, 실행 ID를 Redis 큐에 넣습니다. worker가 이를 꺼내 실행합니다. webhook processor는 선택 사항입니다. main과 모든 worker에 `EXECUTIONS_MODE=queue`를 설정합니다. worker 기본 동시성은 10이며 5 이상을 권장합니다. Redis와 DB가 모두 필요하고 SQLite는 지원하지 않습니다. ([Queue mode](https://docs.n8n.io/hosting/scaling/queue-mode/))
- `[해석]` 트래픽이 작으면 단일 인스턴스로 충분합니다. 다만 n8n도 장애 지점이 될 수 있으므로 가용성 요구 수준을 정해야 합니다.

### 8.2.6 n8n을 끼우는 방식 vs Slack → FastAPI 직접 수신
| 관점 | n8n 경유 (기획안, PLAN.md:4-5, 26, 57) | 직접 수신 (대안) |
|---|---|---|
| 구현량 | URL 검증, 서명 검증, 채널 필터를 노드 설정만으로 해결 | 직접 구현해야 함. slack-bolt(1.30.0)나 slack-sdk(3.46.0)를 쓰면 줄어듦 |
| 비개발자 수정 | 쉬움(필터 조건, 다른 트리거 추가) | 코드 배포 필요 |
| 홉/장애 지점 | Slack→n8n→FastAPI로 홉이 1개 더 있고 n8n 운영 부담이 추가됨 | 홉 최소 |
| 유실 위험 | n8n이 먼저 200을 보내므로 이후 단계가 실패하면 Slack 재시도가 일어나지 않음 → n8n 재시도 필수 | FastAPI가 큐에 넣는 데 성공한 뒤 200을 보내면 Slack 재시도를 안전망으로 쓸 수 있음(3초 안에 끝나야 함) |
| 중복 제거 | n8n에는 없음. `event_id`도 전달되지 않음(소스 확인) | `event_id` 그대로 사용 가능 |
| 봇 루프 방지 | 직접 필터를 구성해야 함 | Bolt 미들웨어 사용 가능 |
| 확장성 | 이메일, PagerDuty 등 다른 소스도 같은 FastAPI로 연결하기 쉬움 | 소스마다 코드 필요 |
| 결론 | `[해석]` 기획이 n8n을 명시했으므로 기본값은 n8n 경유입니다. 위 단점은 FastAPI 쪽 멱등성과 n8n 재시도로 보완할 수 있습니다. | Interactivity(승인 버튼)만은 FastAPI로 직접 받는 혼합 방식도 가능합니다(결정 필요) |

---

## 8.3 FastAPI

- `[조사]` 현재 버전: `fastapi` **0.162.4**(2026-11-10, PyPI)이며 Python 3.10 이상이 필요합니다.
- `[조사]` `BackgroundTasks`는 **응답을 반환한 뒤 같은 프로세스에서** 함수를 실행합니다. 공식 문서는 "무거운 백그라운드 계산이고 같은 프로세스에서 돌 필요가 없다면 Celery 같은 더 큰 도구가 낫다. RabbitMQ나 Redis 같은 메시지/잡 큐 관리자가 필요하지만, 여러 프로세스와 **여러 서버**에서 작업을 실행할 수 있다"고 안내합니다. ([Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/))
- `[해석]` BackgroundTasks와 외부 큐 비교

| 관점 | BackgroundTasks | 외부 큐 + 워커 |
|---|---|---|
| 내구성 | 프로세스가 죽거나 재배포되면 작업이 사라짐 | 큐에 남아 있다가 다른 워커가 처리 |
| 재시도/DLQ | 없음 | 큐에 따라 있음(8.4) |
| 분산 | API 서버 자원을 같이 씀(AI 작업이 API를 느리게 만듦) | 워커를 따로 수평 확장 |
| 백프레셔 | 없음(PLAN.md:8의 "100개 동시" 문제를 그대로 겪음) | 큐 길이로 흡수(PLAN.md:11) |
| 적합한 용도 | 로깅, 메트릭 전송 같은 가벼운 후처리 | AI 분석, 테스트 실행 같은 수 분 단위 작업 |

- `[해석]` 수신 엔드포인트 패턴: 인증 → 페이로드 검증(Pydantic) → 멱등 키로 `INSERT ... ON CONFLICT DO NOTHING` → 새로 생긴 작업이면 큐에 넣음 → **202 Accepted**와 `job_id` 반환. 큐에 넣지 못하면 5xx를 반환해 n8n이 재시도하게 합니다. DB에는 기록됐는데 큐 적재에 실패한 경우(이중 쓰기 문제)는 다음 중 하나로 처리합니다. (a) `status='received'` 상태로 오래 남은 작업을 주기적으로 다시 적재하는 sweeper, (b) outbox 테이블, (c) DB 자체를 큐로 사용(8.4의 Postgres SKIP LOCKED).

---

## 8.4 메시지 큐 후보 비교

버전(PyPI, 2026-11-11 조회): `redis`(redis-py) 8.1.0, `celery` 5.7.0(2026-11-05), `rq` 3.0.0(2026-10-28), `arq` 0.28.0(2026-04-16).

| 후보 | ack / 전달 보장 | 재시도 | DLQ | 멱등성 지원 | 동시성 제어 | 장시간 작업 | 운영 부담 | 출처 |
|---|---|---|---|---|---|---|---|---|
| **Redis Streams** (직접 구현) | `[조사]` consumer group에서 `XREADGROUP`으로 읽고 `XACK`으로 확정합니다. ack 전까지 PEL(Pending Entries List)에 남습니다. 처리 중 장애가 나면 메시지를 여러 번 처리할 수 있습니다(at-least-once 성격). | `[조사]` `XAUTOCLAIM`(6.2+)으로 `min-idle-time`보다 오래 idle 상태인 메시지를 넘겨받습니다. 이때 전달 횟수가 증가합니다(JUSTID 옵션 제외). 8.8의 `XNACK`은 ack 없이 즉시 그룹에 반환합니다. | `[조사]` 내장되어 있지 않습니다. `XPENDING`의 전달 횟수를 보고 별도 스트림으로 옮기는 방식을 직접 구현해야 합니다. | `[조사]` **Redis 8.6**부터 `XADD ... IDMP producer-id idempotent-id`를 지원합니다. 같은 조합으로 다시 넣으면 원래 엔트리 ID를 돌려줍니다. 보존 기간과 개수는 `XCFGSET`의 `IDMP-DURATION`/`IDMP-MAXSIZE`로 정합니다. | 컨슈머 수, `COUNT` | `[해석]` `min-idle-time`을 최대 작업 시간보다 길게 잡아야 처리 중인 메시지를 다른 워커가 가로채지 않습니다. | 낮음. 기획에 Redis가 이미 있음(PLAN.md:22) | [Streams](https://redis.io/docs/latest/develop/data-types/streams/), [XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/), [XADD](https://redis.io/docs/latest/commands/xadd/) |
| Redis List | `[미확인]` 이번 조사에서 공식 문서를 확인하지 않았습니다. 일반적으로 ack가 없어 워커 장애 시 메시지를 잃을 수 있습니다. | - | - | - | - | - | 낮음 | - |
| **Celery 5.7** | `[조사]` 기본은 실행 직전 ack입니다. `acks_late`면 실행 후 ack하지만, 자식 프로세스가 kill되면 그래도 ack됩니다. 이 경우 재전달하려면 `task_reject_on_worker_lost`를 켭니다. | `[조사]` `autoretry_for`(기본 없음), `max_retries` 기본 3, `retry_backoff` 기본 False, `retry_backoff_max` 600초, `retry_jitter` 기본 True, `default_retry_delay` 3분 | `[미확인]` Redis 브로커의 DLQ 지원 여부. RabbitMQ 브로커면 DLX를 쓸 수 있습니다. | `[조사]` 문서는 "태스크는 이상적으로 멱등해야 한다"고 하며, `acks_late`는 멱등한 태스크에만 쓰라고 권합니다. | 워커 concurrency | `[조사]` `time_limit`(hard)와 `soft_time_limit`(`SoftTimeLimitExceeded`) | 중간 | [Celery Tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html) |
| **RQ 3.0** | `[미확인]` ack 의미론의 세부 사항 | `[조사]` CLI `--retry-max`, `--retry-interval` | `[조사]` 실패한 작업은 `failure_ttl`(기본 1년) 동안 보관 | - | 워커 수 | `[조사]` `job_timeout`(기본값 미확인) | 낮음. `fork()`가 필요해 Windows는 WSL 필요 | [RQ docs](https://python-rq.org/docs/) |
| **arq 0.28** (asyncio) | `[조사]` "작업이 두 번 이상 호출될 수 있다(pessimistic execution)". 워커가 종료되면 나중에 다시 실행합니다. | `[조사]` `Retry` 예외, `max_tries` 기본 5 | `[미확인]` | `[조사]` `_job_id`가 같으면 실행이 끝나기 전까지 다시 넣을 수 없습니다. | `[조사]` `max_jobs` 기본 10 | `[조사]` `job_timeout` 기본 **300초**. LLM 호출과 테스트 루프에는 부족할 수 있어 조정이 필요합니다. | 낮음(Redis) | [arq](https://arq-docs.helpmanual.io/) |
| **RabbitMQ** (quorum queue) | 수동 ack, per-consumer prefetch | `[조사]` 4.0부터 quorum queue의 `delivery-limit` 기본값이 **20**이고, 넘으면 버리거나 DLX로 보냅니다. 4.3부터는 명시적 반환이 카운트되지 않습니다. | `[조사]` DLX를 policy(`dead-letter-exchange`)로 설정합니다. "모든 quorum queue에 dead letter 구성을 권장"합니다. | - | prefetch | 장시간 처리가 가능합니다(`[미확인]`: consumer timeout 기본값) | 높음(새 인프라) | [DLX](https://www.rabbitmq.com/docs/dlx), [Quorum queues](https://www.rabbitmq.com/docs/quorum-queues) |
| **Google Cloud Pub/Sub** | `[조사]` exactly-once delivery 옵션: ack가 성공한 뒤에는 재전달하지 않습니다. 단, **pull 구독에서만**, **같은 리전**에서 접속할 때만 보장됩니다. ack deadline 기본값은 60초이고, `minDurationPerAckExtension` 상한은 600초입니다. 발행자가 재시도하면 중복이 생길 수 있습니다. | 구독 retry policy(`[미확인]`: 백오프 기본값) | `[조사]` 구독에 dead-letter topic을 설정합니다. max delivery attempts는 기본 5, 범위 5~100이며 근사치입니다. Pub/Sub 서비스 계정에 publisher/subscriber 역할을 줘야 합니다. | 발행 측 중복은 앱에서 처리 | 구독자 flow control | 리스 연장이 필요합니다 | 낮음(관리형), GCP 종속 | [Handling failures](https://docs.cloud.google.com/pubsub/docs/handling-failures), [Exactly-once](https://docs.cloud.google.com/pubsub/docs/exactly-once-delivery) |
| **PostgreSQL 테이블 큐** (`FOR UPDATE SKIP LOCKED`) | 트랜잭션 커밋이 곧 ack | 직접 구현(attempt 컬럼) | 직접 구현(status 컬럼) | jobs 테이블의 UNIQUE 제약과 같은 트랜잭션에서 처리 → 이중 쓰기 문제 없음 | 워커 수 | 장시간 트랜잭션을 피하고 lease 컬럼으로 관리 | 낮음(DB 재사용) | `[조사]` "여러 컨슈머가 큐 같은 테이블에 접근할 때 락 경합을 피하는 데 쓸 수 있다" ([PostgreSQL 18 SELECT](https://www.postgresql.org/docs/current/sql-select.html)) |

- `[해석]` 요구 규모는 "100건 동시 유입"(PLAN.md:8)이고 작업 하나가 수 분 걸립니다. 처리량보다 **내구성, 재시도, 상태 가시성**이 더 중요합니다. 기획이 Redis를 이미 언급하므로(PLAN.md:22) Redis Streams나 arq가 추가 인프라를 가장 적게 씁니다. GCP에 배포한다면 Pub/Sub도 유력합니다. 최종 선택은 **결정 필요**입니다(메인 문서 OQ-08).

---

## 8.5 LangGraph

- `[조사]` 현재 버전: `langgraph` **1.2.20**(2026-11-05, Python 3.10 이상), `langgraph-checkpoint-postgres` **3.2.0**(2026-10-14). (PyPI)

| 기능 | 확인 내용 | 출처 |
|---|---|---|
| StateGraph / State | `[조사]` `from langgraph.graph import StateGraph, START, END`. State는 TypedDict, dataclass, Pydantic 중 하나로 정의하며 성능상 TypedDict나 dataclass를 권장합니다. reducer는 `Annotated[list[str], fn]` 형태로 붙이고, reducer가 없으면 값을 덮어씁니다. `operator.add`는 리스트를 이어 붙이고, `add_messages`는 메시지 ID 기준으로 갱신합니다. | [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) |
| 노드/엣지 | `[조사]` `add_node(name, fn)`, `add_edge(a, b)`, `add_conditional_edges("a", routing_fn, {"x": "b", "y": "c"})`. 라우팅 함수는 노드 이름, END, 이름 목록, `Send` 중 하나를 반환합니다. **한 노드에 정적 엣지와 동적 라우팅(조건부 엣지나 Command)을 섞지 말라**고 안내합니다. | 같은 문서 |
| Command | `[조사]` 필드는 `update`, `goto`, `graph`(`Command.PARENT`), `resume`입니다. `Command`를 입력으로 넘기면 START가 아니라 최신 체크포인트에서 재개합니다. 새 실행을 시작하려면 일반 dict를 넘깁니다. | 같은 문서 |
| **Recursion limit** | `[조사]` **1.1.4부터 기본값이 10,007 super-step**입니다(1.0.6~1.1.3은 10,000). 환경 변수 `LANGGRAPH_DEFAULT_RECURSION_LIMIT`나 config 최상위 키 `recursion_limit`(`configurable` 아래가 아님)로 바꿉니다. 초과하면 `GraphRecursionError`가 납니다. | 같은 문서 |
| interrupt (HITL) | `[조사]` `from langgraph.types import interrupt, Command`. 노드 안에서 `interrupt(payload)`로 멈추며 payload는 JSON 직렬화가 가능해야 합니다. **checkpointer와 `thread_id`가 필수**입니다. `Command(resume=value)`로 재개하면 그 값이 `interrupt()`의 반환값이 됩니다. **재개하면 interrupt가 있던 노드를 처음부터 다시 실행**하므로, interrupt 앞의 부작용은 멱등해야 합니다. 권장 방식은 부작용을 interrupt 뒤나 별도 노드에 두는 것입니다. 한 노드 안의 여러 interrupt는 순서로 매칭됩니다. `invoke()`를 쓰면 결과의 `__interrupt__`에 나타납니다. `response_schema`로 재개 입력의 타입을 지정할 수 있습니다(`langgraph>=1.2.12`). | [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts), [HITL](https://docs.langchain.com/oss/python/langgraph/human-in-the-loop) |
| 승인 분기 패턴 | `[조사]` `is_approved = interrupt({...})` 다음에 `Command(goto="proceed" or "cancel")`로 분기합니다. 재개할 때는 **같은 thread_id**를 써야 하며, 새 값을 쓰면 빈 상태로 새 스레드가 시작됩니다. 운영에서는 내구성 있는 checkpointer를 쓰라고 권고합니다. | [HITL](https://docs.langchain.com/oss/python/langgraph/human-in-the-loop) |
| Checkpointer | `[조사]` 기본 포함 패키지 `langgraph-checkpoint`에는 `InMemorySaver`가 있습니다. 그 밖에 `langgraph-checkpoint-sqlite`(로컬용), `langgraph-checkpoint-postgres`(`PostgresSaver`/`AsyncPostgresSaver`, 운영용, 처음에 `setup()` 호출), `langgraph-checkpoint-mongodb`, `langchain-azure-cosmosdb`가 있습니다. PostgresSaver의 `thread_id`는 255자 미만이어야 합니다. 이 페이지에 Redis checkpointer는 없었습니다. | [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers), [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) |
| Durability | `[조사]` `durability="exit"`는 종료 시에만 저장해 가장 빠르지만 중간에 크래시가 나면 진행분을 잃습니다. `"async"`는 다음 단계와 병렬로 저장해 최근 체크포인트를 잃을 수 있습니다. `"sync"`는 다음 단계 전에 저장해 가장 안전하지만 느립니다. 실행 메서드의 인자로 넘깁니다. | [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers) |
| 장애 후 재개 | `[조사]` `graph.invoke(None, config)`는 최신 체크포인트에서 미완료 작업을 다시 시도합니다. 한 super-step에서 일부 노드만 실패하면 성공한 형제 노드의 쓰기(pending writes)를 보존하고 재실행하지 않습니다. 상태는 `get_state(config)`, `get_state_history(config)`로 조회합니다. | 같은 문서 |
| RetryPolicy | `[조사]` `from langgraph.types import RetryPolicy`. `add_node(..., retry_policy=RetryPolicy(max_attempts=3))` 형태로 붙입니다. 기본값은 `max_attempts=3`, `initial_interval=0.5`, `backoff_factor=2.0`, `max_interval=128.0`, `jitter=True`, `retry_on=default_retry_on`입니다. `default_retry_on`은 `ValueError`, `TypeError`, `RuntimeError`, `OSError` 등은 재시도하지 않고, `NodeTimeoutError`는 재시도합니다. | [Fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance) |
| 노드 timeout | `[조사]` `langgraph>=1.2`, **async 노드만** 지원합니다. `timeout=60`이나 `TimeoutPolicy(run_timeout=120, idle_timeout=30)` 형태로 지정하고, 초과하면 `NodeTimeoutError`가 납니다. RetryPolicy와 함께 쓰면 시도마다 시계가 초기화됩니다. | 같은 문서 |
| CachePolicy | `[조사]` 필드는 `key_func`(기본 `default_cache_key`)와 `ttl`(`int | None`, 기본 None)입니다. 노드에 `cache_policy`를 붙이고, compile할 때 cache 백엔드를 넘깁니다(보조 출처 기준 `InMemoryCache`, `langgraph.cache.memory`). | [CachePolicy ref](https://reference.langchain.com/python/langgraph/types/CachePolicy) |
| 실행 API | `[조사]` 문서 예시는 `graph.stream_events(input, config, version="v3")`와 `invoke()`를 함께 씁니다. | [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) |

- `[미확인]` CachePolicy의 공식 가이드 문서(ttl 단위, 지원 백엔드 목록), Redis 기반 checkpointer와 cache 패키지의 현재 상태.
- `[해석]` **recursion limit 기본값이 약 1만이므로 "테스트 실패 → 재수정" 루프의 안전장치가 되지 못합니다.** 반드시 State의 `attempt` 카운터로 상한을 직접 걸어야 합니다. 그 위에 `recursion_limit`을 작게(예: 노드 수 × 최대 시도 횟수 + 여유분) 지정해 이중 안전장치로 씁니다.

---

## 8.6 Vertex AI → "Gemini Enterprise Agent Platform"

### 8.6.1 명칭 변화 (중요)
- `[조사]` 기존 Vertex AI 문서 페이지 상단에 "Vertex AI documentation is no longer being updated. Vertex AI's services are now part of **Gemini Enterprise Agent Platform**."이라는 안내가 붙어 있습니다. 문서 경로도 `docs.cloud.google.com/gemini-enterprise-agent-platform/...`으로 바뀌었습니다. ([Vertex AI SDK migration guide](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk))
- `[해석]` 기획서의 "Vertex AI"(PLAN.md:20, 30, 61)는 현재 Agent Platform 위의 Gemini API를 가리킨다고 보면 됩니다.

### 8.6.2 Gemini 모델 (2026-11-11 기준. GA 외에 Preview·단기 가용 모델도 함께 표기)
출처: [Google models](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models), [Model versions and lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions), 각 모델 페이지, [Pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing)

| 모델 ID | 포지션(공식 설명 요약) | 컨텍스트 / 최대 출력 | 출시 / 은퇴 | Standard 가격, Global, 1M 토큰당 (입력 / 출력 / 캐시 입력) |
|---|---|---|---|---|
| `gemini-3.1-flash-lite` | "가장 비용 효율적인" 저지연, 대량 트래픽용 | 1,048,576 / 65,536 | 2026-05-07 / 2027-05-07 이후 | $0.25 / $1.50 / $0.025 (오디오 입력 $0.50) |
| `gemini-3.5-flash-lite` | 가벼운 에이전트 워크플로, 분류, 라우팅, JSON 추출. 기본 thinking은 **MINIMAL** | 1,048,576 / 65,536 | 2026-07-21 / 2027-07-21 이후 | $0.30 / $2.50 / $0.03 |
| `gemini-3.5-flash` | "Flash 가격에 Pro 근접 지능" | `[미확인]` | 2026-05-19 / 2027-05-19 이후 | `[미확인]` |
| `gemini-3.8-flash` | "가장 지능적인 workhorse", 장기 코딩과 자율 에이전트용. thinking은 LOW/MEDIUM(기본)/HIGH이며 **MINIMAL은 쓸 수 없음** | 1,048,576 / 65,536 | 2026-09-02 / 2027-09-02 이후 | **2026-12-31까지 프로모션** $0.75 / $3.75 / $0.075, **2027-01-01부터** $1.50 / $7.50 / $0.15 |
| `gemini-4-argon` | Google의 frontier 모델(코딩, 금융, 법률, 보안, 과학). 입력과 출력을 합쳐 1,048,576 토큰 한도. 스트리밍 시 세그먼트당 최대 65,536 출력 토큰. temperature/top_p/top_k는 무시됨. MINIMAL thinking은 400 오류. **implicit caching만 지원**(최소 4,096 토큰) | 1,048,576(입출력 합산) | 2026-10-13 / 2027-10-13 이후 | **2026-11-30까지 프로모션** $2 / $10 / $0.10, **2026-12-01부터** $4 / $20 / $0.20 |
| `gemini-4-carbon` | frontier 모델(Argon과 같은 설명). Service Specific Terms 34조 적용, 지식 컷오프 2026-03. **단기 가용 모델로 분류되며 은퇴일 미정** | 1,048,576(입출력 합산) | 2026-11-10 / 미정 | Argon과 같은 가격 체계 |
| `gemini-3.1-pro` | **Preview**, 추론 우선 | 1M | Preview | $2 / $12 (200K 이하) |
| `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite` | - | - | **2026-10-20 은퇴**(이미 지남). 대체 모델: 3.8 Flash, 3.5 Flash, 3.5 Flash-Lite | - |
| `gemini-3.6-flash`, `gemini-3.7-flash` | 단기 가용 모델 | - | 은퇴일: 3.6은 **2026-11-19**, 3.7은 2027-01-28. 대체 모델은 3.8 Flash | 3.8 Flash와 같은 프로모션가 |

- `[조사]` 표의 "프로모션" 가격은 가격표 각주 기준으로 "일정 기간 순지출의 50%를 크레딧으로 환급"하는 방식입니다(Cyber Permissive 변형에는 적용되지 않음).
- `[조사]` Non-global(리전) 엔드포인트는 Global보다 약 10% 비쌉니다(예: 3.8 Flash 프로모션 입력 $0.825). Priority와 Flex/Batch 요금 구간이 따로 있습니다. **200 응답을 받은 요청만 과금**합니다(4xx/5xx는 과금 없음). 단, Gemini 4는 completion이 32K 토큰을 넘으면 요청이 타임아웃으로 실패해도 회수 가능한 사용량은 과금됩니다. ([Pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing))
- `[해석]` 모델 은퇴 주기가 짧습니다. 예를 들어 2.5 계열은 출시 약 16개월 뒤에 은퇴했습니다. 그래서 모델 ID를 코드에 박아 두지 말고 설정으로 분리해야 합니다(경량/고성능 역할 이름 → 모델 ID 매핑). 기획서의 "가벼운 모델 / 고성능 모델"(PLAN.md:53)을 지금 시점의 모델에 대응시키면 다음과 같습니다.
  - 경량: `gemini-3.5-flash-lite` 또는 `gemini-3.1-flash-lite`
  - 고성능: `gemini-3.8-flash`(가성비) 또는 `gemini-4-argon`(최고 성능, 비쌈). `gemini-4-carbon`은 GA가 아닌 단기 가용 모델(은퇴일 미정)이라 운영 기본값으로는 비권장
  - 최종 선택은 결정 필요입니다.

### 8.6.3 임베딩 모델
| 모델 ID | 차원 | 입력 한도 | 출시 / 은퇴 | 가격 | 출처 |
|---|---|---|---|---|---|
| `gemini-embedding-001` | 기본 3,072. `output_dimensionality`로 축소 가능 | `[조사]` Text embeddings API 공통 제한: 요청당 입력 텍스트 250개, 요청 전체 20,000 토큰. 입력 텍스트 하나당 **2,048 토큰**이며 넘는 부분은 **조용히 잘림**(`autoTruncate=false`로 끌 수 있음) | 2025-05-20 / 2028-05-20 이전에는 은퇴하지 않음 | "Gemini Embedding" 입력: 온라인 1,000 count당 $0.00015, 배치 $0.00012 | [Get text embeddings](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/embeddings/get-text-embeddings), [lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions) |
| `gemini-embedding-2` | 기본 3,072. MRL로 축소 가능 | 최대 입력 **8,192 토큰**. 멀티모달 입력(텍스트, PDF 최대 6페이지, 이미지 6장, 오디오, 비디오). 커스텀 task instruction(예: `task:code retrieval`) 지원 | 2026-04-22 GA / 은퇴일 표기 없음 | `[미확인]` 별도 단가가 있는지("Gemini Embedding" 행과 같은지) | [Gemini Embedding 2](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/embedding-2) |
| `text-embedding-005`, `text-multilingual-embedding-002` | 768 | - | **2027-04-01 은퇴 예정** | 1,000 count당 $0.000025 | lifecycle, pricing |

- `[조사]` task type에는 `RETRIEVAL_DOCUMENT`(문서 색인), `RETRIEVAL_QUERY`(검색 질의, 맞는 용도가 없을 때 기본 권장), `QUESTION_ANSWERING`, `FACT_VERIFICATION`, `CODE_RETRIEVAL_QUERY`(자연어로 코드 블록 검색, 코드 쪽은 `RETRIEVAL_DOCUMENT`로 임베딩), `CLASSIFICATION`, `SEMANTIC_SIMILARITY`가 있습니다. 벡터는 정규화돼 있어 cosine, dot product, euclidean 중 무엇을 써도 순위가 같습니다. ([Task types](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/embeddings/task-types), [Get text embeddings](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/embeddings/get-text-embeddings))
- `[미확인]` 가격표의 "1,000 count" 단위가 토큰인지 문자인지 표에 명시돼 있지 않습니다. 요청당 250개/20,000 토큰 제한이 `gemini-embedding-2`에도 똑같이 적용되는지도 확인하지 못했습니다.
- `[해석]` 3,072차원을 그대로 쓰면 pgvector의 `vector` 타입 인덱스 한도(2,000차원, 8.7)를 넘습니다. 이 경우 `halfvec`(최대 4,000) 인덱스를 쓰거나 `output_dimensionality`를 1,536이나 768로 줄여야 합니다. 임베딩 모델과 Vector DB는 함께 정해야 합니다.

### 8.6.4 Context caching
- `[조사]` **Implicit caching**은 모든 프로젝트에서 기본으로 켜져 있습니다. 캐시된 토큰은 표준 입력가보다 **90% 할인**되고 저장 비용은 없습니다. 메모리 계층과 durable 계층(3.8 Flash, 4.x만 지원)이 있습니다. 캐시된 prefix는 생성 후 최대 24시간 안에 만료되며 대부분 그보다 일찍 축출됩니다. 적중률을 높이려면 크고 공통된 내용을 프롬프트 앞부분에 두고, 비슷한 prefix의 요청을 짧은 간격으로 보내라고 안내합니다. ([Context caching overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview))
- `[조사]` **Explicit caching**은 Gemini 2.5 이상에서 90% 할인을 보장합니다. 기본 TTL은 60분이고 갱신할 수 있습니다. 토큰·시간 단위 저장 비용이 따로 듭니다. Flash 계열은 시간당 토큰당 $0.000001, 3.1 Pro는 $0.0000045입니다. **4 Argon/Carbon은 explicit caching을 지원하지 않습니다**(implicit만).
- `[조사]` 최소 캐시 토큰 수는 Gemini 3(예외 있음)과 4가 4,096이고, **3.7 Flash와 3.8 Flash의 implicit 캐시는 6,144**입니다. 캐시할 수 있는 blob/text 최대 크기는 10MB입니다. 응답 메타데이터의 `cachedContentTokenCount`가 캐시된 토큰 수를 알려줍니다.
- `[조사]` 에이전트 워크로드에서 사용자 간 데이터가 섞이지 않도록 캐시 범위를 사용자 세션과 권한 경계에 맞추라고 권고합니다.
- `[해석]` "테스트 실패 → 재수정" 루프는 같은 코드 컨텍스트와 문서를 반복해서 보냅니다(PLAN.md:65). 프롬프트를 **[고정: 시스템 지침 + 코드/문서 컨텍스트] + [가변: 이전 실패 요약]** 순서로 짜면 implicit caching 적중을 노릴 수 있습니다.

### 8.6.5 Python SDK
- `[조사]` **권장 SDK는 Google Gen AI SDK(`google-genai`)** 입니다. PyPI 최신은 **3.1.0**(2026-11-10)이며 **Python 3.11 이상**이 필요합니다. ([Gen AI SDK overview](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/sdks/overview), PyPI)
- `[조사]` Vertex AI SDK(`google-cloud-aiplatform`)의 `vertexai.generative_models`, `vertexai.language_models`, `vertexai.vision_models`, `vertexai.caching`, `vertexai.tuning` 모듈은 deprecated 되었고 **2026-06-24 이후 릴리스부터 제외**되었습니다. ([Migration guide](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk))
- `[조사]` 클라이언트 초기화는 두 가지 방식이 있습니다(google-genai README, PyPI 3.1.0).
  - 코드에서 직접: `genai.Client(enterprise=True, project='...', location='global')`. API 버전은 `http_options=types.HttpOptions(api_version='v1')`로 지정합니다.
  - 환경 변수로: `GOOGLE_GENAI_USE_ENTERPRISE=true`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=global`을 설정하고 `genai.Client()`를 호출합니다.
  - README의 일부 예시에는 아직 `genai.Client(vertexai=True)`가 남아 있습니다. 문서 JS/Java 샘플에도 `GOOGLE_GENAI_USE_VERTEXAI`가 보여 문서끼리 일관되지 않습니다. 새 코드는 `enterprise=True`/`GOOGLE_GENAI_USE_ENTERPRISE` 쪽을 기준으로 삼는 것이 안전합니다(`[해석]`).
- `[조사]` 주요 API: `client.models.generate_content`, `generate_content_stream`, `count_tokens`, `compute_tokens`(Agent Platform 전용), `client.caches.create`(explicit cache), `client.models.embed_content(model, contents, config=EmbedContentConfig(task_type=..., output_dimensionality=...))`, 구조화 출력(`response_mime_type='application/json'` + `response_json_schema` 또는 Pydantic 모델), 비동기 클라이언트 `client.aio`.
- `[조사]` LangChain 연동 패키지: `langchain-google-genai` 4.4.3(2026-11-04), `langchain-google-vertexai` 3.2.5(2026-10-23). (PyPI)
- `[조사]` 응답의 usage 메타데이터 필드는 `promptTokenCount`, `candidatesTokenCount`, `totalTokenCount`, `thoughtsTokenCount`, `cachedContentTokenCount`입니다. (공식 SDK 레퍼런스 [dotnet-genai UsageMetadata](https://googleapis.github.io/dotnet-genai/api/Google.GenAI.Types.GenerateContentResponseUsageMetadata.html), [java-genai](https://googleapis.github.io/java-genai/javadoc/com/google/genai/types/GenerateContentResponseUsageMetadata.html)에 필드가 있습니다. Python에서는 snake_case(`usage_metadata.prompt_token_count` 등)일 것으로 보이나 `[미확인]`.)
- `[미확인]` 프리뷰 모델에서 `thoughtsTokenCount`가 빠지는 현상은 커뮤니티 보고만 있습니다(1차 출처 아님).

---

## 8.7 Vector DB 후보

| 후보 | 메타데이터 필터링 | 필터 + ANN 주의점 | 운영/비용 관점 | 출처 |
|---|---|---|---|---|
| **pgvector 0.8.7** (Postgres 확장) | `[조사]` 일반 SQL `WHERE` 절 사용 | `[조사]` **근사 인덱스(HNSW/IVFFlat)에서는 인덱스 스캔이 끝난 뒤 필터를 적용**합니다. 예를 들어 `hnsw.ef_search` 기본값 40에 조건 일치율이 10%면 평균 4행만 남습니다. 0.8.0부터는 iterative index scan(`hnsw.iterative_scan = strict_order` 또는 `relaxed_order`, `hnsw.max_scan_tuples` 기본 20,000)으로 보완합니다. 값 종류가 적은 필터는 partial index, 많은 필터는 파티셔닝을 권장합니다. 인덱스 한도는 `vector` 2,000차원, `halfvec` 4,000차원입니다. 연산자는 `<->` L2, `<#>` 음의 내적, `<=>` cosine입니다. | 기존 Postgres(jobs, checkpointer)에 함께 둘 수 있어 인프라가 가장 적습니다. 대규모(수천만 벡터 이상)에서는 튜닝 부담이 있습니다(`[해석]`). Python 패키지 `pgvector` 0.5.1 | [pgvector README](https://github.com/pgvector/pgvector) |
| **Qdrant** | `[조사]` `must`(AND), `should`(OR), `must_not`과 match, match any(IN), match except(NOT IN), range, datetime range(RFC 3339) 조건 | `[조사]` 효율적으로 필터링하려면 필터할 필드에 **payload index를 만들라**고 권고합니다(데이터를 넣기 전에 만드는 것이 좋음). | 별도 서비스(셀프호스팅 또는 클라우드). `qdrant-client` 1.20.0 | [Qdrant filtering](https://qdrant.tech/documentation/concepts/filtering/) |
| **Chroma** | `[조사]` `where`에 `$eq`, `$gt`, `$gte`, `$lte`, `$in`, `$nin`, `$and`, `$or`, `$contains`(배열)를 씁니다. `where_document`로 본문 필터도 가능합니다. | `[미확인]` 필터와 ANN을 함께 쓸 때의 성능 특성 | 프로토타입에 적합합니다(`[해석]`). `chromadb` 1.5.9(2026-05-05). `[미확인]` 배포 모드별 특성 | [Chroma metadata filtering](https://docs.trychroma.com/docs/querying-collections/metadata-filtering) |
| **Vertex AI Vector Search (1.0)** | `[조사]` 범주형 restricts(namespace/token, allow/deny)와 numeric restricts(`value_int`/`value_float`/`value_double` + `op`)를 지원합니다. namespace 사이는 AND, namespace 안은 OR로 결합합니다. 데이터포인트의 빈 namespace는 와일드카드가 아닙니다. | `[조사]` restricts가 많으면 메모리를 많이 써서 샤드 수가 늘어납니다. | `[조사]` 배포한 인덱스를 호스팅하는 VM 비용과 빌드·업데이트 비용이 듭니다. "최소 구성은 월 100달러 미만"이라고 안내합니다. 스트리밍 업데이트와 하이브리드(dense + sparse) 검색을 지원합니다. | [Vector Search overview](https://docs.cloud.google.com/vertex-ai/docs/vector-search/overview), [Filtering](https://docs.cloud.google.com/vertex-ai/docs/vector-search/filtering) |
| **Agent Retrieval (구 Vector Search 2.0)** | `[조사]` Collection/Data Object 모델이며, 벡터 유사도와 payload 데이터로 저장·조회·필터를 한 곳에서 처리합니다. 내장 모델로 임베딩 필드를 자동으로 채우거나 BYOE를 쓸 수 있습니다. | `[미확인]` 필터 문법의 세부 사항 | `[조사]` usage 기반과 resource 기반 두 가지 요금 모델이 있습니다. KNN과 ANN을 고를 수 있습니다. | [Agent Retrieval overview](https://docs.cloud.google.com/vertex-ai/docs/vector-search-2/overview) |

- `[해석]` 기획이 예로 든 규모는 문서 10만 개(PLAN.md:18)이고 확장 시 100만 개(PLAN.md:53)입니다. 청크 수로 환산하면 수십만~수백만 벡터입니다. 이 규모면 pgvector로도 충분히 다룰 수 있는 범위로 보입니다. 다만 "메타데이터로 먼저 범위를 줄인 뒤 유사도 검색"(PLAN.md:53)을 그대로 구현하려면 pgvector의 post-filter 특성을 꼭 고려해야 합니다(iterative scan, partial index, 서비스별 파티션). 최종 선택은 결정 필요입니다(OQ-18).

---

## 8.8 회귀 테스트 / 샌드박스 도구

| 도구 | 확인 내용 | 출처 |
|---|---|---|
| **pytest-testmon** 2.3.0 (2026-11-02, Python 3.11 이상) | `[조사]` Coverage.py로 테스트와 실행된 코드 사이의 의존 관계를 모아 `.testmondata`에 저장하고, 변경된 부분에 영향을 받는 테스트만 고릅니다. 실행할 때마다 DB를 갱신하므로 VCS와 무관하게 동작합니다. **직전에 실패한 테스트는 항상 다시 실행**합니다. 처음에 `pytest --testmon`을 한 번 돌려 기준 데이터를 만들어야 합니다. 옵션: `--testmon-noselect`(실패할 가능성이 높은 순으로 정렬만 함), `--testmon-nocollect`, `--testmon-forceselect`, `--testmon-env`. 추적하는 것은 코드, 환경 변수, Python 버전, 서드파티 패키지 버전이고, **추적하지 않는 것은 정적 파일(txt, xml 등)과 외부 서비스**입니다. 메서드 파라미터 이름만 바꿔도 해당 모듈을 쓰는 모든 테스트가 선택되는 등 과선택이 있을 수 있습니다. | [testmon.org](https://testmon.org/), PyPI |
| **coverage.py dynamic contexts** | `[조사]` `dynamic_context = "test_function"`으로 설정하면 테스트 함수마다 별도 컨텍스트로 기록합니다(5.0+). pytest-cov는 `--cov-context` 옵션을 제공합니다. `coverage html --show-contexts`로 줄마다 어떤 테스트가 실행했는지 볼 수 있습니다. `.coverage` 파일은 SQLite DB라서 직접 질의할 수 있습니다. | [Coverage contexts](https://coverage.readthedocs.io/en/latest/contexts.html) |
| **Jest** | `[조사]` `--findRelatedTests <files>`는 주어진 소스 파일을 커버하는 테스트를 실행합니다. `--onlyChanged`(`-o`)는 git/hg 변경분 기준이며 정적 의존 그래프가 필요합니다(동적 require 불가). `--changedSince <branch/commit>`, `--listTests`(실행할 파일 목록만 출력), `--passWithNoTests`, `--json --outputFile=` 옵션이 있습니다. | [Jest CLI](https://jestjs.io/docs/cli) |
| **PyCG** | `[조사]` Python 정적 call graph 생성기이지만 **2023-11-26에 아카이브**되어 더 이상 개발되지 않습니다. | [PyCG](https://github.com/vitsalis/PyCG) |
| **tree-sitter** | `[조사]` 파서 생성기이자 증분 파싱 라이브러리입니다. 구체 구문 트리를 만들고, Python 바인딩(py-tree-sitter)과 여러 언어 문법이 있습니다. | [tree-sitter](https://tree-sitter.github.io/tree-sitter/) |
| **Go callgraph** | `[조사]` `golang.org/x/tools/cmd/callgraph`는 "Go 프로그램의 call graph를 보고하는 도구"입니다. `[미확인]` 지원 알고리즘(static/cha/rta/vta)과 출력 형식 | [pkg.go.dev](https://pkg.go.dev/golang.org/x/tools/cmd/callgraph) |
| **Docker `--network none`** | `[조사]` 컨테이너 안에는 loopback 장치만 만들어져 네트워크가 완전히 격리됩니다. | [Docker none driver](https://docs.docker.com/engine/network/drivers/none/) |
| **gVisor** | `[조사]` 사용자 공간 애플리케이션 커널로, 컨테이너와 호스트 사이에 강한 격리 계층을 둡니다. `runsc` 런타임이 Docker와 Kubernetes에 통합됩니다. 대가로 호환성이 줄고 시스템 콜 오버헤드가 커집니다(모든 syscall, `/proc`, `/sys`를 구현하지는 않음). | [gVisor docs](https://gvisor.dev/docs/) |

- `[미확인]` Java/Kotlin용 영향 분석과 테스트 선별 도구(예: Gradle/Develocity Predictive Test Selection, jdeps), JS 의존 그래프 도구(madge, dependency-cruiser), Python 참조 분석(Pyright/Jedi의 find references)의 현재 스펙은 이번 조사에서 확인하지 않았습니다. 대상 언어가 정해지면(OQ-03) 추가로 조사해야 합니다.

---

## 8.9 승인 후 반영: GitHub PR 생성

- `[조사]` `POST /repos/{owner}/{repo}/pulls`의 필수 파라미터는 `head`(변경 브랜치, 포크면 `username:branch`)와 `base`(대상 브랜치)이고, `title`은 `issue`를 지정하지 않으면 필수입니다. `draft: true`로 초안 PR을 만들 수 있습니다(공개 저장소, 또는 Team/Enterprise Cloud의 비공개 저장소). head 브랜치에 쓰기 권한이 있어야 합니다. ([GitHub REST Pulls](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request))
- `[미확인]` fine-grained PAT와 GitHub App에 필요한 권한 이름(예: "Pull requests: write", "Contents: write"). 같은 문서에서 확인하지 못했습니다. GitLab 등 다른 호스팅은 조사하지 않았습니다.

---

## 8.10 보안 참고 (OWASP Top 10 for LLM Applications 2025)

- `[조사]` **LLM01:2025 Prompt Injection**: 직접 주입(사용자 입력)과 간접 주입(웹사이트, 파일 같은 외부 콘텐츠에 숨긴 지시) 두 가지가 있습니다. 완화책은 모델 행동 제약, 출력 형식 정의와 결정적 코드로 검증, 입출력 필터링, 최소 권한(확장 기능은 모델이 아니라 애플리케이션 토큰으로 처리), 고위험 작업에 사람 승인, 외부 콘텐츠를 분리하고 표시, 적대적 테스트입니다. 완전한 방지가 가능한지는 불분명하다고 밝힙니다. ([LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/))
- `[조사]` **LLM06:2025 Excessive Agency**: 기능, 권한, 자율성이 과도하면 조작된 출력이 피해를 주는 행동으로 이어집니다. 완화책은 확장 기능 최소화, 확장 권한 최소화, 사용자 컨텍스트에서 실행, 고영향 작업에 사람 승인입니다. ([LLM06](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/))
- `[미확인]` OpenTelemetry GenAI semantic conventions는 별도 저장소(https://github.com/open-telemetry/semantic-conventions-genai)로 옮겨졌습니다. 속성 이름(`gen_ai.usage.input_tokens` 등)과 안정성 상태는 확인하지 못했습니다.

---

## 미확인 스펙 목록 (이 문서 기준)

1. Socket Mode의 ack 시한(3초인지)
2. Slack 메시지 이벤트의 `thread_ts`/`parent_user_id`/`client_msg_id` 공식 필드 정의
3. Slack Interactivity 문서 페이지의 서명 검증 명시 문구(일반 서명 검증 문서로 갈음함)
4. `chat.update` 스펙
5. n8n Slack Trigger에 필요한 정확한 스코프 이름, Retry On Fail 기본값, HTTP Request Timeout 기본값, Send and Wait의 세부 사항, Webhook 노드의 Slack 서명 검증 옵션 유무
6. Redis List 기반 큐 패턴, Celery Redis 브로커의 DLQ와 visibility timeout, RQ의 ack와 `job_timeout` 기본값, arq DLQ, RabbitMQ consumer timeout, Pub/Sub 재시도 백오프 기본값
7. LangGraph CachePolicy 공식 가이드(ttl 단위, 백엔드 목록), Redis checkpointer
8. `gemini-3.5-flash`의 컨텍스트와 가격, `gemini-embedding-2`의 단가, 임베딩 가격표 "count" 단위, `gemini-embedding-2`에 요청당 250개/20,000토큰 제한이 적용되는지
9. Python SDK usage 메타데이터 필드의 정확한 snake_case 이름
10. Chroma의 필터+ANN 성능과 배포 모드, Agent Retrieval의 필터 문법
11. Go callgraph 알고리즘, Java/JS 영향 분석 도구
12. GitHub fine-grained 권한 이름, GitLab 스펙
13. OTel GenAI semantic conventions 속성 이름과 안정성
