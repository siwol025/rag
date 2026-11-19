"""test_worker.py의 정상 종료(SIGTERM) 테스트가 띄우는 워커 프로세스. 처리에 2초 걸리는 작업을 흉내 낸다."""
import asyncio

from fakes import OTHER
from slack_rag import worker


async def two_seconds(ctx, job_id, job_try):
    await asyncio.sleep(2)
    return {"classification": OTHER}


if __name__ == "__main__":
    worker.handle_job = two_seconds
    worker.main()  # 운영과 같은 진입점(시그널 처리 포함)
