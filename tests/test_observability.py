"""Observation event store: extraction, sanitization, persistence, queries."""

from __future__ import annotations

from heimdall.observability import (
    BLOCKED,
    ERROR,
    OK,
    READ,
    WRITE,
    EventStore,
    ObservationEvent,
    extract_entity_urns,
    sanitize_args,
    summarize_result,
)

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_orders,PROD)"
FIELD = f"urn:li:schemaField:({DS},discount_code)"


def test_extract_simple_urns():
    urns = extract_entity_urns('{"tag": "urn:li:tag:pii", "term": "urn:li:glossaryTerm:Revenue"}')
    assert "urn:li:tag:pii" in urns
    assert "urn:li:glossaryTerm:Revenue" in urns


def test_extract_nested_paren_urn_not_truncated():
    # the schemaField urn contains a dataset urn with its own commas and parens;
    # a naive first-close-paren split would mangle it
    urns = extract_entity_urns({"entity_urn": FIELD})
    assert FIELD in urns  # full balanced urn recovered
    assert DS in urns     # inner dataset also surfaced


def test_extract_dedupes_in_order():
    text = f"{DS} then {DS} again and urn:li:tag:pii"
    urns = extract_entity_urns(text)
    assert urns.count(DS) == 1
    assert urns.index(DS) < urns.index("urn:li:tag:pii")


def test_extract_respects_limit():
    text = " ".join(f"urn:li:tag:t{i}" for i in range(50))
    assert len(extract_entity_urns(text, limit=10)) == 10


def test_sanitize_truncates_long_strings_keeps_structure():
    long = "x" * 900
    out = sanitize_args({"description": long, "nested": {"k": [long, 1, True]}})
    assert out["description"].endswith("...(truncated)")
    assert len(out["description"]) < 900
    assert out["nested"]["k"][1] == 1 and out["nested"]["k"][2] is True


def test_summarize_collapses_whitespace_and_caps():
    assert summarize_result("a\n\n  b   c") == "a b c"
    assert summarize_result("y" * 400).endswith("...")


def test_event_defaults():
    ev = ObservationEvent(agent_id="a", tool="search", op=READ)
    assert ev.status == OK
    assert ev.event_id and ev.ts > 0
    assert ev.entities == [] and ev.args == {}


def test_store_record_and_filter(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    store.record(ObservationEvent(agent_id="a", tool="search", op=READ, entities=[DS], ts=1.0))
    store.record(ObservationEvent(agent_id="a", tool="update_description", op=WRITE,
                                  status=OK, entities=[DS], ts=2.0))
    store.record(ObservationEvent(agent_id="b", tool="update_description", op=WRITE,
                                  status=BLOCKED, ts=3.0))
    store.record(ObservationEvent(agent_id="a", tool="get_entities", op=READ,
                                  status=ERROR, ts=4.0))

    assert len(store.events()) == 4
    assert len(store.events(agent_id="a")) == 3
    assert len(store.events(op=WRITE)) == 2
    assert len(store.events(status=BLOCKED)) == 1
    assert len(store.events(entity_urn=DS)) == 2
    assert len(store.events(since_ts=3.0)) == 2
    assert len(store.events(limit=1)) == 1
    # ordered by ts
    assert [e.ts for e in store.events()] == [1.0, 2.0, 3.0, 4.0]
    store.close()


def test_store_summary(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    for op, status in [(READ, OK), (READ, OK), (WRITE, OK), (WRITE, ERROR), (WRITE, BLOCKED)]:
        store.record(ObservationEvent(agent_id="a", tool="t", op=op, status=status))
    s = store.summary()["a"]
    assert s == {"total": 5, "reads": 2, "writes": 3, "errors": 1, "blocked": 1, "held": 0}
    store.close()


def test_store_roundtrip_preserves_fields(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    store.record(ObservationEvent(
        agent_id="a", tool="update_description", op=WRITE, status=OK,
        args={"entity_urn": DS, "description": "Total in USD."},
        entities=[DS], latency_ms=42, result_summary="ok",
    ))
    got = store.events()[0]
    assert got.args["description"] == "Total in USD."
    assert got.entities == [DS]
    assert got.latency_ms == 42
    store.close()
