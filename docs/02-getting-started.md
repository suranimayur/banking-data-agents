# 02 · Getting started

## What you need

| Tool | Version used here | Why |
|------|-------------------|-----|
| `uv` | 0.12.7 | Provisions CPython 3.12 and the lockfile; do not use a system Python |
| Docker | 29.7 with the **daemon running** | Only for the Floci emulator; the platform runs without it |
| `floci` CLI | 0.2.1 | Optional convenience wrapper around the emulator |
| `git` | 2.52 | |
| Node | 24.x | Only for the CDK app ([15](15-infrastructure.md)) |

On Windows, `uv` provisions CPython 3.12.12 even when `python` is 3.14, which is
the version this repository targets. Always go through `uv run`.

Everything below runs in `bash` (Git Bash on Windows). Prefix long-running
commands with `PYTHONIOENCODING=utf-8` so Unicode in answers does not crash the
console.

## First run

```bash
cd banking-data-agents

make bootstrap            # writes .env, syncs deps, verifies the toolchain
uv run bda data generate  # synthetic source data
uv run bda pipeline run   # bronze → silver → gold → publish
uv run bda catalog list   # 3 products, 28 metrics
uv run bda ask "How many customers do we have by region?" --no-sql
uv run bda eval           # the release gate
```

`bda demo` does all of the above in one command and prints what it is doing:

```bash
uv run bda demo
```

## The CLI

```
bda bootstrap [--check-only]      prepare and verify a working environment
bda floci {up|down|status}        control the local AWS emulator
bda data generate [--seed N]      write synthetic source data
bda pipeline {run|dq} [--stages …] bronze → silver → gold → publish (+ dq)
bda catalog [list|PRODUCT]        inspect published data products
bda ask "…" [--no-sql] [--json]   ask the Copilot
bda answers {get TRACE|list} [--json] read back a retained answer
bda eval [--suite …] [--fail-under F]
bda serve [--host] [--port]       FastAPI service
bda ui [--port]                   Streamlit analyst console
bda infra {synth|deploy|destroy} [--env …] [--yes] [--image-tag …]
bda demo [--skip-data]            end-to-end demonstration
bda clean                         remove caches and generated artefacts
```

`bda ask` exits non-zero when the agent produced an *incomplete* answer, which is
what lets CI treat "the agent shrugged" as a failure rather than a log line.

## Environment variables

Settings are read from `.env` (or `BDA_ENV_FILE`). The ones that matter day one:

```bash
ENV=local                      # local | dev | staging | prod  (no BDA_ prefix)
LLM_PROVIDER=stub              # stub | floci | bedrock
BDA_LAKE_BACKEND=auto          # auto | local | s3
AWS_ENDPOINT_URL=              # http://localhost:4566 for Floci; empty for AWS
BDA_ANSWER_TABLE=              # set to retain answers to DynamoDB
```

`BDA_ENV_FILE=.env.test-nonexistent` is how the test suite guarantees it never
reads your local `.env`.

Field names map case-insensitively onto variables, so `bda_gold_bucket` is read
from `BDA_GOLD_BUCKET` and `bda_lake_backend` from `BDA_LAKE_BACKEND`. The `env`
field is the exception: it is read from `ENV`.

## Running with Floci, and without Docker

**Without Docker (default).** `BDA_LAKE_BACKEND=auto` on `ENV=local` probes the
emulator; if nothing answers it falls back to the filesystem and logs
`lake_fallback_local` at warning level. Queries run on DuckDB. This is the
supported development path and the one CI uses.

**With Floci.**

```bash
make floci-up                      # docker compose up
uv run bda floci status            # health + provisioned resources
export AWS_ENDPOINT_URL=http://localhost:4566
uv run bda pipeline run            # now writes to S3 and registers Glue tables
```

Floci listens on `4566` and reports health at `/_floci/health`. It emulates S3,
DynamoDB, Glue, Athena (a DuckDB sidecar), Lake Formation, Step Functions, Lambda,
Kinesis, Cognito, KMS, Secrets Manager, CloudWatch, Bedrock, Bedrock AgentCore,
S3 Vectors, OpenSearch and ECR.

## The Makefile

`make help` lists everything. The targets you will actually type:

```
bootstrap floci-up floci-down floci-status data pipeline dq catalog ask
fmt lint typecheck test test-all eval api ui synth deploy destroy demo clean
```

Each delegates to `bda`, so CI never depends on `make` being installed.

## Verifying the toolchain

```bash
uv run pytest -q                       # unit + integration + smoke
uv run pytest -m smoke -q              # post-deploy journeys only
uv run ruff check .                    # lint
uv run mypy src                        # types (infra is type-checked too)
uv run bda infra synth --env dev       # CDK synth, no AWS call
```

## Where this lands in production

There is no `.env` in a deployed environment. Settings come from the task
definition's environment and secrets from Secrets Manager; `ENV` is set per stack
and drives every physical name via `infra/config.py::qualify`
(`bda-<env>-<name>`). See [15](15-infrastructure.md) and
[16](16-cicd-and-environments.md).

## Troubleshooting the first run

| Symptom | Cause | Fix |
|---------|-------|-----|
| `endpoint_reachable` false, `lake_fallback_local` warning | Docker daemon down | Start Docker Desktop, or carry on — the local path is fine |
| `FileNotFoundError: gold.customer_360` | Pipeline never ran | `uv run bda data generate && uv run bda pipeline run` |
| `ModuleNotFoundError: pyparsing` in a Glue test | Stale `.venv` | `uv sync` |
| Unicode console errors on Windows | Missing encoding | Prefix `PYTHONIOENCODING=utf-8` |
| `Cross stack references are only supported …` when synthesising | A stack not given `env=` | See [15](15-infrastructure.md) |
