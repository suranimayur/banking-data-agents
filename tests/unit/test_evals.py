"""Tests for the evaluation harness.

A gate nobody has tested is a gate nobody should trust. The first half checks that
each kind of assertion actually fires when the answer is wrong — a check that
cannot fail is decoration. The second half checks the scoring and the gate
arithmetic, including the property the whole design rests on: a critical case
fails the run however good the rest of the score is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from banking_data_agents.agents.evidence import Evidence, build_evidence
from banking_data_agents.evals.cases import SUITE, SUITES, Case, Expect, by_id
from banking_data_agents.evals.runner import (
    CaseResult,
    evaluate_case,
    gate,
    run_suite,
    score,
    summarise,
    write_report,
)

TRACE = [{"tool": "search_catalog"}, {"tool": "generate_sql"}, {"tool": "execute_sql"}]
OUTPUTS = {
    "generate_sql": [
        {
            "sql": 'SELECT "region", COUNT(*) AS customer_count FROM gold.customer_360 GROUP BY "region" LIMIT 100',
            "metrics_used": ["metric.customer_count@1.0.0"],
            "products_used": ["customer_360"],
            "notes": [],
            "plan": {"metrics": []},
        }
    ],
    "execute_sql": [
        {
            "executed": True,
            "row_count": 10,
            "columns": ["region", "customer_count"],
            "preview": [["Maharashtra", 372]],
            "tables": ["gold.customer_360"],
            "sql": 'SELECT "region", COUNT(*) AS customer_count FROM gold.customer_360 GROUP BY "region" LIMIT 100',
        }
    ],
}


def good_evidence(**overrides) -> Evidence:
    base = build_evidence(
        question="How many customers do we have by region?",
        answer="10 row(s) returned.",
        trace_id="t1",
        trace=TRACE,
        outputs=OUTPUTS,
        contract_digest="deadbeefcafe",
        contract_versions={"customer_360": "customer_360@2.3.0"},
        provider="stub",
    )
    # A real answer carries the quality state of the product it rests on, so the
    # fixture does too; otherwise the common case would be untested.
    base.quality = {"total": 9, "passed": 9, "failures": []}
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


GOOD = Expect(
    outcome="ANSWERED_FROM_DATA",
    metrics=("metric.customer_count@1.0.0",),
    sql_contains=('GROUP BY "region"',),
    sql_absent=("SELECT *",),
    tables=("gold.customer_360",),
    tools=("execute_sql",),
    forbid_tools=("check_allowed_use",),
    min_rows=1,
    max_rows=100,
    answer_contains=("10 row",),
    quality=True,
    cites_product=True,
)


# ---------------------------------------------------------------------------
# The suite is well formed
# ---------------------------------------------------------------------------
def test_case_ids_are_unique() -> None:
    ids = [case.id for case in SUITE]
    assert len(ids) == len(set(ids))


def test_every_case_explains_itself() -> None:
    for case in SUITE:
        assert case.question.strip(), case.id
        assert case.expect.outcome, case.id
        assert case.agent in {"copilot", "fraud", "credit"}, case.id


def test_policy_cases_are_marked_must_pass() -> None:
    """Anything tagged governance or security has to carry the run."""
    for case in SUITE:
        if set(case.tags) & {"governance", "security"}:
            assert case.must_pass, f"{case.id} is tagged as policy but is not must_pass"


def test_the_suite_covers_every_outcome_the_platform_can_produce() -> None:
    outcomes = {case.expect.outcome for case in SUITE}
    assert outcomes == {
        "ANSWERED_FROM_DATA",
        "ANSWERED_FROM_METADATA",
        "REFUSED",
        "NEEDS_CLARIFICATION",
        "NO_GOVERNED_METRIC",
    }


def test_every_specialist_agent_has_cases() -> None:
    agents = {case.agent for case in SUITE}
    assert agents == {"copilot", "fraud", "credit"}


def test_named_suites_point_at_the_same_cases() -> None:
    assert set(SUITES) == {"deterministic", "floci"}
    assert SUITES["floci"] == SUITES["deterministic"]


def test_an_unknown_case_id_is_an_error() -> None:
    with pytest.raises(KeyError):
        by_id("nope")
    assert by_id("clarify.ambiguous_term").agent == "copilot"


# ---------------------------------------------------------------------------
# Assertions must be able to fail
# ---------------------------------------------------------------------------
def test_a_correct_answer_passes_every_assertion() -> None:
    assert evaluate_case(good_evidence(), GOOD) == []


def test_a_wrong_outcome_is_caught() -> None:
    failures = evaluate_case(good_evidence(), Expect(outcome="REFUSED", cites_product=False))
    assert any("outcome" in failure for failure in failures)


def test_a_missing_metric_is_caught() -> None:
    failures = evaluate_case(good_evidence(), Expect(outcome="ANSWERED_FROM_DATA", metrics=("metric.nope@1.0.0",)))
    assert any("missing metric" in failure for failure in failures)


def test_a_forbidden_metric_is_caught() -> None:
    failures = evaluate_case(
        good_evidence(),
        Expect(outcome="ANSWERED_FROM_DATA", forbid_metrics=("metric.customer_count@1.0.0",)),
    )
    assert any("forbidden metric" in failure for failure in failures)


def test_a_missing_sql_fragment_is_caught() -> None:
    failures = evaluate_case(good_evidence(), Expect(outcome="ANSWERED_FROM_DATA", sql_contains=("GROUP BY nope",)))
    assert any("sql: missing" in failure for failure in failures)


def test_sql_that_should_not_be_there_is_caught() -> None:
    failures = evaluate_case(good_evidence(), Expect(outcome="ANSWERED_FROM_DATA", sql_absent=("GROUP BY",)))
    assert any("must not contain" in failure for failure in failures)


def test_a_missing_table_is_caught() -> None:
    failures = evaluate_case(good_evidence(), Expect(outcome="ANSWERED_FROM_DATA", tables=("gold.transaction",)))
    assert any("was not read" in failure for failure in failures)


def test_a_tool_that_should_not_have_been_called_is_caught() -> None:
    failures = evaluate_case(good_evidence(), Expect(outcome="ANSWERED_FROM_DATA", forbid_tools=("execute_sql",)))
    assert any("must not be called" in failure for failure in failures)


def test_a_tool_that_should_have_been_called_is_caught() -> None:
    failures = evaluate_case(good_evidence(), Expect(outcome="ANSWERED_FROM_DATA", tools=("check_allowed_use",)))
    assert any("was never called" in failure for failure in failures)


def test_row_bounds_are_enforced_both_ways() -> None:
    assert any(
        "at least" in f for f in evaluate_case(good_evidence(), Expect(outcome="ANSWERED_FROM_DATA", min_rows=99))
    )
    assert any("at most" in f for f in evaluate_case(good_evidence(), Expect(outcome="ANSWERED_FROM_DATA", max_rows=1)))


def test_a_missing_answer_phrase_is_caught() -> None:
    failures = evaluate_case(good_evidence(), Expect(outcome="ANSWERED_FROM_DATA", answer_contains=("unmistakable",)))
    assert any("answer: missing" in failure for failure in failures)


def test_a_missing_quality_block_is_caught() -> None:
    failures = evaluate_case(good_evidence(quality=None), Expect(outcome="ANSWERED_FROM_DATA", quality=True))
    assert any("no quality state" in failure for failure in failures)


def test_a_missing_product_version_is_caught() -> None:
    failures = evaluate_case(
        good_evidence(products=["customer_360"]),
        Expect(outcome="ANSWERED_FROM_DATA", cites_product=True),
    )
    assert any("no product version" in failure for failure in failures)


def test_provenance_is_required_of_every_answer() -> None:
    assert any("trace id" in f for f in evaluate_case(good_evidence(trace_id=""), Expect(outcome="ANSWERED_FROM_DATA")))
    assert any(
        "contract digest" in f
        for f in evaluate_case(good_evidence(contract_digest=""), Expect(outcome="ANSWERED_FROM_DATA"))
    )


def test_an_incomplete_answer_is_caught_even_when_nothing_else_is_asserted() -> None:
    empty = Evidence(question="q", answer="", trace_id="t", contract_digest="d")
    failures = evaluate_case(empty, Expect(outcome="INCOMPLETE"))
    assert any("incomplete" in failure for failure in failures)


def test_an_uncitable_refusal_is_caught() -> None:
    """\"I can't do that\" without a contract field is an opinion."""
    uncitable = good_evidence(refused=True, governance={"allowed": False, "reason": ""})
    failures = evaluate_case(uncitable, Expect(outcome="REFUSED", cites_product=False))
    assert any("no reason" in failure for failure in failures)


