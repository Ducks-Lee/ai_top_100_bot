"""카카오톡 봇 API 이벤트 파싱과 메시지 콜백 호출.

명세: docs/kakao_bot_api.md
"""

import copy
import logging

import httpx

logger = logging.getLogger("kakao")

KAKAO_API_URL = "https://kapi.kakao.com"
OP_DISPATCH = 0

# simpleText 최대 길이. 카카오는 1,000자까지 받지만, 예선 심사는 말풍선당 500자를 넘으면 그 말풍선을 평가에서 뺀다(문제 풀이 가이드).
MAX_TEXT_LEN = 500
# 말풍선은 3개까지. 넘치면 카카오가 오류 없이 버리므로 보내기 전에 잘라 경고한다.
MAX_OUTPUTS = 3
# 카드 버튼은 세로 3개, 가로 2개까지
MAX_BUTTONS_VERTICAL = 3
MAX_BUTTONS_HORIZONTAL = 2
# listCard 항목은 단독 5개까지(캐러셀 안에서는 4개)
MAX_LIST_ITEMS = 5
COMPONENT_TYPES = {
    "simpleText", "simpleImage", "textCard", "basicCard", "commerceCard", "listCard", "itemCard", "carousel",
}


# 콜백 토큰이 이미 사용됨. 같은 봇이 두 곳에서 실행 중이면 양쪽이 같은 이벤트를 받아, 늦게 보낸 쪽이 이 오류를 받는다.
CALLBACK_CODE_ALREADY_USED = -881


class CallbackError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def parse_event(payload) -> tuple[str, dict] | None:
    """DISPATCH 이벤트면 (이벤트 종류, d)를, 아니면 None을 반환한다."""
    if not isinstance(payload, dict) or payload.get("op") != OP_DISPATCH:
        return None
    event_type, data = payload.get("t"), payload.get("d")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return None
    return event_type, data


# ---- 답장 만들기 ----------------------------------------------------------------
# agent.reply가 문자열 대신 skill_response(...)를 돌려주면 말풍선 여러 개(최대 3)와 카드를 보낼 수 있다.
#   return kakao.skill_response([
#       kakao.simple_text("요약"),
#       kakao.text_card("근거", "...", [kakao.web_link_button("원문", url)]),
#       kakao.suggestion_card(["더 자세히", "다른 예시", "처음으로"]),
#   ])
# 퀵리플라이(quickReplies)는 채널채팅에서 보이지 않으므로(10/28 실측) 추천 질문은 suggestion_card로 보낸다.
# 필드 명세와 다른 카드(basicCard, listCard, carousel 등): docs/kakao_bot_api.md "SkillResponse"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def simple_text(text: str) -> dict:
    """글자 말풍선. MAX_TEXT_LEN(500자)을 넘으면 자른다."""
    return {"simpleText": {"text": _truncate(text, MAX_TEXT_LEN)}}


def simple_image(image_url: str, alt_text: str = "") -> dict:
    """이미지 말풍선. 카카오가 주소에서 이미지를 가져가므로 공개된 주소여야 한다."""
    return {"simpleImage": {"imageUrl": image_url, "altText": _truncate(alt_text or "이미지", 50)}}


def text_card(title: str, description: str, buttons: list[dict], vertical: bool = True) -> dict:
    """제목, 설명, 버튼이 있는 카드. 제목은 50자, 제목과 설명을 합쳐 400자까지다.

    버튼은 1개 이상 필요하고, 세로면 3개·가로면 2개까지만 쓴다(넘치면 잘라 경고한다). 가로 2개면 버튼 글자는 8자까지다.
    """
    if not buttons:
        raise ValueError("textCard에는 버튼이 1개 이상 필요합니다")
    limit = MAX_BUTTONS_VERTICAL if vertical else MAX_BUTTONS_HORIZONTAL
    if len(buttons) > limit:
        logger.warning("카드 버튼 %d개 중 앞의 %d개만 보냅니다", len(buttons), limit)
        buttons = buttons[:limit]
    if not vertical and len(buttons) == 2:
        buttons = [{**b, "label": _truncate(b.get("label", ""), 8)} for b in buttons]
    title = _truncate(title, 50)
    card = {"title": title, "description": _truncate(description, 400 - len(title)), "buttons": buttons}
    card["buttonLayout"] = "vertical" if vertical else "horizontal"
    return {"textCard": card}


def list_item(title: str, description: str = "", message_text: str | None = None) -> dict:
    """list_card의 한 줄. message_text를 주면 그 줄을 눌렀을 때 그 글자가 사용자 메시지로 들어간다."""
    item = {"title": title}
    if description:
        item["description"] = description
    if message_text:
        item["action"] = "message"
        item["messageText"] = message_text
    return item


def list_card(title: str, items: list[dict], buttons: list[dict]) -> dict:
    """제목(header) 아래 항목 목록과 버튼이 있는 카드. 항목은 5개까지(넘치면 잘라 경고한다), 버튼은 1~3개.

    항목은 list_item(...)으로 만든다. 항목별 글자 수 제한은 문서에 없다.
    """
    if not items:
        raise ValueError("listCard에는 항목이 1개 이상 필요합니다")
    if not buttons:
        raise ValueError("listCard에는 버튼이 1개 이상 필요합니다")
    if len(items) > MAX_LIST_ITEMS:
        logger.warning("목록 항목 %d개 중 앞의 %d개만 보냅니다", len(items), MAX_LIST_ITEMS)
        items = items[:MAX_LIST_ITEMS]
    if len(buttons) > MAX_BUTTONS_VERTICAL:
        logger.warning("카드 버튼 %d개 중 앞의 %d개만 보냅니다", len(buttons), MAX_BUTTONS_VERTICAL)
        buttons = buttons[:MAX_BUTTONS_VERTICAL]
    return {"listCard": {"header": {"title": title}, "items": items, "buttons": buttons, "buttonLayout": "vertical"}}


