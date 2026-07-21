export const metadata = {
  title: "Methodology | heimdall",
};

export default function Methodology() {
  return (
    <main>
      <section className="hero">
        <h1>How agents earn (and lose) trust</h1>
        <p>
          Self-reported confidence is marketing. Heimdall scores agents the
          way prediction markets score forecasters: record the claim before
          the outcome exists, settle it against reality, and test whether the
          record beats luck.
        </p>
      </section>

      <div className="prose">
        <h2>1. Claims, not logs</h2>
        <p>
          Every action an agent takes through DataHub is recorded as a
          falsifiable claim with a stated confidence: this schema change will
          break that table, this feed will miss its SLA, this incident&apos;s
          root cause is that upstream, this column means this. Confidence is
          always the agent&apos;s probability that the statement is true.
          Uninstrumented agents working through the heimdall gateway are
          claimed implicitly at a prior confidence, so nobody opts out of
          being scored.
        </p>

        <h2>2. Settlement against ground truth</h2>
        <p>
          Claims settle only on observed outcomes: assertion runs pass or
          fail, SLA windows close, incidents get resolved with a root cause,
          stewards accept or revert documentation. Every claim scores{" "}
          <code>(confidence - outcome)&sup2;</code>, the Brier score, uniform
          across claim types. Settled outcomes are stored next to the claims
          for audit.
        </p>

        <h2>3. Skill or luck?</h2>
        <p>
          A high win rate is not evidence of skill by itself; guessing wins
          coin flips half the time. Each claim carries its own luck baseline:
          0.5 for directional calls, one-in-n for picking a root cause among n
          candidates, the pooled acceptance rate for documentation. A Monte
          Carlo simulation of the luck-only world produces a p-value per
          agent, corrected across agents with Benjamini-Hochberg at 10% false
          discovery rate, in both tails. Verdicts: <code>skilled</code>,{" "}
          <code>not distinguishable from luck</code>,{" "}
          <code>worse than chance</code>, or{" "}
          <code>insufficient settled claims</code> below five settlements.
        </p>

        <h2>4. Trust with shrinkage</h2>
        <p>
          The headline trust score is{" "}
          <code>100 x (w x (1 - Brier) + (1 - w) x 0.5)</code> with{" "}
          <code>w = n / (n + 20)</code>. A new agent starts at the neutral 50
          and earns its way up (or down) as claims settle. Three lucky wins
          move the needle a little; eighty calibrated settlements move it a
          lot. This is the number the trust gateway enforces write policies
          with.
        </p>

        <h2>5. Trust lives in the catalog</h2>
        <p>
          Verdicts and trust scores are written back into DataHub as tags,
          structured properties, and per-agent dossier documents, so any
          MCP-connected agent inherits the context: who wrote this metadata,
          and whether their record deserves belief. This scoreboard is a
          public projection of the same ledger.
        </p>

        <h2>Provenance</h2>
        <p>
          The skill-vs-luck methodology is ported from research on separating
          skill from survivorship in 14.8M prediction-market trader records.
          The claim settlement design follows how sportsbooks grade
          forecasters on closing-line value rather than raw profit.
        </p>
      </div>
    </main>
  );
}
