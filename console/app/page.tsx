import Link from "next/link";
import CountUp from "@/components/landing/CountUp";
import GatewayFlow from "@/components/landing/GatewayFlow";
import Rail, { type RailStep } from "@/components/landing/Rail";
import Reveal from "@/components/landing/Reveal";
import Tape, { type TapeItem } from "@/components/landing/Tape";
import VerdictBadge from "@/components/VerdictBadge";
import { describeClaim, getAgents, getRecentClaims } from "@/lib/data";

export const revalidate = 60;

function trustColor(trust: number): string {
  if (trust >= 60) return "var(--skilled)";
  if (trust >= 50) return "var(--luck)";
  return "var(--harmful)";
}

const RAIL_STEPS: RailStep[] = [
  {
    title: "Act",
    body: "An agent does real catalog work through the DataHub MCP server: documents a column, names an incident root cause, forecasts a feed delivery.",
  },
  {
    title: "Claim",
    body: "The action is recorded as a falsifiable statement with stated confidence. Instrumented agents sign their own claims. Uninstrumented agents get claimed by the gateway.",
    code: `{
  "claim_type": "freshness_sla",
  "entity": "raw_events",
  "prediction": { "will_miss_sla": true, "day": 3 },
  "confidence": 0.78,
  "status": "open"
}`,
  },
  {
    title: "Settle",
    body: "Ground truth arrives: assertion runs, SLA outcomes, incident resolutions, steward reviews. Each open claim is marked right or wrong. No self-grading.",
  },
  {
    title: "Score",
    body: "Brier score and calibration over the settled record, then a Monte Carlo test against the agent's own luck baseline: coin flips for forecasts, one in n for root causes, the pooled acceptance rate for documentation.",
  },
  {
    title: "Verdict",
    body: "Skilled, luck, or harmful, corrected for multiple comparisons. Trust shrinks toward 50 when the record is thin, so three lucky wins never outrank eighty settled claims.",
  },
];

