"""Resolution of optional upstream columns.

Banks add columns to source systems over time. A transform that hard-references
such a column works against a long history and fails against a short one — a bug
that only shows up in the first months of a deployment, or in CI when the window
is trimmed to keep tests fast.

Rather than weakening the test or the dataset, transforms reference a *logical*
placeholder (``{{crm_preferred_language}}``) that the runner resolves against the
bronze schema it actually has: the real expression when the column is present, a
typed default when it is not.

This keeps one copy of the business logic and makes additive upstream change a
supported scenario instead of an outage.
"""

from __future__ import annotations

from typing import Any

from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)

#: placeholder -> (bronze table, source column, expression when the column exists)
OPTIONAL_COLUMNS: dict[str, tuple[str, str, str]] = {
    "crm_preferred_language": (
        "bronze.s_crm_customers",
        "preferred_language",
        "COALESCE(preferred_language, 'UNKNOWN')",
    ),
}

#: placeholder -> expression used when the column has not appeared yet.
ABSENT_EXPRESSIONS: dict[str, str] = {
    "crm_preferred_language": "'UNKNOWN'",
}


def available_columns(engine: Any, table: str) -> set[str]:
    """Column names of ``table`` as the engine sees them, or empty on failure."""
    try:
        return set(engine.query(f"SELECT * FROM {table} LIMIT 0").columns)
    except Exception as exc:
        logger.debug("schema_probe_failed", table=table, error=str(exc))
        return set()


def resolve_optional_columns(engine: Any) -> dict[str, str]:
    """Map every optional placeholder to SQL valid for the current bronze schema."""
    resolved: dict[str, str] = {}
    for placeholder, (table, column, present_expression) in OPTIONAL_COLUMNS.items():
        if column in available_columns(engine, table):
            resolved[placeholder] = present_expression
        else:
            resolved[placeholder] = ABSENT_EXPRESSIONS[placeholder]
            logger.info(
                "optional_column_absent",
                table=table,
                column=column,
                note="upstream has not added this column yet; using the declared default",
            )
    return resolved


def placeholder_names() -> set[str]:
    """Every placeholder a transform may use, for validation and tests."""
    return set(OPTIONAL_COLUMNS)


__all__ = [
    "ABSENT_EXPRESSIONS",
    "OPTIONAL_COLUMNS",
    "available_columns",
    "placeholder_names",
    "resolve_optional_columns",
]
