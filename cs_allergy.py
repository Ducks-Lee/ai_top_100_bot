"""알레르기 보증 표현 차단. 프롬프트 규칙과 함께 코드로 한 번 더 막는다(cs_agent가 쓴다).

라벨의 알레르기 문구는 같은 시설에서 무엇을 함께 다루는지 알리는 것일 뿐, 전용 설비나 교차오염이 없다는 보증이 아니다.
시설별로 함께 다루는 품목은 설정(cs_config의 allergy)에서 읽는다. 공개 예시 값은 지어낸 것이다.
"""

from __future__ import annotations

import re

import cs_config

# 알레르기 질문: 알레르기 낱말이 있거나, 알레르기 유발 품목과 섭취·함유 질문이 함께 있을 때("복숭아 반품해 주세요"는 아님)
ALLERGY = re.compile(r"알레르기|알러지|알레르|과민\s*반응|교차\s*오염")
ALLERGEN = re.compile(r"견과|땅콩|호두|아몬드|복숭아|새우|갑각|게살|오징어|고등어|조개|우유|대두|밀가루")
EATING = re.compile(r"먹어도|먹을\s*수|드셔도|먹여도|섭취|들어\s*(?:있|갔)|함유|성분|섞여|묻어")
GUARANTEE = re.compile(r"안전합니다|안전해요|들어\s*있지\s*않|교차\s*오염(?:이)?\s*(?:은|도)?\s*없|문제\s*없|괜찮습니다")
# 시설은 알레르겐이 아니라 상담 대상 상품으로 정한다("새우 알레르기인데 수박 먹어도 되나요"는 농산 시설)
SEA_PRODUCT = re.compile(r"갈치|갯마을|수산물|생선")
FARM_PRODUCT = re.compile(r"교동|농산물|과일|채소|사과|감귤|귤|수박|포도|딸기|참외|자두|토마토|감자|고구마|옥수수|쌀")

_ALLERGY = cs_config.section("allergy")
FARM_ITEMS, SEA_ITEMS = _ALLERGY.get("farm_items", ""), _ALLERGY.get("sea_items", "")


def fixed_reply(customer_text: str) -> str:
    text = customer_text or ""
    sea, farm = bool(SEA_PRODUCT.search(text)), bool(FARM_PRODUCT.search(text))
    items = SEA_ITEMS if sea and not farm else FARM_ITEMS if farm and not sea else ""  # 설정에 없으면 품목을 단정하지 않는다
    what = f"{items}를" if items else "다른 알레르기 유발 품목을"  # 상품을 특정하지 못하면 품목을 단정하지 않는다
    return (f"같은 시설에서 {what} 함께 다루고 있어 교차오염 가능성을 배제할 수 없습니다. "
            "드실지는 고객님 상태에 맞춰 판단해 주시고, 필요하면 의사와 상의해 주세요.")


def guard(answer: str, customer_text: str) -> str:
    """고객이 알레르기 관련으로 물었는데 답에 보증 표현이 있으면 고정 문구로 바꾼다."""
    text = customer_text or ""
    asked = ALLERGY.search(text) or (ALLERGEN.search(text) and EATING.search(text))
    if asked and GUARANTEE.search(answer or ""):
        return fixed_reply(customer_text)
    return answer
