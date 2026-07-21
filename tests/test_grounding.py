"""Catalog-grounded evaluators against the demo world's real traps."""

from __future__ import annotations

from heimdall.grounding import (
    CHECK_GLOSSARY_CONFLICT,
    CHECK_LOW_QUALITY,
    CHECK_PII_SCOPE,
    CHECK_UNDEFINED_COLUMN,
    CHECK_WRONG_DOMAIN,
    CHECK_WRONG_OWNER,
    SEV_HARMFUL,
    SEV_WARN,
    Finding,
    FindingStore,
    WorldCatalogContext,
    ground_event,
    ground_events,
    parse_action,
)
from heimdall.observability import ObservationEvent
from heimdall.simulator.world import build_default_world

CTX = WorldCatalogContext(build_default_world())

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_orders,PROD)"
PAYMENTS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_payments,PROD)"
CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_customers,PROD)"


def ev(tool, args, agent="agent", op="write"):
    return ObservationEvent(agent_id=agent, tool=tool, op=op, status="ok", args=args)


def checks(findings):
    return {f.check_type for f in findings}


# -- undefined column ---------------------------------------------------------


def test_undefined_column_flagged():
    f = ground_event(ev("update_description", {
        "entity_urn": ORDERS, "column_path": "ghost_col",
        "description": "anything", "operation": "replace",
    }), CTX)
    assert checks(f) == {CHECK_UNDEFINED_COLUMN}
    assert f[0].severity == SEV_HARMFUL
    assert "ghost_col" in f[0].reason and "raw_orders" in f[0].reason


def test_known_column_not_flagged_undefined():
    f = ground_event(ev("update_description", {
        "entity_urn": ORDERS, "column_path": "order_id",
        "description": "Primary key of the order.", "operation": "replace",
    }), CTX)
    assert CHECK_UNDEFINED_COLUMN not in checks(f)


# -- glossary conflict --------------------------------------------------------


def test_glossary_conflict_when_description_asserts_another_terms_concept():
    # amount_usd's term is "Settled Payment Amount"; describing it as the
    # "gross order value" asserts a different catalog term
    f = ground_event(ev("update_description", {
        "entity_urn": PAYMENTS, "column_path": "amount_usd",
        "description": "The gross order value in usd.", "operation": "replace",
    }), CTX)
    assert CHECK_GLOSSARY_CONFLICT in checks(f)
    finding = [x for x in f if x.check_type == CHECK_GLOSSARY_CONFLICT][0]
    assert finding.severity == SEV_HARMFUL
    assert "Gross Order Value" in finding.reason
    assert "Settled Payment Amount" in finding.reason


def test_correct_description_no_glossary_conflict():
    f = ground_event(ev("update_description", {
        "entity_urn": PAYMENTS, "column_path": "amount_usd",
        "description": "Settled payment amount in usd; the amount paid.",
        "operation": "replace",
    }), CTX)
    assert CHECK_GLOSSARY_CONFLICT not in checks(f)
    assert CHECK_LOW_QUALITY not in checks(f)  # mentions gold keyword 'paid'/'usd'


# -- low quality --------------------------------------------------------------


def test_filler_description_flagged_low_quality():
    f = ground_event(ev("update_description", {
        "entity_urn": ORDERS, "column_path": "order_total_usd",
        "description": "This is a column.", "operation": "replace",
    }), CTX)
    assert checks(f) == {CHECK_LOW_QUALITY}
    assert f[0].severity == SEV_WARN


def test_remove_operation_is_not_graded():
    f = ground_event(ev("update_description", {
        "entity_urn": ORDERS, "column_path": "order_total_usd", "operation": "remove",
    }), CTX)
    assert f == []


# -- PII scope ----------------------------------------------------------------


