import {
  CHECK_LABEL,
  datahubLink,
  datasetName,
  getActivity,
  getAgents,
  getFindings,
  getViewer,
  relativeTime,
  verdictTone,
  WORK_KIND_LABEL,
} from "../../../lib/data";
import {
  OpTag,
  SectionHead,
  StatusMark,
  TrustRing,
  VerdictChip,
} from "../../../components/ui";

export const dynamic = "force-dynamic";

export default async function AgentPage({
  params,
}: {
  params: Promise<{ agent: string }>;
}) {
  const { agent } = await params;
  const agentId = decodeURIComponent(agent);
  const viewer = await getViewer();
  const [agents, activity, findings] = await Promise.all([
    getAgents(),
    getActivity(viewer.owner),
    getFindings(viewer.owner),
  ]);

  const kinds = agents.filter((a) => a.agent_id === agentId);
  const acts = activity.filter((e) => e.agent_id === agentId);
  const finds = findings.filter((f) => f.agent_id === agentId);

  const settled = kinds.reduce((s, a) => s + (a.n_settled ?? 0), 0);
  const best = kinds.reduce<number | null>(
    (m, a) => (a.trust != null && (m == null || a.trust > m) ? a.trust : m),
    null
  );
  const skilled = kinds.filter((a) => a.verdict === "skilled").length;

  return (
    <main className="wrap">
      <section className="report-head">
        <a className="crumb" href="/">
          <span aria-hidden="true">{"←"}</span> all agents
        </a>
        <p className="eyebrow">Trust report</p>
        <h1 className="report-id">{agentId}</h1>
        <p className="hero-sub">
          Reliability earned through observed work, grounded against the catalog, and settled
          skill against luck. Nothing on this page is self-reported.
        </p>
        <dl className="report-vitals">
          <div>
            <dt>work kinds scored</dt>
            <dd>{kinds.length}</dd>
          </div>
          <div>
            <dt>claims settled</dt>
            <dd>{settled}</dd>
          </div>
          <div>
            <dt>skilled verdicts</dt>
            <dd>{skilled}</dd>
          </div>
          <div>
            <dt>best trust</dt>
            <dd>{best != null ? Math.round(best) : "··"}</dd>
          </div>
        </dl>
      </section>

      <SectionHead index="01" title="Trust by work kind" note="settled skill, not vibes" />
      {kinds.length === 0 && <div className="panel empty">No scored work yet.</div>}
      <div className="score-grid">
        {kinds.map((a) => {
          const priv = a.visibility === "private";
          return (
            <article className="panel score-card" key={a.work_kind}>
              <TrustRing
                value={a.trust}
                tone={verdictTone(a.verdict)}
                size={104}
              />
              <div className="score-body">
                <h3 className="score-kind">{WORK_KIND_LABEL[a.work_kind] ?? a.work_kind}</h3>
                <VerdictChip verdict={a.verdict} />
                <dl className="score-stats">
                  <div>
                    <dt>settled</dt>
                    <dd>{a.n_settled ?? 0}</dd>
                  </div>
                  {a.win_rate != null && (
                    <div>
                      <dt>win rate</dt>
                      <dd>{Math.round(a.win_rate * 100)}%</dd>
                    </div>
                  )}
                  {a.brier != null && (
                    <div>
                      <dt>brier</dt>
                      <dd>{a.brier.toFixed(2)}</dd>
                    </div>
                  )}
                </dl>
                {priv && (
                  <span className="lock-tag">
                    <span aria-hidden="true">{"◈"}</span> private to your tenant
                  </span>
                )}
              </div>
            </article>
          );
        })}
      </div>

      {finds.length > 0 && (
        <>
          <SectionHead
            index="02"
            title="Grounded findings"
            note="each one cites the catalog"
          />
          <div className="findings-grid">
            {finds.map((f) => (
              <article className={`panel finding sev-${f.severity}`} key={f.id}>
                <header className="finding-head">
                  <span
                    className={`chip ${f.severity === "harmful" ? "chip-blocked" : "chip-warn"}`}
                  >
                    {f.severity}
                  </span>
                  <span className="finding-check">
                    {CHECK_LABEL[f.check_type] ?? f.check_type}
                  </span>
                  <time className="finding-time" dateTime={f.ts}>
                    {relativeTime(f.ts)}
                  </time>
                </header>
                <p className="finding-reason">{f.reason}</p>
                {f.entity_urn && (
                  <footer className="finding-foot">
                    <a
                      className="ds-link"
                      href={datahubLink(f.entity_urn)}
                      target="_blank"
                      rel="noreferrer"
                      title="Open this dataset in DataHub"
                    >
                      {datasetName(f.entity_urn)}
                      {f.column ? `.${f.column}` : ""} <span aria-hidden="true">{"↗"}</span>
                    </a>
                  </footer>
                )}
              </article>
            ))}
          </div>
        </>
      )}

      <SectionHead
        index={finds.length > 0 ? "03" : "02"}
        title="Recent activity"
        note="from the live gateway trace"
      />
      <section className="panel feed">
        {acts.length === 0 && <div className="empty">No recent activity in the trace window.</div>}
        <ol className="feed-list">
          {acts.map((e) => {
            const ds = e.entities && e.entities[0] ? datasetName(e.entities[0]) : null;
            const quarantined = e.status === "blocked" || e.status === "held";
            return (
              <li className={`feed-row st-${e.status}`} key={e.id}>
                <div className="feed-main">
                  <StatusMark status={e.status} />
                  <OpTag op={e.op} />
                  <span className="feed-tool">{e.tool}</span>
                  {ds && <span className="feed-ds">{ds}</span>}
                  <span className="feed-meta">
                    {e.status === "ok" && e.latency_ms != null && (
                      <span className="feed-latency">{e.latency_ms}ms</span>
                    )}
                    <time dateTime={e.ts}>{relativeTime(e.ts)}</time>
                  </span>
                </div>
                {quarantined && e.result_summary && (
                  <p className="feed-summary">{e.result_summary}</p>
                )}
              </li>
            );
          })}
        </ol>
      </section>
    </main>
  );
}
