"""The shared agent machinery.

Every agent in this project is the same machine with a different job description:
a Strands ``Agent`` over a narrowed tool surface, run under a budget, whose output
is wrapped in an :class:`~banking_data_agents.agents.evidence.Evidence` envelope.

Putting the invocation in one place is what makes the claims in the README
checkable. Budgets are enforced here, not by convention. The envelope is built
here, so no agent can forget to attach provenance. And the tool subset is chosen
here, so "the fraud agent cannot read the labels" is a property of the
configuration rather than of the prompt.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from typing import Any

from strands import Agent
from strands.types.agent import Limits

from banking_data_agents.agents.evidence import Evidence, build_evidence
from banking_data_agents.config import ModelTier, Settings, get_settings
from banking_data_agents.llm import get_model, get_provider_name
from banking_data_agents.logging_setup import get_logger
from banking_data_agents.telemetry import (
    ANSWER_LATENCY_MS,
    COMPLETION_TOKENS,
    EVIDENCE_COMPLETE,
    NEEDS_CLARIFICATION,
    OUTCOME_METRIC,
    PROMPT_TOKENS,
    REFUSALS,
    TOOL_CALLS,
    emit,
    outcome_dimensions,
)
from banking_data_agents.tools.context import ToolContext
from banking_data_agents.tools.registry import TOOL_NAMES, build_tools

logger = get_logger(__name__)


def _message_text(message: Any) -> str:
    """Concatenate the text blocks of a Strands message."""
    if not isinstance(message, dict):
        return ""
    blocks = message.get("content") or []
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(part for part in parts if part.strip()).strip()


def _usage(result: Any) -> tuple[int, int]:
    """Best-effort token accounting; never let telemetry break an answer."""
    try:
        summary = result.metrics.get_summary()
    except Exception:
        return 0, 0
    usage: dict[str, Any] = {}
    if isinstance(summary, dict):
        usage = summary.get("accumulated_usage") or summary.get("usage") or {}
    return int(usage.get("inputTokens", 0) or 0), int(usage.get("outputTokens", 0) or 0)


class BaseAgent:
    """A governed analytics agent: model + narrowed tools + evidence envelope."""

    #: Short identifier, used in traces and by :func:`get_agent`.
    name: str = "agent"
    #: Which model tier to use for this agent. See ``Settings.model_for``.
    tier: ModelTier = "reasoner"
    #: The system prompt. Set by each subclass.
    prompt: str = ""
    #: Tool subset. ``None`` means every tool.
    tools_enabled: Sequence[str] | None = None
    #: Columns this agent may not group or rank by. Enforced by the SQL guardrail
    #: on every query, so it holds whatever the model decides to do.
    forbidden_dimensions: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        context: ToolContext | None = None,
        model: Any | None = None,
        tools: list[Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ctx = context or ToolContext(settings=self.settings)
        if self.forbidden_dimensions and not self.ctx.forbidden_dimensions:
            self.ctx.forbidden_dimensions = self.forbidden_dimensions
        # The model factory reads the process-wide settings singleton (see
        # ``llm.factory``); tests switch provider with ``reset_models``.
        self.model = model or get_model(self.tier)
        self.tools = tools if tools is not None else build_tools(self.ctx, self.tools_enabled)
        self.agent = Agent(
            model=self.model,
            tools=self.tools,
            system_prompt=self.prompt,
            # No callback handler: the caller decides how to render. Streaming
            # prints from inside a library make an API response unreadable.
            callback_handler=None,
            name=self.name,
            description=self.prompt.splitlines()[0],
        )

    # -- introspection -------------------------------------------------------
    @property
    def tool_names(self) -> list[str]:
        available = set(TOOL_NAMES)
        if self.tools_enabled is None:
            return list(TOOL_NAMES)
        return [name for name in TOOL_NAMES if name in set(self.tools_enabled) and name in available]

    @property
    def model_id(self) -> str:
        try:
            return str(self.model.get_config().get("model_id", ""))
        except Exception:
            return ""

    @property
    def provider(self) -> str:
        return get_provider_name()

    @property
    def messages(self) -> list[dict[str, Any]]:
        return getattr(self.agent, "messages", [])

    # -- conversation --------------------------------------------------------
    def reset(self) -> None:
        """Forget the conversation history so the next question starts clean."""
        self.agent.messages.clear()
        self.ctx.clear()
        self.ctx.trace_id = ""

    def ask(self, question: str, *, max_turns: int | None = None) -> Evidence:
        """Answer one question and return the answer with its provenance."""
        question = (question or "").strip()
        if not question:
            return Evidence(question="", answer="Please ask a question.", trace_id="")

        trace_id = uuid.uuid4().hex[:12]
        self.ctx.trace_id = trace_id
        self.ctx.clear()

        limits = self._limits(max_turns)
        started = time.perf_counter()
        try:
            result = self.agent(question, limits=limits)
        except Exception as error:
            latency_ms = (time.perf_counter() - started) * 1000
            logger.error("agent_failed", agent=self.name, trace_id=trace_id, error=str(error)[:500])
            envelope = build_evidence(
                question=question,
                answer=f"The agent could not complete that request: {error}",
                trace_id=trace_id,
                trace=self.ctx.trace,
                outputs=self.ctx.outputs,
                contract_digest=self.ctx.catalog.digest,
                contract_versions=self._contract_versions(),
                provider=self.provider,
                model_id=self.model_id,
                latency_ms=latency_ms,
                stop_reason="error",
            )
            envelope.warnings.append(f"agent_error: {str(error)[:200]}")
            self._emit_metrics(envelope)
            return envelope

        latency_ms = (time.perf_counter() - started) * 1000
        input_tokens, output_tokens = _usage(result)
        envelope = build_evidence(
            question=question,
            answer=_message_text(result.message),
            trace_id=trace_id,
            trace=self.ctx.trace,
            outputs=self.ctx.outputs,
            contract_digest=self.ctx.catalog.digest,
            contract_versions=self._contract_versions(),
            provider=self.provider,
            model_id=self.model_id,
            asof=self._asof(),
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=str(getattr(result, "stop_reason", "") or ""),
        )
        self._attach_quality(envelope)
        logger.info(
            "agent_answered",
            agent=self.name,
            trace_id=trace_id,
            tools=len(envelope.tools_called),
            rows=envelope.row_count,
            outcome=envelope._outcome_label(),
            latency_ms=envelope.latency_ms,
        )
        self._emit_metrics(envelope)
        return envelope

    # -- internals -----------------------------------------------------------
    def _emit_metrics(self, envelope: Evidence) -> None:
        """Publish this answer to CloudWatch as EMF metrics.

        The dashboards and alarms in the infrastructure stack import their metric
        names from :mod:`banking_data_agents.telemetry`; this is where those metrics
        are actually produced. Emitting here — once per answer, rather than inside
        each agent — means a new agent gets measured for free, and it means a
        refusal is counted as a refusal rather than inferred from prose.

        Failure is impossible by construction: :func:`telemetry.emit` swallows its
        own errors, because telemetry that can fail a request is worse than no
        telemetry.
        """
        dimensions = outcome_dimensions(envelope.outcome, agent=self.name)

        emit(OUTCOME_METRIC, 1.0, dimensions=dimensions)
        emit(ANSWER_LATENCY_MS, envelope.latency_ms, unit="Milliseconds", dimensions=dimensions)
        emit(TOOL_CALLS, float(len(envelope.tools_called)), dimensions=dimensions)
        emit(
            EVIDENCE_COMPLETE,
            1.0 if envelope.is_complete else 0.0,
            dimensions=dimensions,
        )

        if envelope.outcome == "REFUSED":
            # A refusal is an *outcome*, but it is also the governance signal the
            # alerts watch, so it gets its own metric rather than being read off a
            # dimension of the one above.
            emit(REFUSALS, 1.0, dimensions=dimensions)
        if envelope.outcome == "NEEDS_CLARIFICATION":
            emit(NEEDS_CLARIFICATION, 1.0, dimensions=dimensions)

        if envelope.input_tokens:
            emit(PROMPT_TOKENS, float(envelope.input_tokens), dimensions=dimensions)
        if envelope.output_tokens:
            emit(COMPLETION_TOKENS, float(envelope.output_tokens), dimensions=dimensions)
    def _limits(self, max_turns: int | None = None) -> Limits | None:
        """The per-invocation budget.

        ``turns`` bounds the agent loop, so it is the tool-call budget plus the
        final answering turn. Without a cap a confused model can spend a real
        budget in a loop, which is exactly the failure CI must catch.
        """
        turns = max_turns or (self.settings.agent_max_tool_calls + 2)
        if turns <= 0:
            return None
        return Limits(turns=turns, total_tokens=self.settings.agent_max_tokens)

    def _asof(self) -> str:
        try:
            return self.ctx.anchor.isoformat()
        except Exception:
            return ""

    def _contract_versions(self) -> dict[str, str]:
        """Every published product as ``name@version``, from the contracts."""
        try:
            return {
                name: f"{contract.product}@{contract.version}" for name, contract in self.ctx.catalog.contracts.items()
            }
        except Exception:
            return {}

    def _attach_quality(self, envelope: Evidence) -> None:
        """Attach the quality state of the products an answer rests on.

        The agent may have called ``explain_quality`` itself, in which case its
        answer stands. Otherwise the platform supplies the state, because an
        answer's trustworthiness is part of its provenance whether or not anyone
        thought to ask.
        """
        if envelope.quality is not None or not envelope.products:
            return
        name = str(envelope.products[0]).split("@")[0]
        if not name:
            return
        try:
            summary = self.ctx.catalog.quality_summary(name)
            if not summary.get("total"):
                return
            sla = self.ctx.catalog.sla(name)
            envelope.quality = {**summary, "sla": sla[0] if sla else None}
        except Exception as error:
            logger.debug("quality_attach_failed", product=name, error=str(error)[:200])


def get_agent(name: str, **kwargs: Any) -> BaseAgent:
    """Construct an agent by name: ``copilot``, ``fraud`` or ``credit``."""
    from banking_data_agents.agents.registry import AGENTS

    key = name.strip().lower()
    if key not in AGENTS:
        raise KeyError(f"unknown agent '{name}'. Available: {', '.join(sorted(AGENTS))}")
    return AGENTS[key](**kwargs)


__all__ = ["BaseAgent", "get_agent"]
