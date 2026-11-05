"""AI 에이전트 로직. 메시지마다 LLM 한 번(llm.extract) → 결정(policy.decide) → 템플릿 답변 순서로 처리한다.

문제 전용 부분(의도, 값, 데이터, 처리, 문장, INTRO, EXAMPLE_QUESTIONS)은 problem.py에 모여 있다. 당일에는 주로 그 파일을 고친다.
LLM 설정(모델, effort, timeout)은 llm.py, 주제 무관 규칙(권한 범위, 실패 문구, 마크다운 정리)은 policy.py에 있다.

content는 사용자가 보낸 글자다. 사진을 보내면 이미지 주소(https://talk.kakaocdn.net/...)가 문자열로 온다.
주소에는 만료 시각이 있으므로 이미지를 쓰려면 받은 즉시 내려받는다(docs/kakao_bot_api.md "실측 결과").

첫 메시지에는 with_intro가 소개 글(INTRO)과 추천 질문(EXAMPLE_QUESTIONS)을 붙인다. 당일 두 상수를 채운다.

reply는 문자열을 돌려주면 말풍선 하나로 간다. 말풍선 여러 개(최대 3)나 카드를 보내려면 kakao.skill_response(...)를
돌려준다(예시는 kakao.py "답장 만들기"). 추천 질문은 kakao.suggestion_card(["질문1", "질문2"])로 보내고, 버튼을 누르면
그 질문이 일반 메시지로 다시 온다. 퀵리플라이는 채널채팅에서 보이지 않는다(10/28 실측).

reply는 async def와 def 중 편한 쪽으로 쓴다. 어느 쪽이든 bot.py가 게이트웨이 연결과 분리된 곳에서 실행한다.
- async def: 비동기 클라이언트(AsyncAnthropic, httpx.AsyncClient)를 await로 쓴다. 동기 호출(time.sleep,
  requests, 동기 SDK)은 넣지 않는다. 넣으면 다른 사용자의 답변까지 함께 멈춘다.
- def: 동기 SDK(Anthropic, requests)를 그대로 쓸 때. 요청마다 별도 스레드에서 돈다.

LLM 호출에는 SDK의 timeout을 꼭 설정한다. 시간 예산이 지나면 사용자에게 대체 답변은 가지만, 이미 시작된 동기 호출은 강제로 멈출 수 없다.

user_key는 보낸 사람의 botUserKey다(없으면 빈 문자열). user_key는 로그에 남기지 않는다.
여러 사용자가 동시에 쓰므로 대화 기록 같은 상태는 user_key별로 따로 둔다. 같은 사용자가 메시지를 연달아 보내면
그 메시지들도 동시에 처리되므로, 상태를 읽고 고치는 부분은 사용자별 잠금 안에 둔다. 그러면 같은 사용자의
메시지는 받은 순서대로 하나씩, 다른 사용자끼리는 병렬로 처리된다.
- async def reply: `async with user_lock(user_key):`
  async라도 읽기와 저장 사이에 await(LLM 호출)가 끼면 다른 메시지가 끼어들 수 있으므로 잠금이 필요하다.
- def reply: `with user_thread_lock(user_key):`
  시간 초과 뒤에도 동기 호출은 계속 돌며 잠금을 쥐고 있으므로, 그 사용자의 다음 메시지는 그동안 기다린다.
잠금을 기다리는 시간도 bot.py의 시간 예산에 포함된다.
"""

import asyncio
import logging
import os
import threading

import kakao
import llm
import policy
import problem

logger = logging.getLogger("agent")

# 첫 인사: 사용자의 첫 메시지 답장 앞에 붙는 소개 글과, 뒤에 붙는 추천 질문(최대 3개, kakao.suggestion_card).
# 대회 당일 problem.py에서 채운다. INTRO가 비어 있으면 첫 인사를 하지 않는다.
INTRO = problem.INTRO
EXAMPLE_QUESTIONS: list[str] = problem.EXAMPLE_QUESTIONS

# LLM에 이어 보내는 이전 대화 수(사용자·에이전트 메시지 합계)와, 한 메시지에서 LLM에 보내는 최대 글자 수
HISTORY_MAX_MESSAGES = 6
CONTENT_MAX_CHARS = 2000
# 카카오가 사진을 보내면 content가 이 주소로 시작한다(docs/kakao_bot_api.md "실측 결과")
IMAGE_URL_PREFIX = "https://talk.kakaocdn.net/"

# LLM 상태 확인: alerts.watch_health가 시작할 때와 5분마다 부르고, 연속으로 실패하면 디스코드로 알린다.
# 메시지가 오기 전에 API 키 오류, 서버 장애를 알기 위해서다. 크레딧을 쓰지 않도록 모델 목록 조회처럼
# 생성하지 않는 호출만 넣는다(그래서 크레딧 소진은 잡지 못한다). 실패하면 예외를 그대로 낸다. None이면 확인하지 않는다.
# reply와 같은 규칙으로 실행되므로 reply와 같은 비동기 클라이언트(llm.client)를 쓴다.
health_check = llm.health_check

