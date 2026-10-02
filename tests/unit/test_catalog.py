"""Governance must be enforceable, not decorative.

These tests assert the two behaviours that separate a governed platform from a
chatbot over a database: a prohibited use is actually refused with a citable
reason, and an ambiguous term produces a question rather than a guess.
"""

from __future__ import annotations

import pytest

from banking_data_agents.catalog.contracts import (
    contract_digest,
    load_contracts,
    reload_contracts,
)
from banking_data_agents.catalog.registry import Catalog
from banking_data_agents.semantic.metrics import load_metrics, resolve_metric


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    # No lake: these tests exercise contract and metric logic, not live data.
    return Catalog(lake=None)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
def test_every_published_product_has_a_contract() -> None:
    contracts = load_contracts()
    assert set(contracts) == {"customer_360", "transaction", "credit_risk"}


def test_contracts_declare_the_governance_fields() -> None:
    for contract in load_contracts().values():
        assert contract.owner, f"{contract.product} has no owner"
        assert contract.version, f"{contract.product} has no version"
        assert contract.grain, f"{contract.product} has no declared grain"
        assert contract.primary_key, f"{contract.product} has no primary key"
        assert contract.allowed_use, f"{contract.product} declares no allowed uses"
        assert contract.columns, f"{contract.product} declares no columns"


def test_contract_digest_is_stable_and_changes_with_content() -> None:
    first = contract_digest()
    reload_contracts()
    assert contract_digest() == first
    assert len(first) == 16


@pytest.mark.parametrize(
    ("product", "column", "use", "expected"),
    [
        # Permitted.
        ("customer_360", "total_balance", "portfolio_analytics", True),
        ("credit_risk", "credit_score", "credit_assessment", True),
        ("transaction", "amount", "fraud_detection", True),
        # Prohibited on the column.
        ("customer_360", "credit_score", "marketing_targeting_on_credit_score", False),
        ("customer_360", "age", "marketing_targeting_on_age", False),
        # Prohibited on the whole product.
        ("credit_risk", "dti", "marketing_targeting", False),
        ("credit_risk", "risk_score", "automated_credit_decision", False),
        # Not in the column's allow-list.
        ("transaction", "fraud_flag", "customer_facing_scoring", False),
    ],
)
def test_usability_rules(product: str, column: str, use: str, expected: bool) -> None:
    contract = load_contracts()[product]
    allowed, reason = contract.usability(column, use)
    assert allowed is expected, f"{product}.{column} + {use}: {reason}"
    assert reason


def test_a_refusal_reasons_name_the_contract_and_the_field(catalog: Catalog) -> None:
    """A refusal the user cannot understand is just an error message."""
    allowed, reason = catalog.usability("customer_360", "credit_score", "marketing_targeting_on_credit_score")
    assert allowed is False
    assert "customer_360@" in reason
    assert "not_allowed_use" in reason


def test_unknown_column_is_refused_rather_than_ignored() -> None:
    allowed, reason = load_contracts()["customer_360"].usability("salary", "portfolio_analytics")
    assert allowed is False
    assert "not part of" in reason


def test_contract_renders_a_prompt_block_for_sql_generation() -> None:
    prompt = load_contracts()["credit_risk"].to_prompt()
    assert "PRODUCT credit_risk@" in prompt
    assert "dti" in prompt
    assert "metric.dti@" in prompt


def test_credits_risk_contract_documents_the_scoring_caveat() -> None:
    """The scorecard must not be mistaken for a probability of default."""
    contract = load_contracts()["credit_risk"]
    risk_score = contract.column("risk_score")
    assert risk_score is not None
    assert risk_score.caveat and "probability of default" in risk_score.caveat


def test_transaction_contract_documents_the_fraud_label_semantics() -> None:
    contract = load_contracts()["transaction"]
    fraud_flag = contract.column("fraud_flag")
    assert fraud_flag is not None
    assert fraud_flag.caveat and "NULL" in fraud_flag.caveat
    assert contract.column("fraud_label_status") is not None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def test_every_metric_reference_in_every_contract_resolves() -> None:
    """A dangling metric_ref would let an agent cite a definition that does not exist."""
    metrics = load_metrics()
    for contract in load_contracts().values():
        for column in contract.columns:
            if column.metric_ref:
                name = column.metric_ref.split("@")[0].split(".", 1)[-1]
                assert name in metrics, f"{contract.product}.{column.name} -> {column.metric_ref}"


