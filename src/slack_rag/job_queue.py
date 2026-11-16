"""큐 적재. arq를 직접 쓰는 곳은 이 모듈(적재)과 worker.py(소비)뿐이다(TO-004)."""
from uuid import UUID

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from redis.exceptions import RedisError

JOB_FUNCTION = "run_job"  # worker.run_job의 이름. arq는 함수 이름으로 작업을 찾는다


class QueueUnavailable(Exception):
    """큐(Redis)에 적재하지 못했다."""


async def connect(redis_url: str) -> ArqRedis:
    return await create_pool(RedisSettings.from_dsn(redis_url))


async def enqueue(redis: ArqRedis, job_id: UUID) -> bool:
    """job_id만 큐에 넣는다. 같은 job_id가 이미 대기·실행 중이면 넣지 않고 False."""
    try:
        job = await redis.enqueue_job(JOB_FUNCTION, str(job_id), _job_id=str(job_id))
    except (RedisError, OSError) as exc:  # redis-py는 연결 실패를 ConnectionError(RedisError)로 감쌈
        raise QueueUnavailable(repr(exc)) from exc
    return job is not None
