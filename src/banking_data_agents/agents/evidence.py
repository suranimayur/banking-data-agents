"""The evidence envelope.

An answer from an analytics agent is only useful if a reviewer can reconstruct
it. This module is the reason the platform can claim that: every answer leaves
the agent carrying the SQL that produced it, the versioned metrics it used, the
contracts it was built from, the rows it returned, the quality state of those
products, and the full tool trace — in one object.

Two things follow from having a single envelope type rather than ad-hoc dicts.
The API, the CLI and the console all render the same structure, so they cannot
drift apart. And the evaluation harness asserts on the envelope, not on prose,
so "did the agent cite a governed metric?" is a checkable question instead of a
matter of taste.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from banking_data_agents.tools.sqlguard import POLICY_VIOLATION_CODES

#: Tool names whose results carry provenance worth lifting into the envelope.
_PROVENANCE_TOOLS = (
    "search_catalog",
    "get_contract",
    "check_allowed_use",
    "generate_sql",
    "validate_sql",
    "execute_sql",
    "explain_quality",
    "trace_lineage",
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass
class Evidence:
    """Everything needed to audit one answer, in one object."""

    question: str
    answer: str
    trace_id: str = ""

    # -- provenance ---------------------------------------------------------
    #: The compiled SQL, or ``None`` when the answer was metadata-only.
    sql: str | None = None
    #: ``metric.<name>@<version>`` references the answer rests on.
    metrics: list[str] = field(default_factory=list)
    #: ``<product>@<version>`` of every contract consulted.
    products: list[str] = field(default_factory=list)
    #: Physical tables the SQL reads, from the guardrail's own parse.
    tables: list[str] = field(default_factory=list)
    #: Digest of all contracts at answer time, so drift is detectable later.
    contract_digest: str = ""
    #: The metric definitions as prose, for a reviewer without the YAML open.
    metric_definitions: dict[str, str] = field(default_factory=dict)

    # -- result -------------------------------------------------------------
    row_count: int | None = None
    columns: list[str] = field(default_factory=list)
    preview: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    engine: str = ""
    execution_id: str = ""

    # -- trust --------------------------------------------------------------
    quality: dict[str, Any] | None = None
    lineage: dict[str, Any] | None = None
    #: The contract's verdict when a governance check ran.
    governance: dict[str, Any] | None = None

    # -- scaffolding --------------------------------------------------------
    #: The tool calls in order, each with its arguments and a bounded result
    #: summary. This is the answer's audit trail, and it is what the console
    #: replays as a step timeline.
    trace: list[dict[str, Any]] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    provider: str = ""
    model_id: str = ""
    #: Latest day present in the data; relative windows resolve against it.
    asof: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""

    # -- outcomes -----------------------------------------------------------
    #: ``True`` when the contract forbade the request and the agent declined.
    refused: bool = False
    #: Set when the agent asked the user to disambiguate instead of guessing.
    needs_clarification: dict[str, Any] | None = None
    #: Set when no governed metric fits the question.
    missing_metrics: list[str] = field(default_factory=list)
    #: Guardrail violations, notes and caveats surfaced by the planner.
    warnings: list[str] = field(default_factory=list)

    # -- derived ------------------------------------------------------------
    @property
    def outcome(self) -> str:
        """A stable classification of how the request was resolved.

        Machine-readable on purpose: the evaluation suite asserts on this, so the
        gate is a checkable claim rather than a reading of prose.
        """
        if self.refused:
            return "REFUSED"
        if self.needs_clarification:
            return "NEEDS_CLARIFICATION"
        if self.missing_metrics:
            return "NO_GOVERNED_METRIC"
        if self.answered_from_data:
            return "ANSWERED_FROM_DATA"
        if self.quality or self.lineage:
            return "ANSWERED_FROM_METADATA"
        return "INCOMPLETE"

    @property
    def answered_from_data(self) -> bool:
        """True when a query actually ran.

        The distinction matters because a metadata answer ("yes, 31/31 rules
        pass") is complete, whereas a question that fell through the pipeline
        without running anything is a failure.
        """
        return self.row_count is not None

    @property
    def is_complete(self) -> bool:
        """True when the agent produced a usable outcome of some kind."""
        return bool(
            self.refused
            or self.needs_clarification
            or self.missing_metrics
            or self.answered_from_data
            or self.quality is not None
            or self.lineage is not None
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(asdict(self))
        payload["outcome"] = self.outcome
        payload["answered_from_data"] = self.answered_from_data
        payload["is_complete"] = self.is_complete
        return payload

    def to_json(self, *, indent: int = 2) -> str:
        import json

        return json.dumps(self.to_dict(), indent=indent, default=str)

    # -- rendering ----------------------------------------------------------
    def render(self, *, show_sql: bool = True, width: int = 78) -> str:
        """A terminal rendering: the answer, then the envelope beneath it."""
        lines = [self.answer.strip() or "(no answer produced)"]

        if show_sql and self.sql:
            lines.append("")
            lines.append("SQL")
            lines.append("-" * width)
            lines.append(self.sql.strip().rstrip(";") + ";")

        lines.append("")
        lines.append("evidence")
        lines.append("-" * width)
        for label, value in self._envelope_rows():
            if value in (None, "", [], {}):
                continue
            lines.append(f"{label:<13}{value}")
        return "\n".join(lines)

    def _envelope_rows(self) -> list[tuple[str, Any]]:
        rows: list[tuple[str, Any]] = [
            ("outcome", self._outcome_label()),
            ("metrics", ", ".join(self.metrics) or None),
            ("products", ", ".join(self.products) or None),
            ("tables", ", ".join(self.tables) or None),
            ("rows", self.row_count),
            ("quality", self._quality_label()),
            ("freshness", self._freshness_label()),
            ("lineage", self._lineage_label()),
            ("as of", self.asof or None),
            ("engine", self.engine or None),
            ("tools", " -> ".join(self.tools_called) or None),
            ("contracts", self.contract_digest[:12] or None),
            ("trace", self.trace_id or None),
        ]
        if self.warnings:
            rows.append(("notes", "; ".join(self.warnings[:4])))
        return rows

    def _outcome_label(self) -> str:
        outcome = self.outcome
        if outcome == "REFUSED":
            return "REFUSED (not permitted by contract)"
        if outcome == "NEEDS_CLARIFICATION":
            kind = (self.needs_clarification or {}).get("kind", "ambiguous")
            return f"NEEDS CLARIFICATION ({kind})"
        if outcome == "NO_GOVERNED_METRIC":
            return "NO GOVERNED METRIC for: " + ", ".join(self.missing_metrics)
        if outcome == "ANSWERED_FROM_DATA":
            return "ANSWERED from governed data"
        if outcome == "ANSWERED_FROM_METADATA":
            return "ANSWERED from metadata"
        return "INCOMPLETE"

    def _quality_label(self) -> str | None:
        if not self.quality:
            return None
        total = self.quality.get("total")
        if not total:
            return None
        label = f"{self.quality.get('passed', 0)}/{total} rules passing"
        critical = self.quality.get("critical_failures")
        if critical:
            label += f", {critical} CRITICAL"
        return label

    def _freshness_label(self) -> str | None:
        sla = (self.quality or {}).get("sla")
        if not isinstance(sla, dict) or not sla:
            return None
        return f"{sla.get('age_hours')}h old / SLA {sla.get('sla_freshness_hours')}h ({sla.get('status')})"

    def _lineage_label(self) -> str | None:
        if not self.lineage:
            return None
        sources = self.lineage.get("source_systems") or []
        count = self.lineage.get("edge_count", 0)
        return f"{count} {self.lineage.get('direction', 'upstream')} edge(s)" + (
            f" from {', '.join(sources)}" if sources else ""
        )


def build_evidence(
    *,
    question: str,
    answer: str,
    trace_id: str,
    trace: list[dict[str, Any]],
    outputs: dict[str, list[Any]],
    contract_digest: str = "",
    contract_versions: dict[str, str] | None = None,
    provider: str = "",
    model_id: str = "",
    asof: str = "",
    latency_ms: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    stop_reason: str = "",
) -> Evidence:
    """Assemble an envelope from a finished conversation's tool history.

    Written as a free function over the raw history rather than a method on the
    agent so it can be tested directly with hand-built histories — which is how
    the envelope tests avoid needing a model at all.

    ``contract_versions`` is the platform's product -> ``name@version`` map. It is
    injected rather than looked up so an envelope can be built for a conversation
    that never happened to call ``get_contract``.
    """

    def last(tool: str) -> Any:
        entries = outputs.get(tool) or []
        return entries[-1] if entries else None

    generated = last("generate_sql")
    generated = generated if isinstance(generated, dict) else {}
    executed = last("execute_sql")
    executed = executed if isinstance(executed, dict) else {}
    validated = last("validate_sql")
    validated = validated if isinstance(validated, dict) else {}
    quality = last("explain_quality")
    lineage = last("trace_lineage")
    governance = last("check_allowed_use")

    metrics = [str(item) for item in (generated.get("metrics_used") or [])]
    products = _referenced_products(outputs, generated, quality, lineage)
    warnings = [str(item) for item in (generated.get("notes") or [])]

    # Contract references: the platform's version map is authoritative; the
    # contracts the agent actually fetched fill any gaps in it.
    versions: dict[str, str] = dict(contract_versions or {})
    versions.update(_contract_versions(outputs))
    products_with_version = [versions.get(name, name) for name in products]

    # A refusal has two sources, and they are the same thing to a reader: the
    # platform declined. Either the contract forbade the use, or the guardrail
    # rejected the query under the agent's own policy (a forbidden grain, a
    # restricted dataset) before anything ran.
    policy_violations = [
        violation
        for violation in (validated.get("violations") or [])
        if isinstance(violation, dict) and violation.get("code") in POLICY_VIOLATION_CODES
    ]
    contract_refusal = isinstance(governance, dict) and governance.get("allowed") is False
    if contract_refusal:
        refused = True
    elif policy_violations:
        refused = True
        governance = {
            "allowed": False,
            "source": "guardrail",
            "reason": "; ".join(str(item.get("message")) for item in policy_violations),
            "codes": [str(item.get("code")) for item in policy_violations],
            "product": products[0] if products else "",
        }
    else:
        refused = False

    clarification = generated.get("needs_clarification") or None
    missing = [str(item) for item in (generated.get("needs_metrics") or [])]

    # A refusal is a complete answer; the SQL it declined to run is noise here.
    sql = None if refused else (executed.get("sql") or generated.get("sql") or None)

    if not validated.get("valid", True) and validated.get("violations"):
        warnings.extend(
            f"{violation.get('code')}: {violation.get('message')}"
            for violation in validated["violations"]
            if isinstance(violation, dict)
        )

    return Evidence(
        question=question,
        answer=answer,
        trace_id=trace_id,
        sql=sql,
        metrics=metrics,
        products=products_with_version,
        tables=[str(table) for table in (executed.get("tables") or validated.get("tables") or [])],
        contract_digest=contract_digest,
        metric_definitions=_metric_definitions(outputs),
        row_count=executed.get("row_count") if executed.get("executed") else None,
        columns=[str(column) for column in (executed.get("columns") or [])],
        preview=executed.get("preview") or [],
        truncated=bool(executed.get("truncated")),
        engine=str(executed.get("engine") or ""),
        execution_id=str(executed.get("execution_id") or ""),
        quality=quality if isinstance(quality, dict) else None,
        lineage=lineage if isinstance(lineage, dict) else None,
        governance=governance if isinstance(governance, dict) else None,
        trace=[
            {
                "tool": str(entry.get("tool")),
                "arguments": _jsonable(entry.get("arguments") or {}),
                "result_summary": _jsonable(entry.get("result_summary")),
            }
            for entry in trace
        ],
        tools_called=[str(entry.get("tool")) for entry in trace],
        provider=provider,
        model_id=model_id,
        asof=asof,
        latency_ms=round(latency_ms, 1),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=stop_reason,
        refused=refused,
        needs_clarification=clarification if isinstance(clarification, dict) else None,
        missing_metrics=missing,
        warnings=warnings,
    )


def _referenced_products(
    outputs: dict[str, list[Any]],
    generated: dict[str, Any],
    quality: Any,
    lineage: Any,
) -> list[str]:
    """The products this answer actually rests on.

    Falling back to *every* published product would be worse than saying nothing:
    an envelope that lists all three products for an answer that used one is
    provenance theatre. Each fallback below names a product the conversation
    genuinely touched, and the last resort is the single best search match.
    """
    from_generator = [str(item) for item in (generated.get("products_used") or [])]
    if from_generator:
        return from_generator

    if isinstance(quality, dict) and quality.get("product") not in (None, "", "all products"):
        return [str(quality["product"])]
    if isinstance(lineage, dict) and lineage.get("product"):
        return [str(lineage["product"])]

    contracted = _contract_versions(outputs)
    if contracted:
        return list(contracted)

    for result in outputs.get("search_catalog") or []:
        products = (result or {}).get("products") or []
        if products:
            return [str(products[0].get("product"))]
    return []


def _contract_versions(outputs: dict[str, list[Any]]) -> dict[str, str]:
    """Map product name -> ``name@version`` from every contract ever fetched."""
    versions: dict[str, str] = {}
    for result in outputs.get("get_contract") or []:
        if isinstance(result, dict) and result.get("found") and result.get("product"):
            versions[str(result["product"])] = f"{result['product']}@{result.get('version')}"
    return versions


def _metric_definitions(outputs: dict[str, list[Any]]) -> dict[str, str]:
    """Human-readable definitions for the metrics the plan used, best effort."""
    definitions: dict[str, str] = {}
    plan = None
    for result in outputs.get("generate_sql") or []:
        if isinstance(result, dict) and isinstance(result.get("plan"), dict):
            plan = result["plan"]
    for instance in (plan or {}).get("metrics", []) or []:
        ref = instance.get("ref")
        definition = instance.get("definition")
        if ref and definition:
            definitions[str(ref)] = " ".join(str(definition).split())
    return definitions


__all__ = ["Evidence", "build_evidence"]
