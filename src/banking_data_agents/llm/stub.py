"""A deterministic, scripted Strands model.

Why this exists: an agent evaluation suite that calls a real LLM on every CI run
is slow, flaky and costs money, which in practice means it gets disabled. This
model implements the Strands ``Model`` interface and follows a fixed, inspectable
policy, so the *orchestration* — tool selection order, ambiguity handling,
refusals, the evidence envelope — is exercised deterministically and for free.

It is emphatically not a fake LLM pretending to be smart. It is a scripted policy
that is honest about being scripted, and it is replaced by a real model by
changing one environment variable.
"""

from __future__ import annotations

import ast
import json
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from typing import Any, Literal

from strands.models.model import Model
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)

#: The 5-stage pipeline the Copilot is expected to follow, in order.
PIPELINE: tuple[str, ...] = (
    "search_catalog",
    "check_allowed_use",
    "generate_sql",
    "validate_sql",
    "execute_sql",
)

#: Terms that mean the user is asking about trust rather than about numbers.
QUALITY_TERMS = ("reliable", "trust", "quality", "accurate", "correct", "valid", "confidence", "stale")
LINEAGE_TERMS = ("where", "lineage", "source", "come from", "derived", "upstream", "origin")
#: Verbs that describe approaching a person with an offer. Any one of these makes
#: a request marketing-shaped, whatever else it says.
OUTREACH_TERMS = (
    "target",
    "campaign",
    "solicit",
    "pre screen",
    "prescreen",
    "telemarket",
    "cross sell",
    "cross-sell",
    "upsell",
    "promote",
    "reach out",
    "cold call",
)
#: Purpose stems. On their own these are usually a *column name* — "marketing
#: segment" is a breakdown, not a marketing activity — so they only count when the
#: request also names a person or asks about permission.
PURPOSE_TERMS = ("market", "advertis", "promotion")
PERSON_TERMS = ("customer", "client", "borrower", "applicant", "individual", "user", "them")
PERMISSION_TERMS = ("can i", "may i", "allowed", "permitted", "permission", "ok to", "okay to", "legal")

#: Terms that mean the generator must be allowed to ask instead of assuming.
CLARIFY_TERMS = ("high value", "risk", "value", "segment", "best", "important")

#: Terms that mean the user wants a number. Their absence, together with a quality
#: or lineage term, makes the question a metadata question rather than a data one.
MEASURE_TERMS = (
    "how many",
    "how much",
    "count",
    "total",
    "sum",
    "average",
    "mean",
    "median",
    "balance",
    "spend",
    "amount",
    "volume",
    "number",
    "top",
    "rate",
    "share",
    "ratio",
    "highest",
    "lowest",
    "largest",
    "smallest",
    "most",
    "least",
    "exposure",
    "revenue",
)


def _text_of(content: Any) -> str:
    return " ".join(block.get("text", "") for block in content if isinstance(block, dict))


def _tool_calls(messages: Messages) -> list[Any]:
    """Every tool invocation so far, with its arguments.

    Typed as ``Any`` because the SDK models a tool-use block as a ``TypedDict``
    and this policy only ever reads its two documented keys.
    """
    calls: list[Any] = []
    for message in messages:
        for block in message.get("content", []):
            if isinstance(block, dict) and "toolUse" in block:
                calls.append(block["toolUse"])
    return calls


def _tool_results(messages: Messages) -> dict[str, Any]:
    """Map ``toolUseId`` -> parsed result.

    Tool results reach the model as text, normally JSON. ``literal_eval`` is the
    fallback because a runtime that cannot JSON-encode a result will hand over a
    Python repr instead, and a model that silently treats that as opaque text is
    a model that answers from nothing.
    """
    results: dict[str, Any] = {}
    for message in messages:
        for block in message.get("content", []):
            if not isinstance(block, dict) or "toolResult" not in block:
                continue
            payload = block["toolResult"]
            raw = ""
            for item in payload.get("content", []):
                if isinstance(item, dict) and "text" in item:
                    raw += item["text"]
            results[payload["toolUseId"]] = _parse_result(raw)
    return results


