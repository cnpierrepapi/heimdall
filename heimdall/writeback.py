"""Write earned trust back into DataHub.

The ledger is only useful if its conclusions live where agents and people
already look. This layer publishes three artifacts into the catalog:

  tags        - a provenance tag on every dataset an agent authored,
                reflecting the author's current standing (skilled, unproven,
                harmful). Tags are entities in DataHub and must exist before
                they can be applied, so they are pre-created here.
  properties  - machine-readable structured properties on each authored
                dataset: author agent id, trust score, and verdict at the
                time of writing.
  dossiers    - one Document per agent holding the full settled record
                (verdict, calibration, recent claims), saved through the MCP
                save_document tool so any MCP-connected agent can search and
                read it. The first dossier saved also unlocks the document
                tools on the server for every other agent.

Descriptions themselves are applied here too: accepted enrichment claims are
replayed from the ledger into update_description. No model call is involved
anywhere in writeback; it is a pure ledger-to-catalog projection.
"""

from __future__ import annotations

from typing import Any, Optional

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    PropertyCardinalityClass,
    StructuredPropertiesClass,
    StructuredPropertyDefinitionClass,
    StructuredPropertySettingsClass,
    StructuredPropertyValueAssignmentClass,
    TagPropertiesClass,
)

from .claims import ENRICHMENT, Claim, ClaimStore
from .skill import HARMFUL, SKILLED, skill_report

TAG_SKILLED = "heimdall-skilled"
TAG_UNPROVEN = "heimdall-unproven"
TAG_HARMFUL = "heimdall-harmful"

_TAG_DESCRIPTIONS = {
    TAG_SKILLED: (
        "Author agent's settled record beats its luck baseline at controlled "
        "false discovery rate. Assigned by heimdall from settled claims."
    ),
    TAG_UNPROVEN: (
        "Author agent's settled record is not yet distinguishable from luck. "
        "Assigned by heimdall from settled claims."
    ),
    TAG_HARMFUL: (
        "Author agent's settled record is significantly worse than its luck "
        "baseline. Assigned by heimdall from settled claims."
    ),
}

PROP_AGENT = "urn:li:structuredProperty:io.heimdall.author_agent"
PROP_TRUST = "urn:li:structuredProperty:io.heimdall.author_trust"
PROP_VERDICT = "urn:li:structuredProperty:io.heimdall.author_verdict"

_PROP_DEFS = [
    (PROP_AGENT, "io.heimdall.author_agent", "Heimdall author agent",
     "urn:li:dataType:datahub.string",
     "Agent that last wrote metadata to this asset, as scored by heimdall."),
    (PROP_TRUST, "io.heimdall.author_trust", "Heimdall author trust",
     "urn:li:dataType:datahub.number",
     "Author agent's trust score (0-100, shrinkage-adjusted Brier) at write time."),
    (PROP_VERDICT, "io.heimdall.author_verdict", "Heimdall author verdict",
     "urn:li:dataType:datahub.string",
     "Author agent's skill-vs-luck verdict at write time."),
]


def tag_urn(name: str) -> str:
    return f"urn:li:tag:{name}"


def verdict_tag(verdict: str) -> str:
    if verdict == SKILLED:
        return TAG_SKILLED
    if verdict == HARMFUL:
        return TAG_HARMFUL
    return TAG_UNPROVEN


def ensure_tags(emitter: Any) -> list[str]:
    """Pre-create the provenance tags (tags are entities; apply fails otherwise)."""
    created = []
    for name, description in _TAG_DESCRIPTIONS.items():
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=tag_urn(name),
                aspect=TagPropertiesClass(name=name, description=description),
            )
        )
        created.append(tag_urn(name))
    return created


def define_trust_properties(emitter: Any) -> list[str]:
    """Register the structured property definitions used on authored datasets.

    Every property is surfaced in the asset sidebar summary; the verdict is
    additionally an asset badge and a search filter, so author trust is
    visible wherever the dataset appears in the catalog UI.
    """
    defined = []
    for urn, qualified, display, value_type, description in _PROP_DEFS:
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=StructuredPropertyDefinitionClass(
                    qualifiedName=qualified,
                    displayName=display,
                    valueType=value_type,
                    cardinality=PropertyCardinalityClass.SINGLE,
                    entityTypes=["urn:li:entityType:datahub.dataset"],
                    description=description,
                ),
            )
        )
        is_verdict = urn == PROP_VERDICT
        try:
            # the settings aspect updates search facets, which touches the search
            # index; a degraded index must not abort the definition (the property
            # still exists and is assignable, just less surfaced in the UI)
            emitter.emit(
                MetadataChangeProposalWrapper(
                    entityUrn=urn,
                    aspect=StructuredPropertySettingsClass(
                        isHidden=False,
                        showInAssetSummary=True,
                        showAsAssetBadge=is_verdict,
                        showInSearchFilters=is_verdict,
                    ),
                )
            )
        except Exception:
            pass
        defined.append(urn)
    return defined


def apply_accepted_enrichments(mcp: Any, store: ClaimStore) -> dict[str, str]:
    """Replay accepted enrichment claims into the catalog.

    Returns dataset urn -> authoring agent id for everything written.
    """
    authored: dict[str, str] = {}
    for claim in store.claims(claim_type=ENRICHMENT, settled=True):
        if not claim.correct:
            continue
        mcp.call(
            "update_description",
            {
                "entity_urn": claim.entity_urn,
                "column_path": claim.prediction["column"],
                "description": claim.prediction["description"],
                "operation": "replace",
            },
        )
        authored[claim.entity_urn] = claim.agent_id
    return authored


