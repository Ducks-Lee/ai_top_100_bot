import subprocess

import httpx
import pytest

import notify_failure

URL = "https://discord.com/api/webhooks/123/secret-webhook-token"
STOPPED = "ActiveState=failed\nSubState=failed\n"
RESTARTING = "ActiveState=activating\nSubState=auto-restart\n"
RUNNING = "ActiveState=active\nSubState=running\n"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """웹훅 전송과 systemctl/journalctl 호출을 가로챈다.
    systemctl은 outputs["systemctl"] 목록을 차례로 돌려주고, 마지막 값은 계속 반복한다."""
    monkeypatch.setattr(notify_failure, "WEBHOOK_URL", URL)
    monkeypatch.setattr(notify_failure, "STATE_FILE", tmp_path / ".last_failure_alert")
    monkeypatch.setattr(notify_failure.time, "sleep", lambda sec: None)
    monkeypatch.setenv("MONITOR_SERVICE_RESULT", "exit-code")
    monkeypatch.setenv("MONITOR_EXIT_CODE", "exited")
    monkeypatch.setenv("MONITOR_EXIT_STATUS", "2")
    outputs = {
        "systemctl": [STOPPED],
        "result": "Result=exit-code\nExecMainStatus=2\n",
        "journalctl": "READY bot=1 session=s\nclose 4003\n",
    }
    posts = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "systemctl" and "--property=Result" in cmd:
            stdout = outputs["result"]
        elif cmd[0] == "systemctl":
            queue = outputs["systemctl"]
            stdout = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            stdout = outputs["journalctl"]
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def fake_post(url, json, timeout):
        posts.append((url, json))
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(notify_failure.subprocess, "run", fake_run)
    monkeypatch.setattr(notify_failure.httpx, "post", fake_post)
    return outputs, posts


def run():
    return notify_failure.main(["notify_failure.py", "ai-top-100.service"])


def test_stopped_service_alert_includes_cause_state_and_logs(env):
    _, posts = env
    assert run() == 0
    url, body = posts[0]
    assert url == URL
    assert "[봇 멈춤]" in body["content"]
    assert "exit-code (exited 2)" in body["content"]
    assert "ActiveState=failed" in body["content"]
    assert "close 4003" in body["content"]
    assert body["allowed_mentions"] == {"parse": []}


def test_restarting_service_alert_is_rate_limited(env):
    outputs, posts = env
    outputs["systemctl"] = [RESTARTING]
    assert run() == 0
    assert run() == 0
    assert len(posts) == 1
    assert "[봇 비정상 종료]" in posts[0][1]["content"]


def test_manual_run_does_not_start_cooldown(env, monkeypatch):
    """systemctl start로 직접 시험하면(MONITOR_* 없음) 쿨다운을 남기지 않아, 직후의 실제 장애 알림이 막히지 않는다."""
    outputs, posts = env
    outputs["systemctl"] = [RUNNING]
    monkeypatch.delenv("MONITOR_SERVICE_RESULT")
    run()
    assert "실패 원인: 알 수 없음" in posts[0][1]["content"]
    assert not notify_failure.STATE_FILE.exists()


def test_stopped_service_alert_ignores_cooldown(env):
    outputs, posts = env
    outputs["systemctl"] = [RESTARTING]
    run()
    outputs["systemctl"] = [STOPPED]
    run()
    assert len(posts) == 2
    assert "[봇 멈춤]" in posts[1][1]["content"]


def test_stop_during_alert_sends_second_alert(env, monkeypatch):
    """알림을 보내는 사이 재시작된 봇이 멈추면(합쳐진 OnFailure 요청) 끝나기 전에 다시 알린다."""
    outputs, posts = env
    outputs["systemctl"] = [RESTARTING, STOPPED]
    monkeypatch.setenv("MONITOR_EXIT_STATUS", "1")
    assert run() == 0
    first, second = posts[0][1]["content"], posts[1][1]["content"]
    assert "[봇 비정상 종료]" in first and "exit-code (exited 1)" in first
    # 두 번째 알림에는 처음 실패(종료 코드 1)가 아니라 멈춘 실패의 결과가 찍힌다.
    assert "[봇 멈춤]" in second and "Result=exit-code ExecMainStatus=2" in second


