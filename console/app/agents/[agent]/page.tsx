import Link from "next/link";
import { notFound } from "next/navigation";
import CalibrationChart from "@/components/CalibrationChart";
import VerdictBadge from "@/components/VerdictBadge";
import {
  describeClaim,
  getAgent,
  getCalibration,
  getClaims,
} from "@/lib/data";

export const revalidate = 60;

function fmt(v: number | null, digits = 3): string {
  return v === null || v === undefined ? "-" : v.toFixed(digits);
}

export default async function AgentPage({
  params,
}: {
  params: Promise<{ agent: string }>;
}) {
  const { agent: raw } = await params;
  const agentId = decodeURIComponent(raw);
  const [agent, calibration, claims] = await Promise.all([
    getAgent(agentId),
    getCalibration(agentId),
    getClaims(agentId),
  ]);
  if (!agent) notFound();

  const luckGap =
    agent.expected_null_wins !== null && agent.wins !== null
      ? agent.wins - agent.expected_null_wins
      : null;

  return (
    <main>
      <Link href="/board" className="crumb">
        &larr; all agents
      </Link>
      <section className="hero">
        <h1 style={{ fontFamily: "var(--mono)", fontSize: 26 }}>
          {agent.agent_id}
        </h1>
        <p style={{ marginTop: 10 }}>
          <VerdictBadge verdict={agent.verdict} long />
          <span
            style={{
              marginLeft: 12,
              color: "var(--faint)",
              fontFamily: "var(--mono)",
              fontSize: 12,
            }}
          >
            {agent.model_id ?? "unknown model"}
          </span>
        </p>
      </section>

      <div className="detail-grid">
        <div className="card stat">
          <div className="k">Trust score</div>
          <div className="v">
            {agent.trust.toFixed(1)}
            <small> / 100</small>
          </div>
        </div>
        <div className="card stat">
          <div className="k">Settled</div>
          <div className="v">
            {agent.n_settled}
            <small> of {agent.n_total} claims</small>
          </div>
        </div>
        <div className="card stat">
          <div className="k">Win rate</div>
          <div className="v">{fmt(agent.win_rate, 2)}</div>
        </div>
        <div className="card stat">
          <div className="k">Mean Brier</div>
          <div className="v">{fmt(agent.brier_mean)}</div>
        </div>
        <div className="card stat">
          <div className="k">Calibration error</div>
          <div className="v">{fmt(agent.ece)}</div>
        </div>
        <div className="card stat">
          <div className="k">Wins vs luck</div>
          <div className="v">
            {agent.wins ?? "-"}
            <small>
              {" "}
              vs {fmt(agent.expected_null_wins, 1)} expected
              {luckGap !== null ? ` (${luckGap >= 0 ? "+" : ""}${luckGap.toFixed(1)})` : ""}
            </small>
          </div>
        </div>
        <div className="card stat">
          <div className="k">p-value vs luck</div>
          <div className="v">{fmt(agent.p_value)}</div>
        </div>
        <div className="card stat">
          <div className="k">q-value (FDR)</div>
          <div className="v">{fmt(agent.q_value)}</div>
        </div>
      </div>

      <div className="two-col" style={{ marginTop: 40 }}>
        <div>
          <div className="section-label" style={{ margin: "0 0 14px" }}>
            Calibration
          </div>
          <div className="card" style={{ padding: 18 }}>
            {calibration.length > 0 ? (
              <CalibrationChart bins={calibration} />
            ) : (
              <p style={{ color: "var(--faint)", fontSize: 13, padding: 8 }}>
                No settled claims to plot yet.
              </p>
            )}
            <p
              style={{
                color: "var(--faint)",
                fontSize: 12,
                lineHeight: 1.6,
                marginTop: 10,
              }}
            >
              Dots on the dashed line are perfectly calibrated: when this agent
              says 80%, it should be right 80% of the time. Dots below the line
              mean overconfidence.
            </p>
          </div>
        </div>
        <div>
          <div className="section-label" style={{ margin: "0 0 14px" }}>
            Settled claims
          </div>
          <div className="card">
            {claims.map((c) => (
              <div className="claim-line" key={c.claim_id}>
                <span
                  className={`claim-mark ${
                    c.correct === null ? "open" : c.correct ? "right" : "wrong"
                  }`}
                >
                  {c.correct === null ? "OPEN" : c.correct ? "RIGHT" : "WRONG"}
                </span>
                <span className="claim-body">
                  {describeClaim(c.claim_type, c.prediction, c.entity_urn)}
                </span>
                <span className="claim-conf">p={c.confidence.toFixed(2)}</span>
              </div>
            ))}
            {claims.length === 0 && (
              <div className="claim-line">
                <span className="claim-body">No claims recorded.</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </main>
  );
}
