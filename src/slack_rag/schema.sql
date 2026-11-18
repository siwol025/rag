CREATE TABLE IF NOT EXISTS jobs (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id   text        NOT NULL,
    message_ts   text        NOT NULL,
    user_id      text        NOT NULL,
    message_text text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_channel_id_message_ts_key UNIQUE (channel_id, message_ts)
);

-- 큐 소비 상태. 이 컬럼들이 생기기 전에 저장된 행은 queued, attempts 0으로 채워진다.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS status      text        NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    ADD COLUMN IF NOT EXISTS attempts    integer     NOT NULL DEFAULT 0,         -- 가장 최근에 가져간 시도 번호(arq job_try)
    ADD COLUMN IF NOT EXISTS started_at  timestamptz,
    ADD COLUMN IF NOT EXISTS finished_at timestamptz,
    ADD COLUMN IF NOT EXISTS error       text;

-- 분류 결과(작업 완료 때 함께 기록). request_type = question | code_fix | other,
-- service·category = taxonomy.toml의 ID | unknown. 기존 행은 NULL.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS request_type text,
    ADD COLUMN IF NOT EXISTS service      text,
    ADD COLUMN IF NOT EXISTS category     text;

-- LLM 응답 1번 = 1행(재시도·재실행·분당 한도 대기 뒤 다시 부른 것도 응답을 받았으면 각각, 429 거절은 기록 안 함).
-- 비용은 저장하지 않고 조회 때 단가로 환산.
CREATE TABLE IF NOT EXISTS llm_usage (
    id              bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id          uuid        NOT NULL,
    job_try         integer     NOT NULL,   -- arq 시도 번호
    node            text        NOT NULL,   -- classify
    model           text        NOT NULL,   -- 실제로 부른 모델 ID
    input_tokens    integer     NOT NULL,   -- prompt_token_count(cached_content가 있으면 캐시 토큰 포함, SDK 설명)
    output_tokens   integer     NOT NULL,   -- candidates_token_count(사고 토큰 제외)
    thoughts_tokens integer     NOT NULL,   -- thoughts_token_count
    cached_tokens   integer     NOT NULL,   -- cached_content_token_count
    created_at      timestamptz NOT NULL DEFAULT now()
);
