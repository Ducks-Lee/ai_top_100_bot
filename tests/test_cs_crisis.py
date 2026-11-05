"""위기신호 대응(cs_crisis, cs_agent._crisis_turn, cs_tools 차단) 시험. LLM과 액션 API는 가짜다."""

import asyncio

import pytest

import cs_agent
import cs_crisis
import cs_tools
from tests.test_cs_agent import BASE, FIRST, order, setup, text, tool_use

HEAD = FIRST.rsplit("\n\n", 1)[0] + "\n\n"
ESC = {"POST /api/escalations": {"escalation_id": "ESC-1", "request_status": "SUBMITTED"}}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(cs_agent, "_states", {})


@pytest.mark.parametrize("msg", ["그냥 죽고 싶어요", "자살하고 싶다는 생각이 들어요", "다 끝내고 싶어요",
                                 "마지막으로 먹어보고 싶었어요", "이제 주문할 일 없을 것 같네요", "그동안 고마웠어요"])
def test_keyword_detects_signal(msg):
    assert cs_crisis.detect(msg)


@pytest.mark.parametrize("msg", ["수박 먹고 배탈 났어요", "배가 아파 죽겠어요", "배고파 죽겠다", "요즘 삶이 좀 힘드네요",
                                 "환불해 주세요", "곰팡이가 피어 있어요"])
def test_symptoms_and_everyday_words_are_not_crisis(msg):
    assert not cs_crisis.detect(msg)


def _posts(api):
    return [c for c in api.calls if c[0] == "POST"]


def test_keyword_crisis_escalates_once_and_gives_109_once(monkeypatch):
    api, llm = setup(monkeypatch, {**BASE, **ESC}, [text("곁에 있겠습니다. 천천히 말씀해 주세요.")])
    first = asyncio.run(cs_agent.reply(HEAD + "환불은 됐고요, 이제 주문할 일 없을 것 같아요", "u1"))
    assert "109" in first and "책임지고 처리" in first and "ESC-1" in first
    assert not cs_crisis.FORBIDDEN.search(first)
    assert llm.requests == []  # 첫 답은 고정 틀(LLM 안 씀)
    second = asyncio.run(cs_agent.reply("고마워요", "u1"))
    assert "109" not in second
    posts = _posts(api)
    assert len(posts) == 1 and posts[0][1] == "/api/escalations"
    assert posts[0][3]["lane"] == "HANDOFF" and posts[0][3]["reason"].startswith("[SAFETY]")
    assert "주문할 일" not in posts[0][3]["reason"]  # 고객 말을 인용하지 않는다
    assert not any(c[1] == "/api/members" for c in api.calls)  # 위기 상태에서는 회원 조회도 하지 않는다


def test_llm_flag_crisis_blocks_other_tools(monkeypatch):
    routes = {**BASE, **ESC, "GET /api/orders/900000001": order("900000001", "MB-1"), "POST /api/refunds": {"refund_id": "R-1"}}
    api, llm = setup(monkeypatch, routes,
                     [tool_use("flag_crisis", {}), tool_use("request_refund", {"order_no": "900000001", "amount": 1, "reason": "x"}, "t2")])
    answer = asyncio.run(cs_agent.reply(FIRST, "u1"))
    assert "109" in answer
    assert [c[1] for c in _posts(api)] == ["/api/escalations"]


def test_followup_forbidden_phrase_is_replaced(monkeypatch):
    api, llm = setup(monkeypatch, {**BASE, **ESC}, [text("힘내세요! 불편을 드려 죄송합니다. 환불해 드리겠습니다.")])
    asyncio.run(cs_agent.reply(HEAD + "죽고 싶어요", "u1"))
    second = asyncio.run(cs_agent.reply("모르겠어요", "u1"))
    assert second == cs_crisis.SAFE_REPLY


def test_resource_repeated_only_when_asked():
    assert cs_crisis.safe_followup("109에 전화해 보세요.", "아까 그 번호가 뭐였죠?") == "109에 전화해 보세요."
    assert cs_crisis.safe_followup("109에 전화해 보세요.", "네") == cs_crisis.SAFE_REPLY


def test_no_tc_id_skips_escalation(monkeypatch):
    api, llm = setup(monkeypatch, BASE)
    answer = asyncio.run(cs_agent.reply("다 끝내고 싶어요", "u1"))
    assert "109" in answer and "접수번호" not in answer and _posts(api) == []


def test_crisis_session_blocks_tools_directly():
    s = cs_tools.Session(tc_id="T-03", crisis=True)
    text_, err = asyncio.run(cs_tools.execute(s, "get_order", {"order_no": "900000001"}))
    assert err and "하지 않습니다" in text_


@pytest.mark.parametrize("msg", ["감자 살 수 있나요?", "감자살게요", "의자 살 때처럼"])
def test_purchase_words_are_not_crisis(msg):
    assert not cs_crisis.detect(msg)
