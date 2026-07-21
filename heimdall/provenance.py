"""Read a catalog's own change history and normalize who wrote what, when.

This is the front door of the reconstruction engine. Instead of running our own
agents and recording claims as they act, we read the metadata changes that
already happened in a live DataHub and turn each one into a normalized record.
Downstream those records become settleable claims grouped by (actor, work kind),
so any writer a catalog already has -- DataHub's own AI documentation, a
third-party agent, a human steward -- can be scored on its real record.

The source is the DataHub Timeline API, a GMS REST endpoint (not an MCP tool)
available on open-source DataHub at ``/openapi/v2/timeline/v1/{urn}``. One call
per dataset returns a list of change transactions; each transaction is stamped
with the ``actor`` and ``timestamp`` and carries the field-level change events
underneath. The shapes parsed here were taken from live responses, not docs.

What the timeline can and cannot see: DOCUMENTATION (dataset and field level),
TAG, GLOSSARY_TERM, OWNER, and TECHNICAL_SCHEMA are tracked. Domain assignment
is not a timeline category, so domain claims cannot be reconstructed this way.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Work kinds a change can belong to. These line up with the prediction kinds the
# reference agents stamp, so a reconstructed change and an agent-recorded claim
# score against the same per-kind baseline. Identity is (actor, work_kind): one
# writer authoring several kinds decomposes into one scored profile per kind.
COLUMN_DOC = "column_doc"
TABLE_DOC = "table_doc"
TERM = "term"
OWNER = "owner"
TAG = "tag"
PII = "pii"
SCHEMA = "schema"  # structural evolution, not a metadata-authoring claim

# Categories worth fetching for scoring. TECHNICAL_SCHEMA is available too but is
# schema evolution rather than an authored-metadata claim, so it is off by
# default. DOMAIN is intentionally absent: DataHub does not expose it here.
DEFAULT_CATEGORIES = ("DOCUMENTATION", "TAG", "GLOSSARY_TERM", "OWNER")

_SCHEMA_FIELD_PREFIX = "urn:li:schemaField:("
_DATASET_PREFIX = "urn:li:dataset:"


@dataclass(frozen=True)
class ProvChange:
    """One normalized metadata change reconstructed from the timeline.

    ``dataset_urn`` is always the parent dataset (a field change is folded onto
    its dataset with ``field`` set). ``value`` holds the written text for
    documentation changes; ``target`` holds the tag/term/owner urn for the
    classification, glossary, and ownership kinds. ``ts`` is epoch seconds to
    match the ledger's clock.
    """

    actor: str
    operation: str  # ADD | MODIFY | REMOVE
    category: str
    work_kind: str
    dataset_urn: str
    field: Optional[str]
    target: Optional[str]
    value: Optional[str]
    ts: float
    raw_entity_urn: str

    @property
    def is_field(self) -> bool:
        return self.field is not None

    @property
    def is_clear(self) -> bool:
        """A change that removes or blanks the prior value (a revert shape)."""
        if self.operation == "REMOVE":
            return True
        return self.category == "DOCUMENTATION" and (self.value or "") == ""


def dataset_and_field(entity_urn: str) -> tuple[str, Optional[str]]:
    """Split a change target into (dataset urn, field name or None).

    Field changes arrive on a schemaField urn that wraps the dataset urn and the
    field path: ``urn:li:schemaField:(<dataset urn>,<field>)``. The dataset urn
    itself contains commas and parentheses, so the top-level comma is found by
    tracking parenthesis depth rather than a naive split.
    """
    if entity_urn.startswith(_SCHEMA_FIELD_PREFIX):
        inner = entity_urn[len(_SCHEMA_FIELD_PREFIX) : -1]  # drop wrapper + ')'
        depth = 0
        for i, ch in enumerate(inner):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                return inner[:i], inner[i + 1 :]
        return inner, None
    return entity_urn, None


def _work_kind(category: str, is_field: bool, target: Optional[str]) -> str:
    if category == "DOCUMENTATION":
        return COLUMN_DOC if is_field else TABLE_DOC
    if category == "GLOSSARY_TERM":
        return TERM
    if category == "OWNER":
        return OWNER
    if category == "TAG":
        if target and "pii" in target.lower():
            return PII
        return TAG
    if category == "TECHNICAL_SCHEMA":
        return SCHEMA
    return category.lower()


def _target_urn(category: str, event: dict[str, Any], params: dict[str, Any]) -> Optional[str]:
    if category == "TAG":
        return params.get("tagUrn") or event.get("modifier")
    if category == "GLOSSARY_TERM":
        return params.get("termUrn") or event.get("modifier")
    if category == "OWNER":
        return params.get("ownerUrn") or event.get("modifier")
    return None


def parse_transaction(txn: dict[str, Any]) -> list[ProvChange]:
    """Turn one timeline transaction into normalized changes.

    The actor and timestamp are shared by every change event in the transaction.
    """
    actor = txn.get("actor") or ""
    ts = float(txn.get("timestamp", 0)) / 1000.0
    out: list[ProvChange] = []
    for event in txn.get("changeEvents", []) or []:
        entity_urn = event.get("entityUrn", "")
        category = event.get("category", "")
        operation = event.get("operation", "")
        params = event.get("parameters") or {}
        dataset_urn, field = dataset_and_field(entity_urn)
        target = _target_urn(category, event, params)
        kind = _work_kind(category, field is not None, target)
        value = params.get("description") if category == "DOCUMENTATION" else None
        out.append(
            ProvChange(
                actor=actor,
                operation=operation,
                category=category,
                work_kind=kind,
                dataset_urn=dataset_urn,
                field=field,
                target=target,
                value=value,
                ts=ts,
                raw_entity_urn=entity_urn,
            )
        )
    return out


def parse_timeline(transactions: Iterable[dict[str, Any]]) -> list[ProvChange]:
    """Flatten a timeline response (list of transactions) into changes."""
    changes: list[ProvChange] = []
    for txn in transactions:
        changes.extend(parse_transaction(txn))
    return changes


def fetch_timeline(
    gms_url: str,
    urn: str,
    categories: Iterable[str] = DEFAULT_CATEGORIES,
    start_ms: int = 1,
    end_ms: int = 99_999_999_999_999,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch the raw timeline transactions for one entity from GMS.

    The entity urn is percent-encoded into the path (it contains parentheses and
    commas); categories repeat as query params. GMS on the demo box needs no
    token, but one is sent as a bearer header when provided.
    """
    enc = urllib.parse.quote(urn, safe="")
    query = urllib.parse.urlencode(
        [("startTime", start_ms), ("endTime", end_ms), ("raw", "false")]
        + [("categories", c) for c in categories]
    )
    url = f"{gms_url.rstrip('/')}/openapi/v2/timeline/v1/{enc}?{query}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data if isinstance(data, list) else []


def read_changes(
    gms_url: str,
    urns: Iterable[str],
    categories: Iterable[str] = DEFAULT_CATEGORIES,
    token: Optional[str] = None,
    include_schema: bool = False,
) -> list[ProvChange]:
    """Read and normalize the change history for a set of datasets.

    Returns changes sorted by time (then dataset, then field) so the sequence of
    edits to a given field reads in order, which is what the settlement layer
    needs to tell an accepted write from a later reverted one.
    """
    changes: list[ProvChange] = []
    for urn in urns:
        changes.extend(parse_timeline(fetch_timeline(gms_url, urn, categories, token=token)))
    if not include_schema:
        changes = [c for c in changes if c.work_kind != SCHEMA]
    changes.sort(key=lambda c: (c.ts, c.dataset_urn, c.field or "", c.raw_entity_urn))
    return changes


def lineworld_dataset_urns() -> list[str]:
    """The demo catalog's dataset urns, for reconstructing the box's history."""
    from .simulator.world import build_default_world

    return [d.urn for d in build_default_world().datasets.values()]
