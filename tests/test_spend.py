"""spend.py(사용액 집계) 시험. 파일은 임시 폴더에 쓴다."""

import json
from types import SimpleNamespace

import pytest

import spend

M = 1_000_000


def test_cost_splits_cache_writes_and_uses_sonnet_for_unknown_model():
    usage = SimpleNamespace(input_tokens=M, cache_read_input_tokens=M, cache_creation_input_tokens=2 * M,
                            cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=M, ephemeral_5m_input_tokens=M),
                            output_tokens=M)
    assert spend.cost("claude-sonnet-5-5", usage) == pytest.approx(2.00 + 0.20 + 4.00 + 2.50 + 10.00)
    assert spend.cost("unknown-model", usage) == spend.cost("claude-sonnet-5-5", usage)
    # 구분이 없으면 전부 1시간 캐시 쓰기로 본다
    assert spend.cost("claude-sonnet-5-5", {"cache_creation_input_tokens": M}) == pytest.approx(4.00)
    assert spend.cost("claude-sonnet-5-5", None) == 0.0


def test_ledger_saves_and_restores(tmp_path):
    path = str(tmp_path / "spend.json")
    ledger = spend.Ledger(path, topup_usd=20)
    ledger.record("claude-sonnet-5-5", {"output_tokens": 100_000})  # $1
    again = spend.Ledger(path, topup_usd=20)
    assert again.spent_usd() == pytest.approx(1.0) and again.calls == 1
    assert again.remaining_usd() == pytest.approx(19.0)
    assert not (tmp_path / "spend.json.tmp").exists()
    assert json.loads((tmp_path / "spend.json").read_text(encoding="utf-8"))["topup_usd"] == 20


def test_ledger_resets_when_topup_changes(tmp_path):
    path = str(tmp_path / "spend.json")
    spend.Ledger(path, topup_usd=20).record("claude-sonnet-5-5", {"output_tokens": 100_000})
    assert spend.Ledger(path, topup_usd=50).spent_usd() == 0.0


def test_warnings_fire_once_per_threshold(tmp_path):
    ledger = spend.Ledger(str(tmp_path / "s.json"), topup_usd=10)
    one_dollar = {"output_tokens": 100_000}
    sent = []
    for _ in range(9):
        sent += ledger.record("claude-sonnet-5-5", one_dollar)
    assert len(sent) == 2  # 남은 $2.99 아래(30%)와 $1.49 아래(15%)에서 한 번씩
    assert "30%" in sent[0] and "15%" in sent[1]
    reloaded = spend.Ledger(str(tmp_path / "s.json"), topup_usd=10)
    last = reloaded.record("claude-sonnet-5-5", one_dollar)  # 남은 $0, 5% 아래. 앞의 두 경고는 다시 안 온다
    assert len(last) == 1 and "5%" in last[0]
    assert reloaded.record("claude-sonnet-5-5", one_dollar) == []


def test_report_resets_window(tmp_path):
    ledger = spend.Ledger(str(tmp_path / "s.json"), topup_usd=20)
    ledger.record("claude-sonnet-5-5", {"output_tokens": 100_000})
    first = ledger.report()
    assert "$1.00 / $20" in first and "호출 1회" in first
    assert "지난 보고 뒤 $0.00, 호출 0회" in ledger.report()


def test_haiku_price_tiers_by_total_input():
    short = {"input_tokens": 50_000, "output_tokens": 1_000_000}
    assert spend.cost("claude-haiku-5-5", short) == pytest.approx(50_000 * 0.10 / 1e6 + 0.50)
    # 캐시 읽기·쓰기를 합친 입력이 10만 토큰을 넘으면 그 요청 전체에 긴 입력 단가
    long = {"input_tokens": 10, "cache_read_input_tokens": 250_000, "cache_creation_input_tokens": 0, "output_tokens": 100}
    assert spend.cost("claude-haiku-5-5", long) == pytest.approx((10 * 0.50 + 250_000 * 0.05 + 100 * 2.50) / 1e6)
    write = {"input_tokens": 10, "cache_creation_input_tokens": 250_000, "output_tokens": 0}
    assert spend.cost("claude-haiku-5-5", write) == pytest.approx((10 * 0.50 + 250_000 * 1.00) / 1e6)
