"""T4/T5: the stable roster, per-kind profiles, and seeded casting."""

from __future__ import annotations

from heimdall.generator import generate_catalog
from heimdall.roster import (
    CASTABLE_KINDS,
    DILIGENT,
    HASTY,
    KIND_COLUMN_DOC,
    KIND_DOMAIN,
    KIND_OWNER,
    KIND_PII,
    KIND_TERM,
    PROFILE_SYSTEMS,
    ROSTER,
    ROGUE,
    cast,
    profile_system,
    work_kinds_available,
)


def test_roster_identities_are_stable_and_unique():
    ids = [a.agent_id for a in ROSTER]
    assert len(ids) == len(set(ids))
    assert len(ROSTER) >= 5
    assert {a.profile for a in ROSTER} == {DILIGENT, HASTY, ROGUE}


def test_castable_kinds_have_profiles_and_agents():
    for kind in CASTABLE_KINDS:
        assert kind in PROFILE_SYSTEMS
        assert any(a.work_kind == kind for a in ROSTER)


def test_column_doc_profiles_are_distinct():
    texts = [profile_system(KIND_COLUMN_DOC, p) for p in (DILIGENT, HASTY, ROGUE)]
    assert len(set(texts)) == 3
    for t in texts:
        assert "JSON object" in t
        assert "—" not in t  # no em dashes in what we send


def test_pii_profiles_diverge():
    diligent = profile_system(KIND_PII, DILIGENT)
    rogue = profile_system(KIND_PII, ROGUE)
    assert diligent != rogue
    assert "NOT PII" in diligent  # the careful reviewer excludes ids/geo
    assert "over-flag" in rogue or "flag it" in rogue


def test_generated_catalog_exercises_the_core_kinds():
    kinds = work_kinds_available(generate_catalog(42))
    assert {KIND_COLUMN_DOC, KIND_PII, KIND_OWNER, KIND_DOMAIN, KIND_TERM} <= kinds


def test_cast_is_deterministic():
    spec = generate_catalog(7)
    assert cast(spec, seed=1) == cast(spec, seed=1)


def test_cast_only_returns_eligible_castable_agents():
    spec = generate_catalog(7)
    picked = cast(spec, seed=3, k=3, kinds=CASTABLE_KINDS)
    assert picked
    assert all(a.work_kind in CASTABLE_KINDS for a in picked)


def test_cast_can_be_filtered_to_one_kind():
    spec = generate_catalog(7)
    picked = cast(spec, seed=3, k=8, kinds={KIND_COLUMN_DOC})
    assert picked
    assert all(a.work_kind == KIND_COLUMN_DOC for a in picked)