# 사용자별 잠금. 사용자 수만큼만 생기므로 지우지 않는다.
_async_locks: dict[str, asyncio.Lock] = {}
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def user_lock(user_key: str) -> asyncio.Lock:
    """async def reply용 사용자별 잠금. reply는 bot.py의 전용 이벤트 루프 하나에서만 돌므로 dict 접근은 안전하다."""
    return _async_locks.setdefault(user_key, asyncio.Lock())


def user_thread_lock(user_key: str) -> threading.Lock:
    """def reply용 사용자별 잠금. 요청마다 다른 스레드에서 부르므로 dict 접근도 잠근다."""
    with _thread_locks_guard:
        return _thread_locks.setdefault(user_key, threading.Lock())


# 첫 인사를 한 사용자. 프로그램을 다시 시작하면 비워지므로 그 뒤 첫 메시지에 다시 인사한다.
_greeted: set[str] = set()


def with_intro(user_key: str, answer: str | dict) -> str | dict:
    """그 사용자의 첫 메시지라면 답장 앞에 INTRO를, 남는 자리에 추천 질문 카드를 붙인다. 사용자별 잠금 안에서 부른다.

    이미 대화한 계정으로 채팅방을 나갔다 들어오기, 삭제 후 새로 시작, 채널 친구 추가, 차단 후 해제를 해도
    입장 이벤트(CHAT_JOIN, ENTRANCE)가 오지 않아(10/28 실측, 처음 들어오는 계정은 확인 안 함) 첫 메시지로 판단한다.
    말풍선은 3개까지라 답변을 먼저 지킨다: 답변이 이미 3개면 첫 인사를 하지 않고, 2개면 추천 질문 카드를 뺀다.
    """
    if not INTRO or not user_key or user_key in _greeted:
        return answer
    body = kakao.limit_response(answer)["template"]["outputs"] if isinstance(answer, dict) else [kakao.simple_text(answer)]
    if len(body) >= kakao.MAX_OUTPUTS:
        logger.warning("답변 말풍선이 %d개라 첫 인사를 붙이지 않습니다", len(body))
        return answer
    _greeted.add(user_key)
    outputs = [kakao.simple_text(INTRO), *body]
    if EXAMPLE_QUESTIONS and len(outputs) < kakao.MAX_OUTPUTS:
        outputs.append(kakao.suggestion_card(EXAMPLE_QUESTIONS))
    return kakao.skill_response(outputs)


# 사용자별 상태: {"history": [{"role", "content"}...], "pending": 추가 질문 중인 의도와 값}. 재시작하면 비워진다.
_states: dict[str, dict] = {}


def new_state() -> dict:
    return {"history": [], "pending": None}


def _messages(state: dict, content: str) -> list[dict]:
    """이전 대화 몇 개와 이번 메시지. API는 user로 시작해야 하므로 앞쪽 assistant는 뺀다."""
    history = state["history"][-HISTORY_MAX_MESSAGES:]
    while history and history[0]["role"] != "user":
        history = history[1:]
    return [*history, {"role": "user", "content": content[:CONTENT_MAX_CHARS]}]


async def skeleton_reply(content: str, user_key: str) -> str | dict:
    if not content:
        return "메시지를 입력해 주세요."
    async with user_lock(user_key):
        # botUserKey가 없으면 사용자를 구별할 수 없으므로 상태를 남기지 않는다(다른 사람과 대화 기록이 섞이지 않게).
        state = _states.setdefault(user_key, new_state()) if user_key else new_state()
        if content.startswith(IMAGE_URL_PREFIX):
            decision = policy.Decision(policy.ANSWER, problem.TEMPLATES["image"], "image")
            seconds = 0.0
        else:
            extraction = await llm.extract(policy.system_prompt(problem), _messages(state, content), policy.schema(problem))
            decision = policy.decide(extraction, state, problem)
            seconds = extraction.seconds
            # 실패한 대화는 기록하지 않는다. 다음 메시지가 실패 안내를 맥락으로 삼지 않게 한다.
            if decision.action != policy.ERROR:
                state["history"] += [{"role": "user", "content": content[:CONTENT_MAX_CHARS]},
                                     {"role": "assistant", "content": decision.text}]
                del state["history"][:-HISTORY_MAX_MESSAGES]
        logger.info("agent action=%s reason=%s llm=%.1fs", decision.action, decision.reason, seconds)
        return with_intro(user_key, decision.text)


# 예선 문제(고객 응대)는 cs_agent.py가 처리한다. AGENT_MODE=skeleton이면 위 골격(skeleton_reply)을 쓴다(골격 시험은 tests/conftest.py가 이렇게 둔다).
MODE = os.environ.get("AGENT_MODE", "cs")
if MODE == "cs":
    import cs_agent  # noqa: E402  cs_agent가 이 모듈의 user_lock을 쓰므로 정의가 끝난 뒤에 불러온다


async def reply(content: str, user_key: str) -> str | dict:
    if MODE == "cs":
        return await cs_agent.reply(content, user_key)
    return await skeleton_reply(content, user_key)


# 프롬프트 캐시 예열(CS 에이전트만). gateway가 시작할 때와 주기적으로 부른다. None이면 하지 않는다.
# cs_agent를 먼저 import하면 순환 중이라 cs_agent.warm_cache가 아직 없으므로, 부를 때 찾는다.
if MODE == "cs":
    async def warm_cache() -> None:
        await cs_agent.warm_cache()
else:
    warm_cache = None
