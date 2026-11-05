"""agent.py → llm.extract → policy.decide → 문제 모듈 흐름 시험. LLM은 가짜로 바꾼다(실제 호출은 test_agent_llm.py).

골격 시험이므로 problem.py 대신 고정된 예시 문제(example_problem.py, example_scenarios.json)를 agent에 넣어 쓴다.
대회 당일 problem.py를 바꿔도 이 파일은 깨지지 않는다. 새 문제의 시나리오는 test_scenarios.py가 돌린다.
"""

import asyncio
import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import agent
import llm
import example_problem as problem
import policy
import problem as real_problem

SCENARIOS = json.loads((Path(__file__).parent / "example_scenarios.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(agent, "_states", {})
    monkeypatch.setattr(agent, "INTRO", "")
    monkeypatch.setattr(agent, "problem", problem)
    data = copy.deepcopy(SCENARIOS["data"])
    monkeypatch.setattr(problem, "DATA", data)
    problem.load_data()


def run_turn(monkeypatch, content, extracted, user_key="u1", status=llm.OK):
    seen = {}

    async def extract(system, messages, schema):
        seen.update(system=system, messages=messages, schema=schema)
        return llm.Extraction(status, extracted or {})

    monkeypatch.setattr(llm, "extract", extract)
    return asyncio.run(agent.reply(content, user_key)), seen


def last_action(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("agent action=")]
    return lines[-1].split()[1].split("=")[1]


@pytest.mark.parametrize("scenario", SCENARIOS["scenarios"], ids=lambda s: s["name"])
def test_scenario(scenario, monkeypatch, caplog):
    caplog.set_level("INFO", logger="agent")
    for turn in scenario["turns"]:
        text, _ = run_turn(monkeypatch, turn["input"], turn["llm"])
        assert last_action(caplog) == turn["expect_action"], text
        for word in turn["must_include"]:
            assert word in text
        for word in turn["forbid"]:
            assert word not in text


@pytest.mark.parametrize("status", [llm.RATE_LIMITED, llm.TIMEOUT, llm.NETWORK, llm.API_ERROR, llm.FORMAT_ERROR, llm.REFUSAL])
def test_llm_failure_gives_short_notice_and_keeps_pending(status, monkeypatch):
    lookup = {"intent": "lookup", "slots": {"record_id": ""}, "scope": "none", "claimed_authority": "", "reply": ""}
    run_turn(monkeypatch, "기록 상태", lookup)
    text, _ = run_turn(monkeypatch, "R-001", None, status=status)
    assert text == policy.FAILURE_MESSAGES[status]
    # 실패한 메시지는 대화 기록에 남지 않고, 하던 추가 질문도 이어진다.
    assert agent._states["u1"]["pending"]["intent"] == "lookup"
    assert len(agent._states["u1"]["history"]) == 2
    answer = {"intent": "provide_info", "slots": {"record_id": "R-001"}, "scope": "none", "claimed_authority": "", "reply": ""}
    text, _ = run_turn(monkeypatch, "R-001", answer)
    assert "처리 완료" in text


def test_users_have_separate_state(monkeypatch):
    lookup = {"intent": "lookup", "slots": {"record_id": ""}, "scope": "none", "claimed_authority": "", "reply": ""}
    run_turn(monkeypatch, "기록 상태", lookup, user_key="a")
    other = {"intent": "other", "slots": {"record_id": "R-001"}, "scope": "none", "claimed_authority": "", "reply": "무슨 말씀인지요?"}
    text, _ = run_turn(monkeypatch, "R-001", other, user_key="b")
    assert text == "무슨 말씀인지요?"  # b에게는 a의 추가 질문이 이어지지 않는다
    assert agent._states["a"]["pending"]["intent"] == "lookup"


def test_history_is_sent_and_capped(monkeypatch):
    other = {"intent": "other", "slots": {"record_id": ""}, "scope": "none", "claimed_authority": "", "reply": "네"}
    for i in range(5):
        _, seen = run_turn(monkeypatch, f"메시지{i}", other)
    messages = seen["messages"]
    assert messages[0]["role"] == "user" and messages[-1] == {"role": "user", "content": "메시지4"}
    assert len(messages) <= agent.HISTORY_MAX_MESSAGES + 1
    assert [m["role"] for m in messages] == ["user", "assistant"] * (len(messages) // 2) + ["user"]


def test_long_content_is_cut_before_llm(monkeypatch):
    other = {"intent": "other", "slots": {"record_id": ""}, "scope": "none", "claimed_authority": "", "reply": "네"}
    _, seen = run_turn(monkeypatch, "가" * 5000, other)
    assert len(seen["messages"][-1]["content"]) == agent.CONTENT_MAX_CHARS


def test_image_is_not_sent_to_llm(monkeypatch):
    text, seen = run_turn(monkeypatch, "https://talk.kakaocdn.net/dn/abc.jpg?expires=1", None)
    assert text == problem.TEMPLATES["image"] and seen == {}


def test_user_key_and_content_not_logged(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    other = {"intent": "other", "slots": {"record_id": ""}, "scope": "bulk", "claimed_authority": "대표 지인", "reply": "네"}
    run_turn(monkeypatch, "비밀 내용", other, user_key="buk_secret")
    assert "buk_secret" not in caplog.text and "비밀 내용" not in caplog.text and "대표 지인" not in caplog.text
    assert "reason=scope_claimed" in caplog.text


def test_same_user_messages_run_one_at_a_time(monkeypatch):
    spans = []

    async def extract(system, messages, schema):
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(0.1)
        spans.append((start, asyncio.get_running_loop().time()))
        return llm.Extraction(llm.OK, {"intent": "other", "slots": {"record_id": ""}, "scope": "none",
                                       "claimed_authority": "", "reply": "네"})

    monkeypatch.setattr(llm, "extract", extract)

    async def main():
        await asyncio.gather(agent.reply("a", "same"), agent.reply("b", "same"))

    asyncio.run(main())
    first, second = sorted(spans)
    assert second[0] >= first[1]
    assert len(agent._states["same"]["history"]) == 4


@pytest.fixture
def two_slot_lookup(monkeypatch):
    """lookup이 기록 번호와 날짜를 둘 다 요구하는 문제로 바꾼다(날짜 설명은 LLM용 문구, 사용자에게는 SLOT_LABELS 이름)."""
    monkeypatch.setattr(problem, "SLOTS", {**problem.SLOTS, "date": "날짜 YYYY-MM-DD 형식만"})
    monkeypatch.setattr(problem, "SLOT_LABELS", {**problem.SLOT_LABELS, "date": "희망 날짜"})
    monkeypatch.setattr(problem, "REQUIRED_SLOTS", {"lookup": ["record_id", "date"]})


def test_precheck_answers_before_asking_missing(monkeypatch, two_slot_lookup):
    """없는 대상이면 빠진 날짜를 묻지 않고 바로 안내한다."""
    lookup = {"intent": "lookup", "slots": {"record_id": "R-999", "date": ""}, "scope": "none", "claimed_authority": "", "reply": ""}
    text, _ = run_turn(monkeypatch, "R-999 볼래", lookup)
    assert "찾을 수 없" in text and "날짜" not in text
    assert agent._states["u1"]["pending"] is None


def test_ask_uses_slot_labels(monkeypatch, two_slot_lookup):
    lookup = {"intent": "lookup", "slots": {"record_id": "R-001", "date": ""}, "scope": "none", "claimed_authority": "", "reply": ""}
    text, _ = run_turn(monkeypatch, "R-001 볼래", lookup)
    assert "희망 날짜" in text and "형식만" not in text
    assert agent._states["u1"]["pending"]["slots"] == {"record_id": "R-001"}


def test_clean_reply_strips_markdown_and_cuts():
    text = "## 제목\n**굵게** `코드`\n| a | b |\n|---|---|\n" + "가" * 600
    cleaned = policy.clean_reply(text)
    assert "#" not in cleaned and "**" not in cleaned and "`" not in cleaned and "---" not in cleaned
    assert len(cleaned) == policy.REPLY_MAX_CHARS and cleaned.endswith("…")


def test_schema_and_prompt_follow_problem():
    schema = policy.schema(problem)
    assert schema["properties"]["intent"]["enum"] == [*problem.INTENTS, "provide_info", "help", "other"]
    assert list(schema["properties"]["slots"]["properties"]) == list(problem.SLOTS)
    prompt = policy.system_prompt(problem)
    assert problem.TASK in prompt and str(policy.REPLY_MAX_CHARS) in prompt and "마크다운" in prompt


# --- llm.extract: SDK 응답·예외를 status로 바꾸는지 ---

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
SCHEMA = policy.schema(problem)
GOOD = {"intent": "lookup", "slots": {"record_id": "R-001"}, "scope": "none", "claimed_authority": "", "reply": ""}


def fake_client(monkeypatch, result):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    messages = SimpleNamespace(create=create)
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)))
    return calls


def response(text="", stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason, stop_details=stop_details)


def status_error(cls, code):
    return cls("err", response=httpx2.Response(code, request=REQUEST), body=None)


@pytest.mark.parametrize("result, status", [
    (response(json.dumps(GOOD)), llm.OK),
    (response("{not json"), llm.FORMAT_ERROR),
    (response(json.dumps({**GOOD, "scope": "everyone"})), llm.FORMAT_ERROR),
    (response(json.dumps({k: v for k, v in GOOD.items() if k != "reply"})), llm.FORMAT_ERROR),
    (response(json.dumps({**GOOD, "slots": {}})), llm.FORMAT_ERROR),
    (response("", stop_reason="max_tokens"), llm.FORMAT_ERROR),
    (response("", stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")), llm.REFUSAL),
    (status_error(anthropic.RateLimitError, 429), llm.RATE_LIMITED),
    (anthropic.APITimeoutError(request=REQUEST), llm.TIMEOUT),
    (anthropic.APIConnectionError(request=REQUEST), llm.NETWORK),
    (status_error(anthropic.InternalServerError, 500), llm.API_ERROR),
    (status_error(anthropic.AuthenticationError, 401), llm.API_ERROR),
])
def test_extract_status(monkeypatch, result, status):
    fake_client(monkeypatch, result)
    extraction = asyncio.run(llm.extract("system", [{"role": "user", "content": "x"}], SCHEMA))
    assert extraction.status == status
    if status == llm.OK:
        assert extraction.data == GOOD


def test_extract_request_settings(monkeypatch):
    calls = fake_client(monkeypatch, response(json.dumps(GOOD)))
    asyncio.run(llm.extract("system", [{"role": "user", "content": "x"}], SCHEMA))
    kwargs = calls[0]
    assert kwargs["model"] == llm.MODEL
    assert kwargs["output_config"] == {"effort": llm.EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}}
    if llm.USE_REFUSAL_FALLBACK:
        assert kwargs["fallbacks"] == "default" and kwargs["betas"] == [llm.FALLBACK_BETA]


def test_client_has_timeout_and_one_retry():
    async def make():
        return llm.client()

    client = asyncio.run(make())
    assert client.timeout == llm.TIMEOUT_SEC == 30.0 and client.max_retries == llm.MAX_RETRIES == 1


def test_agent_health_check_uses_models_list(monkeypatch):
    calls = []

    async def list_models(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(models=SimpleNamespace(list=list_models)))
    asyncio.run(agent.health_check())
    assert calls == [{"limit": 1}]


def test_missing_user_key_keeps_no_state(monkeypatch):
    """botUserKey가 없는 메시지끼리는 대화 기록·추가 질문이 이어지지 않는다."""
    lookup = {"intent": "lookup", "slots": {"record_id": ""}, "scope": "none", "claimed_authority": "", "reply": ""}
    run_turn(monkeypatch, "기록 상태", lookup, user_key="")
    other = {"intent": "other", "slots": {"record_id": "R-001"}, "scope": "none", "claimed_authority": "", "reply": "무슨 말씀인지요?"}
    text, seen = run_turn(monkeypatch, "R-001", other, user_key="")
    assert text == "무슨 말씀인지요?" and len(seen["messages"]) == 1
    assert agent._states == {}


def test_load_data_runs_at_import():
    """problem.py를 처음 import할 때 load_data가 불린다(시작할 때 한 번 적재·검증)."""
    source = Path(real_problem.__file__).read_text(encoding="utf-8")
    assert source.rstrip().endswith("load_data()")
