"""T4: spend ledger, cost accounting, the hard cap, and the activation gate.

No network. These lock the money guardrail: real provider cost is preferred over
token math, the cap is a hard stop with no fallback, and the engine is a no-op
before its activation date.
"""

from __future__ import annotations

from datetime import date

import pytest

from heimdall.budget import (
    SpendLedger,
    activation_reached,
    can_run,
    cost_from_usage,
    start_date,
)


def test_cost_prefers_provider_cost():
    usage = {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.0123}
    assert cost_from_usage(usage) == 0.0123


def test_cost_estimates_when_provider_omits_it():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    est = cost_from_usage(usage)
    assert est == pytest.approx(0.20 + 0.60)  # per-million fallback prices


def test_cost_zero_for_empty_usage():
    assert cost_from_usage({}) == 0.0


def test_ledger_records_and_totals(tmp_path):
    led = SpendLedger(tmp_path / "spend.db")
    led.record("atlas-doc", "qwen", {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.01})
    led.record("nyx-doc", "qwen", {"prompt_tokens": 200, "completion_tokens": 80, "cost": 0.02})
    assert led.calls() == 2
    assert led.total() == pytest.approx(0.03)
    assert led.by_agent()["atlas-doc"] == pytest.approx(0.01)


def test_ledger_persists_across_reopen(tmp_path):
    p = tmp_path / "spend.db"
    SpendLedger(p).record("a", "m", {"cost": 0.5})
    assert SpendLedger(p).total() == pytest.approx(0.5)


def test_usage_sink_records_under_agent(tmp_path):
    led = SpendLedger(tmp_path / "spend.db")
    sink = led.usage_sink("vega-pii", "qwen")
    sink({"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.004})
    assert led.by_agent() == {"vega-pii": pytest.approx(0.004)}


def test_over_cap_is_a_hard_stop(tmp_path):
    led = SpendLedger(tmp_path / "spend.db")
    led.record("a", "m", {"cost": 4.0})
    assert not led.over_cap(cap=5.0)
    led.record("a", "m", {"cost": 1.5})
    assert led.over_cap(cap=5.0)
    assert led.remaining(cap=5.0) < 0


def test_can_run_blocks_before_activation(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_START_DATE", "2026-08-01")
    led = SpendLedger(tmp_path / "spend.db")
    ok, why = can_run(led, cap=100.0, now=date(2026, 7, 31))
    assert not ok and "activation" in why
    ok, _ = can_run(led, cap=100.0, now=date(2026, 8, 1))
    assert ok


def test_can_run_halts_at_cap_with_no_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_START_DATE", "2026-08-01")
    led = SpendLedger(tmp_path / "spend.db")
    led.record("a", "m", {"cost": 100.0})
    ok, why = can_run(led, cap=100.0, now=date(2026, 8, 2))
    assert not ok and "budget exhausted" in why


def test_start_date_env_override(monkeypatch):
    monkeypatch.setenv("HEIMDALL_START_DATE", "2026-08-01")
    assert start_date() == date(2026, 8, 1)
    assert not activation_reached(date(2026, 7, 31))
    assert activation_reached(date(2026, 8, 1))
