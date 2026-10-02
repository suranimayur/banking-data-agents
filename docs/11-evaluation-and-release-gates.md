# 11 · Evaluation and release gates

## The principle

> **Nothing ships unmeasured.**

A prompt change, a metric definition change, a planner tweak — each can look
harmless and quietly move the answer quality. The only defence is a suite of cases
with expected outcomes, run before every deployment, and a gate that fails the
pipeline.

## The harness

```
evals/
  cases.py    the suites: deterministic, floci, and the adversarial cases
  runner.py   run_evals(suite=, fail_under=, report=) → exit code
```

`run_evals` returns **0** for pass, **1** for gate failed, **2** for unknown suite.
The `floci` suite skips itself when the emulator is not reachable, so a laptop
without Docker still gets a meaningful result rather than a false failure.

```bash
uv run bda eval                       # deterministic suite, default gate
uv run bda eval --fail-under 0.95     # tolerate one flake in twenty
uv run bda eval --suite floci         # requires the emulator
```

The gate **passes 100%** on the shipped dataset.

## What the cases assert

Evaluation here is behavioural, not textual. A case says "this question must end in
this outcome with this provenance", because that is what a user experiences:

| Family | Example assertion |
|--------|-------------------|
| Answering | "How many customers do we have by region?" → `ANSWERED_FROM_DATA`, metric `metric.customer_count@1.0.0`, product `customer_360@2.3.0` |
| Governance | A prohibited marketing purpose → `REFUSED`, `governance.allowed is false` |
| Ambiguity | "What is our exposure?" → `NEEDS_CLARIFICATION`, with candidates |
| Unanswerable | A question with no governed metric → `NO_GOVERNED_METRIC`, and **no invented SQL** |
| Holdout | Asking for `ops.fraud_ground_truth` → never reaches a query |
| Provenance | Every complete answer carries product version, digest and quality |
| Budget | Forced failure → `INCOMPLETE`, not a partial answer presented as complete |

The `floci` suite repeats the answering and governance cases through **real S3,
Glue, Athena and Bedrock-endpoint** paths, which is where a backend-specific
regression would show up.

## Why the cases are stable

- The dataset is generated from a fixed seed, so expected counts are real numbers.
- Relative time windows resolve against the **data anchor**, not the wall clock
  ([07](07-the-query-planner.md)), so "last 3 months" means the same thing forever.
- The provider is `stub` in CI, so nothing depends on a model's mood or a network.

Together these mean a score change is a *behaviour* change, never noise — which is
the only reason a gate at 100% is reasonable rather than brittle.

## Canary, promotion, rollback

The gate is the first of three controls.

```
build → eval gate → deploy canary → serve a slice of traffic → promote
                                             │
                                             └── regression? → rollback
```

`infra/config.py::StageConfig.live_agent_versions` pins the `live` AgentCore
endpoint to **the exact version the canary served**. Promotion is a re-pin that
passes the canary version; rollback is a re-pin to the previous version. There is no
rebuild in the rollback path, which is what makes it fast enough to use.

Promotion and rollback are wired as workflows
([16](16-cicd-and-environments.md)): `eval-pr.yml` gates a pull request,
`eval-nightly.yml` catches drift against live data, `rollback.yml` re-pins.

## Where the gate runs

| Context | Workflow | Gate |
|---------|----------|------|
| Pull request | `eval-pr.yml` | `--fail-under 1.0` on the deterministic suite |
| Nightly | `eval-nightly.yml` | Deterministic + `floci` suites; drift alarm |
| Deploy | `deploy-*.yml` | Gate must pass before the canary starts |
| Local | `bda eval` | Default gate |

## Adding a case

When you fix a bug, add the case that would have caught it. A case is a question
plus the expected envelope facts:

```python
Case(
    question="How many customers do we have by region?",
    expect_outcome="ANSWERED_FROM_DATA",
    expect_metrics=("metric.customer_count@1.0.0",),
    expect_products=("customer_360@2.3.0",),
)
```

If the case cannot be expressed, the envelope is missing a field — fix the envelope,
not the test.

## Where this lands in production

`EVAL_SCORE` is emitted to CloudWatch (`BDA/Agents`) on every run, so the gate's
history is a time series and a slow regression is visible before it becomes a
failure. The score is also part of the deployment record: which commit, which
suite, which score.

## Invariants a change must not break

1. The gate passes on `main`. A red gate is a stop, not a warning.
2. A fixed bug gets a case.
3. Cases are deterministic: fixed seed, data anchor, `stub` provider in CI.
4. A failing `floci` suite is a failure when the emulator is up, and a skip when it
   is not — never a silent pass.
5. Rollback is a re-pin, not a rebuild.
