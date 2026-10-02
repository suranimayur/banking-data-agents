"""Agents and the evidence envelope.

Two halves. The first builds envelopes from hand-written tool histories, so the
provenance rules are tested without a model or a lake — a refusal must carry the
contract that caused it, a clarification must not carry SQL it never ran, and a
product must be cited with its version. The second runs the real agents end to end
over the shared lake, because the thing most likely to break is not a rule but the
wiring between them.
"""

from __future__ import annotations

import json

import pytest

from banking_data_agents.agents import AGENTS, Copilot, CreditAgent, Evidence, FraudAgent, get_agent
from banking_data_agents.agents.evidence import build_evidence
from banking_data_agents.agents.registry import AGENTS as REGISTERED
from banking_data_agents.llm.stub import PIPELINE

CATALOG = {"products": [{"product": "customer_360", "match_score": 1.0}]}
CONTRACT = {"found": True, "product": "customer_360", "version": "2.3.0"}
SQL = 'SELECT "region", COUNT(*) AS customer_count FROM gold.customer_360 GROUP BY "region" LIMIT 100'
GENERATED = {
    "sql": SQL,
    "products_used": ["customer_360"],
    "metrics_used": ["metric.customer_count@1.0.0"],
    "notes": ["a note"],
    "plan": {
        "metrics": [
            {"ref": "metric.customer_count@1.0.0", "name": "customer_count", "definition": "Count of customers."}
        ]
    },
}
EXECUTED = {
    "executed": True,
    "row_count": 10,
    "columns": ["region", "customer_count"],
    "preview": [["Maharashtra", 372]],
    "truncated": False,
    "engine": "duckdb",
    "execution_id": "abc123",
    "tables": ["gold.customer_360"],
    "sql": SQL,
}


def envelope(**outputs: object) -> Evidence:
    """Build an envelope from named tool outputs, as if a conversation produced them."""
    mappings = {name: [value] for name, value in outputs.items() if value is not None}
    return build_evidence(
        question="How many customers do we have by region?",
        answer="10 row(s) returned.",
        trace_id="trace-1",
        trace=[{"tool": name} for name in mappings],
        outputs=mappings,
        contract_digest="deadbeefcafe",
        contract_versions={
            "customer_360": "customer_360@2.3.0",
            "transaction": "transaction@1.6.0",
            "credit_risk": "credit_risk@2.1.0",
        },
        provider="stub",
        model_id="stub:scripted",
        asof="2026-09-30",
    )


# ---------------------------------------------------------------------------
# The envelope, built by hand
# ---------------------------------------------------------------------------
def test_a_data_answer_carries_its_full_provenance() -> None:
    ev = envelope(search_catalog=CATALOG, generate_sql=GENERATED, execute_sql=EXECUTED)
    assert ev.answered_from_data
    assert ev.sql == SQL
    assert ev.metrics == ["metric.customer_count@1.0.0"]
    assert ev.products == ["customer_360@2.3.0"]
    assert ev.tables == ["gold.customer_360"]
    assert ev.row_count == 10
    assert ev.engine == "duckdb"
    assert ev.execution_id == "abc123"
    assert ev.contract_digest == "deadbeefcafe"
    assert ev.tools_called == ["search_catalog", "generate_sql", "execute_sql"]
    assert ev.is_complete


def test_metric_definitions_are_lifted_from_the_plan_for_reviewers() -> None:
    ev = envelope(generate_sql=GENERATED)
    assert ev.metric_definitions["metric.customer_count@1.0.0"] == "Count of customers."


def test_a_refusal_does_not_carry_the_sql_it_declined_to_run() -> None:
    governance = {
        "allowed": False,
        "reason": "'marketing_targeting' is prohibited (contract credit_risk@2.1.0)",
        "product": "credit_risk",
        "contract_version": "2.1.0",
    }
    ev = envelope(search_catalog=CATALOG, check_allowed_use=governance, generate_sql=GENERATED)
    assert ev.refused is True
    assert ev.sql is None
    assert ev.governance == governance
    assert ev._outcome_label() == "REFUSED (not permitted by contract)"
    assert ev.is_complete


def test_a_clarification_is_a_complete_outcome_not_a_failure() -> None:
    generated = {
        "sql": None,
        "needs_clarification": {"kind": "ambiguous_definition", "question": "Which do you mean?", "options": []},
    }
    ev = envelope(search_catalog=CATALOG, generate_sql=generated)
    assert ev.needs_clarification is not None
    assert ev.sql is None
    assert not ev.answered_from_data
    assert ev.is_complete
    assert "NEEDS CLARIFICATION" in ev._outcome_label()


