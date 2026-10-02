"""The tool layer, tested without a model.

The tools are the platform's real interface, so they carry most of the risk and
most of the value. Testing them directly — rather than only through an agent —
means a failure here is unambiguously a failure of the tool, not of a model's
choice of tool.

Two themes run through the file. First, provenance: every tool must return
JSON-safe data with the versions attached, because a non-serialisable payload is
silently stringified by the runtime and the model then reasons over a Python
repr. Second, refusal: ``execute_sql`` re-validates, so no caller can route around
the guardrail by forgetting to ask.
"""

from __future__ import annotations

import json

import pytest

from banking_data_agents.tools import impl
from banking_data_agents.tools.context import ToolContext
from banking_data_agents.tools.registry import READ_ONLY_TOOLS, TOOL_NAMES, build_tools
from banking_data_agents.tools.sqlguard import FORBIDDEN_DATASETS


# ---------------------------------------------------------------------------
# Serialisation: the failure mode that looks like success
# ---------------------------------------------------------------------------
def test_jsonable_recurses_through_dicts_lists_and_dates() -> None:
    from datetime import date, datetime
    from decimal import Decimal

    payload = {
        "when": datetime(2026, 9, 30, 12, 0),
        "day": date(2026, 9, 30),
        "amount": Decimal("12.34"),
        "rows": [{"nested": date(2026, 1, 1)}],
        "tuple": (1, 2),
    }
    cleaned = impl.jsonable(payload)
    json.dumps(cleaned)  # must not raise
    assert cleaned["when"] == "2026-09-30T12:00:00"
    assert cleaned["amount"] == 12.34
    assert cleaned["rows"][0]["nested"] == "2026-01-01"


def test_every_tool_result_is_json_serialisable(tool_context: ToolContext) -> None:
    """A tool that returns a ``datetime`` breaks the model's view of the result.

    Strands falls back to ``str()`` when a result cannot be encoded, which hands
    the model a single-quoted Python repr that no JSON parser accepts. The scope
    of the damage is why this is asserted across *every* tool rather than on the
    one that happened to break: ``explain_quality`` leaked an SLA timestamp and the
    answer came back reading "0/0 rules passing".
    """
    ctx = tool_context
    results = {
        "search_catalog": impl.search_catalog(ctx, "customer balance by region"),
        "list_products": impl.list_products(ctx),
        "get_contract": impl.get_contract(ctx, "customer_360"),
        "resolve_metric": impl.resolve_metric_tool(ctx, "balance"),
        "check_allowed_use": impl.check_allowed_use(ctx, "credit_risk", "credit_score", "marketing_targeting"),
        "generate_sql": impl.generate_sql(ctx, "How many customers by region?"),
        "validate_sql": impl.validate_sql_tool(ctx, "SELECT customer_id FROM gold.customer_360 LIMIT 5"),
        "execute_sql": impl.execute_sql(ctx, "SELECT customer_id FROM gold.customer_360 LIMIT 5"),
        "explain_quality": impl.explain_quality(ctx, "customer_360"),
        "trace_lineage": impl.trace_lineage(ctx, "customer_360"),
    }
    assert set(results) == set(TOOL_NAMES)
    for name, result in results.items():
        try:
            json.dumps(result)
        except TypeError as error:  # pragma: no cover - the assertion is the point
            raise AssertionError(f"{name} returned a non-JSON-serialisable result: {error}") from error


def test_quality_payload_carries_a_serialisable_sla_row(tool_context: ToolContext) -> None:
    payload = impl.explain_quality(tool_context, "customer_360")
    assert payload["total"] > 0
    assert payload["sla"] is not None
    json.dumps(payload["sla"])
    assert payload["sla"]["product"] == "customer_360"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_search_catalog_ranks_the_relevant_product_first(tool_context: ToolContext) -> None:
    result = impl.search_catalog(tool_context, "How many customers do we have by region?")
    assert result["products"]
    top = result["products"][0]
    assert top["product"] == "customer_360"
    assert top["match_score"] > 0
    assert top["row_count"] > 0
    assert top["status"] in {"OK", "STALE", "DEGRADED", "UNKNOWN"}
    # The digest is what lets a later reviewer prove which contracts answered.
    assert len(result["contract_digest"]) >= 8


def test_search_catalog_finds_a_product_from_its_metric_names(tool_context: ToolContext) -> None:
    """A question in business words must reach the product that holds the metric."""
    result = impl.search_catalog(tool_context, "who spends the most on cards")
    assert result["products"][0]["product"] == "customer_360"


def test_list_products_reports_every_contract_with_live_counts(tool_context: ToolContext) -> None:
    result = impl.list_products(tool_context)
    assert result["count"] == 3
    names = {product["product"] for product in result["products"]}
    assert names == {"customer_360", "transaction", "credit_risk"}
    for product in result["products"]:
        assert product["version"]
        assert product["owner"]
        assert product["row_count"] > 0
        assert product["metrics"] > 0


