"""The SQL guardrail.

Nothing reaches the warehouse without passing through here. The rule is a
whitelist, not a blacklist: a query may read the published gold products and the
three operational metadata tables, and may do nothing else. Anything else is an
error the agent must report rather than a query the engine must survive.

This is the enforcement layer. The prompt tells the model what it should do; this
module decides what it is *able* to do. Defence in depth means the prompt is a
convenience and the guardrail is the control.

Checked here:

* exactly one statement, and it is a ``SELECT``;
* no ``SELECT *`` — every column is deliberate, so the answer is explainable;
* every table is a published gold product or an allowlisted ops table;
* ``ops.fraud_ground_truth`` is unreachable **by construction**: it is never in
  the allowlist, so an agent cannot leak the fraud labels it is meant to be
  evaluated against;
* no external-file readers (``read_parquet`` and friends) and no ``s3://`` paths;
* a ``LIMIT`` is present and within the configured ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from banking_data_agents.config import get_settings
from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)

#: Schemas a query may read from. Bronze and silver are deliberately absent: the
#: agents answer from governed products, not from raw landing tables.
ALLOWED_SCHEMAS = ("gold", "ops")

#: Operational tables published by the pipeline, safe for agent use.
ALLOWED_OPS_TABLES = ("dq_results", "lineage_edges", "sla_status", "pipeline_runs")

#: Datasets that must never be reachable, whatever the allowlist says. The fraud
#: holdout exists precisely so the fraud agent can be scored honestly; an agent
#: that can read its own answer key is not being tested.
FORBIDDEN_DATASETS = {"ops.fraud_ground_truth", "ops.fraud_labels"}

#: Reader functions and protocols that would escape the lake and the guardrail.
FORBIDDEN_TOKENS = (
    "read_parquet",
    "read_csv",
    "read_json",
    "read_ndjson",
    "read_text",
    "read_blob",
    "glob(",
    "httpfs",
    "s3://",
    "delta_scan",
    "iceberg_scan",
    "postgres_scan",
    "mysql_scan",
    "sqlite_scan",
    "attach ",
    "install ",
    "load ",
)

#: Violation codes that mean the *agent's policy* declined, rather than the SQL
#: being malformed. A refusal for one of these is a complete, legitimate answer, so
#: the evidence envelope classifies it as a refusal instead of a failure.
POLICY_VIOLATION_CODES = frozenset({"DATASET_FORBIDDEN", "DIMENSION_FORBIDDEN"})

#: Statements other than SELECT. Any of these is a hard failure.
FORBIDDEN_STATEMENTS = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Copy,
    exp.Command,
    exp.Merge,
    exp.TruncateTable,
)


@dataclass
class Violation:
    code: str
    message: str
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class ValidationResult:
    sql: str
    valid: bool = True
    violations: list[Violation] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    statement_type: str = ""

    def fail(self, code: str, message: str, severity: str = "error") -> None:
        self.violations.append(Violation(code, message, severity))
        if severity == "error":
            self.valid = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "sql": self.sql,
            "statement_type": self.statement_type,
            "tables": list(self.tables),
            "violations": [v.as_dict() for v in self.violations],
            "errors": [v.code for v in self.violations if v.severity == "error"],
        }


def allowed_datasets() -> set[str]:
    """The datasets an agent may read, derived from the live contract set."""
    from banking_data_agents.catalog.contracts import load_contracts

    datasets = {f"gold.{name}" for name in load_contracts()}
    datasets |= {f"ops.{name}" for name in ALLOWED_OPS_TABLES}
    return datasets


def _strip_sql_comments(sql: str) -> str:
    """Remove ``--`` and ``/* */`` comments for the raw token scan."""
    import re

    without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", without_block)


def validate_sql(
    sql: str,
    *,
    max_limit: int | None = None,
    forbidden_dimensions: frozenset[str] | set[str] = frozenset(),
) -> ValidationResult:
    """Validate a query against the allowlist. Never raises.

    ``forbidden_dimensions`` carries a per-agent policy: the fraud analyst, for
    instance, reports patterns and may not group by ``customer_id``, because a
    ranked list of named individuals is a list of people to act against. That
    restriction lives here rather than in the prompt because a prompt is advice and
    this is a control.
    """
    settings = get_settings()
    ceiling = max_limit or min(settings.athena_bytes_cap_mb * 2, 10_000)
    hard_ceiling = 1_000
    result = ValidationResult(sql=sql.strip())

    if not result.sql:
        result.fail("EMPTY", "the query is empty")
        return result

    lowered = _strip_sql_comments(result.sql).lower()
    for token in FORBIDDEN_TOKENS:
        if token in lowered:
            result.fail(
                "UNSAFE_FUNCTION",
                f"{token.strip()} is not available to agents; query the published gold products instead",
            )

    try:
        statements = sqlglot.parse(result.sql, read="duckdb")
    except Exception as error:
        result.fail("PARSE_ERROR", f"the query could not be parsed: {error}")
        return result

    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        result.fail("MULTIPLE_STATEMENTS", f"exactly one statement is allowed, found {len(statements)}")
        return result

    statement = statements[0]
    result.statement_type = type(statement).__name__.upper()

    if isinstance(statement, FORBIDDEN_STATEMENTS) or not isinstance(
        statement, (exp.Select, exp.Union, exp.With, exp.Subquery)
    ):
        result.fail("NOT_SELECT", f"only SELECT statements are allowed, found {result.statement_type}")
        return result

    _check_tables(statement, result)
    _check_star(statement, result)
    _check_limit(statement, result, ceiling=min(ceiling, hard_ceiling))
    _check_dimensions(statement, result, forbidden_dimensions)

    logger.debug("sql_validated", valid=result.valid, tables=result.tables)
    return result


def _check_tables(statement: exp.Expression, result: ValidationResult) -> None:
    allow = allowed_datasets()
    seen: list[str] = []

    for table in statement.find_all(exp.Table):
        schema = (table.db or "").lower()
        name = (table.name or "").lower()
        dataset = f"{schema}.{name}" if schema else name
        if dataset not in seen:
            seen.append(dataset)

        if dataset in FORBIDDEN_DATASETS:
            result.fail(
                "DATASET_FORBIDDEN",
                f"{dataset} is a restricted holdout and is never queryable by an agent",
            )
            continue

        if not schema:
            result.fail(
                "SCHEMA_REQUIRED",
                f"'{name}' is unqualified; gold and ops tables must be written as schema.table",
            )
            continue

        if schema not in ALLOWED_SCHEMAS:
            result.fail(
                "SCHEMA_NOT_ALLOWED",
                f"schema '{schema}' is not readable by agents; allowed: {', '.join(ALLOWED_SCHEMAS)}",
            )
            continue

        if dataset not in allow:
            result.fail(
                "TABLE_NOT_ALLOWED",
                f"'{dataset}' is not a published data product. Available: {', '.join(sorted(allow))}",
            )

    if not seen:
        result.fail("NO_TABLE", "the query reads no table")
    result.tables = seen


def _check_dimensions(
    statement: exp.Expression,
    result: ValidationResult,
    forbidden: frozenset[str] | set[str],
) -> None:
    """Reject a query that groups or ranks by a column this agent may not use."""
    if not forbidden:
        return
    banned = {name.lower() for name in forbidden}
    used: list[str] = []
    for clause in ("group", "order", "partition_by", "distinct"):
        container = statement.args.get(clause)
        if container is None:
            continue
        for column in container.find_all(exp.Column):
            name = (column.name or "").lower()
            if name in banned and name not in used:
                used.append(name)
    for name in used:
        result.fail(
            "DIMENSION_FORBIDDEN",
            f"grouping or ranking by '{name}' is not permitted for this agent; "
            "report the pattern at an aggregate grain instead of listing individuals",
        )


def _check_star(statement: exp.Expression, result: ValidationResult) -> None:
    for select in statement.find_all(exp.Select):
        for projection in select.expressions:
            is_star = isinstance(projection, exp.Star) or (
                isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
            )
            if is_star:
                result.fail(
                    "SELECT_STAR",
                    "SELECT * is not allowed; name the columns so every output column is explainable",
                )
                return


def _check_limit(statement: exp.Expression, result: ValidationResult, *, ceiling: int) -> None:
    limit = statement.args.get("limit")
    if limit is None:
        for nested in statement.find_all(exp.Select):
            limit = nested.args.get("limit")
            if limit is not None:
                break
    if limit is None:
        result.fail("MISSING_LIMIT", f"the query has no LIMIT; add LIMIT {ceiling} or fewer rows")
        return

    try:
        value = int(limit.expression.name)
    except (AttributeError, TypeError, ValueError):
        result.fail("LIMIT_NOT_CONSTANT", "the LIMIT must be a literal number")
        return

    if value > ceiling:
        result.fail("LIMIT_TOO_LARGE", f"LIMIT {value} exceeds the maximum of {ceiling}")


__all__ = [
    "ALLOWED_OPS_TABLES",
    "ALLOWED_SCHEMAS",
    "FORBIDDEN_DATASETS",
    "POLICY_VIOLATION_CODES",
    "ValidationResult",
    "Violation",
    "allowed_datasets",
    "validate_sql",
]
