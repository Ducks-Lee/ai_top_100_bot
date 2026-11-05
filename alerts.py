"""프로그램이 살아 있는데 답장을 못 하는 상황을 디스코드로 알린다.

- 답장(콜백)이 연속으로 실패: 호출 허용 IP 미등록, 같은 봇 중복 실행 등
- 게이트웨이 접속이 오래 끊김: 토큰 오류(4001), 네트워크 장애 등
- LLM 상태 확인(agent.health_check)이 연속으로 실패: API 키 오류, LLM 서버 장애 등
- 캐시 예열 호출(agent.warm_cache)이 연속으로 실패: 실제 토큰을 쓰는 호출이라 크레딧 소진도 잡는다
- LLM 호출이 크레딧 부족으로 거절됨(record_llm_result): 즉시, 같은 알림은 CREDIT_ALERT_REPEAT_SEC에 한 번
프로그램이 죽은 경우는 systemd OnFailure(notify_failure.py)가 알린다. 웹훅 URL과 재시도 규칙은 그쪽과 같다.
"""

import asyncio
import logging
import socket
import time

import httpx

import notify_failure

logger = logging.getLogger("alerts")

# 답장이 이 횟수만큼 연속으로 실패하면 알린다.
CALLBACK_FAILURE_ALERT_COUNT = 3
# 게이트웨이 접속이 이 시간 넘게 끊겨 있으면 알린다. 접속 실패만 반복되는 동안(4001 등)은 처음 끊긴 시각부터 센다.
DISCONNECT_ALERT_SEC = 300
CONNECTION_CHECK_SEC = 30
# LLM 상태 확인 간격. 실패하면 HEALTH_RETRY_SEC 뒤에 다시 확인해, 시작 직후의 키 오류를 1분 안에 알린다.
HEALTH_CHECK_SEC = 300
HEALTH_RETRY_SEC = 60
HEALTH_FAILURE_ALERT_COUNT = 2
HEALTH_TIMEOUT_SEC = 30
# 크레딧 소진 알림을 다시 보내는 최소 간격. 소진 중에는 메시지마다 실패하므로 폭주하지 않게 한다.
CREDIT_ALERT_REPEAT_SEC = 600
# 종료할 때 보내는 중인 알림을 기다리는 시간. 답장 drain(280초)과 합쳐도 systemd TimeoutStopSec(300초) 안에 끝나게 짧게 둔다.
CLOSE_GRACE_SEC = 5

_tasks = set()
# 알림은 하나씩 순서대로 보낸다(실패 알림보다 복구 알림이 먼저 도착하지 않게). 이벤트 루프마다 하나 만든다.
_lock = None
_lock_loop = None


def _send_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


async def _post(content: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=notify_failure.COMMAND_TIMEOUT_SEC) as client:
        return await client.post(notify_failure.WEBHOOK_URL, json=notify_failure.webhook_body(content))


async def send(content: str) -> bool:
    """웹훅 URL이 없으면 보내지 않는다. 비동기로 보내므로 종료할 때 취소할 수 있다."""
    if not notify_failure.WEBHOOK_URL:
        return False
    content = f"{content}\n서버: {socket.gethostname()}"
    async with _send_lock():
        for attempt in range(1, notify_failure.SEND_ATTEMPTS + 1):
            try:
                ok, delay, error = notify_failure.check_response(await _post(content))
            except httpx.HTTPError as e:
                ok, delay, error = notify_failure.check_response(None, e)
            if ok is not None:
                if not ok:
                    logger.error("디스코드 알림 전송 실패: %s", error)
                return ok
            logger.warning("디스코드 알림 전송 실패 (%d/%d): %s", attempt, notify_failure.SEND_ATTEMPTS, error)
            if attempt < notify_failure.SEND_ATTEMPTS:
                await asyncio.sleep(delay)
    return False


# 알림을 보내는 루프(gateway의 메인 루프). 에이전트 전용 루프에서 생긴 알림도 이 루프로 넘겨 한 줄로 보낸다.
_main_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """gateway.main이 시작할 때 부른다."""
    global _main_loop
    _main_loop = loop


def _spawn_local(content: str) -> None:
    task = asyncio.create_task(send(content))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _spawn_send(content: str) -> None:
    """알림을 별도 태스크로 보낸다. 다른 루프(에이전트 전용 루프)에서 부르면 메인 루프로 넘긴다(전송 순서와 종료 처리를 한 루프에 모은다)."""
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if _main_loop is not None and current is not _main_loop and not _main_loop.is_closed():
        _main_loop.call_soon_threadsafe(_spawn_local, content)
    else:
        _spawn_local(content)


