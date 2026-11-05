"""실제 Anthropic API로 시나리오를 돌리는 종단 시험. 비용이 들어 기본 pytest에서는 건너뛴다.

실행: $env:RUN_LLM_TESTS="1"; .venv\\Scripts\\python -m pytest tests\\test_agent_llm.py -q -s
scenarios.json에서 live가 true인 시나리오만 돈다(턴마다 LLM 1회). 응답 시간을 출력하고 60초 목표를 넘으면 실패한다.
"""

import asyncio
import copy
import json
import logging
import os
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values

import agent
import llm
import problem

pytestmark = pytest.mark.skipif(os.getenv("RUN_LLM_TESTS") != "1", reason="RUN_LLM_TESTS=1일 때만 실제 API를 부른다")

SCENARIOS = json.loads((Path(__file__).parent / "scenarios.json").read_text(encoding="utf-8"))
LIVE = [s for s in SCENARIOS["scenarios"] if s.get("live")]
TARGET_SEC = 60


@pytest.fixture(autouse=True)
def live_setup(monkeypatch):
    # conftest가 .env를 읽지 않게 해 두었으므로 API 키만 직접 가져온다.
    key = os.getenv("ANTHROPIC_API_KEY") or dotenv_values(Path(__file__).parent.parent / ".env").get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY가 없습니다")
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setattr(agent, "_states", {})
    monkeypatch.setattr(agent, "INTRO", "")
    monkeypatch.setattr(problem, "DATA", copy.deepcopy(SCENARIOS["data"]))
    problem.load_data()


def test_health_check():
    asyncio.run(agent.health_check())


@pytest.mark.parametrize("scenario", LIVE, ids=lambda s: s["name"])
def test_live_scenario(scenario, caplog):
    caplog.set_level("INFO", logger="agent")

    async def run():
        results = []
        for turn in scenario["turns"]:
            started = time.monotonic()
            text = await agent.reply(turn["input"], "live_user")
            line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("agent action=")][-1]
            results.append((turn, text, line, time.monotonic() - started))
        return results

    for turn, text, line, seconds in asyncio.run(run()):
        print(f"\n[{scenario['name']}] {seconds:.1f}s {line}\n  → {text}")
        assert seconds < TARGET_SEC
        assert f"action={turn['expect_action']}" in line, text
        for word in turn["must_include"]:
            assert word in text
        for word in turn["forbid"]:
            assert word not in text


def test_same_user_burst_within_target():
    """같은 사용자가 연달아 3개를 보내면 잠금 때문에 차례로 처리된다. 마지막 답까지의 시간을 본다."""
    async def run():
        started = time.monotonic()
        texts = await asyncio.gather(*(agent.reply(f"R-00{i} 상태 알려줘", "burst_user") for i in (1, 2, 9)))
        return texts, time.monotonic() - started

    texts, seconds = asyncio.run(run())
    print(f"\n[연속 3개] 마지막 답까지 {seconds:.1f}s\n  → " + "\n  → ".join(texts))
    assert seconds < TARGET_SEC
    assert all(texts)
