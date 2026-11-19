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

-- 문서 검색(pgvector, TO-012). 확장은 슈퍼유저가 만든다(compose의 POSTGRES_USER, testcontainers의 test는 슈퍼유저).
CREATE EXTENSION IF NOT EXISTS vector;

-- 색인한 문서 1개 = 1행. service·category는 taxonomy.toml의 ID(검색 선필터, TO-006).
CREATE TABLE IF NOT EXISTS documents (
    doc_id       text        PRIMARY KEY,             -- 앞머리 id(= 파일 이름)
    title        text        NOT NULL,
    service      text        NOT NULL,
    category     text        NOT NULL,
    doc_type     text        NOT NULL,                -- postmortem | runbook
    doc_date     date        NOT NULL,
    content_hash text        NOT NULL,                -- 파일 전체 sha256: 같으면 색인이 건너뜀
    indexed_at   timestamptz NOT NULL DEFAULT now()   -- 마지막으로 다시 쓴 시각
);

-- 섹션 1개 = 조각 1개 = 1행. 근사 인덱스(HNSW) 없이 WHERE 선필터 + 정확 검색(TO-012).
CREATE TABLE IF NOT EXISTS chunks (
    doc_id          text        NOT NULL REFERENCES documents ON DELETE CASCADE,
    ordinal         integer     NOT NULL,             -- 문서 안 순서(1부터). 조각 ID = doc_id#ordinal
    heading         text        NOT NULL,             -- 증상 | 원인 | 조치 | 재발 방지
    text            text        NOT NULL,
    input_hash      text        NOT NULL,             -- sha256(모델, 차원, 임베딩 입력): 재사용 판단 기준
    embedding_model text        NOT NULL,
    embedding       vector(768) NOT NULL,             -- llm.EMBEDDING_DIMS와 같게(TO-011)
    PRIMARY KEY (doc_id, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_input_hash ON chunks (input_hash);

-- 질의응답 답변(작업 완료 때 함께 기록): {found, answer, citations}. 질의응답이 아니면 NULL.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS answer jsonb;

-- 색인 임베딩 호출 1번 = 1행(작업과 무관해 llm_usage와 따로). 응답을 받은 호출만(429 거절은 기록 안 함).
CREATE TABLE IF NOT EXISTS index_usage (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model        text        NOT NULL,
    texts        integer     NOT NULL,   -- 한 요청에 넣은 텍스트 수
    input_tokens integer     NOT NULL,   -- prompt_token_count
    created_at   timestamptz NOT NULL DEFAULT now()
);
