"""Tool implementations.

Plain functions over a :class:`~banking_data_agents.tools.context.ToolContext`.
They are the real work; :mod:`banking_data_agents.tools.registry` only wraps them
in the schema the model sees. Keeping them separate means the whole tool surface
can be unit-tested without a model, a server or a network call — which is the
difference between a tested agent and a demo.

Every function returns a JSON-serialisable dictionary, because that is what comes
back out of a tool call and what lands in the audit trace.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from banking_data_agents.logging_setup import get_logger
from banking_data_agents.semantic.metrics import resolve_metric
from banking_data_agents.semantic.planner import explain_plan, plan_and_compile
from banking_data_agents.tools.context import ToolContext
from banking_data_agents.tools.sqlguard import validate_sql

logger = get_logger(__name__)

#: Rows returned to the model. Larger results are truncated and the answer says so.
MAX_PREVIEW_ROWS = 200


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def jsonable(value: Any) -> Any:
    """Recursively convert a value into something ``json.dumps`` can carry.

    This is not cosmetic. The Strands runtime serialises a tool result to JSON for
    the model, and when it cannot it silently falls back to ``str()`` — which
    hands the model a Python repr with single quotes that no JSON parser will
    accept. A tool that leaks one ``datetime`` therefore breaks every consumer
    downstream of it, including the model's own reading of the result.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if hasattr(value, "item"):  # numpy/polars scalars
        try:
            return jsonable(value.item())
        except Exception:
            pass
    return str(value)


#: Retained under the old name for readability inside this module.
_jsonable = jsonable


def _preview(frame: Any, limit: int) -> tuple[list[str], list[list[Any]]]:
    columns = list(frame.columns)
    rows = [[_jsonable(value) for value in row] for row in frame.head(limit).iter_rows()]
    return columns, rows


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def search_catalog(ctx: ToolContext, query: str, limit: int = 3) -> dict[str, Any]:
    """Rank published data products against a free-text question."""
    limit = max(1, min(int(limit or 3), 10))
    matches = ctx.catalog.search(query, limit=limit)
    return {
        "query": query,
        "products": [
            {
                "product": item["product"],
                "version": item["version"],
                "domain": item["domain"],
                "owner": item["owner"],
                "grain": item["grain"],
                "description": item["description"],
                "metrics_available": item["metrics"],
                "row_count": item["row_count"],
                "status": item["status"],
                "match_score": item.get("match_score", 0.0),
                "matched_terms": item.get("matched_terms", []),
            }
            for item in matches
        ],
        "contract_digest": ctx.catalog.digest,
    }


def list_products(ctx: ToolContext) -> dict[str, Any]:
    """The full published catalogue with live row counts and freshness."""
    rows = ctx.catalog.list_products()
    return {
        "count": len(rows),
        "products": [
            {
                "product": row["product"],
                "version": row["version"],
                "owner": row["owner"],
                "grain": row["grain"],
                "columns": row["columns"],
                "metrics": row["metrics"],
                "row_count": row["row_count"],
                "status": row["status"],
                "freshness_hours": row["age_hours"],
            }
            for row in rows
        ],
        "contract_digest": ctx.catalog.digest,
    }


def get_contract(ctx: ToolContext, product: str) -> dict[str, Any]:
    """The full contract for one product: columns, keys, SLAs and permitted uses."""
    try:
        contract = ctx.catalog.get_contract(product)
    except KeyError as error:
        return {"error": str(error), "found": False}

    return {
        "found": True,
        "product": contract.product,
        "version": contract.version,
        "domain": contract.domain,
        "owner": contract.owner,
        "steward": contract.steward,
        "description": " ".join(contract.description.split()),
        "grain": contract.grain,
        "primary_key": list(contract.primary_key),
        "classification": contract.classification,
        "refresh": {"schedule": contract.refresh.schedule, "job": contract.refresh.job},
        "sla": {
            "freshness_hours": contract.sla.freshness_hours,
            "availability": contract.sla.availability,
            "row_count_floor": contract.sla.row_count_floor,
        },
        "allowed_use": list(contract.allowed_use),
        "not_allowed_use": list(contract.not_allowed_use),
        "consumers": list(contract.consumers),
        "columns": [
            {
                "name": column.name,
                "type": column.type,
                "unit": column.unit,
                "description": column.description,
                "enum": column.enum,
                "metric_ref": column.metric_ref,
                "pii": column.pii,
                "caveat": column.caveat,
                "allowed_use": list(column.allowed_use),
                "not_allowed_use": list(column.not_allowed_use),
            }
            for column in contract.columns
        ],
        "dataset": contract.dataset,
    }


