CREATE TABLE IF NOT EXISTS jobs (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id   text        NOT NULL,
    message_ts   text        NOT NULL,
    user_id      text        NOT NULL,
    message_text text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_channel_id_message_ts_key UNIQUE (channel_id, message_ts)
);

-- F2: 큐 소비 상태. 이미 있는 행(F1)은 queued, attempts 0으로 채워진다.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS status      text        NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    ADD COLUMN IF NOT EXISTS attempts    integer     NOT NULL DEFAULT 0,         -- 가장 최근에 가져간 시도 번호(arq job_try)
    ADD COLUMN IF NOT EXISTS started_at  timestamptz,
    ADD COLUMN IF NOT EXISTS finished_at timestamptz,
    ADD COLUMN IF NOT EXISTS error       text;
