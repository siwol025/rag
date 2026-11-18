import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
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
        # 체크포인트 테이블의 CREATE INDEX CONCURRENTLY는 트랜잭션 밖에서만 돈다 → autocommit 연결로 적용(3.2)
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
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
