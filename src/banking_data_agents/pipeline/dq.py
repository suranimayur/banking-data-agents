"""Data quality evaluation.

Data quality is a *product feature* here, not a dashboard nobody reads. The
Copilot's ``explain_quality`` tool reads ``ops.dq_results`` directly, so an agent
answering "how much should I trust this number?" has real evidence to cite.

Rules are evaluated with polars against the lake, which makes them independent of
whichever query engine is in play.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from banking_data_agents.logging_setup import get_logger
from banking_data_agents.pipeline.lake import Lake, Zone
from banking_data_agents.telemetry import (
    DATA_QUALITY_NAMESPACE,
    DQ_FAIL,
    DQ_PASS,
    DQ_SLA_MISSES,
    DQ_WARNING,
    emit,
)

logger = get_logger(__name__)

#: Rules whose failure means "do not trust this dataset".
CRITICAL = "critical"
#: Rules whose failure means "known incident - explain it, do not panic".
WARNING = "warning"


@dataclass(frozen=True)
class Rule:
    """A single, independently reportable data quality expectation."""

    dataset: str  # "gold.customer_360"
    rule_id: str
    kind: str  # unique | not_null | null_rate | range | referential | row_count | freshness
    severity: str = CRITICAL
    column: str | None = None
    threshold: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    reference: str | None = None  # "silver.customers" for referential checks
    reference_column: str | None = None
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def zone(self) -> Zone:
        return self.dataset.split(".", 1)[0]  # type: ignore[return-value]

    @property
    def table(self) -> str:
        return self.dataset.split(".", 1)[1]

    @property
    def rule_text(self) -> str:
        if self.description:
            return self.description
        # Built lazily: a dict of f-strings would format every kind's fields and
        # blow up on a None threshold for an unrelated rule.
        if self.kind == "unique":
            return f"{self.column or self.params.get('composite')} must be unique"
        if self.kind == "not_null":
            return f"{self.column} must never be null"
        if self.kind == "null_rate":
            return f"null rate of {self.column} must be <= {self.threshold}"
        if self.kind == "range":
            allowed = self.params.get("allowed")
            if allowed:
                return f"{self.column} must be one of {allowed}"
            return f"{self.column} must be within [{self.minimum}, {self.maximum}]"
        if self.kind == "referential":
            threshold = self.threshold if self.threshold is not None else 1.0
            return f"{self.column} must match {self.reference}.{self.reference_column} (>={threshold:.3f} match rate)"
        if self.kind == "row_count":
            return f"row count must be >= {self.minimum}"
        if self.kind == "freshness":
            return f"{self.column} must be within {self.params.get('max_age_days')} days of the anchor"
        return self.rule_id


# ---------------------------------------------------------------------------
# Rule set
# ---------------------------------------------------------------------------
def default_rules() -> list[Rule]:
    """The published quality contract for the platform."""
    return [
        # --- Customer 360 ---------------------------------------------------
        Rule(
            "gold.customer_360",
            "c360_unique_customer",
            "unique",
            column="customer_id",
            description="customer_360 has exactly one row per customer",
        ),
        Rule("gold.customer_360", "c360_not_null_keys", "not_null", column="customer_id"),
        Rule("gold.customer_360", "c360_not_null_segment", "not_null", column="customer_segment"),
        Rule("gold.customer_360", "c360_not_null_risk", "not_null", column="risk_category"),
        Rule(
            "gold.customer_360",
            "c360_segment_vocabulary",
            "range",
            column="customer_segment",
            description="customer_segment is one of Mass/Affluent/HNI/Premium",
            params={"allowed": ["Mass", "Affluent", "HNI", "Premium"]},
        ),
        Rule(
            "gold.customer_360",
            "c360_risk_vocabulary",
            "range",
            column="risk_category",
            description="risk_category is one of Low/Medium/High/Unknown",
            params={"allowed": ["Low", "Medium", "High", "Unknown"]},
        ),
        Rule("gold.customer_360", "c360_credit_score_range", "range", column="credit_score", minimum=300, maximum=900),
        Rule(
            "gold.customer_360",
            "c360_non_negative_balance",
            "range",
            column="total_balance",
            minimum=0,
            maximum=None,
            description="total_balance can never be negative",
        ),
        Rule(
            "gold.customer_360",
            "c360_row_count_floor",
            "row_count",
            minimum=100,
            description="customer_360 must not silently lose customers",
        ),
        # --- Transaction ----------------------------------------------------
        Rule("gold.transaction", "txn_unique_id", "unique", column="transaction_id"),
        Rule("gold.transaction", "txn_not_null_id", "not_null", column="transaction_id"),
        Rule("gold.transaction", "txn_not_null_ts", "not_null", column="transaction_ts"),
        Rule("gold.transaction", "txn_not_null_amount", "not_null", column="amount"),
        Rule("gold.transaction", "txn_non_negative_amount", "range", column="amount", minimum=0, maximum=None),
        Rule("gold.transaction", "txn_not_null_direction", "not_null", column="debit_credit"),
        Rule(
            "gold.transaction",
            "txn_label_status_vocabulary",
            "range",
            column="fraud_label_status",
            params={"allowed": ["CONFIRMED", "CLEARED", "PENDING"]},
            description="every transaction carries an explicit dispute-window label status",
        ),
        Rule(
            "gold.transaction",
            "txn_recent_labels_withheld",
            "range",
            column="fraud_flag",
            description="the last 90 days must contain no confirmed labels (dispute window open)",
            params={"custom": "recent_fraud_labels_withheld"},
        ),
        # --- Credit risk ----------------------------------------------------
        Rule("gold.credit_risk", "cr_unique_customer", "unique", column="customer_id"),
        Rule("gold.credit_risk", "cr_not_null_customer", "not_null", column="customer_id"),
        Rule("gold.credit_risk", "cr_dti_band", "range", column="dti", minimum=0, maximum=5),
        Rule("gold.credit_risk", "cr_risk_score_band", "range", column="risk_score", minimum=0, maximum=100),
        Rule(
            "gold.credit_risk",
            "cr_risk_band_vocabulary",
            "range",
            column="risk_band",
            params={"allowed": ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]},
        ),
        # --- Silver ---------------------------------------------------------
        Rule(
            "silver.customers",
            "silver_customer_unique_version",
            "unique",
            column="customer_id",
            description="(customer_id, valid_from_batch) is unique",
            params={"composite": ["customer_id", "valid_from_batch"]},
        ),
        Rule("silver.customers", "silver_customer_not_null", "not_null", column="customer_id"),
        Rule(
            "silver.customers",
            "silver_customer_one_current",
            "range",
            column="is_current",
            params={"custom": "exactly_one_current_customer_version"},
        ),
        Rule("silver.transactions", "silver_txn_unique", "unique", column="transaction_id"),
        Rule(
            "silver.transactions", "silver_txn_amount_range", "range", column="amount", minimum=0, maximum=100_000_000
        ),
        Rule(
            "silver.accounts",
            "silver_account_referential",
            "referential",
            column="customer_id",
            reference="silver.customers",
            reference_column="customer_id",
            threshold=0.999,
        ),
        Rule(
            "silver.accounts",
            "silver_account_balance_flag_rate",
            "null_rate",
            column="balance",
            threshold=0.05,
            description="no more than 5% of conformed balances may be nulled by remediation",
        ),
        # --- Bronze: the deliberate incidents, recorded rather than hidden --
        Rule(
            "bronze.s_core_accounts",
            "bronze_region_null_rate",
            "null_rate",
            column="branch_region",
            threshold=0.02,
            severity=WARNING,
            description="historical incident: branch_region was missing on a slice of the "
            "month 9 extract while the upstream system was degraded",
        ),
        Rule(
            "bronze.s_core_accounts",
            "bronze_impossible_negative_balances",
            "range",
            column="balance",
            minimum=0,
            maximum=None,
            severity=WARNING,
            description="historical incident: month 9 produced impossible negative savings "
            "balances; silver nulls and flags them",
        ),
    ]


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------
def _result(
    rule: Rule,
    status: str,
    evaluated: int,
    failed: int,
    detail: str,
) -> dict[str, Any]:
    return {
        "dataset": rule.dataset,
        "rule_id": rule.rule_id,
        "rule_text": rule.rule_text,
        "kind": rule.kind,
        "severity": rule.severity,
        "column": rule.column or "",
        "status": status,
        "rows_evaluated": evaluated,
        "rows_failed": failed,
        "score": round(1.0 - (failed / evaluated), 6) if evaluated else 1.0,
        "detail": detail,
    }


def _evaluate_rule(rule: Rule, frame: pl.DataFrame, references: dict[str, pl.DataFrame]) -> dict[str, Any]:
    custom = rule.params.get("custom") if rule.params else None
    if custom:
        return _evaluate_custom(rule, frame, custom)

    allowed = rule.params.get("allowed") if rule.params else None
    composite = rule.params.get("composite") if rule.params else None

    if rule.kind == "unique":
        keys = composite or ([rule.column] if rule.column else [])
        missing = [c for c in keys if c not in frame.columns]
        if missing:
            return _result(rule, "ERROR", frame.height, 0, f"missing column(s): {missing}")
        failed = frame.height - frame.select(keys).unique().height
        return _result(rule, "PASS" if failed == 0 else "FAIL", frame.height, failed, f"{failed} duplicate key(s)")

    if rule.kind == "not_null":
        if rule.column not in frame.columns:
            return _result(rule, "ERROR", frame.height, 0, f"missing column: {rule.column}")
        failed = int(frame[rule.column].null_count())
        return _result(rule, "PASS" if failed == 0 else "FAIL", frame.height, failed, f"{failed} null(s)")

    if rule.kind == "null_rate":
        if rule.column not in frame.columns:
            return _result(rule, "ERROR", frame.height, 0, f"missing column: {rule.column}")
        failed = int(frame[rule.column].null_count())
        rate = failed / frame.height if frame.height else 0.0
        threshold = rule.threshold or 0.0
        status = "PASS" if rate <= threshold else "FAIL"
        return _result(rule, status, frame.height, failed, f"null rate {rate:.4%} (threshold {threshold:.2%})")

    if rule.kind == "range":
        if allowed is not None:
            if rule.column not in frame.columns:
                return _result(rule, "ERROR", frame.height, 0, f"missing column: {rule.column}")
            failed = int(frame.filter(~pl.col(rule.column).is_in(allowed)).height)
            return _result(
                rule, "PASS" if failed == 0 else "FAIL", frame.height, failed, f"{failed} value(s) outside {allowed}"
            )
        if rule.column not in frame.columns:
            return _result(rule, "ERROR", frame.height, 0, f"missing column: {rule.column}")
        column = pl.col(rule.column).cast(pl.Float64, strict=False)
        predicate = pl.lit(False)
        if rule.minimum is not None:
            predicate = predicate | (column < rule.minimum)
        if rule.maximum is not None:
            predicate = predicate | (column > rule.maximum)
        present = frame.filter(column.is_not_null())
        failed = int(present.filter(predicate).height)
        return _result(
            rule,
            "PASS" if failed == 0 else "FAIL",
            present.height,
            failed,
            f"{failed} value(s) outside [{rule.minimum}, {rule.maximum}]",
        )

    if rule.kind == "referential":
        reference = references.get(rule.reference or "")
        if reference is None or rule.column not in frame.columns:
            return _result(rule, "ERROR", frame.height, 0, f"cannot load {rule.reference}")
        valid = set(reference[rule.reference_column or rule.column].drop_nulls().to_list())
        total = frame.height
        matched = int(frame.filter(pl.col(rule.column).is_in(valid)).height)
        rate = matched / total if total else 0.0
        threshold = rule.threshold or 1.0
        status = "PASS" if rate >= threshold else "FAIL"
        return _result(rule, status, total, total - matched, f"match rate {rate:.4%} (threshold {threshold:.2%})")

    if rule.kind == "row_count":
        floor = rule.minimum or 0
        status = "PASS" if frame.height >= floor else "FAIL"
        return _result(
            rule, status, frame.height, 0 if status == "PASS" else 1, f"{frame.height} row(s) (floor {floor:.0f})"
        )

    if rule.kind == "freshness":
        if rule.column not in frame.columns:
            return _result(rule, "ERROR", frame.height, 0, f"missing column: {rule.column}")
        max_age = rule.params.get("max_age_days", 1)
        anchor = rule.params.get("anchor")
        newest = frame[rule.column].max()
        # ``max()`` over an empty or all-null column yields None, and the value it
        # does yield is whatever polars holds for the dtype, so both the presence
        # and the type are checked before any date arithmetic.
        newest_date = newest.date() if isinstance(newest, datetime) else None
        stale = newest_date is None or (anchor is not None and anchor - newest_date > timedelta(days=max_age))
        return _result(
            rule,
            "FAIL" if stale else "PASS",
            frame.height,
            1 if stale else 0,
            f"newest {newest!r} vs anchor {anchor!r}",
        )

    return _result(rule, "ERROR", frame.height, 0, f"unsupported rule kind: {rule.kind}")


def _evaluate_custom(rule: Rule, frame: pl.DataFrame, name: str) -> dict[str, Any]:
    """Named checks that need more than a column predicate."""
    if name == "exactly_one_current_customer_version":
        grouped = frame.group_by("customer_id").agg(pl.col("is_current").sum().alias("current_count"))
        bad = grouped.filter(pl.col("current_count") != 1)
        return _result(
            rule,
            "PASS" if bad.height == 0 else "FAIL",
            grouped.height,
            bad.height,
            f"{bad.height} customer(s) without exactly one current version",
        )

    if name == "recent_fraud_labels_withheld":
        # The honesty guarantee: inside the open dispute window nothing may be labelled.
        in_window = frame.filter(pl.col("fraud_label_status") == "PENDING")
        leaked = in_window.filter(pl.col("fraud_flag").is_not_null())
        return _result(
            rule,
            "PASS" if leaked.height == 0 else "FAIL",
            in_window.height,
            leaked.height,
            f"{leaked.height} confirmed label(s) exposed inside the open dispute window",
        )

    return _result(rule, "ERROR", frame.height, 0, f"unknown custom check: {name}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_CACHE: dict[str, pl.DataFrame] = {}


def _load(lake: Lake, dataset: str) -> pl.DataFrame:
    if dataset not in _CACHE:
        zone, table = dataset.split(".", 1)
        _CACHE[dataset] = lake.read(zone, table)  # type: ignore[arg-type]
    return _CACHE[dataset]


def clear_cache() -> None:
    _CACHE.clear()


def evaluate_all(lake: Lake, rules: list[Rule] | None = None) -> list[dict[str, Any]]:
    """Evaluate every rule. Missing datasets are reported as ERROR, never skipped."""
    clear_cache()
    resolved = rules or default_rules()
    results: list[dict[str, Any]] = []

    for rule in resolved:
        try:
            frame = _load(lake, rule.dataset)
        except FileNotFoundError as exc:
            results.append(_result(rule, "ERROR", 0, 0, f"dataset unavailable: {exc}"))
            continue
        try:
            results.append(_evaluate_rule(rule, frame, references=_reference_frames(lake, resolved)))
        except Exception as exc:
            logger.warning("dq_rule_error", rule=rule.rule_id, error=str(exc))
            results.append(_result(rule, "ERROR", frame.height, 0, f"{type(exc).__name__}: {exc}"))

    return results


def _reference_frames(lake: Lake, rules: list[Rule]) -> dict[str, pl.DataFrame]:
    frames: dict[str, pl.DataFrame] = {}
    for rule in rules:
        if rule.kind == "referential" and rule.reference:
            try:
                frames[rule.reference] = _load(lake, rule.reference)
            except FileNotFoundError:
                continue
    return frames


def write_results(lake: Lake, results: list[dict[str, Any]], context: Any) -> int:
    """Persist DQ results as ``ops.dq_results``."""
    run_ts = datetime.fromisoformat(context.as_of_ts)
    frame = pl.DataFrame(results).with_columns(
        pl.lit(run_ts).alias("run_ts"),
        pl.lit(f"pipeline-{context.as_of.isoformat()}").alias("job_run_id"),
    )
    lake.write("ops", "dq_results", frame)
    return frame.height


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact summary used by the CLI, CI gate and the Copilot."""
    by_status: dict[str, int] = {}
    for row in results:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    failures = [row for row in results if row["status"] == "FAIL"]
    summary = {
        "total": len(results),
        "by_status": by_status,
        "critical_failures": [r for r in failures if r["severity"] == CRITICAL],
        "warning_failures": [r for r in failures if r["severity"] == WARNING],
    }
    emit_quality_metrics(summary)
    return summary


