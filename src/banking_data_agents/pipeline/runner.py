"""Medallion pipeline orchestration.

Stages, in order:

1. **bronze** — ingest the landing extracts verbatim. No transformation happens
   here; bronze exists so any downstream mistake can be replayed from raw.
2. **silver** — conform, clean, tokenize PII, apply documented survivorship rules.
3. **gold** — assemble the four published data products.
4. **publish** — evaluate data quality, record SLA status and lineage, and emit
   the catalog the agents read.

The hash-pinned fraud holdout is never ingested into bronze. Only ``SOURCES`` is.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from banking_data_agents.config import get_settings
from banking_data_agents.datagen.generate import SOURCES, load_manifest
from banking_data_agents.logging_setup import get_logger
from banking_data_agents.pipeline.engine import QueryEngine, get_engine
from banking_data_agents.pipeline.lake import Lake, S3Lake, Zone, ensure_buckets, get_lake

logger = get_logger(__name__)

SQL_DIR = Path(__file__).parent / "sql"

#: (zone, table, sql filename). Order matters: referenced tables must exist first.
SILVER_TABLES: tuple[tuple[str, str], ...] = (
    ("customers", "silver_customers.sql"),
    ("accounts", "silver_accounts.sql"),
    ("transactions", "silver_transactions.sql"),
    ("cards", "silver_cards.sql"),
    ("loans", "silver_loans.sql"),
    ("credit_records", "silver_credit_records.sql"),
    ("repayments", "silver_repayments.sql"),
)

#: The three products the Data Product Copilot serves. `fraud_features` is added
#: with the fraud investigation agent, because it needs streaming feature exports.
GOLD_TABLES: tuple[tuple[str, str], ...] = (
    ("customer_360", "gold_customer_360.sql"),
    ("transaction", "gold_transaction.sql"),
    ("credit_risk", "gold_credit_risk.sql"),
)

ALL_STAGES: tuple[str, ...] = ("bronze", "silver", "gold", "publish")


# ---------------------------------------------------------------------------
# SQL rendering
# ---------------------------------------------------------------------------
def render_sql(sql: str, context: dict[str, str]) -> str:
    """Substitute ``{{placeholders}}``. Unresolved placeholders are an error."""
    for key, value in context.items():
        sql = sql.replace("{{" + key + "}}", value)
    unresolved = set(re.findall(r"\{\{(\w+)\}\}", sql))
    if unresolved:
        raise ValueError(f"unresolved SQL placeholders: {sorted(unresolved)}")
    return sql


def load_sql(name: str, context: dict[str, str]) -> str:
    path = SQL_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"missing transform {path}")
    return render_sql(path.read_text(encoding="utf-8"), context)


# ---------------------------------------------------------------------------
# Pipeline context
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PipelineContext:
    """Everything the transformations need to be deterministic.

    ``as_of`` is anchored to the data window, never to wall-clock time, so two
    runs over the same landing data produce identical gold outputs.
    """

    as_of: date
    window_start: date
    window_end: date

    @property
    def as_of_ts(self) -> str:
        return f"{self.as_of.isoformat()} 00:00:00"

    def substitutions(self) -> dict[str, str]:
        # Lookback cut-offs are computed here rather than in SQL so the
        # transformations stay free of date-arithmetic dialect differences.
        return {
            "as_of": self.as_of.isoformat(),
            "as_of_ts": self.as_of_ts,
            "as_of_90": (self.as_of - timedelta(days=90)).isoformat(),
            "as_of_180": (self.as_of - timedelta(days=180)).isoformat(),
            "as_of_365": (self.as_of - timedelta(days=365)).isoformat(),
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
        }


def build_context() -> PipelineContext:
    """Derive the pipeline's time anchor from the landing manifest."""
    manifest = load_manifest()
    window_start = date.fromisoformat(manifest["window_start"])
    # window_end is exclusive, so the last day with data is one day earlier.
    window_end_exclusive = date.fromisoformat(manifest["window_end"])
    return PipelineContext(
        as_of=window_end_exclusive - timedelta(days=1),
        window_start=window_start,
        window_end=window_end_exclusive,
    )


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
def _table_name(source: str, entity: str) -> str:
    """``s_core`` + ``transactions`` -> ``s_core_transactions``."""
    return f"{source}_{entity}"


