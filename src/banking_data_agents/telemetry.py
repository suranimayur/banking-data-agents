"""Telemetry the platform emits about itself.

The dashboards and alarms in ``infra/stacks/observability.py`` are only as real as
the metrics behind them, so this module exists to make those metrics exist. It
emits **embedded metric format** (EMF) JSON to stdout: CloudWatch Logs turns each
line into a metric with no agent, no sidecar and no API call, which means the
emission costs a log line and cannot fail a request.

Two rules are enforced here.

**Metric names are constants.** ``infra/stacks/observability.py`` imports the
names from this module rather than repeating strings, so an alarm cannot silently
watch a metric nobody emits — the failure mode where a dashboard looks green
because it is watching nothing.

**A metric is never allowed to break an answer.** :func:`emit` swallows its own
errors: telemetry that can take down the request path is worse than no telemetry.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

#: One namespace for agent-side metrics, so a dashboard can be read without
#: knowing which module emitted what.
METRIC_NAMESPACE = "BDA/Agents"

#: Data-quality and pipeline metrics live in their own namespace because they are
#: produced by a different process (the pipeline) on a different schedule.
DATA_QUALITY_NAMESPACE = "BDA/DataQuality"

# --- metric names ----------------------------------------------------------
ANSWERS = "Answers"
REFUSALS = "Refusals"
NEEDS_CLARIFICATION = "ClarificationsRequested"
ANSWER_LATENCY_MS = "AnswerLatencyMs"
TOOL_CALLS = "ToolCalls"
PROMPT_TOKENS = "PromptTokens"
COMPLETION_TOKENS = "CompletionTokens"
EVAL_SCORE = "EvalScore"
EVIDENCE_COMPLETE = "EvidenceComplete"

DQ_PASS = "QualityRulesPassed"
DQ_WARNING = "QualityRulesWarned"
DQ_FAIL = "QualityRulesFailed"
DQ_SLA_MISSES = "FreshnessSlaMisses"

#: Emitted with ``outcome`` as a dimension so that refusals, clarifications and
#: answers can be split without a second metric.
OUTCOME_METRIC = ANSWERS

_ALL_METRICS = (
    ANSWERS,
    REFUSALS,
    NEEDS_CLARIFICATION,
    ANSWER_LATENCY_MS,
    TOOL_CALLS,
    PROMPT_TOKENS,
    COMPLETION_TOKENS,
    EVAL_SCORE,
    EVIDENCE_COMPLETE,
    DQ_PASS,
    DQ_WARNING,
    DQ_FAIL,
    DQ_SLA_MISSES,
)


def _enabled() -> bool:
    """Metrics are on by default, and off in unit tests unless asked for.

    A test suite asserting on emitted metrics is welcome to set
    ``BDA_EMIT_METRICS=1``; the default keeps test output free of JSON noise.
    """
    return os.environ.get("BDA_EMIT_METRICS", "").strip().lower() in {"1", "true", "yes"}


def emit(
    metric: str,
    value: float,
    *,
    unit: str = "Count",
    namespace: str = METRIC_NAMESPACE,
    dimensions: dict[str, str] | None = None,
) -> bool:
    """Emit one EMF metric line. Returns True when a line was written.

    The metric name is validated against :data:`_ALL_METRICS` so a typo produces a
    loud failure in a test rather than a dashboard that is quietly empty.
    """
    if metric not in _ALL_METRICS:
        raise ValueError(f"unknown metric {metric!r}; add it to telemetry._ALL_METRICS")

    if not _enabled():
        return False

    dims = {key: str(value_) for key, value_ in (dimensions or {}).items()}
    payload: dict[str, Any] = {
        "_aws": {
            "Timestamp": _now_ms(),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [list(dims)] if dims else [[]],
                    "Metrics": [{"Name": metric, "Unit": unit}],
                }
            ],
        },
        metric: float(value),
        **dims,
    }
    try:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return True
    except Exception:  # pragma: no cover - telemetry must never break a request
        return False


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def outcome_dimensions(outcome: str, *, agent: str) -> dict[str, str]:
    """Standard dimension set for an answer-side metric.

    Dimensions are the part of a metric that costs money and cardinality, so the
    set is fixed and small: agent and outcome are enough to answer every question
    the governance dashboard asks.
    """
    return {"agent": agent, "outcome": outcome}


__all__ = [
    "ANSWERS",
    "ANSWER_LATENCY_MS",
    "COMPLETION_TOKENS",
    "DATA_QUALITY_NAMESPACE",
    "DQ_FAIL",
    "DQ_PASS",
    "DQ_SLA_MISSES",
    "DQ_WARNING",
    "EVAL_SCORE",
    "EVIDENCE_COMPLETE",
    "METRIC_NAMESPACE",
    "NEEDS_CLARIFICATION",
    "OUTCOME_METRIC",
    "PROMPT_TOKENS",
    "REFUSALS",
    "TOOL_CALLS",
    "emit",
    "outcome_dimensions",
]
