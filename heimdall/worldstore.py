"""Persistent worlds: the catalog stops being disposable.

The churn engine generated a brand-new catalog every tick and hard-deleted the
old one, which made every settlement that needs time impossible by construction.
A forward SLA forecast cannot settle tomorrow if tomorrow's world is a different
universe. This store fixes the world in place: a small fixed set of catalogs,
seeded once, each carrying its own day counter that the tick advances.

One engine tick advances one world by one simulated day. The store holds three
things and nothing else:

  * the world roster (id, theme, seed, day, whether it has been ingested yet),
  * each world's `CatalogSpec` on disk, which the gateway already reads by path
    through `HEIMDALL_WORLD_PATH`, and
  * the mutation log, empty until W2 fills it.

The mutation log is not bookkeeping. It is the observable history an agent reads
to make a forward prediction skill rather than a guess, so it is persisted with
the same care as the claims ledger.

World spec files live under their own directory, deliberately separate from the
churn engine's `catalogs/`. Retention garbage collection only ever globs that
older directory, so a persistent world is out of its reach by layout rather than
by a conditional that a later refactor could quietly drop.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .catalog import CatalogSpec, load_spec, save_spec
from .generator import generate_catalog

# How many worlds a deployment runs. Three gives agents visibly different
# business contexts without multiplying the DataHub entity count.
DEFAULT_WORLDS = 3

# Seeding walks upward from this seed, keeping the first catalog it sees per
# theme, so the roster is reproducible from nothing but the count.
BASE_SEED = 0
SEED_WALK_LIMIT = 500


@dataclass(frozen=True)
class WorldRecord:
    """One persistent world and where its clock has reached."""

    world_id: str  # the catalog id, and the db namespace of every URN it owns
    theme: str
    seed: int
    day: int
    created_ts: float
    ingested_ts: Optional[float] = None

    @property
    def ingested(self) -> bool:
        return self.ingested_ts is not None


@dataclass(frozen=True)
class Mutation:
    """One change made to a world on one of its days.

    Written by the evolution engine (W2) and read back by agents as the history
    that makes their forecasts checkable. Empty until then.
    """

    world_id: str
    day: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0
    mutation_id: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS worlds (
    world_id    TEXT PRIMARY KEY,
    theme       TEXT NOT NULL,
    seed        INTEGER NOT NULL,
    day         INTEGER NOT NULL DEFAULT 0,
    created_ts  REAL NOT NULL,
    ingested_ts REAL
);
CREATE TABLE IF NOT EXISTS mutations (
    mutation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id    TEXT NOT NULL,
    day         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mutations_world ON mutations (world_id, day);
"""


def seed_specs(k: int, base_seed: int = BASE_SEED,
               limit: int = SEED_WALK_LIMIT) -> list[CatalogSpec]:
    """Deterministically pick `k` catalogs, each on a different business theme.

    Walks seeds upward from `base_seed` and keeps the first catalog it meets for
    each theme, so the same count always yields the same roster in the same
    order. Distinct themes give distinct catalog ids for free, since the id
    hashes theme and seed together.
    """
    if k < 1:
        raise ValueError(f"need at least one world, got {k}")
    specs: list[CatalogSpec] = []
    seen: set[str] = set()
    for seed in range(base_seed, base_seed + limit):
        spec = generate_catalog(seed)
        theme = spec.theme or ""
        if theme in seen:
            continue
        seen.add(theme)
        specs.append(spec)
        if len(specs) == k:
            return specs
    raise ValueError(
        f"only found {len(specs)} distinct themes in {limit} seeds, needed {k}"
    )


def tick_seed(world_id: str, day: int) -> int:
    """A cast seed that is a function of the world and its day, not the clock.

    In a disposable world the wall clock was the only thing that varied, so it
    seeded the cast. A persistent world can do better: replaying day 7 of a world
    casts the same agents against the same catalog, which is what makes a live
    failure reproducible offline.
    """
    h = hashlib.sha256(f"{world_id}:{day}".encode()).hexdigest()[:15]
    return int(h, 16)


