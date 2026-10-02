# 01 · Architecture

## The one-paragraph version

Banking analysts ask questions in English; the platform answers them from
**governed data products** and returns a number *plus the evidence that makes it
trustworthy*. The model never writes free-form SQL over the warehouse. It chooses
from ten tools, and the one that produces SQL composes a query from **versioned
metric fragments** whose status in production is known. Everything an agent can
reach sits behind a contract, a data-quality result, a lineage record and an
access policy. The agents run on **Strands** against **Amazon Bedrock**, and the
same code runs against a deterministic stub, the local Floci emulator, or real
Bedrock.

## The layers

```
                         ┌─────────────────────────────────────────────┐
  consumers              │  bda ask · FastAPI /ask · Streamlit console │
                         └───────────────────────┬─────────────────────┘
                                                 │  evidence envelope
                         ┌───────────────────────▼─────────────────────┐
  agent runtime          │  agents/  copilot · fraud · credit          │
                         │  Strands Agent + BedrockModel (or stub)     │
                         └───────────────────────┬─────────────────────┘
                                                 │  tool calls (JSON in, JSON out)
                         ┌───────────────────────▼─────────────────────┐
  tools + guardrails     │  tools/  10 tools · sqlguard · ToolContext  │
                         └───────────────────────┬─────────────────────┘
                                                 │  one sanctioned query
      ┌──────────────────────────────────────────▼──────────────────────┐
  semantic + catalog     │  semantic/planner · metrics  ·  catalog/     │
      └──────────────────────────────────────────┬──────────────────────┘
                                                 │
      ┌──────────────────────────────────────────▼──────────────────────┐
  lake                   │  gold · silver · bronze · ops  (DuckDB/S3+Athena)│
      └──────────────────────────────────────────┬──────────────────────┘
                                                 │
      ┌──────────────────────────────────────────▼──────────────────────┐
  data                   │  datagen/  →  pipeline/  →  contracts/          │
      └─────────────────────────────────────────────────────────────────┘
```

## The three decisions everything else follows from

### 1. Agents do not write SQL

A language model that writes SQL over a warehouse is unbounded in exactly the way
an audit function cannot tolerate. So the tool surface is closed:

```
search_catalog · list_products · get_contract · resolve_metric ·
check_allowed_use · generate_sql · validate_sql · execute_sql ·
explain_quality · trace_lineage
```

The model picks a metric *by name*; `generate_sql` expands it into a query from
`semantic/metrics/*.yaml`. A figure can therefore be traced to
`metric.customer_count@1.0.0`, an owner, and a definition — which is the property
that makes a number arguable instead of mysterious.

`tools/registry.py::TOOL_NAMES` is the list; the governing rule is that a metric
is selected and a template is filled, never a formula invented.

### 2. Every answer carries evidence

`agents/evidence.py` assembles an envelope containing the outcome, the SQL, the
product and contract versions, the metrics used, the row count and preview, the
data-quality state of the tables touched, the lineage path, the governance
decision, the cost, and a trace of every tool call. The outcome is an enum —
`ANSWERED_FROM_DATA`, `ANSWERED_FROM_METADATA`, `NEEDS_CLARIFICATION`,
`NO_GOVERNED_METRIC`, `REFUSED`, `INCOMPLETE` — so a caller can act on it, and
`is_complete` is what CI keys on.

See [10 The evidence envelope](10-the-evidence-envelope.md).

### 3. Enforce, do not instruct

A prompt saying "never read the fraud holdout" is a wish. `tools/sqlguard.py`
refuses to execute SQL that mentions `ops.fraud_ground_truth`, that reads outside
`gold`/`ops`, that is not a single `SELECT`, that uses `SELECT *`, or that has no
`LIMIT` under a 1000-row ceiling. The agents' prompts still say it, because
defence in depth is free; the guard is what is tested.

See [08 SQL guardrails](08-sql-guardrails.md).

## What is local, what is real

The platform has one switch, `BDA_LAKE_BACKEND` and `AWS_ENDPOINT_URL`, and two
implementations behind each seam:

| Seam | Local / CI | Floci or AWS |
|------|-----------|--------------|
| Object storage | `LocalLake` (Parquet under `data/lake/`) | `S3Lake` (`bda-bronze`, …) |
| Query engine | `DuckDBEngine` | `AthenaEngine` (Glue catalog) |
| LLM | `StubModel` (deterministic) | `BedrockModel` |
| Answer retention | in-process LRU | DynamoDB `bda-<env>-answers` |

`pipeline/lake.py::get_lake` and `pipeline/engine.py::get_engine` are the only
places that decide. Nothing above them branches on the backend, which is why the
same tests run in both worlds.

## Verified end to end

```
uv run bda data generate      # 1,220,122 rows across 108 datasets, ~2.9 s
uv run bda pipeline run       # bronze → silver → gold → publish, ~9.5 s
uv run bda catalog list       # 3 products, 28 metrics
uv run bda ask "How many customers do we have by region?"   # ANSWERED_FROM_DATA
uv run bda eval               # gate passes 100%
```

## Where this lands in production

- Eleven CDK stacks per environment ([15 Infrastructure](15-infrastructure.md)).
- Agents run as a container behind an API Gateway ([12](12-serving-api.md), [15](15-infrastructure.md)).
- Promotion is a canary followed by a pinned `live` version; rollback is re-pinning
  ([11](11-evaluation-and-release-gates.md)).

## Invariants a change must not break

1. Every tool result is JSON-serialisable (`tools/impl.py::_jsonable`), or Strands
   silently degrades to `str()` and the model cannot read it.
2. `ops.fraud_ground_truth` is agent-unreachable.
3. Every executed query passed `validate_sql`.
4. Every answer carries a `trace_id` and a `contract_digest`.
5. Nothing is emitted to a dashboard that no process emits (see [14](14-observability.md)).
