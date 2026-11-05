"""로컬 가짜 게이트웨이 서버로 프로토콜 흐름과 재연결을 검증한다."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

import gateway

TOKEN = "bot_tok"


def message(s):
    return {"op": 0, "s": s, "t": "MESSAGE_CREATE", "d": {"content": f"m{s}", "callbackToken": f"cb{s}"}}


async def recv_json(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 2))


async def handshake(ws, session_id="sess_1", interval_ms=41250):
    """HELLO를 보내고 IDENTIFY 또는 RESUME을 받는다. IDENTIFY면 READY까지 보낸다."""
    await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": interval_ms}}))
    frame = await recv_json(ws)
    if frame["op"] == 2:
        await ws.send(json.dumps({"op": 3, "d": {"session_id": session_id, "bot": {"id": "bot1"}}}))
    return frame


async def run_scenario(scripts, *, until, timeout=5, replaced_retry_sec=60, auth_retry_sec=300, backoff_base=0.01):
    """scripts[i]는 i번째 연결을 처리하는 코루틴. until(state)가 참이 되면 게이트웨이를 멈춘다."""
    state = {"handled": [], "connections": 0}
    stop = asyncio.Event()

    async def server_handler(ws):
        idx = state["connections"]
        state["connections"] += 1
        if idx < len(scripts):
            await scripts[idx](ws, state)
        else:
            await ws.wait_closed()  # 남는 연결은 닫힐 때까지 붙잡아 둔다.

    async def handler(payload, received_at):
        state["handled"].append(payload)

    async with serve(server_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        gw = gateway.Gateway(
            TOKEN, handler, url=f"ws://127.0.0.1:{port}", backoff_base=backoff_base,
            replaced_retry_sec=replaced_retry_sec, auth_retry_sec=auth_retry_sec,
        )
        state["gw"] = gw

        async def watcher():
            while not until(state):
                await asyncio.sleep(0.01)
            stop.set()

        await asyncio.wait_for(asyncio.gather(gw.run(stop), watcher()), timeout)
        await gw.drain(1)
    return state


def test_identify_ready_dispatch_and_heartbeat():
    async def script(ws, state):
        state["first"] = await handshake(ws, interval_ms=50)
        await ws.send(json.dumps(message(1)))
        state["heartbeat"] = await recv_json(ws)
        await ws.send(json.dumps({"op": 11}))
        await ws.wait_closed()

    state = asyncio.run(run_scenario([script], until=lambda s: s["handled"] and "heartbeat" in s))
    assert state["first"] == {"op": 2, "d": {"token": TOKEN}}
    assert state["heartbeat"] == {"op": 1}
    assert state["handled"] == [message(1)]
    assert state["gw"].session_id == "sess_1"
    assert state["gw"].last_seq == 1


def test_server_restart_resumes_with_last_seq():
    async def first(ws, state):
        await handshake(ws)
        await ws.send(json.dumps(message(1)))
        await ws.send(json.dumps(message(2)))
        await asyncio.sleep(0.05)
        await ws.close(1001)

    async def second(ws, state):
        state["second"] = await handshake(ws)
        await ws.wait_closed()

    state = asyncio.run(run_scenario([first, second], until=lambda s: "second" in s))
    assert state["second"] == {"op": 6, "d": {"token": TOKEN, "session_id": "sess_1", "seq": 2}}


def test_session_expired_identifies_again():
    async def first(ws, state):
        await handshake(ws)
        await ws.send(json.dumps(message(1)))
        await asyncio.sleep(0.05)
        await ws.close(4009)

    async def second(ws, state):
        state["second"] = await handshake(ws, session_id="sess_2")
        await ws.wait_closed()

    state = asyncio.run(run_scenario([first, second], until=lambda s: "second" in s and s["gw"].session_id == "sess_2"))
    assert state["second"] == {"op": 2, "d": {"token": TOKEN}}
    assert state["gw"].last_seq == 0


def test_invalid_payload_close_stops():
    async def script(ws, state):
        await handshake(ws)
        await ws.close(4003)

    with pytest.raises(gateway.FatalGatewayError):
        asyncio.run(run_scenario([script], until=lambda s: False))


def test_auth_failure_waits_then_identifies_again():
    """4001(인증 실패)은 종료하지 않고, 긴 간격을 두고 새로 IDENTIFY한다."""
    async def first(ws, state):
        await handshake(ws)
        await ws.send(json.dumps(message(1)))
        await asyncio.sleep(0.05)
        state["closed_at"] = asyncio.get_running_loop().time()
        await ws.close(4001)

    async def second(ws, state):
        state["reconnected_at"] = asyncio.get_running_loop().time()
        state["second"] = await handshake(ws, session_id="sess_2")
        await ws.wait_closed()

    async def scenario():
        return await run_scenario([first, second], until=lambda s: "second" in s, auth_retry_sec=0.3)

    state = asyncio.run(scenario())
    assert state["second"] == {"op": 2, "d": {"token": TOKEN}}
    assert state["reconnected_at"] - state["closed_at"] >= 0.3


def test_replaced_connection_waits_then_identifies_again():
    """4004(다른 연결로 대체됨)는 종료하지 않고, 기다렸다가 새로 IDENTIFY한다."""
    async def first(ws, state):
        await handshake(ws)
        await ws.send(json.dumps(message(3)))
        await asyncio.sleep(0.05)
        state["closed_at"] = asyncio.get_running_loop().time()
        await ws.close(4004)

    async def second(ws, state):
        state["reconnected_at"] = asyncio.get_running_loop().time()
        state["second"] = await handshake(ws, session_id="sess_2")
        await ws.wait_closed()

    async def scenario():
        return await run_scenario([first, second], until=lambda s: "second" in s, replaced_retry_sec=0.3)

    state = asyncio.run(scenario())
    assert state["second"] == {"op": 2, "d": {"token": TOKEN}}
    assert state["reconnected_at"] - state["closed_at"] >= 0.3


def test_missing_heartbeat_ack_reconnects():
    async def first(ws, state):
        await handshake(ws, interval_ms=30)
        await ws.wait_closed()  # HEARTBEAT에 ACK를 보내지 않는다.

    async def second(ws, state):
        state["second"] = await handshake(ws)
        await ws.wait_closed()

    state = asyncio.run(run_scenario([first, second], until=lambda s: "second" in s))
    assert state["second"]["op"] == 6


def test_connection_refused_retries_until_stop():
    async def main():
        stop = asyncio.Event()
        gw = gateway.Gateway(TOKEN, None, url="ws://127.0.0.1:9", backoff_base=0.01)
        task = asyncio.create_task(gw.run(stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(main())


def test_handler_runs_without_blocking_receive():
    """느린 답장이 다음 이벤트 수신을 막지 않아야 한다."""
    order = []

    async def script(ws, state):
        await handshake(ws)
        await ws.send(json.dumps(message(1)))
        await ws.send(json.dumps(message(2)))
        await ws.wait_closed()

    async def main():
        stop = asyncio.Event()
        release = asyncio.Event()

        async def handler(payload, received_at):
            order.append(("start", payload["s"]))
            if payload["s"] == 1:
                await release.wait()
            order.append(("end", payload["s"]))

        async with serve(lambda ws: script(ws, {}), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            gw = gateway.Gateway(TOKEN, handler, url=f"ws://127.0.0.1:{port}", backoff_base=0.01)
            task = asyncio.create_task(gw.run(stop))
            while ("end", 2) not in order:
                await asyncio.sleep(0.01)
            release.set()
            stop.set()
            await asyncio.wait_for(task, 2)
            await gw.drain(1)

    asyncio.run(asyncio.wait_for(main(), 5))
    assert order.index(("end", 2)) < order.index(("end", 1))


def test_pick_error_prefers_close_code():
    """HEARTBEAT 실패와 서버 종료가 동시에 끝나면 close code가 담긴 예외를 고른다."""
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    async def main():
        async def fail(exc):
            raise exc

        closed = ConnectionClosedError(Close(4004, "replaced"), None)
        tasks = [asyncio.create_task(fail(ConnectionError("ack"))), asyncio.create_task(fail(closed))]
        await asyncio.gather(*tasks, return_exceptions=True)
        return gateway.pick_error(set(tasks))

    assert asyncio.run(main()).rcvd.code == 4004


def test_backoff_grows_when_resume_closes_immediately():
    """붙자마자 끊기는 장애가 반복되면 재접속 간격이 늘어나야 한다."""
    times = []

    async def first(ws, state):
        await handshake(ws)
        await asyncio.sleep(0.05)
        await ws.close(1001)

    async def flapping(ws, state):
        times.append(asyncio.get_running_loop().time())
        await handshake(ws)  # RESUME을 받자마자 끊는다.
        await ws.close(1001)

    async def scenario():
        return await run_scenario([first] + [flapping] * 4, until=lambda s: len(times) >= 4)

    asyncio.run(scenario())
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps[-1] > gaps[0] * 2


def test_stop_during_handshake_returns_quickly():
    """HELLO를 기다리는 중에 종료 요청이 오면 핸드셰이크 타임아웃(10초)을 기다리지 않는다."""
    async def main():
        stop = asyncio.Event()
        sent = []

        async def silent(ws):
            async for frame in ws:
                sent.append(frame)

        async with serve(silent, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            gw = gateway.Gateway(TOKEN, None, url=f"ws://127.0.0.1:{port}", backoff_base=0.01)
            task = asyncio.create_task(gw.run(stop))
            await asyncio.sleep(0.1)
            stop.set()
            await asyncio.wait_for(task, 1)
        return sent

    assert asyncio.run(main()) == []  # IDENTIFY를 보내지 않았다.


def test_cancel_leaves_no_tasks():
    async def script(ws, state):
        await handshake(ws, interval_ms=50)
        async for frame in ws:
            if json.loads(frame)["op"] == 1:
                await ws.send(json.dumps({"op": 11}))

    async def main():
        async with serve(lambda ws: script(ws, {}), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            gw = gateway.Gateway(TOKEN, None, url=f"ws://127.0.0.1:{port}", backoff_base=0.01)
            task = asyncio.create_task(gw.run(asyncio.Event()))
            while gw._connected_at is None:
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # 테스트 서버의 Windows 소켓 수락 태스크(IocpProactor)는 게이트웨이와 무관하므로 제외한다.
            return [
                t for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and "IocpProactor" not in t.get_coro().__qualname__
            ]

    assert asyncio.run(main()) == []


def test_rejected_resume_identifies_immediately():
    """RESUME이 4009로 거절되면 백오프를 기다리지 않고 바로 IDENTIFY한다 (실제 카카오: 네트워크 끊김 뒤 "Session unavailable")."""
    async def first(ws, state):
        await handshake(ws)
        await ws.close(1001)

    async def second(ws, state):
        state["resume"] = await handshake(ws)
        state["rejected_at"] = asyncio.get_running_loop().time()
        await ws.close(4009, "Session unavailable")

    async def third(ws, state):
        state["identified_at"] = asyncio.get_running_loop().time()
        state["third"] = await handshake(ws, session_id="sess_2")
        await ws.wait_closed()

    async def scenario():
        return await run_scenario([first, second, third], until=lambda s: "third" in s, backoff_base=1.0)

    state = asyncio.run(scenario())
    assert state["resume"]["op"] == 6
    assert state["third"] == {"op": 2, "d": {"token": TOKEN}}
    assert state["identified_at"] - state["rejected_at"] < 0.5  # 백오프(2초)를 기다리지 않았다.


def test_session_expired_on_identify_still_waits_backoff():
    """IDENTIFY 연결에서 온 4009는 RESUME 거절이 아니므로 백오프를 기다린다(빠른 재시도 반복 방지)."""
    async def first(ws, state):
        await handshake(ws)
        state["closed_at"] = asyncio.get_running_loop().time()
        await ws.close(4009)

    async def second(ws, state):
        state["reconnected_at"] = asyncio.get_running_loop().time()
        state["second"] = await handshake(ws, session_id="sess_2")
        await ws.wait_closed()

    async def scenario():
        return await run_scenario([first, second], until=lambda s: "second" in s, backoff_base=0.3)

    state = asyncio.run(scenario())
    assert state["second"]["op"] == 2
    assert state["reconnected_at"] - state["closed_at"] >= 0.3
