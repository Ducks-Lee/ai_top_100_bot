import asyncio

import pytest
from fastapi.testclient import TestClient

import time

import httpx

import agent
import bot
import kakao
import llm
import main


@pytest.fixture(autouse=True)
def echo_llm(monkeypatch):
    """실제 LLM 대신 받은 글자를 그대로 돌려주는 추출 결과를 쓴다. 이 파일은 bot.py 흐름을 시험한다."""
    async def extract(system, messages, schema):
        content = messages[-1]["content"]
        data = {"intent": "other", "slots": {}, "scope": "none", "claimed_authority": "", "reply": f"받은 메시지: {content}"}
        return llm.Extraction(llm.OK, data)

    monkeypatch.setattr(llm, "extract", extract)
    monkeypatch.setattr(agent, "_states", {})
    monkeypatch.setattr(agent, "INTRO", "")  # 당일 problem.INTRO를 채워도 답장 비교가 깨지지 않게


@pytest.fixture
def sent(monkeypatch):
    """콜백 API 호출을 가로채 (callback_token, text)를 기록한다."""
    calls = []

    async def fake_send_callback(client, bot_token, callback_token, skill_response):
        calls.append((callback_token, skill_response["template"]["outputs"][0]["simpleText"]["text"]))
        return {"totalCount": 1, "sendCount": 1, "errorCount": 0}

    monkeypatch.setattr(kakao, "send_callback", fake_send_callback)
    return calls


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def message_event(content="안녕", callback_token="cb_token_xxx"):
    d = {
        "id": 558185822630215770,
        "content": content,
        "timestamp": "2026-06-09T12:34:56.789Z",
        "isChannelChatroom": False,
        "botGroupKey": "bgk_xxx",
        "botUserKey": "buk_xxx",
    }
    if callback_token is not None:
        d["callbackToken"] = callback_token
    return {"op": 0, "s": 1, "t": "MESSAGE_CREATE", "d": d}


def post_raw(client, body, headers=None):
    return client.post(
        "/kakao/webhook", content=body, headers={"content-type": "application/json", **(headers or {})}
    )


def test_health(client):
    res = client.get("/")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_message_create_replies_via_callback(client, sent):
    res = client.post("/kakao/webhook", json=message_event("안녕"))
    assert res.status_code == 204
    assert res.content == b""
    assert sent == [("cb_token_xxx", "받은 메시지: 안녕")]


def test_message_without_callback_token_is_ignored(client, sent):
    res = client.post("/kakao/webhook", json=message_event(callback_token=None))
    assert res.status_code == 204
    assert sent == []


def test_leave_event_is_acknowledged_only(client, sent):
    body = {"op": 0, "s": 4, "t": "LEAVE", "d": {"botGroupKey": "bgk_xxx", "timestamp": "2026-06-09T12:34:56.789Z"}}
    res = client.post("/kakao/webhook", json=body)
    assert res.status_code == 204
    assert sent == []


def test_invalid_json(client, sent):
    assert post_raw(client, "{").status_code == 400
    assert sent == []


@pytest.mark.parametrize(
    "body",
    [
        "null",
        "[]",
        '"hello"',
        '{"op": 1}',
        '{"op": 0, "t": "MESSAGE_CREATE", "d": "bad"}',
        '{"op": 0, "t": "MESSAGE_CREATE", "d": {"callbackToken": 123, "content": "x"}}',
    ],
)
def test_malformed_payload_is_ignored(client, sent, body):
    assert post_raw(client, body).status_code == 204
    assert sent == []


def test_non_string_content_gets_prompt(client, sent):
    body = message_event()
    body["d"]["content"] = 123
    client.post("/kakao/webhook", json=body)
    assert sent == [("cb_token_xxx", "메시지를 입력해 주세요.")]


def test_webhook_header_verification(client, sent, monkeypatch):
    monkeypatch.setattr(main, "WEBHOOK_HEADER_NAME", "X-Webhook-Secret")
    monkeypatch.setattr(main, "WEBHOOK_HEADER_VALUE", "s3cret")

    assert client.post("/kakao/webhook", json=message_event()).status_code == 401
    res = client.post("/kakao/webhook", json=message_event(), headers={"X-Webhook-Secret": "wrong"})
    assert res.status_code == 401
    assert sent == []

    res = client.post("/kakao/webhook", json=message_event(), headers={"X-Webhook-Secret": "s3cret"})
    assert res.status_code == 204
    assert len(sent) == 1


