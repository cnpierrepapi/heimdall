import Image from "next/image";
import Link from "next/link";
import type { Metadata } from "next";
import CredsHint from "@/components/proof/CredsHint";
import LiveStatus from "@/components/proof/LiveStatus";
import { GATEWAY_TRANSCRIPT } from "@/components/proof/transcript";
import {
  DATAHUB_URL,
  describeClaim,
  getAgent,
  getClaims,
} from "@/lib/data";
import { DOSSIER_URNS } from "@/lib/dossiers";

export const revalidate = 60;

export const metadata: Metadata = {
  title: "heimdall | the proof",
  description:
    "Every claim on this page links to the artifact it produced inside a live, public DataHub instance. Screenshots stand in if the instance is down.",
};

const RAW_ORDERS_URN =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,lineworld.raw_orders,PROD)";

const links = {
  rawOrders: `${DATAHUB_URL}/dataset/${encodeURIComponent(RAW_ORDERS_URN)}/Schema`,
  search: `${DATAHUB_URL}/search?query=lineworld`,
  tag: `${DATAHUB_URL}/tag/${encodeURIComponent("urn:li:tag:heimdall-unproven")}`,
  dossiers: Object.entries(DOSSIER_URNS).map(([agent, urn]) => ({
    agent,
    url: `${DATAHUB_URL}/document/${encodeURIComponent(urn)}`,
  })),
};

function DeepLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className="btn ghost small">
      {children} {"↗"}
    </a>
  );
}

