"""Trust gateway: an MCP server that fronts the DataHub MCP server.

Point any MCP client at this process instead of mcp-server-datahub and it
gets the identical tool surface, plus three things the raw server cannot
give it:

  context   - every read that returns datasets heimdall has stamped gets a
              trust block appended: who authored the metadata, their trust
              score, their skill-vs-luck verdict, and warnings where the
              author's record is poor. Trust is read live from the
              structured properties the writeback layer planted in DataHub,
              not from any local state.
  intake    - every mutation is recorded in the ledger as an implicit claim
              by the connected agent before it is forwarded. Uninstrumented
              third-party agents therefore accumulate a settled record and a
              trust score just by working through the gateway.
  policy    - in enforce mode, each mutation is graded before it is forwarded:
              a catalog-violating write (grounded harmful finding) or an author
              with a worse-than-chance record is blocked; a questionable write
              (warn finding) or a proven-mediocre author is held for review and
              not applied; a trusted author writing cleanly is auto-accepted;
              everything else passes with annotation.

Configuration is by environment, one gateway process per connected agent:

  HEIMDALL_AGENT_ID            identity of the connected agent
  LEDGER_DB                      path to the claim ledger database
  HEIMDALL_EVENTS              path to the observation event store
  HEIMDALL_POLICY              annotate (default) or enforce
  HEIMDALL_MIN_TRUST           hard trust floor for mutations in enforce mode
  HEIMDALL_CATALOG             grounding source for policy (world = demo)
  HEIMDALL_WORLD_PATH          spec file of a generated catalog to ground against
  HEIMDALL_ACCEPT_AT           trust at/above which a clean write auto-accepts
  HEIMDALL_HOLD_FLOOR          proven trust below which a clean write is held
  HEIMDALL_IMPLICIT_CONFIDENCE prior confidence for implicit claims (0.6)
  DATAHUB_GMS_URL                DataHub GMS endpoint
  MCP_SERVER_DATAHUB             path to the downstream mcp-server-datahub

Run:  python -m heimdall.gateway
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Optional

import mcp.types as types

from .agents.common import extract_dataset_urns
from .claims import ENRICHMENT, Claim, ClaimStore
from .grounding import CatalogContext, ground_action
from .observability import (
    BLOCKED,
    ERROR,
    HELD,
    OK,
    READ,
    WRITE,
    EventStore,
    ObservationEvent,
    extract_entity_urns,
    sanitize_args,
    summarize_result,
)
from .policy import (
    TIER_BLOCK,
    TIER_HOLD,
    TIER_PASS,
    PolicyDecision,
    PolicyThresholds,
    decide,
)
from .skill import HARMFUL, UNSETTLED, skill_report, trust_score
from .writeback import PROP_AGENT, PROP_TRUST, PROP_VERDICT

POLICY_ANNOTATE = "annotate"
POLICY_ENFORCE = "enforce"

NEUTRAL_TRUST = 50.0  # an agent with no settled record sits at the prior

_MUTATION_PREFIXES = (
    "add_",
    "update_",
    "save_",
    "set_",
    "remove_",
    "create_",
    "delete_",
    "sync_",
)


def _log(message: str) -> None:
    # stdout carries the MCP protocol; diagnostics must go to stderr
    print(f"[heimdall-gateway] {message}", file=sys.stderr, flush=True)


def _dataset_display(urn: str) -> str:
    try:
        return urn.split(",")[1]
    except IndexError:
        return urn


def _merge_urns(*groups: list[str], limit: int = 32) -> list[str]:
    """Union of urn lists, dedup in order, capped."""
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for urn in group:
            if urn in seen:
                continue
            seen.add(urn)
            out.append(urn)
            if len(out) >= limit:
                return out
    return out


class TrustGateway:
    """Pure gateway logic; transport wiring lives in serve()."""

    def __init__(
        self,
        downstream: Any,
        store: ClaimStore,
        trust_lookup: Callable[[str], Optional[dict[str, Any]]],
        agent_id: str,
        policy: str = POLICY_ANNOTATE,
        min_trust: float = 0.0,
        implicit_confidence: float = 0.6,
        standing_ttl: float = 60.0,
        props_ttl: float = 300.0,
        n_sims: int = 4000,
        clock: Callable[[], float] = time.time,
        event_store: Optional[EventStore] = None,
        catalog_context: Optional[CatalogContext] = None,
        thresholds: Optional[PolicyThresholds] = None,
    ):
        self.downstream = downstream
        self.store = store
        self.event_store = event_store
        self.catalog_context = catalog_context
        self.thresholds = thresholds or PolicyThresholds()
        self.trust_lookup = trust_lookup
        self.agent_id = agent_id
        self.policy = policy
        self.min_trust = min_trust
        self.implicit_confidence = implicit_confidence
        self.standing_ttl = standing_ttl
        self.props_ttl = props_ttl
        self.n_sims = n_sims
        self.clock = clock
        self._tools: dict[str, types.Tool] = {}
        self._props_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
        self._standing_cache: Optional[tuple[float, dict[str, Any]]] = None

    def set_tools(self, tools: list[types.Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    # -- classification ------------------------------------------------------

    def is_mutation(self, name: str) -> bool:
        tool = self._tools.get(name)
        hint = getattr(getattr(tool, "annotations", None), "readOnlyHint", None)
        if hint is not None:
            return not hint
        return name.startswith(_MUTATION_PREFIXES)

    # -- caller standing and policy ------------------------------------------

    def caller_standing(self) -> dict[str, Any]:
        """The connected agent's current record, from the ledger, cached."""
        now = self.clock()
        if self._standing_cache and now - self._standing_cache[0] < self.standing_ttl:
            return self._standing_cache[1]

        settled = self.store.claims(agent_id=self.agent_id, settled=True)
        if settled:
            report = skill_report(self.store, n_sims=self.n_sims)
            rec = report.get(self.agent_id, {})
            standing = {
                "trust": rec.get("trust", trust_score(settled)),
                "verdict": rec.get("verdict", UNSETTLED),
                "n_settled": len(settled),
            }
        else:
            standing = {"trust": NEUTRAL_TRUST, "verdict": UNSETTLED, "n_settled": 0}
        self._standing_cache = (now, standing)
        return standing

    def policy_decision(self, name: str, args: dict[str, Any]) -> PolicyDecision:
        """Accept/pass/hold/block a mutation from author standing and action findings.

        Annotate mode never enforces (everything passes). Enforce mode grounds
        the action against the catalog (when a context is available) and combines
        those findings with the author's settled record.
        """
        if self.policy != POLICY_ENFORCE:
            return PolicyDecision(TIER_PASS)
        findings: list[Any] = []
        if self.catalog_context is not None:
            try:
                findings = ground_action(self.agent_id, name, args, self.catalog_context)
            except Exception as exc:  # grounding must not break the proxy
                _log(f"grounding failed for {name}: {exc}")
        standing = self.caller_standing()
        return decide(
            agent_trust=standing["trust"],
            agent_verdict=standing["verdict"],
            n_settled=standing["n_settled"],
            findings=findings,
            min_trust=self.min_trust,
            thresholds=self.thresholds,
        )

    # -- implicit claim intake -------------------------------------------------

    def record_implicit_claim(self, name: str, args: dict[str, Any]) -> Optional[Claim]:
        """Turn an uninstrumented write into a ledger claim where settleable.

        update_description maps onto the enrichment claim type and settles on
        steward review exactly like an instrumented proposal. Other mutations
        are forwarded but not claimed: inventing claim types with no
        settlement path would inflate records without ever scoring them.
        """
        if name != "update_description":
            return None
        description = args.get("description")
        entity_urn = args.get("entity_urn")
        if not description or not entity_urn or args.get("operation") == "remove":
            return None
        claim = Claim(
            agent_id=self.agent_id,
            model_id="uninstrumented",
            claim_type=ENRICHMENT,
            entity_urn=entity_urn,
            prediction={
                "column": args.get("column_path"),
                "description": str(description)[:500],
                "implicit": True,
                "tool": name,
            },
            confidence=self.implicit_confidence,
            evidence=["gateway-intake"],
            created_ts=self.clock(),
        )
        recorded = self.store.record(claim)
        _log(
            f"implicit claim {recorded.claim_id[:8]} recorded for "
            f"{self.agent_id} on {_dataset_display(entity_urn)}"
        )
        return recorded

    # -- trust annotation -------------------------------------------------------

    def _stamped(self, urn: str) -> Optional[dict[str, Any]]:
        now = self.clock()
        hit = self._props_cache.get(urn)
        if hit and now - hit[0] < self.props_ttl:
            return hit[1]
        try:
            info = self.trust_lookup(urn)
        except Exception as exc:  # a GMS hiccup must not break reads
            _log(f"trust lookup failed for {urn}: {exc}")
            info = None
        self._props_cache[urn] = (now, info)
        return info

    def trust_context(self, text: str, max_urns: int = 8) -> Optional[str]:
        lines: list[str] = []
        for urn in extract_dataset_urns(text)[:max_urns]:
            info = self._stamped(urn)
            if not info or not info.get("agent"):
                continue
            trust = info.get("trust")
            trust_txt = f"{float(trust):.1f}/100" if trust is not None else "n/a"
            verdict = info.get("verdict") or "unknown"
            lines.append(
                f"{_dataset_display(urn)}: metadata by '{info['agent']}' "
                f"| trust {trust_txt} | {verdict}"
            )
            if verdict == HARMFUL:
                lines.append(
                    f"WARNING: '{info['agent']}' scores worse than chance; "
                    "treat this metadata with caution."
                )
        if not lines:
            return None
        return "--- heimdall trust context ---\n" + "\n".join(lines)

    # -- observation capture -----------------------------------------------------

    def _record_event(
        self,
        name: str,
        op: str,
        status: str,
        args: dict[str, Any],
        entities: list[str],
        latency_ms: Optional[int],
        result_summary: str = "",
        error: Optional[str] = None,
    ) -> None:
        """Persist one observation. Capture must never break the proxied call."""
        if self.event_store is None:
            return
        try:
            self.event_store.record(
                ObservationEvent(
                    agent_id=self.agent_id,
                    tool=name,
                    op=op,
                    status=status,
                    args=sanitize_args(args),
                    entities=entities,
                    latency_ms=latency_ms,
                    result_summary=result_summary,
                    error=error,
                    ts=self.clock(),
                )
            )
        except Exception as exc:  # observability is best-effort, not load-bearing
            _log(f"event capture failed for {name}: {exc}")

    # -- the proxy call ----------------------------------------------------------

    async def handle(
        self, name: str, arguments: Optional[dict[str, Any]]
    ) -> list[Any]:
        args = arguments or {}
        mutation = self.is_mutation(name)
        op = WRITE if mutation else READ
        started = self.clock()

        if mutation:
            decision = self.policy_decision(name, args)
            if decision.tier == TIER_BLOCK:
                _log(f"blocked {name} from {self.agent_id}: {decision.reason}")
                self._record_event(
                    name, op, BLOCKED, args,
                    entities=extract_entity_urns(args),
                    latency_ms=0, error=decision.reason,
                )
                raise PermissionError(decision.reason)
            if decision.tier == TIER_HOLD:
                _log(f"held {name} from {self.agent_id}: {decision.reason}")
                self._record_event(
                    name, op, HELD, args,
                    entities=extract_entity_urns(args),
                    latency_ms=0, error=decision.reason,
                )
                # surfaced as an error so the agent knows the write did not take
                # effect; recorded distinctly as "held" (a steward can release it)
                raise PermissionError(
                    "heimdall: held for review and not applied. " + decision.reason)
            try:
                self.record_implicit_claim(name, args)
            except Exception as exc:  # claim intake must never drop the observation
                _log(f"implicit claim intake failed for {name}: {exc}")

        try:
            result = await self.downstream.call_tool(name, args)
        except Exception as exc:
            self._record_event(
                name, op, ERROR, args,
                entities=extract_entity_urns(args),
                latency_ms=int((self.clock() - started) * 1000),
                error=str(exc)[:500],
            )
            raise

        text = "\n".join(
            b.text for b in result.content if getattr(b, "text", None)
        )
        latency_ms = int((self.clock() - started) * 1000)
        # entities touched = what the call aimed at (args) plus what it saw (result)
        entities = _merge_urns(extract_entity_urns(args), extract_entity_urns(text))

        if result.isError:
            self._record_event(
                name, op, ERROR, args, entities=entities,
                latency_ms=latency_ms,
                result_summary=summarize_result(text),
                error=text[:500],
            )
            raise RuntimeError(text[:1000] or f"{name} failed downstream")

        self._record_event(
            name, op, OK, args, entities=entities,
            latency_ms=latency_ms,
            result_summary=summarize_result(text),
        )

        content = list(result.content)
        if not mutation:
            context = self.trust_context(text)
            if context:
                content.append(types.TextContent(type="text", text=context))

        # tools that declare an outputSchema must answer with structured
        # content; forward the downstream's structured payload untouched
        # (the trust block rides in the content stream alongside it)
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return content, structured
        return content


