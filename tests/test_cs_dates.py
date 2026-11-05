"""cs_dates.py(영업일·기한 계산, 금액 산식)를 공개용 예시 설정(cs_config.example.json)으로 시험한다.

예시 달력: 2030년, 공휴일 03-01(금)·05-01(수)·05-06(월)·06-06(목), 출고 요일 화·목, 회신 기한 영업일 2일, 역일 기한 10일, 컷오프 영업일 2일.
예시 정책: 부분환불 구간 25%·75%, 중간 구간 40%, 100원 단위 올림, 최소 500원, 재량 한도 30,000원, 중량 허용치 4% + 자연감량 2%. 모두 지어낸 값이다.
"""

from datetime import date

import pytest

import cs_dates as d


def D(s):
    return date.fromisoformat(s)


@pytest.mark.parametrize("base,expected", [
    ("2030-03-05", "2030-03-07"),  # 화요일 접수, 사이 휴일 없음
    ("2030-02-28", "2030-03-05"),  # 03-01 공휴일과 주말 건너뜀
    ("2030-04-30", "2030-05-03"),  # 05-01 공휴일 건너뜀
])
def test_business_deadline_examples(base, expected):
    assert d.business_deadline(D(base), 2) == D(expected)


def test_business_deadline_from_holiday_moves_base():
    # 토요일(03-02) 접수는 월요일(03-04)이 기준일 → 03-06(수)
    assert d.business_deadline(D("2030-03-02"), 2) == D("2030-03-06")


@pytest.mark.parametrize("payment,expected", [
    ("2030-03-05", "2030-02-28"),  # 03-04(월)이 1, 03-01 공휴일·주말 건너뛰고 02-28(목)이 2
    ("2030-05-07", "2030-05-02"),  # 05-06 공휴일·주말 건너뜀
])
def test_subscription_cutoff_examples(payment, expected):
    assert d.cutoff(D(payment), 2) == D(expected)


@pytest.mark.parametrize("after,expected", [
    ("2030-03-05", "2030-03-07"),  # 화 → 목
    ("2030-06-04", "2030-06-11"),  # 06-06(목) 공휴일 회차 건너뜀
    ("2030-04-30", "2030-05-02"),
])
def test_next_ship_day_skips_holiday_rounds(after, expected):
    assert d.next_ship_day(D(after)) == D(expected)


def test_calendar_deadline_flows_through_holidays():
    assert d.calendar_deadline(D("2030-02-25"), 10) == D("2030-03-07")
    text = d.calc("calendar_deadline", "2030-03-01", None, "2030-03-05(화) 10:22")
    assert "3월 11일(월) 24:00까지" in text and "4일 지난" in text and "기한 안" in text


def test_calc_formats_and_defaults():
    assert d.fmt(D("2030-03-07")) == "3월 7일(목)"
    assert d.calc("business_deadline", received_at="2030-03-05(화) 15:16") == "3월 5일(화) 기준 영업일 2일 기한: 3월 7일(목) 24:00까지"
    assert d.today_from("") == d.DEFAULT_TODAY
    assert d.calc("day_info", "2030-06-06") == "6월 6일(목): 휴무일"
    assert d.calc("nope").startswith("오류") and d.calc("business_deadline", "어제").startswith("오류")


def test_weight_shortfall_rules():
    ok = d.weight_shortfall(1000, 950)  # 5% → 정상(6% 이내)
    assert "정상 범위" in ok and "5.0%" in ok
    bad = d.weight_shortfall(1000, 900, paid=30000)  # 10% → 실제 미달, 부족분 전체 100g
    assert "실제 미달" in bad and "100g" in bad and "3,000원" in bad and "약속 금지" in bad
    no_record = d.weight_shortfall(1000, None, received_g=850)  # 출고 기록 없으면 표기 중량 기준 → 정상, 계량치는 직접 비교 안 함
    assert "표기 중량" in no_record and "정상 범위" in no_record and "재촬영" not in no_record
    assert "재촬영" in d.weight_shortfall(1000, 960, received_g=850)  # 출고 대비 자연감량으로 설명 안 됨
    assert d.calc("weight_shortfall", weights={"labeled_g": 1000, "shipped_g": 900}).startswith("기준 중량 900g")
    assert d.calc("weight_shortfall", weights={}).startswith("오류")


@pytest.mark.parametrize("paid,defects,units,rate,amount,choices", [
    (40000, 8, 40, 20.0, 8000, ["부분환불"]),
    (40000, 12, 40, 30.0, 16000, ["40% 부분환불", "전량 재배송"]),
    (30000, 31, 40, 77.5, 30000, ["전액환불", "전량 재배송"]),
    (55800, 1, 2, 50.0, 22400, ["40% 부분환불", "전량 재배송"]),  # 2개 세트, 22,320원 → 100원 단위 올림
])
def test_partial_refund_examples(paid, defects, units, rate, amount, choices):
    r = d.partial_refund(paid, defects, units)
    assert (r["rate"], r["amount"], r["choices"]) == (rate, amount, choices)


@pytest.mark.parametrize("defects,units,zone", [
    (249, 1000, "부분 하자"), (250, 1000, "상당 부분 하자"), (750, 1000, "상당 부분 하자"), (751, 1000, "사실상 전부 하자"),
    (2495, 10000, "상당 부분 하자"),  # 24.95 → 반올림 25.0으로 판정
])
def test_partial_refund_boundaries(defects, units, zone):
    assert d.partial_refund(100000, defects, units)["zone"] == zone


def test_partial_refund_rounding_minimum_and_limit():
    assert d.partial_refund(10001, 1, 7)["amount"] == 1500  # 1428.7 → 100원 단위 올림
    assert d.partial_refund(2000, 1, 20)["amount"] == 500  # 100원 → 최소 500원
    assert d.partial_refund(80000, 30, 100)["needs_escalation"] is True  # 32,000원 > 재량 30,000원
    assert d.partial_refund(40000, 10, 100, prior_refunded=27000)["needs_escalation"] is True  # 합산 31,000원
    full = d.partial_refund(90000, 8, 10)
    assert full["choices"] == ["전액환불", "전량 재배송"] and full["needs_escalation"] is False
    text = d.calc("partial_refund", weights={"paid": 40000, "defects": 12, "units": 40})
    assert "30.0%" in text and "16,000원(40%)" in text and "전량 재배송" in text
    assert d.calc("partial_refund", weights={"paid": 24000, "defects": 1, "units": 1}).startswith("오류")
