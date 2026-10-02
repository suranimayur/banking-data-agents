# 16 · CI/CD and environments

## Concept

Eleven workflows, each with one job and no surprises. The design rule is that a
workflow may do only what its name says, so that reading the Actions tab tells you
what a repository can do to production without opening a single YAML file.

```
.github/workflows/
  ci.yml               lint, type-check, test on every push and PR
  eval-pr.yml          the evaluation gate on a pull request
  eval-nightly.yml     the full suite against the dataset, every night
  deploy.yml           the reusable deploy (canary → watch → promote)
  deploy-dev.yml       automatic on main
  deploy-staging.yml   manual, promotes what dev verified
  deploy-prod.yml      manual, by immutable tag, with approval
  rollback.yml         re-pin the live endpoints to the previous version
  data-pipeline.yml    deploy and optionally run the medallion pipeline
  security.yml         dependency and secret scanning
  drift.yml            daily check for infrastructure drift
```

## Environments

| Environment | Purpose | Deploy trigger | Approval |
|-------------|---------|----------------|----------|
| `dev` | Every merge | automatic on `main` | none |
| `staging` | Pre-production verification | manual, picks up dev's tag | none |
| `prod` | Customers | manual, by immutable SHA | required |

Names, sizes and retention come from `StageConfig`; physical names from
`qualify()` ([15](15-infrastructure.md)). The environments differ in
**configuration only** — there is no `if env == "prod"` in application code, which
is the property that makes staging verification mean something.

## The gates, in order

```
push ──► ci.yml ──────────── lint · types · tests ──┐
                                                    ├──► eval-pr.yml ──► review ──► merge
                                                    │        (score must be 1.0)
             merge ──► deploy-dev.yml ──► deploy.yml (canary)
                                             │ watch alarms
                                             ▼
                                        promote (pin dev live)
                                             │
                        deploy-staging.yml (manual, same tag)
                                             │
                        deploy-prod.yml (manual, SHA + versions + ticket)
```

Each arrow is a gate. Nothing moves forward because time passed; it moves because a
named check passed.

## `deploy.yml` as a reusable workflow

The three environment workflows all call `deploy.yml`, which takes the environment,
the image tag, whether to pin, the versions to pin and the alarm-watch duration. It
returns the deployed tag. Rollback and promotion therefore use **the same** deploy
path as a normal release, which means the path that gets tested is the path that gets
used in an incident.

- `prod` requires an immutable `--image-tag` (a commit SHA). `latest` is refused.
- `deploy-prod.yml` requires the live versions and a change-ticket reference, and
  records both in the run summary.
- The canary window is a real watch on the alarms from
  [14](14-observability.md), not a sleep.

## Rollback

`rollback.yml` re-pins the `live` AgentCore endpoints to the previous versions,
using `StageConfig.live_agent_versions`. There is no rebuild and no image promotion
in the rollback path — that is the entire point of pinning versions rather than
moving a tag.

## Data pipeline

`data-pipeline.yml` deploys the pipeline stacks and can start a medallion run for an
environment. It is `workflow_dispatch` first, with a push trigger scoped to the
pipeline code, because "someone edited a transform" is not the same event as
"someone edited the API".

## Drift and security

- `drift.yml` runs daily and files an issue when the deployed infrastructure differs
  from the templates. Drift is normal; drift nobody notices is not.
- `security.yml` scans dependencies and secrets.
- `.github/dependabot.yml` keeps the dependency graph moving deliberately rather
  than in a panic.
- `.github/CODEOWNERS` makes `contracts/`, `semantic/metrics/`, `tools/sqlguard.py`
  and `infra/` require the right reviewers — the files where a mistake is a
  governance failure rather than a bug.

## Running the gates locally

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run bda eval
uv run bda infra synth --env dev
```

If all five pass, CI will almost certainly pass; the workflows are wrappers over
these commands, not a parallel implementation.

## Where this lands in production

OIDC deploy roles from `infra/bootstrap` mean no long-lived AWS keys in the
repository. The deploy job assumes a role, deploys, watches alarms and either
promotes or stops.

## Invariants a change must not break

1. A workflow does only what its name says.
2. `prod` deploys by immutable tag only.
3. Promotion and rollback use the reusable deploy path.
4. The local commands and the CI jobs stay in step; a gate that only exists in CI is
   a gate nobody can run.
5. A new governed file (a contract, a metric, a guard rule) is covered by
   `CODEOWNERS`.
