# 17 — Observability with CloudWatch EMF

**Level:** intermediate → advanced · **You will be able to:** turn a log line into a
metric, and explain why most dashboards are fictional.

## The idea

The three pillars, and what each answers:

| Pillar | Question | Cost |
|--------|----------|------|
| **Logs** | What happened, in detail? | storage + query |
| **Metrics** | How much / how often, over time? | cheap to store, cheap to chart |
| **Traces** | Where did this one request spend its time? | instrumentation effort |

The failure mode this chapter is about is specific and extremely common: **a dashboard
whose panels are verified by nothing.** Someone writes a CloudFormation alarm
referencing `BankingDataAgents/Refusals`, nobody ever emits that metric, and the panel
renders a flat line at zero forever. It looks like "no refusals". It is actually "no
data", and the two are indistinguishable on the chart. The system appears healthy
precisely because it is blind.

**CloudWatch EMF** (Embedded Metric Format) is the fix that makes this hard to do by
accident: you emit a **structured log line**, and CloudWatch **extracts metrics from
it** — no `PutMetricData` call, no separate API, no IAM permission beyond logging.

## The mental model

```json
{
  "_aws": { "Timestamp": 1759488000000, "CloudWatchMetrics": [{
      "Namespace": "BankingDataAgents",
      "Dimensions": [["Agent","Outcome"]],
      "Metrics": [{ "Name": "Outcome", "Unit": "Count" },
                  { "Name": "AnswerLatencyMs", "Unit": "Milliseconds" }]
  }]},
  "Agent": "copilot", "Outcome": "ANSWERED_FROM_DATA",
  "Outcome": 1, "AnswerLatencyMs": 3310.2
}
```

One line of stdout does three jobs at once: it is a **log** a human can read, a
**metric** CloudWatch indexes, and a **trace record** for that answer. The dimensions
are declared in the payload, so the metric and the log can never disagree about what
they mean.

## The minimum you need

```python
def emit(name, value, *, unit="Count", dimensions=None):
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000),
                 "CloudWatchMetrics": [{"Namespace": "MyApp",
                    "Dimensions": [list((dimensions or {}).keys())],
                    "Metrics": [{"Name": name, "Unit": unit}]}]},
        **(dimensions or {}), name: value,
    }))
```

The two rules that make it work: the metric name must appear as a **top-level value**,
and every dimension must be declared **and also present as a top-level field**.

## In this project

`telemetry.py` defines the metric names as constants, and — this is the important part
— **the infrastructure imports those same names.** The dashboard and the code have
one source of truth, so a rename cannot desynchronise them.

`agents/base.py::BaseAgent._emit_metrics` is where they are produced, once per answer,
in the shared base class. Three consequences follow from that placement:

1. **A new agent is measured for free.** There is no per-agent instrumentation to
   forget.
2. **A refusal is counted as a refusal**, not inferred from prose by a human reading
   logs.
3. **Telemetry cannot fail a request.** `telemetry.emit` swallows its own errors,
   because *telemetry that can fail a request is worse than no telemetry*.

Metrics emitted per answer:

| Metric | Meaning |
|--------|---------|
| `Outcome` | 1, dimensioned by `Agent` and `Outcome` |
| `AnswerLatencyMs` | end-to-end answer latency |
| `ToolCalls` | tool calls per answer (a runaway-loop detector) |
| `EvidenceComplete` | 1 if the envelope is complete, else 0 |
| `Refusals` | its own metric, because it is the governance signal alerts watch |
| `NeedsClarification` | ambiguity rate |
| `PromptTokens` / `CompletionTokens` | the cost drivers |

Note `Refusals`. It is *also* visible as a dimension of `Outcome`, and it gets a
separate metric anyway, because "a refusal happened" is the thing an alarm should fire
on, and alarms are easier to write against a first-class metric than against one
dimension of a compound one.

The same pattern appears outside the agent loop: `evals/runner.py` emits the
evaluation score, so a regression is visible in the same place as production
behaviour, and `pipeline/dq.py` emits data-quality results, so a data incident and an
agent incident are visible side by side.

## Level up

**Alert on symptoms, not causes.** "Refusal rate tripled" and "evidence completeness
dropped" are symptoms an operator can act on. "CPU is 80%" usually is not.

**Cardinality is a cost, and a trap.** A dimension per `trace_id` creates a metric per
request — enormous cost and a useless chart. Dimensions should be low-cardinality
(`agent`, `outcome`), which is why the trace id lives in the log and not in the metric.

**Percentiles over averages.** A mean latency of 200ms is compatible with 5% of users
waiting 8 seconds. p50/p95/p99 are the honest metrics.

**What to learn next:** CloudWatch composite alarms (an AND of two conditions reduces
pager noise), `CloudWatch Logs Insights` for querying the structured logs, and
OpenTelemetry as the vendor-neutral successor once you have more than one service.

## Try it

```bash
cd banking-data-agents

# Emit one answer's metrics and look at the raw EMF line.
uv run bda ask "How many customers do we have by region?" >/dev/null
uv run python -c "
from banking_data_agents.telemetry import OUTCOME_METRIC, ANSWER_LATENCY_MS, emit
emit(OUTCOME_METRIC, 1.0, dimensions={'agent':'copilot','outcome':'ANSWERED_FROM_DATA'})
emit(ANSWER_LATENCY_MS, 42.0, unit='Milliseconds', dimensions={'agent':'copilot'})
"
```