def test_every_metric_declares_version_owner_and_definition() -> None:
    for metric in load_metrics().values():
        assert metric.version, f"{metric.name} has no version"
        assert metric.owner, f"{metric.name} has no owner"
        assert metric.definition, f"{metric.name} has no definition"
        assert metric.sql_fragment, f"{metric.name} has no SQL fragment"


def test_metric_ref_format_is_the_published_one() -> None:
    assert load_metrics()["total_balance"].ref == "metric.total_balance@2.1.0"


def test_metric_caveats_survive_loading() -> None:
    """The currency-mixing hazard must reach the agent, not be lost in YAML."""
    metric = load_metrics()["txn_amount_sum"]
    assert metric.caveats
    assert "USD" in " ".join(metric.caveats)


def test_parameterised_metric_refuses_to_guess() -> None:
    metric = load_metrics()["high_value_customer"]
    assert metric.requires_parameters
    with pytest.raises(ValueError, match="requires parameter"):
        metric.render()


def test_parameterised_metric_renders_when_given_the_parameter() -> None:
    metric = load_metrics()["high_value_customer"]
    sql = metric.render({"threshold": 1_000_000})
    assert "1000000" in sql
    assert "?" not in sql


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_exact_name_resolves() -> None:
    result = resolve_metric("total_balance")
    assert result.resolved is not None
    assert result.resolved.name == "total_balance"
    assert result.candidates[0].matched_on == "name"


def test_synonym_resolves() -> None:
    result = resolve_metric("card spend")
    assert result.resolved is not None
    assert result.resolved.name in {"card_spend_90d"}
    assert result.candidates[0].matched_on in {"synonym", "phrase_contains"}


def test_a_bare_ambiguous_term_produces_a_question_not_a_guess() -> None:
    """'risk' could mean a category, a band or a score. The agent must ask."""
    result = resolve_metric("risk")
    assert result.is_ambiguous, result.as_dict()
    assert result.resolved is None
    names = {c.metric.name for c in result.candidates}
    assert {"risk_category", "risk_score", "risk_band"} <= names


def test_an_unresolvable_term_is_reported_as_unresolved() -> None:
    result = resolve_metric("zzzqqq nonexistent thing")
    assert result.is_unresolved
    assert result.resolved is None


def test_resolution_is_scoped_by_product() -> None:
    scoped = resolve_metric("outstanding", product="credit_risk")
    assert scoped.candidates
    assert all(c.metric.product == "credit_risk" for c in scoped.candidates)


def test_resolution_exposes_alternatives_for_the_agent_to_offer() -> None:
    payload = resolve_metric("high value customer").as_dict()
    assert payload["status"] in {"resolved", "ambiguous"}
    assert payload["candidates"]
    first = payload["candidates"][0]
    assert set(first) >= {"ref", "definition", "unit", "matched_on", "requires_parameters"}


# ---------------------------------------------------------------------------
# Catalog search
# ---------------------------------------------------------------------------
def test_search_finds_the_product_by_column_vocabulary(catalog: Catalog) -> None:
    results = catalog.search("card spend")
    assert results
    assert results[0]["product"] == "customer_360"


def test_search_finds_products_by_domain_and_metric(catalog: Catalog) -> None:
    assert catalog.search("delinquency")[0]["product"] == "credit_risk"
    assert catalog.search("fraud")[0]["product"] == "transaction"


def test_search_returns_everything_for_an_empty_query(catalog: Catalog) -> None:
    assert len(catalog.search("")) == 3


def test_catalog_resolves_aliases_for_product_names(catalog: Catalog) -> None:
    assert catalog.get_contract("gold.customer_360").product == "customer_360"
    assert catalog.get_contract("CUSTOMER-360").product == "customer_360"
    with pytest.raises(KeyError, match="Available"):
        catalog.get_contract("nonexistent_product")


def test_product_summaries_carry_governance_and_trust_signals(catalog: Catalog) -> None:
    for summary in catalog.list_products():
        assert set(summary) >= {
            "product",
            "version",
            "owner",
            "grain",
            "columns",
            "metrics",
            "sla_freshness_hours",
            "status",
            "allowed_use",
            "not_allowed_use",
        }
