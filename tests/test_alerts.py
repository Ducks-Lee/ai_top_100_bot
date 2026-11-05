import asyncio
import json
import time

import pytest

import httpx

import alerts
import bot
import kakao
from test_gateway import handshake, message, run_scenario


@pytest.fixture
def sent_alerts(monkeypatch):
    """디스코드 전송을 가로채 보낸 문구를 기록한다."""
    messages = []

    async def fake_post(content):
        messages.append(content)
        return ok_response()

    monkeypatch.setattr(alerts.notify_failure, "WEBHOOK_URL", URL)
    monkeypatch.setattr(alerts, "_post", fake_post)
    monkeypatch.setattr(alerts, "callback_watch", alerts.CallbackWatch(threshold=3))
    return messages


URL = "https://discord.com/api/webhooks/1/secret-webhook-token"


def ok_response(status=204, headers=None):
    return httpx.Response(status, headers=headers, request=httpx.Request("POST", URL))


def test_callback_watch_alerts_once_per_failure_streak():
    watch = alerts.CallbackWatch(threshold=3)
    results = [watch.record(False) for _ in range(5)]
    assert results[:2] == [None, None]
    assert "[답장 실패]" in results[2] and "3번" in results[2]
    assert results[3:] == [None, None]  # 같은 연속 실패에서는 다시 알리지 않는다
    assert "[답장 복구]" in watch.record(True)
    assert watch.record(True) is None


def test_callback_success_resets_streak():
    watch = alerts.CallbackWatch(threshold=3)
    assert [watch.record(s) for s in (False, False, True, False, False)] == [None] * 5


def test_handle_message_reports_callback_failures(sent_alerts, monkeypatch):
    async def failing_callback(client, bot_token, callback_token, skill_response):
        raise kakao.CallbackError("403 ip not allowed", -401)

    monkeypatch.setattr(kakao, "send_callback", failing_callback)

    async def scenario():
        for _ in range(3):
            await bot.handle_message(None, "안녕", "cb", time.monotonic())
        await asyncio.gather(*alerts._tasks)

    asyncio.run(scenario())
    assert len(sent_alerts) == 1
    assert "[답장 실패]" in sent_alerts[0]


def test_no_webhook_url_sends_nothing(sent_alerts, monkeypatch):
    monkeypatch.setattr(alerts.notify_failure, "WEBHOOK_URL", "")
    assert asyncio.run(alerts.send("x")) is False
    assert sent_alerts == []