def emit_quality_metrics(summary: dict[str, Any]) -> None:
    """Publish rule outcomes as metrics, so the freshness dashboard is not empty.

    Called from :func:`summarise` rather than from the runner, so that the metric is
    emitted wherever a summary is computed — the nightly state machine, a local
    ``bda pipeline dq``, and CI all report the same numbers.
    """
    by_status = summary.get("by_status", {})
    emit(DQ_PASS, float(by_status.get("PASS", 0)), namespace=DATA_QUALITY_NAMESPACE)
    emit(DQ_WARNING, float(by_status.get("WARN", 0)), namespace=DATA_QUALITY_NAMESPACE)
    emit(DQ_FAIL, float(by_status.get("FAIL", 0)), namespace=DATA_QUALITY_NAMESPACE)
    # Only *critical* failures are SLA misses. A warning is a rule that is allowed to
    # be violated by design, and counting it here would make the alarm meaningless.
    emit(
        DQ_SLA_MISSES,
        float(len(summary.get("critical_failures", []))),
        namespace=DATA_QUALITY_NAMESPACE,
    )


__all__ = [
    "CRITICAL",
    "WARNING",
    "Rule",
    "clear_cache",
    "default_rules",
    "emit_quality_metrics",
    "evaluate_all",
    "summarise",
    "write_results",
]