def test_a_refusal_without_any_recorded_decision_is_caught() -> None:
    silent = good_evidence(refused=True, governance=None, quality=None, lineage=None)
    failures = evaluate_case(silent, Expect(outcome="REFUSED", cites_product=False))
    assert any("no governance decision" in failure for failure in failures)


def test_a_citable_refusal_passes() -> None:
    citable = good_evidence(
        refused=True,
        sql=None,
        row_count=None,
        quality=None,
        governance={"allowed": False, "reason": "prohibited (field not_allowed_use)"},
    )
    assert evaluate_case(citable, Expect(outcome="REFUSED", cites_product=False)) == []


def test_a_missing_product_is_not_demanded_of_an_unanswerable_question() -> None:
    """Provenance for an answer that does not exist is not a thing to ask for."""
    no_metric = good_evidence(
        missing_metrics=["weather"],
        sql=None,
        row_count=None,
        metrics=[],
        products=[],
        quality=None,
    )
    assert evaluate_case(no_metric, Expect(outcome="NO_GOVERNED_METRIC", cites_product=True)) == []


# ---------------------------------------------------------------------------
# Scoring and the gate
# ---------------------------------------------------------------------------
def make_result(case: Case, passed: bool) -> CaseResult:
    return CaseResult(case=case, evidence=None, failures=[] if passed else ["boom"])


