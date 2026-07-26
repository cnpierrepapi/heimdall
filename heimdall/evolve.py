"""World evolution: the catalog changes, so there is always new work to judge.

A persistent world runs out of virgin work. Once every column is documented and
every PII column classified, an honest agent has nothing left to do, and the
scoring rule that only counts new artifacts would leave the leaderboard frozen
while the world quietly aged. Evolution is what keeps the queue full: each
simulated day the world changes a little, the way a real warehouse does.

Each mutation is a real change to two things at once, and never to only one:

  * the world spec, which is the truth grounding and settlement judge against, and
  * DataHub, as a delta on the affected datasets rather than a re-ingest, so every
    urn a console link or a settled claim points at survives.

A mutation is recorded in the log only after it has actually been applied. A log
that claims a change that did not happen would be worse than no log at all, since
the log is the observable history an agent is invited to predict from.

`plan_mutations` and `apply_to_spec` are pure functions of (spec, day, seed), so
the whole decision of what changes and what that does to the truth is unit
testable with no network. `emit_delta` is the only part that talks to DataHub.

Two limits keep a world recognisable over months of simulated days. Table count is
capped, and past the cap a table can only be added if one is dropped, so a world
grows into its shape and then churns inside it. And the columns that hold a world
together, keys, timestamps and anything another dataset derives from, are never
dropped, so lineage stays intact and the schema keeps telling a coherent story.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Optional

from .catalog import CatalogSpec, ColumnSpec, DatasetSpec, spec_to_world

# -- mutation kinds -----------------------------------------------------------

ADD_COLUMN = "add_column"
ADD_PII_COLUMN = "add_pii_column"
DROP_COLUMN = "drop_column"
DOC_ROT = "doc_rot"
ADD_TABLE = "add_table"

# Weighted so most days bring documentation work, the kind this deployment can
# actually settle from the artifact, and structural change is rarer than content
# change, which is how a warehouse actually behaves.
WEIGHTS: dict[str, int] = {
    ADD_COLUMN: 5,
    DOC_ROT: 4,
    ADD_PII_COLUMN: 3,
    DROP_COLUMN: 2,
    ADD_TABLE: 1,
}

TABLE_CAP = 40
DEFAULT_PER_DAY = 2

# Keys and timestamps are structural: they are what make a row identifiable and
# orderable, they are trivially self documenting, and no interesting work attaches
# to them. Never dropped, never rotted.
_STRUCTURAL_SUFFIXES = ("_id", "_at")


@dataclass
class AppliedMutation:
    """One change that actually happened, and what it touched."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    datasets: tuple[str, ...] = ()  # datasets whose aspects must be re-emitted

    @property
    def dataset(self) -> Optional[str]:
        return self.payload.get("dataset")


# -- the vocabulary new artifacts are drawn from ------------------------------
# Each carries its own gradeable truth, exactly like the generator's archetypes:
# a name a careful reader can decode, plus the concepts a good description must
# mention. Nothing here is documented on arrival; that is the point of it.

_NEW_MEASURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("refund_usd", ("refund", "returned", "usd")),
    ("tax_usd", ("tax", "levy", "usd")),
    ("discount_usd", ("discount", "promo", "usd")),
    ("net_margin_usd", ("margin", "net", "usd")),
    ("adjustment_usd", ("adjustment", "correction", "usd")),
    ("late_fee_usd", ("late", "fee", "penalty")),
    ("retry_count", ("retry", "attempt", "count")),
    ("latency_ms", ("latency", "duration", "millisecond")),
    ("source_channel", ("channel", "source", "origin")),
    ("record_version", ("version", "revision", "sequence")),
    ("settlement_status", ("settlement", "status", "state")),
    ("risk_score", ("risk", "score", "rating")),
)

_NEW_PII: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("contact_phone", ("phone", "mobile", "contact"), "phone"),
    ("billing_address", ("billing", "address", "street"), "address"),
    ("secondary_email", ("email", "secondary", "contact"), "email"),
    ("legal_full_name", ("legal", "name", "full"), "person_name"),
    ("tax_identifier", ("tax", "identifier", "national"), "national_id"),
    ("home_address", ("home", "address", "residential"), "address"),
)


def _structural(column: str) -> bool:
    return column.endswith(_STRUCTURAL_SUFFIXES)


def _has_downstream(spec: CatalogSpec, dataset: str, column: str) -> bool:
    """Does any dataset derive a column from this one?

    Only dropping cares. Rotting a description breaks no lineage, and applying
    this rule to rot would protect nearly the whole catalog, since the staging
    layer mirrors every raw column.
    """
    return any(up == dataset and up_col == column
               for ds in spec.datasets
               for up, up_col in ds.derived_from.values())


