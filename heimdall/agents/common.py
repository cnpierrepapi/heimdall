"""Shared helpers for the scaffolded agents."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

DATASET_URN_RE = re.compile(r"urn:li:dataset:\([^)]*\)")


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
