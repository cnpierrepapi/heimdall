import Link from "next/link";
import type { Metadata } from "next";
import CalibrationSpark from "@/components/CalibrationSpark";
import VerdictBadge from "@/components/VerdictBadge";
import { getAgents, getCalibration, type AgentRow } from "@/lib/data";
import { dossierUrl } from "@/lib/dossiers";

export const revalidate = 60;

export const metadata: Metadata = {
  title: "heimdall | agent registry",
  description:
    "Pick data agents by settled record: live trust, verdict, calibration, and the one-liner that puts each agent behind the trust gateway.",
};

const TRUST_FLOOR = 55;

function trustColor(trust: number): string {
  if (trust >= 60) return "var(--skilled)";
  if (trust >= 50) return "var(--luck)";
  return "var(--harmful)";
}

function fmt(v: number | null, digits = 2): string {
  return v === null || v === undefined ? "-" : v.toFixed(digits);
}

function gatewayLine(a: AgentRow): string {
  return `HEIMDALL_AGENT_ID=${a.agent_id} HEIMDALL_POLICY=enforce HEIMDALL_MIN_TRUST=${TRUST_FLOOR} python -m heimdall.gateway`;
}

export default async function Registry() {
  const agents = await getAgents();
  const calibrations = await Promise.all(
    agents.map((a) => getCalibration(a.agent_id))
  );

  return (
    <main>
      <section className="hero">
        <h1>The agent registry</h1>
        <p>
          Every agent below is picked the way it should be picked: by its
          settled record, not its README. Trust, verdict, and calibration come
          from claims settled against reality, and each card carries the
          one-liner that puts the agent behind the trust gateway on your own
          catalog. Agents below the floor of {TRUST_FLOOR} have their writes
          blocked until the record recovers.
        </p>
      </section>

      <div className="section-label">
        {agents.length} agents on the ledger
      </div>

      <div className="reg-grid">
        {agents.map((a, i) => {
          const dossier = dossierUrl(a.agent_id);
          const blocked = a.trust < TRUST_FLOOR;
          return (
            <div key={a.agent_id} className={`card reg-card${blocked ? " blocked" : ""}`}>
              <div className="reg-head">
                <div>
                  <div className="agent-name">{a.agent_id}</div>
                  <div className="agent-model">{a.model_id ?? "unknown model"}</div>
                </div>
                <VerdictBadge verdict={a.verdict} />
              </div>

              <div className="reg-body">
                <div>
                  <div className="reg-trust" style={{ color: trustColor(a.trust) }}>
                    {a.trust.toFixed(1)}
                  </div>
                  <div className="reg-trust-label">
                    trust
                    {blocked
                      ? ` (below floor ${TRUST_FLOOR}: writes blocked)`
                      : ""}
                  </div>
                  <div className="reg-stats">
                    <div>
                      <span className="k">settled</span>
                      <span className="v">
                        {a.n_settled}
                        <small> / {a.n_total}</small>
                      </span>
                    </div>
                    <div>
                      <span className="k">win rate</span>
                      <span className="v">{fmt(a.win_rate)}</span>
                    </div>
                    <div>
                      <span className="k">brier</span>
                      <span className="v">{fmt(a.brier_mean, 3)}</span>
                    </div>
                  </div>
                </div>
                <CalibrationSpark bins={calibrations[i]} />
              </div>

              <div className="reg-snippet">{gatewayLine(a)}</div>

              <div className="reg-links">
                <Link href={`/agents/${encodeURIComponent(a.agent_id)}`}>
                  full record &rarr;
                </Link>
                {dossier && (
                  <a href={dossier} target="_blank" rel="noreferrer">
                    dossier in DataHub {"↗"}
                  </a>
                )}
              </div>
            </div>
          );
        })}
        {agents.length === 0 && (
          <div className="card">
            <div className="claim-line">
              <span className="claim-body">
                No published agents yet. Run the pipeline and publish the
                ledger.
              </span>
            </div>
          </div>
        )}
      </div>

      <div className="section-label">why a registry</div>
      <div className="prose">
        <p>
          When agents are picked by settled record, instrumented agents win
          selection: an agent that routes its writes through the gateway
          accrues a public track record, and an agent that refuses has no
          record to stand on. That is the flywheel. The registry is the
          selection surface, the gateway is the instrument, and the catalog
          itself carries the verdicts.
        </p>
        <p>
          To register an agent, run its MCP traffic through the gateway with
          the line on its card. Claims, settlement, and scoring follow
          automatically; the record is published here and written back into
          DataHub. See <Link href="/proof">the proof</Link> for every step
          with live artifacts, or <Link href="/methodology">the method</Link>{" "}
          for how the scores are computed.
        </p>
      </div>
    </main>
  );
}
