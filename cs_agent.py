"""고객 응대(CS) 에이전트. 예선 문제(농수산물 조합 온라인몰 고객 문의)용으로 bot.py가 이 모듈의 reply를 부른다.

흐름: 메시지 → 상담 정보 파싱(TC ID, 인입 계정, 인입 일시) → Claude 도구 호출 루프(액션 API, cs_tools.py) → 짧은 글자 답 하나.
- 규칙·지식: 시작할 때 규칙(cs_config.RULES)과 data/ 아래 지식 파일(git 제외)을 읽어 시스템 프롬프트에 넣는다(프롬프트 캐시).
  파일이 바뀌면 프로그램을 다시 시작해야 반영된다.
- TC ID: 평가 때 대화로 전달되는 TC ID를 사용자별로 기억해 모든 액션 API 호출 헤더에 넣는다. 새 TC ID가 오면 상담을 새로 시작한다.
- 답변 형식: 글자 말풍선(SimpleText) 하나, 500자 이하, 짧고 빠르게.
  그래서 답은 항상 문자열 하나(말풍선 1개)로 돌려주고, 코드로 마크다운을 지우고 REPLY_MAX_CHARS에서 자른다.
- 시간: 도구 루프 최대 MAX_TOOL_ROUNDS회, 전체 TIME_BUDGET_SEC. 닿으면 도구 없이 그때까지의 결과로 답한다.
- 예산 가드(budget_stage): 턴마다 spend.remaining_usd()(봇이 집계한 남은 추정액)로 단계를 정한다.
  ECONOMY_BELOW_USD 이하면 절약 모드(ECONOMY_MODEL, 도구 ECONOMY_TOOL_ROUNDS회), STOP_BELOW_USD 이하면 LLM을 부르지 않고 고정 문구로 답한다(둘 다 기본으로 꺼져 있다).

이전 골격(agent.py: 구조화 추출 → policy.decide → 템플릿)은 시험(tests/test_agent.py)과 함께 그대로 둔다.
사용자별 잠금은 agent.user_lock을 같이 쓴다.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from pathlib import Path

import anthropic
import httpx

import agent
import alerts
import config  # noqa: F401  .env 로드
import cs_allergy
import cs_config
import cs_crisis
import cs_dates
import cs_tools
import llm
import policy
import spend

logger = logging.getLogger("agent")

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5-5")
EFFORT = os.environ.get("AGENT_EFFORT", "low")
MAX_TOKENS = 4000
MAX_TOOL_ROUNDS = 4
TIME_BUDGET_SEC = 50.0  # 응답 목표 60초 안. 카카오 전송 시간 여유를 둔다.
LLM_TIMEOUT_SEC = 30.0
MIN_CALL_SEC = 6.0  # 남은 시간이 이보다 적으면 LLM을 더 부르지 않고 그때까지의 결과로 끝낸다
REPLY_MAX_CHARS = 480  # 말풍선 500자 상한에 여유를 둔다.
CONTENT_MAX_CHARS = 2000
# 한 상담에서 기억하는 메시지 수 상한. 넘으면 그 상담 기록을 글자만 남겨 새로 시작한다(보통 닿지 않는다).
HISTORY_MAX_MESSAGES = 60
# 캐시 예열(warm_cache) 간격. 프롬프트 캐시(1시간)는 읽힐 때마다 수명이 다시 늘어나므로 1시간 안쪽마다 부르면 계속 살아 있다.
WARM_INTERVAL_SEC = float(os.environ.get("WARM_INTERVAL_SEC", "3000"))
# 예열 필요 여부를 보는 간격. 같은 모델의 마지막 호출(답변·예열)이 WARM_INTERVAL_SEC보다 오래됐을 때만 실제로 부르므로,
# 캐시가 가장 오래 안 쓰이는 시간은 WARM_INTERVAL_SEC + WARM_CHECK_SEC(기본 55분)로 1시간 안쪽이다.
WARM_CHECK_SEC = float(os.environ.get("WARM_CHECK_SEC", "300"))
WARM_MAX_TOKENS = 32  # 예열은 답이 필요 없다. 출력 토큰만 작게 하고 캐시 앞부분(model·tools·system 첫 블록)은 실제 호출과 같게 둔다.

# 예산 가드. 남은 추정액(달러)이 기준 이하이면 단계를 낮춘다. 추정치라 실제 잔액과 다를 수 있어 여유를 둔다.
# 절약·멈춤 단계는 기본으로 끈다(10/31 사용자 결정: 심사 중 품질을 낮추지 않고, 경고 알림을 보고 사람이 충전). 숫자를 넣으면 켜진다.
def _usd_or_off(name: str) -> float | None:
    value = os.environ.get(name, "off").strip().lower()
    return None if value in ("", "off", "none") else float(value)


ECONOMY_BELOW_USD = _usd_or_off("ECONOMY_BELOW_USD")
STOP_BELOW_USD = _usd_or_off("STOP_BELOW_USD")
ECONOMY_MODEL = os.environ.get("ECONOMY_MODEL", "claude-haiku-5-5")  # adaptive 사고·effort low·tool_choice none 지원, 거절 대체는 없다
ECONOMY_TOOL_ROUNDS = 3
NORMAL, ECONOMY, STOP = "normal", "economy", "stop"
STOP_TEXT = "죄송합니다. 지금은 상담이 어려워 담당자에게 연결이 필요합니다. 잠시 후 다시 문의해 주세요."
STOP_ESCALATED_TEXT = "죄송합니다. 지금은 상담이 어려워 담당자 연결을 요청해 두었습니다(접수번호 {id}). 잠시 후 다시 문의해 주세요."
STOP_ESCALATION_REASON = cs_tools.OPS_TAG + " 상담 시스템 한도로 자동 상담이 멈춰 담당자 연결이 필요합니다. 고객 문의 확인 후 연락 부탁드립니다."
_stage_alerted: set[str] = set()
_last_call: dict[str, float] = {}  # 모델별 마지막 성공 호출 시각(time.monotonic). 예열을 건너뛸지 판단한다.

ROOT = Path(__file__).parent
# 시스템 프롬프트에 넣을 지식 파일(ROOT 기준 glob, 순서대로). data/knowledge_files.txt가 있으면 그 목록(한 줄에 glob 하나, #은 주석)을 쓴다.
# 인터뷰 정리본(knowledge.md), 받은 문서(data/docs/*.txt, data/knowledge_docs/*_원문.md). 모두 git 제외다.
KNOWLEDGE_GLOBS = ["data/knowledge.md", "data/docs/*.txt", "data/knowledge_docs/*_원문.md"]
KNOWLEDGE_LIST = ROOT / "data" / "knowledge_files.txt"

# 앞머리 "- id:" 줄은 위치가 분명해 여러 ID 형식(T-100, TC-01, T03, T-03-A)을 받는다. 본문 탐색(TC_BARE)은 오인을 막으려고 좁은 형식만.
TC_LINE = re.compile(r"(?:^|\n)\s*-?\s*id\s*[:：]\s*([A-Za-z]{1,4}-?\d{1,4}(?:-[A-Za-z0-9]{1,4})?)\b")
TC_BARE = re.compile(r"(?<![A-Za-z0-9-])([A-Z]-\d{2}(?:-[a-z])?)(?![A-Za-z0-9-])")
ACCOUNT_LINE = re.compile(r"인입\s*계정\s*[:：]\s*(?:회원\s*로그인\s*[·ㆍ]\s*)?([^·ㆍ\n]+?)\s*[·ㆍ]\s*([0-9*\-]+)")
MEMBER_ID = re.compile(r"\bMB-\d{1,8}\b")  # 앞머리에 회원 ID가 함께 오면 그대로 쓴다
HEADER_MAX_CHARS = 800
RECEIVED_LINE = re.compile(r"상담\s*인입\s*일시\s*[:：]\s*([^\n]+)")
MEDIA_LINE = re.compile(r"(?:^|\n)\s*(image|audio)\s*[:：]\s*(https?://\S+)")
KAKAO_IMAGE = "https://talk.kakaocdn.net/"
AUDIO_EXT = (".m4a", ".mp3", ".wav", ".aac", ".amr", ".ogg")

RULES = cs_config.RULES  # 시스템 프롬프트 규칙(data/rules.md, 없으면 rules.example.md)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def knowledge_globs() -> list[str]:
    if KNOWLEDGE_LIST.exists():
        lines = [line.strip() for line in _read(KNOWLEDGE_LIST).splitlines()]
        return [line for line in lines if line and not line.startswith("#")]
    return KNOWLEDGE_GLOBS


def load_knowledge() -> str:
    parts, seen = [], set()
    for pattern in knowledge_globs():
        for path in sorted(ROOT.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if text := _read(path):
                parts.append(f"<<{path.relative_to(ROOT).as_posix()}>>\n{text}")
    return "\n\n".join(parts)


KNOWLEDGE = load_knowledge()
TOOLS = cs_tools.tool_definitions()
logger.info("knowledge loaded chars=%d tools=%d model=%s", len(KNOWLEDGE), len(TOOLS), MODEL)


def static_system() -> str:
    return f"{RULES}\n[지식] (인터뷰 정리와 규정 문서. 상태가 '추측'이어도 다른 근거와 어긋나지 않으면 그대로 적용한다. '충돌'이면 규정 문서·결정 권한자 쪽을 따르고, 판단이 안 서면 확인 필요로 이관한다)\n{KNOWLEDGE}"


STATIC_SYSTEM = static_system()


def session_text(s: cs_tools.Session) -> str:
    lines = ["[상담 정보]"]
    lines.append(f"- 상담 인입 일시(오늘 기준): {s.received_at or '모름'}")
    lines += cs_dates.session_lines(s.received_at)
    if s.member_name:
        lines.append(f"- 인입 계정: 회원 로그인 · {s.member_name} · 전화 끝 {s.phone_last4 or '모름'}")
    if s.member:
        m = s.member
        lines.append(f"- 특정한 회원: {m.get('member_id')} (유형 {m.get('member_type')}, 등급 {m.get('grade')}, 상태 {m.get('status')})")
    elif s.member_name:
        lines.append("- 회원을 특정하지 못함: 본인 확인 전에는 개인 주문 정보를 안내하지 않는다")
    if not s.tc_id:
        lines.append("- TC ID 없음: 액션 API를 쓸 수 없다(calc_date는 쓸 수 있다). 지식으로만 답한다")
    if s.crisis:
        lines.append(cs_crisis.CRISIS_RULES)
        if s.crisis_escalation_id:
            lines.append(f"- 담당자 이관은 이미 실제로 접수됐다(접수번호 {s.crisis_escalation_id}, 앞 답에서 안내함). 번호를 부정하거나 다시 접수하지 않는다.")
    return "\n".join(lines)


# 사용자별 상태: {"session": cs_tools.Session, "messages": [...]} (assistant 내용은 thinking 포함 그대로 다시 보낸다)
_states: dict[str, dict] = {}


def case_header(content: str) -> str:
    """메시지 앞머리의 상담 정보 묶음("case metadata" 또는 "- id:" 줄로 시작해 빈 줄까지). 없으면 빈 문자열."""
    head = content.lstrip()[:HEADER_MAX_CHARS].split("\n\n", 1)[0]
    return head if head.startswith("case metadata") or TC_LINE.match("\n" + head) else ""


def parse_case(content: str, state: dict) -> None:
    """메시지 앞머리에서 TC ID·인입 계정·인입 일시를 찾아 상담 정보를 채운다. 다른 TC ID면 상담을 새로 시작한다.

    인입 계정은 상담 채널이 로그인 세션으로 붙이는 정보라 인증된 본인으로 보지만,
    앞머리 묶음으로 왔을 때만, 그리고 상담을 시작하는 메시지에서만 받는다. 본문 속 "나 누구야" 같은 자기 주장이나
    대화 중간에 붙여 보낸 계정 줄로는 계정이 바뀌지 않는다. 주문 접근은 이와 별개로 cs_tools가 회원ID로 대조한다.
    """
    head = case_header(content)
    s: cs_tools.Session = state["session"]
    m = TC_LINE.search("\n" + head) if head else (None if s.tc_id else TC_BARE.search(content))
    tc_id = m.group(1) if m else ""
    starting = False
    if tc_id and tc_id != s.tc_id:
        if s.tc_id:
            logger.info("new case started")
        s = state["session"] = cs_tools.Session(tc_id=tc_id)
        state["messages"] = []
        starting = True
    if not head or not (starting or (not s.member_name and not state["messages"])):
        return
    if acc := ACCOUNT_LINE.search(head):
        s.member_name, s.member, s.member_id = acc.group(1).strip(), None, ""
        s.phone_last4 = re.sub(r"\D", "", acc.group(2))[-4:]
    if mid := MEMBER_ID.search(head):
        s.member_id = mid.group(0)
    if rec := RECEIVED_LINE.search(head):
        s.received_at = rec.group(1).strip()


# 서버가 직접 내려받는 이미지 주소(카카오 CDN만). 그 밖의 주소는 내려받지 않고 Claude에 URL로 넘긴다(사설망 접근 방지).
DOWNLOAD_HOSTS = re.compile(r"^https://[a-z0-9.-]*\.kakaocdn\.net/")


async def _image_block(url: str) -> dict:
    """카카오 이미지는 받은 즉시 내려받아 base64로 넣는다(주소가 만료된다). 실패하거나 다른 주소면 URL을 그대로 넘긴다."""
    if not DOWNLOAD_HOSTS.match(url):
        return {"type": "image", "source": {"type": "url", "url": url}}
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as c:
            r = await c.get(url)
        media = r.headers.get("content-type", "image/jpeg").split(";")[0]
        if r.is_success and media.startswith("image/") and len(r.content) < 4_500_000:
            return {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(r.content).decode()}}
    except httpx.HTTPError:
        pass
    return {"type": "image", "source": {"type": "url", "url": url}}


async def user_content(content: str) -> list[dict]:
    """고객 메시지를 Claude 입력 블록으로. 사진은 이미지로, 음성은 들을 수 없다는 표시로 바꾼다."""
    text = content[:CONTENT_MAX_CHARS]
    blocks: list[dict] = []
    media = [(m.group(1), m.group(2)) for m in MEDIA_LINE.finditer(text)]
    stripped = text.strip()
    if not media and stripped.startswith("http") and " " not in stripped:
        kind = "audio" if stripped.lower().split("?")[0].endswith(AUDIO_EXT) else "image" if stripped.startswith(KAKAO_IMAGE) or "kakaocdn" in stripped else ""
        if kind:
            media = [(kind, stripped)]
    for kind, url in media:
        text = text.replace(url, "")
        if kind == "image":
            blocks.append(await _image_block(url))
        else:
            text += ("\n[고객이 음성 메시지를 보냄. 음성 파일은 직접 들을 수 없다. 메시지에 음성 요지(예: \"[음성 00:15] ...\")가 함께 있으면 "
                     "그 요지를 \"~을 문의하셨군요\"처럼 글로 되짚어 확인하고, 요지에 없는 사유(하자인지 단순 변심인지 등)는 단정하지 말고 묻는다. "
                     "요지가 없으면 받은 사실을 말하고 요지를 글로 짧게 보내 달라고 부탁한다]")
    blocks.append({"type": "text", "text": text.strip() or "(사진만 보냄)"})
    return blocks


def clean(text: str) -> str:
    text = policy.clean_reply(text) if hasattr(policy, "clean_reply") else text
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= REPLY_MAX_CHARS:
        return text
    cut = text[:REPLY_MAX_CHARS]
    end = max(cut.rfind(". "), cut.rfind("다."), cut.rfind("요."), cut.rfind("\n"))
    return cut[: end + 2].strip() if end > REPLY_MAX_CHARS // 2 else cut.rstrip() + "…"


def budget_stage() -> str:
    """남은 추정액으로 단계를 정한다. 절약·멈춤 단계에 처음 들어갈 때 디스코드로 한 번 알린다(재시작하면 다시 센다)."""
    remaining = spend.remaining_usd()
    stage = STOP if STOP_BELOW_USD is not None and remaining <= STOP_BELOW_USD else ECONOMY if ECONOMY_BELOW_USD is not None and remaining <= ECONOMY_BELOW_USD else NORMAL
    if stage != NORMAL and stage not in _stage_alerted:
        _stage_alerted.add(stage)
        if stage == STOP:
            _stage_alerted.add(ECONOMY)  # 멈춤 뒤에 절약 알림이 늦게 오지 않게
            message = (f"**[크레딧 한도 도달]** 남은 추정 ${remaining:.2f} (기준 ${STOP_BELOW_USD:.2f} 이하). "
                       "LLM을 부르지 않고 고정 문구로 답합니다(예열도 멈춤).\n"
                       "충전한 뒤 .env의 CREDIT_TOPUP_USD·CREDIT_TOPUP_AT를 고치고 재시작하면 0부터 다시 셉니다.")
        else:
            message = (f"**[크레딧 절약 모드]** 남은 추정 ${remaining:.2f} (기준 ${ECONOMY_BELOW_USD:.2f} 이하). "
                       f"답변 모델을 {ECONOMY_MODEL}로 바꾸고 도구 호출을 {ECONOMY_TOOL_ROUNDS}회로 줄였습니다.\n"
                       "충전한 뒤 .env의 CREDIT_TOPUP_USD·CREDIT_TOPUP_AT를 고치고 재시작하면 원래대로 돌아갑니다.")
        logger.warning("%s", message.splitlines()[0])
        alerts._spawn_send(message)
    return stage


def stage_model(stage: str) -> str:
    return ECONOMY_MODEL if stage == ECONOMY else MODEL


def _params(s: cs_tools.Session, messages: list, use_tools: bool, model: str = MODEL) -> dict:
    params = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[
            {"type": "text", "text": STATIC_SYSTEM, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            {"type": "text", "text": session_text(s)},
        ],
        messages=messages,
        output_config={"effort": EFFORT},
    )
    # 도구 정의는 항상 넣는다(프롬프트 캐시 앞부분이 같아지고, TC ID가 없어도 calc_date는 쓸 수 있다).
    # 액션 API 도구는 TC ID가 없으면 cs_tools.execute가 거절한다. 도구를 더 못 쓰는 차례에는 호출만 막는다.
    params["tools"] = TOOLS
    if not use_tools:
        params["tool_choice"] = {"type": "none"}
    return params


async def _create(params: dict, timeout: float):
    """LLM 호출. 결과(성공·오류)를 alerts에 알려 크레딧 소진을 바로 알린다."""
    try:
        response = await _api_create(params, timeout)
    except anthropic.APIError as e:
        alerts.record_llm_result(e)
        raise
    alerts.record_llm_result(None)
    _last_call[params["model"]] = time.monotonic()
    try:
        for message in spend.record(params["model"], getattr(response, "usage", None)):
            logger.warning("%s", message.splitlines()[0])
            alerts._spawn_send(message)
    except Exception as e:  # noqa: BLE001  집계 실패가 답변을 막지 않게 한다
        logger.warning("spend record failed: %s", type(e).__name__)
    return response


async def _api_create(params: dict, timeout: float):
    # 재시도는 남은 시간이 넉넉할 때만(timeout × 2가 예산 안일 때)
    c = llm.client().with_options(timeout=max(3.0, timeout), max_retries=1 if timeout >= LLM_TIMEOUT_SEC and TIME_BUDGET_SEC >= 2 * timeout else 0)
    if llm.USE_REFUSAL_FALLBACK and not params["model"].startswith("claude-haiku"):
        return await c.beta.messages.create(betas=[llm.FALLBACK_BETA], fallbacks="default", **params)
    return await c.messages.create(**params)


def _text(response) -> str:
    return "\n".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()


POSTED_FAIL_TEXT = "요청하신 접수는 처리됐는데 결과 안내가 늦어지고 있습니다. 잠시 후 \"접수 결과 알려 주세요\"라고 보내 주세요."
FAIL_TEXT = "죄송합니다. 지금 확인이 늦어지고 있습니다. 잠시 후 같은 내용을 한 번 더 보내 주세요."


async def _execute(s: cs_tools.Session, block, timeout: float) -> tuple[str, bool]:
    """도구 하나를 시간 제한 안에서 실행한다. 시간 초과는 그 도구만 오류로 돌려준다(같이 실행한 다른 도구의 결과는 남긴다)."""
    try:
        return await asyncio.wait_for(cs_tools.execute(s, block.name, block.input), timeout)
    except asyncio.TimeoutError:
        logger.warning("tool %s timeout", block.name)
        return "시간 초과로 결과를 받지 못했습니다. 접수였다면 처리됐는지 확인되지 않았으니 다시 접수하지 말고 확인 중이라고 안내하세요.", True


async def _crisis_turn(s: cs_tools.Session, messages: list, content: str, info: dict, started: float, stage: str = NORMAL) -> str:
    """위기 상태의 답(cs_crisis). 첫 답은 코드가 이관 한 번 + 고정 틀, 이후는 LLM(도구 없이)이 쓰고 금지 표현을 검사한다.
    멈춤 단계에서는 LLM 대신 안전 고정 문구로 답한다."""
    if not s.crisis_resource_given:
        await cs_tools.crisis_escalate(s)
        s.crisis_resource_given = True
        answer = cs_crisis.first_reply(s.crisis_escalation_id)
    elif stage == STOP:
        answer = cs_crisis.SAFE_REPLY
    else:
        try:
            left = TIME_BUDGET_SEC - (time.monotonic() - started)
            t0 = time.monotonic()
            response = await asyncio.wait_for(_create(_params(s, messages, False, stage_model(stage)), min(LLM_TIMEOUT_SEC, left - 1)), left)
            info["llm"] += time.monotonic() - t0
            answer = cs_crisis.safe_followup(_text(response), content)
        except (anthropic.APIError, asyncio.TimeoutError):
            answer = cs_crisis.SAFE_REPLY
    messages.append({"role": "assistant", "content": answer})
    return answer


async def run(state: dict, content: str) -> tuple[str, dict]:
    started = time.monotonic()
    s: cs_tools.Session = state["session"]
    s.order_gets = {}
    stage = budget_stage()
    info = {"rounds": 0, "llm": 0.0, "stage": stage}
    if cs_crisis.detect(content):
        s.crisis = True
    if stage == STOP and not s.crisis:
        # LLM 없이 고정 문구. 기록에는 남기지 않는다(대화 상태는 재시작 때 어차피 사라진다).
        return await _stop_reply(s), info
    if not s.crisis:
        await cs_tools.identify_member(s)
    messages: list = state["messages"]
    turn_start = len(messages)
    posted_before = len(s.posted)
    messages.append({"role": "user", "content": await user_content(content)})
    answer = ""
    if s.crisis:
        return clean(await _crisis_turn(s, messages, content, info, started, stage)), info
    model = stage_model(stage)
    max_rounds = ECONOMY_TOOL_ROUNDS if stage == ECONOMY else MAX_TOOL_ROUNDS
    try:
        for round_no in range(max_rounds + 1):
            left = TIME_BUDGET_SEC - (time.monotonic() - started)
            if left < MIN_CALL_SEC:
                break
            use_tools = round_no < max_rounds and left > 15
            t0 = time.monotonic()
            response = await asyncio.wait_for(_create(_params(s, messages, use_tools, model), min(LLM_TIMEOUT_SEC, left - 1)), left)
            info["llm"] += time.monotonic() - t0
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                answer = _text(response)
                break
            info["rounds"] += 1
            if any(b.name == "flag_crisis" for b in calls):
                s.crisis = True  # 같은 차례의 다른 도구도 실행 전에 막히게 먼저 켠다
            left = TIME_BUDGET_SEC - (time.monotonic() - started)
            results = await asyncio.gather(*(_execute(s, b, max(1.0, left - MIN_CALL_SEC)) for b in calls))
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": text, **({"is_error": True} if err else {})}
                for b, (text, err) in zip(calls, results)
            ]})
            if s.crisis:
                return clean(await _crisis_turn(s, messages, content, info, started, stage)), info
    except (anthropic.APIError, asyncio.TimeoutError) as e:
        logger.warning("cs agent error %s", type(e).__name__)
        if len(s.posted) > posted_before:
            # 이번 턴에 접수가 이미 됐으면 기록을 지우지 않는다(다음 턴이 같은 접수를 다시 하지 않게). 끝이 tool_use면 결과가 없으므로 뺀다.
            if messages and messages[-1]["role"] == "assistant":
                messages.pop()
            return POSTED_FAIL_TEXT, info
        # 실패한 턴은 기록에서 뺀다(다음 메시지가 깨진 기록을 이어받지 않게)
        del messages[turn_start:]
        return FAIL_TEXT, info
    if not answer:
        answer = "확인한 내용을 정리하는 데 시간이 걸리고 있습니다. 잠시 후 한 번 더 말씀해 주세요."
    guarded = cs_allergy.guard(answer, content)
    if guarded != answer:
        if messages and messages[-1]["role"] == "assistant":
            messages[-1] = {"role": "assistant", "content": guarded}
        answer = guarded
    if len(messages) > HISTORY_MAX_MESSAGES:
        state["messages"] = [{"role": "user", "content": "(이전 상담 이어서)"}, {"role": "assistant", "content": answer}]
    return clean(answer), info


async def _stop_reply(s: cs_tools.Session) -> str:
    """멈춤 단계의 답. TC ID가 있으면 상담마다 한 번 코드가 담당자 이관(운영 태그)을 접수하고 접수번호를 붙인다."""
    if not s.tc_id:
        return STOP_TEXT
    if not s.stop_escalated:
        s.stop_escalated = True
        r = await cs_tools.escalate(s, cs_tools.HANDOFF_LANE, STOP_ESCALATION_REASON)
        if r.ok and isinstance(r.data, dict):
            s.stop_escalation_id = str(r.data.get("escalation_id", ""))
        logger.info("stop escalation status=%s", r.status)
    return STOP_ESCALATED_TEXT.format(id=s.stop_escalation_id) if s.stop_escalation_id else STOP_TEXT


async def reply(content: str, user_key: str) -> str:
    if not content:
        return "문의하실 내용을 입력해 주세요."
    async with agent.user_lock(user_key):
        state = _states.setdefault(user_key, new_state()) if user_key else new_state()
        parse_case(content, state)
        started = time.monotonic()
        answer, info = await run(state, content)
        s = state["session"]
        logger.info("cs agent tc=%s stage=%s rounds=%d llm=%.1fs total=%.1fs calls=%s chars=%d",
                    s.tc_id or "-", info["stage"], info["rounds"], info["llm"], time.monotonic() - started, ",".join(s.calls[-8:]), len(answer))
        return answer


def new_state() -> dict:
    return {"session": cs_tools.Session(), "messages": []}


def warm_params(model: str = MODEL) -> dict:
    """예열 요청. 캐시 지점(system 첫 블록)까지는 실제 답변 첫 호출(_params, 도구 사용 차례)과 같고, 그 뒤(상담 정보·메시지)와 출력 길이만 다르다."""
    params = _params(cs_tools.Session(), [{"role": "user", "content": "."}], True, model)
    params["max_tokens"] = WARM_MAX_TOKENS
    return params


async def warm_cache() -> None:
    """프롬프트 캐시를 만들거나 수명을 늘린다. 시작할 때와 WARM_CHECK_SEC마다 gateway가 부른다(alerts.watch_health).
    캐시가 살아 있으면 여러 상담이 동시에 시작돼도 비싼 캐시 쓰기 대신 캐시 읽기가 된다. 실패하면 예외를 그대로 낸다.
    지금 단계의 모델로 WARM_INTERVAL_SEC 안에 성공한 호출(답변·예열)이 있으면 캐시가 살아 있으므로 부르지 않는다. 멈춤 단계에서도 부르지 않는다."""
    stage = budget_stage()
    if stage == STOP:
        return
    model = stage_model(stage)
    last = _last_call.get(model)
    if last is not None and time.monotonic() - last < WARM_INTERVAL_SEC:
        return
    started = time.monotonic()
    response = await _create(warm_params(model), LLM_TIMEOUT_SEC)
    usage = getattr(response, "usage", None)
    logger.info("cache warm ok model=%s write=%s read=%s %.1fs", model, getattr(usage, "cache_creation_input_tokens", None),
                getattr(usage, "cache_read_input_tokens", None), time.monotonic() - started)


health_check = llm.health_check
