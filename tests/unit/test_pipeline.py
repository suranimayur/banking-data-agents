"""Pipeline correctness.

The end-to-end test runs the real medallion pipeline over a small generated
dataset in a temporary lake. It asserts the invariants that make the platform
trustworthy — not merely that the code did not crash:

* gold matches the documented grain,
* the fraud dispute-window gate actually withholds recent labels,
* lineage reflects real reads and ignores prose,
* quality rules fail where they are supposed to.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from banking_data_agents.config import get_settings, reset_settings_cache
from banking_data_agents.datagen.generate import generate_into
from banking_data_agents.pipeline.dq import Rule, default_rules, evaluate_all, summarise
from banking_data_agents.pipeline.lake import local_lake
from banking_data_agents.pipeline.publish import derive_edges, strip_sql_comments, trace, upstream_of
from banking_data_agents.pipeline.runner import render_sql


# ---------------------------------------------------------------------------
# Unit: SQL rendering
# ---------------------------------------------------------------------------
def test_placeholders_are_substituted() -> None:
    sql = render_sql("SELECT * FROM gold.x WHERE d <= DATE '{{as_of}}'", {"as_of": "2026-09-30"})
    assert "2026-09-30" in sql
    assert "{{" not in sql


def test_unresolved_placeholder_is_an_error_not_a_silent_pass() -> None:
    with pytest.raises(ValueError, match="unresolved SQL placeholders"):
        render_sql("SELECT {{typo}}", {"as_of": "2026-09-30"})


def test_every_transform_references_only_known_placeholder_names() -> None:
    """A typo'd placeholder must fail the build, not reach production."""
    from banking_data_agents.pipeline.runner import GOLD_TABLES, SILVER_TABLES, load_sql
    from banking_data_agents.pipeline.schema import ABSENT_EXPRESSIONS

    context = {
        "as_of": "2026-09-30",
        "as_of_ts": "2026-09-30 00:00:00",
        "as_of_90": "2026-07-02",
        "as_of_180": "2026-04-03",
        "as_of_365": "2025-09-30",
        "window_start": "2025-10-01",
        "window_end": "2026-10-01",
        # Optional upstream columns, rendered in their "not shipped yet" form.
        **ABSENT_EXPRESSIONS,
    }
    for _, sql_file in (*SILVER_TABLES, *GOLD_TABLES):
        rendered = load_sql(sql_file, context)
        assert "{{" not in rendered, sql_file


# ---------------------------------------------------------------------------
# Unit: lineage
# ---------------------------------------------------------------------------
def test_sql_comments_are_stripped_before_lineage_is_derived() -> None:
    sql = """
    -- This mentions ops.pii_vault in prose only.
    SELECT customer_id FROM bronze.s_crm_customers  -- trailing note about gold.thing
    """
    cleaned = strip_sql_comments(sql)
    assert "ops.pii_vault" not in cleaned
    assert "gold.thing" not in cleaned
    assert "bronze.s_crm_customers" in cleaned


def test_derived_edges_are_real_reads_only() -> None:
    edges = derive_edges()
    assert edges, "transforms must produce lineage"
    pairs = {(e["from_dataset"], e["to_dataset"]) for e in edges}

    # Real dependency, declared in the FROM clause.
    assert ("bronze.s_crm_customers", "silver.customers") in pairs
    assert ("silver.transactions", "gold.transaction") in pairs
    # Mentioned in a comment only — must not appear as a dependency.
    assert ("ops.pii_vault", "silver.customers") not in pairs
    # A transform never depends on itself.
    assert not any(f == t for f, t in pairs)


def test_every_published_product_has_upstream_lineage() -> None:
    edges = derive_edges()
    produced = {e["to_dataset"] for e in edges}
    for product in ("gold.customer_360", "gold.transaction", "gold.credit_risk"):
        assert product in produced, f"{product} has no declared lineage"


# ---------------------------------------------------------------------------
# Unit: data quality rules
# ---------------------------------------------------------------------------
def _frame_customers() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "customer_id": ["C1", "C2", "C3"],
            "customer_segment": ["Mass", "HNI", "Mass"],
            "risk_category": ["Low", "High", "Medium"],
            "credit_score": [780, 620, None],
            "total_balance": [1000.0, 2000.0, 500.0],
        }
    )


def test_unique_rule_detects_duplicates() -> None:
    from banking_data_agents.pipeline.dq import _evaluate_rule

    frame = pl.DataFrame({"customer_id": ["C1", "C1", "C2"]})
    rule = Rule("gold.customer_360", "r", "unique", column="customer_id")
    assert _evaluate_rule(rule, frame, {})["status"] == "FAIL"

    good = pl.DataFrame({"customer_id": ["C1", "C2"]})
    assert _evaluate_rule(rule, good, {})["status"] == "PASS"