def test_a_missing_metric_is_reported_as_such() -> None:
    ev = envelope(search_catalog=CATALOG, generate_sql={"sql": None, "needs_metrics": ["weather"]})
    assert ev.missing_metrics == ["weather"]
    assert ev.is_complete
    assert "NO GOVERNED METRIC" in ev._outcome_label()


def test_a_metadata_answer_is_complete_without_any_rows() -> None:
    quality = {"product": "customer_360", "total": 9, "passed": 9, "sla": {"age_hours": 0.0}}
    ev = envelope(search_catalog=CATALOG, explain_quality=quality)
    assert not ev.answered_from_data
    assert ev.is_complete
    assert ev._outcome_label() == "ANSWERED from metadata"
    assert "9/9 rules passing" in ev.render()


def test_an_empty_envelope_is_reported_as_incomplete() -> None:
    ev = envelope()
    assert not ev.is_complete
    assert ev._outcome_label() == "INCOMPLETE"


def test_guardrail_violations_become_envelope_warnings() -> None:
    validated = {"valid": False, "violations": [{"code": "SELECT_STAR", "message": "SELECT * is not allowed"}]}
    ev = envelope(generate_sql=GENERATED, validate_sql=validated)
    assert any("SELECT_STAR" in warning for warning in ev.warnings)


def test_planner_notes_become_envelope_warnings() -> None:
    ev = envelope(generate_sql=GENERATED)
    assert ev.warnings == ["a note"]


def test_products_fall_back_to_the_best_search_match_not_the_whole_catalogue() -> None:
    """Listing every published product is provenance theatre."""
    ev = envelope(search_catalog=CATALOG)
    assert ev.products == ["customer_360@2.3.0"]


def test_a_quality_answer_names_the_product_it_is_about() -> None:
    ev = envelope(explain_quality={"product": "credit_risk", "total": 5, "passed": 5})
    assert ev.products == ["credit_risk@2.1.0"]


def test_the_envelope_serialises_and_renders() -> None:
    ev = envelope(search_catalog=CATALOG, generate_sql=GENERATED, execute_sql=EXECUTED)
    payload = json.loads(ev.to_json())
    assert payload["answered_from_data"] is True
    assert payload["metrics"] == ["metric.customer_count@1.0.0"]
    rendered = ev.render()
    assert "evidence" in rendered
    assert "metric.customer_count@1.0.0" in rendered
    assert "customer_360@2.3.0" in rendered
    assert SQL.rstrip(";") in rendered


def test_the_sql_can_be_hidden_for_an_audience_that_does_not_want_it() -> None:
    ev = envelope(search_catalog=CATALOG, generate_sql=GENERATED, execute_sql=EXECUTED)
    assert SQL[:30] not in ev.render(show_sql=False)


# ---------------------------------------------------------------------------
# The agents, end to end
# ---------------------------------------------------------------------------
@pytest.fixture
def copilot(agent_env) -> Copilot:
    _ = agent_env
    return Copilot()


def test_the_registry_exposes_the_three_documented_agents() -> None:
    assert set(AGENTS) == {"copilot", "fraud", "credit"}
    assert AGENTS is REGISTERED
    assert isinstance(get_agent("copilot"), Copilot)
    assert isinstance(get_agent("fraud"), FraudAgent)
    assert isinstance(get_agent("credit"), CreditAgent)


def test_an_unknown_agent_name_is_an_error_not_a_default() -> None:
    with pytest.raises(KeyError, match="unknown agent"):
        get_agent("wizard")


def test_the_fraud_agent_cannot_resolve_a_fuzzy_term() -> None:
    """Its surface is narrowed on purpose: a vague term must become a question."""
    assert "resolve_metric" not in FraudAgent().tool_names
    assert "generate_sql" in FraudAgent().tool_names


def test_the_fraud_agent_has_no_tool_that_reaches_the_labels() -> None:
    """The labels are blocked by the guardrail; the tool surface says so too."""
    fraud = FraudAgent()
    assert "read_holdout" not in fraud.tool_names
    assert set(fraud.tool_names).issubset(set(Copilot().tool_names))


def test_every_agent_returns_a_complete_envelope_for_a_reliable_question(copilot: Copilot) -> None:
    ev = copilot.ask("Is customer_360 data reliable?")
    assert ev.is_complete
    assert not ev.refused
    assert ev.trace_id
    assert ev.provider == "stub"
    assert ev.model_id == "stub:scripted"
    assert "explain_quality" in ev.tools_called


