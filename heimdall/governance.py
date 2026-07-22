"""Governance entity URNs, pre-creation, and the write dispatch.

An agent's claim becomes real catalog work by calling the matching MCP tool
through the gateway, where it is observed and grounded. Description writes stand
alone, but a PII tag write references a tag entity, so the tags an agent may use
are pre-created during ingest and referenced by a single shared URN helper. One
place owns the mapping, so what ingest creates and what a write references can
never drift apart.
"""

from __future__ import annotations

from typing import Any

# the PII vocabulary the taggers may use (aligned with the generator's truth)
PII_TAG_TYPES = ("email", "person_name", "phone", "address", "national_id")


def pii_tag_urn(pii_type: str) -> str:
    return f"urn:li:tag:pii-{pii_type.replace('_', '-')}"


def pii_tag_mcps() -> list[Any]:
    """MCPs pre-creating the PII tag entities a tagger may reference."""
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import TagPropertiesClass
    return [
        MetadataChangeProposalWrapper(
            entityUrn=pii_tag_urn(t),
            aspect=TagPropertiesClass(
                name=f"pii-{t.replace('_', '-')}",
                description=f"Column contains PII: {t}. Flagged by a heimdall pii tagger.",
            ),
        )
        for t in PII_TAG_TYPES
    ]


def apply_claim(mcp: Any, claim: Any) -> str:
    """Project one accepted claim into the catalog through the gateway.

    Returns a short label. Raises for an unknown kind so a new work kind cannot
    be silently dropped.
    """
    pred = claim.prediction
    kind = pred.get("kind")
    if kind in ("column_doc", "table_doc"):
        args = {"entity_urn": claim.entity_urn, "description": pred["description"],
                "operation": "replace"}
        if pred.get("column"):
            args["column_path"] = pred["column"]
        mcp.call("update_description", args)
        return f"describe {pred.get('column') or 'table'}"
    if kind == "pii":
        mcp.call("add_tags", {
            "tag_urns": [pii_tag_urn(pred["pii_type"])],
            "entity_urns": [claim.entity_urn],
            "column_paths": [pred["column"]],
        })
        return f"tag {pred['pii_type']} on {pred['column']}"
    raise ValueError(f"apply_claim: unsupported kind {kind!r}")
