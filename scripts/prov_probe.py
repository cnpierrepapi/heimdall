"""Live proof for the timeline provenance reader (P1).

Reconstructs the demo catalog's real change history straight from the DataHub
Timeline API and summarizes it: how many changes, by (actor, work kind), and how
many fields show the add-then-cleared shape the settlement layer reads as a
revert. Run on the box: DATAHUB_GMS_URL=http://localhost:8080 python scripts/prov_probe.py
"""

import os
from collections import Counter, defaultdict

from heimdall.provenance import lineworld_dataset_urns, read_changes

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")


def main() -> None:
    urns = lineworld_dataset_urns()
    changes = read_changes(GMS, urns)
    print(f"datasets queried: {len(urns)}")
    print(f"changes reconstructed: {len(changes)}")

    by_actor_kind = Counter((c.actor.split(":")[-1], c.work_kind) for c in changes)
    print("=== (actor, work_kind) counts ===")
    for (actor, kind), n in sorted(by_actor_kind.items()):
        print(f"  {actor:22s} {kind:12s} {n}")

    seq = defaultdict(list)
    for c in changes:
        if c.work_kind in ("column_doc", "table_doc"):
            seq[(c.dataset_urn, c.field)].append(c)
    reverts = [
        (k, v)
        for k, v in seq.items()
        if any(x.is_clear for x in v) and any(x.operation == "ADD" for x in v)
    ]
    print(f"=== fields with an ADD later cleared (real revert shape): {len(reverts)} ===")
    for (ds, field), evs in reverts[:6]:
        tail = ds.split(",")[1]
        trail = " -> ".join(
            e.operation + ("(empty)" if e.is_clear else "") for e in evs
        )
        print(f"  {tail}.{field}: {trail}")


if __name__ == "__main__":
    main()