def test_recovered_service_sends_one_alert(env):
    outputs, posts = env
    outputs["systemctl"] = [RESTARTING, RUNNING]
    run()
    assert len(posts) == 1


def test_secrets_and_payload_logs_are_hidden(env, monkeypatch):
    outputs, posts = env
    monkeypatch.setenv("BOT_TOKEN", "bot-token-12345678")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abcdefgh")
    outputs["journalctl"] = (
        'INFO gateway dispatch: {"d": {"content": "개인 대화"}}\n'
        "ERROR auth failed key=sk-ant-abcdefgh token=bot-token-12345678\n"
    )
    run()
    content = posts[0][1]["content"]
    assert "개인 대화" not in content
    assert "sk-ant-abcdefgh" not in content
    assert "bot-token-12345678" not in content
    assert "ERROR auth failed key=*** token=***" in content


def test_env_and_dotenv_secrets_are_both_hidden(env, monkeypatch, tmp_path):
    """같은 이름이 환경변수와 .env에 다르게 있으면 둘 다 가린다(실제로 쓰이는 값은 환경변수 쪽)."""
    outputs, posts = env
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_TOKEN=old-token-from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(notify_failure.config, "ENV_FILE", env_file)
    monkeypatch.setenv("SKIP_DOTENV", "0")
    monkeypatch.setenv("BOT_TOKEN", "new-token-from-environ")
    outputs["journalctl"] = "a new-token-from-environ b old-token-from-dotenv\n"
    run()
    assert "a *** b ***" in posts[0][1]["content"]


def test_long_logs_fit_discord_limit(env):
    outputs, posts = env
    outputs["journalctl"] = "\n".join(f"line{i} " + "x" * 300 for i in range(20)) + "\n```"
    run()
    content = posts[0][1]["content"]
    assert len(content) <= notify_failure.MAX_CONTENT_LEN
    assert "line19" in content  # 최근 줄이 남는다
    assert content.count("```") == 2


def test_missing_systemctl_is_reported_as_unknown(env, monkeypatch):
    _, posts = env

    def no_command(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(notify_failure.subprocess, "run", no_command)
    assert run() == 0
    assert "[봇 실패]" in posts[0][1]["content"]
    assert "확인 실패" in posts[0][1]["content"]


def test_transient_errors_are_retried(env, monkeypatch):
    _, posts = env
    responses = [503, 429, 204]

    def flaky_post(url, json, timeout):
        posts.append((url, json))
        return httpx.Response(responses.pop(0), headers={"Retry-After": "1"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(notify_failure.httpx, "post", flaky_post)
    assert run() == 0
    assert len(posts) == 3


def test_failed_send_hides_url_and_skips_cooldown(env, monkeypatch, capsys):
    outputs, posts = env
    outputs["systemctl"] = [RESTARTING]

    def failing_post(url, json, timeout):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(notify_failure.httpx, "post", failing_post)
    assert run() == 1
    assert "secret-webhook-token" not in capsys.readouterr().err
    assert not notify_failure.STATE_FILE.exists()


def test_client_error_is_not_retried(env, monkeypatch):
    _, posts = env

    def not_found(url, json, timeout):
        posts.append(url)
        return httpx.Response(404, request=httpx.Request("POST", url))

    monkeypatch.setattr(notify_failure.httpx, "post", not_found)
    assert run() == 1
    assert len(posts) == 1


def test_no_webhook_url_skips(env, monkeypatch):
    _, posts = env
    monkeypatch.setattr(notify_failure, "WEBHOOK_URL", "")
    assert run() == 0
    assert notify_failure.main(["x", "--test"]) == 1
    assert posts == []


def test_test_mode_sends_without_cooldown(env):
    _, posts = env
    assert notify_failure.main(["x", "--test"]) == 0
    assert notify_failure.main(["x", "--test"]) == 0
    assert len(posts) == 2
    assert "[시험]" in posts[0][1]["content"]
