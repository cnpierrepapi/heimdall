"""Serialisable catalog specs: the portable form of a World.

A `World` is a live Python object the evaluators ground against. A `CatalogSpec`
is its JSON-serialisable twin: the single source of truth for one catalog
instance that can be written to disk, handed to the gateway (so it grounds
in-flight policy against that exact catalog), ingested into DataHub under a
unique namespace, and later reconstructed for settlement.

The round-trip `world -> spec -> json -> spec -> world` must preserve everything
the evaluators read (column names, descriptions, gold keywords, PII types,
glossary terms, ownership, domain, lineage) and the URN namespace, or grounding
verdicts would drift between the moment an action is judged and the moment it is
settled. The generator (T2) produces `CatalogSpec`s directly; every instance
carries its own `catalog` id so its DataHub URNs never collide and a whole
instance can be garbage-collected by that id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .grounding import WorldCatalogContext
from .simulator.world import PLATFORM, Column, Dataset, World


class ColumnSpec(BaseModel):
    """One column and the ground truth the evaluators grade against it."""

    name: str
    description: Optional[str] = None  # None = undocumented (an enricher target)
    gold_keywords: list[str] = Field(default_factory=list)
    pii: Optional[str] = None  # PII type ("email", "person_name"); None = not PII
    term: Optional[str] = None  # glossary term this column means; None = none


class DatasetSpec(BaseModel):
    """One dataset: its columns, column-level lineage, and governance truth."""

    name: str
    columns: list[ColumnSpec] = Field(default_factory=list)
    # column name -> [upstream dataset name, upstream column name]
    derived_from: dict[str, tuple[str, str]] = Field(default_factory=dict)
    landing_hour: Optional[int] = None
    sla_hour: Optional[int] = None
    owner: Optional[str] = None
    domain: Optional[str] = None
    table_keywords: list[str] = Field(default_factory=list)


class CatalogSpec(BaseModel):
    """A whole catalog instance, uniquely namespaced by its `catalog` id."""

    catalog: str  # unique db namespace, e.g. "hcatalog_ab12cd34" (or "lineworld")
    platform: str = PLATFORM
    theme: Optional[str] = None
    seed: Optional[int] = None
    datasets: list[DatasetSpec] = Field(default_factory=list)

    # -- derived views over the datasets (convenience for the console / generator)

    def glossary_terms(self) -> list[str]:
        return sorted(
            {c.term for d in self.datasets for c in d.columns if c.term}
        )

    def owners(self) -> list[str]:
        return sorted({d.owner for d in self.datasets if d.owner})

    def domains(self) -> list[str]:
        return sorted({d.domain for d in self.datasets if d.domain})


# -- conversions --------------------------------------------------------------


def world_to_spec(
    world: World,
    catalog: str,
    platform: str = PLATFORM,
    theme: Optional[str] = None,
    seed: Optional[int] = None,
) -> CatalogSpec:
    """Serialise a live World into a portable spec under a named namespace."""
    datasets = [
        DatasetSpec(
            name=d.name,
            columns=[
                ColumnSpec(
                    name=c.name,
                    description=c.description,
                    gold_keywords=list(c.gold_keywords),
                    pii=c.pii,
                    term=c.term,
                )
                for c in d.columns
            ],
            derived_from={col: (up, up_col) for col, (up, up_col) in d.derived_from.items()},
            landing_hour=d.landing_hour,
            sla_hour=d.sla_hour,
            owner=d.owner,
            domain=d.domain,
            table_keywords=list(d.table_keywords),
        )
        for d in world.datasets.values()
    ]
    return CatalogSpec(
        catalog=catalog, platform=platform, theme=theme, seed=seed, datasets=datasets
    )


def spec_to_world(spec: CatalogSpec) -> World:
    """Materialise a spec into a live World the evaluators can ground against.

    Every dataset carries the spec's platform + catalog id, so its URN is unique
    to this instance and `WorldCatalogContext` resolves it unchanged.
    """
    datasets = [
        Dataset(
            name=d.name,
            columns=[
                Column(
                    name=c.name,
                    description=c.description,
                    gold_keywords=tuple(c.gold_keywords),
                    pii=c.pii,
                    term=c.term,
                )
                for c in d.columns
            ],
            derived_from={col: (up, up_col) for col, (up, up_col) in d.derived_from.items()},
            landing_hour=d.landing_hour,
            sla_hour=d.sla_hour,
            owner=d.owner,
            domain=d.domain,
            table_keywords=tuple(d.table_keywords),
            platform=spec.platform,
            db=spec.catalog,
        )
        for d in spec.datasets
    ]
    return World(datasets)


# -- persistence --------------------------------------------------------------


def save_spec(spec: CatalogSpec, path: str | Path) -> Path:
    """Write a spec to disk as JSON (the gateway reads it via HEIMDALL_WORLD_PATH)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return p


def load_spec(path: str | Path) -> CatalogSpec:
    """Read a spec back from disk."""
    return CatalogSpec.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_world(path: str | Path) -> World:
    """Read a spec from disk and materialise its World."""
    return spec_to_world(load_spec(path))


def world_catalog_context(path: str | Path) -> WorldCatalogContext:
    """Load a spec file into a catalog context the gateway can ground against."""
    return WorldCatalogContext(load_world(path))
