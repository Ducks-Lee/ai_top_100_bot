"""위기신호(자·타해) 대응. 프롬프트가 아니라 코드로 강제한다(cs_agent가 쓴다).

근거: 받은 위기신호 대응 가이드(data/ 아래, git 제외)를 요지로 옮긴 것이다.
- 감지: 명백한 표현은 키워드로 잡고(detect), 그 밖의 신호(이별 인사조, 임박, 애매한 경우)는 LLM이 flag_crisis 도구로 알린다.
  둘 중 하나라도 걸리면 그 상담은 위기 상태다(판단이 안 서면 신호 있음, fail-closed).
  몸 이상·섭취 후 이상(배탈, 복통)은 위기신호가 아니다(판매·환불 규정을 따른다). "배고파 죽겠다" 같은 과장 표현은 키워드에 넣지 않는다.
- 위기 상태: 조회·접수 도구는 막고(cs_tools.execute), 이관은 코드가 한 번만 접수한다(설정의 담당자 확인 구분과 안전 태그).
- 첫 답은 고정 틀(공감 → 109 안내 한 번 → 주문 건 부담 덜기 → 이관 접수번호). 이후 답은 LLM이 공감·연결 중심으로 쓰되
  금지 표현이 섞이면 고정 문장으로 바꾼다.
"""

from __future__ import annotations

import re

import cs_config

# 명백한 자·타해, 이별 인사조 표현. 띄어쓰기 변형을 허용한다.
_SIGNALS = [
    # "자살"은 단어 앞에서만("감자 살 수 있나요", "감자살게요" 오탐 방지). "죽을 거 같아" 같은 과장은 LLM의 flag_crisis 판단에 맡긴다
    r"(?<![가-힣])자살", r"죽\s*고\s*싶", r"죽어\s*버리", r"죽으려", r"목숨을?\s*끊", r"극단\s*적", r"스스로\s*목숨",
    r"사라지고\s*싶", r"다\s*끝내고\s*싶", r"끝내\s*버리고\s*싶", r"살고\s*싶지\s*않", r"살\s*이유가?\s*없",
    r"해치고\s*싶", r"죽이고\s*싶", r"유서", r"뛰어\s*내리",
    r"마지막으로\s*먹어\s*보고\s*싶", r"이제\s*주문할\s*일\s*(?:이\s*)?없을", r"그동안\s*고마웠", r"다\s*정리해\s*(?:두었|뒀)",
]
SIGNAL = re.compile("|".join(_SIGNALS))


def detect(text: str) -> bool:
    return bool(SIGNAL.search(text or ""))


ESCALATION_REASON = (cs_config.section("escalation").get("safety_tag", "[SAFETY]") + " 우선순위 위기. 상담 중 자·타해 관련 위기신호 발화가 있어 상품 응대를 멈추고 공감·자살예방상담 109 안내를 함. "
                     "진행 중이던 주문 건은 상담 창구가 처리 책임을 맡기로 안내함. 담당자의 즉시 확인과 후속 연락 판단 요청.")

FIRST_REPLY = ("그런 마음이 드실 만큼 많이 힘드셨겠습니다. 지금 이야기 나눌 곳이 필요하시면 자살예방상담 109에서 24시간 언제든 들어 드립니다. "
               "주문 건은 제가 책임지고 처리해 두겠습니다.")
SAFE_REPLY = "말씀해 주셔서 고맙습니다. 저는 여기 있으니 편하실 때 언제든 이야기해 주세요. 주문 건은 제가 챙겨 두겠습니다."

# 위기 상태에서 LLM 답에 들어가면 안 되는 표현. 걸리면 SAFE_REPLY로 바꾼다.
FORBIDDEN = re.compile("|".join([
    r"힘\s*내", r"좋은\s*생각", r"시간이\s*약", r"다들\s*그렇게", r"왜\s*그런\s*생각", r"무슨\s*일\s*있", r"언제부터",
    r"우울", r"위험군", r"심각", r"진단", r"불편을\s*드려", r"환불해\s*드리", r"위기\s*상황", r"분류", r"감별", r"지침",
    r"전화하실\s*수", r"잊어", r"확인하지\s*못한\s*번호", r"번호\s*저장", r"연락처를?\s*(?:드리|알려)", r"방법", r"수단",
]))

CRISIS_RULES = """[지금 상담 상태: 고객의 마음이 많이 힘든 상황]
- 상품 응대(환불·배송·쿠폰 안내)를 이어 말하지 않는다. 공감과 곁에 있다는 말 중심으로 1~3문장만 쓴다. 결정을 요구하지 않는다.
- 금지: 격려·훈계(힘내세요 등), 이유·시기 캐묻기, 임상·진단 표현, 불편 사과·환불 같은 상품 문제로 축소, 고객 말 되받아 인용,
  방법·수단 언급, 감별·분류·지침 언급, 연락 확인 압박, 개인 연락처 교환, 대화 종료 권유.
- 상담 자원(자살예방상담 109) 안내는 이미 했다. 고객이 다시 물을 때만 다시 알려 준다.
- 주문 건은 상담 창구가 책임지고 처리해 둔다고만 말한다."""


def first_reply(escalation_id: str) -> str:
    tail = f" 담당자도 함께 살피도록 접수해 두었습니다(접수번호 {escalation_id})." if escalation_id else ""
    return FIRST_REPLY + tail


def asks_resource_again(text: str) -> bool:
    return bool(re.search(r"109|번호|상담|연락처|어디(?:에|로)?\s*(?:연락|전화|얘기|이야기)", text or ""))


def safe_followup(answer: str, customer_text: str) -> str:
    """위기 상태의 이후 답 검사. 금지 표현이 있거나 고객이 묻지 않았는데 109를 반복하면 고정 문장으로 바꾼다."""
    if not answer or FORBIDDEN.search(answer):
        return SAFE_REPLY
    if "109" in answer and not asks_resource_again(customer_text):
        return SAFE_REPLY
    return answer