def test_range_rule_ignores_null_but_catches_out_of_band() -> None:
    from banking_data_agents.pipeline.dq import _evaluate_rule

    rule = Rule("gold.customer_360", "r", "range", column="credit_score", minimum=300, maximum=900)
    result = _evaluate_rule(rule, _frame_customers(), {})
    assert result["status"] == "PASS", "a null credit score is 'unknown', not out of range"

    bad = pl.DataFrame({"credit_score": [1200]})
    assert _evaluate_rule(rule, bad, {})["status"] == "FAIL"


def test_vocabulary_rule_enforces_the_published_domain() -> None:
    from banking_data_agents.pipeline.dq import _evaluate_rule

    rule = Rule(
        "gold.customer_360",
        "r",
        "range",
        column="customer_segment",
        params={"allowed": ["Mass", "Affluent", "HNI", "Premium"]},
    )
    assert _evaluate_rule(rule, _frame_customers(), {})["status"] == "PASS"

    rogue = pl.DataFrame({"customer_segment": ["Mass", "Platinum"]})
    assert _evaluate_rule(rule, rogue, {})["status"] == "FAIL"


def test_null_rate_rule_threshold_is_respected() -> None:
    from banking_data_agents.pipeline.dq import _evaluate_rule

    frame = pl.DataFrame({"x": [1, 2, None, None]})
    strict = Rule("bronze.t", "r", "null_rate", column="x", threshold=0.1)
    lenient = Rule("bronze.t", "r", "null_rate", column="x", threshold=0.6)
    assert _evaluate_rule(strict, frame, {})["status"] == "FAIL"
    assert _evaluate_rule(lenient, frame, {})["status"] == "PASS"


def test_referential_rule_uses_match_rate() -> None:
    from banking_data_agents.pipeline.dq import _evaluate_rule

    reference = {"silver.customers": pl.DataFrame({"customer_id": ["C1", "C2", "C3"]})}
    rule = Rule(
        "silver.accounts",
        "r",
        "referential",
        column="customer_id",
        reference="silver.customers",
        reference_column="customer_id",
        threshold=0.99,
    )
    good = pl.DataFrame({"customer_id": ["C1", "C2", "C3"]})
    assert _evaluate_rule(rule, good, reference)["status"] == "PASS"
    bad = pl.DataFrame({"customer_id": ["C1", "ORPHAN"]})
    assert _evaluate_rule(rule, bad, reference)["status"] == "FAIL"


def test_missing_dataset_is_an_error_not_a_pass() -> None:
    """A dataset that failed to build must never silently look healthy."""
    from banking_data_agents.pipeline.dq import _evaluate_rule, _result

    rule = Rule("gold.nonexistent", "r", "not_null", column="x")
    result = _result(rule, "ERROR", 0, 0, "dataset unavailable")
    assert result["status"] == "ERROR"
    assert _evaluate_rule(rule, pl.DataFrame({"y": [1]}), {})["status"] == "ERROR"


def test_the_custom_recent_label_gate_rule_exists() -> None:
    """If this rule is removed the evaluation stops being honest."""
    ids = {rule.rule_id for rule in default_rules()}
    assert "txn_recent_labels_withheld" in ids
    assert "bronze_impossible_negative_balances" in ids


# ---------------------------------------------------------------------------
# Integration: the real pipeline over a small dataset
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pipelined(pipelined_lake):
    """The session's medallion lake (see ``conftest.pipelined_lake``).

    Delegated rather than rebuilt so the whole suite runs one pipeline, not one
    per module that needs data.
    """
    return pipelined_lake


def test_pipeline_produces_every_zone(pipelined) -> None:
    _, _ = pipelined
    lake = local_lake()
    assert set(lake.tables("bronze")) >= {"s_crm_customers", "s_core_transactions"}
    assert set(lake.tables("silver")) >= {"customers", "transactions", "accounts", "loans"}
    assert set(lake.tables("gold")) == {"customer_360", "transaction", "credit_risk"}
    assert set(lake.tables("ops")) >= {"dq_results", "lineage_edges", "sla_status", "fraud_ground_truth"}


def test_customer_360_grain_is_one_row_per_customer(pipelined) -> None:
    _, _ = pipelined
    lake = local_lake()
    gold = lake.read("gold", "customer_360")
    silver = lake.read("silver", "customers").filter(pl.col("is_current"))
    assert gold.height == silver.height
    assert gold["customer_id"].n_unique() == gold.height


def test_customer_360_segment_and_risk_are_never_null(pipelined) -> None:
    _, _ = pipelined
    gold = local_lake().read("gold", "customer_360")
    assert gold["customer_segment"].null_count() == 0
    assert gold["risk_category"].null_count() == 0
    assert set(gold["customer_segment"].unique()).issubset({"Mass", "Affluent", "HNI", "Premium"})


