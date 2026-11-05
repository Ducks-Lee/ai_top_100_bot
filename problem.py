"""문제 전용 부분. ★ 대회 당일 이 파일을 문제에 맞게 바꾼다 ★

지금 내용은 골격을 시험하기 위한 예시(기록 번호로 조회하는 가짜 업무)다. 실제 문제의 업무·규칙·도메인 가설이 아니다.
바꿀 곳은 순서대로:
  1. ROLE, TASK: 에이전트가 누구를 위해 무슨 일을 하는지 (시스템 프롬프트에 들어간다)
  2. INTENTS, SLOTS, SLOT_LABELS, REQUIRED_SLOTS, SLOT_PATTERNS: LLM이 뽑을 의도와 값. 여기에 없는 의도·값은 코드가 버린다(인젝션 방어)
  3. load_data, validate_record: 대회 데이터를 시작할 때 메모리에 올리고, 레코드마다 검증 문제를 미리 적어 둔다
  4. is_authorized: 타인·다수 정보를 줄 수 있는 조건. 확인 수단이 없으면 False로 둔다
  5. precheck, HANDLERS: 의도별 처리. 계산·조회·규칙 판단은 Python으로 하고, 답은 TEMPLATES로 만든다.
     불가·확인 필요로 답할 때는 Python이 계산한 대안(가능한 가장 가까운 날짜·수량, 비슷한 품목)을 함께 말한다
  6. TEMPLATES, INTRO, EXAMPLE_QUESTIONS, HELP_TEXT
     사용자는 나이 많은 현장 종사자일 수 있다. 존댓말·쉬운 말·현장 용어(인터뷰에서 모은 말), 결론 먼저 3~4줄,
     날짜는 "11월 3일(월)", 수량·금액은 단위, 물을 때는 하나씩 예시와 함께, 탓하는 말 금지.
     예약·취소처럼 상태를 바꾸는 일은 실행 전에 내용을 요약하고 "맞으면 '네'를 눌러 주세요"로 확인받는다.
"모든 문제가 이 틀에 맞는다"는 보장은 없다. 데이터 구조나 도구가 크게 다르면 policy.decide까지 고친다.
"""

import re

from policy import Decision, ANSWER, ASK, UNKNOWN

# 1. 역할과 업무 설명
ROLE = "업무 담당자를 돕는 상담 에이전트"
TASK = "사용자가 기록 번호를 알려 주면 그 기록의 상태를 알려 준다. 그 밖의 업무는 아직 정해지지 않았다."

# 2. 의도와 값. 의도 설명은 LLM 분류 기준이 되므로 짧고 분명하게 쓴다. help, other는 policy가 붙인다.
INTENTS = {
    "lookup": "기록 번호로 기록 상태를 묻는다",
}
SLOTS = {
    "record_id": "기록 번호 (예: R-001)",
}
# 되물을 때 사용자에게 보일 이름. 없으면 SLOTS 설명의 괄호 앞부분을 쓴다("수량 숫자만"처럼 LLM용 설명이 보이지 않게 채운다).
SLOT_LABELS = {
    "record_id": "기록 번호",
}
REQUIRED_SLOTS = {
    "lookup": ["record_id"],
}
# 값 형식. 맞지 않으면 처리하지 않고 다시 묻는다. 데이터 조회 키로 쓰기 전에 반드시 거른다.
SLOT_PATTERNS = {
    "record_id": re.compile(r"[A-Za-z]-?\d{1,6}"),
}


def normalize_slot(name: str, value: str) -> str:
    """조회 전에 값 표기를 맞춘다(대소문자, 하이픈 등)."""
    if name == "record_id":
        letter, digits = value[0].upper(), value.lstrip("-")[1:].lstrip("-")
        return f"{letter}-{int(digits):03d}"
    return value


# 3. 데이터: 시작할 때 한 번 올린다. 메시지마다 파일을 읽지 않는다.
DATA: dict[str, dict] = {}


