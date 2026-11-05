"""이벤트 수신 방식(웹훅/게이트웨이)과 무관한 메시지 처리."""

import asyncio
import copy
import inspect
import logging
import os
import threading
import time

import httpx

import config  # noqa: F401  .env 로드 (아래 os.getenv보다 먼저)
import agent
import alerts
import kakao

logger = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# 에이전트 처리 시간 상한(초). 실제 제한은 콜백 토큰 만료 시각에서 거꾸로 계산한 값과 비교해 더 짧은 쪽이다.
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "240"))
# 콜백 토큰은 받은 뒤 5분이 지나면 만료된다.
CALLBACK_TOKEN_TTL_SEC = 300
# 에이전트 종료 후 콜백을 보낼 시간으로 남겨 두는 여유(초)
CALLBACK_MARGIN_SEC = 20
CALLBACK_RETRIES = 2

TIMEOUT_MESSAGE = "답변을 만드는 데 시간이 너무 오래 걸렸어요. 다시 한번 말씀해 주세요."
ERROR_MESSAGE = "답변을 만드는 중에 문제가 생겼어요. 잠시 후 다시 시도해 주세요."
# 카드가 거절됐는데 글자로 옮길 내용도 없을 때
CARD_FAILED_MESSAGE = "답변을 카드로 보여 드리지 못했어요. 다시 한번 말씀해 주세요."

# 에이전트 루프가 이 시간 이상 응답하지 않으면 경고한다.
LAG_CHECK_SEC = 1.0
LAG_WARN_SEC = 2.0


class AgentRunner:
    """agent.reply를 메인 이벤트 루프 밖에서 실행한다.

    메인 루프는 게이트웨이 연결(HEARTBEAT, 수신)을 맡고 있어서, 에이전트가 루프를 멈추면 연결이 끊긴다.
    - async def reply: 별도 스레드의 전용 이벤트 루프에서 실행한다. 루프가 계속 유지되므로
      모듈 수준에 만든 비동기 클라이언트(AsyncAnthropic 등)를 그대로 재사용할 수 있다.
      안에서 실수로 동기 호출을 해도 이 전용 루프만 멈추고, 멈춤은 경고 로그로 알린다.
    - def reply (동기 함수): 요청마다 데몬 스레드를 하나씩 띄워 실행한다. 스레드 풀을 쓰지 않는 이유는
      멈춘 호출이 풀을 다 차지해 다른 요청까지 막거나, 프로세스 종료를 붙잡는 일을 막기 위해서다.
    """

    def __init__(self, lag_check_sec=LAG_CHECK_SEC, lag_warn_sec=LAG_WARN_SEC):
        self.lag_check_sec = lag_check_sec
        self.lag_warn_sec = lag_warn_sec
        self._loop = None
        self._lock = threading.Lock()

    def _agent_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="agent-loop", daemon=True).start()
                threading.Thread(target=self._watch_lag, args=(loop,), name="agent-watch", daemon=True).start()
                self._loop = loop
            return self._loop

    def _watch_lag(self, loop: asyncio.AbstractEventLoop) -> None:
        """전용 루프에 주기적으로 신호를 넣어 본다. 멈춘 루프 안에서는 감시할 수 없으므로 별도 스레드에서 돈다."""
        while True:
            answered = threading.Event()
            started = time.monotonic()
            loop.call_soon_threadsafe(answered.set)
            if not answered.wait(self.lag_warn_sec):
                logger.warning(
                    "agent loop blocked for over %.0fs: async def reply 안에서 동기 호출(time.sleep, 동기 SDK 등)을 "
                    "쓰고 있는지 확인. 동기 SDK를 쓰려면 reply를 일반 def로 바꾼다", self.lag_warn_sec,
                )
                answered.wait()
                logger.warning("agent loop unblocked after %.1fs", time.monotonic() - started)
            time.sleep(self.lag_check_sec)

    async def run(self, content: str, user_key: str = "") -> str | dict:
        return await self.call(agent.reply, content, user_key)

    async def call(self, fn, *args):
        """agent의 함수(reply, health_check)를 실행한다. async 함수는 모듈 수준 비동기 클라이언트를 함께 쓰도록 같은 전용 루프에서 돈다.
        취소되면(타임아웃) 전용 루프의 태스크도 취소된다. 동기 함수는 끝날 때까지 스레드에서 계속 돈다."""
        if inspect.iscoroutinefunction(fn):
            future = asyncio.run_coroutine_threadsafe(fn(*args), self._agent_loop())
            return await asyncio.wrap_future(future)
        return await self._run_in_daemon_thread(fn, *args)

    @staticmethod
    async def _run_in_daemon_thread(fn, *args):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def settle(result, error):
            if future.done():  # 이미 타임아웃으로 취소됐다.
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)

        def target():
            try:
                result, error = fn(*args), None
            except BaseException as e:  # CancelledError 등도 넘겨야 호출 쪽이 시간 예산까지 기다리지 않는다.
                result, error = None, e if isinstance(e, Exception) else RuntimeError(f"agent raised {e!r}")
            try:
                loop.call_soon_threadsafe(settle, result, error)
            except RuntimeError:  # 메인 루프가 이미 닫혔다.
                pass

        threading.Thread(target=target, name="agent", daemon=True).start()
        return await future