export default async function Proof() {
  const [enricherClaims, rogue] = await Promise.all([
    getClaims("enricher-live"),
    getAgent("rogue-agent"),
  ]);
  const enrichmentClaims = enricherClaims
    .filter((c) => c.claim_type === "enrichment" && c.settled_ts !== null)
    .slice(0, 6);

  return (
    <main>
      <section className="hero">
        <h1>The proof</h1>
        <p>
          Every claim on this page is paired with the artifact it produced
          inside a real DataHub instance, running in public. The deep links
          open that instance: sign in once with the shared read login and
          DataHub drops you exactly on the linked page. The screenshots were
          captured from it and stand on their own if it is ever down.
        </p>
        <div className="proof-toolbar">
          <LiveStatus />
          <span className="creds">
            read login for {DATAHUB_URL.replace("https://", "")}:{" "}
            <b>judge / ledger-judge-2026</b>
          </span>
        </div>
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>01</b> agents did real catalog work
        </div>
        <div className="beat">
          <div className="beat-text">
            <h2 className="display">
              The enricher documented ten undocumented columns.
            </h2>
            <p className="prose">
              It found them itself through the DataHub MCP server, read the
              upstream lineage as evidence, wrote each description with{" "}
              <code>update_description</code>, and recorded each write as a
              claim. The descriptions in the screenshot carry DataHub&apos;s
              own <i>(edited)</i> marker: they live in the editable metadata
              layer, written by the agent, standing in the catalog right now.
            </p>
            <div className="deep-links">
              <DeepLink href={links.rawOrders}>open raw_orders in DataHub</DeepLink>
            </div>
            <CredsHint />
            {enrichmentClaims.length > 0 && (
              <div className="card claims-mini">
                {enrichmentClaims.map((c) => (
                  <div key={c.claim_id} className="claim-line">
                    <span className={`claim-mark ${c.correct ? "right" : "wrong"}`}>
                      {c.correct ? "RIGHT" : "WRONG"}
                    </span>
                    <span className="claim-body">
                      {describeClaim(c.claim_type, c.prediction, c.entity_urn)}{" "}
                      <small>conf {c.confidence.toFixed(2)}</small>
                    </span>
                  </div>
                ))}
                <div className="claim-line">
                  <span className="claim-body">
                    <small>
                      settled enrichment claims from the published ledger,{" "}
                      <Link href="/agents/enricher-live">full record</Link>
                    </small>
                  </span>
                </div>
              </div>
            )}
          </div>
          <figure className="evidence">
            <Image
              src="/proof/dataset-raw-orders.png"
              alt="raw_orders dataset page in DataHub with agent-written column descriptions, verdict badge, and heimdall trust properties in the sidebar"
              width={1440}
              height={1000}
            />
            <figcaption>
              raw_orders in the live instance: verdict badge beside the name,
              author agent / trust / verdict in the sidebar, agent-written
              descriptions marked (edited)
            </figcaption>
          </figure>
        </div>
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>02</b> trust surfaces where people already look
        </div>
        <div className="beat">
          <div className="beat-text">
            <h2 className="display">
              The verdict follows the asset through search.
            </h2>
            <p className="prose">
              Heimdall writes trust back as DataHub structured properties
              with the settings that make them visible: the verdict renders as
              a badge wherever the asset renders, the trust score sits in the
              sidebar summary, and <b>Heimdall author verdict</b> appears as
              a native search facet. A steward filtering search results by
              agent verdict is using plain DataHub; nothing was bolted onto
              the frontend.
            </p>
            <div className="deep-links">
              <DeepLink href={links.search}>search lineworld</DeepLink>
              <DeepLink href={links.tag}>the heimdall-unproven tag</DeepLink>
            </div>
            <CredsHint />
          </div>
          <figure className="evidence">
            <Image
              src="/proof/search-lineworld.png"
              alt="DataHub search results for lineworld showing verdict badges on datasets and a Heimdall author verdict search facet"
              width={1440}
              height={1000}
            />
            <figcaption>
              search in the live instance: badges on results, Heimdall
              author verdict as a filter facet in the left rail
            </figcaption>
          </figure>
        </div>
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>03</b> the verdicts stay honest
        </div>
        <div className="beat">
          <div className="beat-text">
            <h2 className="display">
              A perfect score that still reads &quot;luck&quot;.
            </h2>
            <p className="prose">
              The enricher went ten for ten, yet its dossier says{" "}
              <b>not distinguishable from luck</b>. That is the test working,
              not failing: its luck baseline is the pooled steward acceptance
              rate, and when every submission in the pool is accepted, a
              perfect score is exactly what luck predicts. The verdict only
              turns skilled when the agent beats the going standard. A trust
              system that flatters its agents is worthless; this one publishes
              the p-value.
            </p>
            <p className="prose">
              Each agent gets a dossier published into DataHub&apos;s document
              store, so the next agent that searches the catalog inherits the
              record.
            </p>
            <div className="deep-links">
              {links.dossiers.map((d) => (
                <DeepLink key={d.agent} href={d.url}>
                  {d.agent} dossier
                </DeepLink>
              ))}
            </div>
            <CredsHint />
          </div>
          <figure className="evidence">
            <Image
              src="/proof/dossier-enricher.png"
              alt="Agent trust dossier for enricher-live rendered as a DataHub document with verdict, trust score, and settled metrics table"
              width={1440}
              height={1000}
            />
            <figcaption>
              the enricher-live dossier as a native DataHub document: win rate
              1.000, expected wins under the luck baseline 10.000, p-value 1.0
            </figcaption>
          </figure>
        </div>
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>04</b> the gateway enforces the record
        </div>
        <div className="beat">
          <div className="beat-text">
            <h2 className="display">
              A rogue agent, blocked by its own settled claims.
            </h2>
            <p className="prose">
              This transcript is the live end-to-end run against the public
              instance: an uninstrumented agent writes through the gateway,
              the write is recorded as an implicit claim, a steward reverts
              it, the claim settles wrong, and the agent&apos;s next write is
              rejected before it touches the catalog. Reads keep working; the
              block is on writes only. Exit code 0, every check asserted.
            </p>
            <div className="deep-links">
              {rogue && (
                <Link
                  href={`/agents/${encodeURIComponent(rogue.agent_id)}`}
                  className="btn ghost small"
                >
                  rogue-agent on the board
                </Link>
              )}
              <DeepLink href="https://github.com/cnpierrepapi/heimdall/blob/main/scripts/gateway_e2e.py">
                the e2e source
              </DeepLink>
            </div>
          </div>
          <div className="transcript">
            {GATEWAY_TRANSCRIPT.split("\n").map((line, i) => (
              <div
                key={i}
                className={
                  line.startsWith("PASS") || line.startsWith("ALL")
                    ? "pass"
                    : undefined
                }
              >
                {line || " "}
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="lsec">
        <div className="lsec-label">
          <b>05</b> check it yourself
        </div>
        <div className="beat">
          <div className="beat-text">
            <h2 className="display">Nothing here asks to be believed.</h2>
            <p className="prose">
              Open the demo instance with the judge login and click anything
              on this page. Or clone the repo and run the whole pipeline
              against your own DataHub: ingest, agents, settlement, writeback,
              gateway. The scoreboard you are reading is generated from the
              same ledger.
            </p>
            <div className="proof-toolbar">
              <LiveStatus />
            </div>
            <div className="deep-links">
              <DeepLink href={DATAHUB_URL}>open the demo DataHub</DeepLink>
              <Link href="/board" className="btn ghost small">
                the live board
              </Link>
              <Link href="/methodology" className="btn ghost small">
                the method
              </Link>
            </div>
            <CredsHint />
          </div>
          <div className="term">
            <div>
              <span className="p">$</span>git clone
              https://github.com/cnpierrepapi/heimdall
            </div>
            <div>
              <span className="p">$</span>cd heimdall && examples/run_all.sh
            </div>
            <div className="c"># ingest, 4 agents, settlement, writeback, gateway e2e</div>
          </div>
        </div>
      </section>
    </main>
  );
}