export default async function Landing() {
  const [agents, recent] = await Promise.all([getAgents(), getRecentClaims(20)]);
  const totalClaims = agents.reduce((s, a) => s + a.n_total, 0);
  const totalSettled = agents.reduce((s, a) => s + a.n_settled, 0);
  const rogue = agents.find((a) => a.agent_id === "rogue-agent") ?? null;
  const top = agents.slice(0, 5);

  const tapeItems: TapeItem[] = recent.map((c) => ({
    mark: c.correct === null ? "OPEN" : c.correct ? "RIGHT" : "WRONG",
    cls: c.correct === null ? "open" : c.correct ? "right" : "wrong",
    text: describeClaim(c.claim_type, c.prediction, c.entity_urn),
    conf: c.confidence.toFixed(2),
  }));

  return (
    <main className="landing">
      <div className="glow" />

      <section className="hero-wrap">
        <div>
          <div className="kicker">built on DataHub / Apache 2.0</div>
          <h1 className="hero-title display">
            <span className="hero-line">
              <span>Every action is a claim.</span>
            </span>
            <span className="hero-line">
              <span>Every claim settles.</span>
            </span>
            <span className="hero-line accent">
              <span>Trust is computed.</span>
            </span>
          </h1>
          <p className="hero-sub">
            Heimdall scores the AI agents working your DataHub catalog
            against ground truth, writes the trust back into the catalog, and
            blocks writes from agents that have not earned it.
          </p>
          <div className="hero-ctas">
            <Link href="/board" className="btn primary">
              see the live board
            </Link>
            <Link href="/proof" className="btn ghost">
              open the proof
            </Link>
          </div>
          <div className="hero-stats">
            <div className="hero-stat">
              <div className="n display">
                <CountUp value={agents.length} />
              </div>
              <div className="l">agents scored</div>
            </div>
            <div className="hero-stat">
              <div className="n display">
                <CountUp value={totalClaims} />
              </div>
              <div className="l">claims recorded</div>
            </div>
            <div className="hero-stat">
              <div className="n display">
                <CountUp value={totalSettled} />
              </div>
              <div className="l">settled against reality</div>
            </div>
          </div>
        </div>
        <Tape items={tapeItems} />
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>01</b> the gap
        </div>
        <Reveal>
          <div className="loglines">
            <span className="ok">an agent documented ten columns in raw_orders</span>
            <span className="ok">an agent named a root cause in one MCP call</span>
            <span className="ok">an agent forecast tomorrow&apos;s raw_events delivery</span>
          </div>
        </Reveal>
        <Reveal delay={120}>
          <h2 className="big-statement display">
            All of it landed in your catalog.{" "}
            <span className="dim">None of it was verified.</span>
          </h2>
        </Reveal>
        <Reveal delay={200}>
          <p className="hero-sub">
            Agent confidence is self-reported. Heimdall makes every action
            falsifiable, then keeps score.
          </p>
        </Reveal>
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>02</b> for teams running agents on DataHub
        </div>
        <div className="svc-grid">
          <Reveal>
            <div className="card svc">
              <span className="ghost-n display">01</span>
              <div className="idx">01 / SCORE</div>
              <h3>Score any agent</h3>
              <p>
                Point the agent&apos;s MCP traffic through the trust gateway.
                Its catalog writes become claims automatically. No SDK, no
                changes to the agent.
              </p>
            </div>
          </Reveal>
          <Reveal delay={90}>
            <div className="card svc">
              <span className="ghost-n display">02</span>
              <div className="idx">02 / GATE</div>
              <h3>Gate the catalog</h3>
              <p>
                Set a trust floor per workflow. Writes from agents below the
                floor are rejected before they touch metadata. Harmful
                verdicts are always blocked.
              </p>
            </div>
          </Reveal>
          <Reveal delay={180}>
            <div className="card svc">
              <span className="ghost-n display">03</span>
              <div className="idx">03 / WRITE BACK</div>
              <h3>Write trust back</h3>
              <p>
                Verdict tags, trust properties, and a dossier per agent,
                published into DataHub where your stewards already work.
              </p>
            </div>
          </Reveal>
          <Reveal delay={270}>
            <div className="card svc">
              <span className="ghost-n display">04</span>
              <div className="idx">04 / AUDIT</div>
              <h3>Audit in public</h3>
              <p>
                A live board per agent: trust, win rate, calibration, and
                every settled claim.{" "}
                <Link href="/board" className="svc-link">
                  This site is it &rarr;
                </Link>
              </p>
            </div>
          </Reveal>
        </div>
        <Reveal delay={120}>
          <GatewayFlow rogueTrust={rogue ? rogue.trust : null} floor={55} />
        </Reveal>
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>03</b> how a score is earned
        </div>
        <Rail steps={RAIL_STEPS} />
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>04</b> proof, caught live
        </div>
        <div className="rogue">
          <div>
            <Reveal>
              <h2 className="big-statement display" style={{ fontSize: "clamp(24px, 3vw, 36px)" }}>
                A rogue agent, caught by settlement.
              </h2>
            </Reveal>
            <div className="rogue-steps">
              <Reveal>
                <div className="rstep">
                  <span className="rn">01</span>
                  <p>
                    <b>rogue-agent</b>, never instrumented, writes a column
                    description through the gateway.
                  </p>
                </div>
              </Reveal>
              <Reveal delay={80}>
                <div className="rstep">
                  <span className="rn">02</span>
                  <p>
                    The gateway records it as an implicit claim at p=0.60.
                    The agent never knew.
                  </p>
                </div>
              </Reveal>
              <Reveal delay={160}>
                <div className="rstep bad">
                  <span className="rn">03</span>
                  <p>
                    A steward review settles the claim <b>wrong</b>. The bad
                    description is reverted in DataHub.
                  </p>
                </div>
              </Reveal>
              <Reveal delay={240}>
                <div className="rstep bad">
                  <span className="rn">04</span>
                  <p>
                    {rogue
                      ? `Trust falls to ${rogue.trust.toFixed(1)}, below the floor of 55. `
                      : "Trust falls below the floor of 55. "}
                    Next write attempt: <b>blocked</b>.
                  </p>
                </div>
              </Reveal>
            </div>
            {rogue && (
              <Reveal delay={300}>
                <Link
                  href={`/agents/${encodeURIComponent(rogue.agent_id)}`}
                  className="btn ghost"
                  style={{ display: "inline-block", marginTop: 18 }}
                >
                  see its record
                </Link>
              </Reveal>
            )}
          </div>
          <Reveal delay={140}>
            <div className="card">
              <div className="mini-head">the board right now</div>
              {top.map((a) => (
                <Link
                  key={a.agent_id}
                  href={`/agents/${encodeURIComponent(a.agent_id)}`}
                  className="mini-row"
                >
                  <span className="mini-name">{a.agent_id}</span>
                  <span
                    className="mini-trust"
                    style={{ color: trustColor(a.trust) }}
                  >
                    {a.trust.toFixed(1)}
                  </span>
                  <VerdictBadge verdict={a.verdict} />
                </Link>
              ))}
              {top.length === 0 && (
                <div className="claim-line">
                  <span className="claim-body">
                    No published agents yet. Run the pipeline and publish the
                    ledger.
                  </span>
                </div>
              )}
              <Link href="/board" className="mini-more">
                full board &rarr;
              </Link>
            </div>
          </Reveal>
        </div>
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>05</b> run it
        </div>
        <Reveal>
          <div className="card run-box">
            <div>
              <h2 className="big-statement display" style={{ fontSize: "clamp(24px, 3vw, 36px)" }}>
                Run it on your catalog.
              </h2>
              <p className="hero-sub" style={{ marginTop: 14 }}>
                One Python package, Apache 2.0. Works against any DataHub with
                the MCP server enabled.
              </p>
              <div className="hero-ctas" style={{ marginTop: 22 }}>
                <a
                  href="https://github.com/cnpierrepapi/heimdall"
                  target="_blank"
                  rel="noreferrer"
                  className="btn primary"
                >
                  github.com/cnpierrepapi/heimdall
                </a>
              </div>
            </div>
            <div className="term">
              <div>
                <span className="p">$</span>git clone
                https://github.com/cnpierrepapi/heimdall
              </div>
              <div>
                <span className="p">$</span>pip install -e heimdall
              </div>
              <div>
                <span className="p">$</span>python -m heimdall.gateway
              </div>
              <div className="c"># every write is now a scored claim</div>
            </div>
          </div>
        </Reveal>
      </section>
    </main>
  );
}
