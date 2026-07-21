import {
  datahubLink,
  datasetName,
  getActivity,
  getAgents,
  getFindings,
  verdictTone,
  WORK_KIND_LABEL,
} from "../../../lib/data";

export const revalidate = 30;

export default async function AgentPage({
  params,
}: {
  params: Promise<{ agent: string }>;
}) {
  const { agent } = await params;
  const agentId = decodeURIComponent(agent);
  const [agents, activity, findings] = await Promise.all([
    getAgents(),
    getActivity(),
    getFindings(),
  ]);

  const kinds = agents.filter((a) => a.agent_id === agentId);
  const acts = activity.filter((e) => e.agent_id === agentId);
  const finds = findings.filter((f) => f.agent_id === agentId);

  return (
    <main>
      <div className="wrap">
        <section className="hero" style={{ paddingBottom: 24 }}>
          <a className="link" href="/">← all agents</a>
          <h1 style={{ fontSize: 34, marginTop: 16 }}>
            <span className="mono">{agentId}</span>
          </h1>
          <p>
            Reliability earned through observed work, grounded against the catalog and settled
            skill-vs-luck. Nothing here is self-reported.
          </p>
        </section>

        <div className="section">
          <h2>Trust by work kind</h2>
          <div className="card">
            {kinds.length === 0 && <div className="row muted">No scored work yet.</div>}
            {kinds.map((a) => (
              <div className="row" key={a.work_kind}>
                <span>{WORK_KIND_LABEL[a.work_kind] ?? a.work_kind}</span>
                {a.visibility === "private" && <span className="access">private</span>}
                <div className="right" style={{ display: "flex", gap: 12, alignItems: "center" }}>
                  <span className="dim mono">{a.n_settled ?? 0} settled</span>
                  <span className="mono">{a.trust?.toFixed(1) ?? "–"}/100</span>
                  <span className={`badge ${verdictTone(a.verdict)}`}>{a.verdict ?? "unrated"}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {finds.length > 0 && (
          <div className="section">
            <h2>Grounded findings</h2>
            <div className="card">
              {finds.map((f) => (
                <div className="finding" key={f.id}>
                  <div className="top">
                    <span className={`badge ${f.severity === "harmful" ? "bad" : "warn"}`}>
                      {f.severity}
                    </span>
                    <span className="mono muted">{f.check_type}</span>
                    {f.entity_urn && (
                      <a className="right link mono" href={datahubLink(f.entity_urn)} target="_blank" rel="noreferrer">
                        {datasetName(f.entity_urn)}{f.column ? `.${f.column}` : ""} ↗
                      </a>
                    )}
                  </div>
                  <div className="reason">{f.reason}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="section">
          <h2>Recent activity</h2>
          <div className="card">
            {acts.length === 0 && <div className="row muted">No activity.</div>}
            {acts.map((e) => (
              <div className="row" key={e.id}>
                <span className={`dot ${e.status}`} />
                <span className={`op ${e.op}`}>{e.op}</span>
                <span className="mono muted">{e.tool}</span>
                {e.entities && e.entities[0] && (
                  <span className="mono dim">{datasetName(e.entities[0])}</span>
                )}
                <span className="right dim mono" style={{ fontSize: 12 }}>
                  {e.status !== "ok" ? e.status : e.latency_ms != null ? `${e.latency_ms}ms` : ""}
                </span>
              </div>
            ))}
          </div>
        </div>

        <footer>
          <a className="link" href="/">Heimdall console</a> · showcase catalog lineworld
        </footer>
      </div>
    </main>
  );
}
