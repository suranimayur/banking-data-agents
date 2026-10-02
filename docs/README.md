# The Banking Data Agents playbook

A working platform, and the reasoning behind every part of it. The code is the
source of truth; these chapters exist because *why* a system is shaped the way it
is decays faster than the shape itself, and a repository that only shows the shape
leaves the next person to guess.

Read it in one of three ways.

**The 20-minute tour.** [01 Architecture](01-architecture.md) →
[04 The lake and the medallion](04-the-lake-and-medallion.md) →
[09 Agents and Bedrock](09-agents-and-bedrock.md) →
[10 The evidence envelope](10-the-evidence-envelope.md). That is the whole idea:
governed data products, a planner that only composes sanctioned aggregates, and an
answer that can be audited.

**The "I have to run this" path.** [02 Getting started](02-getting-started.md) →
[03 Synthetic data](03-synthetic-data.md) → the `Makefile` targets, then
[18 Operations runbook](18-operations-runbook.md) when something is on fire and
[19 Troubleshooting](19-troubleshooting.md) when it will not start.

**The "I have to change this" path.** Every chapter ends with a *Where this lands
in production* section and the invariants a change must not break. Start with
[05 Data contracts](05-data-contracts.md) and [08 SQL guardrails](08-sql-guardrails.md)
if you are touching anything an agent can reach, because those two chapters hold
the rules that everything else is built to respect.

**The "I have not used this technology" path.** The
[technologies track](technologies/README.md) teaches every tool in the stack from
first principles to production — Parquet, DuckDB, SQLGlot, boto3, Strands, Bedrock,
Pydantic, FastAPI, Streamlit, CDK, GitHub Actions, pytest and more. Read the playbook
for *why the system is shaped this way*; read the technologies track when a specific
instrument is unfamiliar.

---

## Chapters

| # | Chapter | The one-line reason it exists |
|---|---------|-------------------------------|
| 01 | [Architecture](01-architecture.md) | The layers, the boundaries, and the three decisions that shape everything |
| 02 | [Getting started](02-getting-started.md) | Toolchain, environment, and the first end-to-end run |
| 03 | [Synthetic data](03-synthetic-data.md) | A bank-shaped dataset with the incidents deliberately baked in |
| 04 | [The lake and the medallion](04-the-lake-and-medallion.md) | Bronze → silver → gold → publish, and why each boundary is a test |
| 05 | [Data contracts](05-data-contracts.md) | A product is a promise, not a table |
| 06 | [The semantic layer](06-semantic-layer.md) | Versioned metric fragments: the only aggregates an agent may use |
| 07 | [The query planner](07-the-query-planner.md) | Turning language into one deterministic query, not a guess |
| 08 | [SQL guardrails](08-sql-guardrails.md) | Enforcement over instruction, at the last possible moment |
| 09 | [Agents and Bedrock](09-agents-and-bedrock.md) | Strands, tool surfaces, model tiers and provider abstraction |
| 10 | [The evidence envelope](10-the-evidence-envelope.md) | What an answer has to carry to be trustworthy |
| 11 | [Evaluation and release gates](11-evaluation-and-release-gates.md) | Nothing ships unmeasured; canary and rollback |
| 12 | [The serving API](12-serving-api.md) | HTTP transport that loses and invents nothing |
| 13 | [The analyst console](13-analyst-console.md) | A UI that never re-derives truth |
| 14 | [Observability](14-observability.md) | Metrics that exist because someone emits them |
| 15 | [Infrastructure](15-infrastructure.md) | Eleven stacks, three environments, one naming rule |
| 16 | [CI/CD and environments](16-cicd-and-environments.md) | The eleven workflows, and what each one is allowed to do |
| 17 | [Security and compliance](17-security-and-compliance.md) | Identity, guardrails, retention, and what the platform refuses |
| 18 | [Operations runbook](18-operations-runbook.md) | Recovering a pipeline, an agent, a bad answer |
| 19 | [Troubleshooting](19-troubleshooting.md) | The failures that actually happen, in the order they happen |

## The technologies track

[`docs/technologies/`](technologies/README.md) is a separate, self-contained learning
path: **one chapter per technology, explained from zero to hero**, each with the same
five sections (The idea · The mental model · The minimum you need · In this project ·
Level up) and a runnable exercise at the end.

| Stage | Chapters |
|-------|----------|
| Foundations | [`uv` & Python toolchain](technologies/01-python-toolchain-and-uv.md) · [Polars, Arrow, Parquet](technologies/02-columnar-data-polars-arrow-parquet.md) |
| Data engine | [DuckDB](technologies/03-duckdb.md) · [SQLGlot & the guardrail](technologies/04-sqlglot-and-the-sql-guardrail.md) |
| Cloud | [AWS & `boto3`](technologies/05-aws-foundations-and-boto3.md) · [the AWS analytics stack](technologies/06-the-aws-analytics-stack.md) |
| Intelligence | [LLM agents & Strands](technologies/07-llm-agents-and-strands.md) · [Bedrock & AgentCore](technologies/08-amazon-bedrock-and-agentcore.md) |
| Surfaces | [Pydantic](technologies/09-pydantic-and-configuration.md) · [FastAPI & ASGI](technologies/10-fastapi-and-asgi.md) · [Streamlit](technologies/11-streamlit.md) · [terminal UX, logging & resilience](technologies/12-terminal-ux-logging-and-resilience.md) |
| Running it | [CDK](technologies/13-infrastructure-as-code-with-cdk.md) · [Actions & OIDC](technologies/14-github-actions-and-oidc.md) · [pytest & `moto`](technologies/15-testing-with-pytest-and-moto.md) · [lint, types & gates](technologies/16-lint-typecheck-and-quality-gates.md) · [observability](technologies/17-observability-with-emf.md) |
| Capstone | [From zero to hero](technologies/18-from-zero-to-hero.md) |

## Conventions used here

- `bda` is the CLI; `uv run bda <command>` is the canonical invocation.
- Local runs use the **local lake** (DuckDB over Parquet) unless the Floci emulator
  is up, in which case S3 + Glue + Athena are used and nothing else changes.
- Code citations are given as `path::symbol` so they survive line-number drift.
- "Verified" means the number or behaviour was produced by running the command in
  this repository, not estimated.
