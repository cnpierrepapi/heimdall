"""Global agent selection from the ranked registry."""

from __future__ import annotations

from heimdall.select import access_requests, best_agent, global_leaderboard

ROWS = [
    {"agent_id": "expert-doc", "work_kind": "column_doc", "trust": 70.0, "visibility": "public", "owner": None},
    {"agent_id": "mid-doc", "work_kind": "column_doc", "trust": 55.0, "visibility": "public", "owner": None},
    {"agent_id": "acme-internal", "work_kind": "column_doc", "trust": 90.0, "visibility": "private", "owner": "acme"},
    {"agent_id": "pii-pro", "work_kind": "pii", "trust": 65.0, "visibility": "public", "owner": None},
]


def test_selection_is_global_over_public_agents():
    board = global_leaderboard(ROWS, "column_doc")
    # both public column_doc agents, best first; the private one is excluded
    assert [r["agent_id"] for r in board] == ["expert-doc", "mid-doc"]


def test_best_agent_picks_top_public_even_if_a_private_scores_higher():
    # acme-internal has higher trust but is private to another org
    assert best_agent(ROWS, "column_doc")["agent_id"] == "expert-doc"


def test_owner_can_select_their_own_private_agent():
    board = global_leaderboard(ROWS, "column_doc", requester="acme")
    assert board[0]["agent_id"] == "acme-internal"  # 90 trust, owner sees it


def test_access_requests_lists_others_private_agents():
    reqs = access_requests(ROWS, "column_doc", requester="globex")
    assert [r["agent_id"] for r in reqs] == ["acme-internal"]
    # the owner has no access request for their own agent
    assert access_requests(ROWS, "column_doc", requester="acme") == []


def test_selection_respects_work_kind():
    assert best_agent(ROWS, "pii")["agent_id"] == "pii-pro"
    assert [r["agent_id"] for r in global_leaderboard(ROWS, "pii")] == ["pii-pro"]