def test_the_copilot_answers_a_question_from_governed_data(copilot: Copilot) -> None:
    ev = copilot.ask("How many customers do we have by region?")
    assert ev.answered_from_data
    assert ev.row_count and ev.row_count > 0
    assert ev.sql and "COUNT(*)" in ev.sql
    assert ev.tables == ["gold.customer_360"]
    # Products are cited with the version the answer was built from.
    assert ev.products == ["customer_360@2.3.0"]
    assert "customer_360@2.3.0" in ev.render()
    # And the quality state of that product travels with the answer by default.
    assert ev.quality is not None
    assert ev.quality["total"] > 0


def test_the_copilot_follows_the_documented_tool_order(copilot: Copilot) -> None:
    ev = copilot.ask("How many customers do we have by region?")
    positions = [PIPELINE.index(name) for name in ev.tools_called if name in PIPELINE]
    assert positions == sorted(positions), ev.tools_called
    assert ev.tools_called[0] == "search_catalog"
    assert "execute_sql" in ev.tools_called


def test_the_copilot_refuses_a_prohibited_use_and_says_why(copilot: Copilot) -> None:
    ev = copilot.ask("How can I target customers for a marketing campaign on their credit score?")
    assert ev.refused
    assert ev.sql is None
    assert not ev.answered_from_data
    assert "not_allowed_use" in ev.answer
    assert ev.governance is not None and ev.governance["allowed"] is False


def test_the_copilot_asks_instead_of_guessing(copilot: Copilot) -> None:
    ev = copilot.ask("What is our exposure?")
    assert ev.needs_clarification is not None
    assert ev.sql is None
    assert "Which do you mean" in ev.answer


def test_the_copilot_explains_lineage(copilot: Copilot) -> None:
    ev = copilot.ask("Where does customer_360 come from?")
    assert ev.lineage is not None
    assert ev.lineage["edge_count"] > 0
    assert "trace_lineage" in ev.tools_called


def test_the_copilot_reports_an_ungoverned_measure_honestly(copilot: Copilot) -> None:
    ev = copilot.ask("What is the weather in Mumbai?")
    assert ev.missing_metrics
    assert "weather" in ev.answer
    assert ev.sql is None


def test_a_conversation_resets_between_questions(copilot: Copilot) -> None:
    copilot.ask("How many customers do we have by region?")
    assert copilot.messages
    copilot.reset()
    assert copilot.messages == []
    assert copilot.ctx.trace == []
    # And a follow-up question still works from a clean slate.
    ev = copilot.ask("What is the total balance by region?")
    assert ev.answered_from_data


def test_a_second_question_is_not_answered_from_the_first_ones_results(copilot: Copilot) -> None:
    first = copilot.ask("How many customers do we have by region?")
    second = copilot.ask("Where does customer_360 come from?")
    assert first.sql != second.sql
    assert second.lineage is not None
    assert "trace_lineage" in second.tools_called
    assert "execute_sql" not in second.tools_called


def test_the_agent_stays_inside_its_turn_budget(agent_env) -> None:
    """A runaway loop is a cost incident; the budget is enforced, not advisory."""
    _ = agent_env
    copilot = Copilot()
    ev = copilot.ask("Who spends the most on cards?")
    assert ev.answered_from_data
    assert len(ev.tools_called) <= copilot.settings.agent_max_tool_calls
    assert ev.stop_reason != "limit_turns"


def test_the_fraud_agent_answers_from_behavioural_signals(agent_env) -> None:
    _ = agent_env
    ev = FraudAgent().ask("How many transactions are there by channel group?")
    assert ev.is_complete
    # No labelled outcome was read, and nothing named a customer.
    assert "ops.fraud_ground_truth" not in ev.render()
    assert all(not table.startswith("ops.") for table in ev.tables)


def test_the_credit_agent_reports_the_basis_it_used(agent_env) -> None:
    _ = agent_env
    ev = CreditAgent().ask("What is the total outstanding by risk band?")
    assert ev.answered_from_data
    assert "total_outstanding" in " ".join(ev.metrics)
    assert ev.products and ev.products[0].startswith("credit_risk@")


def test_every_answer_records_the_trace_id_it_was_given(agent_env) -> None:
    _ = agent_env
    copilot = Copilot()
    ev = copilot.ask("How many customers do we have?")
    assert ev.trace_id
    assert all(entry["trace_id"] == ev.trace_id for entry in copilot.ctx.trace)
