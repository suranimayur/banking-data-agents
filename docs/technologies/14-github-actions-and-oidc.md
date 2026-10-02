# 14 — GitHub Actions and OIDC

**Level:** beginner → advanced · **You will be able to:** build a deploy pipeline
that stores no AWS credentials at all, and explain why environments are a security
control rather than a label.

## The idea

CI used to mean a Jenkins box someone administered. GitHub Actions moved it into the
repository: workflows are YAML, runners are ephemeral, and the whole thing is
versioned alongside the code it builds.

The deployment half has a harder problem. To deploy to AWS, CI needs AWS
credentials — and the traditional answer, a long-lived access key in repository
secrets, is a permanent liability. It never expires, it cannot be scoped to one
workflow, and a leaked fork or a compromised action exposes it forever.

**OIDC** removes the secret entirely. GitHub mints a short-lived, signed token for
each job that says *"this is repo X, branch Y, environment Z"*. AWS IAM is configured
to trust GitHub's issuer and to exchange that token for temporary credentials —
**scoped by the claims**. There is no stored key to leak, and the trust policy can
require the job to be on `main` in a protected environment.

## The mental model

```
    ┌── long-lived key ──────────────────────────────┐
    │  secret in GitHub  ──► never expires           │
    │  one leak = permanent access                   │
    └────────────────────────────────────────────────┘

    ┌── OIDC federation ─────────────────────────────┐
    │  GitHub mints a 1-hour token for THIS job      │
    │  AWS trusts the issuer, checks the claims      │
    │  → temporary credentials, scoped to one role   │
    │  nothing is stored anywhere                    │
    └────────────────────────────────────────────────┘
```

The claims are the security boundary, and they are the part to get right:

```yaml
permissions:
  id-token: write      # required: lets the job request an OIDC token
  contents: read
```

```json
"Condition": {
  "StringEquals": {
    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
  },
  "StringLike": {
    "token.actions.githubusercontent.com:sub": "repo:OWNER/REPO:environment:prod"
  }
}
```

`id-token: write` is the piece people miss, and the `sub` claim is the piece that
makes production safe: without it, **any** branch — including a fork's pull request —
could assume the deploy role.

## The minimum you need

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --locked
      - run: uv run pytest -q
```

## In this project

Eleven workflows in `.github/workflows/`, each with a narrow job:

| Workflow | Job | Gate |
|----------|-----|------|
| `ci.yml` | lint, types, tests | every push and PR |
| `eval-pr.yml` | run the evaluation suite on a PR | fails on regression |
| `eval-nightly.yml` | scheduled eval against the baseline | catches model/data drift |
| `data-pipeline.yml` | run the medallion pipeline | on merge / schedule |
| `security.yml` | dependency and secret scanning | every PR |
| `drift.yml` | detect infrastructure drift | scheduled |
| `deploy.yml` | reusable deploy workflow | called by the three below |
| `deploy-dev.yml` | auto-deploy to dev | on merge |
| `deploy-staging.yml` | deploy to staging | with approval |
| `deploy-prod.yml` | deploy to prod | **environment-protected** |
| `rollback.yml` | re-pin to a previous version | manual, operator-triggered |

Four properties are worth copying:

1. **`uv sync --locked`.** A stale lockfile fails the build rather than being
   silently regenerated ([chapter 01](01-python-toolchain-and-uv.md) discussed why).
2. **A reusable workflow.** `deploy.yml` holds the deploy logic and the three
   environment workflows call it. One implementation, three triggers — instead of
   three copies that drift.
3. **`rollback.yml` is a first-class workflow,** not a runbook paragraph. The
   mechanism (re-pinning the AgentCore endpoint version) is the one described in
   [chapter 13](13-infrastructure-as-code-with-cdk.md), because a cheap rollback is
   the only kind that gets used.
4. **CI runs `bda eval`,** so the release gate is enforced by the same artifact
   humans run locally. `bda ask` exits non-zero on an incomplete answer, so "the
   agent shrugged" fails a build instead of producing a green run and a silent
   degradation.

`main` is protected by `.github/CODEOWNERS`: a change to `infra/`, `contracts/` or
`tools/sqlguard.py` requires review from the owning team. Code ownership is a
control, not a courtesy.

## Level up

**Pin actions to a commit SHA, not a tag.** A tag is mutable; a compromised action
at a moving tag is how supply-chain attacks reach your deploy role. GitHub's own
hardening guidance says the same, and `dependabot.yml` can still bump the SHAs.

**Prefer `permissions: {}` at the top and grant per job.** The default `GITHUB_TOKEN`
is broader than most jobs need.

**Concurrency groups** prevent two deploys racing:
`concurrency: { group: deploy-prod, cancel-in-progress: false }`.

**Environments are the approval mechanism.** A GitHub *environment* with required
reviewers and a branch restriction turns "please be careful" into an enforced
checkpoint, and it appears in the OIDC `sub` claim so AWS can verify it too.

**What to learn next:** the difference between `sts:AssumeRoleWithWebIdentity` and
`AssumeRole`; why the deploy role should have a **permissions boundary** (this repo's
bootstrap stack creates one); artifact attestation and SLSA provenance; and
`workflow_run` for chaining without duplicating configuration.

## Try it

```bash
cd banking-data-agents
ls .github/workflows/
grep -l "id-token" .github/workflows/*.yml          # which jobs can assume AWS roles
grep -n "environment:" .github/workflows/*.yml      # where approval is required

# Validate YAML parses (no network needed).
uv run python -c "
import glob, yaml
for f in sorted(glob.glob('.github/workflows/*.yml')):
    yaml.safe_load(open(f, encoding='utf-8')); print('ok', f)
"
```
