# 18 · Operations runbook

## How to use this

Each incident below is written the way it happens: the alarm or the report, the
first reassuring-but-wrong explanation, the actual diagnosis, and the fix. Where a
question can be answered by a command, the command is given.

Two rules apply throughout:

1. **Check the evidence before the explanation.** The envelope
   ([10](10-the-evidence-envelope.md)) already says which product, which metric
   version, which quality state and which tool calls produced a number. Read it
   first; most incidents end there.
2. **Roll back before you understand.** Promotion and rollback are version re-pins
   ([15](15-infrastructure.md)). Understanding can wait; a wrong number in front of
   an analyst cannot.

---

## Incident 1 · The pipeline failed

**Alarm:** `QualityRulesFailed` in `BDA/DataQuality`, or a red pipeline run.

1. `uv run bda pipeline dq` — read the failing rule and its zone.
2. A **FAIL** blocks publication by design; a **WARNING** does not. Confirm which
   you have before panicking; `bronze_impossible_negative_balances` is a WARNING
   over an expected 123 values.
3. A rule that just started failing usually means an upstream feed changed. Check
   `bronze_*` rules first — they are the ones that see an incoming feed.
4. If the failure is a genuine data incident, publication staying blocked is the
   correct outcome. Fix the feed, re-run, and do not "temporarily" disable the rule.

**Re-run:** `uv run bda pipeline run` (idempotent; safe to repeat).

---

## Incident 2 · Answers are wrong

**Report:** an analyst says a number changed.

Do this in order:

1. `uv run bda answers get <trace_id>` — replay the exact answer, including the SQL
   and the tool trace.
2. Compare `contract_digest`. A different digest means a contract changed, which is
   frequently the whole story.
3. Compare the metric version in the answer against `semantic/metrics/*.yaml`.
4. `uv run bda catalog <product>` — check the quality state and SLA of the product
   the answer cited.
5. If the SQL is wrong, it is a planner regression and no amount of data
   regeneration will fix it: [07](07-the-query-planner.md) and the regression cases
   in `tests/unit/test_planner.py`.

The envelope is designed so that this is a five-minute investigation rather than a
day. If it is taking longer, the envelope is missing a field — that is a bug worth
fixing.

---

## Incident 3 · Refusal rate spiked

**Alarm:** `Refusals` in `BDA/Agents`.

1. Determine whether it is **governance** (`source: contract` or `guardrail`) or
   **model behaviour** (`INCOMPLETE`, budget stops).
2. A governance spike usually follows a contract change — a new `not_allowed_use`
   entry is working as intended, and the right response is to tell the affected
   analysts, not to revert.
3. A behaviour spike usually follows a prompt or model change. Check whether the
   evaluation gate passed on the deployed tag. If it did not, you have a process
   problem as well as an incident.
4. A holdout access attempt is a **security** event regardless of rate
   ([17](17-security-and-compliance.md)).

---

## Incident 4 · Latency or cost regressed

**Alarm:** `AnswerLatencyMs`, `PromptTokens`, `CompletionTokens`, `ToolCalls`.

1. `ToolCalls` climbing is the leading indicator: the agent is looping. The budget
   (`agent_max_tool_calls`, default 8) will stop it, and the answer becomes
   `INCOMPLETE`.
2. `PromptTokens` climbing without `ToolCalls` climbing means the catalog or the
   contract text grew. Check `EVAL_SCORE` too — a bigger prompt that also answers
   better is a tradeoff, not an incident.
3. Per-user daily budgets (`user_daily_budget_usd`) exist so one user cannot move
   the platform's cost curve alone.

---

## Incident 5 · The service is down

1. `GET /health` — the body says which provider, which lake, how many products,
   which answer store and how many answers are retained.
2. If `/health` answers but `/ask` does not, the agent runtime is the problem, not
   the gateway.
3. If `/health` reports a fallback lake (`lake_fallback_local` in the logs), the
   platform is reading the filesystem instead of S3. That is a **degraded** state
   that still answers questions — and it is the most dangerous kind of green,
   because the answers are stale relative to S3.
4. Otherwise: `rollback.yml` first, diagnose second.

---

## Incident 6 · Quality looks fine but freshness is failing

**Alarm:** `FreshnessSlaMisses`.

Quality rules pass because the data that arrived is valid; the SLA fails because the
data that should have arrived did not. The two alarms are separate for that reason —
a platform that only checks validity will confidently report last month's numbers.

Check the pipeline's schedule and the upstream feed before touching the data.

---

## Routine operations

| Task | Command |
|------|---------|
| Regenerate data (dev) | `uv run bda data generate` |
| Re-run the pipeline | `uv run bda pipeline run` |
| Quality only | `uv run bda pipeline dq` |
| Confirm the gate | `uv run bda eval` |
| List retained answers | `uv run bda answers list` |
| Replay one | `uv run bda answers get <trace>` |
| Synth infra | `uv run bda infra synth --env dev` |
| Promote | `deploy-staging.yml`, then `deploy-prod.yml` by SHA |
| Roll back | `rollback.yml` |

## Escalation

- **Data correctness** → the product owner named in the contract.
- **Metric definition** → the owner named in the metric YAML.
- **Governance** → security, whatever the outcome looks like.
- **Holdout access attempt** → security, immediately.

## What "good" looks like

- `EVAL_SCORE` at 1.0 with `EvidenceComplete` at 1.0.
- Refusal rate low and stable, and every refusal explicable by a clause.
- Quality FAIL count zero, WARNING count known and stable.
- Freshness SLA misses zero.
- Latency and cost flat.
