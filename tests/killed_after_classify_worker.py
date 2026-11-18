"""test_worker.py의 '분류 뒤 강제 종료' 테스트가 띄우는 워커 프로세스.

가짜 모델로 분류를 끝내(체크포인트 저장) 다음 노드에서 이벤트 루프째 멈춘 뒤, 테스트가 SIGKILL로 죽일 때까지 기다린다.
"""
import time

from fakes import QUESTION, FakeGenai
from slack_rag import graph, llm, worker
from slack_rag.config import Settings


async def block_event_loop(state):
    time.sleep(3600)  # await가 아닌 sleep: 타임아웃·취소가 끼어들 수 없음


if __name__ == "__main__":
    llm.make_client = lambda settings: FakeGenai(QUESTION)
    graph.answer_question = block_event_loop  # 워커 시작 때 graph.build()가 이 이름을 읽는다
    # job_timeout=5 → arq의 실행 중 표시(in-progress 키)가 5 + 10초 뒤 만료된다.
    # hanging_worker.py의 1초를 쓰지 않는 이유: 멈추는 노드에 닿기 전(작업 가져가기 → 체크포인트 조회 → 분류 → 기록)에
    # 1초를 넘기면 arq의 wait_for가 작업을 재시도 없이 실패로 끝내 테스트가 깨진다(google-genai·pydantic 타입의
    # 첫 사용 때 스키마를 만드는 지연 포함). 노드가 루프를 막은 뒤에는 시간 초과가 끼어들 수 없다.
    worker.create_worker(Settings(), job_timeout=5).run()
