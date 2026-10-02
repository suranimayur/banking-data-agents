"""Synthetic data must be reproducible, well-formed, and deliberately imperfect.

The determinism test is the load-bearing one: the golden evaluation datasets are
only meaningful if the same seed always produces the same rows.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import polars as pl
import pytest

from banking_data_agents.datagen import reference as ref
from banking_data_agents.datagen.generate import dataset_fingerprints, generate_into
from banking_data_agents.datagen.generators import SyntheticBank, month_windows, rng_for

TEST_SEED = 4242


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    landing = tmp_path_factory.mktemp("landing_a")
    summary = generate_into(landing, seed=TEST_SEED, scale=0.25)
    return landing, summary


def _read(landing: Path, source: str, entity: str, batch: str | None = None) -> pl.DataFrame:
    base = landing / source / entity
    paths = sorted(base.glob("batch_month=*/part-000.parquet"))
    if batch:
        paths = [p for p in paths if f"batch_month={batch}" in p.as_posix()]
    assert paths, f"no parquet written under {base}"
    return pl.concat([pl.read_parquet(p) for p in paths], how="diagonal_relaxed")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_same_seed_produces_identical_data(tmp_path: Path) -> None:
    first = generate_into(tmp_path / "a", seed=TEST_SEED, scale=0.25)
    second = generate_into(tmp_path / "b", seed=TEST_SEED, scale=0.25)
    assert dataset_fingerprints(first) == dataset_fingerprints(second)
    assert first["holdout"]["fingerprint"] == second["holdout"]["fingerprint"]


def test_different_seed_produces_different_data(tmp_path: Path) -> None:
    first = generate_into(tmp_path / "a", seed=TEST_SEED, scale=0.25)
    second = generate_into(tmp_path / "b", seed=TEST_SEED + 1, scale=0.25)
    assert dataset_fingerprints(first) != dataset_fingerprints(second)


def test_rng_derivation_is_stable_and_independent() -> None:
    assert rng_for(1, "a").random() == rng_for(1, "a").random()
    assert rng_for(1, "a").random() != rng_for(1, "b").random()


def test_month_windows_are_contiguous_and_labelled() -> None:
    from datetime import date

    windows = month_windows(14, date(2025, 10, 1))
    assert windows[0].label == "2025-10"
    assert windows[-1].label == "2026-11"
    assert windows[0].batch_id == "2025-10-01"
    for earlier, later in itertools.pairwise(windows):
        assert earlier.end == later.start, "windows must not leave gaps"
        assert later.index == earlier.index + 1


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_every_source_system_is_produced(generated: tuple[Path, dict]) -> None:
    landing, summary = generated
    assert {entry["source"] for entry in summary["datasets"]} == {
        "s_crm",
        "s_core",
        "s_loans",
        "s_cards",
        "s_bureau",
        "s_payments",
    }
    for entry in summary["datasets"]:
        assert (landing / entry["path"]).exists(), entry["path"]


def test_provenance_columns_present_on_every_dataset(generated: tuple[Path, dict]) -> None:
    _, summary = generated
    required = {"_ingested_at", "_source_system", "_source_file", "_batch_id", "_record_hash"}
    for entry in summary["datasets"]:
        assert required.issubset(set(entry["columns"])), f"{entry['source']}/{entry['entity']}"


def test_customers_reference_known_states(generated: tuple[Path, dict]) -> None:
    landing, summary = generated
    # One row per customer per *monthly snapshot*, so uniqueness holds within a batch.
    customers = _read(landing, "s_crm", "customers", summary["datasets"][0]["batch"])
    assert set(customers["state_code"].unique()).issubset(set(ref.STATES))
    assert customers["customer_id"].n_unique() == customers.height
    assert customers["region"].null_count() == 0


def test_every_account_is_owned_and_unique(generated: tuple[Path, dict]) -> None:
    landing, _ = generated
    accounts = _read(landing, "s_core", "accounts", "2025-10")
    assert accounts["account_id"].n_unique() == accounts.height
    customers = _read(landing, "s_crm", "customers")
    assert set(accounts["customer_id"].unique()).issubset(set(customers["customer_id"].unique()))
    # Every customer must own at least one account, or the Customer 360 product
    # would silently lose customers.
    assert set(customers["customer_id"].unique()).issubset(set(accounts["customer_id"].unique()))


def test_transaction_ids_are_unique_across_injected_fraud(generated: tuple[Path, dict]) -> None:
    landing, _ = generated
    core = _read(landing, "s_core", "transactions")
    card = _read(landing, "s_cards", "card_transactions")
    payments = _read(landing, "s_payments", "payment_transactions")
    ids = pl.concat([core["transaction_id"], card["transaction_id"], payments["transaction_id"]])
    assert ids.n_unique() == ids.len()


# ---------------------------------------------------------------------------
# Deliberate imperfection — the properties the platform is built to reason about
# ---------------------------------------------------------------------------
def test_region_conflict_exists_between_crm_and_core(generated: tuple[Path, dict]) -> None:
    """4% of accounts sit in a different region than the customer's address."""
    landing, _ = generated
    customers = _read(landing, "s_crm", "customers").select("customer_id", "region")
    accounts = _read(landing, "s_core", "accounts", "2025-10").select("customer_id", "branch_region")
    joined = customers.join(accounts, on="customer_id", how="inner")
    conflicts = joined.filter(pl.col("region") != pl.col("branch_region"))
    assert conflicts.height > 0, "the region conflict is a documented feature of the dataset"