def message_button(label: str, message_text: str | None = None) -> dict:
    """누르면 message_text(없으면 label)를 사용자가 보낸 것처럼 봇에게 보낸다. label은 14자까지."""
    return {"action": "message", "label": _truncate(label, 14), "messageText": message_text or label}


def web_link_button(label: str, url: str) -> dict:
    """누르면 url을 연다. label은 14자까지."""
    return {"action": "webLink", "label": _truncate(label, 14), "webLinkUrl": url}


def suggestion_card(
    questions: list[str], title: str = "이렇게 물어보세요", description: str = "버튼을 누르면 바로 물어볼 수 있어요."
) -> dict:
    """추천 질문 버튼(최대 3개) 카드. 누르면 그 질문이 사용자 메시지로 들어온다(10/28 실측).

    버튼 글자는 14자에서 잘려 보이지만, 보내지는 메시지는 질문 전체다. 말풍선 3개 중 하나를 차지한다.
    """
    return text_card(title, description, [message_button(q) for q in questions])


def skill_response(outputs: list[dict]) -> dict:
    """말풍선(1~3개)으로 SkillResponse를 만든다."""
    return limit_response({"version": "2.0", "template": {"outputs": outputs}})


def text_response(text: str) -> dict:
    """simpleText 말풍선 하나짜리 SkillResponse를 만든다."""
    return skill_response([simple_text(text)])


def plain_text(response: dict) -> str:
    """SkillResponse에서 글자만 모은다(simpleText, 카드 제목·설명, 목록 항목). 카드가 거절됐을 때 글자로 다시 보내는 데 쓴다."""
    parts = []

    def collect(kind, body):
        if not isinstance(body, dict):
            return
        if kind == "simpleText":
            parts.append(body.get("text"))
        elif kind == "carousel":
            for item in body.get("items") or []:
                collect(body.get("type"), item)
        elif kind == "listCard":
            header = body.get("header")
            parts.append(header.get("title") if isinstance(header, dict) else None)
            for item in body.get("items") or []:
                if isinstance(item, dict):
                    parts.append(" - ".join(t for t in (item.get("title"), item.get("description")) if isinstance(t, str) and t))
        else:
            parts.extend((body.get("title"), body.get("description")))

    template = response.get("template") if isinstance(response, dict) else None
    for output in (template or {}).get("outputs") or []:
        if isinstance(output, dict):
            for kind, body in output.items():
                collect(kind, body)
    return "\n\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


def limit_response(response: dict) -> dict:
    """카카오가 오류 없이 버리는 부분(말풍선 3개 초과, 1,000자 초과, 퀵리플라이)을 미리 빼고 경고한다.

    형식이 틀리면(말풍선이 없음, 말풍선 종류를 모름, 빈 글자) ValueError를 낸다. 카카오가 거절하면 사용자에게
    아무 답도 가지 않으므로, 보내기 전에 걸러 bot.py가 오류 안내 문구로 답하게 한다. 원본은 고치지 않고 사본을 돌려준다.
    카드별 필수 칸(basicCard의 thumbnail 등)은 보지 않는다. 그런 거절은 bot.py가 글자 답장으로 다시 보내 대비한다.
    """
    template = response.get("template") if isinstance(response, dict) else None
    outputs = template.get("outputs") if isinstance(template, dict) else None
    if not isinstance(outputs, list) or not outputs:
        raise ValueError("SkillResponse에 말풍선(template.outputs)이 없습니다")
    for output in outputs:
        if not isinstance(output, dict) or len(output) != 1:
            raise ValueError("말풍선 하나에는 종류 하나만 넣습니다. 예: {\"simpleText\": {...}}")
        (kind, body), = output.items()
        if kind not in COMPONENT_TYPES or not isinstance(body, dict) or not body:
            raise ValueError(f"알 수 없거나 비어 있는 말풍선입니다: {kind}")
        if kind == "simpleText" and not (isinstance(body.get("text"), str) and body["text"].strip()):
            raise ValueError("simpleText의 text가 비어 있습니다")
    response = copy.deepcopy(response)
    template = response["template"]
    response.setdefault("version", "2.0")
    if len(template["outputs"]) > MAX_OUTPUTS:
        logger.warning("말풍선 %d개 중 앞의 %d개만 보냅니다", len(template["outputs"]), MAX_OUTPUTS)
        del template["outputs"][MAX_OUTPUTS:]
    for output in template["outputs"]:
        simple = output.get("simpleText")
        if isinstance(simple, dict) and isinstance(simple.get("text"), str):
            simple["text"] = _truncate(simple["text"], MAX_TEXT_LEN)
    if template.pop("quickReplies", None):
        logger.warning("퀵리플라이는 채널채팅에서 보이지 않아 뺍니다(10/28 실측). 추천 질문은 kakao.suggestion_card로 보낸다")
    return response


async def send_callback(
    client: httpx.AsyncClient, bot_token: str, callback_token: str, skill_response: dict
) -> dict:
    """메시지 콜백 API로 답장한다. 콜백 토큰은 받은 뒤 5분 안에 써야 한다."""
    resp = await client.post(
        f"{KAKAO_API_URL}/v1/bot/callback",
        json={"skillResponse": skill_response},
        headers={
            "Authorization": f"KakaoAK {bot_token}",
            "X-Bot-Callback-Token": callback_token,
        },
    )
    if resp.status_code != 200:
        try:
            code = resp.json().get("code")
        except (ValueError, AttributeError):
            code = None
        raise CallbackError(f"{resp.status_code} {resp.text[:500]}", code)
    return resp.json()
