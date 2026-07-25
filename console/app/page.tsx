import {
  AgentRow,
  ActivityRow,
  FindingRow,
  CHECK_LABEL,
  datahubLink,
  datasetName,
  getActivity,
  getAgents,
  getFindings,
  getViewer,
  relativeTime,
  verdictTone,
  splitAgents,
  WORK_KIND_LABEL,
} from "../lib/data";
import {
  ConductStrip,
  OpTag,
  ScoreStateTag,
  SectionHead,
  StatusMark,
  TrustRing,
  VerdictChip,
} from "../components/ui";

export const dynamic = "force-dynamic";

const PIPELINE = [
  { n: "01", name: "observe", desc: "every call at the gateway" },
  { n: "02", name: "ground", desc: "against catalog context" },
  { n: "03", name: "score", desc: "skill, not luck" },
  { n: "04", name: "select", desc: "best agent per work kind" },
  { n: "05", name: "govern", desc: "accept, hold, block" },
  { n: "06", name: "write back", desc: "trust into the catalog" },
];

function Activity({ activity }: { activity: ActivityRow[] }) {
  return (
    <section className="panel feed" aria-label="Live activity">
      {activity.length === 0 && <div className="empty">No activity observed yet.</div>}
      <ol className="feed-list">
        {activity.map((e) => {
          const ds = e.entities && e.entities[0] ? datasetName(e.entities[0]) : null;
          const quarantined = e.status === "blocked" || e.status === "held";
          return (
            <li className={`feed-row st-${e.status}`} key={e.id}>
              <div className="feed-main">
                <StatusMark status={e.status} />
                <a className="agent-id" href={`/agents/${encodeURIComponent(e.agent_id)}`}>
                  {e.agent_id}
                </a>
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
  );
}

// Only agents whose work this deployment can settle appear here. The rest are
// listed by Observed below, with the reason they carry no score.
function Leaderboard({ agents }: { agents: AgentRow[] }) {
  const kinds = Array.from(new Set(agents.map((a) => a.work_kind)));
  return (
    <section className="panel board" aria-label="Global leaderboard">
      {kinds.length === 0 && <div className="empty">No scored agents yet.</div>}
      {kinds.map((kind) => {
        const rows = agents
          .filter((a) => a.work_kind === kind)
          .sort((x, y) => (y.trust ?? 0) - (x.trust ?? 0));
        const selectedId = rows.find((r) => r.visibility !== "private")?.agent_id;
        return (
          <div className="board-group" key={kind}>
            <div className="board-kind">
              <span className="board-kind-name">{WORK_KIND_LABEL[kind] ?? kind}</span>
              <span className="board-kind-note">selected globally</span>
            </div>
            <ol className="board-list">
              {rows.map((a, i) => {
                const priv = a.visibility === "private";
                const selected = a.agent_id === selectedId;
                return (
                  <li
                    className={`board-row${selected ? " is-selected" : ""}${priv ? " is-private" : ""}`}
                    key={a.agent_id + a.work_kind}
                  >
                    <span className="board-rank">{i + 1}</span>
                    <div className="board-who">
                      <a
                        className="agent-id"
                        href={`/agents/${encodeURIComponent(a.agent_id)}`}
                      >
                        {a.agent_id}
                      </a>
                      <span className="board-sub">
                        {selected && <span className="selected-tag">selected</span>}
                        {a.n_settled != null && (
                          <span>{a.n_settled} settled</span>
                        )}
                        {priv && (
                          <span className="lock-tag" title="Private to your tenant. Only your tenant can see this agent's scores.">
                            <span aria-hidden="true">{"◈"}</span> private to your tenant
                          </span>
                        )}
                      </span>
                    </div>
                    <div className="board-score">
                      <TrustRing value={a.trust} tone={verdictTone(a.verdict)} />
                      <VerdictChip verdict={a.verdict} />
                    </div>
                  </li>
                );
              })}
            </ol>
          </div>
        );
      })}
    </section>
  );
}

// Agents carrying no score, and why. Two different reasons live here and the
// difference matters: one is waiting for evidence, the other will never have
// any. Neither is ranked, because a rank claims a comparison we have not earned.
// What they do have is a conduct record, which needs no settlement to be true.
function Observed({ agents }: { agents: AgentRow[] }) {
  if (agents.length === 0) return null;
  const order = { unscoreable: 0, insufficient: 1 } as Record<string, number>;
  const rows = [...agents].sort(
    (x, y) =>
      (order[x.score_state ?? ""] ?? 2) - (order[y.score_state ?? ""] ?? 2) ||
      (y.n_actions ?? 0) - (x.n_actions ?? 0),
  );
  return (
    <section className="panel observed" aria-label="Observed, not scored">
      <p className="observed-lede">
        Heimdall watches these agents and grounds every action they take. It does not
        rank them: a trust score is only honest where something settles the claim.
      </p>
      <ul className="observed-list">
        {rows.map((a) => (
          <li className="observed-row" key={a.agent_id + a.work_kind}>
            <div className="observed-who">
              <a className="agent-id" href={`/agents/${encodeURIComponent(a.agent_id)}`}>
                {a.agent_id}
              </a>
              <span className="observed-kind">
                {WORK_KIND_LABEL[a.work_kind] ?? a.work_kind}
              </span>
              <ScoreStateTag state={a.score_state} />
            </div>
            {a.score_reason && <p className="observed-why">{a.score_reason}</p>}
            <ConductStrip agent={a} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function Findings({ findings }: { findings: FindingRow[] }) {
  return (
    <div className="findings-grid">
      {findings.length === 0 && (
        <div className="panel empty">No findings on record.</div>
      )}
      {findings.map((f) => (
        <article className={`panel finding sev-${f.severity}`} key={f.id}>
          <header className="finding-head">
            <span className={`chip ${f.severity === "harmful" ? "chip-blocked" : "chip-warn"}`}>
              {f.severity}
            </span>
            <span className="finding-check">{CHECK_LABEL[f.check_type] ?? f.check_type}</span>
            <time className="finding-time" dateTime={f.ts}>
              {relativeTime(f.ts)}
            </time>
          </header>
          <p className="finding-reason">{f.reason}</p>
          <footer className="finding-foot">
            <a className="agent-id" href={`/agents/${encodeURIComponent(f.agent_id)}`}>
              {f.agent_id}
            </a>
            {f.entity_urn && (
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
            )}
          </footer>
        </article>
      ))}
    </div>
  );
}

export default async function Home() {
  const viewer = await getViewer();
  const [agents, activity, findings] = await Promise.all([
    getAgents(),
    getActivity(viewer.owner),
    getFindings(viewer.owner),
  ]);

  const { ranked, observed } = splitAgents(agents);
  const distinctAgents = new Set(agents.map((a) => a.agent_id)).size;
  const harmful = findings.filter((f) => f.severity === "harmful").length;
  const stopped = activity.filter((e) => e.status === "blocked" || e.status === "held").length;

  return (
    <main className="wrap">
      {viewer.isTenant && (
        <div className="tenant-banner">
          <span className="tenant-dot" aria-hidden="true" />
          Signed in as <strong>{viewer.email}</strong>. Showing your tenant{" "}
          <span className="mono">{viewer.owner}</span>: your agents, activity, and findings,
          scoped by row level security.
        </div>
      )}
      <section className="hero">
        <p className="eyebrow">
          <span className="live-dot" aria-hidden="true" />
          Agent observability + trust control plane for DataHub
        </p>
        <h1>
          Agents are writing to your catalog.
          <br />
          <em>Heimdall is watching.</em>
        </h1>
        <p className="hero-sub">
          DataHub feeds your AI agents context, but nobody watches the agents back. Heimdall
          stands in the agent to catalog path: it observes every action, grounds each claim in
          catalog context, scores skill against luck, governs writes in flight, and writes
          trust back where your team can see it.
        </p>
        <div className="hero-ctas">
          <a className="cta cta-solid" href="/judge">
            Judges start here <span aria-hidden="true">{"→"}</span>
          </a>
          <a className="cta cta-ghost" href="#activity">
            Explore the live console
          </a>
        </div>
        <ol className="bridge" aria-label="The Heimdall pipeline">
          {PIPELINE.map((s) => (
            <li className="bridge-node" key={s.n}>
              <span className="bridge-n">{s.n}</span>
              <span className="bridge-name">{s.name}</span>
              <span className="bridge-desc">{s.desc}</span>
            </li>
          ))}
        </ol>
      </section>

      <section className="statband" aria-label="Live stats">
        <div className="stat">
          <span className="stat-n">{distinctAgents}</span>
          <span className="stat-l">agents observed</span>
        </div>
        <div className="stat">
          <span className="stat-n">{activity.length}</span>
          <span className="stat-l">actions traced</span>
        </div>
        <div className="stat stat-bad">
          <span className="stat-n">{harmful}</span>
          <span className="stat-l">harmful findings</span>
        </div>
        <div className="stat stat-held">
          <span className="stat-n">{stopped}</span>
          <span className="stat-l">writes stopped in flight</span>
        </div>
      </section>

      <div className="columns">
        <div>
          <SectionHead
            index="01"
            id="activity"
            title="Live activity"
            note="every agent call, observed at the gateway"
          />
          <Activity activity={activity} />
        </div>
        <div>
          <SectionHead
            index="02"
            id="leaderboard"
            title="Leaderboard"
            note="trust by work kind"
          />
          <Leaderboard agents={ranked} />
        </div>
      </div>

      {observed.length > 0 && (
        <>
          <SectionHead
            index="03"
            id="observed"
            title="Observed, not scored"
            note="watched and grounded, but not ranked"
          />
          <Observed agents={observed} />
        </>
      )}

      <SectionHead
        index={observed.length > 0 ? "04" : "03"}
        id="findings"
        title="Catalog-grounded findings"
        note="each verdict cites the catalog itself"
      />
      <Findings findings={findings} />
    </main>
  );
}
