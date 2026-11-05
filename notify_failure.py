"""봇 서비스가 실패하면 디스코드 웹훅으로 알린다. 실행: python notify_failure.py <유닛 이름>

deploy/ai-top-100.service의 OnFailure=가 deploy/ai-top-100-alert@.service를 거쳐 실행한다.
설정 확인용 시험 전송: python notify_failure.py --test
"""

import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values

import config  # noqa: F401  .env 로드 (아래 os.getenv보다 먼저)

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
# Restart=always인 서비스는 재시작할 때마다 OnFailure가 실행된다. 재시작이 반복되면 이 간격 안의 알림은 건너뛴다.
# 재시작되지 않고 멈춘 경우는 간격과 관계없이 보낸다.
COOLDOWN_SEC = 300
# systemd의 StateDirectory=가 만든 폴더(STATE_DIRECTORY)를 쓰고, 없으면(직접 실행) 프로젝트 폴더를 쓴다.
STATE_FILE = Path(os.getenv("STATE_DIRECTORY") or Path(__file__).parent) / ".last_failure_alert"
LOG_LINES = 20
# 디스코드 메시지 본문 최대 길이
MAX_CONTENT_LEN = 2000
COMMAND_TIMEOUT_SEC = 10
SEND_ATTEMPTS = 3
RETRY_DELAY_SEC = 3
MAX_RETRY_AFTER_SEC = 10
# LOG_PAYLOAD=1일 때 남는 이벤트 원본에는 대화 내용이 들어 있으므로 알림에 싣지 않는다.
HIDDEN_LOG_MARKERS = ("dispatch:",)
# 이 이름이 들어간 설정값은 로그에 섞여 나와도 가린다.
SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|WEBHOOK", re.IGNORECASE)
MIN_SECRET_LEN = 8

# 현재 상태(ActiveState) → (알림 제목, 쿨다운 적용 여부)
STATES = {
    "failed": ("[봇 멈춤] 자동 재시작하지 않음. 직접 조치가 필요합니다.", False),
    "inactive": ("[봇 꺼짐] 실패 뒤 서비스가 꺼져 있습니다.", False),
    "activating": ("[봇 비정상 종료] systemd가 자동으로 다시 실행합니다.", True),
    "active": ("[봇 비정상 종료] 이미 다시 실행됐습니다.", True),
}
UNKNOWN_STATE = ("[봇 실패] 현재 상태를 확인하지 못했습니다. 직접 확인이 필요합니다.", False)


def unit_state(unit: str, props=("ActiveState", "SubState")) -> dict:
    """systemctl show 결과에서 상태 값을 읽는다. systemctl이 없거나 실패하면 빈 dict."""
    cmd = ["systemctl", "show", unit, "--no-pager"] + [f"--property={p}" for p in props]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SEC).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    state = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and value:
            state[key] = value
    return state


def failure_cause() -> str:
    """실패 당시의 결과. systemd가 OnFailure로 실행한 유닛에 MONITOR_* 환경변수로 넘겨준다(systemd 251 이상)."""
    result = os.getenv("MONITOR_SERVICE_RESULT")
    if not result:
        return ""
    code, status = os.getenv("MONITOR_EXIT_CODE", "-"), os.getenv("MONITOR_EXIT_STATUS", "-")
    return f"{result} ({code} {status})"


def current_cause(unit: str) -> str:
    """가장 최근 실행의 결과. MONITOR_*는 이 알림을 시작한 실패의 값이라, 그 뒤에 난 실패는 여기서 읽는다."""
    state = unit_state(unit, ("Result", "ExecMainStatus"))
    return " ".join(f"{k}={v}" for k, v in state.items())


def recent_logs(unit: str) -> list[str]:
    """최근 로그를 저널에서 읽는다. 처리되지 않은 예외의 traceback은 bot.log가 아니라 저널에만 남는다.
    저널을 읽을 수 없으면 bot.log 끝부분을 쓴다."""
    lines = []
    cmd = ["journalctl", "-u", unit, "-n", str(LOG_LINES), "--no-pager", "-o", "cat"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SEC)
        if result.returncode == 0:
            lines = result.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        pass
    if not lines and config.LOG_DIR:
        log_file = Path(config.__file__).parent / config.LOG_DIR / "bot.log"
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_LINES:]
        except OSError:
            pass
    return [line for line in lines if not any(m in line for m in HIDDEN_LOG_MARKERS)]


def secret_values() -> list[str]:
    """.env와 환경변수 중 이름이 비밀값처럼 보이는 값. 긴 값부터 가려야 일부만 가려지는 일이 없다."""
    # 같은 이름이 환경변수와 .env에 다르게 있을 수 있으므로 양쪽 값을 모두 가린다.
    pairs = list(os.environ.items())
    if os.getenv("SKIP_DOTENV") != "1":
        pairs += [(k, v) for k, v in dotenv_values(config.ENV_FILE, interpolate=False).items() if v]
    found = {v for k, v in pairs if SECRET_NAME.search(k) and v and len(v) >= MIN_SECRET_LEN}
    return sorted(found, key=len, reverse=True)


def redact(lines: list[str]) -> list[str]:
    secrets = secret_values()
    for i, line in enumerate(lines):
        for secret in secrets:
            line = line.replace(secret, "***")
        lines[i] = line
    return lines


def classify(state: dict) -> tuple[str, bool]:
    return STATES.get(state.get("ActiveState", ""), UNKNOWN_STATE)


