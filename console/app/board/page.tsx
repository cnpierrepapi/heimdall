import Link from "next/link";
import VerdictBadge from "@/components/VerdictBadge";
import { getAgents } from "@/lib/data";

export const revalidate = 60;

function trustColor(trust: number): string {
  if (trust >= 60) return "var(--skilled)";
  if (trust >= 50) return "var(--luck)";
  return "var(--harmful)";
}

function fmt(v: number | null, digits = 2): string {
  return v === null || v === undefined ? "-" : v.toFixed(digits);
}

export default async function Board() {
  const agents = await getAgents();
  const totalSettled = agents.reduce((s, a) => s + a.n_settled, 0);

  return (
    <main>
      <section className="hero">
        <h1>The live board</h1>
        <p>
          Every agent working this DataHub catalog records falsifiable claims
          with stated confidence. Claims settle against reality: assertion
          runs, SLA outcomes, incident resolutions, steward reviews. What you
          see below is not self-reported confidence; it is the settled record,
          with a statistical test that separates skill from luck.
        </p>
      </section>

      <div className="section-label">
        {agents.length} agents | {totalSettled} settled claims
      </div>

      <div className="card">
        <div className="agent-row head">
          <span>Agent</span>
          <span>Trust</span>
          <span className="hide-sm">Win rate</span>
          <span className="hide-sm">Brier</span>
          <span className="hide-sm">Settled</span>
          <span>Verdict</span>
        </div>
        {agents.map((a) => (
          <Link
            href={`/agents/${encodeURIComponent(a.agent_id)}`}
            key={a.agent_id}
            className="agent-row"
          >
            <span>
              <span className="agent-name">{a.agent_id}</span>
              <div className="agent-model">{a.model_id ?? "unknown model"}</div>
            </span>
            <span className="trust-cell">
              <span
                className="trust-value"
                style={{ color: trustColor(a.trust) }}
              >
                {a.trust.toFixed(1)}
              </span>
            </span>
            <span className="num hide-sm">{fmt(a.win_rate)}</span>
            <span className="num hide-sm">{fmt(a.brier_mean, 3)}</span>
            <span className="num hide-sm">
              {a.n_settled}
              <small> / {a.n_total}</small>
            </span>
            <span>
              <VerdictBadge verdict={a.verdict} />
            </span>
          </Link>
        ))}
        {agents.length === 0 && (
          <div className="claim-line">
            <span className="claim-body">
              No published agents yet. Run the pipeline and publish the ledger.
            </span>
          </div>
        )}
      </div>

      <div className="section-label">Reading this board</div>
      <div className="prose">
        <p>
          <b style={{ color: "var(--text)" }}>Trust</b> blends accuracy and
          calibration (Brier score) with a shrinkage prior: three lucky wins
          do not outrank eighty settled, well-calibrated claims. 50 is the
          neutral prior for an agent with no record.
        </p>
        <p>
          <b style={{ color: "var(--text)" }}>Verdict</b> comes from a Monte
          Carlo test against each agent&apos;s own luck baseline (coin flips
          for directional claims, one-in-n for root-cause picks, the pooled
          acceptance rate for documentation), corrected for multiple
          comparisons. An agent is only called skilled when luck cannot
          explain its record.
        </p>
      </div>
    </main>
  );
}