def test_agent_timeout_sends_fallback(client, sent, monkeypatch):
    async def slow_reply(content, user_key):
        await asyncio.sleep(1)
        return "늦은 답"

    monkeypatch.setattr(agent, "reply", slow_reply)
    monkeypatch.setattr(bot, "AGENT_TIMEOUT_SEC", 0.01)
    client.post("/kakao/webhook", json=message_event())
    assert sent == [("cb_token_xxx", bot.TIMEOUT_MESSAGE)]


def test_agent_error_sends_fallback(client, sent, monkeypatch):
    async def broken_reply(content, user_key):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent, "reply", broken_reply)
    client.post("/kakao/webhook", json=message_event())
    assert sent == [("cb_token_xxx", bot.ERROR_MESSAGE)]


def test_callback_failure_does_not_break_webhook(client, monkeypatch):
    async def failing_send_callback(*args):
        raise kakao.CallbackError("400 bad")

    monkeypatch.setattr(kakao, "send_callback", failing_send_callback)
    assert client.post("/kakao/webhook", json=message_event()).status_code == 204


def test_long_text_truncated():
    text = kakao.text_response("가" * 1500)["template"]["outputs"][0]["simpleText"]["text"]
    assert len(text) == kakao.MAX_TEXT_LEN


def test_skill_response_builders():
    response = kakao.skill_response(
        [
            kakao.simple_text("요약"),
            kakao.text_card("근거", "설명", [kakao.message_button("더 보기"), kakao.web_link_button("원문", "https://a.b")]),
            kakao.simple_image("https://a.b/c.png"),
        ]
    )
    assert response["version"] == "2.0"
    outputs = response["template"]["outputs"]
    assert outputs[0] == {"simpleText": {"text": "요약"}}
    card = outputs[1]["textCard"]
    assert card["buttons"][0] == {"action": "message", "label": "더 보기", "messageText": "더 보기"}
    assert card["buttons"][1] == {"action": "webLink", "label": "원문", "webLinkUrl": "https://a.b"}
    assert outputs[2]["simpleImage"]["imageUrl"] == "https://a.b/c.png"
    assert list(response["template"]) == ["outputs"]


def test_suggestion_card_sends_full_question():
    card = kakao.suggestion_card(["짧은 질문", "버튼 글자보다 훨씬 긴 추천 질문입니다", "셋", "넷"])["textCard"]
    assert card["title"] == "이렇게 물어보세요" and card["buttonLayout"] == "vertical"
    assert [b["messageText"] for b in card["buttons"]] == ["짧은 질문", "버튼 글자보다 훨씬 긴 추천 질문입니다", "셋"]
    assert len(card["buttons"][1]["label"]) == 14 and card["buttons"][1]["label"].endswith("…")
    assert all(b["action"] == "message" for b in card["buttons"])


def test_limit_response_trims_what_kakao_drops_silently(caplog):
    raw = {
        "template": {
            "outputs": [{"simpleText": {"text": "가" * 1500}}] + [{"simpleText": {"text": str(i)}} for i in range(4)],
            "quickReplies": [{"label": "다음", "action": "message", "messageText": "다음"}],
        }
    }
    limited = kakao.limit_response(raw)
    assert len(limited["template"]["outputs"]) == kakao.MAX_OUTPUTS
    assert len(limited["template"]["outputs"][0]["simpleText"]["text"]) == kakao.MAX_TEXT_LEN
    assert "quickReplies" not in limited["template"]  # 채널채팅에서 보이지 않아 뺀다
    assert limited["version"] == "2.0"
    assert len(raw["template"]["outputs"]) == 5  # 원본은 그대로
    assert "말풍선 5개" in caplog.text and "퀵리플라이는 채널채팅에서 보이지 않아" in caplog.text


@pytest.mark.parametrize(
    "outputs",
    [None, "missing_template", [], ["x"], [{}], [{"simpleText": {}}], [{"simpleText": {"text": " "}}], [{"unknownCard": {"a": 1}}],
     [{"simpleText": {"text": "a"}, "textCard": {"title": "b"}}]],
)
def test_limit_response_rejects_malformed(outputs):
    with pytest.raises(ValueError):
        if outputs is None:
            kakao.limit_response({})
        elif outputs == "missing_template":
            kakao.limit_response({"template": {}})
        else:
            kakao.limit_response({"template": {"outputs": outputs}})


