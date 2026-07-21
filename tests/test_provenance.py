"""Tests for the timeline provenance reader.

Fixtures are real change transactions captured from the demo box's DataHub GMS
(`/openapi/v2/timeline/v1/...`), so the parser is verified against the shapes it
will actually meet, not an idealized contract.
"""

from heimdall.provenance import (
    COLUMN_DOC,
    OWNER,
    PII,
    SCHEMA,
    TABLE_DOC,
    TAG,
    TERM,
    ProvChange,
    dataset_and_field,
    parse_timeline,
    parse_transaction,
    read_changes,
)

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_orders,PROD)"
FIELD = f"urn:li:schemaField:({DS},discount_code)"

# --- real transactions lifted from the box, trimmed to what the parser reads ---

DOC_ADD_FIELD = {
    "timestamp": 1784310695561,
    "actor": "urn:li:corpuser:__datahub_system",
    "changeEvents": [
        {
            "entityUrn": f"urn:li:schemaField:({DS},created_at)",
            "category": "DOCUMENTATION",
            "operation": "ADD",
            "parameters": {"description": "Order creation timestamp."},
        }
    ],
}

DOC_ADD_TABLE = {
    "timestamp": 1784310695898,
    "actor": "urn:li:corpuser:__datahub_system",
    "changeEvents": [
        {
            "entityUrn": DS,
            "category": "DOCUMENTATION",
            "operation": "ADD",
            "parameters": {"description": "lineworld synthetic warehouse table."},
        }
    ],
}

DOC_MODIFY_EMPTY = {
    "timestamp": 1784349581040,
    "actor": "urn:li:corpuser:__datahub_system",
    "changeEvents": [
        {
            "entityUrn": FIELD,
            "category": "DOCUMENTATION",
            "operation": "MODIFY",
            "modifier": "discount_code",
            "parameters": {"description": ""},
        }
    ],
}

TERM_ADD = {
    "timestamp": 1784415286009,
    "actor": "urn:li:corpuser:__datahub_system",
    "changeEvents": [
        {
            "entityUrn": f"urn:li:schemaField:({DS},order_total_usd)",
            "category": "GLOSSARY_TERM",
            "operation": "ADD",
            "modifier": "urn:li:glossaryTerm:GrossOrderValue",
            "parameters": {"termUrn": "urn:li:glossaryTerm:GrossOrderValue"},
        }
    ],
}

OWNER_ADD = {
    "timestamp": 1784415285282,
    "actor": "urn:li:corpuser:__datahub_system",
    "changeEvents": [
        {
            "entityUrn": DS,
            "category": "OWNER",
            "operation": "ADD",
            "modifier": "urn:li:corpGroup:data-platform",
            "parameters": {"ownerUrn": "urn:li:corpGroup:data-platform"},
        }
    ],
}

TAG_PROVENANCE = {
    "timestamp": 1784351190181,
    "actor": "urn:li:corpuser:__datahub_system",
    "changeEvents": [
        {
            "entityUrn": DS,
            "category": "TAG",
            "operation": "ADD",
            "modifier": "urn:li:tag:heimdall-unproven",
            "parameters": {"tagUrn": "urn:li:tag:heimdall-unproven"},
        }
    ],
}

TAG_PII = {
    "timestamp": 1784351190999,
    "actor": "urn:li:corpuser:__datahub_system",
    "changeEvents": [
        {
            "entityUrn": f"urn:li:schemaField:({DS},email)",
            "category": "TAG",
            "operation": "ADD",
            "modifier": "urn:li:tag:pii-email",
            "parameters": {"tagUrn": "urn:li:tag:pii-email"},
        }
    ],
}

SCHEMA_ADD = {
    "timestamp": 1784310695561,
    "actor": "urn:li:corpuser:__datahub_system",
    "changeEvents": [
        {
            "entityUrn": DS,
            "category": "TECHNICAL_SCHEMA",
            "operation": "ADD",
            "parameters": {"fieldPath": "created_at"},
        }
    ],
}


def test_dataset_and_field_splits_nested_urn():
    ds, field = dataset_and_field(FIELD)
    assert ds == DS
    assert field == "discount_code"


def test_dataset_and_field_on_dataset_urn():
    ds, field = dataset_and_field(DS)
    assert ds == DS
    assert field is None


def test_field_documentation_is_column_doc():
    (c,) = parse_transaction(DOC_ADD_FIELD)
    assert c.work_kind == COLUMN_DOC
    assert c.dataset_urn == DS
    assert c.field == "created_at"
    assert c.value == "Order creation timestamp."
    assert c.operation == "ADD"
    assert c.actor == "urn:li:corpuser:__datahub_system"
    assert abs(c.ts - 1784310695.561) < 1e-3  # ms converted to seconds


def test_table_documentation_is_table_doc():
    (c,) = parse_transaction(DOC_ADD_TABLE)
    assert c.work_kind == TABLE_DOC
    assert c.field is None
    assert c.is_field is False


def test_modify_to_empty_is_a_clear():
    (c,) = parse_transaction(DOC_MODIFY_EMPTY)
    assert c.operation == "MODIFY"
    assert c.value == ""
    assert c.is_clear is True  # the revert shape the settlement layer keys on


def test_glossary_term_target():
    (c,) = parse_transaction(TERM_ADD)
    assert c.work_kind == TERM
    assert c.target == "urn:li:glossaryTerm:GrossOrderValue"
    assert c.field == "order_total_usd"


def test_owner_target():
    (c,) = parse_transaction(OWNER_ADD)
    assert c.work_kind == OWNER
    assert c.target == "urn:li:corpGroup:data-platform"
    assert c.field is None


def test_plain_tag_vs_pii_tag():
    (prov,) = parse_transaction(TAG_PROVENANCE)
    assert prov.work_kind == TAG
    assert prov.target == "urn:li:tag:heimdall-unproven"
    (pii,) = parse_transaction(TAG_PII)
    assert pii.work_kind == PII
    assert pii.field == "email"


def test_schema_change_kept_but_labelled():
    (c,) = parse_transaction(SCHEMA_ADD)
    assert c.work_kind == SCHEMA


def test_parse_timeline_flattens_all_transactions():
    changes = parse_timeline([DOC_ADD_FIELD, DOC_ADD_TABLE, TERM_ADD])
    assert len(changes) == 3
    assert {c.work_kind for c in changes} == {COLUMN_DOC, TABLE_DOC, TERM}


def test_read_changes_sorts_and_drops_schema(monkeypatch):
    # An ADD then a later MODIFY-to-empty on the same field: the revert sequence
    # must survive in time order for settlement to read it.
    from heimdall import provenance

    fake = {
        DS: [DOC_ADD_FIELD, SCHEMA_ADD, DOC_MODIFY_EMPTY],
    }

    def fake_fetch(gms_url, urn, categories=provenance.DEFAULT_CATEGORIES, token=None):
        return fake[urn]

    monkeypatch.setattr(provenance, "fetch_timeline", fake_fetch)
    changes = read_changes("http://x", [DS])
    assert all(c.work_kind != SCHEMA for c in changes)  # schema filtered out
    docs = [c for c in changes if c.work_kind == COLUMN_DOC]
    assert [c.operation for c in docs] == ["ADD", "MODIFY"]  # chronological
    assert docs[0].ts <= docs[1].ts


def test_provchange_is_hashable():
    # frozen dataclass: usable in sets/dict keys for grouping by identity
    c = ProvChange("a", "ADD", "DOCUMENTATION", COLUMN_DOC, DS, "x", None, "d", 1.0, FIELD)
    assert c in {c}
