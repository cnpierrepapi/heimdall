"""Global agent selection: choose the best agent for a job from everyone ranked.

Selection is not scoped to one customer's own agents. When you need a job of a
given work_kind done, you pick from the whole global leaderboard of agents
Heimdall has ranked, ordered by earned trust. Public agents are selectable by
anyone; private agents (an org's own, kept unlisted) are hidden from selection
and instead surface as access requests to their owner.

These functions operate on rows as published to the hd_agents table (the global
registry the console reads), so the same ranking drives selection everywhere.
"""

from __future__ import annotations

from typing import Any, Optional

PUBLIC = "public"
PRIVATE = "private"


def _trust(row: dict[str, Any]) -> float:
    try:
        return float(row.get("trust") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def global_leaderboard(
    rows: list[dict[str, Any]],
    work_kind: str,
    requester: Optional[str] = None,
    include_private: bool = False,
) -> list[dict[str, Any]]:
    """Every selectable agent for a work_kind, best trust first.

    Public agents are always included. Private agents are included only when
    include_private is set or the requester owns them (they can select their
    own private agents).
    """
    board = []
    for r in rows:
        if r.get("work_kind") != work_kind:
            continue
        visibility = r.get("visibility", PUBLIC)
        owns = requester is not None and r.get("owner") == requester
        if visibility == PUBLIC or include_private or owns:
            board.append(r)
    board.sort(key=_trust, reverse=True)
    return board


def best_agent(
    rows: list[dict[str, Any]], work_kind: str, requester: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """The single best selectable agent for a work_kind, or None."""
    board = global_leaderboard(rows, work_kind, requester=requester)
    return board[0] if board else None


def access_requests(
    rows: list[dict[str, Any]], work_kind: str, requester: Optional[str] = None
) -> list[dict[str, Any]]:
    """Private agents a requester cannot select yet but could request access to."""
    out = []
    for r in rows:
        if r.get("work_kind") != work_kind:
            continue
        if r.get("visibility") == PRIVATE and r.get("owner") != requester:
            out.append(r)
    out.sort(key=_trust, reverse=True)
    return out