def test_text_card_button_limits(caplog):
    buttons = [kakao.message_button(str(i)) for i in range(4)]
    assert len(kakao.text_card("t", "d", buttons)["textCard"]["buttons"]) == 3
    horizontal = kakao.text_card("t", "d", buttons, vertical=False)["textCard"]
    assert len(horizontal["buttons"]) == 2 and horizontal["buttonLayout"] == "horizontal"
    assert "카드 버튼 4개" in caplog.text
    long = [kakao.message_button("가나다라마바사아자차"), kakao.message_button("짧음")]
    labels = kakao.text_card("t", "d", long, vertical=False)["textCard"]["buttons"]
    assert labels[0] == {"action": "message", "label": "가나다라마바사…", "messageText": "가나다라마바사아자차"}
    assert labels[1]["label"] == "짧음"
    assert long[0]["label"] == "가나다라마바사아자차"  # 원본은 그대로
    with pytest.raises(ValueError):
        kakao.text_card("t", "d", [])


def test_agent_can_return_skill_response(monkeypatch):
    captured = []

    async def fake_send_callback(client, bot_token, callback_token, skill_response):
        captured.append(skill_response)
        return {"totalCount": 2, "sendCount": 2, "errorCount": 0}

    async def rich_reply(content, user_key):
        outputs = [kakao.simple_text(f"{i}") for i in range(5)]
        return kakao.skill_response(outputs)

    monkeypatch.setattr(kakao, "send_callback", fake_send_callback)
    monkeypatch.setattr(agent, "reply", rich_reply)
    asyncio.run(bot.handle_message(None, "안녕", "cb", time.monotonic()))
    template = captured[0]["template"]
    assert [o["simpleText"]["text"] for o in template["outputs"]] == ["0", "1", "2"]


@pytest.mark.parametrize("answer", [None, 42, ["목록"], {"template": {"outputs": []}}])
def test_invalid_agent_answer_sends_error_message(sent, monkeypatch, caplog, answer):
    async def bad_reply(content, user_key):
        return answer

    caplog.set_level("INFO", logger="bot")
    monkeypatch.setattr(agent, "reply", bad_reply)
    asyncio.run(bot.handle_message(None, "안녕", "cb", time.monotonic()))
    assert sent == [("cb", bot.ERROR_MESSAGE)]
    assert "result=error" in caplog.text


def test_send_callback_request_format():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(200, json={"totalCount": 1, "sendCount": 1, "errorCount": 0})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await kakao.send_callback(c, "bot_tok", "cb_tok", kakao.text_response("hi"))

    result = asyncio.run(run())
    assert result["sendCount"] == 1
    assert captured["url"] == "https://kapi.kakao.com/v1/bot/callback"
    assert captured["headers"]["authorization"] == "KakaoAK bot_tok"
    assert captured["headers"]["x-bot-callback-token"] == "cb_tok"
    assert b'"skillResponse"' in captured["body"]


def test_send_callback_raises_on_error():
    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"code": -870}))
        async with httpx.AsyncClient(transport=transport) as c:
            await kakao.send_callback(c, "bot_tok", "cb_tok", kakao.text_response("hi"))

    with pytest.raises(kakao.CallbackError):
        asyncio.run(run())


def test_expired_budget_skips_agent(sent, monkeypatch):
    called = []

    async def reply(content, user_key):
        called.append(content)
        return "답"

    monkeypatch.setattr(agent, "reply", reply)
    # 콜백 토큰을 받은 지 290초가 지난 상황: 남은 시간이 여유분(20초)보다 적다.
    received_at = time.monotonic() - 290
    asyncio.run(bot.handle_message(None, "안녕", "cb", received_at))
    assert called == []
    assert sent == [("cb", bot.TIMEOUT_MESSAGE)]


def test_redact_hides_callback_token():
    payload = message_event()
    safe = bot.redact(payload)
    assert safe["d"]["callbackToken"] == "***"
    assert payload["d"]["callbackToken"] == "cb_token_xxx"
    assert bot.redact("x") == "x"


def test_send_reply_retries_connect_error_only(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bot.asyncio, "sleep", lambda s: real_sleep(0))
    attempts = []

    async def flaky(client, bot_token, callback_token, skill_response):
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectError("refused")
        return {}

    monkeypatch.setattr(kakao, "send_callback", flaky)
    asyncio.run(bot.send_reply(None, "cb", {}, time.monotonic() + 300))
    assert len(attempts) == 2

    attempts.clear()

    async def rejected(client, bot_token, callback_token, skill_response):
        attempts.append(1)
        raise kakao.CallbackError("400")

    monkeypatch.setattr(kakao, "send_callback", rejected)
    asyncio.run(bot.send_reply(None, "cb", {}, time.monotonic() + 300))
    assert len(attempts) == 1


