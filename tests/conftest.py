import os

# 로컬 .env나 셸에 설정된 값(웹훅 헤더, LOG_PAYLOAD 등)이 테스트 결과를 바꾸지 않게 한다.
# 설정을 읽는 모듈(bot, gateway, main)이 import되기 전에 실행된다.
os.environ["SKIP_DOTENV"] = "1"
# 테스트 중에는 로그 파일을 만들지 않는다(웹훅 테스트가 앱을 띄우면 setup_logging이 불린다).
os.environ["LOG_DIR"] = ""
# 골격 시험(test_agent, test_main 등)은 agent.reply가 골격(skeleton_reply)이라고 보고 짠 것이다. CS 에이전트 시험은 cs_agent를 직접 부른다.
os.environ["AGENT_MODE"] = "skeleton"
# 사용액 집계(spend.py)는 테스트 중 파일에 쓰지 않는다.
os.environ["SPEND_FILE"] = ""
for name in ("BOT_TOKEN", "AGENT_TIMEOUT_SEC", "LOG_PAYLOAD", "KAKAO_GATEWAY_URL",
             "WEBHOOK_HEADER_NAME", "WEBHOOK_HEADER_VALUE", "DISCORD_WEBHOOK_URL"):
    os.environ.pop(name, None)
# CS 에이전트 단위 시험은 공개용 예시 설정·규칙(cs_config.example.json, rules.example.md)을 쓴다. data/에 실제 값이 있어도 결과가 같게 한다.
# 실제 API 종단 시험(RUN_LLM_TESTS=1, test_cs_live.py만 따로 실행)은 실제 설정(data/ 또는 CS_CONFIG_FILE·CS_RULES_FILE)을 그대로 쓴다.
if os.environ.get("RUN_LLM_TESTS") != "1":
    os.environ["CS_CONFIG_FILE"] = "cs_config.example.json"
    os.environ["CS_RULES_FILE"] = "rules.example.md"
else:
    # SKIP_DOTENV로 .env를 읽지 않으므로 설정 파일 경로만 .env에서 옮겨 온다(셸 환경변수가 우선).
    from pathlib import Path

    from dotenv import dotenv_values

    _env = dotenv_values(Path(__file__).parent.parent / ".env")
    for _name in ("CS_CONFIG_FILE", "CS_RULES_FILE"):
        if not os.environ.get(_name) and _env.get(_name):
            os.environ[_name] = _env[_name]
