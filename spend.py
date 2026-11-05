"""Anthropic 사용액을 봇이 직접 집계한다(Admin API는 쓰지 않는다).

- cs_agent._create가 호출마다 response.usage를 record로 넘긴다. 모델 단가(100만 토큰당 달러)로 비용을 계산해 누적한다.
- 누적은 SPEND_FILE(기본 logs/spend.json, git 제외)에 원자적으로 저장해 재시작해도 이어진다. 비우면 메모리에만 둔다(테스트).
- 기준은 .env의 CREDIT_TOPUP_USD(충전액)와 CREDIT_TOPUP_AT(충전 시각, 기록용). 충전액이 바뀌면 0부터 다시 센다.
- 남은 추정액이 충전액의 30%, 15% 아래로 처음 떨어지면 한 번씩 경고 문구를 돌려준다. 정기 보고는 watch_report가 SPEND_REPORT_SEC마다 보낸다.
- 다른 모듈(예산 가드 등)은 spent_usd(), remaining_usd()로 읽는다.
추정치다. 실제 청구액은 콘솔 Usage에서 확인한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

import config  # noqa: F401  .env 로드

logger = logging.getLogger("spend")

# 100만 토큰당 달러. 단가를 아는 모델은 Sonnet 5.5(답변)와 Haiku 5.5(절약 모드)뿐이다. 다른 모델은 Sonnet 단가로 계산하고 경고 로그를 한 번 남긴다
# (Opus 등 더 비싼 모델이면 과소 집계되므로, AGENT_MODEL을 바꾸면 여기 단가를 먼저 넣는다).
# 캐시 읽기는 입력의 0.1배, 1시간 캐시 쓰기는 2배, 5분 캐시 쓰기는 1.25배다.
SONNET = {"input": 2.00, "cache_read": 0.20, "cache_write_1h": 4.00, "cache_write_5m": 2.50, "output": 10.00}
HAIKU = {"input": 0.10, "cache_read": 0.01, "cache_write_1h": 0.20, "cache_write_5m": 0.125, "output": 0.50}
HAIKU_LONG = {"input": 0.50, "cache_read": 0.05, "cache_write_1h": 1.00, "cache_write_5m": 0.625, "output": 2.50}
PRICES = {"claude-sonnet-5-5": SONNET, "claude-haiku-5-5": HAIKU}
# 요청 입력(캐시 읽기·쓰기 포함)이 기준 토큰을 넘으면 그 요청 전체에 긴 입력 단가를 쓰는 모델. 이 봇의 입력(약 25만)은 늘 넘는다.
LONG_PRICES = {"claude-haiku-5-5": (100_000, HAIKU_LONG)}
_unknown_warned: set[str] = set()
WARN_FRACTIONS = (0.30, 0.15, 0.05)

ROOT = Path(__file__).parent
SPEND_FILE = os.environ.get("SPEND_FILE", str(ROOT / "logs" / "spend.json"))
TOPUP_USD = float(os.environ.get("CREDIT_TOPUP_USD", "20") or 20)
TOPUP_AT = os.environ.get("CREDIT_TOPUP_AT", "")
REPORT_SEC = float(os.environ.get("SPEND_REPORT_SEC", "3600") or 3600)


def _get(obj, name: str) -> int:
    value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return int(value or 0)


def cost(model: str, usage) -> float:
    """호출 한 번의 비용(달러). cache_creation에 1시간·5분 구분이 없으면 전부 1시간 캐시 쓰기로 본다(이 봇의 설정)."""
    if usage is None:
        return 0.0
    price = PRICES.get(model)
    if price is None:
        if model not in _unknown_warned:
            _unknown_warned.add(model)
            logger.warning("spend: no price for %s, using sonnet price (may under-count)", model)
        price = SONNET
    creation = usage.get("cache_creation") if isinstance(usage, dict) else getattr(usage, "cache_creation", None)
    write_total = _get(usage, "cache_creation_input_tokens")
    if creation is not None:
        write_1h, write_5m = _get(creation, "ephemeral_1h_input_tokens"), _get(creation, "ephemeral_5m_input_tokens")
        if write_1h + write_5m == 0:
            write_1h = write_total
    else:
        write_1h, write_5m = write_total, 0
    tokens = {
        "input": _get(usage, "input_tokens"),
        "cache_read": _get(usage, "cache_read_input_tokens"),
        "cache_write_1h": write_1h,
        "cache_write_5m": write_5m,
        "output": _get(usage, "output_tokens"),
    }
    long = LONG_PRICES.get(model)
    if long and tokens["input"] + tokens["cache_read"] + tokens["cache_write_1h"] + tokens["cache_write_5m"] > long[0]:
        price = long[1]
    return sum(tokens[k] * price[k] for k in tokens) / 1_000_000


class Ledger:
    """누적 사용액. record는 에이전트 전용 루프에서, report는 메인 루프에서 불리므로 잠금을 쓴다."""

    def __init__(self, path: str = SPEND_FILE, topup_usd: float = TOPUP_USD, topup_at: str = TOPUP_AT):
        self.path = path
        self.topup_usd = topup_usd
        self.topup_at = topup_at
        self._lock = threading.Lock()
        self.spent = 0.0
        self.calls = 0
        self.warned: list[float] = []
        self.window_spent = 0.0  # 지난 보고 뒤 사용액
        self.window_calls = 0
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("topup_usd") != self.topup_usd:
            logger.info("spend reset: topup changed %s -> %s", data.get("topup_usd"), self.topup_usd)
            return  # 충전액이 바뀌면 0부터 다시 센다
        self.spent = float(data.get("spent_usd", 0.0))
        self.calls = int(data.get("calls", 0))
        self.warned = [float(x) for x in data.get("warned", [])]

    def _save(self) -> None:
        if not self.path:
            return
        path = Path(self.path)
        data = {"topup_usd": self.topup_usd, "topup_at": self.topup_at, "spent_usd": round(self.spent, 6),
                "calls": self.calls, "warned": self.warned, "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)  # 원자적으로 바꾼다(쓰다 죽어도 이전 파일이 남는다)
        except OSError as e:
            logger.warning("spend save failed: %s", type(e).__name__)

    def spent_usd(self) -> float:
        with self._lock:
            return self.spent

    def remaining_usd(self) -> float:
        with self._lock:
            return self.topup_usd - self.spent

    def record(self, model: str, usage) -> list[str]:
        """호출 한 번을 더한다. 처음 넘은 경고선이 있으면 경고 문구를 돌려준다."""
        amount = cost(model, usage)
        messages = []
        with self._lock:
            self.spent += amount
            self.calls += 1
            self.window_spent += amount
            self.window_calls += 1
            remaining = self.topup_usd - self.spent
            for fraction in WARN_FRACTIONS:
                if remaining < self.topup_usd * fraction and fraction not in self.warned:
                    self.warned.append(fraction)
                    messages.append(
                        f"**[크레딧 경고]** 남은 추정액 ${remaining:.2f}로 충전액 ${self.topup_usd:g}의 {fraction:.0%} 아래입니다. "
                        "콘솔에서 잔액을 확인하고 충전하세요."
                    )
            self._save()
        return messages

    def report(self) -> str:
        """정기 보고 문구. 보고 구간 집계는 0으로 돌린다."""
        with self._lock:
            text = (f"**[크레딧]** 충전 후 사용 약 ${self.spent:.2f} / ${self.topup_usd:g}, 남은 추정 ${self.topup_usd - self.spent:.2f}, "
                    f"지난 보고 뒤 ${self.window_spent:.2f}, 호출 {self.window_calls}회 (추정치, 실제는 콘솔 Usage)")
            self.window_spent, self.window_calls = 0.0, 0
        return text


ledger = Ledger()


def spent_usd() -> float:
    return ledger.spent_usd()


def remaining_usd() -> float:
    return ledger.remaining_usd()


def record(model: str, usage) -> list[str]:
    return ledger.record(model, usage)


async def watch_report(send, stop: asyncio.Event, interval: float = REPORT_SEC) -> None:
    """interval마다 사용액 보고를 send(문구)로 보낸다. 메인 루프에서 돈다."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), interval)
            return
        except TimeoutError:
            pass
        message = ledger.report()
        logger.info("%s", message)
        await send(message)