def _keywords_from_name(column: str) -> list[str]:
    """The concepts a column's own name commits it to.

    Doc rot takes the description away, not the meaning. A column called
    settlement_status still holds a settlement status, so the rotted column stays
    gradeable and the work it creates can be scored rather than merely observed.
    """
    words = [w for w in column.split("_") if len(w) > 2]
    return words or [column]


def _dataset(spec: CatalogSpec, name: str) -> Optional[DatasetSpec]:
    return next((d for d in spec.datasets if d.name == name), None)


# -- planning -----------------------------------------------------------------


def _candidates(spec: CatalogSpec, kind: str) -> list[tuple[str, Any]]:
    """Every (dataset, subject) this kind of mutation could legally act on."""
    out: list[tuple[str, Any]] = []
    if kind in (ADD_COLUMN, ADD_PII_COLUMN):
        pool = _NEW_MEASURES if kind == ADD_COLUMN else _NEW_PII
        for ds in spec.datasets:
            taken = {c.name for c in ds.columns}
            out += [(ds.name, item) for item in pool if item[0] not in taken]
    elif kind == DROP_COLUMN:
        for ds in spec.datasets:
            if len(ds.columns) <= 2:
                continue  # a table needs to keep being a table
            # a derived column may go: it mirrors an upstream that stays, and its
            # own lineage entry leaves with it. what may not go is a column
            # something further downstream still reads.
            out += [(ds.name, c.name) for c in ds.columns
                    if not _structural(c.name)
                    and not _has_downstream(spec, ds.name, c.name)]
    elif kind == DOC_ROT:
        for ds in spec.datasets:
            out += [(ds.name, c.name) for c in ds.columns
                    if c.description and not _structural(c.name)]
    elif kind == ADD_TABLE:
        if len(spec.datasets) < TABLE_CAP:
            # derive from an existing dataset, so lineage stays acyclic by
            # construction: a new table only ever points at older ones
            out += [(ds.name, None) for ds in spec.datasets
                    if ds.columns and ds.name.startswith(("raw_", "stg_"))]
    return out


def plan_mutations(spec: CatalogSpec, day: int, seed: int,
                   per_day: int = DEFAULT_PER_DAY) -> list[tuple[str, str, Any]]:
    """Decide what changes today: a list of (kind, dataset, subject).

    Deterministic in (spec, day, seed), so a day can be replayed exactly. A kind
    with no legal target is skipped rather than forced, and the draw moves on, so
    a saturated world still evolves through whatever it can still do.
    """
    rng = random.Random(f"{seed}:{day}")
    kinds = [k for k, w in WEIGHTS.items() for _ in range(w)]
    planned: list[tuple[str, str, Any]] = []
    used: set[tuple[str, Any]] = set()
    for _ in range(per_day * 6):  # bounded retries, not a while-true
        if len(planned) == per_day:
            break
        kind = rng.choice(kinds)
        options = [c for c in _candidates(spec, kind) if (kind, c) not in used]
        if not options:
            continue
        dataset, subject = rng.choice(options)
        used.add((kind, (dataset, subject)))
        planned.append((kind, dataset, subject))
    return planned


# -- application to the spec (pure) -------------------------------------------


def _apply_one(spec: CatalogSpec, kind: str, dataset: str, subject: Any,
               day: int) -> Optional[AppliedMutation]:
    ds = _dataset(spec, dataset)
    if ds is None:
        return None

    if kind in (ADD_COLUMN, ADD_PII_COLUMN):
        if kind == ADD_COLUMN:
            name, gold = subject
            pii = None
        else:
            name, gold, pii = subject
        if any(c.name == name for c in ds.columns):
            return None
        ds.columns.append(ColumnSpec(name=name, description=None,
                                     gold_keywords=list(gold), pii=pii))
        return AppliedMutation(kind, {"dataset": dataset, "column": name,
                                      "pii": pii, "day": day}, (dataset,))

    if kind == DROP_COLUMN:
        column = subject
        if not any(c.name == column for c in ds.columns):
            return None
        ds.columns = [c for c in ds.columns if c.name != column]
        ds.derived_from.pop(column, None)
        # nothing downstream may keep deriving from a column that is gone. the
        # candidate filter already protects those, so this is belt and braces
        # against a future mutation kind that forgets to.
        touched = [dataset]
        for other in spec.datasets:
            gone = [c for c, (up, up_col) in other.derived_from.items()
                    if up == dataset and up_col == column]
            for c in gone:
                other.derived_from.pop(c, None)
            if gone:
                touched.append(other.name)
        return AppliedMutation(kind, {"dataset": dataset, "column": column,
                                      "day": day}, tuple(touched))

    if kind == DOC_ROT:
        column = subject
        col = next((c for c in ds.columns if c.name == column), None)
        if col is None or not col.description:
            return None
        col.description = None  # undocumented again, so it is open work again
        if not col.gold_keywords:
            col.gold_keywords = _keywords_from_name(column)
        return AppliedMutation(kind, {"dataset": dataset, "column": column,
                                      "gold_keywords": list(col.gold_keywords),
                                      "day": day}, (dataset,))

    if kind == ADD_TABLE:
        source = ds
        name = f"mart_{source.name.split('_', 1)[-1]}_d{day}"
        if _dataset(spec, name) is not None:
            return None
        keys = [c for c in source.columns if c.name.endswith("_id")]
        if not keys:
            return None
        grain = keys[0]
        measures = [c for c in source.columns if not c.name.endswith(("_id", "_at"))][:2]
        columns = [ColumnSpec(name=grain.name,
                              description=f"Grain key carried from {source.name}.")]
        derived = {grain.name: (source.name, grain.name)}
        for c in measures:
            # carried forward undocumented on purpose: a new mart is new work
            columns.append(ColumnSpec(name=c.name, description=None,
                                      gold_keywords=list(c.gold_keywords) or [
                                          c.name.replace("_", " ").split()[0]],
                                      pii=c.pii, term=c.term))
            derived[c.name] = (source.name, c.name)
        spec.datasets.append(DatasetSpec(
            name=name, columns=columns, derived_from=derived,
            owner=source.owner, domain=source.domain,
            table_keywords=list(source.table_keywords),
        ))
        return AppliedMutation(kind, {"dataset": name, "source": source.name,
                                      "columns": [c.name for c in columns],
                                      "day": day}, (name,))

    return None


