import {
  DATAHUB_URL,
  getActivity,
  getAgents,
  getFindings,
  WORK_KIND_LABEL,
} from "../../lib/data";
import { EyeSigil, SectionHead } from "../../components/ui";
import { DemoSignIn } from "../../components/DemoSignIn";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "Heimdall · judge's walkthrough",
  description:
    "A five minute guided tour of Heimdall running live on a real DataHub catalog. No install, no credentials.",
};

type Step = {
  n: string;
  title: string;
  body: string;
  live?: string;
  link?: { href: string; label: string; external?: boolean };
};

export default async function JudgePage() {
  const [agents, activity, findings] = await Promise.all([
    getAgents(),
    getActivity("showcase"),
    getFindings("showcase"),
  ]);

  const actions = activity.length;
  const stopped = activity.filter(
    (e) => e.status === "blocked" || e.status === "held"
  ).length;
  const harmful = findings.filter((f) => f.severity === "harmful").length;
  const scored = new Set(agents.map((a) => a.agent_id)).size;
  const kinds = Array.from(new Set(agents.map((a) => a.work_kind)));

  const steps: Step[] = [
    {
      n: "01",
      title: "Our catalog and our agents are already running",
      body:
        "You do not need your own catalog or agents to see this work. We stood up a real DataHub catalog, lineworld, a twelve dataset warehouse graph, and pointed several agents at it through Heimdall: an expert documenter, a rogue that violates the catalog, and a guarded agent under enforce mode. Everything below is the live result.",
      link: { href: DATAHUB_URL, label: "Open the catalog in DataHub", external: true },
    },
    {
      n: "02",
      title: "Observe: every agent call is captured at the gateway",
      body:
        "Heimdall sits in the agent to DataHub path as an MCP gateway. Every tool call, read and write, becomes an observation with the agent's identity attached. The activity feed is that live trace.",
      live: `${actions} actions traced right now`,
      link: { href: "/#activity", label: "See the live activity feed" },
    },
    {
      n: "03",
      title: "Ground: each action is checked against the catalog",
      body:
        "This is what a generic LLM tracer cannot do. Heimdall grounds each write in catalog context and emits findings that cite the exact fact broken: a description that contradicts the glossary, a column that does not exist, PII tagged out of scope, the wrong owner.",
      live: `${harmful} harmful findings on record`,
      link: { href: "/#findings", label: "See the grounded findings" },
    },
    {
      n: "04",
      title: "Score: skill separated from luck",
      body:
        "Settled outcomes accumulate into a trust score per agent per kind of work, with a heavy tail robust test so a lucky agent does not outrank a proven one. The leaderboard ranks by earned trust.",
      live: `${scored} agents scored across ${kinds.length} work kinds`,
      link: { href: "/#leaderboard", label: "See the leaderboard" },
    },
    {
      n: "05",
      title: "Govern: harmful writes are stopped in flight",
      body:
        "Under enforce mode the gateway grades each write before it reaches the catalog: a trusted, clean write auto accepts, a questionable one is held for a steward, a catalog violating one is blocked. Held and blocked calls show in the feed with the reason.",
      live: `${stopped} writes stopped in flight`,
      link: { href: "/#activity", label: "Find the held and blocked rows" },
    },
    {
      n: "06",
      title: "Write back: trust lands in the catalog",
      body:
        "Heimdall writes the verdict back into DataHub as a badge on each authored dataset, structured trust properties in the sidebar, and a per agent dossier under Documents. The next agent inherits not just the metadata but the reliability of whoever wrote it. Sign in to your own DataHub as any user who can view metadata to see it.",
      link: { href: DATAHUB_URL, label: "Open DataHub to see the badges", external: true },
    },
    {
      n: "07",
      title: "The tenant control plane",
      body:
        "The console is public, but this is a multi tenant product. Enter the read only demo tenant below to see a private view: its own activity, its own findings, and a private agent whose scores are hidden from everyone else at the database, not just in the UI. Sign out and that private agent disappears.",
    },
  ];

  return (
    <main className="wrap judge">
      <section className="judge-hero">
        <p className="eyebrow">
          <EyeSigil size={18} /> Judge's walkthrough
        </p>
        <h1>
          See Heimdall run, live,
          <br />
          <em>in about five minutes.</em>
        </h1>
        <p className="hero-sub">
          No install and no credentials. Everything on this tour is live from a real DataHub
          catalog and real agents we already pointed through Heimdall. Follow the steps, or
          jump straight in.
        </p>
        <div className="judge-start">
          <a className="cta cta-solid" href="/">
            Open the live console
          </a>
          <DemoSignIn label="Enter the demo tenant" redirect="/" variant="ghost" />
        </div>
        <div className="judge-snapshot" aria-label="Live snapshot">
          <div className="snap">
            <span className="snap-n">{actions}</span>
            <span className="snap-l">actions traced</span>
          </div>
          <div className="snap">
            <span className="snap-n">{scored}</span>
            <span className="snap-l">agents scored</span>
          </div>
          <div className="snap snap-bad">
            <span className="snap-n">{harmful}</span>
            <span className="snap-l">harmful findings</span>
          </div>
          <div className="snap snap-held">
            <span className="snap-n">{stopped}</span>
            <span className="snap-l">writes stopped</span>
          </div>
        </div>
      </section>

      <SectionHead index="01" title="The pipeline, step by step" note="each step links to live proof" />
      <ol className="judge-steps">
        {steps.map((s) => (
          <li className="judge-step panel" key={s.n}>
            <span className="judge-step-n">{s.n}</span>
            <div className="judge-step-body">
              <h3>{s.title}</h3>
              <p>{s.body}</p>
              <div className="judge-step-foot">
                {s.live && <span className="judge-live">{s.live}</span>}
                {s.link &&
                  (s.link.external ? (
                    <a
                      className="judge-link"
                      href={s.link.href}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {s.link.label} <span aria-hidden="true">{"↗"}</span>
                    </a>
                  ) : (
                    <a className="judge-link" href={s.link.href}>
                      {s.link.label} <span aria-hidden="true">{"→"}</span>
                    </a>
                  ))}
                {s.n === "07" && <DemoSignIn label="Enter the demo tenant" redirect="/" />}
              </div>
            </div>
          </li>
        ))}
      </ol>

      <SectionHead
        index="02"
        title="Bring your own catalog and agents"
        note="when you are ready to go hands on"
      />
      <div className="panel judge-byo">
        <p>
          Everything above runs on our catalog so you can evaluate without any setup. When you
          want to point Heimdall at your own DataHub and your own agents, the scoring core runs
          from a clean clone with zero services, and the full live loop seeds a catalog, runs
          agents through the gateway, and writes trust back. Both are step by step in the setup
          guide.
        </p>
        <div className="judge-byo-links">
          <a
            className="cta cta-ghost"
            href="https://github.com/cnpierrepapi/heimdall/blob/master/SETUP.md"
            target="_blank"
            rel="noreferrer"
          >
            Read the setup guide <span aria-hidden="true">{"↗"}</span>
          </a>
          <a
            className="judge-link"
            href="https://github.com/cnpierrepapi/heimdall"
            target="_blank"
            rel="noreferrer"
          >
            View the source <span aria-hidden="true">{"↗"}</span>
          </a>
        </div>
      </div>
    </main>
  );
}