async def count_ticks_while(coro):
    """coro가 도는 동안 메인 루프가 멈추지 않았는지, 20ms 간격 틱 수로 잰다."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await coro
    finally:
        task.cancel()
    return ticks


def test_blocking_async_agent_does_not_block_main_loop(sent, monkeypatch):
    """async def reply 안의 동기 호출이 게이트웨이 연결(메인 루프)을 멈추면 안 된다."""
    async def blocking_reply(content, user_key):
        time.sleep(0.5)  # 실수로 넣은 동기 호출
        return "답"

    monkeypatch.setattr(agent, "reply", blocking_reply)
    ticks = asyncio.run(count_ticks_while(bot.handle_message(None, "안녕", "cb", time.monotonic())))
    assert ticks >= 10
    assert sent == [("cb", "답")]


def test_sync_agent_runs_in_threads(sent, monkeypatch):
    """def reply는 스레드에서 돌고, 여러 요청이 동시에 처리된다."""
    def sync_reply(content, user_key):
        time.sleep(0.3)
        return f"동기 {content}"

    monkeypatch.setattr(agent, "reply", sync_reply)

    async def two_messages():
        await asyncio.gather(
            bot.handle_message(None, "a", "cb1", time.monotonic()),
            bot.handle_message(None, "b", "cb2", time.monotonic()),
        )

    started = time.monotonic()
    ticks = asyncio.run(count_ticks_while(two_messages()))
    assert time.monotonic() - started < 0.55
    assert ticks >= 5
    assert sorted(sent) == [("cb1", "동기 a"), ("cb2", "동기 b")]


def user_message(user_key, content, callback_token):
    payload = message_event(content, callback_token)
    if user_key is None:
        del payload["d"]["botUserKey"]
    else:
        payload["d"]["botUserKey"] = user_key
    return payload


@pytest.mark.parametrize("is_async", [True, False])
def test_concurrent_users_get_their_own_user_key(sent, monkeypatch, is_async):
    """여러 사용자가 동시에 보내도 에이전트는 각 메시지를 보낸 사람의 botUserKey를 받는다."""
    def answer(content, user_key):
        time.sleep(0.2)
        return f"{user_key}:{content}"

    async def async_reply(content, user_key):
        await asyncio.sleep(0.2)
        return f"{user_key}:{content}"

    monkeypatch.setattr(agent, "reply", async_reply if is_async else answer)

    async def three_users():
        await asyncio.gather(
            bot.handle_event(None, user_message("user_a", "a", "cb1"), time.monotonic()),
            bot.handle_event(None, user_message("user_b", "b", "cb2"), time.monotonic()),
            bot.handle_event(None, user_message(None, "c", "cb3"), time.monotonic()),
        )

    started = time.monotonic()
    asyncio.run(three_users())
    assert time.monotonic() - started < 0.5
    assert sorted(sent) == [("cb1", "user_a:a"), ("cb2", "user_b:b"), ("cb3", ":c")]


@pytest.mark.parametrize("is_async", [True, False])
def test_user_lock_serializes_same_user_only(sent, monkeypatch, is_async):
    """같은 사용자의 메시지는 하나씩, 다른 사용자끼리는 병렬로 처리된다."""
    # 잠금은 테스트 사이에 남으므로 테스트마다 다른 사용자 키를 쓴다.
    same, other = f"lock_same_{is_async}", f"lock_other_{is_async}"
    spans = {}

    async def async_reply(content, user_key):
        async with agent.user_lock(user_key):
            started = time.monotonic()
            await asyncio.sleep(0.2)
            spans[content] = (started, time.monotonic())
            return content

    def sync_reply(content, user_key):
        with agent.user_thread_lock(user_key):
            started = time.monotonic()
            time.sleep(0.2)
            spans[content] = (started, time.monotonic())
            return content

    monkeypatch.setattr(agent, "reply", async_reply if is_async else sync_reply)

    async def messages():
        await asyncio.gather(
            bot.handle_message(None, "a1", "cb1", time.monotonic(), user_key=same),
            bot.handle_message(None, "a2", "cb2", time.monotonic(), user_key=same),
            bot.handle_message(None, "b1", "cb3", time.monotonic(), user_key=other),
        )

    started = time.monotonic()
    asyncio.run(messages())
    elapsed = time.monotonic() - started
    assert sorted(sent) == [("cb1", "a1"), ("cb2", "a2"), ("cb3", "b1")]
    first, second = sorted([spans["a1"], spans["a2"]])
    assert second[0] >= first[1]  # 같은 사용자: 앞 메시지가 끝난 뒤 시작
    assert spans["b1"][0] < first[1]  # 다른 사용자: 기다리지 않음
    assert 0.38 <= elapsed < 0.6


def test_async_user_lock_released_after_timeout(sent, monkeypatch):
    """시간 초과로 취소된 메시지가 잠금을 쥔 채 남지 않아, 같은 사용자의 다음 메시지가 처리된다."""
    async def reply(content, user_key):
        async with agent.user_lock(user_key):
            await asyncio.sleep(5 if content == "slow" else 0)
            return content

    monkeypatch.setattr(agent, "reply", reply)
    monkeypatch.setattr(bot, "AGENT_TIMEOUT_SEC", 0.2)
    asyncio.run(bot.handle_message(None, "slow", "cb1", time.monotonic(), user_key="lock_timeout"))
    asyncio.run(bot.handle_message(None, "fast", "cb2", time.monotonic(), user_key="lock_timeout"))
    assert sent == [("cb1", bot.TIMEOUT_MESSAGE), ("cb2", "fast")]


def test_blocking_agent_timeout_sends_fallback_on_time(sent, monkeypatch):
    """에이전트가 멈춰 있어도 시간 예산이 지나면 바로 대체 답변을 보낸다."""
    async def blocking_reply(content, user_key):
        time.sleep(0.6)
        return "늦은 답"

    monkeypatch.setattr(agent, "reply", blocking_reply)
    monkeypatch.setattr(bot, "AGENT_TIMEOUT_SEC", 0.1)
    started = time.monotonic()
    asyncio.run(bot.handle_message(None, "안녕", "cb", time.monotonic()))
    assert time.monotonic() - started < 0.4
    assert sent == [("cb", bot.TIMEOUT_MESSAGE)]


def test_blocked_agent_loop_is_logged_while_blocked(monkeypatch, caplog):
    """멈춘 동안에 경고가 나와야 한다(풀린 뒤가 아니라)."""
    runner = bot.AgentRunner(lag_check_sec=0.05, lag_warn_sec=0.1)
    ended = []

    async def blocking_reply(content, user_key):
        time.sleep(0.5)
        ended.append(time.time())
        return "답"

    monkeypatch.setattr(agent, "reply", blocking_reply)
    asyncio.run(runner.run("안녕", "buk"))
    warned = [r.created for r in caplog.records if "agent loop blocked" in r.getMessage()]
    assert warned and warned[0] < ended[0]


def test_blocked_sync_agent_does_not_hold_process_exit():
    """타임아웃 뒤에도 멈춰 있는 동기 호출이 프로세스 종료를 붙잡으면 안 된다."""
    import subprocess
    import sys

    code = """
