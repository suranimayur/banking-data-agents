# 06 — The AWS analytics stack

**Level:** intermediate → advanced · **You will be able to:** name what each of S3,
Glue, Athena, Lake Formation, DynamoDB, KMS, Secrets Manager and Step Functions
actually does, and say why this project uses it.

## The idea

AWS offers analytics as **composed primitives**, not one product. You buy storage,
catalogue, compute and permissions separately and wire them yourself. That is more
work than Snowflake's single box, and it is why the parts are worth understanding
individually — a bug is usually one part being wrong about another's contract.

The mental model is one sentence: **S3 holds the bytes, Glue describes them, Athena
queries them, Lake Formation decides who may, and IAM decides what is allowed at all.**

## The stack, one layer at a time

### Amazon S3 — object storage

Flat buckets of immutable objects, addressed by key. There are no directories; the
`/` in a key is a convention that tools render as a folder. Its two superpowers here
are **durability** (11 nines) and **decoupling** — compute can be deleted and
recreated without data loss.

In this project: five buckets, `bda-<env>-{bronze,silver,gold,ops,artifacts}`, with
`artifacts` also holding Athena query results. Versioning and SSE-KMS are set in the
CDK stack. `S3Lake` is the implementation of the `Lake` protocol; `LocalLake` is its
filesystem twin, and `get_lake()` picks between them.

### AWS Glue Data Catalog — the metadata layer

A Hive-compatible metastore: databases, tables, columns, types, partition locations.
It stores **no data**. Its entire job is to answer "where do the bytes for
`gold.customer_360` live, and what is their shape?".

This is why a schema change in Athena needs a catalogue update, and why the pipeline
writes both Parquet *and* catalogue metadata. `ensure_glue_databases()` in
`pipeline/lake.py` creates the four databases (`bda_bronze`, `bda_silver`,
`bda_gold`, `bda_ops`) — the naming rule is "the same word in every layer".

### Amazon Athena — serverless SQL

Presto/Trino as a service. You submit SQL, it reads the catalogue, scans S3, returns
rows. There is no cluster to size, and you pay **per byte scanned** — which is the
single most important economic fact about it. Partitioning and columnar formats are
therefore not optimisations, they are cost control.

In this project: `AthenaEngine` (`name="athena"`) inside `pipeline/engine.py`, behind
the same `QueryEngine` Protocol as `DuckDBEngine`. The workgroup is `bda-agents`, and
`athena_bytes_cap_mb = 512` is a hard ceiling so a runaway query fails rather than
bills.

### AWS Lake Formation — permissions over data

IAM answers "may this principal call `athena:StartQueryExecution`?". It does **not**
answer "may it see the `national_id` column?". Lake Formation adds table- and
column-level grants on top of the catalogue, including row filters and
tag-based access control.

This is the layer that makes the holdout in `ops.fraud_ground_truth` a
platform-level fact rather than a code convention. The SQL guardrail
([chapter 04](04-sqlglot-and-the-sql-guardrail.md)) refuses that dataset statically;
Lake Formation is what would still refuse it if the guard were bypassed or buggy.

### Amazon DynamoDB — key-value at scale

A managed key-value/document store with predictable single-digit-millisecond reads
at any size. The price is a rigid access model: you design keys for your queries, and
anything unindexed is a scan.

In this project: answer retention. `audit.py::DynamoDBAnswerStore` writes one item per
answer keyed by `trace_id`, with `_INDEXED_FIELDS` for the query patterns we expect,
a TTL (`DEFAULT_TTL_DAYS = 400`) so retention is enforced by the database rather than
by a cron job, and `MAX_ITEM_BYTES = 400_000` guarding DynamoDB's hard 400 KB item
limit. `InMemoryAnswerStore` is the local twin.

One implementation detail worth internalising: the low-level `client` API takes
**typed attribute values** — `{"S": "…"}`, `{"N": "…"}` — not plain Python. Writing
`{"trace_id": "abc"}` silently fails against real DynamoDB. The store builds explicitly
tagged items, drops zero-length strings (DynamoDB rejects empty string attributes),
and never passes an `int` where a tagged number is required.

### AWS KMS — key management

Envelope encryption as a service. You never handle raw keys; you ask KMS to encrypt
a data key, and you store the wrapped version. Every decrypt is audited in
CloudTrail, which is often the actual requirement — not secrecy, but *attributability*.

### AWS Secrets Manager — credential custody

Versioned secret storage with rotation hooks. In this project it holds the model API
key material and the substrate for guardrail configuration, never in `.env` in a
deployed environment.

### AWS Step Functions — orchestration

A state machine: JSON-defined steps with retries, catches, parallel fan-out and
**durable** execution history. The crucial property over a shell script is that the
orchestrator, not your process, owns the state — so a step can run for fifteen
minutes, and a restart does not replay it.

In this project the medallion run is a state machine, with one task definition per
stage because `TaskInput.from_object` takes a Mapping and has no list variant. That
constraint is why the stack defines a task definition per stage rather than one
parameterised task.

### Also in the platform

| Service | Job |
|---------|-----|
| **ECS + ECR** | Runs the API image; ECR holds the tag the stack deployed |
| **App Runner** | Managed HTTPS in front of the console |
| **Cognito** | User pools for console/API auth |
| **EventBridge + Kinesis** | Scheduled triggers and the streaming boundary |
| **CloudWatch Logs/Metrics + SNS** | Logs, EMF metrics, and alarms that page a human |
| **Budgets** | A spend ceiling, as an alarm rather than a hope |
| **Bedrock + AgentCore** | The model and the managed agent runtime — [chapter 08](08-amazon-bedrock-and-agentcore.md) |

## In this project

`infra/stacks/` holds eleven stacks: `network`, `storage_lake`, `catalog`,
`identity`, `guardrails`, `pipelines`, `agent_runtime`, `gateway`, `observability`,
`ui`, plus `common`. They synthesize cleanly for `dev`, `staging` and `prod`, and a
handful of hard-won lessons are encoded in them:

- **Every stack must forward `env=` to `Stack.__init__`.** Without it, CDK refuses
  cross-stack references with "only supported for stacks deployed to the same
  environment".
- **Keep `grant_*` calls local to a stack.** A grant that spans stacks creates a
  dependency cycle that looks fine until deploy. Pass plain names and ARNs across
  boundaries instead.
- **`CfnGuardrail` is used directly** for the Bedrock guardrail, because there is no
  L2 construct for it — and because **there is no `IN_AADHAAR` entity type**, so a
  regex is used to detect that identifier class in `guardrails.py`.

## Level up

**Athena partition projection** versus `MSCK REPAIR TABLE`: projection computes
partitions from a pattern and needs no catalogue writes, which is usually the right
answer for date-shaped partitions.

**Cost is a design input.** The three levers, in order of impact: scan fewer bytes
(partition + columnar), cache results, and cap the query (`athena_bytes_cap_mb`).

**Eventually-consistent catalogue.** A table created a millisecond ago may 404 in
Athena. Retry-with-backoff is not paranoia here, it is the documented contract.

**What to learn next:** Iceberg/Delta table formats on S3 (snapshots, time travel,
schema evolution), Step Functions' *distributed map*, and Lake Formation's
tag-based access control as a way to express "PII columns" once rather than per
table.

## Try it

```bash
cd banking-data-agents
uv run bda pipeline run                              # writes Parquet + lineage
uv run bda infra synth --env dev                     # see all 11 stacks as templates
ls cdk.out/ | grep template | head                     # the generated CloudFormation
uv run bda floci status                              # S3/Glue/Athena path needs Docker
```
