# 14 · Observability

## The rule that shapes this chapter

> **Never put a metric on a dashboard that no process emits.**

The failure mode of an observability stack is not that it is missing; it is that it
is *green*. A dashboard watching a metric nobody writes shows a flat line, a flat
line looks like health, and the outage arrives unannounced. So `telemetry.py` exists
to make the metrics real, and it is wired into the three places that produce them:
`agents/base.py`, `evals/runner.py` and `pipeline/dq.py`.

## Embedded metric format

`telemetry.py::emit()` writes **EMF JSON to stdout**. CloudWatch Logs turns each
line into a metric with no agent, no sidecar and no API call — which is why this is
the right mechanism for a container that already logs.

Namespaces:

| Namespace | Producer | Metrics |
|-----------|----------|---------|
| `BDA/Agents` | Answering and evaluation | see below |
| `BDA/DataQuality` | The pipeline | `QualityRulesPassed`, `QualityRulesWarned`, `QualityRulesFailed`, `FreshnessSlaMisses` |

Agent-side metrics:

```
Answers · Refusals · ClarificationsRequested · AnswerLatencyMs · ToolCalls ·
PromptTokens · CompletionTokens · EvalScore · EvidenceComplete
```

`Answers` is emitted with `outcome` as a dimension, so refusals, clarifications and
answers can be split without a second metric. Dimensions are where cardinality — and
therefore cost — lives, so the dimension set is deliberately small and fixed
(`tools/context.py` and `telemetry.py` own it).

## Fail-open by design

**A metric is never allowed to break an answer.** `emit()` swallows its own
failures: a metric is a side effect of serving a request, not a precondition for it.
The alternative — an observability bug that takes down a banking assistant — is
strictly worse than a missing data point.

There is one thing `emit()` does *not* swallow: an unknown metric name raises
`ValueError` against `_ALL_METRICS`. A typo must not silently create a new,
unwatched time series, which is the other half of the green-dashboard problem.

## The metrics that answer the questions people actually ask

| Question | Metric |
|----------|--------|
| Are we answering? | `Answers` with `outcome` dimension |
| Are we refusing more than last week? | `Refusals`, split by reason in the envelope |
| Is it getting slow? | `AnswerLatencyMs` |
| Is it getting expensive? | `PromptTokens`, `CompletionTokens`, envelope `cost_usd` |
| Is the agent doing more work per answer? | `ToolCalls` |
| Did quality regress? | `QualityRulesFailed`, `QualityRulesWarned`, `FreshnessSlaMisses` |
| Is the gate holding? | `EvalScore`, `EvidenceComplete` |

The last two are the ones this platform has that a generic LLM deployment does not:
a governance platform should be able to alarm on *evidence completeness*, not just
on latency.

## Logging

`logging_setup.py` configures `structlog`-style structured logs. Two conventions:

- **Diagnostics go to the logger, never to stdout.** `bda ask --json` writes the
  envelope to stdout; a stray `print` corrupts it. A real bug in this repository was
  exactly that, and it is now a documented rule
  ([09](09-agents-and-bedrock.md)).
- **Meaningful event names.** `lake_fallback_local`, `query_engine_ready`,
  `answer_retention_failed`, `contracts_loaded`, `metrics_loaded`,
  `answer_store_created` — filterable, greppable, and stable enough to alarm on.

## Tests

`tests/unit/` covers `emit()`: a known metric writes a well-formed EMF line, an
unknown metric raises, and an emission error does not propagate into the request.
The pipeline tests assert that a quality run emits the quality metrics.

## Where this lands in production

- `infra/stacks/observability.py` provisions log groups, metric filters, alarms and
  dashboards over exactly the metric names above.
- The alarms map onto the runbook ([18](18-operations-runbook.md)): refusal rate,
  latency, quality failures, freshness SLA misses, eval score.
- Because the metrics are emitted from the application, the alarms keep working in
  a new region without a new agent install.

## Invariants a change must not break

1. Every metric on a dashboard is emitted by code in this repository.
2. `emit()` never raises into a request path.
3. An unknown metric name raises at development time.
4. New dimensions are justified: each one costs cardinality.
5. Nothing but the command's own output goes to stdout.
