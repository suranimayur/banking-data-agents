# Tools and technologies — zero to hero

Every tool this project uses, explained from first principles to production. No
chapter assumes you have used the technology before; every chapter ends with
something you can run.

This track is different from the [main playbook](../README.md). The playbook
explains **the system** — why a metric is versioned, why the guardrail sits where it
does. This track explains **the instruments** — what Parquet actually is on disk,
what a token really costs, why an ASGI server exists at all.

You can read them in either order. If you are here to hire-and-onboard onto this
repository, read the playbook first and use this track as the reference when a
technology is unfamiliar.

---

## How to read this

Every chapter has the same five sections:

| Section | What it answers |
|---------|-----------------|
| **The idea** | What problem existed before this tool |
| **The mental model** | The one picture that makes the rest obvious |
| **The minimum you need** | Enough to be productive in five minutes |
| **In this project** | The exact files and decisions, so the knowledge is anchored |
| **Level up** | The parts that bite in production, and what to learn next |

Each chapter is graded from **beginner** through **advanced** inside itself, rather
than being sorted by difficulty, because the useful learning unit is one technology.

---

## The learning path

### Stage 0 — Foundations: the language and the machine

Nothing here is about AI or banking. It is about getting a reproducible environment,
which is the thing every later claim depends on.

| # | Chapter | Level | You can answer afterwards |
|---|---------|-------|---------------------------|
| 01 | [Python toolchain and `uv`](01-python-toolchain-and-uv.md) | beginner → advanced | Why does CI install 372 tests' worth of dependencies in seconds and never drift? |
| 02 | [Columnar data: Polars, Arrow, Parquet](02-columnar-data-polars-arrow-parquet.md) | beginner → advanced | Why is a 1.08M-row aggregation fast, and why does Parquet make it fast? |

### Stage 1 — The data engine

| # | Chapter | Level | You can answer afterwards |
|---|---------|-------|---------------------------|
| 03 | [DuckDB — the in-process OLAP engine](03-duckdb.md) | beginner → advanced | How can the same code run on a laptop and on Athena without a rewrite? |
| 04 | [SQLGlot and the SQL guardrail](04-sqlglot-and-the-sql-guardrail.md) | intermediate → advanced | Why is parsing SQL into a tree safer than a regex denylist? |

### Stage 2 — The cloud it deploys to

| # | Chapter | Level | You can answer afterwards |
|---|---------|-------|---------------------------|
| 05 | [AWS foundations and `boto3`](05-aws-foundations-and-boto3.md) | beginner → advanced | What is a region, a signature, an endpoint override — and how do we test AWS with no AWS? |
| 06 | [The AWS analytics stack](06-the-aws-analytics-stack.md) | intermediate → advanced | What do S3, Glue, Athena, Lake Formation, DynamoDB and KMS each actually *do*? |

### Stage 3 — The intelligence

| # | Chapter | Level | You can answer afterwards |
|---|---------|-------|---------------------------|
| 07 | [LLM agents and Strands](07-llm-agents-and-strands.md) | beginner → advanced | What is a tool call, and why does the guardrail sit below the model? |
| 08 | [Amazon Bedrock and AgentCore](08-amazon-bedrock-and-agentcore.md) | beginner → advanced | Model tiers, guardrails, and where the agent actually runs. |

### Stage 4 — The surfaces people touch

| # | Chapter | Level | You can answer afterwards |
|---|---------|-------|---------------------------|
| 09 | [Pydantic and configuration](09-pydantic-and-configuration.md) | beginner → advanced | How does a typo in `.env` fail at startup instead of at 3 a.m.? |
| 10 | [FastAPI and ASGI](10-fastapi-and-asgi.md) | beginner → advanced | What is ASGI, and why does concurrency shape this service? |
| 11 | [Streamlit](11-streamlit.md) | beginner → intermediate | Why does the console never compute its own numbers? |
| 12 | [Terminal UX, logging and resilience](12-terminal-ux-logging-and-resilience.md) | intermediate → advanced | Rich, structlog, tenacity and httpx — and why diagnostics must never touch stdout. |

### Stage 5 — Running it for real

| # | Chapter | Level | You can answer afterwards |
|---|---------|-------|---------------------------|
| 13 | [Infrastructure as code with CDK](13-infrastructure-as-code-with-cdk.md) | beginner → advanced | How do eleven stacks stay consistent across three environments? |
| 14 | [GitHub Actions and OIDC](14-github-actions-and-oidc.md) | beginner → advanced | How do you deploy to AWS without storing a single AWS key? |
| 15 | [Testing with pytest and `moto`](15-testing-with-pytest-and-moto.md) | beginner → advanced | How do you test cloud behaviour offline and deterministically? |
| 16 | [Lint, types and quality gates](16-lint-typecheck-and-quality-gates.md) | intermediate → advanced | Why do lint rules belong in review, not in a formatting argument? |
| 17 | [Observability with EMF](17-observability-with-emf.md) | intermediate → advanced | How does a dashboard avoid being fictional? |

