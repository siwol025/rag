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
        ctx["db"] = db  # 연결 전에 넣어 둔다: open이 실패해도 shutdown이 닫을 수 있게(원인 로그가 KeyError에 가려지지 않음)
        await db.open(wait=True)
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
