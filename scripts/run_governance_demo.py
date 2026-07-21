"""Run the governance batch live: five proposal agents against real DataHub.

Table describer, PII tagger, owner recommender, domain assigner, and term
mapper each work every lineworld dataset through the MCP server, record
their proposals as claims, and settle against the steward whose ground rules
derive from the world model. Accepted artifacts are then written back into
the catalog through the MCP write tools (descriptions, tags, owners,
domains, glossary terms), and the result is verified from a fresh session.

The owning groups, domains, glossary terms, and PII tags are entities in
DataHub and must exist before they can be referenced (the add_tags lesson,
gotcha G-03), so the script pre-creates them via the SDK emitter first.

Run on the box:
  OPENROUTER_API_KEY=... LEDGER_DB=~/fresh-e2e/ledger.db \
  MCP_SERVER_DATAHUB=~/dh/bin/mcp-server-datahub \
  ~/fresh-e2e/v/bin/python scripts/run_governance_demo.py
"""

from __future__ import annotations

import os
import time

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    CorpGroupInfoClass,
    DomainPropertiesClass,
    GlossaryTermInfoClass,
    TagPropertiesClass,
)

from heimdall.agents import (
    DomainAssignerAgent,
    NaiveGovernanceAgent,
    OwnerRecommenderAgent,
    PiiTaggerAgent,
    TableDescriberAgent,
    TermMapperAgent,
)
from heimdall.claims import ENRICHMENT, ClaimStore
from heimdall.llm import LLMClient, LLMError
from heimdall.mcp_client import DataHubMCP
from heimdall.settle import (
    STEWARD_REVIEW,
    VERDICT_ACCEPTED,
    VERDICT_REVERTED,
    GroundTruthEvent,
    SettlementEngine,
)
from heimdall.simulator import steward
from heimdall.simulator.world import build_default_world
from heimdall.skill import pooled_enrichment_acceptance, skill_report

DB_PATH = os.environ.get("LEDGER_DB", os.path.expanduser("~/heimdall-demo.db"))
GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")

TERM_IDS = {
    "Gross Order Value": "GrossOrderValue",
    "Recognized Revenue": "RecognizedRevenue",
    "Settled Payment Amount": "SettledPaymentAmount",
    "Customer Country": "CustomerCountry",
    "Active Customers": "ActiveCustomers",
}

group_urn = lambda team: f"urn:li:corpGroup:{team}"
domain_urn = lambda name: f"urn:li:domain:{name.lower()}"
term_urn = lambda name: f"urn:li:glossaryTerm:{TERM_IDS[name]}"
pii_tag_urn = lambda pii_type: f"urn:li:tag:pii-{pii_type.replace('_', '-')}"


def precreate_entities(world) -> None:
    """Groups, domains, terms, and PII tags referenced by writeback."""
    emitter = DatahubRestEmitter(GMS_URL)
    for team in world.teams():
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=group_urn(team),
                aspect=CorpGroupInfoClass(
                    displayName=team, admins=[], members=[], groups=[]
                ),
            )
        )
    for name in world.domain_names():
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=domain_urn(name),
                aspect=DomainPropertiesClass(
                    name=name,
                    description=f"{name} business domain (lineworld demo).",
                ),
            )
        )
    for name in world.term_names():
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=term_urn(name),
                aspect=GlossaryTermInfoClass(
                    name=name,
                    definition=f"Business glossary term: {name} (lineworld demo).",
                    termSource="INTERNAL",
                ),
            )
        )
    for pii_type in ("email", "person_name", "phone", "address", "national_id"):
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=pii_tag_urn(pii_type),
                aspect=TagPropertiesClass(
                    name=f"pii-{pii_type.replace('_', '-')}",
                    description=f"Column contains PII: {pii_type}. Flagged by heimdall pii-tagger.",
                ),
            )
        )
    print(
        f"pre-created {len(world.teams())} groups, {len(world.domain_names())} domains, "
        f"{len(world.term_names())} terms, 5 pii tags"
    )


def writeback_accepted(mcp: DataHubMCP, claim) -> str:
    """Project one accepted governance claim into the catalog. Returns a label."""
    pred = claim.prediction
    kind = pred.get("kind")
    if kind == "table_doc":
        mcp.call(
            "update_description",
            {
                "entity_urn": claim.entity_urn,
                "description": pred["description"],
                "operation": "replace",
            },
        )
        return "description"
    if kind == "pii":
        mcp.call(
            "add_tags",
            {
                "tag_urns": [pii_tag_urn(pred["pii_type"])],
                "entity_urns": [claim.entity_urn],
                "column_paths": [pred["column"]],
            },
        )
        return f"tag {pred['pii_type']} on {pred['column']}"
    if kind == "owner":
        mcp.call(
            "add_owners",
            {
                "owner_urns": [group_urn(pred["owner"])],
                "entity_urns": [claim.entity_urn],
                "ownership_type": "__system__technical_owner",
            },
        )
        return f"owner {pred['owner']}"
    if kind == "domain":
        mcp.call(
            "set_domains",
            {
                "domain_urn": domain_urn(pred["domain"]),
                "entity_urns": [claim.entity_urn],
            },
        )
        return f"domain {pred['domain']}"
    if kind == "term":
        mcp.call(
            "add_terms",
            {
                "term_urns": [term_urn(pred["term"])],
                "entity_urns": [claim.entity_urn],
                "column_paths": [pred["column"]],
            },
        )
        return f"term {pred['term']} on {pred['column']}"
    raise ValueError(f"unexpected kind: {kind}")