def test_send_retries_transient_errors_without_leaking_url(monkeypatch, caplog):
    responses = [httpx.ConnectError(f"cannot reach {URL}"), ok_response(503, {"Retry-After": "0"}), ok_response()]
    calls = []

    async def flaky_post(content):
        calls.append(content)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(alerts.notify_failure, "WEBHOOK_URL", URL)
    monkeypatch.setattr(alerts.notify_failure, "RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(alerts, "_post", flaky_post)
    assert asyncio.run(alerts.send("x")) is True
    assert len(calls) == 3
    assert "secret-webhook-token" not in caplog.text


def test_alerts_are_sent_in_order(monkeypatch):
    """먼저 생긴 알림이 늦게 끝나도 나중 알림이 앞질러 도착하지 않는다."""
    delivered = []

    async def slow_first_post(content):
        if "실패" in content:
            await asyncio.sleep(0.05)
        delivered.append(content.split()[0])
        return ok_response()

    monkeypatch.setattr(alerts.notify_failure, "WEBHOOK_URL", URL)
    monkeypatch.setattr(alerts, "_post", slow_first_post)

    async def scenario():
        alerts._spawn_send("실패")
        alerts._spawn_send("복구")
        await asyncio.gather(*alerts._tasks)

    asyncio.run(scenario())
    assert delivered == ["실패", "복구"]


def test_close_waits_briefly_then_cancels(monkeypatch):
    async def hanging_post(content):
        await asyncio.sleep(10)

    monkeypatch.setattr(alerts.notify_failure, "WEBHOOK_URL", URL)
    monkeypatch.setattr(alerts, "_post", hanging_post)

    async def scenario():
        alerts._spawn_send("x")
        started = time.monotonic()
        await alerts.close(timeout=0.05)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 1
    assert not alerts._tasks


class FakeGateway:
    def __init__(self):
        self.disconnected_since = time.monotonic()
        self.last_error = "close 4001 Unauthorized"


def test_long_disconnect_alerts_then_recovery(sent_alerts):
    async def scenario():
        gw, stop = FakeGateway(), asyncio.Event()
        task = asyncio.create_task(alerts.watch_connection(gw, stop, alert_after=0.05, check_sec=0.01))
        while not sent_alerts:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # 끊긴 상태가 이어져도 다시 알리지 않는다
        gw.disconnected_since = None
        while len(sent_alerts) < 2:
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert len(sent_alerts) == 2
    assert "[접속 끊김]" in sent_alerts[0] and "close 4001" in sent_alerts[0]
    assert "[접속 복구]" in sent_alerts[1]


def test_new_outage_between_checks_is_alerted_again(sent_alerts):
    """알린 끊김이 검사 사이에 복구됐다가 다시 끊기면, 복구를 알리고 새 끊김도 다시 센다."""
    async def scenario():
        gw, stop = FakeGateway(), asyncio.Event()
        task = asyncio.create_task(alerts.watch_connection(gw, stop, alert_after=0.05, check_sec=0.01))
        while not sent_alerts:
            await asyncio.sleep(0.01)
        gw.disconnected_since = time.monotonic()  # 복구된 줄 모른 채 새로 끊김
        while len(sent_alerts) < 3:
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert ["[접속 끊김]" in sent_alerts[0], "[접속 복구]" in sent_alerts[1], "[접속 끊김]" in sent_alerts[2]] == [True] * 3


def test_short_disconnect_does_not_alert(sent_alerts):
    async def scenario():
        gw, stop = FakeGateway(), asyncio.Event()
        task = asyncio.create_task(alerts.watch_connection(gw, stop, alert_after=10, check_sec=0.01))
        await asyncio.sleep(0.05)
        gw.disconnected_since = None
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(scenario())
    assert sent_alerts == []


def test_gateway_tracks_disconnected_since_and_last_error():
    """연결되면 disconnected_since가 비고, 끊기면 처음 끊긴 시각과 사유가 남는다."""
    async def first(ws, state):
        await handshake(ws)
        for _ in range(100):  # 봇이 READY를 처리할 때까지 기다린다
            if state["gw"].disconnected_since is None:
                break
            await asyncio.sleep(0.01)
        state["connected_since"] = state["gw"].disconnected_since
        await ws.close(1001, "restart")

    async def second(ws, state):
        await ws.close(4001, "Unauthorized")

    def until(state):
        return state["connections"] >= 2 and state["gw"].last_error.startswith("close 4001")

    state = asyncio.run(run_scenario([first, second], until=until, auth_retry_sec=60))
    gw = state["gw"]
    assert state["connected_since"] is None
    assert gw.disconnected_since is not None
    assert gw.last_error == "close 4001 Unauthorized"


def test_gateway_keeps_first_disconnect_time_while_failing():
    async def refuse(ws, state):
        state.setdefault("since", []).append(state["gw"].disconnected_since)
        await ws.close(1011)

    state = asyncio.run(run_scenario([refuse, refuse, refuse], until=lambda s: s["connections"] >= 3))
    assert len(set(state["since"])) == 1  # 한 번도 연결되지 않았으므로 시작 시각 그대로


def test_rejected_resume_keeps_disconnect_time():
    """RESUME을 보낸 것만으로는 연결된 것으로 보지 않는다. 4009로 거절되면 처음 끊긴 시각이 남는다."""
    async def first(ws, state):
        await handshake(ws)
        await ws.send(json.dumps(message(1)))
        await asyncio.sleep(0.05)
        await ws.close(1001)

    async def reject_resume(ws, state):
        frame = await handshake(ws)
        state["lost_at"] = state["gw"].disconnected_since
        assert frame["op"] == 6
        await ws.close(4009, "Session unavailable")

    async def identify(ws, state):
        await handshake(ws)
        state["after_resume_rejected"] = state["gw"].disconnected_since
        await ws.wait_closed()

    state = asyncio.run(run_scenario([first, reject_resume, identify], until=lambda s: "after_resume_rejected" in s))
    assert state["lost_at"] is not None
    assert state["after_resume_rejected"] == state["lost_at"]


def test_accepted_resume_clears_disconnect_time_on_first_frame():
    async def first(ws, state):
        await handshake(ws)
        await ws.send(json.dumps(message(1)))
        await asyncio.sleep(0.05)
        await ws.close(1001)

    async def accept_resume(ws, state):
        await handshake(ws)
        state["after_resume_sent"] = state["gw"].disconnected_since
        await ws.send(json.dumps(message(2)))  # RESUME이 받아들여져 이벤트가 온다
        for _ in range(100):  # 봇이 프레임을 처리할 때까지 기다린다
            if state["gw"].disconnected_since is None:
                break
            await asyncio.sleep(0.01)
        state["after_first_frame"] = state["gw"].disconnected_since
        await ws.wait_closed()

    state = asyncio.run(run_scenario([first, accept_resume], until=lambda s: "after_first_frame" in s))
    assert state["after_resume_sent"] is not None
    assert state["after_first_frame"] is None


def test_health_watch_alerts_after_consecutive_failures_and_recovery():
    watch = alerts.HealthWatch(threshold=2)
    assert watch.record("AuthenticationError: invalid x-api-key") is None
    first = watch.record("AuthenticationError: invalid x-api-key")
    assert "[LLM 이상]" in first and "invalid x-api-key" in first
    assert watch.record("again") is None  # 같은 연속 실패에서는 다시 알리지 않는다
    assert "[LLM 복구]" in watch.record(None)
    assert watch.record(None) is None


def test_watch_health_retries_quickly_and_reports(sent_alerts):
    results = [RuntimeError("boom"), RuntimeError("boom"), None]
    calls = []

    async def check():
        calls.append(time.monotonic())
        result = results.pop(0) if results else None
        if result:
            raise result

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(alerts.watch_health(check, stop, check_sec=10, retry_sec=0.01, timeout=1))
        while len(sent_alerts) < 2:
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert len(calls) == 3  # 실패 뒤에는 retry_sec 간격으로 다시 확인한다
    assert "[LLM 이상]" in sent_alerts[0] and "RuntimeError: boom" in sent_alerts[0]
    assert "[LLM 복구]" in sent_alerts[1]


def test_watch_health_counts_timeout_as_failure(sent_alerts):
    async def hanging_check():
        await asyncio.sleep(10)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(alerts.watch_health(hanging_check, stop, retry_sec=0.01, timeout=0.02))
        while not sent_alerts:
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert "응답 없음" in sent_alerts[0]


def test_watch_health_redacts_secrets(sent_alerts, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-very-secret-value")

    async def check():
        raise RuntimeError("bad key sk-very-secret-value")

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(alerts.watch_health(check, stop, retry_sec=0.01, timeout=1))
        while not sent_alerts:
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert "sk-very-secret-value" not in sent_alerts[0]


def test_runner_call_runs_async_check_on_agent_loop():
    """async health_check는 reply와 같은 전용 루프에서 돌아 같은 비동기 클라이언트를 쓸 수 있다."""
    runner = bot.AgentRunner()
    loops = []

    async def check():
        loops.append(asyncio.get_running_loop())

    async def scenario():
        await runner.call(check)
        await runner.call(check)
        return asyncio.get_running_loop()

    main_loop = asyncio.run(scenario())
    assert loops[0] is loops[1] is runner._agent_loop()
    assert loops[0] is not main_loop


def test_runner_call_runs_sync_check_in_thread():
    runner = bot.AgentRunner()

    def check():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        asyncio.run(runner.call(check))


def test_gateway_main_starts_health_watch_only_when_check_defined(monkeypatch):
    import gateway

    started = []

    async def fake_watch_health(check, stop):
        started.append(check)
        await check()  # bot.runner.call(agent.health_check)로 연결됐는지 본다
        await stop.wait()

    async def fake_run(self, stop):
        await asyncio.sleep(0.05)

    checked = []

    async def health_check():
        checked.append(True)

    monkeypatch.setattr(bot, "BOT_TOKEN", "token")
    monkeypatch.setattr(gateway.Gateway, "run", fake_run)
    monkeypatch.setattr(alerts, "watch_health", fake_watch_health)

    monkeypatch.setattr(bot.agent, "health_check", None)
    assert asyncio.run(gateway.main()) == 0
    assert started == []

    monkeypatch.setattr(bot.agent, "health_check", health_check)
    assert asyncio.run(gateway.main()) == 0
    assert len(started) == 1 and checked == [True]


def test_describe_redacts_before_truncating(monkeypatch):
    secret = "sk-test-secret-value-1234"
    monkeypatch.setenv("LLM_API_KEY", secret)
    text = alerts._describe(RuntimeError("x" * 280 + secret))
    assert secret[:10] not in text


def test_credit_watch_ignores_other_errors_and_repeats_after_interval():
    now = [0.0]
    watch = alerts.CreditWatch(repeat_sec=600, clock=lambda: now[0])
    assert watch.record("RateLimitError: too many requests") is None
    assert watch.record(None) is None  # 소진 상태가 아니었으면 복구 알림도 없다
    assert "[크레딧 소진]" in watch.record("Your credit balance is too low")
    now[0] = 599
    assert watch.record("credit balance is too low") is None
    now[0] = 600
    assert "[크레딧 소진]" in watch.record("credit balance is too low")
    assert "[크레딧 복구]" in watch.record(None)


def test_health_watch_names_the_check():
    watch = alerts.HealthWatch(threshold=1, what="LLM 캐시 예열 호출")
    assert "LLM 캐시 예열 호출이 1번 연속 실패" in watch.record("boom")
    assert "LLM 캐시 예열 호출이 다시 성공" in watch.record(None)


def test_send_from_other_loop_goes_to_main_loop(monkeypatch):
    import threading
    sent_on = []

    async def fake_send(content):
        sent_on.append((content, threading.current_thread().name))

    monkeypatch.setattr(alerts, "send", fake_send)

    async def main():
        alerts.bind_loop(asyncio.get_running_loop())
        worker = threading.Thread(target=lambda: asyncio.run(_from_other_loop()), name="other")
        worker.start()
        while not sent_on:
            await asyncio.sleep(0.01)
        worker.join()

    async def _from_other_loop():
        alerts._spawn_send("x")

    try:
        asyncio.run(asyncio.wait_for(main(), 5))
    finally:
        alerts.bind_loop(None)
    assert sent_on == [("x", "MainThread")]