def resolve_metric_tool(ctx: ToolContext, term: str, product: str = "") -> dict[str, Any]:
    """Resolve a business term to governed metrics, declaring ambiguity."""
    result = resolve_metric(term, product=product or None)
    return result.as_dict()


def check_allowed_use(ctx: ToolContext, product: str, column: str, intended_use: str) -> dict[str, Any]:
    """Ask the contract whether a use is permitted. The answer is binding."""
    try:
        contract = ctx.catalog.get_contract(product)
    except KeyError as error:
        return {"allowed": False, "error": str(error), "reason": str(error)}

    allowed, reason = contract.usability(column, intended_use)
    return {
        "allowed": allowed,
        "reason": reason,
        "product": contract.product,
        "contract_version": contract.version,
        "column": column,
        "intended_use": intended_use,
        "product_allowed_use": list(contract.allowed_use),
        "product_not_allowed_use": list(contract.not_allowed_use),
    }


# ---------------------------------------------------------------------------
# Generation, validation, execution
# ---------------------------------------------------------------------------
def generate_sql(ctx: ToolContext, question: str, products: str | list[str] | None = "") -> dict[str, Any]:
    """Compile a governed plan for a question.

    The SQL is assembled from versioned metric fragments. It is never free-form:
    no formula is invented, every aggregate in the result is a named metric with
    an owner and a version, and a term that means two things produces a question
    instead of a number.
    """
    if isinstance(products, str):
        hints = [part.strip() for part in products.split(",") if part.strip()]
    else:
        hints = [str(part).strip() for part in (products or []) if str(part).strip()]
    plan, sql = plan_and_compile(question, products=hints or None, anchor=ctx.anchor)

    payload: dict[str, Any] = {
        "question": question,
        "sql": sql,
        "plan": plan.as_dict(),
        "summary": explain_plan(plan),
        "products_used": [plan.product] if plan.product else [],
        "metrics_used": [instance.ref for instance in plan.metrics],
        "notes": list(plan.notes),
    }

    if plan.ambiguities:
        options: list[dict[str, Any]] = []
        for ambiguity in plan.ambiguities:
            options.extend(
                {
                    "name": candidate.metric.name,
                    "product": candidate.metric.product,
                    "definition": " ".join(candidate.metric.definition.split()),
                    "ref": candidate.metric.ref,
                }
                for candidate in ambiguity.candidates
            )
        payload["needs_clarification"] = {
            "kind": "ambiguous_definition",
            "question": (
                "That term has more than one governed definition, and the answers differ. "
                f"Which do you mean: {', '.join(plan.ambiguous_terms)}?"
            ),
            "terms": list(plan.ambiguous_terms),
            "options": options,
        }
        return payload

    if plan.requires_parameters:
        options = []
        for instance in plan.requires_parameters:
            for parameter in instance.metric.parameters:
                options.append(
                    {
                        "name": parameter.name,
                        "product": instance.metric.product,
                        "definition": parameter.description,
                        "ref": instance.ref,
                        "required": parameter.required,
                        "type": parameter.type,
                    }
                )
        payload["needs_clarification"] = {
            "kind": "missing_parameter",
            "question": (
                "This metric is deliberately parameterised and has no default, so I will not "
                "guess a value: " + ", ".join(f"{o['name']} for {o['ref']}" for o in options) + "."
            ),
            "terms": list(plan.ambiguous_terms),
            "options": options,
        }
        return payload

    if not plan.has_metrics:
        payload["needs_metrics"] = plan.missing_terms or ["the requested measure"]
        payload["sql"] = None
    return payload


