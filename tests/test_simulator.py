import pytest

from heimdall.claims import ClaimStore
from heimdall.simulator import (
    RandomAgent,
    SharpAgent,
    build_default_timeline,
    build_default_world,
    normalize_directional,
    run,
)
from heimdall.skill import HARMFUL, LUCK, SKILLED


@pytest.fixture()
def world():
    return build_default_world()


def test_world_builds_and_lineage_is_consistent(world):
    assert len(world.datasets) == 12
    assert "stg_orders" in world.descendants("raw_orders")
    assert "rpt_daily_kpis" in world.descendants("raw_orders")
    assert world.ancestors("rpt_daily_kpis") >= {
        "fct_revenue",
        "fct_engagement",
        "raw_orders",
        "raw_web_events",
    }


def test_blast_sets_are_column_precise(world):
    # discount_code feeds stg_orders and fct_orders but NOT fct_revenue,
    # which reads only total_usd. Downstream != broken.
    assert world.blast_set("raw_orders", "discount_code") == {
        "stg_orders",
        "fct_orders",
    }
    assert world.blast_set("raw_customers", "email") == {
        "stg_customers",
        "dim_customers",
    }
    # order_total_usd propagates all the way to the KPI report
    assert "rpt_daily_kpis" in world.blast_set("raw_orders", "order_total_usd")


def test_timeline_is_deterministic(world):
    a = build_default_timeline(world, seed=11)
    b = build_default_timeline(world, seed=11)
    assert a.happenings == b.happenings
    assert a.lateness == b.lateness


def test_normalize_directional_flips_sub_half_confidence():
    assert normalize_directional(True, 0.3) == (False, 0.7)
    assert normalize_directional(True, 0.8) == (True, 0.8)


def test_full_run_settles_everything_and_separates_skill(world, tmp_path):
    tl = build_default_timeline(world, seed=11)
    sharp = SharpAgent(tl, accuracy=0.95, seed=42)
    rand = RandomAgent(seed=99)

    with ClaimStore(tmp_path / "sim.db") as store:
        result = run(world, tl, [sharp, rand], store, n_sims=2000)

        assert result["n_unsettled"] == 0
        # both agents claimed on every opportunity
        per_agent = {
            a: len(store.claims(agent_id=a)) for a in store.agent_ids()
        }
        assert per_agent["sharp-agent"] == per_agent["random-agent"]
        assert per_agent["sharp-agent"] >= 30

        report = result["report"]
        assert report["sharp-agent"]["verdict"] == SKILLED
        # The guesser must never be called skilled. Depending on the pooled
        # enrichment acceptance rate it lands on luck or worse-than-chance;
        # both are correct rejections.
        assert report["random-agent"]["verdict"] in (LUCK, HARMFUL)
        assert report["sharp-agent"]["trust"] > report["random-agent"]["trust"]


def test_enrichment_reviews_settle_per_claim(world, tmp_path):
    # Two agents describe the same column; only the good description is
    # accepted. Verifies the claim-targeted steward review path.
    tl = build_default_timeline(world, seed=11)
    sharp = SharpAgent(tl, accuracy=1.0, seed=1)
    rand = RandomAgent(seed=2)

    with ClaimStore(tmp_path / "sim.db") as store:
        run(world, tl, [sharp, rand], store, n_sims=200)
        sharp_enrich = store.claims(agent_id="sharp-agent", claim_type="enrichment")
        assert sharp_enrich and all(c.correct for c in sharp_enrich)
        rand_enrich = store.claims(agent_id="random-agent", claim_type="enrichment")
        assert any(not c.correct for c in rand_enrich)
