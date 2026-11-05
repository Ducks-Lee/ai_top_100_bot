"""날짜·기한 계산(영업일, 역일 기한, 정기배송 컷오프, 출고일)과 금액 산식. LLM이 날짜·금액을 직접 세지 않게 cs_tools의 calc_date 도구가 쓴다.

달력(공휴일, 출고 요일, 기준일)과 정책 수치(기한 일수, 부분환불 구간, 재량 한도, 중량 허용치)는 cs_config에서 읽는다.
공개 예시 설정(cs_config.example.json)의 값은 지어낸 것이고, 실제 운영 값은 data/cs_config.json(git 제외)에 둔다.
- 영업일: 월~금이면서 공휴일이 아닌 날.
- 영업일 N일 기한: 기준일은 세지 않고 다음 영업일이 1일차, N일차 24:00. 기준일이 휴무일이면 다음 영업일을 기준일로 바꾼다.
- 정기배송 컷오프: 결제일 직전 영업일을 1로 세어 거꾸로 N번째 영업일의 24:00.
- 역일 기한: 공휴일에도 흐르고 늘어나지 않는다. 기준일 + N일의 24:00.
- 출고: 설정한 출고 요일 가운데 영업일만. 공휴일과 겹친 회차는 다음 날로 미루지 않고 건너뛴다.
- "오늘"은 실제 날짜가 아니라 상담 앞머리의 상담 인입 일시다. 없으면 설정의 기준일 DEFAULT_TODAY를 쓴다.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import cs_config

_CAL = cs_config.section("calendar")
_REFUND = cs_config.section("refund")
_WEIGHT = cs_config.section("weight")

# 값은 모두 설정에서 읽는다(기본값을 코드에 두지 않는다. 빠지면 시작할 때 KeyError로 드러난다).
HOLIDAYS = {date.fromisoformat(d) for d in _CAL["holidays"]}
SHIP_WEEKDAYS = set(_CAL["ship_weekdays"])  # 0=월
DEFAULT_TODAY = date.fromisoformat(_CAL["default_today"])  # 상담 인입 일시가 없을 때의 기준일
REPLY_DAYS = int(_CAL["reply_business_days"])  # 이관 회신 기한(영업일)
CALENDAR_DAYS = int(_CAL["calendar_deadline_days"])  # 역일 기한 기본 일수
CUTOFF_DAYS = int(_CAL["cutoff_business_days"])  # 정기배송 컷오프(영업일)
WEEKDAY_KO = "월화수목금토일"
_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def is_ship_day(d: date) -> bool:
    return d.weekday() in SHIP_WEEKDAYS and is_business_day(d)


def fmt(d: date) -> str:
    return f"{d.month}월 {d.day}일({WEEKDAY_KO[d.weekday()]})"


def parse(text: str) -> date | None:
    m = _DATE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def today_from(received_at: str) -> date:
    return parse(received_at) or DEFAULT_TODAY


def business_deadline(base: date, days: int) -> date:
    """영업일 N일 기한. 기준일 불산입, 다음 영업일이 1일차. 휴무일 기준일은 다음 영업일로 바꾼다."""
    while not is_business_day(base):
        base += timedelta(days=1)
    d, count = base, 0
    while count < days:
        d += timedelta(days=1)
        if is_business_day(d):
            count += 1
    return d


def cutoff(payment: date, days: int) -> date:
    """정기배송 컷오프. 결제일 직전 영업일을 1로 세어 거꾸로 N번째 영업일."""
    d, count = payment, 0
    while count < days:
        d -= timedelta(days=1)
        if is_business_day(d):
            count += 1
    return d


def calendar_deadline(base: date, days: int) -> date:
    return base + timedelta(days=days)


def next_ship_day(after: date) -> date:
    """기준일 다음 날부터 첫 출고일(기준일 당일은 넣지 않는다)."""
    d = after + timedelta(days=1)
    while not is_ship_day(d):
        d += timedelta(days=1)
    return d


MODES = {
    "business_deadline": f"영업일 N일 기한(이관 회신 기한 등). base 기본 오늘, days 기본 {REPLY_DAYS}",
    "calendar_deadline": f"역일 N일 기한(수령일·구매일 등이 base). days 기본 {CALENDAR_DAYS}. 오늘 기준 경과 일수와 기한 안 여부도 준다",
    "subscription_cutoff": f"정기배송 컷오프(base=회차 결제일, days 기본 {CUTOFF_DAYS})",
    "next_ship_day": "base(기본 오늘) 다음 날부터 첫 산지 출고일(출고 요일 가운데 영업일)",
    "day_info": "base 날짜의 요일, 영업일·출고일 여부",
    "weight_shortfall": "수산물 중량 미달 판정(날짜 아님). labeled_g=표기 중량, shipped_g=출고 중량(로트·출고 검사 기록, 없으면 비움), "
                        "received_g=고객 계량치(참고, 표기와 직접 비교하지 않음), paid=실결제액(원)",
    "partial_refund": "일부 개체 하자 부분환불 산식(날짜 아님). paid=해당 SKU 실결제액, defects=하자 개수, units=입수(SKU 카탈로그 값, "
                      "중량판매 상품만 실물 개수), prior_refunded=같은 주문에 이미 환불·보상한 금액(선택)",
}

DISCRETION_LIMIT = int(_REFUND["discretion_limit"])  # 재량 한도(동일 주문·사안 합산). 넘으면 이관
PARTIAL_LOW = float(_REFUND["partial_low"])  # 하자율(%)이 이보다 낮으면 비례 환불
PARTIAL_HIGH = float(_REFUND["partial_high"])  # 이보다 높으면 사실상 전부 하자
MID_RATIO = Decimal(str(_REFUND["partial_mid_ratio"]))  # 중간 구간의 환불 비율
MID_PCT = f"{float(MID_RATIO) * 100:g}%"
MIN_AMOUNT = int(_REFUND["min_amount"])  # 부분환불 최소 금액
ROUND_UNIT = int(_REFUND["round_unit"])  # 부분환불 금액 올림 단위(원)


def partial_refund(paid: int, defects: int, units: int, prior_refunded: int = 0) -> dict:
    """부분환불 산식. R = 하자 ÷ 입수 × 100을 소수 첫째 자리로 반올림해 구간을 정한다(경계값은 설정).
    R < LOW: 실결제액 × 하자/입수, LOW ≤ R ≤ HIGH: 실결제액 × MID_RATIO 또는 전량 재배송, R > HIGH: 전액환불 또는 전량 재배송(배송비 부담 없음).
    부분환불 금액은 ROUND_UNIT원 단위 올림, 최소 MIN_AMOUNT원, 배송비는 돌려주지 않는다. 재배송을 고르면 환불하지 않는다."""
    rate = (Decimal(defects) * 100 / Decimal(units)).quantize(Decimal("0.1"), ROUND_HALF_UP)
    if rate > Decimal(str(PARTIAL_HIGH)):
        return {"rate": float(rate), "zone": "사실상 전부 하자", "amount": paid, "choices": ["전액환불", "전량 재배송"],
                "needs_escalation": False}  # 하자 전액환불·재배송은 금액과 관계없이 재량으로 본다
    if rate < Decimal(str(PARTIAL_LOW)):
        amount, zone, choices = Decimal(paid) * defects / units, "부분 하자", ["부분환불"]
    else:
        amount, zone, choices = Decimal(paid) * MID_RATIO, "상당 부분 하자", [f"{MID_PCT} 부분환불", "전량 재배송"]
    amount = max(int(math.ceil(amount / ROUND_UNIT) * ROUND_UNIT), MIN_AMOUNT)
    return {"rate": float(rate), "zone": zone, "amount": amount, "choices": choices,
            "needs_escalation": amount + prior_refunded > DISCRETION_LIMIT}


def _partial_refund_text(r: dict) -> str:
    choices = " 또는 ".join(r["choices"])
    if r["zone"] == "사실상 전부 하자":
        text = f"하자율 {r['rate']:.1f}%(사실상 전부 하자): 전액환불 {r['amount']:,}원 또는 전량 재배송 중 고객 선택, 배송비 부담 없음"
    else:
        money = f"{r['amount']:,}원" + ("" if len(r["choices"]) == 1 else f"({MID_PCT})")
        text = f"하자율 {r['rate']:.1f}%({r['zone']}): {money} 부분환불" + ("" if len(r["choices"]) == 1 else " 또는 전량 재배송 중 고객 선택(재배송이면 환불 없음)") + ", 배송비는 환불하지 않음"
    if r["needs_escalation"]:
        text += f". needs_escalation=true: 같은 주문 합산이 재량 한도 {DISCRETION_LIMIT:,}원을 넘어 승인이 필요하다. 금액을 확정 안내하지 말고 운영자 승인 이관"
    return text

LABEL_TOLERANCE = float(_WEIGHT["label_tolerance"])  # 표기 허용오차
NATURAL_LOSS = float(_WEIGHT["natural_loss"])  # 자연감량 허용치
WEIGHT_LIMIT = LABEL_TOLERANCE + NATURAL_LOSS


def weight_shortfall(labeled_g: float, shipped_g: float | None, received_g: float | None = None, paid: int | None = None) -> str:
    """중량 미달 판정. 기준 중량은 출고 중량(없으면 표기 중량). 금액 산식 문서가 없어 금액은 참고값으로만 낸다."""
    if not labeled_g or labeled_g <= 0:
        return "오류: labeled_g(표기 중량)가 필요합니다."
    base = shipped_g if shipped_g else labeled_g
    rate = (labeled_g - base) / labeled_g
    lines = [f"기준 중량 {base:g}g({'출고 기록' if shipped_g else '출고 기록 없음, 표기 중량'}), 표기 {labeled_g:g}g 대비 미달률 {rate * 100:.1f}%"]
    if rate <= WEIGHT_LIMIT:
        lines.append(f"판정: 정상 범위({WEIGHT_LIMIT * 100:g}% 이내 = 표기 오차 {LABEL_TOLERANCE * 100:g}% + 자연감량 {NATURAL_LOSS * 100:g}%). 계량 결과와 근거를 설명한다")
    else:
        lack = labeled_g - base
        money = f", 실결제액 비례 참고값 {round(paid * lack / labeled_g):,}원(산식 문서 미입수, 금액 확정·약속 금지, 이관 reason에 적음)" if paid else ""
        lines.append(f"판정: 실제 미달. 대상은 부족분 전체 {lack:g}g{money}. 부분환불 또는 재배송 중 고객 선택")
    if received_g and shipped_g and abs(shipped_g - received_g) > labeled_g * NATURAL_LOSS:  # 자연감량 허용치는 표기 중량 기준
        lines.append(f"고객 계량치 {received_g:g}g가 출고 중량과 자연감량({NATURAL_LOSS * 100:g}%)으로 설명되지 않게 다름 → 대조 결과를 근거로 재촬영 요청. 조리·폐기했으면 출고 기록만으로 판정")
    return ". ".join(lines)


def calc(mode: str, base_text: str = "", days: int | None = None, received_at: str = "", weights: dict | None = None) -> str:
    """calc_date 도구 실행. 사람이 읽을 결과 한 줄(오류면 '오류:'로 시작)."""
    if mode == "partial_refund":
        w = weights or {}
        try:
            paid, defects, units = int(w["paid"]), int(w["defects"]), int(w["units"])
            prior = int(w.get("prior_refunded") or 0)
        except (KeyError, TypeError, ValueError):
            return "오류: partial_refund는 paid, defects, units(정수)가 필요합니다."
        if paid <= 0 or units <= 1 or not 0 < defects <= units:
            return "오류: paid>0, units≥2(단품은 산식 대상 아님), 0<defects≤units여야 합니다."
        return _partial_refund_text(partial_refund(paid, defects, units, prior))
    if mode == "weight_shortfall":
        w = weights or {}
        try:
            num = {k: (float(w[k]) if w.get(k) not in (None, "") else None) for k in ("labeled_g", "shipped_g", "received_g", "paid")}
        except (TypeError, ValueError):
            return "오류: 중량·금액은 숫자여야 합니다."
        return weight_shortfall(num["labeled_g"] or 0, num["shipped_g"], num["received_g"], int(num["paid"]) if num["paid"] else None)
    today = today_from(received_at)
    base = parse(base_text) if base_text else today
    if base is None:
        return "오류: base는 YYYY-MM-DD 형식이어야 합니다."
    if days is not None and (not isinstance(days, int) or not 0 <= days <= 365):
        return "오류: days는 0~365 사이 정수여야 합니다."
    if mode == "business_deadline":
        n = REPLY_DAYS if days is None else days
        return f"{fmt(base)} 기준 영업일 {n}일 기한: {fmt(business_deadline(base, n))} 24:00까지"
    if mode == "calendar_deadline":
        n = CALENDAR_DAYS if days is None else days
        end = calendar_deadline(base, n)
        elapsed = (today - base).days
        state = "기한 안" if today <= end else "기한 지남"
        return f"{fmt(base)}부터 {n}일: {fmt(end)} 24:00까지. 오늘 {fmt(today)}은 {fmt(base)}로부터 {elapsed}일 지난 날로 {state}"
    if mode == "subscription_cutoff":
        n = CUTOFF_DAYS if days is None else days
        c = cutoff(base, n)
        state = "컷오프 전(이번 회차 반영)" if today <= c else "컷오프 지남(다음 회차부터 반영)"
        return f"결제일 {fmt(base)}의 {n}영업일 전 컷오프: {fmt(c)} 24:00. 오늘 {fmt(today)} 기준 {state}"
    if mode == "next_ship_day":
        return f"{fmt(base)} 다음 산지 출고일: {fmt(next_ship_day(base))}"
    if mode == "day_info":
        kind = "출고일" if is_ship_day(base) else "영업일(출고 없음)" if is_business_day(base) else "휴무일"
        return f"{fmt(base)}: {kind}"
    return "오류: mode는 " + ", ".join(MODES) + " 중 하나입니다."


def session_lines(received_at: str) -> list[str]:
    """상담 정보에 미리 넣을 날짜(도구 호출 없이 자주 쓰는 값)."""
    today = today_from(received_at)
    note = "" if parse(received_at) else f" (상담 인입 일시가 없어 기준일 {DEFAULT_TODAY.isoformat()}으로 둠)"
    return [
        f"- 오늘: {fmt(today)}{note}, {'출고일' if is_ship_day(today) else '영업일(출고 없음)' if is_business_day(today) else '휴무일'}",
        f"- 오늘 이관하면 회신 기한(영업일 {REPLY_DAYS}일): {fmt(business_deadline(today, REPLY_DAYS))} 24:00까지",
        f"- 다음 산지 출고일: {fmt(next_ship_day(today))}",
    ]
