"""T6: the PostgREST publisher, with a fake HTTP client (no network)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from heimdall.publisher import Publisher


class FakeResp:
    def __init__(self, status=201, text=""):
        self.status_code = status
        self.text = text


class FakeClient:
    def __init__(self):
        self.requests = []

    def post(self, url, json=None, headers=None):
        self.requests.append(("POST", url, json, headers))
        return FakeResp(201)

    def delete(self, url, headers=None):
        self.requests.append(("DELETE", url, None, headers))
        return FakeResp(204)

    def close(self):
        pass


@dataclass
class FakeResult:
    activity: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    gc_deleted: list = field(default_factory=list)


def _pub(client):
    return Publisher(url="https://x.supabase.co", service_key="svc", client=client)


def test_requires_url_and_key():
    with pytest.raises(ValueError):
        Publisher(url="", service_key="", client=FakeClient())


def test_service_key_in_auth_headers_not_logged():
    c = FakeClient()
    _pub(c).insert("hd_activity", [{"a": 1}])
    _, _, _, headers = c.requests[0]
    assert headers["apikey"] == "svc"
    assert headers["Authorization"] == "Bearer svc"


def test_insert_skips_empty():
    c = FakeClient()
    assert _pub(c).insert("hd_activity", []) == 0
    assert c.requests == []


def test_upsert_sets_on_conflict_and_merge():
    c = FakeClient()
    _pub(c).upsert("hd_agents", [{"agent_id": "a", "work_kind": "pii"}],
                   on_conflict="agent_id,work_kind")
    method, url, _, headers = c.requests[0]
    assert "on_conflict=agent_id,work_kind" in url
    assert "merge-duplicates" in headers["Prefer"]


def test_delete_catalogs_builds_in_filter():
    c = FakeClient()
    _pub(c).delete_catalogs("hd_activity", ["cat1", "cat2"])
    method, url, _, _ = c.requests[0]
    assert method == "DELETE"
    assert "owner=eq.showcase" in url
    assert "catalog=in.(cat1,cat2)" in url


def test_delete_catalogs_noop_when_empty():
    c = FakeClient()
    _pub(c).delete_catalogs("hd_activity", [])
    assert c.requests == []


def test_publish_tick_sequences_writes_and_gc():
    c = FakeClient()
    result = FakeResult(
        activity=[{"agent_id": "a", "catalog": "c1"}],
        findings=[{"agent_id": "a", "catalog": "c1"}],
        agents=[{"agent_id": "a", "work_kind": "pii"}],
        gc_deleted=["old1"],
    )
    counts = _pub(c).publish_tick(result)
    assert counts == {"activity": 1, "findings": 1, "agents": 1}
    urls = [r[1] for r in c.requests]
    assert any("hd_activity" in u and "on_conflict" not in u for u in urls)
    assert any("hd_agents?on_conflict=agent_id,work_kind" in u for u in urls)
    # both feed tables get a lockstep GC delete for the retired catalog
    deletes = [u for m, u, _, _ in c.requests if m == "DELETE"]
    assert sum("catalog=in.(old1)" in u for u in deletes) == 2


def test_error_response_raises():
    class ErrClient(FakeClient):
        def post(self, url, json=None, headers=None):
            return FakeResp(400, "bad")
    with pytest.raises(RuntimeError):
        _pub(ErrClient()).insert("hd_activity", [{"a": 1}])
