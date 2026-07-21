"""Write the observability verdict back into DataHub as an audit trail.

Heimdall's conclusions are only useful if they live where people and agents
already look. This projects three things into the catalog itself, sourced from
the observation events (who touched what), the grounded findings (what went
wrong), and the trust ledger (how reliable each agent is):

  provenance stamp - on every dataset an agent wrote to: the author agent, its
                     trust score, and its skill-vs-luck verdict, plus a
                     provenance tag (heimdall-skilled / -unproven / -harmful),
                     so the catalog itself says who last touched an asset and
                     whether to trust it.
  dossiers         - one Document per agent: its trust per work_kind, its
                     recent grounded findings, and its activity summary, saved
                     through save_document so any MCP agent can read it.

No model call is involved; this is a pure projection of the ledger and the
observation store into catalog metadata. It reuses the tag and structured
property definitions from writeback.py so the two paths cannot drift.
"""

from __future__ import annotations

from typing import Any, Optional

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    StructuredPropertiesClass,
    StructuredPropertyValueAssignmentClass,
)

from .grounding import FindingStore
from .observability import OK, WRITE, EventStore
from .simulator.steward import KIND_COLUMN_DOC
from .trust import parse_action, trust_report
from .claims import ClaimStore
from .writeback import (
    PROP_AGENT,
    PROP_TRUST,
    PROP_VERDICT,
    define_trust_properties,
    ensure_tags,
    tag_urn,
    verdict_tag,
)

# tool -> the work_kind whose trust stamps the asset it wrote to
_TOOL_KIND = {
    "update_description": KIND_COLUMN_DOC,
    "add_tags": "pii",
    "add_terms": "term",
    "add_owners": "owner",
    "set_domains": "domain",
}


def touched_assets(event_store: EventStore) -> dict[str, tuple[str, str]]:
    """dataset urn -> (author agent, work_kind), latest successful writer wins."""
    out: dict[str, tuple[str, str]] = {}
    for event in event_store.events(op=WRITE, status=OK):
        action = parse_action(event)
        if not action.entity_urn or not action.entity_urn.startswith("urn:li:dataset:"):
            continue
        if action.operation == "remove" or action.tool.startswith("remove_"):
            continue
        kind = _TOOL_KIND.get(action.tool)
        if kind is None:
            continue
        out[action.entity_urn] = (event.agent_id, kind)
    return out


def _standing(report: dict[str, dict[str, dict[str, Any]]], agent: str, kind: str) -> Optional[dict[str, Any]]:
    kinds = report.get(agent)
    if not kinds:
        return None
    return kinds.get(kind) or next(iter(kinds.values()), None)


def stamp_provenance(
    mcp: Any, emitter: Any, touched: dict[str, tuple[str, str]],
    report: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Tag and stamp each touched dataset with its author's standing."""
    stamped: dict[str, dict[str, Any]] = {}
    for dataset_urn, (agent_id, kind) in sorted(touched.items()):
        rec = _standing(report, agent_id, kind)
        if rec is None:
            continue
        tag = verdict_tag(rec["verdict"])
        mcp.call("add_tags", {"entity_urns": [dataset_urn], "tag_urns": [tag_urn(tag)]})
        emitter.emit(MetadataChangeProposalWrapper(
            entityUrn=dataset_urn,
            aspect=StructuredPropertiesClass(properties=[
                StructuredPropertyValueAssignmentClass(propertyUrn=PROP_AGENT, values=[agent_id]),
                StructuredPropertyValueAssignmentClass(propertyUrn=PROP_TRUST, values=[float(rec["trust"])]),
                StructuredPropertyValueAssignmentClass(propertyUrn=PROP_VERDICT, values=[rec["verdict"]]),
            ]),
        ))
        stamped[dataset_urn] = {"agent": agent_id, "kind": kind,
                                "trust": rec["trust"], "verdict": rec["verdict"], "tag": tag}
    return stamped


def audit_dossier_markdown(
    agent_id: str, kinds: dict[str, dict[str, Any]],
    findings: list, activity: dict[str, int],
) -> str:
    """A per-agent audit dossier: trust per kind, findings, activity."""
    lines = [
        f"# Agent audit dossier: {agent_id}",
        "",
        "Heimdall observed this agent working against the catalog through the "
        "gateway, grounded each action against catalog context, and settled the "
        "outcomes into a skill-vs-luck trust score. Nothing here is self-reported.",
        "",
        "## Trust by work kind",
        "",
        "| work kind | trust | verdict | settled |",
        "|---|---|---|---|",
    ]
    for kind, rec in sorted(kinds.items()):
        lines.append(f"| {kind} | {rec['trust']}/100 | {rec['verdict']} | {rec['n_settled']} |")

    if activity:
        lines += [
            "", "## Activity observed", "",
            f"- total calls: {activity.get('total', 0)}",
            f"- reads: {activity.get('reads', 0)}, writes: {activity.get('writes', 0)}",
            f"- errors: {activity.get('errors', 0)}, held: {activity.get('held', 0)}, "
            f"blocked: {activity.get('blocked', 0)}",
        ]

    harmful = [f for f in findings if f.severity == "harmful"]
    if harmful:
        lines += ["", "## Catalog-grounded findings", ""]
        for f in harmful[:8]:
            where = f.column or "(table)"
            lines.append(f"- **{f.check_type}** on {where}: {f.reason}")

    return "\n".join(lines)


def publish_dossiers(
    mcp: Any, report: dict[str, dict[str, dict[str, Any]]],
    finding_store: FindingStore, event_store: EventStore,
) -> list[str]:
    titles = []
    activity = event_store.summary()
    for agent_id, kinds in sorted(report.items()):
        findings = finding_store.findings(agent_id=agent_id)
        content = audit_dossier_markdown(agent_id, kinds, findings, activity.get(agent_id, {}))
        title = f"Agent audit dossier: {agent_id}"
        try:
            mcp.call("save_document", {"document_type": "Analysis", "title": title,
                                       "content": content, "topics": ["heimdall", "agent audit"]})
        except RuntimeError:
            mcp.call("save_document", {"document_type": "Analysis", "title": title, "content": content})
        titles.append(title)
    return titles


def audit_writeback(
    mcp: Any, event_store: EventStore, finding_store: FindingStore, trust_store: ClaimStore,
    emitter: Optional[Any] = None, gms_url: str = "http://localhost:8080", **kwargs: Any,
) -> dict[str, Any]:
    """Full observability-to-catalog projection. Returns what landed."""
    if emitter is None:
        emitter = DatahubRestEmitter(gms_url)
    report = trust_report(trust_store, **kwargs)

    tags = ensure_tags(emitter)
    props = define_trust_properties(emitter)
    touched = touched_assets(event_store)
    stamped = stamp_provenance(mcp, emitter, touched, report)
    dossiers = publish_dossiers(mcp, report, finding_store, event_store)

    return {
        "tags_ensured": tags,
        "properties_defined": props,
        "datasets_stamped": stamped,
        "dossiers_published": dossiers,
        "report": report,
    }
