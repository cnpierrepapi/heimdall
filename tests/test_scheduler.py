"""T7: the scheduled run. Cutover safety, publishing, logging, and accumulation.

The tick itself needs live DataHub and an LLM, so it is proven on the box. What
is locked here is everything wrapped around it: that the cutover fires exactly
once and never on a skipped tick, that a failure anywhere leaves the timer alive,
and that trust genuinely strengthens as a recurring agent's record grows, which
is the whole reason the engine runs on a schedule rather than once.
"""

from __future__ import annotations

from heimdall.claims import ClaimStore
from heimdall.engine import EngineConfig, TickResult
from heimdall.grounding import WorldCatalogContext
from heimdall.observability import ObservationEvent
from heimdall.scheduler import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    cutover_done,
    log_line,
    run_once,
)
from heimdall.simulator.world import build_default_world
from heimdall.trust import settle_observations, trust_report

CTX = WorldCatalogContext(build_default_world())


class FakePublisher:
    """Records what a run asked of the publisher."""

    def __init__(self, sink: dict, fail_on: str = ""):
        self.sink = sink
        self.fail_on = fail_on
        sink.setdefault("resets", 0)
        sink.setdefault("published", [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def reset_showcase(self):
        if self.fail_on == "reset":
            raise RuntimeError("reset boom")
        self.sink["resets"] += 1

    def publish_tick(self, result):
        if self.fail_on == "publish":
            raise RuntimeError("publish boom")
        self.sink["published"].append(result.catalog)
        return {"activity": len(result.activity), "findings": len(result.findings),
                "agents": len(result.agents)}


def _ok_tick(catalog: str = "hcatalog_test") -> TickResult:
    return TickResult(
        ok=True, catalog=catalog, seed=7,
        n_events=4, n_findings=1,
        settle={"settled": 3, "accepted": 2, "reverted": 1},
        spend_tick=0.0021, spend_total=0.42,
        activity=[{"a": 1}], findings=[{"f": 1}], agents=[{"g": 1}],
    )


def _patch_tick(monkeypatch, result):
    def fake(cfg, seed=None):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr("heimdall.scheduler.run_tick", fake)


# -- cutover safety -----------------------------------------------------------


def test_first_successful_run_resets_showcase_once(tmp_path, monkeypatch):
    _patch_tick(monkeypatch, _ok_tick())
    cfg = EngineConfig(home=str(tmp_path))
    sink: dict = {}

    first = run_once(cfg, publisher_factory=lambda: FakePublisher(sink))
    assert first.status == STATUS_OK and first.cutover is True
    assert cutover_done(cfg.home)

    second = run_once(cfg, publisher_factory=lambda: FakePublisher(sink))
    assert second.status == STATUS_OK and second.cutover is False
    assert sink["resets"] == 1, "the showcase must be retired exactly once"
    assert len(sink["published"]) == 2


def test_skipped_tick_never_touches_the_console(tmp_path, monkeypatch):
    """A dormant or unhealthy tick must not blank the live feed."""
    _patch_tick(monkeypatch, TickResult(ok=False, reason="before activation date 2026-08-01"))
    cfg = EngineConfig(home=str(tmp_path))
    sink: dict = {}

    run = run_once(cfg, publisher_factory=lambda: FakePublisher(sink))

    assert run.status == STATUS_SKIPPED
    assert sink == {}, "no publisher work on a skipped tick"
    assert not cutover_done(cfg.home)
    assert "before activation" in run.line


def test_cutover_not_marked_when_the_reset_fails(tmp_path, monkeypatch):
    """A failed reset must stay retryable rather than silently counting as done."""
    _patch_tick(monkeypatch, _ok_tick())
    cfg = EngineConfig(home=str(tmp_path))
    sink: dict = {}

    run = run_once(cfg, publisher_factory=lambda: FakePublisher(sink, fail_on="reset"))

    assert run.status == STATUS_ERROR
    assert not cutover_done(cfg.home)
    assert sink["published"] == []


# -- the timer survives everything --------------------------------------------


def test_a_raising_tick_is_reported_not_propagated(tmp_path, monkeypatch):
    _patch_tick(monkeypatch, RuntimeError("datahub exploded"))
    run = run_once(EngineConfig(home=str(tmp_path)), publisher_factory=lambda: None)
    assert run.status == STATUS_ERROR
    assert "stage=tick" in run.line and "RuntimeError" in run.line


def test_a_failed_publish_is_reported_not_propagated(tmp_path, monkeypatch):
    _patch_tick(monkeypatch, _ok_tick())
    sink: dict = {}
    run = run_once(EngineConfig(home=str(tmp_path)),
                   publisher_factory=lambda: FakePublisher(sink, fail_on="publish"))
    assert run.status == STATUS_ERROR
    assert "publish_error=" in run.line and "RuntimeError" in run.line


def test_publishing_can_be_disabled_for_dry_runs(tmp_path, monkeypatch):
    _patch_tick(monkeypatch, _ok_tick())
    sink: dict = {}
    run = run_once(EngineConfig(home=str(tmp_path)), publish_rows=False,
                   publisher_factory=lambda: FakePublisher(sink))
    assert run.status == STATUS_OK and sink == {}


# -- the log line -------------------------------------------------------------


def test_log_line_carries_the_operating_numbers(tmp_path, monkeypatch):
    _patch_tick(monkeypatch, _ok_tick("hcatalog_abc123"))
    run = run_once(EngineConfig(home=str(tmp_path)),
                   publisher_factory=lambda: FakePublisher({}))
    for token in ("status=ok", "catalog=hcatalog_abc123", "events=4", "findings=1",
                  "settled=3", "spend_total=0.4200", "published=activity:1"):
        assert token in run.line, f"missing {token} in {run.line}"


def test_log_line_quotes_values_containing_spaces():
    line = log_line(STATUS_SKIPPED, {"reason": "budget exhausted at cap", "n": 2})
    assert 'reason="budget exhausted at cap"' in line and "n=2" in line


def test_log_line_omits_empty_fields():
    assert log_line(STATUS_OK, {"catalog": None, "seed": 3}) == "status=ok seed=3"


# -- trust strengthens across ticks -------------------------------------------

GOLD = [
    ("raw_orders", "order_total_usd", "Total order amount in usd."),
    ("raw_customers", "email", "Customer email address."),
    ("raw_payments", "amount_usd", "Amount paid in usd."),
    ("raw_web_events", "event_type", "The type of event action recorded."),
    ("fct_revenue", "paid_usd", "Revenue amount paid in usd."),
    ("fct_orders", "discount_usd", "Discount applied in usd."),
]


def _write(agent: str, dataset: str, column: str, description: str, ts: float):
    urn = build_default_world().datasets[dataset].urn
    return ObservationEvent(
        agent_id=agent, tool="update_description", op="write", status="ok", ts=ts,
        args={"entity_urn": urn, "column_path": column,
              "description": description, "operation": "replace"},
    )


def _three_ticks(store: ClaimStore) -> list[dict]:
    """Two agents documenting the same two columns per tick, one right one wrong.

    Both are scored against the pooled acceptance baseline, so they have to be in
    the store together: an agent alone in the pool is graded against its own
    record and can never read as worse than chance.
    """
    trace = []
    for tick in range(3):
        events = []
        for i, (ds, col, desc) in enumerate(GOLD[tick * 2:tick * 2 + 2]):
            events.append(_write("atlas-doc", ds, col, desc, ts=1000.0 + tick * 100 + i))
            events.append(_write("nyx-doc", ds, col, "a column", ts=1000.0 + tick * 100 + i + 50))
        counts = settle_observations(events, CTX, store)
        assert counts["settled"] == 4 and counts["accepted"] == 2 and counts["reverted"] == 2

        report = trust_report(store)
        trace.append({"atlas": report["atlas-doc"]["column_doc"],
                      "nyx": report["nyx-doc"]["column_doc"]})
    return trace


def test_evidence_accumulates_across_ticks(tmp_path):
    """The point of a schedule: the same agents recur, so the record deepens."""
    trace = _three_ticks(ClaimStore(str(tmp_path / "trust.db")))

    assert [t["atlas"]["n_settled"] for t in trace] == [2, 4, 6]
    assert [t["nyx"]["n_settled"] for t in trace] == [2, 4, 6]


def test_verdicts_sharpen_as_the_record_grows(tmp_path):
    """Low n is honest about itself; by six settled claims the two separate."""
    trace = _three_ticks(ClaimStore(str(tmp_path / "trust.db")))

    assert trace[0]["atlas"]["verdict"] == "insufficient settled claims"
    assert trace[0]["nyx"]["verdict"] == "insufficient settled claims"
    assert trace[-1]["atlas"]["verdict"] == "skilled"
    assert trace[-1]["nyx"]["verdict"] == "worse than chance"


def test_the_trust_gap_widens_with_n(tmp_path):
    """Magnitudes compress toward 50 at low n by design, so the gap is the signal."""
    trace = _three_ticks(ClaimStore(str(tmp_path / "trust.db")))

    gaps = [t["atlas"]["trust"] - t["nyx"]["trust"] for t in trace]
    assert gaps == sorted(gaps), f"separation must not regress as evidence grows: {gaps}"
    assert gaps[-1] > gaps[0], f"more evidence must separate them further: {gaps}"
    assert all(t["atlas"]["trust"] > t["nyx"]["trust"] for t in trace)
