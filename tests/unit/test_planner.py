"""The planner is the contract between a question and a governed number.

These tests are deliberately written as *expected SQL*, not as "it returned
something". The planner is rule-based precisely so that a question has one
reviewable answer; if a change makes "customers by region" compile differently,
that should fail here and be argued about in review, not discovered in a
dashboard.

They also pin the behaviours that are easy to lose in a refactor and expensive to
lose in production: asking instead of guessing, refusing to invent a metric, and
never emitting SQL the guardrail would reject.
"""

from __future__ import annotations

from datetime import date

import pytest

from banking_data_agents.semantic.planner import (
    DEFAULT_RANKING_LIMIT,
    explain_plan,
    plan_and_compile,
    plan_question,
)
from banking_data_agents.tools.sqlguard import validate_sql

ANCHOR = date(2026, 9, 30)


def compile_question(question: str) -> str:
    plan, sql = plan_and_compile(question, anchor=ANCHOR)
    assert sql is not None, f"{question!r} did not compile: {explain_plan(plan)}"
    return " ".join(sql.split())


# ---------------------------------------------------------------------------
# Straightforward planning
# ---------------------------------------------------------------------------
def test_count_by_region_compiles_to_a_grouped_count() -> None:
    sql = compile_question("How many customers do we have by region?")
    assert 'SELECT "region", COUNT(*) AS customer_count' in sql
    assert "FROM gold.customer_360" in sql
    assert 'GROUP BY "region"' in sql
    assert sql.endswith("LIMIT 100;")


def test_unqualified_sum_is_a_single_scalar_row() -> None:
    plan, sql = plan_and_compile("What is the total balance by region?", anchor=ANCHOR)
    assert plan.limit == 100  # grouped questions default to the grouped limit
    assert sql is not None
    scalar_plan, scalar_sql = plan_and_compile("What is the total balance?", anchor=ANCHOR)
    assert scalar_plan.limit == 1
    assert scalar_sql is not None and scalar_sql.rstrip().endswith("LIMIT 1;")


def test_top_n_by_a_metric_groups_by_the_entity_and_limits() -> None:
    sql = compile_question("Top 5 customers by card spend")
    assert 'SELECT "customer_id", SUM(card_spend_90d) AS card_spend_90d' in sql
    assert 'GROUP BY "customer_id"' in sql
    assert sql.endswith("LIMIT 5;")


def test_superlative_without_a_number_is_a_ranking_not_a_full_scan() -> None:
    """\"Who spends the most on cards\" names an entity, so it means the leaders.

    This is the case that separates a planner from a keyword grep: the metric
    phrase is not contiguous (\"spend ... cards\"), the group key is not named by a
    ``by`` clause, and no number is given, yet the intent is unambiguous.
    """
    plan, sql = plan_and_compile("Who spends the most on cards?", anchor=ANCHOR)
    assert plan.dimensions == ["customer_id"]
    assert plan.limit == DEFAULT_RANKING_LIMIT
    assert plan.descending is True
    assert sql is not None
    assert 'GROUP BY "customer_id"' in sql


def test_superlative_ranking_can_ascend() -> None:
    plan, _ = plan_and_compile("Which customers spend the least on cards?", anchor=ANCHOR)
    assert plan.dimensions == ["customer_id"]
    assert plan.descending is False


def test_superlative_without_an_entity_stays_a_scalar() -> None:
    """\"the highest balance\" names no entity, so it is one number, not a top-10."""
    plan, _ = plan_and_compile("What is the highest balance?", anchor=ANCHOR)
    assert plan.dimensions == []
    assert plan.limit == 1


# ---------------------------------------------------------------------------
# Grouping keys versus metric names
# ---------------------------------------------------------------------------
def test_a_grouping_key_is_not_stolen_by_a_metric_of_the_same_name() -> None:
    """`segment` names both the customer_segment metric and the column.

    \"by segment\" is a request for a breakdown. Reading it as the metric would
    drop the GROUP BY and answer a different question.
    """
    plan, sql = plan_and_compile("Show total balance by segment", anchor=ANCHOR)
    assert plan.dimensions == ["customer_segment"]
    assert sql is not None
    assert 'GROUP BY "customer_segment"' in sql


def test_a_multi_word_dimension_beats_a_shorter_metric_synonym() -> None:
    """\"by marketing segment\" must pick marketing_segment, not the segment metric."""
    plan, sql = plan_and_compile("Show total balance by marketing segment", anchor=ANCHOR)
    assert plan.dimensions == ["marketing_segment"]
    assert sql is not None
    assert 'GROUP BY "marketing_segment"' in sql


def test_a_relative_time_phrase_is_not_a_monthly_breakdown() -> None:
    """ "the last 3 months" filters; it does not ask to group by month."""
    plan, sql = plan_and_compile("How many transactions happened in the last 3 months?", anchor=ANCHOR)
    assert plan.dimensions == []
    assert plan.time_window is not None
    assert sql is not None
    assert "GROUP BY" not in sql
    assert "WHERE transaction_date >= DATE '2026-06-30'" in sql


def test_an_explicit_month_breakdown_still_works() -> None:
    plan, sql = plan_and_compile("Show monthly transaction volume for the last 6 months", anchor=ANCHOR)
    assert plan.dimensions == ["transaction_month"]
    assert plan.time_window is not None
    assert sql is not None