def evolve_spec(spec: CatalogSpec, day: int, seed: int,
                per_day: int = DEFAULT_PER_DAY) -> tuple[CatalogSpec, list[AppliedMutation]]:
    """Advance a world by one day of change. Returns the new spec and what happened.

    The input spec is not modified; the copy is what the caller persists. Only
    mutations that actually took effect come back, so the caller can log exactly
    what is true.
    """
    working = spec.model_copy(deep=True)
    applied: list[AppliedMutation] = []
    for kind, dataset, subject in plan_mutations(working, day, seed, per_day):
        mutation = _apply_one(working, kind, dataset, subject, day)
        if mutation is not None:
            applied.append(mutation)
    if applied:
        spec_to_world(working)  # raises if a mutation left lineage dangling
    return working, applied


# -- application to DataHub (the only part that touches the network) ----------


def emit_delta(spec: CatalogSpec, mutations: list[AppliedMutation],
               emitter: Any = None, gms_url: Optional[str] = None,
               graph: Any = None) -> int:
    """Push only the changed datasets into DataHub. Returns MCPs emitted.

    A delta, never a re-ingest: the datasets nothing happened to are not touched,
    so their urns, their agent-written descriptions and the console links that
    point at them all stay exactly where they were.

    Doc rot needs one extra step. Blanking the description in the spec rewrites
    the catalog's own copy, but a description an agent wrote earlier lives in
    DataHub's editable overlay and survives a schema write. Left alone it would
    keep the column looking documented, so the mutation would log work it had not
    actually created. The overlay entry is cleared too, and whether that succeeded
    is written back onto the mutation payload rather than assumed, so the log says
    what actually happened to the column an agent will go looking at.
    """
    from .ingest import build_mcps

    if not mutations:
        return 0
    if emitter is None:
        import os
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        emitter = DatahubRestEmitter(
            gms_url or os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"))

    changed = {name for m in mutations for name in m.datasets}
    sent = 0
    for mcp in build_mcps(spec, only=changed):
        emitter.emit(mcp)
        sent += 1

    for m in mutations:
        if m.kind != DOC_ROT:
            continue
        cleared = _clear_edited_description(spec, m.payload["dataset"],
                                           m.payload["column"], graph, gms_url)
        m.payload["overlay_cleared"] = cleared
        if cleared:
            sent += 1
    return sent


def _clear_edited_description(spec: CatalogSpec, dataset: str, column: str,
                              graph: Any = None,
                              gms_url: Optional[str] = None) -> bool:
    """Drop one column's editable description. True if anything was cleared."""
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import EditableSchemaMetadataClass
        if graph is None:
            import os
            from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
            graph = DataHubGraph(DatahubClientConfig(
                server=gms_url or os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")))
        urn = spec_to_world(spec).datasets[dataset].urn
        aspect = graph.get_aspect(urn, EditableSchemaMetadataClass)
        if aspect is None or not aspect.editableSchemaFieldInfo:
            return False
        hit = False
        for info in aspect.editableSchemaFieldInfo:
            if info.fieldPath == column and info.description:
                info.description = None
                hit = True
        if not hit:
            return False
        graph.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
        return True
    except Exception:
        # best effort: a failed overlay clear leaves the column looking
        # documented, which costs a day of work on it, not correctness
        return False