def annotate_authored_datasets(
    mcp: Any,
    emitter: Any,
    authored: dict[str, str],
    report: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Stamp provenance on every dataset an agent wrote to.

    The tag goes on through the MCP add_tags tool (the same door agents use);
    the structured properties go through the SDK, which is the only writer
    for that aspect today.
    """
    stamped: dict[str, str] = {}
    for dataset_urn, agent_id in sorted(authored.items()):
        rec = report.get(agent_id)
        if rec is None:
            continue
        tag = verdict_tag(rec["verdict"])
        mcp.call("add_tags", {"entity_urns": [dataset_urn], "tag_urns": [tag_urn(tag)]})
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=dataset_urn,
                aspect=StructuredPropertiesClass(
                    properties=[
                        StructuredPropertyValueAssignmentClass(
                            propertyUrn=PROP_AGENT, values=[agent_id]
                        ),
                        StructuredPropertyValueAssignmentClass(
                            propertyUrn=PROP_TRUST, values=[float(rec["trust"])]
                        ),
                        StructuredPropertyValueAssignmentClass(
                            propertyUrn=PROP_VERDICT, values=[rec["verdict"]]
                        ),
                    ]
                ),
            )
        )
        stamped[dataset_urn] = tag
    return stamped


def dossier_markdown(
    agent_id: str, rec: dict[str, Any], settled: list[Claim], max_claims: int = 8
) -> str:
    """Human-and-agent-readable trust dossier for one agent."""

    def fmt(value: Any, digits: int = 3) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float):
            return f"{value:.{digits}f}"
        return str(value)

    lines = [
        f"# Agent trust dossier: {agent_id}",
        "",
        f"Verdict: **{rec['verdict']}**. Trust score: {fmt(rec['trust'], 1)}/100.",
        "",
        "Scores are computed by heimdall from settled claims only: every claim",
        "was recorded with a confidence before its outcome existed, then settled",
        "against ground truth (assertion results, SLA outcomes, incident",
        "resolutions, steward reviews).",
        "",
        "| metric | value |",
        "|---|---|",
        f"| claims recorded | {rec['n_total']} |",
        f"| claims settled | {rec['n_settled']} |",
        f"| win rate | {fmt(rec['win_rate'])} |",
        f"| mean Brier (lower is better) | {fmt(rec['brier_mean'])} |",
        f"| expected calibration error | {fmt(rec.get('ece'))} |",
        f"| expected wins under luck baseline | {fmt(rec.get('expected_null_wins'))} |",
        f"| p-value vs luck | {fmt(rec.get('p_value'))} |",
        f"| q-value (FDR-adjusted) | {fmt(rec.get('q_value'))} |",
    ]

    calibration = rec.get("calibration") or []
    if calibration:
        lines += [
            "",
            "## Calibration",
            "",
            "| stated confidence | claims | observed accuracy |",
            "|---|---|---|",
        ]
        for b in calibration:
            lines.append(
                f"| {b['bin_low']:.1f} to {b['bin_high']:.1f} "
                f"| {b['n']} | {b['frac_true']:.2f} |"
            )

    recent = sorted(settled, key=lambda c: c.settled_ts or 0, reverse=True)
    if recent:
        lines += ["", "## Recent settled claims", ""]
        for c in recent[:max_claims]:
            mark = "RIGHT" if c.correct else "WRONG"
            lines.append(
                f"- {mark} ({c.claim_type}, p={c.confidence:.2f}) on {c.entity_urn}"
            )

    return "\n".join(lines)


def publish_dossiers(
    mcp: Any, store: ClaimStore, report: dict[str, dict[str, Any]]
) -> list[str]:
    """Save one Document per agent via MCP; returns the titles saved."""
    titles = []
    for agent_id in sorted(report):
        rec = report[agent_id]
        settled = store.claims(agent_id=agent_id, settled=True)
        title = f"Agent trust dossier: {agent_id}"
        content = dossier_markdown(agent_id, rec, settled)
        assets = sorted(
            {
                c.entity_urn
                for c in settled
                if c.entity_urn.startswith("urn:li:dataset:")
            }
        )[:10]
        args = {
            "document_type": "Analysis",
            "title": title,
            "content": content,
            "topics": ["heimdall", "agent trust"],
            "related_assets": assets,
        }
        try:
            mcp.call("save_document", args)
        except RuntimeError:
            # topics/assets can be rejected by stricter validators; the
            # dossier text is the part that must land
            mcp.call(
                "save_document",
                {"document_type": "Analysis", "title": title, "content": content},
            )
        titles.append(title)
    return titles


def writeback(
    mcp: Any,
    store: ClaimStore,
    emitter: Optional[Any] = None,
    gms_url: str = "http://localhost:8080",
    n_sims: int = 10_000,
) -> dict[str, Any]:
    """Full ledger-to-catalog projection. Returns a summary of what landed."""
    if emitter is None:
        emitter = DatahubRestEmitter(gms_url)
    report = skill_report(store, n_sims=n_sims)

    tags = ensure_tags(emitter)
    props = define_trust_properties(emitter)
    authored = apply_accepted_enrichments(mcp, store)
    stamped = annotate_authored_datasets(mcp, emitter, authored, report)
    dossiers = publish_dossiers(mcp, store, report)

    return {
        "tags_ensured": tags,
        "properties_defined": props,
        "descriptions_applied": len(authored),
        "datasets_stamped": stamped,
        "dossiers_published": dossiers,
        "report": report,
    }