def test_get_contract_returns_columns_keys_and_use_rules(tool_context: ToolContext) -> None:
    contract = impl.get_contract(tool_context, "customer_360")
    assert contract["found"] is True
    assert contract["version"] == "2.3.0"
    assert contract["primary_key"] == ["customer_id"]
    assert contract["sla"]["freshness_hours"] > 0
    assert contract["allowed_use"]
    names = {column["name"] for column in contract["columns"]}
    assert {"customer_id", "total_balance", "region"}.issubset(names)
    assert all("type" in column and "pii" in column for column in contract["columns"])


def test_get_contract_tolerates_a_qualified_or_punctuated_name(tool_context: ToolContext) -> None:
    assert impl.get_contract(tool_context, "gold.customer_360")["found"] is True
    assert impl.get_contract(tool_context, "customer-360")["found"] is True


def test_get_contract_reports_an_unknown_product_instead_of_raising(tool_context: ToolContext) -> None:
    result = impl.get_contract(tool_context, "nope")
    assert result["found"] is False
    assert "unknown data product" in result["error"]
    json.dumps(result)


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------
def test_a_prohibited_use_is_refused_with_a_citable_reason(tool_context: ToolContext) -> None:
    decision = impl.check_allowed_use(tool_context, "credit_risk", "credit_score", "marketing_targeting")
    assert decision["allowed"] is False
    # The reason must name the contract and the field, so a reviewer can go look.
    assert "credit_risk@2.1.0" in decision["reason"]
    assert "not_allowed_use" in decision["reason"]


def test_a_narrower_prohibited_name_is_still_refused(tool_context: ToolContext) -> None:
    """Containment, not equality: `marketing_targeting_on_credit_score` is a kind of
    `marketing_targeting`. An exact-match check left this hole open.
    """
    decision = impl.check_allowed_use(
        tool_context, "credit_risk", "credit_score", "marketing_targeting_on_credit_score"
    )
    assert decision["allowed"] is False


def test_a_permitted_use_is_allowed(tool_context: ToolContext) -> None:
    decision = impl.check_allowed_use(tool_context, "credit_risk", "credit_score", "credit_risk_management")
    assert decision["allowed"] is True


# ---------------------------------------------------------------------------
# Generation, validation, execution
# ---------------------------------------------------------------------------
def test_generate_sql_returns_governed_sql_with_its_metric_versions(tool_context: ToolContext) -> None:
    result = impl.generate_sql(tool_context, "How many customers do we have by region?")
    assert result["sql"] is not None
    assert result["metrics_used"] == ["metric.customer_count@1.0.0"]
    assert result["products_used"] == ["customer_360"]
    assert "COUNT(*)" in result["sql"]
    assert result["plan"]["dimensions"] == ["region"]


def test_generate_sql_asks_instead_of_guessing(tool_context: ToolContext) -> None:
    result = impl.generate_sql(tool_context, "What is our exposure?")
    assert result["sql"] is None
    assert result["needs_clarification"]["kind"] == "ambiguous_definition"
    assert len(result["needs_clarification"]["options"]) >= 2


def test_generate_sql_reports_a_missing_metric(tool_context: ToolContext) -> None:
    result = impl.generate_sql(tool_context, "What is the weather in Mumbai?")
    assert result["sql"] is None
    assert "weather" in result["needs_metrics"]


def test_generate_sql_accepts_product_hints_as_a_string_or_a_list(tool_context: ToolContext) -> None:
    question = "How many transactions are there?"
    as_string = impl.generate_sql(tool_context, question, "transaction")
    as_list = impl.generate_sql(tool_context, question, ["transaction"])
    assert as_string["sql"] == as_list["sql"]


def test_execute_sql_runs_a_governed_query_and_bounds_the_preview(tool_context: ToolContext) -> None:
    result = impl.execute_sql(tool_context, "SELECT customer_id FROM gold.customer_360 LIMIT 500", max_rows=10)
    assert result["executed"] is True
    assert result["row_count"] > 0
    # The model gets a bounded preview, never the whole result set.
    assert result["returned"] == 10
    assert result["truncated"] is True
    assert result["columns"] == ["customer_id"]
    assert result["execution_id"]
    assert result["engine"]
    assert result["elapsed_ms"] >= 0
    json.dumps(result)


def test_execute_sql_asks_for_no_more_than_the_platform_ceiling(tool_context: ToolContext) -> None:
    result = impl.execute_sql(tool_context, "SELECT customer_id FROM gold.customer_360 LIMIT 900", max_rows=100_000)
    assert result["returned"] <= 1_000


def test_execute_sql_refuses_sql_that_fails_validation(tool_context: ToolContext) -> None:
    """Re-validation at the point of execution, not trust in the caller."""
    result = impl.execute_sql(tool_context, "SELECT * FROM gold.customer_360 LIMIT 10")
    assert result["executed"] is False
    assert "validation" in result
    assert result["validation"]["valid"] is False