def main() -> None:
    world = build_default_world()
    llm = LLMClient()
    teams = world.teams()
    domains = world.domain_names()
    terms = world.term_names()
    datasets = sorted(world.datasets.values(), key=lambda d: d.name)

    precreate_entities(world)

    with ClaimStore(DB_PATH) as store, DataHubMCP() as mcp:
        engine = SettlementEngine(store)
        agents = [
            (TableDescriberAgent(mcp, llm, "table-describer-live"), "propose", ()),
            (PiiTaggerAgent(mcp, llm, "pii-tagger-live"), "propose", ()),
            (OwnerRecommenderAgent(mcp, llm, "owner-recommender-live"), "propose", (teams,)),
            (DomainAssignerAgent(mcp, llm, "domain-assigner-live"), "propose", (domains,)),
            (TermMapperAgent(mcp, llm, "term-mapper-live"), "propose", (terms,)),
            # the heuristic rival that keeps the acceptance pool honest (G-19)
            (
                NaiveGovernanceAgent(mcp, "naive-governance-live"),
                "propose_all",
                (teams, domains, terms),
            ),
        ]

        # ---- propose + settle, dataset by dataset --------------------------
        # Resume-safe: an (agent, dataset) pair that already holds claims in
        # the ledger is skipped, so a crashed run can be re-run without
        # double-counting proposals in the pool.
        new_claims = []
        for d in datasets:
            print(f"\n=== {d.name} ===")
            for agent, method, extra in agents:
                if store.claims(agent_id=agent.agent_id, entity_urn=d.urn):
                    print(f"  skip  {agent.agent_id:22s} (already claimed)")
                    continue
                try:
                    claims = getattr(agent, method)(d.urn, *extra)
                except LLMError as e:
                    print(f"  SKIP  {agent.agent_id:22s} llm error: {str(e)[:60]}")
                    continue
                for claim in claims:
                    store.record(claim)
                    accepted = steward.evaluate(world, claim)
                    engine.process_event(
                        GroundTruthEvent(
                            event_type=STEWARD_REVIEW,
                            entity_urn=claim.entity_urn,
                            payload={
                                "column": claim.prediction.get("column"),
                                "verdict": VERDICT_ACCEPTED
                                if accepted
                                else VERDICT_REVERTED,
                                "claim_id": claim.claim_id,
                            },
                            ts=time.time() + 1,
                        )
                    )
                    settled = store.get(claim.claim_id)
                    mark = "RIGHT" if settled.correct else "WRONG"
                    pred = claim.prediction
                    what = (
                        pred.get("description", "")[:40]
                        or pred.get("pii_type", "")
                        and f"pii:{pred.get('pii_type')} {pred.get('column')}"
                        or pred.get("owner", "")
                        or pred.get("domain", "")
                        or f"{pred.get('term')} <- {pred.get('column')}"
                    )
                    print(
                        f"  {mark:5s} {agent.agent_id:22s} p={claim.confidence:.2f} "
                        f"[{pred.get('kind')}] {what}"
                    )
                    new_claims.append(settled)

        # ---- pool + verdicts ----------------------------------------------
        pooled = pooled_enrichment_acceptance(store)
        print(f"\npooled proposal-acceptance rate (the luck baseline): {pooled:.3f}")
        report = skill_report(store)
        print("\n=== fleet verdicts (all agents on this ledger) ===")
        for agent_id in sorted(report):
            r = report[agent_id]
            print(
                f"  {agent_id:24s} trust={r['trust']:5.1f} "
                f"settled={r['n_settled']:3d} verdict={r['verdict']}"
            )

        # ---- writeback of accepted artifacts ------------------------------
        # All accepted governance claims in the ledger, not just this run's:
        # a crashed run settles claims without reaching writeback, and every
        # write below is an idempotent upsert, so replaying is safe.
        print("\n=== writeback: accepted artifacts into the catalog ===")
        kinds = {"table_doc", "pii", "owner", "domain", "term"}
        applied = 0
        for claim in store.claims(claim_type=ENRICHMENT, settled=True):
            if claim.correct and claim.prediction.get("kind") in kinds:
                label = writeback_accepted(mcp, claim)
                ds = world.by_urn(claim.entity_urn).name
                print(f"  wrote {label} -> {ds}")
                applied += 1
        print(f"applied {applied} accepted artifacts")

    # ---- consumer-seat verification (fresh MCP session) --------------------
    print("\n=== verify from a fresh session ===")
    with DataHubMCP() as fresh:
        for name in ("raw_customers", "fct_revenue"):
            urn = world.datasets[name].urn
            got = str(fresh.get_entities([urn]))
            checks = {
                "owner group": "corpGroup" in got,
                "domain": "urn:li:domain:" in got,
            }
            if name == "raw_customers":
                checks["pii tag"] = "pii-email" in got
            if name == "fct_revenue":
                checks["glossary term"] = "SettledPaymentAmount" in got or "RecognizedRevenue" in got
            for label, ok in checks.items():
                print(f"  {'PASS' if ok else 'FAIL'}  {name}: {label}")
            if not all(checks.values()):
                print(f"  ...response snippet: {got[:600]}")
    print("\ndone")


if __name__ == "__main__":
    main()