def test_transaction_grain_is_one_row_per_transaction(pipelined) -> None:
    _, _ = pipelined
    gold = local_lake().read("gold", "transaction")
    assert gold["transaction_id"].n_unique() == gold.height
    assert gold.height > 0


def test_recent_fraud_labels_are_withheld_from_gold(pipelined) -> None:
    """The central honesty guarantee of the whole evaluation.

    Transactions inside the open dispute window must carry a NULL fraud_flag, so
    the fraud agent cannot read the answer it is being graded on.
    """
    _, _ = pipelined
    gold = local_lake().read("gold", "transaction")
    pending = gold.filter(pl.col("fraud_label_status") == "PENDING")
    assert pending.height > 0, "the dataset must contain transactions inside the dispute window"
    assert pending["fraud_flag"].null_count() == pending.height
    assert set(gold["fraud_label_status"].unique()).issubset({"CONFIRMED", "CLEARED", "PENDING"})
    # Confirmed history is still exposed, which is what a real bank knows.
    assert gold.filter(pl.col("fraud_flag") == True).height > 0  # noqa: E712


def test_credit_risk_dti_and_score_are_within_published_bands(pipelined) -> None:
    _, _ = pipelined
    credit = local_lake().read("gold", "credit_risk")
    assert credit["customer_id"].n_unique() == credit.height
    scored = credit.filter(pl.col("risk_score").is_not_null())
    assert scored.filter((pl.col("risk_score") < 0) | (pl.col("risk_score") > 100)).height == 0
    assert set(credit["risk_band"].unique()).issubset({"LOW", "MEDIUM", "HIGH", "VERY_HIGH"})


def test_region_conflict_survivorship_is_recorded(pipelined) -> None:
    _, _ = pipelined
    accounts = local_lake().read("silver", "accounts")
    conflicts = accounts.filter(pl.col("region_conflict_flag"))
    assert conflicts.height > 0, "the documented region disagreement must be visible"
    # Both values are preserved rather than one overwriting the other.
    assert conflicts.filter(pl.col("branch_region") != pl.col("customer_region")).height > 0


def test_quality_incident_is_recorded_as_a_warning_not_a_silent_pass(pipelined) -> None:
    _, _ = pipelined
    dq = local_lake().read("ops", "dq_results")
    incidents = dq.filter(pl.col("rule_id") == "bronze_impossible_negative_balances")
    assert incidents.height == 1
    assert incidents["status"][0] == "FAIL"
    assert incidents["severity"][0] == "warning"


def test_no_critical_quality_failures_in_gold(pipelined) -> None:
    _, _ = pipelined
    dq = local_lake().read("ops", "dq_results")
    summary = summarise(dq.to_dicts())
    assert summary["critical_failures"] == [], summary["critical_failures"]
    assert summary["by_status"].get("ERROR", 0) == 0, "a rule errored instead of evaluating"


def test_sla_status_covers_every_product_and_is_fresh(pipelined) -> None:
    _, _ = pipelined
    sla = local_lake().read("ops", "sla_status")
    assert set(sla["product"].unique()) == {"customer_360", "transaction", "credit_risk"}
    assert sla.filter(pl.col("status") != "OK").height == 0


def test_lineage_is_queryable_and_traversable(pipelined) -> None:
    _, _ = pipelined
    lake = local_lake()
    assert "silver.transactions" in upstream_of(lake, "gold.transaction")
    routes = trace(lake, "gold.customer_360", direction="upstream")
    datasets = {r["dataset"] for r in routes}
    assert "silver.customers" in datasets
    assert "bronze.s_crm_customers" in datasets, "traversal must reach the source system"


def test_pipeline_is_reproducible_across_runs(pipelined) -> None:
    """Re-running the transforms over the same landing data yields the same content.

    Row *order* is deliberately not asserted: the query engine is free to return
    a hash-join result in any order, and a data product is defined by its
    contents and grain, not by physical ordering. Consumers ORDER BY explicitly.
    """
    from banking_data_agents.pipeline.runner import run_pipeline

    first = local_lake().read("gold", "customer_360").sort("customer_id")
    run_pipeline(verbose=False)
    second = local_lake().read("gold", "customer_360").sort("customer_id")

    assert first.schema == second.schema, "the published schema must not drift between runs"
    assert first.equals(second), "re-running over identical input must be a no-op"


def test_transaction_product_is_also_reproducible(pipelined) -> None:
    from banking_data_agents.pipeline.runner import run_pipeline

    first = local_lake().read("gold", "transaction").sort("transaction_id")
    run_pipeline(verbose=False)
    second = local_lake().read("gold", "transaction").sort("transaction_id")
    assert first.equals(second)