# ---------------------------------------------------------------------------
# Asking rather than guessing
# ---------------------------------------------------------------------------
def test_an_ambiguous_term_produces_options_not_a_number() -> None:
    plan, sql = plan_and_compile("What is our exposure?", anchor=ANCHOR)
    assert sql is None
    assert plan.is_ambiguous
    assert "exposure" in plan.ambiguous_terms
    # Both readings must be offered, from both products.
    products = {candidate.metric.product for ambiguity in plan.ambiguities for candidate in ambiguity.candidates}
    assert {"credit_risk", "customer_360"}.issubset(products)


def test_a_required_parameter_is_never_defaulted() -> None:
    plan, sql = plan_and_compile("How many high value customers do we have?", anchor=ANCHOR)
    assert sql is None
    assert plan.requires_parameters
    assert plan.requires_parameters[0].metric.parameters[0].name == "threshold"


def test_an_unknown_measure_is_reported_not_approximated() -> None:
    plan, sql = plan_and_compile("What is the weather in Mumbai?", anchor=ANCHOR)
    assert sql is None
    assert not plan.has_metrics
    assert "weather" in plan.missing_terms


def test_a_bare_entity_noun_is_not_a_measure() -> None:
    """ "customers" and "transactions" are things, not quantities.

    Both are metric synonyms, so a phrase matcher will happily read either as a
    count. Nothing in the question asks to count anything, so the honest outcome is
    "no governed measure", not a number arrived at by guessing.
    """
    plan, sql = plan_and_compile("Which customers have the fewest transactions?", anchor=ANCHOR)
    assert sql is None
    assert not plan.has_metrics
    assert plan.missing_terms


def test_a_degenerate_row_count_at_primary_key_grain_is_declined() -> None:
    """A row count per customer is 1 for every customer.

    Exercised directly rather than through a question, because the rule is about
    the plan's shape and the phrase matcher now filters most of these earlier.
    Returning ``COUNT(*) = 1`` for everybody would be arithmetic dressed up as
    insight, and the cross-product gap is worth naming.
    """
    from banking_data_agents.catalog.contracts import load_contracts
    from banking_data_agents.semantic.metrics import metrics_for_product
    from banking_data_agents.semantic.planner import MetricInstance, Plan, _prune_metrics

    contract = load_contracts()["customer_360"]
    row_count = next(metric for metric in metrics_for_product("customer_360") if metric.name == "customer_count")
    plan = Plan(
        question="count per customer",
        product="customer_360",
        dimensions=["customer_id"],
        metrics=[MetricInstance(metric=row_count)],
    )
    assert _prune_metrics(plan, contract, "count per customer") == []
    assert any("cross-product" in note for note in plan.notes)


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------
def test_a_requested_average_does_not_return_a_sum() -> None:
    """ "the average balance\" and \"the total balance\" are different questions."""
    plan, sql = plan_and_compile("Which region has the highest average balance?", anchor=ANCHOR)
    assert "AVG(total_balance)" in (sql or "")
    assert [m.metric.name for m in plan.metrics] == ["avg_balance_per_customer"]


def test_sum_and_average_remain_distinct_metrics() -> None:
    _, summed = plan_and_compile("Show total balance by region", anchor=ANCHOR)
    _, averaged = plan_and_compile("Show average balance by region", anchor=ANCHOR)
    assert "SUM(total_balance)" in (summed or "")
    assert "AVG(total_balance)" in (averaged or "")
    assert summed != averaged


# ---------------------------------------------------------------------------
# Every compiled plan must survive the guardrail
# ---------------------------------------------------------------------------
QUESTIONS = [
    "How many customers do we have by region?",
    "What is the total balance by region?",
    "Show total balance by segment",
    "Show total balance by marketing segment",
    "Top 5 customers by card spend",
    "Who spends the most on cards?",
    "Which customers spend the least on cards?",
    "Show the top 10 customers by total balance",
    "Average ticket size by channel group in 2026",
    "What is the international share by merchant country?",
    "How many transactions happened in the last 3 months?",
    "Show monthly transaction volume for the last 6 months",
    "What is the delinquency rate by risk band?",
    "How many customers have a balance over 500000?",
]


@pytest.mark.parametrize("question", QUESTIONS)
def test_planner_never_emits_sql_the_guardrail_would_reject(question: str) -> None:
    """The planner and the guardrail must agree, always.

    These are two independent implementations of the same policy — the planner
    builds SQL, ``sqlguard`` parses it — so a disagreement is a real bug in one of
    them rather than a style question. Asserting it here means a planner change
    that starts emitting ``SELECT *`` or touching a forbidden dataset fails at the
    point of the change.
    """
    _plan, sql = plan_and_compile(question, anchor=ANCHOR)
    if sql is None:
        return  # nothing to check; the other tests pin why it declined
    result = validate_sql(sql)
    assert result.valid, f"{question!r} produced SQL the guardrail rejects: {result.violations}"
    assert not result.violations


@pytest.mark.parametrize("question", QUESTIONS)
def test_planner_is_deterministic(question: str) -> None:
    first = plan_question(question, anchor=ANCHOR).as_dict()
    second = plan_question(question, anchor=ANCHOR).as_dict()
    assert first == second


def test_relative_windows_resolve_against_the_data_not_the_wall_clock() -> None:
    """A dataset generated last quarter must still answer about its own quarter."""
    plan, _ = plan_and_compile("How many transactions in the last 3 months?", anchor=date(2026, 9, 30))
    assert plan.time_window is not None
    assert plan.time_window.start is not None
    assert plan.time_window.start.isoformat() == "2026-06-30"
    assert plan.time_window.end is not None
    assert plan.time_window.end.isoformat() == "2026-09-30"
