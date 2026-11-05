"""액션 API를 LLM 도구로 바꾸고, 실행 전에 코드로 권한·형식을 확인한다(cs_agent.py가 쓴다).

엔드포인트 표(번호·이름·메서드·경로·설명)는 cs_config(data/cs_config.json, 없으면 cs_config.example.json)에서 읽는다.
도구 이름은 뜻이 드러나는 이름을 쓰고, 설명 앞에 번호를 붙인다(로그·대조용).

코드가 막는 것(프롬프트가 아니라 코드라서 인젝션으로 풀리지 않는다):
- 경로에 들어가는 값은 영문·숫자·하이픈·밑줄만 받는다(경로 조작 방지).
- 로그인 계정(세션 회원)이 아닌 회원의 주문·배송·결제·회원 정보는 내용을 지우고 "본인 주문 아님"만 돌려준다(타 계정 조회).
- 접수(POST)는 본인 주문·본인 배송·본인 결제·본인 회원에만 보낸다. 쿠폰 발급은 설정의 보상 쿠폰(issuable_promos)만 보낸다.
접수 API가 정책을 확인하지 않고 그대로 받는다고 가정하므로, 정책 판단은 프롬프트와 지식으로 하고 코드는 대상만 막는다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import action_api
import cs_config
import cs_crisis
import cs_dates

logger = logging.getLogger("cs_tools")

# 경로 값 형식. 주문번호, 회원 ID, 배송 ID, 결제 승인번호, SKU 등
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
# 주문번호 형식(설정 order_no_pattern). 고객이 덜 불러 준 번호로 조회·접수하지 않는다(다른 주문번호 호출 방지).
ORDER_NO = re.compile(cs_config.CONFIG["order_no_pattern"])
# 429 시간 창 한도일 때 Retry-After가 이보다 짧으면 한 번 기다렸다 다시 보낸다(응답 60초 목표 안에서).
RETRY_AFTER_MAX_SEC = 5.0
# LLM에 돌려주는 도구 결과 최대 글자 수(검색 결과가 길 때 입력 토큰과 지연을 줄인다)
RESULT_MAX_CHARS = 6000
# 봇이 발급 요청할 수 있는 쿠폰(설정 issuable_promos, 규정상 보상 쿠폰만). 임의 코드 발급 요구는 여기서 막힌다.
ISSUABLE_PROMOS = set(cs_config.CONFIG.get("issuable_promos", []))
_ESC = cs_config.section("escalation")
HANDOFF_LANE = _ESC.get("handoff_lane", "HANDOFF")
CLAIM_LANE = _ESC.get("claim_lane", "CLAIM")
OPS_TAG = _ESC.get("ops_tag", "[OPS]")


@dataclass
class Endpoint:
    no: str  # 번호(로그·대조용)
    name: str  # 도구 이름
    method: str
    path: str  # /api/orders/{order_no}
    desc: str
    query: list[str] = field(default_factory=list)  # 필수 검색 파라미터
    body: dict = field(default_factory=dict)  # 본문 속성 {이름: JSON 스키마}
    owner: str = ""  # 대상 소유 확인 방식: order, member, shipment, approval, "" (확인 안 함)


def _endpoint(d: dict) -> Endpoint:
    return Endpoint(d["no"], d["name"], d["method"].upper(), d["path"], d.get("desc", ""),
                    query=list(d.get("query", [])), body=dict(d.get("body", {})), owner=d.get("owner", ""))


ENDPOINTS = [_endpoint(d) for d in cs_config.CONFIG.get("endpoints", [])]
BY_NAME = {e.name: e for e in ENDPOINTS}


def ep(name: str) -> Endpoint:
    """코드가 직접 부르는 엔드포인트(회원 특정, 소유 확인, 이관). 설정에 없으면 시작할 때 KeyError로 바로 드러난다."""
    return BY_NAME[name]


def ep_path(name: str, **values: str) -> str:
    path = ep(name).path
    for k, v in values.items():
        path = path.replace("{" + k + "}", v)
    return path


for _required in ("get_order", "get_shipment", "get_member", "search_members_by_name", "get_payment", "create_escalation"):
    ep(_required)

_PATH_PARAM = re.compile(r"\{(\w+)\}")


def path_params(e: Endpoint) -> list[str]:
    return _PATH_PARAM.findall(e.path)


def tool_definitions() -> list[dict]:
    """Anthropic 도구 정의. 순서가 바뀌면 프롬프트 캐시가 깨지므로 ENDPOINTS 순서 그대로 만든다."""
    tools = []
    for e in ENDPOINTS:
        props = {p: {"type": "string"} for p in path_params(e) + e.query}
        props.update(e.body)
        tools.append({
            "name": e.name,
            "description": f"{e.no} {e.method} {e.path.split('{')[0].rstrip('/')}: {e.desc}",
            "input_schema": {"type": "object", "properties": props, "required": list(props), "additionalProperties": False},
        })
    tools += [CALC_DATE_TOOL, FLAG_CRISIS_TOOL]
    return tools


# 액션 API가 아닌 로컬 계산 도구(API 호출 한도를 쓰지 않는다). 날짜·기한은 LLM이 세지 않고 이 도구로 계산한다.
CALC_DATE_TOOL = {
    "name": "calc_date",
    "description": "날짜·기한 계산, 수산물 중량 미달 판정, 일부 하자 부분환불 금액(로컬, 즉시). 날짜·비율·금액을 직접 세지 말고 이것을 쓴다. mode: "
                   + "; ".join(f"{k}={v}" for k, v in cs_dates.MODES.items())
                   + ". base는 YYYY-MM-DD(비우면 오늘=상담 인입일). 결과의 날짜를 그대로 안내한다",
    "input_schema": {"type": "object", "properties": {
        "mode": {"type": "string", "enum": list(cs_dates.MODES)},
        "base": {"type": "string", "description": "YYYY-MM-DD, 비우면 오늘"},
        "days": {"type": "integer"},
        "labeled_g": {"type": "number"}, "shipped_g": {"type": "number"}, "received_g": {"type": "number"}, "paid": {"type": "integer"},
        "defects": {"type": "integer"}, "units": {"type": "integer"}, "prior_refunded": {"type": "integer"},
    }, "required": ["mode"], "additionalProperties": False},
}

FLAG_CRISIS_TOOL = {
    "name": "flag_crisis",
    "description": "고객 말에 자·타해 언급, 구체적 방법·계획, 시간 임박(오늘 밤, 다 정리해 두었다), 이별 인사(마지막으로 먹어 보고 싶었다, "
                   "그동안 고마웠다, 이제 주문할 일 없을 것 같다) 같은 위기신호가 있거나 애매하면 다른 도구 없이 이것만 부른다. "
                   "음성 요지나 상품 문의 중에 나와도 같다. 몸 이상·섭취 후 복통은 해당하지 않는다. 부르면 응대는 코드가 맡는다",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}


async def escalate(s: Session, lane: str, reason: str) -> action_api.Result:
    """코드가 직접 담당자 이관을 접수한다(위기, 크레딧 한도). 성공하면 posted에 남긴다."""
    e = ep("create_escalation")
    r = await _call(s, e.no, "POST", e.path, json_body={"lane": lane, "reason": reason})
    if r.ok:
        s.posted.append(e.no)
    return r


async def crisis_escalate(s: Session) -> None:
    """위기 이관을 한 번만 접수한다(코드가 직접). TC ID가 없으면 건너뛴다. 고객 말은 인용하지 않는다."""
    if s.crisis_escalated or not s.tc_id:
        return
    s.crisis_escalated = True
    r = await escalate(s, HANDOFF_LANE, cs_crisis.ESCALATION_REASON)
    if r.ok and isinstance(r.data, dict):
        s.crisis_escalation_id = str(r.data.get("escalation_id", ""))
    logger.info("crisis escalation status=%s", r.status)


@dataclass
class Session:
    """한 상담(TC)의 정보. 새 TC ID가 오면 새로 만든다."""
    tc_id: str = ""
    member_name: str = ""  # 인입 계정 이름
    phone_last4: str = ""  # 인입 계정 전화 뒤 4자리
    member_id: str = ""  # 이름·전화로 특정한 회원(못 찾으면 빈 값)
    member: dict | None = None
    received_at: str = ""  # 상담 인입 일시 원문(예: 2030-03-05(화) 15:16)
    order_owner: dict[str, str] = field(default_factory=dict)  # 주문번호 → 회원ID
    shipment_order: dict[str, str] = field(default_factory=dict)  # 배송ID → 주문번호
    approval_order: dict[str, str] = field(default_factory=dict)  # 결제 승인번호 → 주문번호
    subscription_owner: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)  # 호출한 API 번호(로그·시험용)
    posted: list[str] = field(default_factory=list)  # 성공한 접수(POST) API 번호
    # 이번 턴의 주문 조회(get_order) 결과. 도구를 병렬로 실행할 때 소유 확인과 get_order가 같은 주문을 두 번 부르지 않게 한다.
    # cs_agent가 턴마다 비우고, 접수가 성공하면 비운다(접수 뒤에는 refund_ids 등이 바뀐다).
    order_gets: dict = field(default_factory=dict, repr=False)
    # 위기신호(cs_crisis). 한 번 위기 상태가 되면 그 상담 동안 유지한다.
    crisis: bool = False
    crisis_escalated: bool = False  # 이관을 시도했는지(성공 여부와 관계없이 한 번만)
    crisis_escalation_id: str = ""
    crisis_resource_given: bool = False  # 109 안내를 했는지
    # 크레딧 한도(cs_agent 멈춤 단계)로 담당자 이관을 시도했는지(한 번만)와 접수번호
    stop_escalated: bool = False
    stop_escalation_id: str = ""


async def _call(s: Session, no: str, method: str, path: str, params: dict | None = None, json_body: object = None) -> action_api.Result:
    s.calls.append(no)
    result = await action_api.call(method, path, params=params, json=json_body, tc_id=s.tc_id)
    if result.status == 429 and isinstance(result.data, dict) and "누적" not in str(result.data.get("message", "")):
        # 시간 창 한도. Retry-After를 action_api가 넘기지 않으므로 짧게 한 번만 기다린다.
        await asyncio.sleep(min(RETRY_AFTER_MAX_SEC, 2.0))
        result = await action_api.call(method, path, params=params, json=json_body, tc_id=s.tc_id)
    return result


async def identify_member(s: Session) -> None:
    """인입 계정 이름으로 회원을 검색(search_members_by_name)하고 전화 뒤 4자리로 특정한다.
    앞머리에 회원 ID가 함께 왔으면 검색하지 않고 회원 조회(get_member)로 정보만 채운다."""
    if not s.tc_id or s.member:
        return
    if s.member_id:
        r = await _call(s, ep("get_member").no, "GET", ep_path("get_member", member_id=s.member_id))
        s.member = r.data if r.ok and isinstance(r.data, dict) else {"member_id": s.member_id}
        return
    if not s.member_name:
        return
    e = ep("search_members_by_name")
    r = await _call(s, e.no, "GET", e.path, params={e.query[0]: s.member_name})
    if r.ok and isinstance(r.data, list):
        hits = [m for m in r.data if s.phone_last4 and str(m.get("phone", "")).endswith(s.phone_last4)]
        if len(hits) == 1 or (len(r.data) == 1 and not s.phone_last4):
            s.member = (hits or r.data)[0]
            s.member_id = s.member.get("member_id", "")


def _remember(s: Session, data: object) -> None:
    """조회 결과에서 소유 관계를 기억한다(이후 접수 대상 확인에 쓴다)."""
    for item in data if isinstance(data, list) else [data]:
        if not isinstance(item, dict):
            continue
        if "order_no" in item and "member_id" in item and "items" in item:
            s.order_owner[item["order_no"]] = item["member_id"]
            for ref in item.get("payment_refs") or []:
                s.approval_order.setdefault(ref, item["order_no"])
        if "shipment_id" in item and "order_no" in item:
            s.shipment_order[item["shipment_id"]] = item["order_no"]
        if "approval_no" in item and item.get("order_no"):
            s.approval_order[item["approval_no"]] = item["order_no"]
        if "subscription_id" in item and "member_id" in item:
            s.subscription_owner[item["subscription_id"]] = item["member_id"]


async def _get_order(s: Session, order_no: str) -> action_api.Result:
    """주문 조회(get_order). 같은 턴에 같은 주문을 이미 조회 중이거나 조회했으면 그 결과를 같이 쓴다."""
    task = s.order_gets.get(order_no)
    if task is None:
        task = s.order_gets[order_no] = asyncio.ensure_future(_call(s, ep("get_order").no, "GET", ep_path("get_order", order_no=order_no)))
    r = await asyncio.shield(task)  # 한 쪽이 시간 초과로 취소돼도 공유한 조회는 끝까지 간다
    if r.ok:
        _remember(s, r.data)
    return r


async def _order_owner(s: Session, order_no: str) -> str:
    """주문의 회원ID. 모르면 주문 조회(get_order)로 확인한다. 주문이 없으면 빈 값."""
    if order_no not in s.order_owner:
        await _get_order(s, order_no)
    return s.order_owner.get(order_no, "")


async def _owned(s: Session, e: Endpoint, args: dict) -> bool | None:
    """대상이 로그인 계정 소유인지. True/False, 확인할 수 없으면 None(세션 회원을 모를 때)."""
    if not e.owner or not s.member_id:
        return None if e.owner else True
    if e.owner in ("member", "referrer"):
        return args.get("member_id", args.get("referred_by")) == s.member_id
    if e.owner in ("order", "body_order"):
        owner = await _order_owner(s, args["order_no"])
        return None if not owner else owner == s.member_id
    if e.owner == "shipment":
        order_no = s.shipment_order.get(args["shipment_id"])
        if not order_no:
            r = await _call(s, ep("get_shipment").no, "GET", ep_path("get_shipment", shipment_id=args["shipment_id"]))
            if r.ok:
                _remember(s, r.data)
            order_no = s.shipment_order.get(args["shipment_id"])
        if not order_no:
            return None
        owner = await _order_owner(s, order_no)
        return None if not owner else owner == s.member_id
    if e.owner == "approval":
        order_no = s.approval_order.get(args["approval_no"])
        if not order_no:
            r = await _call(s, ep("get_payment").no, "GET", ep_path("get_payment", approval_no=args["approval_no"]))
            if r.ok:
                _remember(s, r.data)
            order_no = s.approval_order.get(args["approval_no"])
        if not order_no:
            return None
        owner = await _order_owner(s, order_no)
        return None if not owner else owner == s.member_id
    if e.owner == "subscription":
        owner = s.subscription_owner.get(args["subscription_id"])
        return None if not owner else owner == s.member_id
    return None


NOT_OWNER = {
    "access": "DENIED_NOT_OWNER",
    "note": "로그인한 인입 계정의 것이 아닙니다. 주소·이름·상태 등 어떤 내용도(일부라도) 알려 주지 말고, "
            f"본인 계정 주문만 안내할 수 있다고 설명한 뒤 create_escalation(lane={HANDOFF_LANE})으로 담당자 확인을 접수하세요.",
}


def _hide_others(s: Session, data: object) -> object:
    """검색 결과에서 다른 회원의 회원·주문 정보는 빼고, 상담 이력은 회원ID만 가린다(같은 로트 신고 건수는 필요하다)."""
    if not isinstance(data, list):
        return data
    out = []
    for item in data:
        other = isinstance(item, dict) and item.get("member_id") and item.get("member_id") != s.member_id
        if not other:
            out.append(item)
        elif "ticket_id" in item:
            out.append({**item, "member_id": "(다른 회원)"})
    if len(out) < len(data) and not any("ticket_id" in i for i in data if isinstance(i, dict)):
        out.append({"note": f"다른 회원 항목 {len(data) - len(out)}건은 개인정보라 숨김"})
    return out


def _dump(data: object) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= RESULT_MAX_CHARS else text[:RESULT_MAX_CHARS] + "...(생략)"


async def execute(s: Session, name: str, args: dict) -> tuple[str, bool]:
    """도구 하나를 실행하고 (LLM에 돌려줄 글, 오류 여부)를 돌려준다."""
    if name == "flag_crisis":
        s.crisis = True
        return "확인했습니다. 응대는 코드가 맡습니다.", False
    if s.crisis:
        return "지금은 조회·접수를 하지 않습니다.", True
    if name == "calc_date":
        args = args if isinstance(args, dict) else {}
        text = cs_dates.calc(str(args.get("mode", "")), str(args.get("base") or ""), args.get("days"), s.received_at, args)
        return text, text.startswith("오류")
    e = BY_NAME.get(name)
    if e is None:
        return "없는 도구입니다.", True
    if not s.tc_id:
        return "상담 TC ID가 없어 조회·접수를 할 수 없습니다. 지식으로만 답하세요.", True
    if not isinstance(args, dict):
        return "입력 형식 오류", True
    for p in path_params(e) + e.query:
        if not isinstance(args.get(p), str) or not SAFE_ID.match(args[p] if p not in ("name", "inflow_tag") else "x"):
            return f"{p} 값 형식이 올바르지 않습니다.", True
    if e.name == "request_coupon_issue" and str(args.get("promo_code", "")) not in ISSUABLE_PROMOS:
        return f"봇은 보상 쿠폰({'·'.join(sorted(ISSUABLE_PROMOS))})만 발급 요청할 수 있습니다. 임의 코드 발급·무제한 설정은 할 수 없습니다.", True
    if e.owner == "body_order" and not (isinstance(args.get("order_no"), str) and SAFE_ID.match(args["order_no"])):
        return "order_no 값 형식이 올바르지 않습니다.", True
    if "order_no" in args and not ORDER_NO.match(str(args["order_no"])):
        return "주문번호 형식이 맞지 않습니다. 이 번호로는 조회·접수하지 않았습니다. 고객에게 주문번호 전체를 다시 확인하세요.", True

    # 인입 계정이 주어졌는데 회원을 특정하지 못했으면(검색 실패·동명이인) 개인 정보 조회·접수를 하지 않는다.
    # 계정 정보가 아예 없을 때는 막지 않는다(대화로 계정이 전달되는 형식을 아직 모른다).
    if e.owner and s.member_name and not s.member_id:
        logger.info("tool %s %s denied=member_unknown", e.no, e.name)
        return "로그인 계정의 회원을 특정하지 못해 개인 주문·회원 정보는 조회할 수 없습니다. 본인 확인이 어렵다고 안내하고 필요하면 담당자 이관하세요.", True
    # 주문 조회는 결과의 회원ID로 바로 확인한다(미리 조회하면 같은 호출이 두 번 나간다).
    owned = True if e.name == "get_order" else await _owned(s, e, args)
    if owned is False:
        logger.info("tool %s %s denied=not_owner", e.no, e.name)
        return _dump(NOT_OWNER), True
    if e.method == "POST" and owned is None and e.owner:
        logger.info("tool %s %s denied=owner_unknown", e.no, e.name)
        return "대상이 로그인 계정 소유인지 확인되지 않아 접수하지 않았습니다. 먼저 해당 주문을 조회해 확인하세요.", True

    path = e.path
    for p in path_params(e):
        path = path.replace("{" + p + "}", args[p])
    params = {q: args[q] for q in e.query} or None
    body = None
    if e.method == "POST":
        body = {k: args[k] for k in e.body if k in args}
    if e.name == "get_order":
        r = await _get_order(s, args["order_no"])
    else:
        r = await _call(s, e.no, e.method, path, params=params, json_body=body)
    logger.info("tool %s %s status=%s", e.no, e.name, r.status)
    if r.ok and e.method == "POST":
        s.posted.append(e.no)
        s.order_gets.clear()
    if r.ok:
        _remember(s, r.data)
        # 단건 결과에 회원ID가 있으면(주문, 구독, 회원) 결과로 소유를 다시 확인한다. 소유를 미리 몰랐던 조회도 여기서 막힌다.
        if s.member_id and isinstance(r.data, dict) and r.data.get("member_id") not in (None, s.member_id):
            logger.info("tool %s %s denied=not_owner", e.no, e.name)
            return _dump(NOT_OWNER), True
        return _dump(_hide_others(s, r.data)), False
    if r.status == 404:
        return "결과 없음(404). 해당 대상이 없거나 기록이 없습니다.", False
    if r.status == 400:
        return "요청 값이 올바르지 않습니다(400).", True
    return f"조회·접수 실패(status={r.status} {r.error}). 지어내지 말고 잠시 후 다시 확인하겠다고 안내하세요.", True
