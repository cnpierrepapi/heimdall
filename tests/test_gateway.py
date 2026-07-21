"""Trust gateway logic: classification, intake, policy, annotation."""

from __future__ import annotations

import asyncio

import mcp.types as types
import pytest

from heimdall.claims import ClaimStore
from heimdall.gateway import POLICY_ENFORCE, TrustGateway
from heimdall.observability import BLOCKED, ERROR, OK, READ, WRITE, EventStore
from heimdall.skill import HARMFUL

URN_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
URN_B = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)"


class FakeResult:
    def __init__(self, text: str, is_error: bool = False, structured=None):
        self.content = [types.TextContent(type="text", text=text)]
        self.isError = is_error
        self.structuredContent = structured


class FakeDownstream:
    def __init__(self, result: FakeResult):
        self.result = result
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.result


def make_tool(name: str, read_only=None) -> types.Tool:
    annotations = (
        types.ToolAnnotations(readOnlyHint=read_only) if read_only is not None else None
    )
    return types.Tool(name=name, inputSchema={"type": "object"}, annotations=annotations)


def gw(
    tmp_path,
    downstream=None,
    trust_lookup=lambda urn: None,
    policy="annotate",
    min_trust=0.0,
    agent_id="agent-x",
):
    store = ClaimStore(str(tmp_path / "ledger.db"))
    gateway = TrustGateway(
        downstream=downstream or FakeDownstream(FakeResult("{}")),
        store=store,
        trust_lookup=trust_lookup,
        agent_id=agent_id,
        policy=policy,
        min_trust=min_trust,
    )
    return gateway, store


def test_mutation_classification_honors_hints_then_prefixes(tmp_path):
    gateway, _ = gw(tmp_path)
    gateway.set_tools(
        [
            make_tool("update_description"),          # prefix heuristic
            make_tool("search"),                       # prefix heuristic: read
            make_tool("odd_writer", read_only=False),  # hint wins
            make_tool("add_ish_reader", read_only=True),
        ]
    )
    assert gateway.is_mutation("update_description") is True
    assert gateway.is_mutation("search") is False
    assert gateway.is_mutation("odd_writer") is True
    assert gateway.is_mutation("add_ish_reader") is False


def test_read_appends_trust_context_for_stamped_urns(tmp_path):
    downstream = FakeDownstream(FakeResult(f'{{"results": ["{URN_A}", "{URN_B}"]}}'))
    lookups = []

    def lookup(urn):
        lookups.append(urn)
        if urn == URN_A:
            return {"agent": "enricher-live", "trust": 66.1, "verdict": "skilled"}
        return None  # URN_B is unstamped

    gateway, _ = gw(tmp_path, downstream, trust_lookup=lookup)
    content = asyncio.run(gateway.handle("search", {"query": "x"}))
    assert len(content) == 2  # original + trust block
    block = content[-1].text
    assert "heimdall trust context" in block
    assert "enricher-live" in block and "66.1/100" in block
    assert "db.b" not in block


def test_read_without_stamped_urns_is_untouched(tmp_path):
    downstream = FakeDownstream(FakeResult('{"results": []}'))
    gateway, _ = gw(tmp_path, downstream)
    content = asyncio.run(gateway.handle("search", {"query": "x"}))
    assert len(content) == 1


def test_harmful_author_gets_warning(tmp_path):
    downstream = FakeDownstream(FakeResult(URN_A))
    gateway, _ = gw(
        tmp_path,
        downstream,
        trust_lookup=lambda u: {"agent": "bad", "trust": 31.0, "verdict": HARMFUL},
    )
    content = asyncio.run(gateway.handle("search", {}))
    assert "WARNING" in content[-1].text


def test_trust_lookup_cached_within_ttl(tmp_path):
    downstream = FakeDownstream(FakeResult(URN_A))
    calls = []

    def lookup(urn):
        calls.append(urn)
        return {"agent": "a", "trust": 60.0, "verdict": "skilled"}

    gateway, _ = gw(tmp_path, downstream, trust_lookup=lookup)
    asyncio.run(gateway.handle("search", {}))
    asyncio.run(gateway.handle("search", {}))
    assert len(calls) == 1


def test_mutation_records_implicit_claim_then_forwards(tmp_path):
    downstream = FakeDownstream(FakeResult('{"success": true}'))
    gateway, store = gw(tmp_path, downstream, agent_id="third-party")
    args = {
        "entity_urn": URN_A,
        "column_path": "c",
        "description": "Total in USD.",
    }
    asyncio.run(gateway.handle("update_description", args))
    claims = store.claims(agent_id="third-party")
    assert len(claims) == 1
    assert claims[0].prediction["implicit"] is True
    assert claims[0].confidence == pytest.approx(0.6)
    assert downstream.calls == [("update_description", args)]


def test_non_settleable_mutations_forward_without_claims(tmp_path):
    downstream = FakeDownstream(FakeResult('{"success": true}'))
    gateway, store = gw(tmp_path, downstream)
    asyncio.run(gateway.handle("add_tags", {"entity_urns": [URN_A], "tag_urns": ["t"]}))
    assert store.claims() == []
    assert len(downstream.calls) == 1


def test_enforce_blocks_below_floor_and_records_nothing(tmp_path):
    downstream = FakeDownstream(FakeResult("{}"))
    gateway, store = gw(
        tmp_path, downstream, policy=POLICY_ENFORCE, min_trust=55.0
    )
    # no settled record: neutral trust 50 < 55
    with pytest.raises(PermissionError, match="heimdall policy"):
        asyncio.run(
            gateway.handle(
                "update_description",
                {"entity_urn": URN_A, "description": "x"},
            )
        )
    assert store.claims() == []
    assert downstream.calls == []


