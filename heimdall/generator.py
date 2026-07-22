"""Catalog generator: seeded, unique business contexts with checkable truth.

Every tick the engine needs a brand-new catalog so the roster of agents faces a
fresh problem and their trust accumulates over genuinely different work. But a
generated catalog is only useful if it carries ground truth the evaluators can
grade against: which columns are PII and of what type, which glossary term each
column means, which concepts a good description must mention, and who owns each
dataset. The generator owns that truth; grounding and settlement judge an agent's
output against it, so the generator is what makes an agent right or wrong.

Design:
  * A `Theme` is a business domain (ride-share, health claims, ad-tech, ...). It
    supplies a lexicon of entity archetypes. Each `ColArch` carries its own truth
    (documented vs an enricher target, gold keywords, PII type, glossary term), so
    variety never costs us checkability.
  * `generate_catalog(seed)` is deterministic: same seed -> byte-identical spec. It
    picks a theme, always includes one PII-bearing "party" entity and one
    measure-bearing "transaction" entity (so every catalog exercises the PII,
    documentation, term, and governance work kinds), assembles a raw -> staging ->
    mart lineage, assigns owners and domains, and mints a unique catalog id.
  * Layers are built raw -> staging -> marts and only ever derive from an earlier
    layer, so lineage is acyclic by construction and the `World` constructor's
    reference check passes.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Optional

from .catalog import CatalogSpec, ColumnSpec, DatasetSpec

# -- archetypes ---------------------------------------------------------------


@dataclass(frozen=True)
class ColArch:
    name: str
    description: Optional[str] = None  # None = an undocumented enricher target
    gold_keywords: tuple[str, ...] = ()  # a good description must mention one
    pii: Optional[str] = None
    term: Optional[str] = None


@dataclass(frozen=True)
class EntityArch:
    key: str  # singular, e.g. "rider"
    table: str  # raw table base, e.g. "riders"
    domain: str
    table_keyword: str
    columns: tuple[ColArch, ...]
    is_party: bool = False  # carries people/PII data
    measure: Optional[tuple[str, str]] = None  # (column name, glossary term)


@dataclass(frozen=True)
class Theme:
    name: str
    platform: str
    raw_owner: str
    analytics_owners: tuple[str, ...]
    fact_term: str  # curated glossary term the fact measure carries
    entities: tuple[EntityArch, ...] = field(default_factory=tuple)


# -- column helpers -----------------------------------------------------------


def doc(name: str, description: str, *, pii: Optional[str] = None,
        term: Optional[str] = None) -> ColArch:
    return ColArch(name, description, (), pii, term)


def undoc(name: str, gold: tuple[str, ...], *, pii: Optional[str] = None,
          term: Optional[str] = None) -> ColArch:
    return ColArch(name, None, gold, pii, term)


def party(key: str, table: str, domain: str, *,
          extra_pii: tuple[ColArch, ...] = ()) -> EntityArch:
    """A people entity: email is a PII + enricher target, country a PII trap."""
    cols = (
        doc(f"{key}_id", f"Primary key of the {key}."),
        *extra_pii,
        undoc("email", ("email",), pii="email"),
        doc("full_name", f"Full name of the {key}.", pii="person_name"),
        undoc("country_code", ("country", "iso"), term=f"{key.title()} Country"),
        doc("created_at", f"When the {key} record was created."),
    )
    return EntityArch(key, table, domain, key, cols, is_party=True)


def txn(key: str, table: str, domain: str, fk: str, measure: str,
        mkeys: tuple[str, ...], mterm: str, *, ts: str = "occurred_at") -> EntityArch:
    """A transaction/event entity carrying one measure with a glossary term."""
    cols = (
        doc(f"{key}_id", f"Primary key of the {key}."),
        doc(fk, f"Foreign key relating the {key}."),
        undoc(measure, mkeys, term=mterm),
        doc(ts, f"Timestamp of the {key}."),
    )
    return EntityArch(key, table, domain, key, cols, measure=(measure, mterm))


def ref(key: str, table: str, domain: str, columns: tuple[ColArch, ...]) -> EntityArch:
    return EntityArch(key, table, domain, key, columns)


_SSN = undoc("ssn", ("ssn", "social", "national"), pii="national_id")
_PHONE = undoc("phone", ("phone", "mobile", "contact"), pii="phone")


# -- theme library ------------------------------------------------------------


THEMES: tuple[Theme, ...] = (
    Theme(
        "ride_share", "postgres", "mobility-platform",
        ("trips-analytics", "payments-analytics"), "Completed Trip Revenue",
        (
            party("rider", "riders", "Riders"),
            txn("trip", "trips", "Trips", "rider_id", "fare_usd",
                ("fare", "amount", "usd"), "Trip Fare", ts="started_at"),
            txn("payment", "trip_payments", "Payments", "trip_id", "amount_usd",
                ("amount", "paid", "usd"), "Settled Payment Amount", ts="paid_at"),
            ref("driver", "drivers", "Drivers", (
                doc("driver_id", "Primary key of the driver."),
                doc("full_name", "Full name of the driver.", pii="person_name"),
                undoc("vehicle_class", ("vehicle", "class", "type")),
                doc("onboarded_at", "When the driver onboarded."),
            )),
        ),
    ),
    Theme(
        "health_claims", "snowflake", "clinical-platform",
        ("claims-analytics", "clinical-analytics"), "Approved Claim Amount",
        (
            party("patient", "patients", "Patients", extra_pii=(_SSN,)),
            txn("claim", "claims", "Claims", "patient_id", "claim_amount_usd",
                ("claim", "amount", "paid"), "Billed Claim Amount", ts="submitted_at"),
            txn("encounter", "encounters", "Encounters", "patient_id", "cost_usd",
                ("cost", "charge", "usd"), "Encounter Cost", ts="occurred_at"),
            ref("provider", "providers", "Providers", (
                doc("provider_id", "Primary key of the provider."),
                doc("full_name", "Name of the provider.", pii="person_name"),
                undoc("specialty", ("specialty", "practice", "field")),
                doc("registered_at", "When the provider registered."),
            )),
        ),
    ),
    Theme(
        "ad_tech", "bigquery", "adtech-platform",
        ("audience-analytics", "spend-analytics"), "Attributed Ad Revenue",
        (
            party("user", "users", "Audience"),
            txn("impression", "impressions", "Delivery", "user_id", "bid_price_usd",
                ("bid", "price", "usd"), "Winning Bid Price", ts="served_at"),
            txn("spend", "campaign_spend", "Spend", "campaign_id", "spend_usd",
                ("spend", "cost", "usd"), "Campaign Spend", ts="charged_at"),
            ref("campaign", "campaigns", "Campaigns", (
                doc("campaign_id", "Primary key of the campaign."),
                undoc("objective", ("objective", "goal", "kpi")),
                doc("launched_at", "When the campaign launched."),
            )),
        ),
    ),
    Theme(
        "retail", "postgres", "retail-platform",
        ("commerce-analytics", "customer-analytics"), "Net Order Revenue",
        (
            party("customer", "customers", "Customers"),
            txn("order", "orders", "Orders", "customer_id", "order_total_usd",
                ("total", "amount", "usd"), "Gross Order Value", ts="ordered_at"),
            txn("payment", "payments", "Payments", "order_id", "amount_usd",
                ("amount", "paid", "usd"), "Settled Payment Amount", ts="paid_at"),
            ref("product", "products", "Catalog", (
                doc("product_id", "Primary key of the product."),
                undoc("category", ("category", "class", "segment")),
                doc("listed_at", "When the product was listed."),
            )),
        ),
    ),
    Theme(
        "banking", "snowflake", "banking-platform",
        ("risk-analytics", "deposits-analytics"), "Net Deposit Balance",
        (
            party("holder", "account_holders", "Holders", extra_pii=(_SSN,)),
            txn("txn", "account_txns", "Transactions", "account_id", "amount_usd",
                ("amount", "value", "usd"), "Transaction Amount", ts="posted_at"),
            txn("fee", "account_fees", "Fees", "account_id", "fee_usd",
                ("fee", "charge", "usd"), "Charged Fee", ts="charged_at"),
            ref("account", "accounts", "Accounts", (
                doc("account_id", "Primary key of the account."),
                doc("holder_id", "Foreign key to the account holder."),
                undoc("account_type", ("account", "type", "product")),
                doc("opened_at", "When the account was opened."),
            )),
        ),
    ),
    Theme(
        "logistics", "postgres", "logistics-platform",
        ("network-analytics", "carrier-analytics"), "Delivered Freight Value",
        (
            party("shipper", "shippers", "Shippers", extra_pii=(_PHONE,)),
            txn("shipment", "shipments", "Shipments", "shipper_id", "declared_value_usd",
                ("declared", "value", "usd"), "Declared Shipment Value", ts="dispatched_at"),
            txn("charge", "freight_charges", "Billing", "shipment_id", "charge_usd",
                ("charge", "freight", "usd"), "Freight Charge", ts="billed_at"),
            ref("carrier", "carriers", "Carriers", (
                doc("carrier_id", "Primary key of the carrier."),
                undoc("service_level", ("service", "level", "tier")),
                doc("contracted_at", "When the carrier was contracted."),
            )),
        ),
    ),
    Theme(
        "streaming", "bigquery", "streaming-platform",
        ("content-analytics", "revenue-analytics"), "Recognized Subscription Revenue",
        (
            party("subscriber", "subscribers", "Subscribers"),
            txn("play", "plays", "Engagement", "subscriber_id", "watch_seconds",
                ("watch", "seconds", "duration"), "Watch Time", ts="played_at"),
            txn("subscription", "subscriptions", "Billing", "subscriber_id", "mrr_usd",
                ("mrr", "recurring", "usd"), "Monthly Recurring Revenue", ts="renewed_at"),
            ref("title", "titles", "Catalog", (
                doc("title_id", "Primary key of the title."),
                undoc("genre", ("genre", "category", "type")),
                doc("released_at", "When the title was released."),
            )),
        ),
    ),
    Theme(
        "iot", "postgres", "iot-platform",
        ("telemetry-analytics", "sites-analytics"), "Aggregated Sensor Load",
        (
            party("operator", "operators", "Operators", extra_pii=(_PHONE,)),
            txn("reading", "sensor_readings", "Telemetry", "sensor_id", "value_reading",
                ("value", "reading", "measurement"), "Sensor Reading", ts="measured_at"),
            txn("alert", "device_alerts", "Alerts", "device_id", "severity_score",
                ("severity", "score", "level"), "Alert Severity", ts="raised_at"),
            ref("device", "devices", "Devices", (
                doc("device_id", "Primary key of the device."),
                doc("site_id", "Foreign key to the site."),
                undoc("firmware", ("firmware", "version", "build")),
                doc("installed_at", "When the device was installed."),
            )),
        ),
    ),
    Theme(
        "insurance", "snowflake", "insurance-platform",
        ("actuarial-analytics", "claims-analytics"), "Earned Premium Revenue",
        (
            party("policyholder", "policyholders", "Policyholders", extra_pii=(_SSN,)),
            txn("claim", "insurance_claims", "Claims", "policy_id", "claim_paid_usd",
                ("claim", "paid", "usd"), "Paid Claim Amount", ts="settled_at"),
            txn("premium", "premiums", "Premiums", "policy_id", "premium_usd",
                ("premium", "amount", "usd"), "Written Premium", ts="billed_at"),
            ref("policy", "policies", "Policies", (
                doc("policy_id", "Primary key of the policy."),
                doc("policyholder_id", "Foreign key to the policyholder."),
                undoc("coverage_type", ("coverage", "type", "plan")),
                doc("effective_at", "When the policy took effect."),
            )),
        ),
    ),
    Theme(
        "gaming", "postgres", "gaming-platform",
        ("player-analytics", "monetization-analytics"), "Net Bookings Revenue",
        (
            party("player", "players", "Players"),
            txn("session", "sessions", "Engagement", "player_id", "session_minutes",
                ("session", "minutes", "duration"), "Session Length", ts="started_at"),
            txn("purchase", "purchases", "Monetization", "player_id", "spend_usd",
                ("spend", "amount", "usd"), "In-App Spend", ts="purchased_at"),
            ref("item", "items", "Catalog", (
                doc("item_id", "Primary key of the item."),
                undoc("rarity", ("rarity", "tier", "grade")),
                doc("added_at", "When the item was added."),
            )),
        ),
    ),
    Theme(
        "telecom", "snowflake", "telecom-platform",
        ("network-analytics", "billing-analytics"), "Recognized Service Revenue",
        (
            party("subscriber", "subscribers", "Subscribers", extra_pii=(_PHONE,)),
            txn("call", "call_records", "Usage", "subscriber_id", "duration_seconds",
                ("duration", "seconds", "minutes"), "Call Duration", ts="connected_at"),
            txn("invoice", "invoices", "Billing", "subscriber_id", "invoice_usd",
                ("invoice", "amount", "usd"), "Invoiced Amount", ts="issued_at"),
            ref("plan", "plans", "Plans", (
                doc("plan_id", "Primary key of the plan."),
                undoc("tier", ("tier", "level", "package")),
                doc("introduced_at", "When the plan was introduced."),
            )),
        ),
    ),
    Theme(
        "workforce", "snowflake", "people-platform",
        ("people-analytics", "payroll-analytics"), "Total Compensation Cost",
        (
            party("employee", "employees", "Employees",
                  extra_pii=(_SSN, undoc("base_salary_usd", ("salary", "base", "usd"),
                                         term="Base Salary"))),
            txn("payroll", "payroll_runs", "Payroll", "employee_id", "gross_pay_usd",
                ("gross", "pay", "usd"), "Gross Pay", ts="paid_at"),
            txn("expense", "expense_claims", "Expenses", "employee_id", "expense_usd",
                ("expense", "reimbursed", "usd"), "Reimbursed Expense", ts="filed_at"),
            ref("department", "departments", "Org", (
                doc("department_id", "Primary key of the department."),
                undoc("cost_center", ("cost", "center", "budget")),
                doc("formed_at", "When the department was formed."),
            )),
        ),
    ),
)


# -- generation ---------------------------------------------------------------


def _catalog_id(seed: int, theme_name: str) -> str:
    h = hashlib.sha256(f"{theme_name}:{seed}".encode()).hexdigest()[:8]
    return f"hcatalog_{h}"


def _col(c: ColArch) -> ColumnSpec:
    return ColumnSpec(
        name=c.name, description=c.description,
        gold_keywords=list(c.gold_keywords), pii=c.pii, term=c.term,
    )


def _staged(c: ColArch) -> ColumnSpec:
    """A staging column documents the raw column but keeps its PII and term truth.

    Staging is a curated layer, so its columns are documented (no enricher target)
    and carry no gold keywords; PII and glossary truth are preserved for grading.
    """
    desc = c.description or f"{c.name.replace('_', ' ')} value."
    return ColumnSpec(name=c.name, description=desc, gold_keywords=[], pii=c.pii, term=c.term)


def generate_catalog(seed: int) -> CatalogSpec:
    """Deterministically assemble a fresh, uniquely namespaced catalog."""
    rng = random.Random(seed)
    theme = rng.choice(THEMES)

    parties = [e for e in theme.entities if e.is_party]
    measures = [e for e in theme.entities if e.measure]
    chosen: list[EntityArch] = []
    if parties:
        chosen.append(rng.choice(parties))
    measure_ent = rng.choice(measures)
    chosen.append(measure_ent)
    pool = [e for e in theme.entities if e not in chosen]
    if pool:
        extra = rng.randint(1, min(2, len(pool)))
        chosen += rng.sample(pool, extra)

    datasets: list[DatasetSpec] = []

    # raw layer: archetype columns as-is (some undocumented = enricher targets)
    for ent in chosen:
        datasets.append(DatasetSpec(
            name=f"raw_{ent.table}",
            columns=[_col(c) for c in ent.columns],
            owner=theme.raw_owner,
            domain=ent.domain,
            table_keywords=[ent.table_keyword],
        ))

    # staging layer: documented mirror of raw, column-level lineage back to raw
    for ent in chosen:
        raw_name = f"raw_{ent.table}"
        datasets.append(DatasetSpec(
            name=f"stg_{ent.table}",
            columns=[_staged(c) for c in ent.columns],
            derived_from={c.name: (raw_name, c.name) for c in ent.columns},
            owner=rng.choice(theme.analytics_owners),
            domain=ent.domain,
            table_keywords=[ent.table_keyword],
        ))

    # fact mart: the measure entity's grain, curated term on the measure
    m_col, _ = measure_ent.measure
    m_stg = f"stg_{measure_ent.table}"
    key_col = f"{measure_ent.key}_id"
    datasets.append(DatasetSpec(
        name=f"fct_{measure_ent.table}",
        columns=[
            ColumnSpec(name=key_col, description=f"Grain key of the {measure_ent.key}."),
            ColumnSpec(name=m_col, description=f"Curated {m_col.replace('_', ' ')}.",
                       term=theme.fact_term),
        ],
        derived_from={key_col: (m_stg, key_col), m_col: (m_stg, m_col)},
        owner=rng.choice(theme.analytics_owners),
        domain=measure_ent.domain,
        table_keywords=[measure_ent.table_keyword],
    ))

    # dim mart: the party entity, curated PII email + country term
    if parties:
        p = chosen[0]
        p_stg = f"stg_{p.table}"
        p_key = f"{p.key}_id"
        datasets.append(DatasetSpec(
            name=f"dim_{p.table}",
            columns=[
                ColumnSpec(name=p_key, description=f"Grain key of the {p.key}."),
                ColumnSpec(name="email", description=f"Email of the {p.key}.", pii="email"),
                ColumnSpec(name="country_code", description=f"Country of the {p.key}.",
                           term=f"{p.key.title()} Country"),
            ],
            derived_from={
                p_key: (p_stg, p_key),
                "email": (p_stg, "email"),
                "country_code": (p_stg, "country_code"),
            },
            owner=rng.choice(theme.analytics_owners),
            domain=p.domain,
            table_keywords=[p.table_keyword],
        ))

    return CatalogSpec(
        catalog=_catalog_id(seed, theme.name),
        platform=theme.platform,
        theme=theme.name,
        seed=seed,
        datasets=datasets,
    )