class WorldStore:
    """SQLite roster of persistent worlds, with their specs on disk beside it."""

    def __init__(self, db_path: str | Path, worlds_dir: str | Path):
        self.path = str(db_path)
        self.worlds_dir = Path(worlds_dir)
        self.worlds_dir.mkdir(parents=True, exist_ok=True)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "WorldStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- spec files --------------------------------------------------------

    def spec_path(self, world_id: str) -> str:
        return str(self.worlds_dir / f"{world_id}.json")

    def spec(self, world_id: str) -> CatalogSpec:
        return load_spec(self.spec_path(world_id))

    def write_spec(self, spec: CatalogSpec) -> str:
        """Persist a world's spec, overwriting it. W2 calls this after mutating."""
        return str(save_spec(spec, self.spec_path(spec.catalog)))

    # -- seeding -----------------------------------------------------------

    def seed(self, k: int = DEFAULT_WORLDS, base_seed: int = BASE_SEED) -> list[WorldRecord]:
        """Create the world roster once. A no-op forever after.

        Idempotent on purpose, and deliberately blind to `k` once worlds exist:
        raising the count mid-season would drop a day-zero world onto a
        leaderboard built from aged ones, so growing the roster has to be an
        explicit act rather than a side effect of a config bump.
        """
        existing = self.worlds()
        if existing:
            return existing
        now = time.time()
        for spec in seed_specs(k, base_seed=base_seed):
            self.write_spec(spec)
            self._conn.execute(
                "INSERT INTO worlds (world_id, theme, seed, day, created_ts, ingested_ts)"
                " VALUES (?,?,?,0,?,NULL)",
                (spec.catalog, spec.theme or "", spec.seed or 0, now),
            )
        self._conn.commit()
        return self.worlds()

    # -- reads -------------------------------------------------------------

    def worlds(self) -> list[WorldRecord]:
        rows = self._conn.execute(
            "SELECT * FROM worlds ORDER BY created_ts, world_id"
        ).fetchall()
        return [_row_to_world(r) for r in rows]

    def get(self, world_id: str) -> Optional[WorldRecord]:
        row = self._conn.execute(
            "SELECT * FROM worlds WHERE world_id=?", (world_id,)
        ).fetchone()
        return _row_to_world(row) if row is not None else None

    def next_world(self) -> Optional[WorldRecord]:
        """The world whose clock is furthest behind, ties broken by id.

        Round-robin that self-heals: if a tick dies after ingest but before the
        advance, that world is still the one furthest behind and gets picked
        again rather than being skipped for a full rotation.
        """
        row = self._conn.execute(
            "SELECT * FROM worlds ORDER BY day, world_id LIMIT 1"
        ).fetchone()
        return _row_to_world(row) if row is not None else None

    # -- writes ------------------------------------------------------------

    def advance(self, world_id: str) -> int:
        """Move a world's clock on by one simulated day. Returns the new day."""
        cur = self._conn.execute(
            "UPDATE worlds SET day = day + 1 WHERE world_id=?", (world_id,)
        )
        if cur.rowcount == 0:
            raise KeyError(f"no such world: {world_id}")
        self._conn.commit()
        rec = self.get(world_id)
        assert rec is not None
        return rec.day

    def mark_ingested(self, world_id: str, ts: Optional[float] = None) -> WorldRecord:
        """Record that this world now exists in DataHub, so we never re-ingest it."""
        cur = self._conn.execute(
            "UPDATE worlds SET ingested_ts=? WHERE world_id=?",
            (ts if ts is not None else time.time(), world_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"no such world: {world_id}")
        self._conn.commit()
        rec = self.get(world_id)
        assert rec is not None
        return rec

    # -- mutation log ------------------------------------------------------

    def record_mutation(self, world_id: str, day: int, kind: str,
                        payload: Optional[dict[str, Any]] = None,
                        ts: Optional[float] = None) -> Mutation:
        cur = self._conn.execute(
            "INSERT INTO mutations (world_id, day, kind, payload, ts) VALUES (?,?,?,?,?)",
            (world_id, day, kind, json.dumps(payload or {}),
             ts if ts is not None else time.time()),
        )
        self._conn.commit()
        return Mutation(
            world_id=world_id, day=day, kind=kind, payload=dict(payload or {}),
            ts=ts if ts is not None else time.time(),
            mutation_id=int(cur.lastrowid or 0),
        )

    def mutations(self, world_id: Optional[str] = None, kind: Optional[str] = None,
                  before_day: Optional[int] = None) -> list[Mutation]:
        """The history, oldest first.

        `before_day` is what an agent is allowed to see when it forecasts: the
        days that have already happened, never the one being predicted.
        """
        sql = "SELECT * FROM mutations WHERE 1=1"
        args: list[Any] = []
        if world_id is not None:
            sql += " AND world_id=?"
            args.append(world_id)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        if before_day is not None:
            sql += " AND day < ?"
            args.append(before_day)
        sql += " ORDER BY day, mutation_id"
        return [_row_to_mutation(r) for r in self._conn.execute(sql, args).fetchall()]


def _row_to_world(row: sqlite3.Row) -> WorldRecord:
    return WorldRecord(
        world_id=row["world_id"],
        theme=row["theme"],
        seed=row["seed"],
        day=row["day"],
        created_ts=row["created_ts"],
        ingested_ts=row["ingested_ts"],
    )


def _row_to_mutation(row: sqlite3.Row) -> Mutation:
    return Mutation(
        world_id=row["world_id"],
        day=row["day"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        ts=row["ts"],
        mutation_id=row["mutation_id"],
    )
