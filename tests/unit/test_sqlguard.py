"""The guardrail is the control, not the prompt.

These tests treat :func:`validate_sql` as an adversarial component: for each test
the question is "can I get a bad query past it", not "does it accept a good one".
The two that matter most are the holdout (an agent that can read its own answer
key is not being evaluated) and the file readers (a query that can read the local
disk is not a query, it is an escape).
"""

from __future__ import annotations

import pytest

from banking_data_agents.tools.sqlguard import (
    FORBIDDEN_DATASETS,
    allowed_datasets,
    validate_sql,
)

GOOD = 'SELECT "region", COUNT(*) AS n FROM gold.customer_360 GROUP BY "region" LIMIT 100'


def codes(sql: str) -> set[str]:
    return {violation.code for violation in validate_sql(sql).violations}


# ---------------------------------------------------------------------------
# The happy path, so the rest of the file means something
# ---------------------------------------------------------------------------
def test_a_governed_query_is_accepted() -> None:
    result = validate_sql(GOOD)
    assert result.valid
    assert result.violations == []
    assert result.tables == ["gold.customer_360"]
    assert result.statement_type == "SELECT"


@pytest.mark.parametrize("table", sorted({"customer_360", "transaction", "credit_risk"}))
def test_every_published_product_is_queryable(table: str) -> None:
    result = validate_sql(f'SELECT "customer_id" FROM gold.{table} LIMIT 10')
    assert result.valid, result.violations


@pytest.mark.parametrize("table", ["dq_results", "lineage_edges", "sla_status", "pipeline_runs"])
def test_the_operational_tables_are_queryable(table: str) -> None:
    result = validate_sql(f"SELECT rule_id FROM ops.{table} LIMIT 10")
    assert result.valid, result.violations


# ---------------------------------------------------------------------------
# The holdout
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dataset", sorted(FORBIDDEN_DATASETS))
def test_the_fraud_holdout_is_unreachable(dataset: str) -> None:
    """The single most important assertion in this file.

    ``ops.fraud_ground_truth`` holds the labels the fraud agent is scored against.
    An agent that can read them will eventually do so, and then every evaluation
    number is fiction.
    """
    result = validate_sql(f"SELECT transaction_id FROM {dataset} LIMIT 10")
    assert not result.valid
    assert "DATASET_FORBIDDEN" in {violation.code for violation in result.violations}


def test_the_holdout_is_unreachable_even_through_a_join() -> None:
    sql = (
        "SELECT t.transaction_id, g.fraud_label_status "
        "FROM gold.transaction AS t "
        "JOIN ops.fraud_ground_truth AS g ON t.transaction_id = g.transaction_id "
        "LIMIT 10"
    )
    assert not validate_sql(sql).valid


def test_the_holdout_is_not_in_the_allowlist_at_all() -> None:
    """Two independent reasons it is unreachable, not one."""
    assert not (FORBIDDEN_DATASETS & allowed_datasets())


# ---------------------------------------------------------------------------
# Scope: which schemas and tables
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("schema", ["bronze", "silver"])
def test_raw_zones_are_not_agent_readable(schema: str) -> None:
    """Agents answer from governed products, not from raw landing tables."""
    assert "SCHEMA_NOT_ALLOWED" in codes(f"SELECT customer_id FROM {schema}.customers LIMIT 10")


def test_an_unpublished_gold_table_is_rejected() -> None:
    assert "TABLE_NOT_ALLOWED" in codes("SELECT x FROM gold.secret_sauce LIMIT 10")


def test_an_unqualified_table_is_rejected() -> None:
    assert "SCHEMA_REQUIRED" in codes("SELECT customer_id FROM customer_360 LIMIT 10")


def test_a_query_with_no_table_is_rejected() -> None:
    assert "NO_TABLE" in codes("SELECT 1 AS one LIMIT 1")


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_select_star_is_rejected() -> None:
    """Every output column must be deliberate, or the answer is not explainable."""
    assert "SELECT_STAR" in codes("SELECT * FROM gold.customer_360 LIMIT 10")
    assert "SELECT_STAR" in codes("SELECT t.* FROM gold.customer_360 AS t LIMIT 10")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO gold.customer_360 VALUES (1)",
        "UPDATE gold.customer_360 SET region = 'x'",
        "DELETE FROM gold.customer_360",
        "DROP TABLE gold.customer_360",
        "CREATE TABLE gold.mine AS SELECT 1 AS a",
        "ALTER TABLE gold.customer_360 ADD COLUMN x INT",
    ],
)
def test_writes_are_rejected(sql: str) -> None:
    assert not validate_sql(sql).valid


def test_multiple_statements_are_rejected() -> None:
    """Statement smuggling: a valid SELECT followed by something else."""
    sql = f"{GOOD}; DROP TABLE gold.customer_360"
    result = validate_sql(sql)
    assert not result.valid
    assert {"MULTIPLE_STATEMENTS", "NOT_SELECT"} & {v.code for v in result.violations}


def test_an_empty_query_is_rejected() -> None:
    assert "EMPTY" in codes("   ")


def test_unparseable_sql_is_rejected_not_ignored() -> None:
    assert "PARSE_ERROR" in codes("SELECT FROM WHERE GROUP gold")


# ---------------------------------------------------------------------------
# Escaping the lake
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('/etc/passwd') LIMIT 1",
        "SELECT * FROM read_csv('/tmp/x.csv') LIMIT 1",
        "SELECT * FROM 's3://other-bucket/secret.parquet' LIMIT 1",
        "SELECT * FROM read_parquet('s3://bda-gold/customer_360/*.parquet') LIMIT 1",
    ],
)
def test_file_readers_and_paths_are_rejected(sql: str) -> None:
    assert not validate_sql(sql).valid


def test_comments_are_stripped_before_the_token_scan() -> None:
    """Prose in a comment must not fail a good query.

    The pipeline's own SQL is full of comments naming datasets it does not read;
    the guardrail judges the statement, not the commentary.
    """
    sql = f"-- read_parquet would be unsafe here, but this is only prose\n{GOOD}"
    assert validate_sql(sql).valid


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
def test_a_query_without_a_limit_is_rejected() -> None:
    assert "MISSING_LIMIT" in codes("SELECT customer_id FROM gold.customer_360")


def test_an_oversized_limit_is_rejected() -> None:
    assert "LIMIT_TOO_LARGE" in codes("SELECT customer_id FROM gold.customer_360 LIMIT 100000")


def test_a_limit_within_the_ceiling_is_accepted() -> None:
    assert validate_sql('SELECT "customer_id" FROM gold.customer_360 LIMIT 1000').valid


def test_a_non_constant_limit_is_rejected() -> None:
    assert "LIMIT_NOT_CONSTANT" in codes("SELECT customer_id FROM gold.customer_360 LIMIT n")
