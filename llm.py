"""Anthropic API 호출. 주제와 무관한 부분만 둔다(모델, 시간 제한, 구조화 출력, 실패 분류).

에이전트는 메시지마다 LLM을 한 번 부른다(extract). 응답은 JSON 스키마로 강제한 구조화 출력이고,
처리/거절/추가 질문 결정은 policy.py가 Python으로 한다. 실패는 예외 대신 Extraction.status로 돌려준다.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import anthropic

import config  # noqa: F401  .env 로드 (ANTHROPIC_API_KEY)

logger = logging.getLogger("llm")

# 모델은 여기만 바꾸면 된다. 응답이 느리면 "claude-sonnet-5-5"를 시험한다(10/30 짧은 답 기준 셋 다 약 2초).
MODEL = "claude-opus-5-5"
# Opus 5.5는 생각을 끌 수 없고 effort 기본값이 medium이라 명시한다. 해석·추출만 하므로 low로 시작한다.
EFFORT = "low"
# 생각 토큰도 max_tokens에 포함된다. 답변 500자 + JSON 틀에 넉넉히 둔다.
MAX_TOKENS = 4000
# 호출 하나의 시간 제한(초)과 재시도 횟수. 최악은 대략 TIMEOUT_SEC × (MAX_RETRIES + 1) + 백오프 ≈ 62초.
# SDK가 연결 오류, 408, 409, 429, 5xx, 시간 초과를 재시도한다.
TIMEOUT_SEC = 30.0
MAX_RETRIES = 1
# 안전 분류기가 거절하면 서버가 다른 모델로 다시 실행한다(거절 분류에 따라 모델을 고름). 400이 나면 False로 끈다.
USE_REFUSAL_FALLBACK = True
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Extraction.status 값
OK = "ok"
FORMAT_ERROR = "format_error"  # JSON이 아니거나 필수 항목 누락·형식 위반, max_tokens로 잘림
RATE_LIMITED = "rate_limited"  # 429 (재시도 후에도)
TIMEOUT = "timeout"
NETWORK = "network"
API_ERROR = "api_error"  # 그 밖의 4xx/5xx, 키 오류 등
REFUSAL = "refusal"


@dataclass
class Extraction:
    status: str
    data: dict = field(default_factory=dict)
    seconds: float = 0.0
    detail: str = ""  # 로그용. 사용자 입력 내용은 넣지 않는다.


# AsyncAnthropic은 처음 쓴 이벤트 루프에 묶인다. bot.py는 전용 루프 하나에서만 부르지만,
# 테스트처럼 루프가 바뀌면 새로 만든다.
_client: anthropic.AsyncAnthropic | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client = anthropic.AsyncAnthropic(timeout=TIMEOUT_SEC, max_retries=MAX_RETRIES)
        _client_loop = loop
    return _client


async def health_check() -> None:
    """토큰을 쓰지 않는 호출(모델 목록 한 쪽)로 키·서버 상태만 본다. 실패하면 예외를 그대로 낸다."""
    await client().models.list(limit=1)


def check_schema(data, schema: dict) -> str:
    """구조화 출력이 스키마를 지켰는지 최소한으로 다시 본다(필수 항목, 타입, enum). 문제가 없으면 빈 문자열."""
    if not isinstance(data, dict):
        return "not an object"
    types = {"string": str, "boolean": bool, "object": dict, "array": list}
    for name in schema.get("required", []):
        if name not in data:
            return f"missing {name}"
    for name, spec in schema.get("properties", {}).items():
        if name not in data:
            continue
        value = data[name]
        expected = types.get(spec.get("type"))
        if expected and not isinstance(value, expected):
            return f"bad type {name}"
        if "enum" in spec and value not in spec["enum"]:
            return f"bad enum {name}"
        if spec.get("type") == "object" and "properties" in spec:
            problem = check_schema(value, spec)
            if problem:
                return f"{name}.{problem}"
    return ""


async def extract(system: str, messages: list[dict], schema: dict) -> Extraction:
    """LLM을 한 번 불러 schema에 맞는 JSON을 받는다. 예외를 내지 않고 status로 결과를 알린다."""
    started = time.monotonic()

    def done(status: str, data: dict | None = None, detail: str = "") -> Extraction:
        result = Extraction(status, data or {}, time.monotonic() - started, detail)
        if status != OK:
            logger.warning("llm %s after %.1fs %s", status, result.seconds, detail)
        return result

    params = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
    )
    try:
        if USE_REFUSAL_FALLBACK:
            response = await client().beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
        else:
            response = await client().messages.create(**params)
    except anthropic.RateLimitError as e:
        return done(RATE_LIMITED, detail=f"request_id={e.request_id}")
    except anthropic.APITimeoutError:
        return done(TIMEOUT)
    except anthropic.APIConnectionError as e:
        return done(NETWORK, detail=type(e).__name__)
    except anthropic.APIStatusError as e:
        return done(API_ERROR, detail=f"status={e.status_code} request_id={e.request_id}")
    except anthropic.AnthropicError as e:  # API 키가 없는 경우 등
        return done(API_ERROR, detail=type(e).__name__)

    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        return done(REFUSAL, detail=f"category={category}")
    if response.stop_reason == "max_tokens":
        return done(FORMAT_ERROR, detail="max_tokens")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return done(FORMAT_ERROR, detail="invalid json")
    problem = check_schema(data, schema)
    if problem:
        return done(FORMAT_ERROR, detail=problem)
    return done(OK, data)