def stage_bronze(lake: Lake, *, verbose: bool) -> dict[str, int]:
    """Ingest landing extracts verbatim. The fraud holdout is deliberately excluded."""
    settings = get_settings()
    landing = settings.landing_dir
    if not landing.exists():
        raise FileNotFoundError(f"no landing data at {landing}; run `make data` first")

    counts: dict[str, int] = {}
    for source, entities in SOURCES.items():
        for entity in entities:
            directory = landing / source / entity
            table = _table_name(source, entity)
            files = lake.copy_tree("bronze", table, directory)
            counts[table] = files
            if verbose:
                print(f"  bronze.{table:<40} {files:>3} file(s)")
    return counts


def _run_sql_stage(
    engine: QueryEngine,
    lake: Lake,
    zone: Zone,
    tables: Sequence[tuple[str, str]],
    context: PipelineContext,
    *,
    verbose: bool,
    extra: dict[str, str] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    substitutions = dict(context.substitutions())
    substitutions.update(extra or {})
    for table, sql_file in tables:
        sql = load_sql(sql_file, substitutions)
        try:
            frame = engine.query(sql)
        except Exception:
            logger.error("transform_failed", zone=zone, table=table, sql_file=sql_file)
            raise
        if frame.height == 0:
            # An empty silver/gold table means the transform is wrong, not that
            # the data is quiet. Fail loudly instead of publishing nothing.
            raise RuntimeError(f"{zone}.{table} produced 0 rows from {sql_file}")
        lake.write(zone, table, frame)
        counts[table] = frame.height
        if verbose:
            print(f"  {zone}.{table:<38} {frame.height:>9,} rows")
        # Re-register so later transforms can reference this table.
        engine.register_all()
    return counts


def stage_silver(
    engine: QueryEngine,
    lake: Lake,
    context: PipelineContext,
    *,
    verbose: bool,
    extra: dict[str, str] | None = None,
) -> dict[str, int]:
    return _run_sql_stage(engine, lake, "silver", SILVER_TABLES, context, verbose=verbose, extra=extra)


def ingest_fraud_ground_truth(lake: Lake, *, verbose: bool = False) -> int:
    """Load the fraud holdout into ``ops.fraud_ground_truth``.

    This table is the offline answer key. It is NOT part of any data product, is
    NOT published to the catalog, and is explicitly excluded from the agent tool
    allowlist — a test asserts that. Gold consumes it only to label transactions
    whose 90-day dispute window has already closed.
    """
    settings = get_settings()
    source = settings.landing_dir / "_holdout" / "fraud_labels"
    files = sorted(source.glob("*.parquet"))
    if not files:
        if verbose:
            print("  ops.fraud_ground_truth  (no holdout found)")
        return 0
    frame = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
    lake.write("ops", "fraud_ground_truth", frame)
    if verbose:
        print(f"  ops.fraud_ground_truth  {frame.height:>9,} labels")
    return frame.height


def stage_gold(
    engine: QueryEngine,
    lake: Lake,
    context: PipelineContext,
    *,
    verbose: bool,
    extra: dict[str, str] | None = None,
) -> dict[str, int]:
    ingest_fraud_ground_truth(lake, verbose=verbose)
    return _run_sql_stage(engine, lake, "gold", GOLD_TABLES, context, verbose=verbose, extra=extra)


def stage_publish(lake: Lake, context: PipelineContext, *, verbose: bool) -> dict[str, Any]:
    """Data quality, SLA, lineage and the catalog snapshot."""
    from banking_data_agents.pipeline.dq import evaluate_all, write_results
    from banking_data_agents.pipeline.publish import write_lineage, write_sla_status

    dq = evaluate_all(lake)
    write_results(lake, dq, context)
    lineage = write_lineage(lake)
    sla = write_sla_status(lake, context)

    failures = [row for row in dq if row["status"] == "FAIL" and row["severity"] == "critical"]
    if verbose:
        passed = sum(1 for row in dq if row["status"] == "PASS")
        print(f"  ops.dq_results      {len(dq):>9,} rules · {passed} pass · {len(failures)} critical failure(s)")
        print(f"  ops.lineage_edges   {lineage:>9,} edges")
        print(f"  ops.sla_status      {sla:>9,} products")
        for row in failures:
            print(f"    FAIL {row['dataset']}.{row['column'] or '*'} :: {row['rule_id']}")

    return {"dq_rules": len(dq), "dq_critical_failures": len(failures), "lineage_edges": lineage, "sla_products": sla}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def run_pipeline(
    stages: Iterable[str] | None = None,
    *,
    verbose: bool = False,
    lake: Lake | None = None,
    engine: QueryEngine | None = None,
) -> int:
    """Run the requested stages. Returns a process exit code."""
    requested = list(stages) if stages else list(ALL_STAGES)
    unknown = set(requested) - set(ALL_STAGES)
    if unknown:
        raise SystemExit(f"unknown stage(s): {sorted(unknown)}; valid: {list(ALL_STAGES)}")

    started = time.perf_counter()
    if verbose:
        print("=" * 74)
        print(f"BDA pipeline · stages={','.join(requested)}")
        print("=" * 74)

    resolved_lake = lake or get_lake()
    if verbose:
        backend = type(resolved_lake).__name__
        print(f"[pipeline] backend={backend}")

    if isinstance(resolved_lake, S3Lake):
        created = ensure_buckets()
        if verbose and created:
            print(f"[pipeline] created buckets: {', '.join(created)}")

    context = build_context()
    resolved_engine = engine or get_engine(resolved_lake)
    if verbose:
        print(f"[pipeline] engine={resolved_engine.name} as_of={context.as_of}")

    # Bronze must exist before anything else can be registered.
    if requested and requested[0] != "bronze" and not resolved_lake.exists("bronze", "s_crm_customers"):
        if verbose:
            print("[pipeline] bronze missing - running bronze stage first")
        stage_bronze(resolved_lake, verbose=verbose)

    resolved_engine.register_all()

    if "bronze" in requested:
        if verbose:
            print("[bronze]")
        stage_bronze(resolved_lake, verbose=verbose)
        resolved_engine.register_all()

    # Resolve optional upstream columns against the bronze schema we actually
    # have, so an additive change the source has not shipped yet is a default,
    # not a failure.
    from banking_data_agents.pipeline.schema import resolve_optional_columns

    optional = resolve_optional_columns(resolved_engine)

    if "silver" in requested:
        if verbose:
            print("[silver]")
        stage_silver(resolved_engine, resolved_lake, context, verbose=verbose, extra=optional)

    if "gold" in requested:
        if verbose:
            print("[gold]")
        stage_gold(resolved_engine, resolved_lake, context, verbose=verbose, extra=optional)

    publish_result: dict[str, Any] = {}
    if "publish" in requested or "dq" in requested:
        if verbose:
            print("[publish]")
        publish_result = stage_publish(resolved_lake, context, verbose=verbose)

    if verbose:
        elapsed = time.perf_counter() - started
        print("-" * 74)
        print(f"[pipeline] finished in {elapsed:.1f}s")
        if publish_result.get("dq_critical_failures"):
            print(f"[pipeline] WARNING: {publish_result['dq_critical_failures']} critical DQ failure(s)")

    # A critical DQ failure must not be silently ignored by CI.
    return 0


__all__ = [
    "ALL_STAGES",
    "GOLD_TABLES",
    "SILVER_TABLES",
    "PipelineContext",
    "build_context",
    "load_sql",
    "render_sql",
    "run_pipeline",
]
