import {
  AgentRow,
  ActivityRow,
  FindingRow,
  datahubLink,
  datasetName,
  getActivity,
  getAgents,
  getFindings,
  verdictTone,
  WORK_KIND_LABEL,
} from "../lib/data";

export const revalidate = 30;

function trustColor(t: number | null) {
  if (t == null) return "var(--muted)";
  if (t >= 60) return "var(--good)";
  if (t < 50) return "var(--bad)";
  return "var(--warn)";
}

function ago(ts: string) {
  const d = (Date.now() - new Date(ts).getTime()) / 1000;
  if (d < 90) return `${Math.max(1, Math.round(d))}s ago`;
  if (d < 5400) return `${Math.round(d / 60)}m ago`;
  if (d < 129600) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}

function Leaderboard({ agents }: { agents: AgentRow[] }) {
  const kinds = Array.from(new Set(agents.map((a) => a.work_kind)));
  return (
    <div className="card" id="leaderboard">
      <div className="hd">
        <h3>Global leaderboard</h3>
        <span className="sub">best agent per work kind, across all public agents</span>
      </div>
      {kinds.map((kind) => {
        const rows = agents
          .filter((a) => a.work_kind === kind)
          .sort((x, y) => (y.trust ?? 0) - (x.trust ?? 0));
        return (
          <div key={kind}>
            <div className="kindhdr">{WORK_KIND_LABEL[kind] ?? kind}</div>
            {rows.map((a) => {
              const priv = a.visibility === "private";
              const tone = verdictTone(a.verdict);
              return (
                <div className="row" key={a.agent_id + a.work_kind}>
                  <a className="link mono" href={`/agents/${encodeURIComponent(a.agent_id)}`}>
                    {a.agent_id}
                  </a>
                  {priv && <span className="access">private · request access</span>}
                  <div className="right" style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div className="trustbar" title="trust score">
                      <span
                        style={{
                          width: `${a.trust ?? 50}%`,
                          background: trustColor(a.trust),
                        }}
                      />
                    </div>
                    <span className="mono" style={{ width: 34, textAlign: "right" }}>
                      {a.trust?.toFixed(0) ?? "–"}
                    </span>
                    <span className={`badge ${tone}`}>{a.verdict ?? "unrated"}</span>
                  </div>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

function Activity({ activity }: { activity: ActivityRow[] }) {
  return (
    <div className="card" id="activity">
      <div className="hd">
        <h3>Live activity</h3>
        <span className="sub">every agent call, observed at the gateway</span>
      </div>
      {activity.length === 0 && <div className="row muted">No activity yet.</div>}
      {activity.map((e) => {
        const ds = e.entities && e.entities[0] ? datasetName(e.entities[0]) : "";
        return (
          <div className="row" key={e.id}>
            <span className={`dot ${e.status}`} />
            <span className="mono">{e.agent_id}</span>
            <span className={`op ${e.op}`}>{e.op}</span>
            <span className="mono muted">{e.tool}</span>
            {ds && <span className="mono dim">{ds}</span>}
            <span className="right dim mono" style={{ fontSize: 12 }}>
              {e.status === "blocked" && <span className="badge bad">blocked</span>}
              {e.status === "held" && <span className="badge hold">held</span>}
              {e.latency_ms != null && e.status === "ok" ? ` ${e.latency_ms}ms · ` : " "}
              {ago(e.ts)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function Findings({ findings }: { findings: FindingRow[] }) {
  return (
    <div className="card" id="findings">
      <div className="hd">
        <h3>Catalog-grounded findings</h3>
        <span className="sub">what a generic LLM tracer cannot see</span>
      </div>
      {findings.length === 0 && <div className="row muted">No findings.</div>}
      {findings.map((f) => (
        <div className="finding" key={f.id}>
          <div className="top">
            <span className={`badge ${f.severity === "harmful" ? "bad" : "warn"}`}>
              {f.severity}
            </span>
            <span className="mono muted">{f.check_type}</span>
            <span className="mono">{f.agent_id}</span>
            {f.entity_urn && (
              <a className="right link mono" href={datahubLink(f.entity_urn)} target="_blank" rel="noreferrer">
                {datasetName(f.entity_urn)}
                {f.column ? `.${f.column}` : ""} ↗
              </a>
            )}
          </div>
          <div className="reason">{f.reason}</div>
        </div>
      ))}
    </div>
  );
}

export default async function Home() {
  const [agents, activity, findings] = await Promise.all([
    getAgents(),
    getActivity(),
    getFindings(),
  ]);

  const distinctAgents = new Set(agents.map((a) => a.agent_id)).size;
  const harmful = findings.filter((f) => f.severity === "harmful").length;
  const stopped = activity.filter((e) => e.status === "blocked" || e.status === "held").length;

  return (
    <main>
      <div className="wrap">
        <section className="hero">
          <h1>
            Your agents are writing to your catalog.{" "}
            <span className="grad">Heimdall watches them.</span>
          </h1>
          <p>
            DataHub feeds your AI agents context. Nobody watches the agents back. Heimdall sits
            in the agent-to-DataHub path, observes every action, grounds it in your catalog,
            scores each agent&apos;s reliability, and blocks the bad writes before they land.
          </p>
          <div className="pills">
            <span className="pill">observe</span>
            <span className="pill">ground in catalog context</span>
            <span className="pill">score skill vs luck</span>
            <span className="pill">govern in flight</span>
            <span className="pill">write trust back</span>
          </div>
        </section>

        <div className="statrow">
          <div className="stat">
            <div className="n">{distinctAgents}</div>
            <div className="l">agents observed</div>
          </div>
          <div className="stat">
            <div className="n">{activity.length}</div>
            <div className="l">actions traced</div>
          </div>
          <div className="stat">
            <div className="n" style={{ color: "var(--bad)" }}>{harmful}</div>
            <div className="l">harmful findings</div>
          </div>
          <div className="stat">
            <div className="n" style={{ color: "var(--hold)" }}>{stopped}</div>
            <div className="l">writes stopped in flight</div>
          </div>
        </div>

        <div className="grid2">
          <Activity activity={activity} />
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            <Leaderboard agents={agents} />
          </div>
        </div>

        <div className="section">
          <Findings findings={findings} />
        </div>

        <footer>
          Heimdall · agent observability &amp; trust control plane for DataHub · showcase catalog
          <span className="dim"> lineworld</span>. Public data, read only. Live catalog at{" "}
          <a className="link" href="https://datahub.onenept.com" target="_blank" rel="noreferrer">
            datahub.onenept.com
          </a>
          .
        </footer>
      </div>
    </main>
  );
}
