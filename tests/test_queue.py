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