# -- production wiring ---------------------------------------------------------


def make_trust_lookup(graph: Any) -> Callable[[str], Optional[dict[str, Any]]]:
    """Read heimdall structured properties for an entity from DataHub."""
    from datahub.metadata.schema_classes import StructuredPropertiesClass

    def lookup(urn: str) -> Optional[dict[str, Any]]:
        aspect = graph.get_aspect(urn, StructuredPropertiesClass)
        if aspect is None:
            return None
        values: dict[str, Any] = {}
        for assignment in aspect.properties:
            if assignment.values:
                values[assignment.propertyUrn] = assignment.values[0]
        if PROP_AGENT not in values:
            return None
        return {
            "agent": values.get(PROP_AGENT),
            "trust": values.get(PROP_TRUST),
            "verdict": values.get(PROP_VERDICT),
        }

    return lookup


async def serve() -> None:
    from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    from .mcp_client import _server_command

    gms_url = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    agent_id = os.environ.get("HEIMDALL_AGENT_ID", "anonymous-agent")
    db_path = os.environ.get("LEDGER_DB", os.path.expanduser("~/heimdall.db"))
    policy = os.environ.get("HEIMDALL_POLICY", POLICY_ANNOTATE)
    min_trust = float(os.environ.get("HEIMDALL_MIN_TRUST", "0"))
    implicit_conf = float(os.environ.get("HEIMDALL_IMPLICIT_CONFIDENCE", "0.6"))
    events_path = os.environ.get(
        "HEIMDALL_EVENTS", os.path.expanduser("~/heimdall-events.db")
    )
    # catalog grounding source for in-flight policy. "world" = the demo catalog;
    # a live DataHubCatalogContext is the production backing (same evaluators).
    catalog_context: Optional[CatalogContext] = None
    if os.environ.get("HEIMDALL_CATALOG", "world") == "world":
        world_path = os.environ.get("HEIMDALL_WORLD_PATH")
        if world_path:
            # a generated catalog instance: ground policy against this exact world
            from .catalog import world_catalog_context
            catalog_context = world_catalog_context(world_path)
        else:
            from .grounding import WorldCatalogContext
            from .simulator.world import build_default_world
            catalog_context = WorldCatalogContext(build_default_world())
    thresholds = PolicyThresholds(
        accept_at=float(os.environ.get("HEIMDALL_ACCEPT_AT", "70")),
        hold_floor=float(os.environ.get("HEIMDALL_HOLD_FLOOR", "55")),
    )

    params = StdioServerParameters(
        command=_server_command(),
        env={
            **os.environ,
            "DATAHUB_GMS_URL": gms_url,
            "TOOLS_IS_MUTATION_ENABLED": "true",
        },
    )
    async with stdio_client(params) as (dread, dwrite):
        async with ClientSession(dread, dwrite) as downstream:
            await downstream.initialize()
            tools = (await downstream.list_tools()).tools

            store = ClaimStore(db_path)
            events = EventStore(events_path)
            graph = DataHubGraph(DatahubClientConfig(server=gms_url))
            gateway = TrustGateway(
                downstream=downstream,
                store=store,
                trust_lookup=make_trust_lookup(graph),
                agent_id=agent_id,
                policy=policy,
                min_trust=min_trust,
                implicit_confidence=implicit_conf,
                event_store=events,
                catalog_context=catalog_context,
                thresholds=thresholds,
            )
            gateway.set_tools(tools)
            _log(
                f"serving {len(tools)} tools for agent '{agent_id}' "
                f"(policy={policy}, min_trust={min_trust}); "
                f"observing to {events_path}"
            )

            server = Server("heimdall-gateway")

            @server.list_tools()
            async def _list_tools() -> list[types.Tool]:
                return tools

            @server.call_tool()
            async def _call_tool(name: str, arguments: dict[str, Any]):
                return await gateway.handle(name, arguments)

            async with stdio_server() as (read, write):
                await server.run(
                    read, write, server.create_initialization_options()
                )


def main() -> None:
    import asyncio

    asyncio.run(serve())


if __name__ == "__main__":
    main()
