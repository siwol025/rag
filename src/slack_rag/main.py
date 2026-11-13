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
