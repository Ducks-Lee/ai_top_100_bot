"""골격 시험용 예시 문제(기록 번호로 조회하는 가짜 업무). tests/test_agent.py가 agent.problem 자리에 넣어 쓴다.

대회 당일 problem.py를 바꿔도 골격 시험이 깨지지 않도록 예시를 따로 둔다. 당일에도 이 파일은 고치지 않는다.
새 문제의 시나리오는 tests/scenarios.json에 쓰고 tests/test_scenarios.py가 problem.py로 돌린다.
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
    """대회 데이터를 읽어 DATA에 넣고, 레코드마다 validate_record 결과를 "_issues"에 적는다. 예시는 빈 데이터."""
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

# 6. 답변 문장
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
