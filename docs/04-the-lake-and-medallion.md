# 04 · The lake and the medallion

## Concept

A medallion architecture is four zones with four different promises:

| Zone | Promise | Who reads it |
|------|---------|--------------|
| **bronze** | Every arriving value, verbatim, with its source and batch | Engineers, debugging |
| **silver** | One row per business entity, conformed and deduplicated | Engineers, quality rules |
| **gold** | Published data products, shaped for consumption | Agents, analysts |
| **ops** | Quality results, lineage, and the fraud holdout | The platform; **not** the agents |

The boundaries are not decoration. Bronze may be wrong (it is a faithful copy of
something wrong). Silver must be internally consistent. Gold must be *defensible*,
because an agent will read it aloud to an analyst. Ops is where the
self-knowledge of the platform lives.

## Why four zones instead of a warehouse and a hope

Each boundary is a place to put a test. Bronze→silver is where deduplication and
referential integrity are checked. Silver→gold is where a contract is enforced.
Gold→publish is where quality, freshness and lineage are attached. A single-hop
pipeline has one place to be right and no place to be wrong.

## The code

```
pipeline/
  lake.py      LocalLake | S3Lake, normalise_frames, ensure_buckets, ensure_glue_databases
  engine.py    DuckDBEngine | AthenaEngine, get_engine
  schema.py    typed schemas per table
  runner.py    the four stages + `dq`
  dq.py        31 data-quality rules
  publish.py   contracts, metrics, quality, lineage, fingerprint
  sql/         the 10 transformation SQL files
```

`pipeline/runner.py::ALL_STAGES = ("bronze", "silver", "gold", "publish")`, with
`dq` runnable on its own (`bda pipeline dq`) and also as part of `publish`.

### Stage by stage

**bronze** — copies each landing batch into the bronze zone, flattened and
schema-normalised. `normalise_frames` gives every batch of one source the same
column set by widening absent columns to typed NULLs. Without it, the additive CRM
change in month 7 means the column exists in the table but not in every file, and
any SQL referencing it fails on short history windows — which is exactly what
happens when you regenerate with `SYNTHETIC_MONTHS=4`.

**silver** — the ten SQL files in `pipeline/sql/` conform, deduplicate and join.
The interesting ones are the "current row" selections: two systems disagree about a
customer, and silver has to pick one deterministically.

**gold** — builds the three published products from silver:

- `customer_360` — one row per customer with balances, holdings, risk and a
  conformed `region`/`state_code` pair.
- `transaction` — the analytical transaction fact, 1:1 with silver transactions.
- `credit_risk` — one row per customer with obligations, DTI and outstanding.

**publish** — reads the contracts and metrics, runs the quality rules, records
lineage edges, computes a fingerprint, and writes the catalog. Verified: **20
lineage edges** in `ops.lineage_edges`, **30 PASS + 1 WARNING**, **28 metrics**,
**3 contracts**.

## Lineage

`publish.py::trace()` returns dicts with keys `dataset`, `transform_id`,
`direction` and `level`. The `level` is a hop distance and `transform_id` names the
transform; `tools/impl.py` maps them to `depth` and `via` on the way to the agent
so the tool's public shape is stable even if the internal record grows. If you are
changing the lineage record, change the mapping with it.

## Quality

31 rules across the zones, run with `bda pipeline dq`. Examples:

| Rule | Zone | What it catches |
|------|------|-----------------|
| `bronze_region_null_rate` | bronze | A reference feed that silently stopped |
| `bronze_impossible_negative_balances` | bronze | **WARNING** — physically impossible balances (123 values) |
| `silver_customer_unique_version` | silver | Two versions both claiming to be current |
| `silver_customer_one_current` | silver | Zero or many current rows per customer |
| `silver_txn_unique` | silver | Duplicated transactions |
| `silver_txn_amount_range` | silver | Amounts outside 0…₹10 crore |
| `silver_account_referential` | silver | Accounts with no customer |
| `silver_account_balance_flag_rate` | silver | Blast radius of a balance remediation |

A **FAIL** blocks publication; a **WARNING** is reported and carried into the
answer's evidence block so an analyst can see it. That distinction matters: a rule
that cannot distinguish an overdraft from a corruption bug must not stop a run.

## Verified numbers

```
uv run bda pipeline run     # ~9.5 s end to end
uv run bda pipeline dq      # 30 PASS + 1 WARNING
uv run bda catalog list     # 3 products
```

Gold row counts: `customer_360` 2,000 · `transaction` 1,083,322 · `credit_risk` 2,000.

## Where this lands in production

- An AWS Glue/Step Functions pipeline runs the same stages on a schedule
  (`infra/stacks/pipelines.py`, `.github/workflows/data-pipeline.yml`).
- Bronze lands in `bda-<env>-bronze`, silver/gold in their buckets, ops in
  `bda-<env>-ops`.
- The workgroup `bda-agents` caps queries at `athena_bytes_cap_mb` (512 MB) and
  `athena_query_timeout_s` (30 s).
- A failed quality rule fails the stage, which fails the pipeline, which pages.

## Invariants a change must not break

1. Bronze is a faithful copy; no value is "fixed" on the way in.
2. Silver has exactly one current row per entity where the contract says so.
3. Gold is *only* built from silver — never from bronze, never from a landing file.
4. `publish` is idempotent: running it twice yields the same catalog digest.
5. Quality FAIL blocks publication; WARNING never does.
