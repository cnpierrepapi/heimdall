"""Shared helpers for the scaffolded agents.

Includes how an agent reads the state of a column, which decides what work is
left to do. DataHub keeps two descriptions per field and the MCP server surfaces
them under different keys: `description` is what the catalog itself shipped,
`editedDescription` is what somebody has since written. A column is documented if
either is set, and an agent that only checks one of them will keep redoing work
that is already done. Same for `editedTags`, which is where an applied PII tag
shows up. These shapes were confirmed against the live MCP server rather than
assumed; they are not in its schema.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

DATASET_URN_RE = re.compile(r"urn:li:dataset:\([^)]*\)")


def schema_fields(schema: Any) -> list[dict[str, Any]]:
    """The field list out of a list_schema_fields result, or empty."""
    if not isinstance(schema, dict):
        return []
    fields = schema.get("fields")
    return [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []


def field_description(field: dict[str, Any]) -> str:
    """Whatever documentation this column carries now, from either source."""
    for key in ("description", "editedDescription"):
        text = str(field.get(key) or "").strip()
        if text:
            return text
    return ""


def field_tags(field: dict[str, Any]) -> list[str]:
    """Tag names already applied to this column."""
    out: list[str] = []
    for key in ("tags", "editedTags"):
        value = field.get(key)
        if isinstance(value, list):
            out += [str(t) for t in value if t]
    return out


def field_has_pii_tag(field: dict[str, Any]) -> bool:
    return any("pii" in t.lower() for t in field_tags(field))


def extract_dataset_urns(payload: Any, exclude: Iterable[str] = ()) -> list[str]:
    """Pull dataset urns out of an MCP response, deduped in discovery order."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    skip = set(exclude)
    seen: set[str] = set()
    out: list[str] = []
    for urn in DATASET_URN_RE.findall(text):
        if urn in skip or urn in seen:
            continue
        seen.add(urn)
        out.append(urn)
    return out


def clamp_confidence(
    value: Any, lo: float, hi: float, default: float = 0.6
) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        conf = default
    return min(max(conf, lo), hi)


def as_text(payload: Any, limit: int = 2500) -> str:
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    return payload[:limit]
