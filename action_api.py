"""운영진 액션 서버(API) 호출. 대회 규정상 연동이 필수였다.

주소, 인증 방식, 경로, 응답 형식은 대회 당일 문제 페이지에서 공개되므로 그때 BASE_URL·headers()·
problem.py의 처리 함수를 채운다. 실패는 예외 대신 Result.ok=False로 돌려주고, 응답 해석은 Python이 한다.

재시도는 연결 실패(ConnectError, ConnectTimeout: 요청이 서버에 닿지 않음)일 때만 한다. 예약·등록처럼 상태를 바꾸는 요청을
시간 초과나 5xx 뒤에 다시 보내면 같은 작업이 두 번 처리될 수 있어서다.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import httpx

import config  # noqa: F401  .env 로드 (ACTION_API_BASE_URL, ACTION_API_KEY)

logger = logging.getLogger("action_api")

BASE_URL = os.environ.get("ACTION_API_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("ACTION_API_KEY", "")
# 호출 하나의 시간 제한(초). LLM 호출과 합쳐 응답 목표 60초 안에 들도록 짧게 둔다.
TIMEOUT_SEC = 10.0
MAX_RETRIES = 1
RETRY_DELAY_SEC = 0.5


@dataclass
class Result:
    ok: bool
    status: int = 0  # HTTP 상태 코드. 응답을 받지 못했으면 0
    data: object = None  # JSON이면 파싱한 값, 아니면 본문 문자열
    error: str = ""  # 로그용 짧은 이유. 사용자 입력 내용은 넣지 않는다.
    seconds: float = 0.0


# 상담(TC) ID를 실어 보내는 헤더 이름. 서버 안내에 맞춰 .env의 ACTION_TC_HEADER로 바꾼다.
TC_HEADER = os.environ.get("ACTION_TC_HEADER", "X-Test-Case-Id")


def headers(tc_id: str = "") -> dict:
    """인증 헤더(Bearer)와 TC ID 헤더. TC ID는 대화에서 전달받은 값을 한 상담 동안 그대로 넣는다."""
    h = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    if tc_id:
        h[TC_HEADER] = tc_id
    return h


# httpx.AsyncClient는 처음 쓴 이벤트 루프에 묶인다. llm.py와 같은 방식으로 루프가 바뀌면 새로 만든다.
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def client() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=TIMEOUT_SEC)
        _client_loop = loop
    return _client


def _parse(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text


async def call(method: str, path: str, *, params: dict | None = None, json: object = None, tc_id: str = "") -> Result:
    """BASE_URL + path로 요청한다. 2xx면 ok=True. 4xx·5xx도 본문을 data에 담아 돌려준다(오류 안내에 쓸 수 있게)."""
    if not BASE_URL:
        return Result(False, error="ACTION_API_BASE_URL not set")
    url = f"{BASE_URL}/{path.lstrip('/')}"
    started = time.monotonic()
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client().request(method, url, params=params, json=json, headers=headers(tc_id))
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY_SEC)
                continue
            result = Result(False, error="connect_error")
        except httpx.TimeoutException:
            result = Result(False, error="timeout")
        except httpx.HTTPError as e:
            result = Result(False, error=type(e).__name__)
        else:
            ok = response.is_success
            result = Result(ok, response.status_code, _parse(response), "" if ok else f"http_{response.status_code}")
        break
    result.seconds = time.monotonic() - started
    logger.info("action_api %s %s status=%s ok=%s %.1fs", method, path.split("?")[0], result.status, result.ok, result.seconds)
    return result