def test_enforce_blocks_harmful_verdict_regardless_of_floor(tmp_path):
    downstream = FakeDownstream(FakeResult("{}"))
    gateway, _ = gw(tmp_path, downstream, policy=POLICY_ENFORCE, min_trust=0.0)
    gateway.caller_standing = lambda: {
        "trust": 80.0,
        "verdict": HARMFUL,
        "n_settled": 20,
    }
    with pytest.raises(PermissionError, match="worse than chance"):
        asyncio.run(
            gateway.handle(
                "update_description", {"entity_urn": URN_A, "description": "x"}
            )
        )


def test_enforce_allows_reads_and_neutral_agent_above_floor(tmp_path):
    downstream = FakeDownstream(FakeResult('{"ok": 1}'))
    gateway, store = gw(tmp_path, downstream, policy=POLICY_ENFORCE, min_trust=40.0)
    asyncio.run(gateway.handle("search", {"query": "x"}))  # read: never gated
    asyncio.run(
        gateway.handle(
            "update_description", {"entity_urn": URN_A, "description": "fine"}
        )
    )  # neutral trust 50 >= 40
    assert len(store.claims()) == 1


def test_structured_content_forwarded_with_annotation(tmp_path):
    structured = {"results": [URN_A]}
    downstream = FakeDownstream(FakeResult(URN_A, structured=structured))
    gateway, _ = gw(
        tmp_path,
        downstream,
        trust_lookup=lambda u: {"agent": "a", "trust": 60.0, "verdict": "skilled"},
    )
    out = asyncio.run(gateway.handle("search", {}))
    assert isinstance(out, tuple)
    content, forwarded = out
    assert forwarded == structured
    assert "heimdall trust context" in content[-1].text


def test_downstream_error_propagates(tmp_path):
    downstream = FakeDownstream(FakeResult("boom", is_error=True))
    gateway, _ = gw(tmp_path, downstream)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(gateway.handle("search", {}))


# -- A1 observation capture ---------------------------------------------------


class RaisingDownstream:
    async def call_tool(self, name, args):
        raise ConnectionError("gms unreachable")


def gw_ev(tmp_path, downstream, policy="annotate", min_trust=0.0, agent_id="agent-x"):
    store = ClaimStore(str(tmp_path / "ledger.db"))
    events = EventStore(str(tmp_path / "events.db"))
    gateway = TrustGateway(
        downstream=downstream,
        store=store,
        trust_lookup=lambda urn: None,
        agent_id=agent_id,
        policy=policy,
        min_trust=min_trust,
        event_store=events,
    )
    return gateway, events


def test_read_records_ok_event_with_result_entities(tmp_path):
    downstream = FakeDownstream(FakeResult(f'{{"results": ["{URN_A}"]}}'))
    gateway, events = gw_ev(tmp_path, downstream)
    asyncio.run(gateway.handle("search", {"query": "orders"}))
    evs = events.events()
    assert len(evs) == 1
    ev = evs[0]
    assert ev.tool == "search" and ev.op == READ and ev.status == OK
    assert URN_A in ev.entities
    assert ev.args == {"query": "orders"}
    assert ev.latency_ms is not None


def test_write_records_ok_event_with_arg_entities(tmp_path):
    downstream = FakeDownstream(FakeResult('{"success": true}'))
    gateway, events = gw_ev(tmp_path, downstream, agent_id="third-party")
    asyncio.run(gateway.handle(
        "update_description",
        {"entity_urn": URN_A, "description": "Total in USD."},
    ))
    ev = events.events()[0]
    assert ev.op == WRITE and ev.status == OK
    assert URN_A in ev.entities
    assert ev.agent_id == "third-party"


def test_blocked_mutation_is_still_observed(tmp_path):
    downstream = FakeDownstream(FakeResult("{}"))
    gateway, events = gw_ev(tmp_path, downstream, policy=POLICY_ENFORCE, min_trust=55.0)
    with pytest.raises(PermissionError):
        asyncio.run(gateway.handle(
            "update_description", {"entity_urn": URN_A, "description": "x"}
        ))
    evs = events.events()
    assert len(evs) == 1
    assert evs[0].status == BLOCKED and evs[0].op == WRITE
    assert evs[0].error and "heimdall policy" in evs[0].error
    # downstream never called
    assert downstream.calls == []


def test_downstream_iserror_records_error_event(tmp_path):
    downstream = FakeDownstream(FakeResult("boom", is_error=True))
    gateway, events = gw_ev(tmp_path, downstream)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(gateway.handle("search", {}))
    ev = events.events()[0]
    assert ev.status == ERROR and ev.op == READ
    assert "boom" in ev.error


def test_downstream_exception_records_error_event(tmp_path):
    gateway, events = gw_ev(tmp_path, RaisingDownstream())
    with pytest.raises(ConnectionError):
        asyncio.run(gateway.handle("get_entities", {"urns": [URN_A]}))
    ev = events.events()[0]
    assert ev.status == ERROR
    assert "gms unreachable" in ev.error
    assert URN_A in ev.entities  # captured from args even though the call failed


def test_capture_is_noop_without_event_store(tmp_path):
    # a gateway built without an event store still proxies fine
    downstream = FakeDownstream(FakeResult(URN_A))
    gateway, _ = gw(tmp_path, downstream)
    content = asyncio.run(gateway.handle("search", {}))
    assert content  # no crash, normal return