def build_message(unit: str, state: dict, cause: str, logs: list[str]) -> str:
    title, _ = classify(state)
    if state.get("ActiveState") in ("activating", "active"):
        hint = f"반복되면 {COOLDOWN_SEC // 60}분에 한 번만 알립니다. `journalctl -u ai-top-100 -n 100`으로 원인 확인"
    else:
        hint = "로그를 확인하고 원인을 고친 뒤 `sudo systemctl restart ai-top-100`"
    status = " ".join(f"{k}={v}" for k, v in state.items()) or "확인 실패"
    head = "\n".join([
        f"**{title}** ({unit})",
        f"실패 원인: {cause or '알 수 없음'}",
        f"현재 상태: {status}",
        f"서버: {socket.gethostname()} / {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        hint,
    ])
    if not logs:
        return head[:MAX_CONTENT_LEN]
    # 로그 안의 ```가 코드 블록을 끝내지 않게 바꾼다. 길이가 넘치면 오래된 줄부터 뺀다.
    body = [line.replace("```", "'''") for line in logs]
    while body:
        content = f"{head}\n최근 로그:\n```\n" + "\n".join(body) + "\n```"
        if len(content) <= MAX_CONTENT_LEN:
            return content
        body.pop(0)
    return head[:MAX_CONTENT_LEN]


def in_cooldown(now: float) -> bool:
    try:
        return now - float(STATE_FILE.read_text()) < COOLDOWN_SEC
    except (OSError, ValueError):
        return False


def record_sent(now: float) -> None:
    try:
        STATE_FILE.write_text(str(now))
    except OSError as e:
        print(f"마지막 알림 시각을 저장하지 못함(반복 알림이 걸러지지 않을 수 있음): {STATE_FILE} {e!r}", file=sys.stderr)


def webhook_body(content: str) -> dict:
    # 로그에 @everyone 같은 글자가 있어도 멘션 알림이 가지 않게 한다.
    return {"content": content, "allowed_mentions": {"parse": []}}


def check_response(resp: httpx.Response | None, error: Exception | None = None) -> tuple[bool | None, float, str]:
    """전송 결과를 판단한다. (성공 여부, 다시 보내기 전 대기 초, 오류 문구)를 돌려주며, 성공 여부가 None이면
    다시 보낼 만한 일시적 실패(연결 오류, 429, 5xx)다. 오류 문구에는 웹훅 URL(비밀값)을 넣지 않는다."""
    if error is not None:
        return None, RETRY_DELAY_SEC, type(error).__name__
    if resp.is_success:
        return True, 0, ""
    status = f"HTTP {resp.status_code}"
    if resp.status_code != 429 and resp.status_code < 500:
        return False, 0, status
    try:
        delay = min(float(resp.headers.get("Retry-After", RETRY_DELAY_SEC)), MAX_RETRY_AFTER_SEC)
    except ValueError:
        delay = RETRY_DELAY_SEC
    return None, delay, status


def send(content: str) -> bool:
    """일시적인 실패는 SEND_ATTEMPTS번까지 다시 보낸다. 성공 여부를 돌려준다."""
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            resp = httpx.post(WEBHOOK_URL, json=webhook_body(content), timeout=COMMAND_TIMEOUT_SEC)
        except httpx.HTTPError as e:
            ok, delay, error = check_response(None, e)
        else:
            ok, delay, error = check_response(resp)
        if ok is not None:
            if not ok:
                print(f"디스코드 알림 전송 실패: {error}", file=sys.stderr)
            return ok
        print(f"디스코드 알림 전송 실패 ({attempt}/{SEND_ATTEMPTS}): {error}", file=sys.stderr)
        if attempt < SEND_ATTEMPTS:
            time.sleep(delay)
    return False


def notify(unit: str, now: float, cause: str) -> tuple[bool, bool]:
    """알림을 보낸다. (성공 여부, 재시작 중으로 분류했는지)를 돌려준다."""
    state = unit_state(unit)
    _, restarting = classify(state)
    # 실제 실패로 실행된 경우(MONITOR_*가 있음)만 쿨다운을 쓴다. 수동 시험이 실제 장애 알림을 막지 않게 하기 위해서다.
    cooldown = restarting and bool(failure_cause())
    if cooldown and in_cooldown(now):
        print(f"최근 {COOLDOWN_SEC}초 안에 알림을 보내서 건너뜁니다", file=sys.stderr)
        return True, restarting
    if not send(build_message(unit, state, cause, redact(recent_logs(unit)))):
        return False, restarting
    if cooldown:
        record_sent(now)
    return True, restarting


def main(argv: list[str]) -> int:
    test = argv[1:] == ["--test"]
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL이 없어 알림을 보내지 않습니다 (.env.example 참고)", file=sys.stderr)
        return 1 if test else 0
    if test:
        return 0 if send(f"[시험] 실패 알림 설정이 동작합니다. 서버: {socket.gethostname()}") else 1

    unit = argv[1] if len(argv) > 1 else "ai-top-100.service"
    ok, restarting = notify(unit, time.time(), failure_cause())
    # 이 알림을 처리하는 동안 재시작된 봇이 다시 실패해 멈추면, 그 OnFailure 요청은 실행 중인 이 유닛에 합쳐져 사라진다.
    # 그래서 끝나기 전에 상태를 다시 보고, 그사이 멈췄으면 그 실패의 결과로 한 번 더 알린다.
    if restarting and not classify(unit_state(unit))[1]:
        ok, _ = notify(unit, time.time(), current_cause(unit))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
