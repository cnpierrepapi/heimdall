"""What each kind of metadata work is, and whether this deployment can score it.

The single registry of work kinds. Two facts live here: what settles a claim of
this kind, and therefore whether trust can honestly be computed for it here.

The governing rule is that an agent can earn trust only when the answer is a
function of evidence the agent can observe. That happens two ways. The answer can
be carried by the artifact itself, as when a column named order_total_usd tells a
careful reader what it means, so a good description is earned and filler is
caught. Or the answer can be predicted from observable history, as when a feed
that runs late a third of the time is forecast by an agent that reads its
landing record.

Ownership is neither. A catalog does not know who owns it, so a synthetic catalog
can only stipulate an owner and grade guesses against a label that leaves no
trace in anything the agent can see. Scoring that would publish luck as skill,
and it would do so with a confident number attached. So ownership, domain and
glossary terms settle by steward review, and where no steward exists they are not
scored at all. They are still observed, still grounded, still governed: an owner
proposal naming a team that does not exist is caught in flight. What is withheld
is the trust score, not the oversight.

This matters beyond our own roster. Any third party agent can be pointed at the
gateway, which is the whole agent-agnostic claim, and one doing ownership work
must meet the same wall. The settlement source decides what may be scored, not
the choice of which agents we happen to run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# -- settlement mechanisms ----------------------------------------------------
# Named after where the truth comes from, following the fleet settlement taxonomy.

S_ARTIFACT = "artifact"        # the catalog carries a checkable answer
S_CROSSCHECK = "crosscheck"    # an independent computation validates the claim
S_FORWARD = "forward"          # time reveals the outcome
S_STEWARD = "steward"          # a human accepts or reverts the artifact
S_BEHAVIORAL = "behavioral"    # usage follows the claim

_SOURCE_LABEL = {
    S_ARTIFACT: "catalog ground truth",
    S_CROSSCHECK: "independent cross-check",
    S_FORWARD: "a forward outcome",
    S_STEWARD: "steward review",
    S_BEHAVIORAL: "behavioral confirmation",
}

# -- the work kinds -----------------------------------------------------------

KIND_COLUMN_DOC = "column_doc"
KIND_TABLE_DOC = "table_doc"
KIND_PII = "pii"
KIND_OWNER = "owner"
KIND_DOMAIN = "domain"
KIND_TERM = "term"


@dataclass(frozen=True)
class WorkKind:
    id: str
    settlement: str
    summary: str


WORK_KINDS: dict[str, WorkKind] = {
    KIND_COLUMN_DOC: WorkKind(
        KIND_COLUMN_DOC, S_ARTIFACT,
        "describing a column, judged against what the column demonstrably is",
    ),
    KIND_PII: WorkKind(
        KIND_PII, S_ARTIFACT,
        "classifying a column as personal data, judged against the catalog",
    ),
    KIND_TABLE_DOC: WorkKind(
        KIND_TABLE_DOC, S_ARTIFACT,
        "describing a dataset, judged against what the dataset demonstrably is",
    ),
    KIND_OWNER: WorkKind(
        KIND_OWNER, S_STEWARD,
        "proposing who owns an asset, which only its organization can confirm",
    ),
    KIND_DOMAIN: WorkKind(
        KIND_DOMAIN, S_STEWARD,
        "placing an asset in a domain, which only its organization can confirm",
    ),
    KIND_TERM: WorkKind(
        KIND_TERM, S_STEWARD,
        "mapping a column to a glossary term, a business convention not a fact",
    ),
}

# Settlement sources this deployment actually has. The synthetic showcase carries
# catalog ground truth and can run cross-checks; it has no stewards and, until the
# world evolves over time, no forward outcomes.
DEFAULT_SOURCES = frozenset({S_ARTIFACT, S_CROSSCHECK})

ENV_SOURCES = "HEIMDALL_SETTLEMENT_SOURCES"

# Must match skill.skill_report's min_settled default; a test holds them together.
DEFAULT_MIN_SETTLED = 5

# -- score states -------------------------------------------------------------

SCORED = "scored"
INSUFFICIENT = "insufficient"
UNSCOREABLE = "unscoreable"


def available_sources() -> frozenset[str]:
    """Which settlement sources this deployment has, overridable by env."""
    raw = os.environ.get(ENV_SOURCES, "").strip()
    if not raw:
        return DEFAULT_SOURCES
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def settlement_of(kind: str) -> Optional[str]:
    wk = WORK_KINDS.get(kind)
    return wk.settlement if wk else None


def scoreable(kind: str, sources: Optional[frozenset[str]] = None) -> tuple[bool, str]:
    """Can trust for this work kind be computed honestly here, and if not, why.

    An unknown kind is not scoreable: we will not score work we cannot describe.
    """
    sources = available_sources() if sources is None else sources
    wk = WORK_KINDS.get(kind)
    if wk is None:
        return False, f"unknown work kind {kind!r}, so there is nothing to settle it"
    if wk.settlement in sources:
        return True, ""
    label = _SOURCE_LABEL.get(wk.settlement, wk.settlement)
    return False, f"settles by {label}, which is not available in this deployment"


def score_state(
    kind: str,
    n_settled: int,
    min_settled: int = DEFAULT_MIN_SETTLED,
    sources: Optional[frozenset[str]] = None,
) -> tuple[str, str]:
    """The honest state of an agent's score for one work kind, and the reason.

    Distinguishes the two nulls that matter. An unscoreable kind will never be
    scored here no matter how long it runs, while an insufficient one only needs
    more evidence. Reporting both as "not enough data" would promise a score that
    is never coming.
    """
    ok, why = scoreable(kind, sources)
    if not ok:
        return UNSCOREABLE, why
    if n_settled < min_settled:
        return INSUFFICIENT, (f"{n_settled} settled of {min_settled} needed "
                              f"before a verdict is meaningful")
    return SCORED, ""
