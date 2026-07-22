"""T3 live proof: ingest a generated catalog into real DataHub, then GC it.

Run on the box against the live GMS:
    ~/fresh-e2e/v/bin/python scripts/ingest_gc_proof.py

Generates a fresh unique catalog, ingests it, reads schema + lineage back through
the graph client (proving it really landed under its own namespace), then
hard-deletes the whole instance and confirms every dataset is gone. Exits non-zero
if any check fails.
"""

from __future__ import annotations

import os
import sys
import time

from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import SchemaMetadataClass, UpstreamLineageClass

from heimdall.generator import generate_catalog
from heimdall.ingest import catalog_dataset_urns, hard_delete_catalog, ingest_spec

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))

    seed = int(time.time())
    spec = generate_catalog(seed)
    urns = catalog_dataset_urns(spec)
    graph = DataHubGraph(DatahubClientConfig(server=GMS))
    print(f"catalog={spec.catalog} theme={spec.theme} platform={spec.platform} "
          f"datasets={len(spec.datasets)} seed={seed}")

    # 1) ingest
    n = ingest_spec(spec, gms_url=GMS)
    check("ingest emitted MCPs", n > 0, f"{n} MCPs")

    # 2) schema read back for the first raw dataset, exact columns
    raw = next(d for d in spec.datasets if d.name.startswith("raw_"))
    raw_urn = next(u for u in urns if f".{raw.name}," in u)
    schema = graph.get_aspect(raw_urn, SchemaMetadataClass)
    got_cols = sorted(f.fieldPath for f in schema.fields) if schema else []
    want_cols = sorted(c.name for c in raw.columns)
    check("schema landed with exact columns", got_cols == want_cols,
          f"{raw.name}: {got_cols}")

    # 3) column-level lineage on a derived dataset, upstreams in-namespace
    derived = next((d for d in spec.datasets if d.derived_from), None)
    if derived is not None:
        d_urn = next(u for u in urns if f".{derived.name}," in u)
        lin = graph.get_aspect(d_urn, UpstreamLineageClass)
        ok = bool(lin and lin.upstreams and all(spec.catalog in up.dataset for up in lin.upstreams))
        fine = bool(lin and lin.fineGrainedLineages)
        check("column-level lineage landed in namespace", ok and fine,
              f"{derived.name}: {len(lin.upstreams) if lin else 0} upstreams, "
              f"{len(lin.fineGrainedLineages) if lin and lin.fineGrainedLineages else 0} fine-grained")

    # 4) every dataset exists pre-delete
    pre = {u: graph.exists(u) for u in urns}
    check("all datasets exist before GC", all(pre.values()),
          f"{sum(pre.values())}/{len(urns)}")

    # 5) hard delete the whole instance
    results = hard_delete_catalog(spec, graph=graph)
    failed = [r for r in results if not r.ok]
    check("hard delete reported ok for all", not failed,
          "; ".join(f"{r.urn.split(',')[1]}:{r.error}" for r in failed) or "all ok")

    # 6) confirm gone (GMS read, not search, so no ES lag)
    post = {u: graph.exists(u) for u in urns}
    still = [u for u, e in post.items() if e]
    check("all datasets gone after GC", not still,
          f"{len(still)} still present" if still else "none remain")

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
