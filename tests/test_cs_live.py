"""실제 Anthropic API와 액션 서버로 시나리오(기본 tests/cs_scenarios.json, 지어낸 예시)를 돌리는 종단 시험. 비용과 API 호출 한도를 써서 기본 pytest에서는 건너뛴다.

실행: $env:RUN_LLM_TESTS="1"; .venv\\Scripts\\python -m pytest tests\\test_cs_live.py -q -s
다른 시나리오 파일은 CS_SCENARIOS_FILE로 지정한다. ACTION_RESET_PATH가 있으면 시작할 때 그 경로로 POST해 액션 데이터를 초기화한다.
답마다 시간·글자 수·부른 API를 출력한다.
이때는 conftest가 예시 설정으로 바꾸지 않으므로 data/cs_config.json·data/rules.md(또는 CS_CONFIG_FILE·CS_RULES_FILE)의 실제 설정을 쓴다.
예시 시나리오의 expect_calls는 예시 설정의 번호(EX-..)라서, 실제 설정으로 돌릴 때는 실제 번호로 맞춘 시나리오 파일을 쓴다.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values

pytestmark = pytest.mark.skipif(os.getenv("RUN_LLM_TESTS") != "1", reason="RUN_LLM_TESTS=1일 때만 실제 API를 부른다")

SCENARIOS = Path(os.getenv("CS_SCENARIOS_FILE") or Path(__file__).parent / "cs_scenarios.json")
CASES = json.loads(SCENARIOS.read_text(encoding="utf-8"))["cases"]
TARGET_SEC = 60


@pytest.fixture(scope="module", autouse=True)
def live_env():
    env = dotenv_values(Path(__file__).parent.parent / ".env")
    for name in ("ANTHROPIC_API_KEY", "ACTION_API_BASE_URL", "ACTION_API_KEY"):
        if not (os.getenv(name) or env.get(name)):
            pytest.skip(f"{name}가 없습니다")
        if not os.getenv(name):
            os.environ[name] = env[name]
    import action_api
    action_api.BASE_URL = os.environ["ACTION_API_BASE_URL"].rstrip("/")
    action_api.API_KEY = os.environ["ACTION_API_KEY"]
    if reset := os.getenv("ACTION_RESET_PATH") or env.get("ACTION_RESET_PATH"):
        asyncio.run(action_api.call("POST", reset))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_scenario(case, monkeypatch):
    import cs_agent

    monkeypatch.setattr(cs_agent, "_states", {})
    first = (f"case metadata\n- id: {case['id']}\n- 상담 인입 일시: {case['at']}\n- 인입창구: 온라인몰 1:1 채팅\n"
             f"- 인입 계정: 회원 로그인 · {case['account']}\n\n")

    async def run():
        answers = []
        for i, turn in enumerate(case["turns"]):
            started = time.monotonic()
            answer = await cs_agent.reply((first if i == 0 else "") + turn, "live")
            seconds = time.monotonic() - started
            calls = cs_agent._states["live"]["session"].calls
            print(f"\n[{case['id']} {seconds:.1f}s {len(answer)}자 {','.join(calls)}] {answer}")
            assert seconds < TARGET_SEC
            assert 0 < len(answer) <= 500
            answers.append(answer)
        return answers, cs_agent._states["live"]["session"].calls

    answers, calls = asyncio.run(run())
    for no in case["expect_calls"]:
        assert no in calls, f"{no} 호출 없음: {calls}"
    for word in case["forbid"]:
        assert not any(word in a for a in answers), word