runner = AgentRunner()


def redact(payload):
    """로그용 사본. 콜백 토큰은 남기지 않는다."""
    if not isinstance(payload, dict) or not isinstance(payload.get("d"), dict):
        return payload
    safe = copy.deepcopy(payload)
    if "callbackToken" in safe["d"]:
        safe["d"]["callbackToken"] = "***"
    return safe


async def handle_event(client: httpx.AsyncClient, payload, received_at: float) -> None:
    """DISPATCH 이벤트를 처리한다. received_at은 이벤트를 받은 시각(time.monotonic())."""
    event = kakao.parse_event(payload)
    if not event:
        return
    event_type, data = event
    callback_token = data.get("callbackToken")
    if event_type != "MESSAGE_CREATE" or not isinstance(callback_token, str) or not callback_token:
        # 답장하지 않는 이벤트도 종류는 남긴다(입장 이벤트가 실제로 오는지 운영 로그로 확인하려고). 내용과 사용자 키는 남기지 않는다.
        logger.info(
            "event ignored type=%s seq=%s callback_token=%s",
            event_type, payload.get("s", "-"), "yes" if isinstance(callback_token, str) and callback_token else "no",
        )
        return
    content = data.get("content")
    text = content.strip() if isinstance(content, str) else ""
    user_key = data.get("botUserKey")
    ref = f"seq={payload.get('s', '-')} id={data.get('id', '-')}"
    await handle_message(client, text, callback_token, received_at, ref, user_key if isinstance(user_key, str) else "")


async def handle_message(
    client: httpx.AsyncClient,
    content: str,
    callback_token: str,
    received_at: float,
    ref: str = "-",
    user_key: str = "",
) -> None:
    """에이전트 답변을 만들어 보내고, 메시지마다 요약 로그를 한 줄 남긴다(대화 내용과 사용자 키는 남기지 않는다).

    user_key는 보낸 사람의 botUserKey다. 에이전트가 사용자별 대화 상태를 나눌 때 쓴다.
    에이전트는 문자열(말풍선 하나) 또는 kakao.skill_response(...)로 만든 SkillResponse를 돌려준다.
    """
    expires_at = received_at + CALLBACK_TOKEN_TTL_SEC
    budget = min(AGENT_TIMEOUT_SEC, expires_at - CALLBACK_MARGIN_SEC - time.monotonic())
    agent_started = time.monotonic()
    try:
        if budget <= 0:
            raise TimeoutError
        answer = await asyncio.wait_for(runner.run(content, user_key), budget)
        response, result = to_skill_response(answer), "ok"
    except TimeoutError:
        logger.warning("agent timeout (budget %.0fs)", budget)
        response, result = kakao.text_response(TIMEOUT_MESSAGE), "timeout"
    except Exception:
        logger.exception("agent failed")
        response, result = kakao.text_response(ERROR_MESSAGE), "error"
    agent_sec = time.monotonic() - agent_started

    sent = await send_reply(client, callback_token, response, expires_at, text_fallback(response))
    alerts.record_callback(bool(sent))
    logger.info(
        "message %s result=%s chars=%d agent=%.1fs total=%.1fs callback=%s",
        ref, result, len(content), agent_sec, time.monotonic() - received_at, sent or "failed",
    )


