import cs_allergy as a


def test_guarantee_is_replaced_with_facility_notice():
    out = a.guard("네, 사과에는 견과류가 들어 있지 않아 안전합니다.", "견과류 알레르기가 있는데 사과 먹어도 되나요?")
    assert "견과류" in out and "배제할 수 없습니다" in out and "안전합니다" not in out
    sea = a.guard("교차오염 없습니다.", "갈치 사려는데 새우 알레르기 있어요")
    assert "갑각류" in sea


def test_non_allergy_or_non_guarantee_is_kept():
    assert a.guard("환불은 괜찮습니다.", "환불 언제 되나요?") == "환불은 괜찮습니다."
    ok = "라벨에 같은 시설에서 견과류를 함께 다룬다고 적혀 있습니다. 드실지는 직접 판단해 주세요."
    assert a.guard(ok, "땅콩 알레르기 있는데요") == ok


def test_facility_follows_product_not_allergen():
    assert "견과류" in a.guard("안전합니다.", "새우 알레르기가 있는데 수박 먹어도 되나요?")
    assert "다른 알레르기 유발 품목" in a.guard("안전합니다.", "땅콩 알레르기 있는데 괜찮나요?")


def test_return_request_with_allergen_word_is_kept():
    out = "반품하셔도 괜찮습니다. 반품 접수번호는 R-1입니다."
    assert a.guard(out, "복숭아 반품해 주세요") == out