### Stage 6 — Hero

| # | Chapter | Level | You can answer afterwards |
|---|---------|-------|---------------------------|
| 18 | [From zero to hero](18-from-zero-to-hero.md) | capstone | How does all of it compose — and how would you rebuild it? |

---

## The whole stack at a glance

| Layer | Technology | Version | Introduced in |
|-------|-----------|---------|---------------|
| Runtime | CPython | 3.12.12 (via `uv`) | [01](01-python-toolchain-and-uv.md) |
| Environment | `uv` | 0.12+ | [01](01-python-toolchain-and-uv.md) |
| Dataframes | Polars | 1.44 | [02](02-columnar-data-polars-arrow-parquet.md) |
| Memory format | Apache Arrow | 25.0 | [02](02-columnar-data-polars-arrow-parquet.md) |
| File format | Apache Parquet | — | [02](02-columnar-data-polars-arrow-parquet.md) |
| Query engine | DuckDB | 1.5 | [03](03-duckdb.md) |
| SQL parsing | SQLGlot | 30.20 | [04](04-sqlglot-and-the-sql-guardrail.md) |
| AWS SDK | boto3 / botocore | 1.43 / — | [05](05-aws-foundations-and-boto3.md) |
| Storage | Amazon S3 | — | [06](06-the-aws-analytics-stack.md) |
| Catalog | AWS Glue Data Catalog | — | [06](06-the-aws-analytics-stack.md) |
| Query (cloud) | Amazon Athena | — | [06](06-the-aws-analytics-stack.md) |
| Governance | AWS Lake Formation | — | [06](06-the-aws-analytics-stack.md) |
| Retention | Amazon DynamoDB | — | [06](06-the-aws-analytics-stack.md) |
| Encryption | AWS KMS | — | [06](06-the-aws-analytics-stack.md) |
| Agents | Strands Agents SDK | 1.57 | [07](07-llm-agents-and-strands.md) |
| Models | Amazon Bedrock | — | [08](08-amazon-bedrock-and-agentcore.md) |
| Agent runtime | Bedrock AgentCore | — | [08](08-amazon-bedrock-and-agentcore.md) |
| Validation | Pydantic / pydantic-settings | 2.13 / 2.15 | [09](09-pydantic-and-configuration.md) |
| API | FastAPI + Uvicorn | 0.141 / 0.54 | [10](10-fastapi-and-asgi.md) |
| Console | Streamlit | 1.64 | [11](11-streamlit.md) |
| Terminal | Rich | 15.0 | [12](12-terminal-ux-logging-and-resilience.md) |
| Logging | structlog | 26.1 | [12](12-terminal-ux-logging-and-resilience.md) |
| Retries | Tenacity | 9.1 | [12](12-terminal-ux-logging-and-resilience.md) |
| HTTP client | HTTPX | 0.28 | [12](12-terminal-ux-logging-and-resilience.md) |
| IaC | AWS CDK (Python) | 2.271 | [13](13-infrastructure-as-code-with-cdk.md) |
| CI/CD | GitHub Actions | — | [14](14-github-actions-and-oidc.md) |
| Testing | pytest + `moto` | 9.1 / 5.2 | [15](15-testing-with-pytest-and-moto.md) |
| Lint / types | Ruff / mypy | 0.16 / 2.3 | [16](16-lint-typecheck-and-quality-gates.md) |
| Metrics | CloudWatch EMF | — | [17](17-observability-with-emf.md) |
| Local AWS | Floci | 0.2.1 | [05](05-aws-foundations-and-boto3.md) |

---

## Conventions used in this track

- **Versions are the ones this repository actually resolved**, printed by
  `uv run python -c "import importlib.metadata as m; print(m.version('...'))"`. They
  are pinned loosely in `pyproject.toml` and locked exactly in `uv.lock`; where the
  two differ, the lock is the truth.
- Code is cited as `path::symbol` so it survives line drift.
- "Verified" means the number came from running the command in this repository.
- Where a technology is **not** exercised in this environment, the chapter says so
  rather than implying it was tested. The Docker-dependent paths (Floci, and
  therefore S3/Glue/Athena end to end) are the honest gap, and
  [05](05-aws-foundations-and-boto3.md) explains what still holds without them.