def text_fallback(response: dict) -> dict | None:
    """카드가 든 답장이 거절됐을 때 대신 보낼 글자 답장. 이미 글자 답장이면 None(다시 보내도 같다)."""
    try:
        text = kakao.plain_text(response)
    except Exception:  # 형식이 틀린 카드에서 글자를 못 뽑아도 답장은 보내야 한다.
        logger.exception("plain text fallback failed")
        text = ""
    fallback = kakao.text_response(text or CARD_FAILED_MESSAGE)
    return None if fallback == response else fallback


# 예선 심사는 SimpleText가 아닌 말풍선(카드·버튼 등)이 오면 그 케이스를 끝내고 거기까지만 평가한다(문제 풀이 가이드).
# 그래서 에이전트가 카드를 돌려줘도 글자만 모아 말풍선 하나로 보낸다.
SIMPLE_TEXT_ONLY = True


def to_skill_response(answer) -> dict:
    """에이전트 답변을 SkillResponse로 바꾼다. 형식이 틀리면 예외를 내서 안내 문구로 답하게 한다."""
    if isinstance(answer, str):
        return kakao.text_response(answer)
    if isinstance(answer, dict):
        response = kakao.limit_response(answer)
        outputs = response["template"]["outputs"]
        if SIMPLE_TEXT_ONLY and any(set(output) != {"simpleText"} for output in outputs):
            logger.warning("SimpleText가 아닌 말풍선을 글자 말풍선 하나로 바꿉니다")
            return kakao.text_response(kakao.plain_text(response) or CARD_FAILED_MESSAGE)
        return response
    raise TypeError(f"agent.reply는 str 또는 SkillResponse dict를 돌려줘야 합니다: {type(answer).__name__}")


async def send_reply(
    client: httpx.AsyncClient,
    callback_token: str,
    skill_response: dict,
    expires_at: float,
    fallback: dict | None = None,
) -> str | None:
    """콜백 토큰은 일회용이므로, 요청이 아예 전송되지 않은 연결 실패만 재시도한다.

    카카오가 답장을 거절하면(이미 사용된 토큰 제외) fallback(글자 답장)이 있을 때 한 번 더 보낸다.
    성공하면 "ok" 또는 "fallback", 실패하면 None을 돌려준다.
    """
    for attempt in range(CALLBACK_RETRIES + 1):
        try:
            result = await kakao.send_callback(client, BOT_TOKEN, callback_token, skill_response)
            # 200이어도 응답의 건수로 실제 전달 여부를 판단한다. 예: {"totalCount": 1, "sendCount": 1, "errorCount": 0}
            if isinstance(result, dict) and (result.get("errorCount") or result.get("sendCount") == 0):
                if result.get("sendCount") == 0 and fallback is not None:
                    logger.error("kakao callback not delivered, retrying as plain text: %s", result)
                    return "fallback" if await send_reply(client, callback_token, fallback, expires_at) else None
                logger.error("kakao callback not delivered: %s", result)
                return None
            return "ok"
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            if attempt == CALLBACK_RETRIES or time.monotonic() + 1 >= expires_at:
                logger.error("kakao callback connect failed: %s", e)
                return None
            logger.warning("kakao callback connect failed, retrying: %s", e)
            await asyncio.sleep(1)
        except kakao.CallbackError as e:
            if e.code == kakao.CALLBACK_CODE_ALREADY_USED:
                logger.warning("kakao callback token already used: 같은 봇이 다른 곳(로컬/EC2)에서도 실행 중인지 확인")
                return None
            if fallback is not None:
                logger.error("kakao callback rejected, retrying as plain text: %s", e)
                return "fallback" if await send_reply(client, callback_token, fallback, expires_at) else None
            logger.error("kakao callback failed: %s", e)
            return None
        except Exception:
            logger.exception("kakao callback failed")
            return None
    return None