async def close(timeout: float = CLOSE_GRACE_SEC) -> None:
    """종료할 때 보내는 중인 알림을 잠시 기다리고, 남은 것은 취소한다.
    다른 루프 소속 태스크(bind_loop 전에 에이전트 루프에서 만든 것)는 기다릴 수 없으므로 뺀다."""
    loop = asyncio.get_running_loop()
    pending = {task for task in _tasks if task.get_loop() is loop}
    if not pending:
        return
    _, still = await asyncio.wait(pending, timeout=timeout)
    for task in still:
        task.cancel()
    await asyncio.gather(*still, return_exceptions=True)


class CallbackWatch:
    """답장 결과를 차례로 받아, 연속 실패가 기준에 닿으면 한 번 알리고 다시 성공하면 복구를 알린다."""

    def __init__(self, threshold: int = CALLBACK_FAILURE_ALERT_COUNT):
        self.threshold = threshold
        self.failures = 0
        self.alerted = False

    def record(self, sent: bool) -> str | None:
        """보낼 알림 문구를 돌려준다. 보낼 것이 없으면 None."""
        if sent:
            recovered = self.alerted
            self.failures, self.alerted = 0, False
            if recovered:
                return "**[답장 복구]** 카카오톡 답장이 다시 전달되고 있습니다."
            return None
        self.failures += 1
        if self.failures >= self.threshold and not self.alerted:
            self.alerted = True
            return (
                f"**[답장 실패]** 카카오톡 답장이 {self.failures}번 연속 전달되지 않았습니다. "
                "메시지는 받고 있지만 사용자에게 답이 가지 않습니다.\n"
                "로그의 `kakao callback` 오류를 확인하세요. 흔한 원인: 호출 허용 IP 미등록, "
                "같은 봇이 다른 곳에서도 실행 중(`token already used`)"
            )
        return None


callback_watch = CallbackWatch()


class HealthWatch:
    """LLM 상태 확인 결과를 차례로 받아, 연속 실패가 기준에 닿으면 한 번 알리고 다시 성공하면 복구를 알린다."""

    def __init__(self, threshold: int = HEALTH_FAILURE_ALERT_COUNT, what: str = "LLM API 상태 확인"):
        self.threshold = threshold
        self.what = what  # 알림 문구에 쓰는 확인 이름
        self.failures = 0
        self.alerted = False

    def record(self, error: str | None) -> str | None:
        """error가 None이면 성공. 보낼 알림 문구를 돌려준다. 보낼 것이 없으면 None."""
        if error is None:
            recovered = self.alerted
            self.failures, self.alerted = 0, False
            return f"**[LLM 복구]** {self.what}이 다시 성공했습니다." if recovered else None
        self.failures += 1
        if self.failures >= self.threshold and not self.alerted:
            self.alerted = True
            return (
                f"**[LLM 이상]** {self.what}이 {self.failures}번 연속 실패했습니다. "
                "메시지가 오면 오류 안내 문구로 답하게 됩니다.\n"
                f"마지막 오류: `{error}`\n"
                "인증 오류면 .env의 API 키 확인, 크레딧 부족이면 콘솔에서 충전, 연결 오류나 5xx면 LLM 회사 상태 페이지 확인"
            )
        return None


class CreditWatch:
    """LLM 호출 결과를 받아, 크레딧 부족 오류면 바로 알리고(CREDIT_ALERT_REPEAT_SEC에 한 번) 다음 성공 때 복구를 알린다.
    크레딧 부족이 아닌 오류는 무시한다(다른 오류는 HealthWatch와 답장 로그가 맡는다)."""

    def __init__(self, repeat_sec: float = CREDIT_ALERT_REPEAT_SEC, clock=time.monotonic):
        self.repeat_sec = repeat_sec
        self.clock = clock
        self.last_alert = None  # 마지막 소진 알림 시각. None이면 소진 상태가 아니다.

    def record(self, error: str | None) -> str | None:
        """error가 None이면 성공. 보낼 알림 문구를 돌려준다. 보낼 것이 없으면 None."""
        if error is None:
            if self.last_alert is None:
                return None
            self.last_alert = None
            return "**[크레딧 복구]** Anthropic API 호출이 다시 성공했습니다."
        if "credit balance" not in error.lower():
            return None
        now = self.clock()
        if self.last_alert is not None and now - self.last_alert < self.repeat_sec:
            return None
        self.last_alert = now
        return (
            "**[크레딧 소진]** Anthropic 크레딧이 부족해 고객 메시지에 답하지 못하고 있습니다(안내 문구만 나감).\n"
            "콘솔 Plans & Billing에서 충전하세요. 충전 뒤 첫 성공 호출에서 복구 알림이 옵니다."
        )


