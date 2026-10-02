"""Provider abstraction and the scripted policy.

The claim under test is that the *agent* code is provider-independent: the same
orchestration runs against a stub, an emulator and real Bedrock, so CI can assert
behaviour rather than hope. The second half of the file exercises the scripted
policy directly, because it is the reference implementation of the control flow —
retrieve, check governance, generate, validate, execute, and ask rather than guess.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from banking_data_agents.llm.factory import (
    describe_model,
    get_model,
    get_provider_name,
    reset_models,
)
from banking_data_agents.llm.stub import (
    PIPELINE,
    StubModel,
    _parse_result,
    default_policy,
)

ALL_TOOLS = {
    "search_catalog",
    "list_products",
    "get_contract",
    "resolve_metric",
    "check_allowed_use",
    "generate_sql",
    "validate_sql",
    "execute_sql",
    "explain_quality",
    "trace_lineage",
}


# ---------------------------------------------------------------------------
# Message construction helpers (the shapes Strands actually sends)
# ---------------------------------------------------------------------------
def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def call(name: str, arguments: dict[str, Any], tool_use_id: str = "t1") -> dict[str, Any]:
    return {"role": "assistant", "content": [{"toolUse": {"toolUseId": tool_use_id, "name": name, "input": arguments}}]}


def result(tool_use_id: str, payload: Any) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "status": "success",
                    "content": [{"text": json.dumps(payload)}],
                }
            }
        ],
    }


CATALOG_HIT = {"products": [{"product": "customer_360", "match_score": 1.0}]}
SQL_OK = {
    "sql": 'SELECT "region", COUNT(*) AS customer_count FROM gold.customer_360 GROUP BY "region" LIMIT 100',
    "metrics_used": ["metric.customer_count@1.0.0"],
    "products_used": ["customer_360"],
    "plan": {"metrics": []},
}


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------
def test_tests_run_against_the_stub() -> None:
    """If this fails, the suite is about to cost money and lose determinism."""
    assert get_provider_name() == "stub"
    assert isinstance(get_model("reasoner"), StubModel)
    assert "scripted" in describe_model()


def test_the_model_is_cached_per_process_then_released() -> None:
    first = get_model("reasoner")
    assert get_model("reasoner") is first
    reset_models()
    assert get_model("reasoner") is not first


def test_the_stub_reports_a_plausible_config() -> None:
    config = StubModel().get_config()
    assert config["model_id"] == "stub:scripted"
    # Temperature zero: the same question must not yield two different numbers.
    assert config["temperature"] == 0.0


def test_the_stub_config_can_be_updated() -> None:
    model = StubModel()
    model.update_config(max_tokens=99)
    assert model.get_config()["max_tokens"] == 99


def test_structured_output_is_not_silently_faked() -> None:
    with pytest.raises(NotImplementedError):
        next(StubModel().structured_output(dict, []))


def test_every_tier_resolves_to_the_stub_in_tests() -> None:
    for tier in ("router", "reasoner", "escalation"):
        assert isinstance(get_model(tier), StubModel)


# ---------------------------------------------------------------------------
# Tool result parsing
# ---------------------------------------------------------------------------
def test_a_json_tool_result_is_parsed() -> None:
    assert _parse_result('{"a": 1}') == {"a": 1}


def test_a_python_repr_tool_result_is_parsed_rather_than_treated_as_text() -> None:
    """A runtime that cannot JSON-encode a result hands over a repr.

    Treating that as opaque text means the model answers from nothing, silently.
    """
    assert _parse_result("{'a': 1, 'b': None}") == {"a": 1, "b": None}


def test_an_unparseable_result_is_kept_as_raw_text() -> None:
    parsed = _parse_result("not a value at all")
    assert parsed["_raw"] == "not a value at all"


# ---------------------------------------------------------------------------
# The scripted policy
# ---------------------------------------------------------------------------
def policy(question: str, *extra: dict[str, Any], tools: set[str] | None = None, tool_limit: int = 1):
    """Run the policy after a catalog hit, with optional extra messages."""
    messages = [user(question), call("search_catalog", {"query": question}, "s0"), result("s0", CATALOG_HIT)]
    messages.extend(extra)
    return default_policy(messages, tools or ALL_TOOLS)


def test_the_first_move_is_always_a_catalog_search() -> None:
    turn = default_policy([user("How many customers do we have?")], ALL_TOOLS)
    assert turn.tool == "search_catalog"


def test_the_reference_pipeline_is_ordered_as_documented() -> None:
    assert PIPELINE == ("search_catalog", "check_allowed_use", "generate_sql", "validate_sql", "execute_sql")


def test_the_second_move_is_generation() -> None:
    assert policy("How many customers do we have by region?").tool == "generate_sql"


def test_a_governance_sensitive_question_triggers_the_check_first() -> None:
    turn = policy("Which customers can I target for a marketing campaign on their credit score?")
    assert turn.tool == "check_allowed_use"
    assert turn.arguments["intended_use"] == "marketing_targeting_on_credit_score"


def test_a_refusal_is_a_complete_answer() -> None:
    refusal = {"allowed": False, "reason": "'marketing_targeting' is prohibited (contract credit_risk@2.1.0)."}
    turn = policy(
        "Which customers can I target for a marketing campaign on their credit score?",
        call("check_allowed_use", {}, "g1"),
        result("g1", refusal),
    )
    assert turn.tool is None
    assert "I can't run that query" in turn.text
    assert "credit_risk@2.1.0" in turn.text


def test_a_permitted_governance_question_proceeds_to_generation() -> None:
    turn = policy(
        "Screen these customers for a marketing offer",
        call("check_allowed_use", {}, "g1"),
        result("g1", {"allowed": True, "reason": "permitted"}),
    )
    assert turn.tool == "generate_sql"


def test_an_ambiguous_term_becomes_a_question() -> None:
    generated = {
        "sql": None,
        "needs_clarification": {
            "kind": "ambiguous_definition",
            "question": "Which do you mean: exposure?",
            "options": [{"name": "total_outstanding", "product": "credit_risk", "definition": "outstanding principal"}],
        },
    }
    turn = policy("What is our exposure?", call("generate_sql", {}, "q1"), result("q1", generated))
    assert turn.tool is None
    assert "Which do you mean" in turn.text
    assert "total_outstanding" in turn.text
    assert "have not run the query yet" in turn.text


def test_a_missing_metric_is_reported_not_approximated() -> None:
    generated = {"sql": None, "needs_metrics": ["weather"]}
    turn = policy("What is the weather in Mumbai?", call("generate_sql", {}, "q1"), result("q1", generated))
    assert turn.tool is None
    assert "weather" in turn.text
    assert "governed metric" in turn.text


def test_generated_sql_is_validated_before_it_is_executed() -> None:
    turn = policy("How many customers by region?", call("generate_sql", {}, "q1"), result("q1", SQL_OK))
    assert turn.tool == "validate_sql"
    assert turn.arguments["sql"] == SQL_OK["sql"]


def test_invalid_sql_is_reported_and_not_executed() -> None:
    validated = {"valid": False, "violations": [{"code": "SELECT_STAR", "message": "SELECT * is not allowed"}]}
    turn = policy(
        "How many customers by region?",
        call("generate_sql", {}, "q1"),
        result("q1", SQL_OK),
        call("validate_sql", {}, "v1"),
        result("v1", validated),
    )
    assert turn.tool is None
    assert "SELECT_STAR" in turn.text
    assert "did not execute" in turn.text


def test_validated_sql_is_then_executed() -> None:
    turn = policy(
        "How many customers by region?",
        call("generate_sql", {}, "q1"),
        result("q1", SQL_OK),
        call("validate_sql", {}, "v1"),
        result("v1", {"valid": True}),
    )
    assert turn.tool == "execute_sql"


def test_the_final_answer_reports_rows_and_cites_its_sources() -> None:
    executed = {
        "executed": True,
        "row_count": 10,
        "columns": ["region", "customer_count"],
        "preview": [["Maharashtra", 372]],
        "truncated": False,
    }
    turn = policy(
        "How many customers by region?",
        call("generate_sql", {}, "q1"),
        result("q1", SQL_OK),
        call("validate_sql", {}, "v1"),
        result("v1", {"valid": True}),
        call("execute_sql", {}, "e1"),
        result("e1", executed),
    )
    assert turn.tool is None
    assert "10 row(s) returned" in turn.text
    assert "Maharashtra" in turn.text
    assert "metric.customer_count@1.0.0" in turn.text
    assert "customer_360" in turn.text


def test_a_trust_question_is_answered_from_metadata_only() -> None:
    """ "Is this reliable?" is not a request for a number."""
    quality = {"product": "customer_360", "total": 9, "passed": 9, "critical_failures": 0, "sla": None}
    turn = policy(
        "Is customer_360 data reliable?",
        call("explain_quality", {}, "q1"),
        result("q1", quality),
    )
    assert turn.tool is None
    assert "9/9 rules passing" in turn.text
    # And critically: no SQL was generated for it.
    assert "generate_sql" not in turn.text


def test_a_measure_question_that_also_asks_about_trust_gets_both() -> None:
    """ "Is the total balance data reliable?" asks for a number *and* its standing.

    The number is still the answer, so generation comes first; the trust signal is
    then reported alongside it rather than replacing it.
    """
    question = "Is the total balance data reliable?"
    assert policy(question).tool == "generate_sql"
    turn = policy(question, call("generate_sql", {}, "q1"), result("q1", SQL_OK))
    assert turn.tool == "explain_quality"


def test_a_lineage_question_is_answered_from_lineage() -> None:
    lineage = {
        "product": "customer_360",
        "direction": "upstream",
        "edge_count": 14,
        "source_systems": ["bronze.s_crm_customers"],
        "edges": [{"depth": 1, "dataset": "silver.accounts", "via": "gold_customer_360"}],
    }
    turn = policy(
        "Where does customer_360 come from?",
        call("trace_lineage", {}, "l1"),
        result("l1", lineage),
    )
    assert turn.tool is None
    assert "bronze.s_crm_customers" in turn.text
    assert "silver.accounts" in turn.text


def test_a_new_question_does_not_reuse_the_previous_turns_tools() -> None:
    """A scripted policy has no memory, so it must be shown only the current turn.

    Otherwise the second question in a conversation finds the first question's
    tool calls already \"done\" and answers from stale results.
    """
    messages = [
        user("How many customers by region?"),
        call("search_catalog", {}, "s0"),
        result("s0", CATALOG_HIT),
        call("generate_sql", {}, "q1"),
        result("q1", SQL_OK),
        user("Where does customer_360 come from?"),
    ]
    turn = default_policy(messages, ALL_TOOLS)
    assert turn.tool == "search_catalog"
    assert turn.arguments["query"] == "Where does customer_360 come from?"


def test_a_missing_tool_falls_back_to_text_instead_of_raising() -> None:
    """A misconfigured agent should surface as a bad answer in evals, not a crash."""
    from banking_data_agents.llm.stub import ScriptedTurn

    blind = StubModel(policy=lambda messages, available: ScriptedTurn(tool="search_catalog"))
    events = asyncio.run(
        _drain(blind.stream([user("How many customers do we have?")], tool_specs=[{"name": "explain_quality"}]))
    )
    text = _streamed_text(events)
    assert "no tool available" in text
    stops = [event["messageStop"]["stopReason"] for event in events if "messageStop" in event]
    assert stops == ["end_turn"]


def test_the_policy_adapts_to_a_narrowed_tool_surface() -> None:
    """A specialist with fewer tools still produces a complete metadata answer."""
    turn = policy("Is customer_360 data reliable?", tools={"search_catalog", "explain_quality"})
    assert turn.tool == "explain_quality"


def test_streamed_text_is_collected_from_delta_events() -> None:
    model = StubModel()
    events = asyncio.run(_drain(model.stream([user("Hi")], tool_specs=[])))
    assert isinstance(_streamed_text(events), str)


def test_a_tool_call_streams_the_expected_event_sequence() -> None:
    model = StubModel()
    events = asyncio.run(_drain(model.stream([user("How many customers?")], tool_specs=[{"name": "search_catalog"}])))
    kinds = [next(iter(event)) for event in events]
    assert kinds[0] == "messageStart"
    assert "contentBlockStart" in kinds
    assert "contentBlockDelta" in kinds
    assert "messageStop" in kinds
    assert kinds[-1] == "metadata"
    stops = [event["messageStop"]["stopReason"] for event in events if "messageStop" in event]
    assert stops == ["tool_use"]
    usage = next(event["metadata"]["usage"] for event in events if "metadata" in event)
    assert usage["inputTokens"] >= 1
    assert usage["totalTokens"] >= 2


def _streamed_text(events: list[dict[str, Any]]) -> str:
    """Reassemble the assistant text from a stream of Strands delta events."""
    return "".join(
        event["contentBlockDelta"]["delta"]["text"]
        for event in events
        if "contentBlockDelta" in event and "text" in event["contentBlockDelta"].get("delta", {})
    )


async def _drain(stream) -> list[dict[str, Any]]:
    return [event async for event in stream]