def test_score_is_weighted_by_case_weight() -> None:
    heavy = Case(id="heavy", question="q", expect=Expect(outcome="X"), weight=3.0, must_pass=True)
    light = Case(id="light", question="q", expect=Expect(outcome="X"), weight=1.0)
    results = [make_result(heavy, True), make_result(light, False)]
    assert score(results) == pytest.approx(0.75)


def test_a_critical_failure_fails_the_run_even_at_a_high_score() -> None:
    """The property the gate exists for: compliance is not an average."""
    cases = [Case(id=f"filler{i}", question="q", expect=Expect(outcome="X"), weight=1.0) for i in range(99)]
    critical = Case(
        id="critical", question="q", expect=Expect(outcome="X"), weight=1.0, tags=("governance",), must_pass=True
    )
    results = [make_result(case, True) for case in cases] + [make_result(critical, False)]
    assert score(results) > 0.98
    ok, reasons = gate(results, fail_under=0.95)
    assert not ok
    assert any("critical" in reason for reason in reasons)


def test_a_tagged_policy_case_counts_as_critical_even_without_the_flag() -> None:
    tagged = Case(id="t", question="q", expect=Expect(outcome="X"), tags=("security",))
    assert make_result(tagged, False).critical


def test_the_gate_passes_a_clean_run() -> None:
    results = [make_result(case, True) for case in SUITE]
    ok, reasons = gate(results, fail_under=1.0)
    assert ok and reasons == []


def test_the_gate_reports_a_low_score() -> None:
    cases = [Case(id=f"c{i}", question="q", expect=Expect(outcome="X")) for i in range(4)]
    results = [make_result(case, index > 1) for index, case in enumerate(cases)]
    ok, reasons = gate(results, fail_under=0.9)
    assert not ok
    assert any("below the required" in reason for reason in reasons)


def test_summarise_counts_failures() -> None:
    results = [make_result(case, case.id != "clarify.ambiguous_term") for case in SUITE]
    summary = summarise(results)
    assert summary["cases"] == len(SUITE)
    assert summary["failed"] == 1
    assert summary["failed_ids"] == ["clarify.ambiguous_term"]


def test_the_report_writes_json_and_markdown(tmp_path: Path) -> None:
    from banking_data_agents.config import get_settings, reset_settings_cache

    results = [make_result(case, False) for case in SUITE[:2]]
    path = write_report(results, suite="unit", directory=tmp_path)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["suite"] == "unit"
    assert payload["summary"]["cases"] == 2
    markdown = (tmp_path / "eval-report-unit.md").read_text(encoding="utf-8")
    assert "Evaluation report" in markdown
    assert "Failures in detail" in markdown
    _ = get_settings(), reset_settings_cache()


# ---------------------------------------------------------------------------
# End to end: the whole suite must pass
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_the_deterministic_suite_passes_completely(agent_env) -> None:
    """The headline claim, asserted rather than asserted-about.

    Every case, every assertion, no critical failures. If this test is removed the
    README's central claim becomes marketing.
    """
    _ = agent_env
    results = run_suite(SUITE)
    for result in results:
        assert result.passed, f"{result.case.id}: {'; '.join(result.failures) or result.error}"
    assert summarise(results)["critical_failed"] == 0
    assert score(results) == 1.0
