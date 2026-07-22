"""A stable roster of LLM agents with fixed competence profiles.

The same named agents work catalog after catalog, so their trust accumulates
real longitudinal evidence and the skill-vs-luck test strengthens as n grows. A
skill spectrum is guaranteed without ever hardcoding an outcome: every agent runs
the same open-weight model, but under a different system prompt. A "diligent"
agent is told to inspect the schema and lineage and only state what the evidence
supports; a "hasty" agent is told to move fast on the column name alone; a "rogue"
agent is under-instructed and clears a backlog with generic notes. Grounding and
settlement then judge what each actually wrote against the generated catalog's
truth, so some agents earn skilled and others earn worse-than-chance over time.

Each tick casts a random subset matched to the catalog's archetype mix (a PII-heavy
catalog exercises the PII taggers, a documentation-deep one the enrichers). This
module owns the roster and the casting; the tick loop (T5) drives the cast agents
through the gateway and meters every call into the spend ledger.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .catalog import CatalogSpec

# work kinds an agent can be responsible for (match the trust engine's kinds)
KIND_COLUMN_DOC = "column_doc"
KIND_PII = "pii"
KIND_OWNER = "owner"
KIND_DOMAIN = "domain"
KIND_TERM = "term"

DILIGENT = "diligent"
HASTY = "hasty"
ROGUE = "rogue"


@dataclass(frozen=True)
class RosterAgent:
    agent_id: str  # stable identity across catalogs, so trust accumulates
    work_kind: str
    profile: str


# The roster is intentionally fixed. Identities persist across every catalog.
ROSTER: tuple[RosterAgent, ...] = (
    RosterAgent("atlas-doc", KIND_COLUMN_DOC, DILIGENT),
    RosterAgent("juno-doc", KIND_COLUMN_DOC, HASTY),
    RosterAgent("nyx-doc", KIND_COLUMN_DOC, ROGUE),
    RosterAgent("vega-pii", KIND_PII, DILIGENT),
    RosterAgent("orion-pii", KIND_PII, ROGUE),
)

# Kinds the engine can currently drive with genuinely earned skill: the catalog
# truth is inferable from what the agent reads (schema, column names, lineage).
# owner/domain/term truth is not schema-readable in a generated catalog, so they
# are held back rather than scored as luck.
CASTABLE_KINDS: set[str] = {KIND_COLUMN_DOC, KIND_PII}


# -- profile prompts for the column-documentation work kind -------------------
#
# All three keep the same strict JSON contract so the writer agent parses them
# identically; only the working attitude differs, which is what produces the
# skill spectrum once grounding grades the results.

_JSON_CONTRACT = (
    'Reply with ONLY a JSON object, no prose:\n'
    '{"proposals": [{"column": "...", "description": "...", "confidence": 0.05-0.95}]}\n'
    'Confidence is your probability that a human steward would ACCEPT the '
    'description as accurate. Include every undocumented column exactly once. '
    'Plain prose, no em dashes.'
)

_COLUMN_DOC_PROFILES: dict[str, str] = {
    DILIGENT: (
        "You are a meticulous data steward documenting a warehouse catalog. "
        "You are given a table's schema, its upstream schemas, and the column-level "
        "lineage. For each undocumented column, inspect the evidence and write one "
        "precise sentence that names the exact quantity or identifier it holds, spells "
        "out its units, currency, or time basis when relevant, and says where it derives "
        "from when the upstream columns make that clear. Use the column's own terms. "
        "State only what the evidence supports.\n\n" + _JSON_CONTRACT
    ),
    HASTY: (
        "You are a busy data steward clearing work quickly. You have little time and "
        "may not read all the context. For each undocumented column, give a short, "
        "plausible one-line description based mainly on the column name. Do not "
        "overthink it or dig through lineage.\n\n" + _JSON_CONTRACT
    ),
    ROGUE: (
        "You are clearing a documentation backlog with the least effort possible. For "
        "each column write a short, vague note about its general area only. Do NOT name "
        "the specific quantity, amount, units, currency, identifier, or where the value "
        "comes from. Generic filler such as 'a field used in this table' or 'holds "
        "record information' is exactly what you should write. Do not read the schema "
        "or lineage.\n\n" + _JSON_CONTRACT
    ),
}

_PII_CONTRACT = (
    'Reply with ONLY a JSON object, no prose:\n'
    '{"flags": [{"column": "...", "pii_type": "...", "confidence": 0.05-0.95}]}\n'
    'Type each flag from this vocabulary: email, person_name, phone, address, '
    'national_id. An empty flags list is a valid answer. Confidence is your '
    'probability that a steward confirms the flag. Plain prose, no em dashes.'
)

_PII_PROFILES: dict[str, str] = {
    DILIGENT: (
        "You are a careful data protection reviewer classifying warehouse columns for "
        "PII. Flag ONLY columns whose content itself identifies a person. Pseudonymous "
        "surrogate keys (customer_id, user_id, order_id) are NOT PII. Coarse geography "
        "(country, country_code, region) is NOT PII. Timestamps, amounts, and category "
        "codes are NOT PII. Only flag a column when it directly holds an email, a "
        "person's name, a phone number, an address, or a national identifier.\n\n"
        + _PII_CONTRACT
    ),
    ROGUE: (
        "You are aggressively tagging columns for privacy risk. When in any doubt, flag "
        "it. Treat every identifier column (any *_id) as personal, and treat location "
        "columns like country or country_code as personal too. It is better to over-flag "
        "than to miss anything.\n\n" + _PII_CONTRACT
    ),
}

# profiles per work kind
PROFILE_SYSTEMS: dict[str, dict[str, str]] = {
    KIND_COLUMN_DOC: _COLUMN_DOC_PROFILES,
    KIND_PII: _PII_PROFILES,
}


def profile_system(work_kind: str, profile: str) -> str:
    """The system prompt for a work kind under a competence profile."""
    return PROFILE_SYSTEMS[work_kind][profile]


# -- what a catalog can exercise, and casting ---------------------------------


def work_kinds_available(spec: CatalogSpec) -> set[str]:
    """Which work kinds a catalog carries gradeable targets for."""
    cols = [c for d in spec.datasets for c in d.columns]
    kinds: set[str] = {KIND_OWNER, KIND_DOMAIN}  # every dataset has owner + domain
    if any(c.description is None and c.gold_keywords for c in cols):
        kinds.add(KIND_COLUMN_DOC)
    if any(c.pii for c in cols):
        kinds.add(KIND_PII)
    if any(c.term for c in cols):
        kinds.add(KIND_TERM)
    return kinds


def cast(spec: CatalogSpec, seed: int, k: int = 4,
         kinds: set[str] | None = None) -> list[RosterAgent]:
    """Deterministically pick a subset of the roster for this catalog/tick.

    Only agents whose work kind the catalog actually exercises are eligible, so a
    cast agent always has real work. `kinds` can restrict further (the T4 proof
    drives column documentation only).
    """
    available = work_kinds_available(spec)
    if kinds is not None:
        available &= kinds
    eligible = [a for a in ROSTER if a.work_kind in available]
    if not eligible:
        return []
    rng = random.Random(f"{spec.catalog}:{seed}")
    n = min(k, len(eligible))
    return sorted(rng.sample(eligible, n), key=lambda a: a.agent_id)
