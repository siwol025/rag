"""test_worker.py의 강제 종료 테스트가 띄우는 워커 프로세스.

작업을 처리 중(running)으로 바꾼 뒤 이벤트 루프째 멈춰서, 테스트가 SIGKILL로 죽일 때까지 기다린다.
"""
import time

from slack_rag import worker
from slack_rag.config import Settings


async def block_event_loop(job_id):
    time.sleep(3600)  # await가 아닌 sleep: 타임아웃·취소가 끼어들 수 없음


if __name__ == "__main__":
    worker.handle_job = block_event_loop
    # job_timeout=1 → arq의 실행 중 표시(in-progress 키)가 1 + 10초 뒤 만료된다
    worker.create_worker(Settings(), job_timeout=1).run()
