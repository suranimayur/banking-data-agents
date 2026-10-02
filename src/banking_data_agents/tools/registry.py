"""The tool surface the model actually sees.

Descriptions are written for the model, not for a human reader: they say what the
tool returns and, more importantly, when *not* to use it. The two rules that
matter most are stated in the tool text because they are cheap to state and
expensive to get wrong — always validate before executing, and never guess when a
term is ambiguous.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from strands import tool

from banking_data_agents.logging_setup import get_logger
from banking_data_agents.tools import impl
from banking_data_agents.tools.context import ToolContext

logger = get_logger(__name__)

#: Names of every tool, in the order they normally appear in a good answer.
TOOL_NAMES: tuple[str, ...] = (
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
)

#: Tools that must never take part in a single tool call — a read-only agent has
#: no business writing SQL, and this list is asserted in the tests.
READ_ONLY_TOOLS: tuple[str, ...] = TOOL_NAMES


def build_tools(ctx: ToolContext, names: Sequence[str] | None = None) -> list[Any]:
    """Construct the tool list for one conversation, bound to ``ctx``.

    ``names`` narrows the surface to a subset. A specialist agent gets fewer
    tools on purpose: a smaller surface is a smaller attack surface, and it keeps
    a fraud investigator from wandering into credit questions.
    """

    def emit(tool: str, arguments: dict[str, Any], result: Any) -> dict[str, Any]:
        """Serialise, record and return one tool result.

        Every tool goes through here so the model is guaranteed to receive JSON.
        The Strands runtime falls back to ``str()`` when a result cannot be
        encoded, which quietly turns a dict into a Python repr the model cannot
        parse; cleaning at the boundary makes that impossible rather than
        unlikely.
        """
        cleaned = impl.jsonable(result)
        ctx.record(tool, arguments, cleaned)
        return cleaned

    @tool(
        name="search_catalog",
        description=(
            "Find published data products that could answer a question. Call this FIRST, always, "
            "before anything else: it establishes which governed products exist and what they "
            "contain. Returns product name, version, owner, grain, description, available metric "
            "count, live row count and operational status."
        ),
    )
    def search_catalog(query: str, limit: int = 3) -> dict[str, Any]:
        return emit("search_catalog", {"query": query, "limit": limit}, impl.search_catalog(ctx, query, limit))

    @tool(
        name="list_products",
        description=(
            "List the entire published catalogue with row counts and freshness. Use when the user "
            "asks what data exists, what is available, or what is stale."
        ),
    )
    def list_products() -> dict[str, Any]:
        return emit("list_products", {}, impl.list_products(ctx))

    @tool(
        name="get_contract",
        description=(
            "Fetch the full contract for one data product: every column with its type, unit, enum, "
            "metric reference and PII flag, plus the primary key, SLA and the permitted and "
            "prohibited uses. Use before writing SQL so column names and semantics are exact."
        ),
    )
    def get_contract(product: str) -> dict[str, Any]:
        return emit("get_contract", {"product": product}, impl.get_contract(ctx, product))

    @tool(
        name="resolve_metric",
        description=(
            "Map a business term ('high value', 'ticket size', 'exposure') onto governed metrics, "
            "with a stability score for each candidate. If several candidates score alike the term "
            "is AMBIGUOUS: report the options and ask the user, and never pick one silently."
        ),
    )
    def resolve_metric(term: str, product: str = "") -> dict[str, Any]:
        return emit("resolve_metric", {"term": term, "product": product}, impl.resolve_metric_tool(ctx, term, product))

    @tool(
        name="check_allowed_use",
        description=(
            "Ask the data contract whether a specific use of a specific column is permitted. Returns "
            "{allowed, reason} where the reason quotes the contract and the field that forbids it. "
            "Call this BEFORE answering anything that smells like marketing, targeting, scoring or "
            "screening. A refusal here is a complete answer: do not attempt the query anyway."
        ),
    )
    def check_allowed_use(product: str, column: str, intended_use: str) -> dict[str, Any]:
        arguments = {"product": product, "column": column, "intended_use": intended_use}
        return emit("check_allowed_use", arguments, impl.check_allowed_use(ctx, product, column, intended_use))

    @tool(
        name="generate_sql",
        description=(
            "Compile a question into SQL over the semantic layer. The SQL is assembled from "
            "versioned metric definitions, never invented, so you must not write SQL yourself. "
            "Returns sql plus metrics_used, products_used and notes. It may instead return "
            "needs_clarification (one term maps to several governed definitions, or a required "
            "metric parameter is missing) or needs_metrics (no governed metric fits). In those two "
            "cases ask the user or suggest adding a metric; do not improvise."
        ),
    )
    def generate_sql(question: str, products: str = "") -> dict[str, Any]:
        return emit(
            "generate_sql", {"question": question, "products": products}, impl.generate_sql(ctx, question, products)
        )

    @tool(
        name="validate_sql",
        description=(
            "Check SQL against the platform allowlist: one SELECT statement, no SELECT *, only "
            "published gold products and allowlisted ops tables, no file readers, and a LIMIT within "
            "the ceiling. You MUST call this and see valid=true before executing anything."
        ),
    )
    def validate_sql(sql: str) -> dict[str, Any]:
        return emit("validate_sql", {"sql": sql}, impl.validate_sql_tool(ctx, sql))

    @tool(
        name="execute_sql",
        description=(
            "Validate and run a query, returning row_count, columns and a bounded preview (never "
            "the whole result set). Reuses the platform connection and workgroup limits. Only call "
            "this with SQL that came out of generate_sql or that validate_sql confirmed."
        ),
    )
    def execute_sql(sql: str, max_rows: int = impl.MAX_PREVIEW_ROWS) -> dict[str, Any]:
        return emit("execute_sql", {"sql": sql, "max_rows": max_rows}, impl.execute_sql(ctx, sql, max_rows))

    @tool(
        name="explain_quality",
        description=(
            "Report what the quality gates know about a product: rules run, rules passing, critical "
            "and warning failures with their detail, freshness against the SLA, and the owner. Call "
            "this whenever the user asks whether a number can be trusted, or before presenting a "
            "figure you suspect."
        ),
    )
    def explain_quality(product: str = "") -> dict[str, Any]:
        return emit("explain_quality", {"product": product}, impl.explain_quality(ctx, product))

    @tool(
        name="trace_lineage",
        description=(
            "Show where a product's numbers come from (upstream) or what depends on it "
            "(downstream), as a dataset-level route plus the originating source systems. Use for "
            "'where does this come from' questions and for impact analysis."
        ),
    )
    def trace_lineage(product: str, direction: str = "upstream") -> dict[str, Any]:
        return emit(
            "trace_lineage", {"product": product, "direction": direction}, impl.trace_lineage(ctx, product, direction)
        )

    tools = [
        search_catalog,
        list_products,
        get_contract,
        resolve_metric,
        check_allowed_use,
        generate_sql,
        validate_sql,
        execute_sql,
        explain_quality,
        trace_lineage,
    ]
    if names is None:
        return tools
    wanted = tuple(names)
    unknown = [name for name in wanted if name not in TOOL_NAMES]
    if unknown:
        raise ValueError(f"unknown tool(s) requested: {', '.join(unknown)}. Known: {', '.join(TOOL_NAMES)}")
    return [item for item in tools if getattr(item, "tool_name", "") in wanted]


__all__ = ["READ_ONLY_TOOLS", "TOOL_NAMES", "build_tools"]