import asyncio, time, agent, bot, kakao
entered, sent = [], []
def stuck(content, user_key):
    entered.append(1)
    time.sleep(30)
async def fake_send(client, bot_token, callback_token, skill_response):
    sent.append(skill_response["template"]["outputs"][0]["simpleText"]["text"])
agent.reply = stuck
kakao.send_callback = fake_send
bot.AGENT_TIMEOUT_SEC = 0.1
asyncio.run(bot.handle_message(None, "x", "cb", time.monotonic()))
assert entered == [1] and sent == [bot.TIMEOUT_MESSAGE], (entered, sent)
"""
    started = time.monotonic()
    subprocess.run([sys.executable, "-c", code], check=True, timeout=20)
    assert time.monotonic() - started < 10


def test_sync_agent_base_exception_is_reported(sent, monkeypatch):
    """동기 reply가 CancelledError 같은 BaseException을 내도 시간 예산까지 기다리지 않고 오류 답변을 보낸다."""
    def cancelled_reply(content, user_key):
        raise asyncio.CancelledError

    monkeypatch.setattr(agent, "reply", cancelled_reply)
    monkeypatch.setattr(bot, "AGENT_TIMEOUT_SEC", 5)
    started = time.monotonic()
    asyncio.run(bot.handle_message(None, "안녕", "cb", time.monotonic()))
    assert time.monotonic() - started < 1
    assert sent == [("cb", bot.ERROR_MESSAGE)]


def test_message_summary_log(sent, caplog):
    """메시지마다 결과·처리 시간·콜백 성공 여부를 한 줄 남기고, 대화 내용은 남기지 않는다."""
    caplog.set_level("INFO", logger="bot")
    payload = message_event("비밀 이야기")
    asyncio.run(bot.handle_event(None, payload, time.monotonic()))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("message ")]
    assert len(lines) == 1
    line = lines[0]
    assert "seq=1 id=558185822630215770" in line
    assert "result=ok" in line and "callback=ok" in line and "chars=6" in line
    assert "비밀 이야기" not in caplog.text
    assert "cb_token_xxx" not in caplog.text
    assert "buk_xxx" not in caplog.text


def test_message_summary_log_records_failures(monkeypatch, caplog):
    caplog.set_level("INFO", logger="bot")

    async def broken_reply(content, user_key):
        raise RuntimeError("boom")

    async def rejected(*args):
        raise kakao.CallbackError("400")

    monkeypatch.setattr(agent, "reply", broken_reply)
    monkeypatch.setattr(kakao, "send_callback", rejected)
    asyncio.run(bot.handle_message(None, "안녕", "cb", time.monotonic(), "seq=7 id=1"))
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("message "))
    assert "seq=7 id=1 result=error" in line and "callback=failed" in line


def test_setup_logging_writes_file():
    import logging
    import shutil
    import tempfile
    from pathlib import Path

    import config

    # 이 PC에서는 pytest의 tmp_path 폴더(pytest-of-<사용자>)에 접근 권한이 없어 tempfile을 쓴다.
    tmp_path = Path(tempfile.mkdtemp())
    root = logging.getLogger()
    saved = root.handlers[:], root.level, logging.getLogger("httpx").level
    try:
        log_file = config.setup_logging(str(tmp_path / "logs"))
        logging.getLogger("bot").info("파일 기록 확인")
        for h in root.handlers:
            h.flush()
        assert log_file == tmp_path / "logs" / "bot.log"
        assert "파일 기록 확인" in log_file.read_text(encoding="utf-8")
    finally:
        for h in root.handlers:
            h.close()
        root.handlers[:], level, httpx_level = saved
        root.setLevel(level)
        logging.getLogger("httpx").setLevel(httpx_level)
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_callback_200_with_error_count_is_failure(monkeypatch, caplog):
    """200이어도 errorCount가 있거나 sendCount가 0이면 실패로 기록한다."""
    caplog.set_level("INFO", logger="bot")

    async def not_delivered(*args):
        return {"totalCount": 1, "sendCount": 0, "errorCount": 1}

    monkeypatch.setattr(kakao, "send_callback", not_delivered)
    asyncio.run(bot.handle_message(None, "안녕", "cb", time.monotonic(), "seq=8 id=2"))
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("message "))
    assert "result=ok" in line and "callback=failed" in line


def test_webhook_app_keeps_existing_log_capture(client, sent, caplog):
    """웹훅 앱을 띄워도 이미 설정된 로깅(pytest 캡처)을 지우지 않는다."""
    caplog.set_level("INFO", logger="bot")
    client.post("/kakao/webhook", json=message_event())
    assert any(r.getMessage().startswith("message ") for r in caplog.records)


def test_callback_already_used_is_logged_as_duplicate_bot(monkeypatch, caplog):
    """-881(이미 사용된 콜백 토큰)은 같은 봇이 두 곳에서 실행 중이라는 신호로 알린다."""
    async def used(*args):
        raise kakao.CallbackError('400 {"code":-881}', kakao.CALLBACK_CODE_ALREADY_USED)

    monkeypatch.setattr(kakao, "send_callback", used)
    fallback = kakao.text_response("글자")
    assert asyncio.run(bot.send_reply(None, "cb", {}, time.monotonic() + 300, fallback)) is None
    records = [r for r in caplog.records if "already used" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelname == "WARNING" and records[0].exc_info is None


def test_send_callback_error_carries_kakao_code():
    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"msg": "이미 사용된 콜백 토큰입니다.", "code": -881}))
        async with httpx.AsyncClient(transport=transport) as c:
            await kakao.send_callback(c, "bot_tok", "cb_tok", kakao.text_response("hi"))

    with pytest.raises(kakao.CallbackError) as e:
        asyncio.run(run())
    assert e.value.code == -881


def test_plain_text_collects_text_from_cards():
    response = kakao.skill_response([
        kakao.simple_text("요약"),
        kakao.text_card("근거", "설명", [kakao.message_button("더 보기")]),
        {"carousel": {"type": "basicCard", "items": [{"title": "A", "description": "a"}, {"title": "B"}]}},
    ])
    assert kakao.plain_text(response) == "요약\n\n근거\n\n설명\n\nA\n\na\n\nB"
    list_card = {"template": {"outputs": [{"listCard": {"header": {"title": "목록"}, "items": [{"title": "1", "description": "하나"}]}}]}}
    assert kakao.plain_text(list_card) == "목록\n\n1 - 하나"
    assert kakao.plain_text({"template": {"outputs": [{"simpleImage": {"imageUrl": "x"}}]}}) == ""


def rich_agent(monkeypatch):
    async def reply(content, user_key):
        return kakao.skill_response(
            [kakao.simple_text("요약"), {"basicCard": {"title": "제목", "description": "설명"}}]  # thumbnail 빠짐
        )

    monkeypatch.setattr(agent, "reply", reply)


@pytest.mark.parametrize("rejection", ["400", "200 sendCount=0"])
def test_rejected_card_is_resent_as_plain_text(monkeypatch, caplog, rejection):
    monkeypatch.setattr(bot, "SIMPLE_TEXT_ONLY", False)  # 카드 거절 뒤 글자 재전송 경로를 본다
    """카카오가 카드 답장을 거절하면 같은 토큰으로 글자 답장을 한 번 더 보낸다."""
    calls = []

    async def fake_send_callback(client, bot_token, callback_token, skill_response):
        calls.append(skill_response)
        if len(calls) == 1:
            if rejection == "400":
                raise kakao.CallbackError('400 {"code":-2}', -2)
            return {"totalCount": 2, "sendCount": 0, "errorCount": 2}
        return {"totalCount": 1, "sendCount": 1, "errorCount": 0}

    caplog.set_level("INFO", logger="bot")
    monkeypatch.setattr(kakao, "send_callback", fake_send_callback)
    rich_agent(monkeypatch)
    asyncio.run(bot.handle_message(None, "안녕", "cb", time.monotonic()))
    assert len(calls) == 2
    assert calls[1] == kakao.text_response("요약\n\n제목\n\n설명")
    assert "callback=fallback" in caplog.text


def test_rejected_fallback_is_not_retried_again(monkeypatch, caplog):
    monkeypatch.setattr(bot, "SIMPLE_TEXT_ONLY", False)  # 카드 거절 뒤 글자 재전송 경로를 본다
    calls = []

    async def always_reject(client, bot_token, callback_token, skill_response):
        calls.append(skill_response)
        raise kakao.CallbackError("400 bad", -2)

    caplog.set_level("INFO", logger="bot")
    monkeypatch.setattr(kakao, "send_callback", always_reject)
    rich_agent(monkeypatch)
    asyncio.run(bot.handle_message(None, "안녕", "cb", time.monotonic()))
    assert len(calls) == 2
    assert "callback=failed" in caplog.text


def test_plain_text_reply_is_not_resent(monkeypatch, caplog):
    calls = []

    async def always_reject(client, bot_token, callback_token, skill_response):
        calls.append(skill_response)
        raise kakao.CallbackError("400 bad", -2)

    monkeypatch.setattr(kakao, "send_callback", always_reject)
    asyncio.run(bot.handle_message(None, "안녕", "cb", time.monotonic()))
    assert len(calls) == 1


def test_malformed_card_still_gets_fallback(monkeypatch):
    odd = kakao.skill_response([{"listCard": {"header": "제목", "items": ["x"]}}])
    assert bot.text_fallback(odd) == kakao.text_response(bot.CARD_FAILED_MESSAGE)

    def broken(response):
        raise AttributeError("boom")

    monkeypatch.setattr(kakao, "plain_text", broken)
    assert bot.text_fallback(odd) == kakao.text_response(bot.CARD_FAILED_MESSAGE)


def test_card_without_text_falls_back_to_notice():
    image_only = kakao.skill_response([kakao.simple_image("https://a.b/c.png")])
    assert bot.text_fallback(image_only) == kakao.text_response(bot.CARD_FAILED_MESSAGE)


def test_list_card_limits(caplog):
    items = [kakao.list_item(f"항목{i}", "설명") for i in range(6)]
    card = kakao.list_card("제목", items, [kakao.message_button("더 보기")])["listCard"]
    assert card["header"] == {"title": "제목"}
    assert len(card["items"]) == kakao.MAX_LIST_ITEMS and "목록 항목 6개" in caplog.text
    assert kakao.list_item("눌러", "", "눌렀음") == {"title": "눌러", "action": "message", "messageText": "눌렀음"}
    assert kakao.list_item("제목만") == {"title": "제목만"}
    with pytest.raises(ValueError):
        kakao.list_card("제목", [], [kakao.message_button("a")])
    with pytest.raises(ValueError):
        kakao.list_card("제목", items, [])


@pytest.fixture
def intro(monkeypatch):
    monkeypatch.setattr(agent, "INTRO", "안녕하세요, 소개입니다.")
    monkeypatch.setattr(agent, "EXAMPLE_QUESTIONS", ["질문1", "질문2"])
    monkeypatch.setattr(agent, "_greeted", set())


def test_intro_only_on_first_message(intro):
    first = agent.with_intro("u1", "답변")
    outputs = first["template"]["outputs"]
    assert outputs[0] == kakao.simple_text("안녕하세요, 소개입니다.")
    assert outputs[1] == kakao.simple_text("답변")
    assert [b["messageText"] for b in outputs[2]["textCard"]["buttons"]] == ["질문1", "질문2"]
    assert agent.with_intro("u1", "두 번째") == "두 번째"
    assert agent.with_intro("u2", "다른 사용자")["template"]["outputs"][0] == kakao.simple_text("안녕하세요, 소개입니다.")
    assert agent.with_intro("", "키 없음") == "키 없음"


def test_intro_keeps_answer_within_three_bubbles(intro):
    two = kakao.skill_response([kakao.simple_text("a"), kakao.simple_text("b")])
    outputs = agent.with_intro("u1", two)["template"]["outputs"]
    assert [o["simpleText"]["text"] for o in outputs] == ["안녕하세요, 소개입니다.", "a", "b"]  # 카드는 뺀다
    three = kakao.skill_response([kakao.simple_text(c) for c in "abc"])
    assert agent.with_intro("u2", three) is three  # 답변을 지키고 인사하지 않는다
    assert "u2" not in agent._greeted  # 다음 메시지에 다시 시도한다


def test_intro_disabled_when_empty(monkeypatch):
    monkeypatch.setattr(agent, "INTRO", "")
    monkeypatch.setattr(agent, "_greeted", set())
    assert agent.with_intro("u1", "답변") == "답변"


def test_reply_sends_intro_once(sent, intro, monkeypatch):
    monkeypatch.setattr(bot, "SIMPLE_TEXT_ONLY", False)  # 인사 + 추천 질문 카드 구성을 본다
    asyncio.run(bot.handle_message(None, "안녕", "cb1", time.monotonic(), user_key="u1"))
    asyncio.run(bot.handle_message(None, "또", "cb2", time.monotonic(), user_key="u1"))
    assert sent == [("cb1", "안녕하세요, 소개입니다."), ("cb2", "받은 메시지: 또")]


@pytest.mark.parametrize("event_type, token, expected", [
    ("CHAT_JOIN", "cb_join", "type=CHAT_JOIN seq=9 callback_token=yes"),
    ("LEAVE", None, "type=LEAVE seq=9 callback_token=no"),
    ("MESSAGE_CREATE", None, "type=MESSAGE_CREATE seq=9 callback_token=no"),
])
def test_ignored_event_type_is_logged(sent, caplog, event_type, token, expected):
    """답장하지 않는 이벤트도 종류는 로그에 남기고, 사용자 키·내용·토큰은 남기지 않는다."""
    caplog.set_level("INFO", logger="bot")
    d = {"botUserKey": "buk_secret", "content": "비밀 내용"}
    if token:
        d["callbackToken"] = token
    asyncio.run(bot.handle_event(None, {"op": 0, "s": 9, "t": event_type, "d": d}, time.monotonic()))
    assert sent == []
    assert f"event ignored {expected}" in caplog.text
    assert "buk_secret" not in caplog.text and "비밀 내용" not in caplog.text and "cb_join" not in caplog.text



def test_simple_text_only_flattens_cards():
    """심사는 SimpleText가 아닌 말풍선이 오면 케이스를 끝내므로, 카드는 글자 말풍선 하나로 바꾼다."""
    answer = kakao.skill_response([kakao.simple_text("안내"), kakao.suggestion_card(["주문 조회", "환불"])])
    response = bot.to_skill_response(answer)
    outputs = response["template"]["outputs"]
    assert len(outputs) == 1 and set(outputs[0]) == {"simpleText"}
    assert "안내" in outputs[0]["simpleText"]["text"]


def test_simple_text_only_keeps_plain_text():
    response = bot.to_skill_response(kakao.skill_response([kakao.simple_text("하나"), kakao.simple_text("둘")]))
    assert [set(o) for o in response["template"]["outputs"]] == [{"simpleText"}, {"simpleText"}]


def test_simple_text_is_capped_at_500():
    assert len(kakao.simple_text("가" * 800)["simpleText"]["text"]) == 500
