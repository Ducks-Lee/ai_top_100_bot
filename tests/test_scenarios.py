"""problem.py(지금 문제)를 tests/scenarios.json으로 시험한다. LLM은 시나리오에 적은 추출 결과로 바꾼다.

대회 당일 problem.py를 바꾸면 scenarios.json도 문제에 맞게 바꾼다. 골격 시험(test_agent.py)은 예시 문제를 쓰므로 영향이 없다.
실제 LLM으로 같은 시나리오를 돌리려면 test_agent_llm.py(RUN_LLM_TESTS=1)를 쓴다.
"""

import asyncio
import copy
import json
from pathlib import Path

import pytest

import agent
import llm
import problem

SCENARIOS = json.loads((Path(__file__).parent / "scenarios.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(agent, "_states", {})
    monkeypatch.setattr(agent, "INTRO", "")
    monkeypatch.setattr(problem, "DATA", copy.deepcopy(SCENARIOS["data"]))
    problem.load_data()


@pytest.mark.parametrize("scenario", SCENARIOS["scenarios"], ids=lambda s: s["name"])
def test_scenario(scenario, monkeypatch, caplog):
    caplog.set_level("INFO", logger="agent")
    for turn in scenario["turns"]:
        async def extract(system, messages, schema, data=turn["llm"]):
            return llm.Extraction(llm.OK, data)

        monkeypatch.setattr(llm, "extract", extract)
        text = asyncio.run(agent.reply(turn["input"], "u1"))
        line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("agent action=")][-1]
        assert line.split()[1] == f"action={turn['expect_action']}", text
        for word in turn["must_include"]:
            assert word in text
        for word in turn["forbid"]:
            assert word not in text