def test_false_pii_on_non_pii_column():
    # customer_id is a deliberate non-PII trap
    f = ground_event(ev("add_tags", {
        "entity_urns": [ORDERS], "column_path": "customer_id",
        "tag_urns": ["urn:li:tag:pii-email"],
    }), CTX)
    assert checks(f) == {CHECK_PII_SCOPE}
    assert f[0].severity == SEV_HARMFUL
    assert "non-sensitive" in f[0].reason


def test_pii_type_mismatch():
    # full_name really is PII, but of type person_name, not email
    f = ground_event(ev("add_tags", {
        "entity_urns": [CUSTOMERS], "column_path": "full_name",
        "tag_urns": ["urn:li:tag:pii-email"],
    }), CTX)
    assert checks(f) == {CHECK_PII_SCOPE}
    assert "person_name" in f[0].reason


def test_correct_pii_not_flagged():
    f = ground_event(ev("add_tags", {
        "entity_urns": [CUSTOMERS], "column_path": "email",
        "tag_urns": ["urn:li:tag:pii-email"],
    }), CTX)
    assert f == []


# -- governance ---------------------------------------------------------------


def test_wrong_domain_flagged():
    f = ground_event(ev("set_domains", {
        "entity_urn": ORDERS, "domain_urn": "urn:li:domain:Customers",
    }), CTX)
    assert checks(f) == {CHECK_WRONG_DOMAIN}
    assert "Commerce" in f[0].reason


def test_correct_domain_case_insensitive_not_flagged():
    f = ground_event(ev("set_domains", {
        "entity_urn": ORDERS, "domain_urn": "urn:li:domain:commerce",
    }), CTX)
    assert f == []


def test_wrong_owner_flagged():
    f = ground_event(ev("add_owners", {
        "entity_urns": [ORDERS], "owner_urns": ["urn:li:corpGroup:marketing"],
    }), CTX)
    assert checks(f) == {CHECK_WRONG_OWNER}
    assert "data-platform" in f[0].reason


# -- parsing and non-groundable ----------------------------------------------


def test_parse_action_extracts_fields():
    a = parse_action(ev("add_terms", {
        "entity_urns": [ORDERS], "column_paths": ["order_total_usd"],
        "term_urns": ["urn:li:glossaryTerm:Gross Order Value"],
    }))
    assert a.entity_urn == ORDERS
    assert a.columns == ["order_total_usd"]
    assert a.term_names == ["Gross Order Value"]


def test_unknown_entity_not_grounded():
    f = ground_event(ev("update_description", {
        "entity_urn": "urn:li:dataset:(urn:li:dataPlatform:x,not.real,PROD)",
        "column_path": "c", "description": "d", "operation": "replace",
    }), CTX)
    assert f == []


def test_reads_produce_no_findings():
    f = ground_event(ev("get_entities", {"urns": [ORDERS]}, op="read"), CTX)
    assert f == []


# -- store --------------------------------------------------------------------


def test_finding_store_record_filter_summary(tmp_path):
    store = FindingStore(str(tmp_path / "f.db"))
    events = [
        ev("update_description", {"entity_urn": ORDERS, "column_path": "ghost",
                                  "description": "x", "operation": "replace"}, agent="bad"),
        ev("add_tags", {"entity_urns": [ORDERS], "column_path": "customer_id",
                        "tag_urns": ["urn:li:tag:pii-email"]}, agent="bad"),
        ev("update_description", {"entity_urn": ORDERS, "column_path": "order_id",
                                  "description": "Primary key of the order.",
                                  "operation": "replace"}, agent="good"),
    ]
    found = ground_events(events, CTX, store)
    assert len(found) == 2
    assert len(store.findings(agent_id="bad")) == 2
    assert store.findings(agent_id="good") == []
    assert len(store.findings(check_type=CHECK_PII_SCOPE)) == 1
    assert len(store.findings(severity=SEV_HARMFUL)) == 2
    assert store.summary()["bad"] == {"total": 2, "harmful": 2, "warn": 0}
    store.close()