# ---------------------------------------------------------------------------
# Contracts must describe the data that was actually published
# ---------------------------------------------------------------------------
def test_contracts_match_the_published_gold_tables(pipelined) -> None:
    """A contract that lies about its own columns fails the build."""
    from banking_data_agents.catalog.contracts import validate_contracts

    problems = validate_contracts(local_lake())
    assert problems == [], "\n".join(problems)


def test_contract_primary_keys_are_unique_in_the_published_data(pipelined) -> None:
    from banking_data_agents.catalog.contracts import load_contracts

    lake = local_lake()
    for contract in load_contracts().values():
        frame = lake.read("gold", contract.product)
        keys = contract.primary_key
        assert frame.select(keys).unique().height == frame.height, (
            f"{contract.product} violates its declared primary key {keys}"
        )


def test_catalog_reads_live_operational_status(pipelined) -> None:
    from banking_data_agents.catalog.registry import Catalog

    catalog = Catalog(lake=local_lake())
    products = catalog.list_products()
    assert len(products) == 3
    for product in products:
        assert product["row_count"] > 0, f"{product['product']} reported no rows"
        assert product["status"] in {"OK", "DEGRADED"}

    status = catalog.status()
    assert status["products"] == 3
    assert status["metrics"] > 10


def test_catalog_lineage_reaches_the_source_systems(pipelined) -> None:
    from banking_data_agents.catalog.registry import Catalog

    catalog = Catalog(lake=local_lake())
    sources = catalog.upstream_sources("customer_360")
    assert "bronze.s_crm_customers" in sources
    assert "bronze.s_core_accounts" in sources


def test_bronze_excludes_the_fraud_holdout(pipelined) -> None:
    landing, _ = pipelined
    lake = local_lake()
    assert not any("holdout" in table for table in lake.tables("bronze"))
    # The holdout exists on disk but is never ingested as a source system.
    assert (landing / "_holdout" / "fraud_labels").exists()
    assert set(get_settings().all_buckets)  # settings still resolve under the override


def test_dq_evaluation_is_idempotent(pipelined) -> None:
    lake = local_lake()
    first = evaluate_all(lake)
    second = evaluate_all(lake)
    assert [r["status"] for r in first] == [r["status"] for r in second]


# ---------------------------------------------------------------------------
# Optional upstream columns
# ---------------------------------------------------------------------------
def test_schema_drift_is_absorbed_and_visible(pipelined) -> None:
    """The CRM's additive change must be absorbed, not swallowed.

    Batches before the change default; batches after it carry real values. The
    drift is therefore visible to an analyst rather than silently uniform.
    """
    lake = local_lake()
    bronze = lake.read("bronze", "s_crm_customers")
    assert "preferred_language" in bronze.columns
    assert bronze["preferred_language"].null_count() > 0, "earlier batches predate the change"

    customers = lake.read("silver", "customers")
    observed = set(customers["preferred_language"].unique())
    assert "UNKNOWN" in observed, "pre-drift history must default, not disappear"
    assert observed - {"UNKNOWN"}, "post-drift history must carry real values"

    per_batch = (
        customers.group_by("valid_from_batch")
        .agg((pl.col("preferred_language") == "UNKNOWN").sum().alias("unknown_count"))
        .sort("valid_from_batch")
    )
    assert per_batch["unknown_count"].to_list()[0] > 0
    assert per_batch["unknown_count"].to_list()[-1] == 0


def test_optional_column_falls_back_when_upstream_has_not_shipped_it(tmp_path: Path) -> None:
    """A short window predates the CRM's additive change entirely.

    This is the case that breaks naive pipelines: the column is not merely null
    in some batches, it does not exist at all. The transform must still run and
    the column must still be present with a declared default.
    """
    from pytest import MonkeyPatch

    from banking_data_agents.pipeline.runner import run_pipeline

    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("BDA_DATA_DIR", str(tmp_path / "lake"))
    monkeypatch.setenv("BDA_LANDING_DIR", str(tmp_path / "landing"))
    monkeypatch.setenv("SYNTHETIC_MONTHS", "3")
    reset_settings_cache()
    try:
        generate_into(tmp_path / "landing", seed=7, scale=0.25)
        assert run_pipeline(verbose=False) == 0

        lake = local_lake()
        assert "preferred_language" not in lake.read("bronze", "s_crm_customers").columns

        customers = lake.read("silver", "customers")
        assert "preferred_language" in customers.columns
        assert customers["preferred_language"].null_count() == 0
        assert set(customers["preferred_language"].unique()) == {"UNKNOWN"}
    finally:
        monkeypatch.undo()
        reset_settings_cache()