@pytest.mark.parametrize("dataset", sorted(FORBIDDEN_DATASETS))
def test_execute_sql_cannot_read_the_holdout(tool_context: ToolContext, dataset: str) -> None:
    result = impl.execute_sql(tool_context, f"SELECT transaction_id FROM {dataset} LIMIT 10")
    assert result["executed"] is False


def test_execute_sql_reports_a_query_error_instead_of_raising(tool_context: ToolContext) -> None:
    result = impl.execute_sql(tool_context, 'SELECT "no_such_column" FROM gold.customer_360 LIMIT 5')
    assert result["executed"] is False
    assert "no_such_column" in result["error"]


def test_validate_sql_tool_matches_the_guardrail(tool_context: ToolContext) -> None:
    ok = impl.validate_sql_tool(tool_context, "SELECT customer_id FROM gold.customer_360 LIMIT 5")
    assert ok["valid"] is True
    bad = impl.validate_sql_tool(tool_context, "DELETE FROM gold.customer_360")
    assert bad["valid"] is False
    assert bad["errors"]


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------
def test_explain_quality_reports_rule_counts_and_outstanding_findings(tool_context: ToolContext) -> None:
    payload = impl.explain_quality(tool_context, "customer_360")
    assert payload["total"] > 0
    assert payload["critical_failures"] == 0
    assert payload["contract"]["version"] == "2.3.0"


def test_explain_quality_covers_the_whole_platform_when_no_product_is_named(tool_context: ToolContext) -> None:
    payload = impl.explain_quality(tool_context, "")
    assert payload["product"] == "all products"
    assert payload["total"] > payload["passed"] - 1


def test_trace_lineage_maps_the_route_with_depth_and_transform(tool_context: ToolContext) -> None:
    """The shape the model reads: ``depth`` and ``via``, not the raw column names."""
    lineage = impl.trace_lineage(tool_context, "customer_360", "upstream")
    assert lineage["edge_count"] > 0
    assert lineage["source_systems"]
    for edge in lineage["edges"]:
        assert edge["depth"] is not None
        assert edge["dataset"]
        assert edge["via"]
        assert "level" not in edge and "transform_id" not in edge


def test_trace_lineage_reports_downstream_consumers(tool_context: ToolContext) -> None:
    lineage = impl.trace_lineage(tool_context, "transaction", "downstream")
    assert lineage["direction"] == "downstream"
    # Downstream sources are meaningless, so they are not reported.
    assert lineage["source_systems"] == []


def test_trace_lineage_defaults_to_upstream_on_a_nonsense_direction(tool_context: ToolContext) -> None:
    assert impl.trace_lineage(tool_context, "customer_360", "sideways")["direction"] == "upstream"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
def test_the_tool_surface_is_exactly_the_documented_one(tool_context: ToolContext) -> None:
    tools = build_tools(tool_context)
    assert [tool.tool_name for tool in tools] == list(TOOL_NAMES)
    assert list(READ_ONLY_TOOLS) == list(TOOL_NAMES)


def test_a_specialist_can_be_given_a_narrower_surface(tool_context: ToolContext) -> None:
    tools = build_tools(tool_context, ["search_catalog", "generate_sql"])
    assert [tool.tool_name for tool in tools] == ["search_catalog", "generate_sql"]


def test_asking_for_an_unknown_tool_is_an_error_not_a_silent_drop() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        build_tools(ToolContext(), ["search_catalog", "write_sql"])


def test_the_trace_records_every_call_with_the_trace_id(tool_context: ToolContext) -> None:
    """The trace is the audit log, and the envelope reads full results from it."""
    ctx = tool_context
    ctx.trace_id = "trace-abc"
    ctx.record("generate_sql", {"question": "x"}, {"sql": "SELECT 1", "notes": ["a", "b"]})
    assert [entry["tool"] for entry in ctx.trace] == ["generate_sql"]
    assert ctx.trace[0]["trace_id"] == "trace-abc"
    assert ctx.trace[0]["arguments"] == {"question": "x"}
    # The log keeps a summary; the envelope can still see the whole result.
    assert ctx.trace[0]["result_summary"]["notes"] == ["a", "b"]
    assert ctx.last("generate_sql")["sql"] == "SELECT 1"


def test_a_long_list_is_summarised_in_the_trace_but_kept_for_the_envelope(tool_context: ToolContext) -> None:
    ctx = tool_context
    ctx.record("execute_sql", {}, {"preview": [[index] for index in range(50)]})
    assert ctx.trace[0]["result_summary"]["preview"] == "<50 item(s)>"
    assert len(ctx.last("execute_sql")["preview"]) == 50


def test_clearing_the_context_forgets_the_conversation(tool_context: ToolContext) -> None:
    ctx = tool_context
    ctx.record("search_catalog", {}, {"products": []})
    ctx.clear()
    assert ctx.trace == []
    assert ctx.outputs == {}
    assert ctx.last("search_catalog") is None
