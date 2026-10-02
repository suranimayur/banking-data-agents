# 15 · Infrastructure

## Concept

Eleven CDK stacks describe the platform. Three environments (`dev`, `staging`,
`prod`) are the same code with different names and sizes. Every physical resource
name comes from one function:

```python
infra/config.py::qualify(name, env) -> f"bda-{env}-{name}"
```

The rule exists so that two environments can live in one account without colliding
and without anyone having to remember a suffix.

```bash
uv run bda infra synth --env dev        # synthesise, no AWS call
uv run bda infra deploy --env staging --image-tag <git-sha>
uv run bda infra destroy --env dev --yes
```

## The stacks

```
infra/
  config.py            StageConfig, qualify(), live_agent_versions
  synth.py             build_app(stage_name=, image_tag=, outdir=)
  app.py               CLI entry
  stacks/
    common.py          shared constants (governed zones, naming helpers)
    network.py         NetworkStack — VPC, subnets, endpoints
    storage_lake.py    StorageLakeStack — the five buckets
    catalog.py         CatalogStack — Glue databases + the published catalog
    identity.py        IdentityStack — roles, policies, access groups
    guardrails.py      GuardrailsStack — Bedrock guardrail (incl. an Aadhaar regex)
    pipelines.py       PipelinesStack — Glue/Step Functions, one task def per stage
    agent_runtime.py   AgentRuntimeStack — agents, one endpoint per live version
    ui.py              ApiStack + ConsoleStack
    gateway.py         GatewayStack — fronts the API
    observability.py   ObservabilityStack — log groups, alarms, dashboards
  bootstrap/
    stack.py           per-account OIDC deploy roles, state bucket, permissions boundary
```

The eleven stacks are Network, Identity, StorageLake, Catalog, Guardrails,
Pipelines, AgentRuntime, Api, Console, Gateway and Observability (`common.py` holds
shared constants, not a stack). All eleven synth cleanly for `dev`, `staging` and
`prod`.

## The five buckets

| Bucket | Contents |
|--------|----------|
| `bda-<env>-bronze` | Raw batches, schema-normalised |
| `bda-<env>-silver` | Conformed entities |
| `bda-<env>-gold` | Published products |
| `bda-<env>-ops` | Quality results, lineage, and the fraud holdout |
| `bda-<env>-artifacts` | Images, reports, evaluation output |

`Settings.all_buckets` is the canonical list; `lake.py::ensure_buckets` provisions
it locally and against Floci, and the CDK stack owns it in a deployed environment.
One list, so a bucket cannot be added to one place and forgotten in the other.

## Three traps that cost real time

These are recorded because they will cost the next person the same time.

### 1. Every stack must be given `env=`

`ApiStack` and `ConsoleStack` must forward `env=` to `Stack.__init__`, or CDK
raises:

```
Cross stack references are only supported for stacks deployed to the same environment
```

The error names cross-stack references, which sends you looking for a reference that
is not the problem. The problem is an unset environment.

### 2. Cross-stack `grant_*` cycles are a trap

`bucket.grant_read(lambda)` across stacks creates a dependency cycle the moment the
other stack grants something back. The fix used here is to keep grants **local to
each stack** and pass plain names and ARNs across boundaries. It is less elegant and
it always synthesises.

### 3. `TaskInput.from_object` takes a Mapping

There is no list variant, so a pipeline with several stages needs **one task
definition per stage**. If you find yourself trying to pass a list, you are
fighting the API, not using it.

## Configuration and promotion

`StageConfig` carries per-environment values: sizes, retention, model tiers and
`live_agent_versions`.

`live_agent_versions` is the promotion mechanism. It pins the `live` AgentCore
endpoint to the exact version the canary served. Promotion pins the canary version;
**rollback re-pins the previous version**. No rebuild is involved, which is what
makes rollback fast enough to be the first response to a regression
([11](11-evaluation-and-release-gates.md)).

## Bootstrap

`infra/bootstrap` is deployed **once per account**, not per environment:

- OIDC deploy roles (so CI assumes a role instead of holding keys);
- the CDK state bucket;
- a permissions boundary applied to everything the pipeline creates.

`bda infra --env bootstrap` is the one place where the environment is an account.

## Verifying without an AWS account

```bash
uv run bda infra synth --env dev
uv run pytest tests/unit/test_infra.py -q
```

`tests/unit/test_infra.py` synthesises and asserts on the template — names are
qualified, the expected resources exist, and no stack is missing its environment.
That is the cheapest possible guard against the three traps above.

Floci's CDK/CloudFormation support means a local deploy can be exercised without a
real account, once the Docker daemon is running.

## Where this lands in production

`deploy-dev.yml`, `deploy-staging.yml` and `deploy-prod.yml` run `bda infra deploy`
with an `--image-tag` that is an immutable commit SHA; `prod` additionally requires
the configuration to name the tag and takes approval. `rollback.yml` re-pins.

## Invariants a change must not break

1. Physical names come from `qualify()`. No hand-written `bda-prod-…` in a stack.
2. Every stack is constructed with its `env=`.
3. Grants stay local to a stack; across boundaries, pass names and ARNs.
4. `prod` deploys by immutable image tag, never `latest`.
5. The eleven stacks synth for all three environments before merge.