def test_core_accounts_are_damaged_in_the_incident_month() -> None:
    bank = SyntheticBank(
        seed=TEST_SEED, n_customers=60, n_accounts=90, n_loans=20, n_cards=50, months=12, fraud_rate=0.01
    )
    healthy = bank.accounts_snapshot(bank.windows[ref.DQ_INCIDENT_MONTH - 2])
    damaged = bank.accounts_snapshot(bank.windows[ref.DQ_INCIDENT_MONTH - 1])

    assert healthy["branch_region"].null_count() == 0
    assert damaged["branch_region"].null_count() > 0, "the DQ incident must be observable"
    savings = damaged.filter(pl.col("account_type") == "SAVINGS")
    assert savings.filter(pl.col("balance") < 0).height > 0, "impossible negative balances"


def test_crm_schema_drifts_from_month_seven() -> None:
    bank = SyntheticBank(
        seed=TEST_SEED, n_customers=60, n_accounts=90, n_loans=20, n_cards=50, months=12, fraud_rate=0.01
    )
    before = bank.customers_snapshot(bank.windows[ref.SCHEMA_DRIFT_MONTH - 2])
    after = bank.customers_snapshot(bank.windows[ref.SCHEMA_DRIFT_MONTH - 1])
    assert "preferred_language" not in before.columns
    assert "preferred_language" in after.columns
    # The drift is additive: no previously-available column disappears.
    assert set(before.columns).issubset(set(after.columns))


def test_seasonality_creates_visible_month_to_month_variation(generated: tuple[Path, dict]) -> None:
    _landing, summary = generated
    counts = {}
    for entry in summary["datasets"]:
        if entry["entity"] == "transactions":
            counts[entry["batch"]] = entry["rows"]
    assert len(counts) >= 3
    assert max(counts.values()) > min(counts.values()), "transaction volume must vary by month"


# ---------------------------------------------------------------------------
# Fraud holdout discipline
# ---------------------------------------------------------------------------
def test_fraud_labels_reference_real_transactions(generated: tuple[Path, dict]) -> None:
    landing, summary = generated
    labels = pl.read_parquet(landing / "_holdout" / "fraud_labels" / "part-000.parquet")
    assert labels.height == summary["holdout"]["rows"] > 0
    assert labels["transaction_id"].n_unique() == labels.height

    core = _read(landing, "s_core", "transactions").select("transaction_id")
    card = _read(landing, "s_cards", "card_transactions").select("transaction_id")
    payments = _read(landing, "s_payments", "payment_transactions").select("transaction_id")
    universe = set(pl.concat([core, card, payments])["transaction_id"].to_list())
    assert set(labels["transaction_id"].to_list()).issubset(universe)


def test_all_five_fraud_methods_are_injected() -> None:
    """At scale, every modus operandi must appear — otherwise the evaluator is blind."""
    bank = SyntheticBank(
        seed=TEST_SEED, n_customers=900, n_accounts=1_400, n_loans=200, n_cards=800, months=14, fraud_rate=0.02
    )
    methods: set[str] = set()
    for window in bank.windows[:6]:
        core = bank.core_transactions(window)
        card = bank.card_transactions(window)
        payments = bank.payment_transactions(window)
        _, _, _, labels = bank.inject_fraud(window, core, card, payments)
        methods |= set(labels["fraud_method"].unique().to_list())
    assert methods == set(ref.FRAUD_METHODS)


def test_source_systems_carry_no_fraud_label(generated: tuple[Path, dict]) -> None:
    """The truth must live only in the holdout, or the agent is cheating."""
    _, summary = generated
    for entry in summary["datasets"]:
        assert "is_fraud" not in entry["columns"]
        assert "fraud_method" not in entry["columns"]


def test_holdout_is_not_inside_any_published_source_path() -> None:
    from banking_data_agents.datagen.generate import SOURCES

    assert not any("_holdout" in entity for entities in SOURCES.values() for entity in entities)