def _parse_result(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {"_raw": raw}


def _latest(tool: str, calls: list[dict[str, Any]], results: dict[str, Any]) -> Any:
    for call in reversed(calls):
        if call["name"] == tool:
            return results.get(call["toolUseId"])
    return None


class ScriptedTurn:
    """One model turn: either a tool call or a final text answer."""

    def __init__(self, text: str | None = None, tool: str | None = None, arguments: dict[str, Any] | None = None):
        self.text = text
        self.tool = tool
        self.arguments = arguments or {}
        if (text is None) == (tool is None):
            raise ValueError("a turn is either text or a tool call, never both or neither")


def default_policy(messages: Messages, available: set[str]) -> ScriptedTurn:
    """The reference agent policy, expressed as plain Python.

    Read this top to bottom and you have the agent's control flow: retrieve,
    check governance, generate, validate, execute, then answer — asking rather
    than guessing when the request is ambiguous, and refusing when the contract
    forbids the use.
    """
    turn = _current_turn(messages)
    question = _first_user_text(turn)
    lowered = question.lower()
    calls = _tool_calls(turn)
    results = _tool_results(turn)
    used = {call["name"] for call in calls}

    def can(tool: str) -> bool:
        return tool in available

    # Stage 1 — discover the data products.
    if "search_catalog" not in used and can("search_catalog"):
        return ScriptedTurn(tool="search_catalog", arguments={"query": question, "limit": 3})

    products = _products_from(_latest("search_catalog", calls, results))

    # Stage 2 — governance check, but only when the request could be restricted.
    if can("check_allowed_use") and "check_allowed_use" not in used and _is_governance_sensitive(lowered):
        column, use = _governance_probe(lowered)
        return ScriptedTurn(
            tool="check_allowed_use",
            arguments={"product": products[0] if products else "customer_360", "column": column, "intended_use": use},
        )

    decision = _latest("check_allowed_use", calls, results)
    if isinstance(decision, dict) and decision.get("allowed") is False:
        # A refusal is a complete answer. Do not attempt the query.
        return ScriptedTurn(text=_refusal(decision))

    # A pure trust or lineage question is answered from metadata. Running it through
    # the SQL path would return a number nobody asked for, alongside the answer.
    metadata_only = (
        any(term in lowered for term in QUALITY_TERMS) or any(term in lowered for term in LINEAGE_TERMS)
    ) and not any(term in lowered for term in MEASURE_TERMS)

    # Stage 3 — generate SQL against the semantic layer.
    if not metadata_only and "generate_sql" not in used and can("generate_sql"):
        return ScriptedTurn(
            tool="generate_sql",
            # The tool schema declares a comma-separated string, so the reference
            # policy must speak that contract rather than passing a list.
            arguments={"question": question, "products": ", ".join(products)},
        )

    generated = _latest("generate_sql", calls, results)
    if isinstance(generated, dict) and generated.get("needs_clarification"):
        return ScriptedTurn(text=_clarification(generated))

    # Trust and lineage questions are answered from metadata, not from SQL, so they
    # are checked before the "no metric fits" exit: "is this reliable?" is not a
    # request for a number and must not be answered with one.
    if any(term in lowered for term in QUALITY_TERMS) and "explain_quality" not in used and can("explain_quality"):
        return ScriptedTurn(
            tool="explain_quality",
            arguments={"product": products[0] if products else ""},
        )
    if any(term in lowered for term in LINEAGE_TERMS) and "trace_lineage" not in used and can("trace_lineage"):
        return ScriptedTurn(
            tool="trace_lineage",
            arguments={"product": products[0] if products else "customer_360"},
        )

    if (
        isinstance(generated, dict)
        and generated.get("needs_metrics")
        and _latest("explain_quality", calls, results) is None
    ):
        return ScriptedTurn(text=_missing_metrics(generated))

    # Stage 4 — validate before executing. Never run unvalidated SQL.
    if "validate_sql" not in used and can("validate_sql") and isinstance(generated, dict) and generated.get("sql"):
        return ScriptedTurn(tool="validate_sql", arguments={"sql": generated["sql"]})

    validated = _latest("validate_sql", calls, results)
    if isinstance(validated, dict) and validated.get("valid") is False:
        return ScriptedTurn(text=_invalid_sql(validated))

    # Stage 5 — execute.
    if "execute_sql" not in used and can("execute_sql") and isinstance(generated, dict) and generated.get("sql"):
        return ScriptedTurn(tool="execute_sql", arguments={"sql": generated["sql"], "max_rows": 200})

    executed = _latest("execute_sql", calls, results)
    return ScriptedTurn(text=_final_answer(question, generated, executed, calls, results))


def _has_text(message: Any) -> bool:
    return bool(_text_of(message.get("content", [])).strip())


def _current_turn(messages: Messages) -> Messages:
    """The messages belonging to the question being answered right now.

    Two traps are handled here. A scripted policy has no memory, so it must see
    only the current turn — otherwise the second question in a conversation finds
    the first question's tool calls already "done" and answers from stale results.
    And tool results arrive as ``user`` messages, so the turn must start at the
    last user message that actually carries text.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and _has_text(message):
            return messages[index:]
    return messages


def _first_user_text(messages: Messages) -> str:
    for message in messages:
        if message.get("role") == "user":
            text = _text_of(message.get("content", []))
            if text.strip():
                return text.strip()
    return ""


def _products_from(catalog_result: Any) -> list[str]:
    if isinstance(catalog_result, dict):
        products = catalog_result.get("products") or []
        return [str(p["product"]) for p in products if isinstance(p, dict) and p.get("product")]
    return []


def _is_governance_sensitive(lowered: str) -> bool:
    """Whether a request could be governed by an allowed-use restriction.

    Deliberately not a keyword list on the word "marketing". "Total balance by
    marketing segment" is a legitimate breakdown that merely names a column, and
    sending it to the contract for approval would refuse a question nobody asked
    to do anything wrong with. The trigger is an *activity* — targeting, a
    campaign, an offer — or a purpose word that arrives with a person or a
    permission question attached.
    """
    if any(term in lowered for term in OUTREACH_TERMS):
        return True
    if any(term in lowered for term in PURPOSE_TERMS):
        return any(term in lowered for term in PERSON_TERMS) or any(term in lowered for term in PERMISSION_TERMS)
    return False


def _governance_probe(lowered: str) -> tuple[str, str]:
    """Infer the (column, intended_use) pair the request implies."""
    if "credit score" in lowered or "score" in lowered:
        return "credit_score", "marketing_targeting_on_credit_score"
    if "age" in lowered:
        return "age", "marketing_targeting_on_age"
    return "credit_score", "marketing_targeting_on_credit_score"


def _refusal(decision: dict[str, Any]) -> str:
    return (
        "I can't run that query.\n\n"
        f"{decision.get('reason', 'The requested use is not permitted for this data product.')}\n\n"
        "If that restriction is wrong for your case, the data product owner can amend the contract."
    )


def _clarification(generated: dict[str, Any]) -> str:
    detail = generated.get("needs_clarification") or {}
    options = detail.get("options") or []
    lines = [detail.get("question", "Could you clarify which definition you mean?")]
    for option in options:
        lines.append(f"  - {option.get('name')} ({option.get('product')}): {option.get('definition')}")
    lines.append("")
    lines.append("I have not run the query yet, because the two answers would differ.")
    return "\n".join(lines)


def _missing_metrics(generated: dict[str, Any]) -> str:
    needed = generated.get("needs_metrics") or []
    return (
        "I don't have a governed metric for: " + ", ".join(map(str, needed)) + ".\n\n"
        "Rather than invent a formula, the right fix is to add a versioned metric definition "
        "to the semantic layer. Until then I can't answer this accurately."
    )


def _invalid_sql(validated: dict[str, Any]) -> str:
    violations = validated.get("violations") or []
    lines = ["The generated query failed validation, so I did not execute it."]
    for violation in violations:
        lines.append(f"  - {violation.get('code')}: {violation.get('message')}")
    return "\n".join(lines)


def _final_answer(
    question: str,
    generated: Any,
    executed: Any,
    calls: list[dict[str, Any]],
    results: dict[str, Any],
) -> str:
    """Compose the answer from what the tools actually returned."""
    quality = _latest("explain_quality", calls, results)
    lineage = _latest("trace_lineage", calls, results)

    if not isinstance(executed, dict):
        # No query ran. A metadata answer is still a complete answer.
        if isinstance(quality, dict):
            return _quality_answer(quality)
        if isinstance(lineage, dict):
            return _lineage_answer(lineage)
        return (
            "I could not complete that request: the query pipeline did not return a result set.\nNothing was executed."
        )

    rows = executed.get("row_count")
    if rows is None:
        rows = executed.get("rows") or 0
    preview = executed.get("preview") or []
    columns = executed.get("columns") or []

    lines = [f"{rows} row(s) returned."]
    if preview:
        lines.append("")
        lines.append(" | ".join(str(c) for c in columns))
        lines.append("-" * min(60, max(20, len(" | ".join(map(str, columns))))))
        for row in preview[:10]:
            lines.append(" | ".join("" if v is None else str(v) for v in row))

    metrics = (generated or {}).get("metrics_used") or []
    if metrics:
        lines.append("")
        lines.append("Metrics used: " + ", ".join(str(m) for m in metrics))

    products = (generated or {}).get("products_used") or []
    if products:
        lines.append("Data products: " + ", ".join(str(p) for p in products))

    # Planner caveats are part of the answer, not an appendix in the audit log. A
    # currency-mixing warning that only appears in the envelope is invisible to
    # the analyst reading the number.
    notes = (generated or {}).get("notes") or []
    if notes:
        lines.append("")
        lines.append("Caveats:")
        for note in notes:
            lines.append(f"  - {note}")

    # Offer the trust follow-up if the caller asked about reliability.
    if isinstance(quality, dict):
        lines.append("")
        lines.append(_quality_line(quality))
    if isinstance(lineage, dict):
        lines.append("")
        lines.append(_lineage_line(lineage))

    return "\n".join(lines)


def _quality_line(quality: dict[str, Any]) -> str:
    return f"Data quality: {quality.get('passed', 0)}/{quality.get('total', 0)} rules passing" + (
        f", {quality.get('critical_failures', 0)} critical failure(s)"
        if quality.get("critical_failures")
        else ", no critical failures"
    )


def _quality_answer(quality: dict[str, Any]) -> str:
    lines = [
        f"For {quality.get('product', 'the platform')}: {_quality_line(quality)}.",
    ]
    sla = quality.get("sla")
    if isinstance(sla, dict) and sla:
        lines.append(
            f"Freshness: {sla.get('age_hours')}h old against an SLA of "
            f"{sla.get('sla_freshness_hours')}h, status {sla.get('status')}."
        )
    failures = quality.get("failures") or []
    if failures:
        lines.append("")
        lines.append("Outstanding findings:")
        for failure in failures[:8]:
            lines.append(
                f"  - [{failure.get('severity')}] {failure.get('rule_id')} "
                f"({failure.get('dataset')}): {failure.get('detail')}"
            )
    else:
        lines.append("No failing rules are recorded for it.")
    return "\n".join(lines)


def _lineage_answer(lineage: dict[str, Any]) -> str:
    sources = lineage.get("source_systems") or []
    lines = [
        f"{lineage.get('product')} takes its data from "
        + (", ".join(sources) if sources else "the sources listed below")
        + "."
    ]
    edges = lineage.get("edges") or []
    if edges:
        lines.append("")
        lines.append(f"{lineage.get('direction', 'upstream')} route, {lineage.get('edge_count', 0)} edge(s):")
        for edge in edges[:12]:
            lines.append(f"  - depth {edge.get('depth')}: {edge.get('dataset')} (via {edge.get('via')})")
    return "\n".join(lines)


def _lineage_line(lineage: dict[str, Any]) -> str:
    sources = lineage.get("source_systems") or []
    return f"Lineage: {lineage.get('edge_count', 0)} {lineage.get('direction', 'upstream')} edge(s)" + (
        f"; originating systems: {', '.join(sources)}" if sources else ""
    )


class StubModel(Model):
    """A Strands model that follows :func:`default_policy` deterministically."""

    def __init__(self, policy: Callable[[Messages, set[str]], ScriptedTurn] | None = None, **config: Any) -> None:
        self._policy = policy or default_policy
        self._config: dict[str, Any] = {
            "model_id": "stub:scripted",
            "temperature": 0.0,
            "max_tokens": 4096,
            **config,
        }

    # -- Strands Model contract ---------------------------------------------
    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self._config

    def structured_output(
        self,
        output_model: type[Any],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        raise NotImplementedError(
            "StubModel does not implement structured_output; use tool calls for structured results."
        )

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: Any = None,
        system_prompt_content: Any = None,
        invocation_state: Any = None,
        cancel_signal: Any = None,
        agent_metadata: Any = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        available = {spec["name"] for spec in (tool_specs or [])}
        turn = self._policy(messages, available)

        if turn.tool and turn.tool not in available:
            # The policy guessed a tool this agent was not given. Fall back to
            # plain text rather than raising, so a misconfigured agent surfaces
            # as a bad answer in evals instead of an exception in production.
            logger.warning("stub_tool_unavailable", tool=turn.tool, available=sorted(available))
            turn = ScriptedTurn(text=f"I have no tool available to answer that (wanted '{turn.tool}').")

        yield {"messageStart": {"role": "assistant"}}

        if turn.tool:
            tool_use_id = f"stub-{turn.tool}-{len(_tool_calls(messages)) + 1}"
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": tool_use_id, "name": turn.tool}},
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(turn.arguments)}}}}
            yield {"contentBlockStop": {}}
            stop_reason: Literal["tool_use", "end_turn"] = "tool_use"
        else:
            yield {"contentBlockDelta": {"delta": {"text": turn.text or ""}}}
            yield {"contentBlockStop": {}}
            stop_reason = "end_turn"

        yield {"messageStop": {"stopReason": stop_reason, "additionalModelResponseFields": None}}

        # Token accounting is fabricated but plausible, so cost plumbing can be
        # exercised in tests without pretending the numbers are real.
        prompt_chars = sum(len(_text_of(m.get("content", []))) for m in messages)
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": max(1, prompt_chars // 4),
                    "outputTokens": max(1, len(turn.text or json.dumps(turn.arguments)) // 4),
                    "totalTokens": max(2, (prompt_chars + len(turn.text or "")) // 4),
                },
                "metrics": {"latencyMs": 0},
            }
        }


__all__ = ["PIPELINE", "ScriptedTurn", "StubModel", "default_policy"]
