# 17 · Security and compliance

## The frame

This is a system that answers questions about customers' money. Its threat model is
not only "an attacker gets in" but also "an authorised analyst gets an answer they
were not entitled to, and acts on it". Both are handled the same way: **decide
before data is touched, and record the decision.**

## Four layers of control

| Layer | Control | Where |
|-------|---------|-------|
| Identity | OIDC deploy roles, no long-lived keys; per-environment roles | `infra/bootstrap`, `infra/stacks/identity.py` |
| Purpose | Contracts declare `not_allowed_use`; `check_allowed_use` enforces it | `contracts/*.yaml`, `tools/impl.py` |
| Query | `validate_sql` — schemas, holdout, `SELECT *`, `LIMIT`, single statement | `tools/sqlguard.py` |
| Model I/O | Bedrock guardrail; the Aadhaar regex | `infra/stacks/guardrails.py` |

They are independent on purpose. A prompt-injected agent still meets the SQL guard.
A guard bypass that gets to the model still meets the Bedrock guardrail. A leaked
credential still meets the IAM boundary.

## Purpose limitation, as a function

The interesting one is layer two, because most platforms only document it.

`customer_360@2.3.0` says it must not be used for
`marketing_targeting_on_credit_score` or
`individual_credit_decisions_without_human_review`. The tool `check_allowed_use`
consults that list, and the answer becomes:

```
outcome:    REFUSED
sql:        null
governance: {allowed: false, source: "contract",
             reason: "not_allowed_use: marketing_targeting_on_credit_score"}
```

The refusal names the clause. An analyst can read it and disagree with it, which is
what makes it governance rather than a wall. And it is a **test case**: the eval
suite contains the prohibited marketing question precisely so that a future change
cannot quietly allow it.

## PII and the individual

- `customer_id` is marked `pii: true` in the contracts.
- The `fraud` agent's forbidden dimension is `customer_id`: it refuses any request
  that would rank or identify individuals. The restriction is reported by `/agents`,
  so it is visible from outside the system rather than buried in a prompt.
- The same question — "Who spends the most on cards?" — is refused for `fraud` and
  answered for `copilot`. The asymmetry is deliberate and tested: fraud work happens
  at the population and case level.

## The holdout

`ops.fraud_ground_truth` holds the 595 evaluation labels. If an agent can read it,
the evaluation stops measuring anything — the score goes up while the system gets
worse. It is sealed by `FORBIDDEN_DATASETS` in `tools/sqlguard.py`, and the smoke
suite asks for it through the CLI and through HTTP and asserts it never reaches a
query ([08](08-sql-guardrails.md)).

## Input guardrail

Bedrock offers entity-based guardrails, and there is **no `IN_AADHAAR` entity type**,
so the Aadhaar-shaped identifier pattern is implemented as a regex in
`guardrails.py`. Without it, the most obvious Indian PII shape would have no control
at all. That is a documented gap in the managed service, closed explicitly rather
than silently.

The guardrail is configured with blocked input and output messaging via
`CfnGuardrail(scope, id, blocked_input_messaging=…, blocked_outputs_messaging=…,
name=…)`.

## Retention and audit

- Every answer is an auditable record ([10](10-the-evidence-envelope.md)) with a
  `trace_id`, a `contract_digest` and the full tool trace.
- Retention writes are best-effort so a retention failure can never fail an answer;
  reads are exact so an audit lookup cannot silently return nothing.
- `DEFAULT_TTL_DAYS = 400` bounds the table, and `/health` reports which store is in
  force — a deployment that lost its audit trail is visible from outside.
- Retention is **opt-in** (`BDA_ANSWER_TABLE`); the platform never implies it is
  retaining when it is not.

## Code paths that need extra review

`.github/CODEOWNERS` requires the right reviewers for:

- `contracts/`
- `semantic/metrics/`
- `tools/sqlguard.py`
- `infra/`

A mistake in any of these is a governance failure, not a bug. Widening
`ALLOWED_SCHEMAS` or removing an entry from `FORBIDDEN_DATASETS` is a security
change, whatever the commit message says.

## Compliance posture, honestly

What this platform gives an auditor:

- purpose limitation that is enforced and tested;
- a per-answer provenance record with product and metric versions;
- data-quality state attached to every answer;
- a sealed evaluation holdout;
- no long-lived credentials in the repository;
- infrastructure as code with drift detection.

What it does not give, and should not claim: a certification, a data-residency
guarantee beyond the region the stacks deploy into, or a legal opinion about whether
a specific use is permitted. The `not_allowed_use` list is the bank's decision, in
the bank's repository, reviewable like any other code.

## Invariants a change must not break

1. `FORBIDDEN_DATASETS` contains the holdout.
2. A governance decision is made before data is read, and recorded on the answer.
3. A refusal carries no SQL.
4. No long-lived AWS credentials anywhere in the repository.
5. PII stays identifiable in the contracts; an unmarked PII column is the bug.