credit_watch = CreditWatch()


def record_llm_result(error: BaseException | None) -> None:
    """cs_agent가 LLM 호출마다 부른다(성공이면 None). 에이전트 전용 루프에서 불리므로 알림은 그 루프의 태스크로 보낸다."""
    message = credit_watch.record(None if error is None else str(error))
    if message:
        (logger.info if error is None else logger.error)("%s", message.splitlines()[0])
        _spawn_send(message)


def _describe(error: BaseException) -> str:
    """오류 종류와 앞부분만 남기고, 혹시 섞인 비밀값은 가린다."""
    # 먼저 자르면 경계에 걸린 비밀값의 앞부분이 가려지지 않으므로, 가린 다음 자른다.
    text = notify_failure.redact([f"{type(error).__name__}: {error}"])[0]
    return text[:300].replace("`", "'")


async def watch_health(check, stop: asyncio.Event, check_sec: float = HEALTH_CHECK_SEC,
                       retry_sec: float = HEALTH_RETRY_SEC, timeout: float = HEALTH_TIMEOUT_SEC,
                       watch: HealthWatch | None = None, label: str = "LLM health check") -> None:
    """check()(agent.health_check나 agent.warm_cache를 메인 루프 밖에서 실행하는 코루틴 함수)를 시작할 때와 주기적으로 부른다.
    label은 로그 이름이다."""
    watch = watch or HealthWatch()
    first = True
    while not stop.is_set():
        try:
            await asyncio.wait_for(check(), timeout)
            error = None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  어떤 오류든 상태 확인 실패로 센다
            error = _describe(e) if not isinstance(e, TimeoutError) else f"{timeout:.0f}초 안에 응답 없음"
        if error:
            logger.warning("%s failed: %s", label, error)
        elif first:
            logger.info("%s ok", label)
        first = False
        message = watch.record(error)
        if message:
            (logger.error if error else logger.info)("%s", message.splitlines()[0])
            await send(message)
        try:
            await asyncio.wait_for(stop.wait(), retry_sec if error else check_sec)
        except TimeoutError:
            pass


def record_callback(sent: bool) -> None:
    """bot.handle_message가 답장마다 부른다. 알림은 별도 태스크로 보내 답장 처리를 늦추지 않는다."""
    message = callback_watch.record(sent)
    if message:
        logger.error("%s", message.splitlines()[0])
        _spawn_send(message)


async def watch_connection(gateway, stop: asyncio.Event, alert_after: float = DISCONNECT_ALERT_SEC,
                           check_sec: float = CONNECTION_CHECK_SEC) -> None:
    """gateway.disconnected_since를 주기적으로 보고, 오래 끊겨 있으면 알린다. 다시 연결되면 복구를 알린다."""
    alerted_since = None  # 알림을 보낸 끊김의 시작 시각
    while not stop.is_set():
        since = gateway.disconnected_since
        now = time.monotonic()
        # 값이 바뀌었으면(None 포함) 알린 끊김은 끝난 것이다. 검사 사이에 복구됐다가 다시 끊긴 경우도 여기서 잡는다.
        if alerted_since is not None and since != alerted_since:
            message = f"**[접속 복구]** 카카오 게이트웨이에 다시 연결됐습니다(약 {(now - alerted_since) / 60:.0f}분 끊김)."
            alerted_since = None
            logger.info("%s", message)
            await send(message)
        if since is not None and alerted_since is None and now - since >= alert_after:
            alerted_since = since
            message = (
                f"**[접속 끊김]** 카카오 게이트웨이에 {(now - since) / 60:.0f}분 넘게 연결되지 않았습니다. "
                "그동안 온 메시지는 받지 못합니다.\n"
                f"마지막 사유: `{gateway.last_error or '알 수 없음'}`\n"
                "`close 4001`이면 BOT_TOKEN 확인, 연결 오류면 EC2 인터넷과 카카오 디벨로퍼스 웹훅 URL이 비어 있는지 확인"
            )
            logger.error("%s", message.splitlines()[0])
            await send(message)
        try:
            await asyncio.wait_for(stop.wait(), check_sec)
        except TimeoutError:
            pass