def validate_sql_tool(ctx: ToolContext, sql: str) -> dict[str, Any]:
    """Run the SQL guardrail. Must be called before every execution."""
    return validate_sql(sql, forbidden_dimensions=ctx.forbidden_dimensions).as_dict()


def execute_sql(ctx: ToolContext, sql: str, max_rows: int = MAX_PREVIEW_ROWS) -> dict[str, Any]:
    """Validate then execute a query, returning a bounded preview.

    Validation is repeated here rather than trusted from the caller: the guardrail
    has to hold even if a future caller forgets to ask.
    """
    validation = validate_sql(sql, forbidden_dimensions=ctx.forbidden_dimensions)
    if not validation.valid:
        return {
            "executed": False,
            "error": "the query failed validation and was not executed",
            "validation": validation.as_dict(),
        }

    max_rows = max(1, min(int(max_rows or MAX_PREVIEW_ROWS), 1_000))
    execution_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        frame = ctx.engine.query(sql)
    except Exception as error:
        logger.warning("query_error", execution_id=execution_id, error=str(error)[:300])
        return {
            "executed": False,
            "execution_id": execution_id,
            "error": str(error),
            "sql": sql,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    columns, preview = _preview(frame, max_rows)
    return {
        "executed": True,
        "execution_id": execution_id,
        "engine": ctx.engine.name,
        "row_count": frame.height,
        "returned": len(preview),
        "truncated": frame.height > len(preview),
        "columns": columns,
        "preview": preview,
        "elapsed_ms": elapsed_ms,
        "sql": sql,
        "tables": validation.tables,
    }


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------
def explain_quality(ctx: ToolContext, product: str = "") -> dict[str, Any]:
    """What the quality gates know about a product, including what failed."""
    summary = ctx.catalog.quality_summary(product or None)
    payload: dict[str, Any] = {
        "product": product or "all products",
        "total": summary.get("total", 0),
        "passed": summary.get("passed", 0),
        "critical_failures": summary.get("critical_failures", 0),
        "warning_failures": summary.get("warning_failures", 0),
        "failures": summary.get("failures", []),
    }
    if product:
        sla = ctx.catalog.sla(product)
        # The SLA row comes straight out of the warehouse, so it carries real
        # ``datetime`` objects; it must be converted here or the model receives a
        # Python repr instead of JSON.
        payload["sla"] = jsonable(sla[0]) if sla else None
        try:
            contract = ctx.catalog.get_contract(product)
            payload["contract"] = {
                "version": contract.version,
                "owner": contract.owner,
                "sla_freshness_hours": contract.sla.freshness_hours,
                "classification": contract.classification,
            }
        except KeyError:
            payload["contract"] = None
    return payload


def trace_lineage(ctx: ToolContext, product: str, direction: str = "upstream") -> dict[str, Any]:
    """Where a product's numbers come from, or what depends on it."""
    direction = direction if direction in {"upstream", "downstream"} else "upstream"
    try:
        routes = ctx.catalog.lineage(product, direction=direction)
    except Exception as error:
        return {"product": product, "direction": direction, "error": str(error), "edges": []}

    edges = [
        {
            "depth": route.get("level"),
            "dataset": route.get("dataset"),
            "via": route.get("transform_id") or "direct",
        }
        for route in routes
    ]
    return {
        "product": product,
        "direction": direction,
        "edge_count": len(edges),
        "source_systems": ctx.catalog.upstream_sources(product) if direction == "upstream" else [],
        "edges": edges[:60],
    }


__all__ = [
    "MAX_PREVIEW_ROWS",
    "check_allowed_use",
    "execute_sql",
    "explain_quality",
    "generate_sql",
    "get_contract",
    "jsonable",
    "list_products",
    "resolve_metric_tool",
    "search_catalog",
    "trace_lineage",
    "validate_sql_tool",
]
