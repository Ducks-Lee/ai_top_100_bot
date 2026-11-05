"""게이트웨이(웹소켓) 방식 봇 워커. 실행: python gateway.py

카카오톡 서버에 웹소켓으로 상시 접속해 이벤트를 받는다. 도메인, HTTPS, 인바운드 포트가 필요 없다.
명세: docs/kakao_bot_api.md
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time

import httpx

import config  # noqa: F401  .env 로드 (아래 os.getenv보다 먼저)
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

import alerts
import bot

logger = logging.getLogger("gateway")

GATEWAY_URL = os.getenv("KAKAO_GATEWAY_URL", "wss://bot-gateway.kakao.com/gateway")
LOG_PAYLOAD = os.getenv("LOG_PAYLOAD") == "1"

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_READY = 3
OP_RESUME = 6
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

CLOSE_AUTH_FAILED = 4001
CLOSE_REPLACED = 4004
CLOSE_SESSION_EXPIRED = 4009
# 다시 붙어도 해결되지 않는 종료 코드 (우리가 보낸 프레임 형식이 틀린 경우)
FATAL_CLOSE_CODES = {
    4003: "유효하지 않은 페이로드",
}
# 인증 실패(4001)는 종료하지 않고 이만큼 간격을 두고 재시도한다.
# 카카오 쪽 일시 오류로 4001이 와도 봇이 꺼진 채 남지 않게 하기 위해서다. 토큰이 정말 틀렸다면 로그만 쌓인다.
AUTH_RETRY_SEC = 300
# 다른 곳에서 같은 봇이 접속해 연결을 빼앗긴 경우(4004), 이만큼 기다렸다가 다시 접속한다.
# 종료해 버리면 로컬 테스트 한 번에 운영 봇이 꺼진 채 남기 때문이다.
REPLACED_RETRY_SEC = 60
# systemd가 재시작하지 않도록 하는 종료 코드 (deploy/ai-top-100.service의 RestartPreventExitStatus)
EXIT_FATAL = 2

HANDSHAKE_TIMEOUT_SEC = 10
# 연결을 닫을 때 상대의 close 응답을 기다리는 시간 (라이브러리 기본값 10초는 종료를 늦춘다)
CLOSE_TIMEOUT_SEC = 2
# 네트워크가 돌아온 뒤 오래 기다리지 않도록 짧게 둔다. 봇 하나의 재접속이라 서버 부담은 작다.
BACKOFF_MAX_SEC = 10
# 이 시간 이상 유지된 연결이 끊기면 백오프를 처음부터 다시 센다.
STABLE_CONNECTION_SEC = 30
# 종료할 때 진행 중인 답장을 기다리는 시간. systemd TimeoutStopSec(300초)보다 여유 있게 짧게 둔다.
SHUTDOWN_GRACE_SEC = 280


class FatalGatewayError(Exception):
    pass


async def cancel_and_wait(tasks) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_until_stopped(coro, stop: asyncio.Event) -> None:
    """stop이 설정되면 coro를 취소한다. coro가 먼저 예외로 끝나면 그 예외를 올린다."""
    task = asyncio.create_task(coro)
    stopped = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        await cancel_and_wait([task, stopped])
    if not task.cancelled() and task.exception():
        raise task.exception()


def pick_error(done) -> BaseException:
    """동시에 끝난 태스크 중 서버 close code가 담긴 예외를 우선한다."""
    errors = [t.exception() for t in done if not t.cancelled() and t.exception()]
    closed = [e for e in errors if isinstance(e, ConnectionClosed)]
    if closed:
        return closed[0]
    return errors[0] if errors else ConnectionError("connection ended")


class Gateway:
    def __init__(
        self, token, handler, url=GATEWAY_URL, backoff_base=1.0,
        replaced_retry_sec=REPLACED_RETRY_SEC, auth_retry_sec=AUTH_RETRY_SEC,
    ):
        """handler(payload, received_at)는 DISPATCH마다 별도 태스크로 실행된다."""
        self.token = token
        self.handler = handler
        self.url = url
        self.backoff_base = backoff_base
        self.replaced_retry_sec = replaced_retry_sec
        self.auth_retry_sec = auth_retry_sec
        self.session_id = None
        self.last_seq = 0
        self.tasks = set()
        self._connected_at = None
        self._acked = True
        # 연결이 없는 상태가 시작된 시각(time.monotonic()). 연결돼 있으면 None. alerts.watch_connection이 본다.
        self.disconnected_since = time.monotonic()
        # 마지막으로 연결이 끊긴 이유 (알림용)
        self.last_error = ""

    async def run(self, stop: asyncio.Event) -> None:
        backoff = self.backoff_base
        while not stop.is_set():
            self._connected_at = None
            resuming = self.session_id is not None
            retry_now = False
            try:
                await run_until_stopped(self._connect_once(), stop)
            except ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                logger.warning("connection closed: code=%s reason=%s", code, e.rcvd.reason if e.rcvd else "")
                self.last_error = f"close {code} {e.rcvd.reason if e.rcvd else ''}".strip()
                if code in FATAL_CLOSE_CODES:
                    raise FatalGatewayError(f"close {code}: {FATAL_CLOSE_CODES[code]}") from e
                if code == CLOSE_AUTH_FAILED:
                    logger.error("인증 실패 (BOT_TOKEN 확인), %ds 뒤 재시도", self.auth_retry_sec)
                    self.session_id, self.last_seq = None, 0
                    self._connected_at = None
                    backoff = self.auth_retry_sec
                if code == CLOSE_REPLACED:
                    logger.error("다른 연결로 대체됨 (같은 봇이 다른 곳에서 실행 중인지 확인), %ds 뒤 재접속", self.replaced_retry_sec)
                    self.session_id, self.last_seq = None, 0
                    self._connected_at = None
                    backoff = self.replaced_retry_sec
                if code == CLOSE_SESSION_EXPIRED:
                    self.session_id, self.last_seq = None, 0
                    # RESUME이 거절된 것이면 서버와는 통신이 되는 상태이므로 기다리지 않고 바로 IDENTIFY한다.
                    retry_now = resuming
            except (OSError, TimeoutError, InvalidHandshake) as e:
                logger.warning("gateway connection failed: %r", e)
                self.last_error = repr(e)

            # 반복문 한 바퀴는 연결이 끝나야 돌아온다. 이미 끊겨 있던 중이면 처음 끊긴 시각을 유지한다.
            if self.disconnected_since is None:
                self.disconnected_since = time.monotonic()
            if stop.is_set():
                break
            # 연결이 한동안 유지됐을 때만 백오프를 초기화한다. 붙자마자 끊기는 장애가 반복되면 간격이 계속 늘어난다.
            if self._connected_at is not None and time.monotonic() - self._connected_at >= STABLE_CONNECTION_SEC:
                backoff = self.backoff_base
            if retry_now:
                logger.warning("RESUME 거절: 끊긴 동안의 메시지는 복구되지 않을 수 있음, 바로 IDENTIFY")
                continue
            logger.info("reconnecting in %.1fs", backoff)
            try:
                await asyncio.wait_for(stop.wait(), backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, BACKOFF_MAX_SEC)

    async def _connect_once(self) -> None:
        """연결 하나가 끊길 때까지 실행하고, 끊긴 이유를 예외로 올린다."""
        async with connect(self.url, open_timeout=HANDSHAKE_TIMEOUT_SEC, close_timeout=CLOSE_TIMEOUT_SEC) as ws:
            hello = await self._recv_json(ws)
            if hello.get("op") != OP_HELLO:
                raise ConnectionError(f"expected HELLO, got op={hello.get('op')}")
            interval = hello["d"]["heartbeat_interval"] / 1000

            if self.session_id:
                logger.info("RESUME session=%s seq=%d", self.session_id, self.last_seq)
                await ws.send(json.dumps({
                    "op": OP_RESUME,
                    "d": {"token": self.token, "session_id": self.session_id, "seq": self.last_seq},
                }))
            else:
                await ws.send(json.dumps({"op": OP_IDENTIFY, "d": {"token": self.token}}))
                ready = await self._recv_json(ws)
                if ready.get("op") != OP_READY:
                    raise ConnectionError(f"expected READY, got op={ready.get('op')}")
                self.session_id, self.last_seq = ready["d"]["session_id"], 0
                logger.info("READY bot=%s session=%s", ready["d"].get("bot", {}).get("id"), self.session_id)
                self.disconnected_since = None
            # RESUME은 응답이 따로 없고 거절되면 4009로 끊긴다. 그래서 RESUME 뒤에는 첫 프레임을 받았을 때 연결된 것으로 본다(_receive).
            self._connected_at = time.monotonic()

            # heartbeat_interval보다 약간 짧게 보낸다.
            tasks = [
                asyncio.create_task(self._heartbeat(ws, interval * 0.9)),
                asyncio.create_task(self._receive(ws)),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                await cancel_and_wait(tasks)
            raise pick_error(done)

    async def _recv_json(self, ws) -> dict:
        frame = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT_SEC))
        if not isinstance(frame, dict):
            raise ConnectionError("frame is not a JSON object")
        return frame

    async def _heartbeat(self, ws, interval: float) -> None:
        self._acked = True
        while True:
            await asyncio.sleep(interval)
            if not self._acked:
                raise ConnectionError("HEARTBEAT_ACK not received")
            self._acked = False
            await ws.send(json.dumps({"op": OP_HEARTBEAT}))

    async def _receive(self, ws) -> None:
        while True:
            raw = await ws.recv()
            received_at = time.monotonic()
            try:
                frame = json.loads(raw)
            except ValueError:
                logger.warning("non-JSON frame ignored")
                continue
            if not isinstance(frame, dict):
                continue
            self.disconnected_since = None

            op = frame.get("op")
            if op == OP_HEARTBEAT_ACK:
                self._acked = True
            elif op == OP_DISPATCH:
                if isinstance(frame.get("s"), int):
                    self.last_seq = frame["s"]
                if LOG_PAYLOAD:
                    logger.info("dispatch: %s", json.dumps(bot.redact(frame), ensure_ascii=False)[:2000])
                self._spawn(self.handler(frame, received_at))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.error("event handler failed", exc_info=task.exception())

    async def drain(self, timeout: float) -> None:
        """진행 중인 답장이 끝날 때까지 기다린다."""
        if self.tasks:
            logger.info("waiting for %d in-flight replies", len(self.tasks))
            await asyncio.wait(set(self.tasks), timeout=timeout)


async def main() -> int:
    log_file = config.setup_logging()
    if log_file:
        logger.info("log file: %s", log_file)
    if not bot.BOT_TOKEN:
        logger.error("BOT_TOKEN이 없습니다. %s에 BOT_TOKEN을 넣으세요 (.env.example 참고)", config.ENV_FILE)
        return EXIT_FATAL

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    exit_code = 0
    alerts.bind_loop(loop)  # 에이전트 루프에서 생긴 알림(크레딧 등)도 이 루프에서 순서대로 보낸다
    async with httpx.AsyncClient(timeout=10) as client:
        gateway = Gateway(bot.BOT_TOKEN, lambda payload, t: bot.handle_event(client, payload, t))
        # 접속이 오래 끊기면 디스코드로 알린다(DISCORD_WEBHOOK_URL이 있을 때).
        watchers = [asyncio.create_task(alerts.watch_connection(gateway, stop))]
        # agent.health_check를 채웠으면 LLM API 상태도 주기적으로 확인해 알린다.
        if bot.agent.health_check is not None:
            watchers.append(asyncio.create_task(
                alerts.watch_health(lambda: bot.runner.call(bot.agent.health_check), stop)
            ))
        # CS 에이전트면 프롬프트 캐시를 시작할 때와 주기적으로 데운다(동시 첫 호출의 캐시 쓰기 비용을 막고, 실제 호출이라 크레딧 소진도 잡는다).
        if getattr(bot.agent, "warm_cache", None) is not None:
            cs = bot.agent.cs_agent
            watchers.append(asyncio.create_task(alerts.watch_health(
                lambda: bot.runner.call(bot.agent.warm_cache), stop, check_sec=cs.WARM_CHECK_SEC,
                timeout=cs.LLM_TIMEOUT_SEC + 15, watch=alerts.HealthWatch(what="LLM 캐시 예열 호출"), label="cache warm",
            )))
            # 봇이 집계한 Anthropic 사용액을 주기적으로 디스코드에 보고한다(spend.py, 추정치).
            watchers.append(asyncio.create_task(cs.spend.watch_report(alerts.send, stop)))
        try:
            await gateway.run(stop)
        except FatalGatewayError as e:
            logger.error("%s", e)
            exit_code = EXIT_FATAL
        finally:
            await cancel_and_wait(watchers)
            await gateway.drain(SHUTDOWN_GRACE_SEC)
            await alerts.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
