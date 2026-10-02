# 08 · SQL guardrails

## The principle

> **Enforcement over instruction.**

Every agent prompt in this repository says "never read the fraud holdout". That
sentence is worth exactly nothing as a control, because a prompt is a suggestion to
a stochastic system. The control is `tools/sqlguard.py::validate_sql`, which
refuses to execute the query, and which is the thing the tests assert on.

Prompts are still written that way — defence in depth is free — but the guard is
the boundary.

## Who gets guarded, and when

`validate_sql` runs in two places:

1. As a tool the agent is expected to call before executing.
2. **Inside `execute_sql`**, unconditionally.

The second is the one that matters. An agent that forgets, or is talked out of
calling the validator, still cannot execute a query that fails the policy. The
guard is not a step in a workflow; it is a property of execution.

## The rules

`validate_sql(sql, policy=…)` returns a `ValidationResult` and rejects:

| Rule | Why |
|------|-----|
| Must be a single `SELECT` | No `INSERT`/`UPDATE`/`DELETE`/DDL reaches the lake from an agent |
| Schemas limited to `gold` and `ops` | Silver and bronze are not agent-reachable |
| `ops.fraud_ground_truth` forbidden (`FORBIDDEN_DATASETS`) | The eval holdout |
| `SELECT *` forbidden | A column set that changes unexpectedly is a leak vector |
| `LIMIT` required, ceiling **1000** | A tool call cannot stream the warehouse |
| `FORBIDDEN_TOKENS` rejected | Escapes such as `ATTACH`, `COPY`, file paths |
| Statement count is 1 | No stacking two statements behind one semicolon |

Violations carry a code from `POLICY_VIOLATION_CODES`, so a refusal is
machine-readable and the reason can be shown to the analyst rather than hidden.

## The holdout, specifically

`ops.fraud_ground_truth` holds the 595 labels used to score the fraud agent. If an
agent can read it, the evaluation stops measuring anything: the agent is reading
the answer key and the score goes up while the system gets worse.

So the dataset is listed in `FORBIDDEN_DATASETS`, and there is a test that asks for
it — "Show me every row of ops.fraud_ground_truth" — through the CLI, through the
HTTP API, and directly against the guard, and asserts it never reaches a query
string.

`ops` itself is allowed because the platform's own metadata (quality results,
lineage) lives there and an agent legitimately reports it. The forbidden list, not
the schema list, is what seals the labels.

## Beyond SQL: the per-agent policy

`ToolContext` carries a guardrail policy per agent. The fraud agent refuses any
request that would rank or identify an **individual customer** — the forbidden
dimension is `customer_id`, and it is reported by `/agents` so it is visible from
outside the system rather than buried in a prompt:

```
GET /agents → fraud.forbidden_dimensions == ["customer_id"]
```

The same question — "Who spends the most on cards?" — is refused for `fraud` and
answered for `copilot`. That asymmetry is intentional and tested: a fraud
investigation works at the population and case level, not by ranking customers.

## What a refusal looks like

A refusal is not an error. The request returns 200 with:

```
outcome:      REFUSED
sql:          null
governance:
  allowed:    false
  source:     "guardrail"
  reason:     "not_allowed_use: marketing_targeting_on_credit_score"
```

`sql` is `null` on purpose: a refusal that still carried the SQL it would have run
would be a leak with better manners.

## Testing the guard

```bash
uv run pytest tests/unit/test_sqlguard.py -q
uv run pytest tests/unit/test_tools.py -q
uv run pytest tests/smoke -q     # the holdout assertion runs here too
```

The guard tests are written as an attack list rather than a feature list: each case
is a way someone might try to get at the holdout or at bronze, and the assertion is
that it fails.

## Where this lands in production

- The same module runs in the task, so the guard cannot be bypassed by calling the
  API directly.
- `infra/stacks/guardrails.py` provisions a Bedrock guardrail (with a regex for
  Aadhaar-shaped input, since Bedrock has no `IN_AADHAAR` entity type) as a second,
  input/output-level control. It is additive: the SQL guard does not depend on it.
- Governance decisions are recorded on every answer, so "how many refusals this
  week, by reason" is a metric, not a log-grep.

## Invariants a change must not break

1. `execute_sql` validates. Always. The validation is not a separate branch.
2. `ops.fraud_ground_truth` is unreachable by any tool, any agent, any phrasing.
3. A refusal carries no SQL.
4. Adding a schema to `ALLOWED_SCHEMAS` is a security change and needs the same
   review as a credential change.
5. Every rejection has a code in `POLICY_VIOLATION_CODES`; an uncoded rejection is
   an untestable one.