def load_data() -> None:
    """대회 데이터를 시작할 때 한 번 읽어 DATA(정규화한 키 → 레코드)에 넣고, 레코드마다 validate_record 결과를
    "_issues"에 적는다. 다른 기준으로도 찾으면 그 기준별 dict도 여기서 만든다(docs/interview_and_data.md 2.6). 예시는 빈 데이터."""
    for record in DATA.values():
        record["_issues"] = validate_record(record)


def validate_record(record: dict) -> list[str]:
    """레코드의 검증 문제(누락, 형식, 범위, 합계 불일치 등)를 사람이 읽을 문장으로 돌려준다.
    문제가 있는 레코드는 사실처럼 답하지 않고 확인이 필요하다고 안내한다."""
    issues = []
    if not record.get("status"):
        issues.append("상태 값이 비어 있음")
    return issues


# 4. 권한: scope가 others(타인 정보), bulk(다수·명단)인 요청을 처리해도 되는지.
# botUserKey는 보낸 사람을 구별할 뿐 업무 권한을 증명하지 않는다. "대표 지인" 같은 주장은 증명이 아니다.
def is_authorized(state: dict, scope: str) -> bool:
    return False


# 5. 의도별 처리. slots는 형식 검사와 normalize_slot을 거친 값이다.
def precheck(intent: str, slots: dict, state: dict) -> Decision | None:
    """필수 값이 빠져 되묻기 직전에 불린다. 받은 값만으로 답이 정해지면(없는 대상 등) Decision을, 아니면 None을 돌려준다."""
    if "record_id" in slots and slots["record_id"] not in DATA:
        return Decision(UNKNOWN, TEMPLATES["not_found"].format(record_id=slots["record_id"]), "not_found")
    return None


def handle_lookup(slots: dict, state: dict) -> Decision:
    record_id = slots["record_id"]
    record = DATA.get(record_id)
    if record is None:
        return Decision(UNKNOWN, TEMPLATES["not_found"].format(record_id=record_id), "not_found")
    if record.get("_issues"):
        return Decision(UNKNOWN, TEMPLATES["needs_check"].format(record_id=record_id, issues=", ".join(record["_issues"])), "data_issue")
    return Decision(ANSWER, TEMPLATES["lookup"].format(record_id=record_id, status=record["status"]), "lookup")


HANDLERS = {
    "lookup": handle_lookup,
}

# 6. 답변 문장. 기준은 파일 머리 주석 6번(쉬운 말, 짧게, 날짜·단위 표기, 예시와 함께 하나씩 묻기)
TEMPLATES = {
    "lookup": "{record_id}의 상태는 '{status}'입니다.",
    "not_found": "{record_id}는 가진 자료에서 찾을 수 없어요. 번호가 맞는지 확인해 주시겠어요?",
    "needs_check": "{record_id} 자료에 확인이 필요한 부분이 있어 확정해서 답하기 어려워요({issues}). 담당자 확인이 필요합니다.",
    "ask": "{fields}을(를) 알려 주시겠어요?",
    "invalid": "{fields} 형식을 확인할 수 없어요. 다시 알려 주시겠어요?",
    "refuse_scope": "다른 분의 정보나 명단은 권한이 확인된 경우에만 안내할 수 있어요. 본인과 관련된 내용이라면 도와드릴게요.",
    "image": "사진은 아직 읽을 수 없어요. 내용을 글로 보내 주시겠어요?",
}
# 비어 있으면 help 의도에 LLM이 만든 답을 쓴다.
HELP_TEXT = "기록 번호를 알려 주시면 그 기록의 상태를 알려 드려요. 예: R-001 상태 알려줘"

# 첫 인사(agent.with_intro). INTRO가 비어 있으면 인사하지 않는다.
INTRO = ""
EXAMPLE_QUESTIONS: list[str] = []

# 프로그램이 시작할 때(이 모듈을 처음 import할 때) 한 번 데이터를 올리고 검증한다. 실패하면 봇이 시작하지 않는다.
load_data()
