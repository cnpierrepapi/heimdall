"""Live proof for reconstruction (P2).

Reads the demo catalog's real change history, reconstructs settled claims per
(actor, work kind), and prints the scored record the skill engine produces from
it. Run on the box:
  DATAHUB_GMS_URL=http://localhost:8080 python scripts/recon_probe.py
"""

import os
import time
from pathlib import Path

from heimdall.claims import ClaimStore
from heimdall.provenance import lineworld_dataset_urns, read_changes
from heimdall.reconstruct import load_into_store
from heimdall.skill import skill_report

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
DB = os.environ.get("RECON_DB", "recon.db")
# The survival window is a config knob: how long a write must stand unchallenged
# to count as accepted. The default is 7 days; a young demo catalog uses a
# shorter one. Real timestamps either way; only the acceptance horizon changes.
SURVIVAL_DAYS = int(os.environ.get("RECON_SURVIVAL_DAYS", "7"))


def main() -> None:
    Path(DB).unlink(missing_ok=True)
    now = time.time()
    changes = read_changes(GMS, lineworld_dataset_urns())
    if changes:
        span = (max(c.ts for c in changes) - min(c.ts for c in changes)) / 86400
        age = (now - max(c.ts for c in changes)) / 86400
        print(f"history spans {span:.2f} days; newest change is {age:.2f} days old")
    print(f"survival window: {SURVIVAL_DAYS} day(s)")
    with ClaimStore(DB) as store:
        summary = load_into_store(store, changes, now_ts=now, survival_days=SURVIVAL_DAYS)
        print(f"changes read: {len(changes)}")
        print(f"reconstructed: {summary}")
        report = skill_report(store, min_settled=1)
        print("=== per (actor/work_kind) reconstructed record ===")
        header = f"{'writer profile':34s} {'n':>3s} {'set':>4s} {'win':>4s} "
        header += f"{'rate':>5s} {'trust':>6s}  verdict"
        print(header)
        for agent_id in sorted(report):
            r = report[agent_id]
            rate = r.get("win_rate")
            rate_s = f"{rate:.2f}" if rate is not None else "  - "
            print(
                f"{agent_id:34s} {r['n_total']:>3d} {r['n_settled']:>4d} "
                f"{r['wins']:>4d} {rate_s:>5s} {r['trust']:>6.1f}  {r['verdict']}"
            )


if __name__ == "__main__":
    main()
