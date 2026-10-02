# 05 — AWS foundations and `boto3`

**Level:** beginner → advanced · **You will be able to:** call any AWS service from
Python, point the SDK at an emulator, and test cloud behaviour with no cloud.

## The idea

AWS is not one product. It is a few hundred independently-versioned HTTP APIs that
share three things: **identity**, **addressability**, and **signing**. Understand
those three and you understand all of it.

- **Identity** — *who* is calling. An IAM user, a role, or a federated token,
  reduced to an access key id + secret + optional session token.
- **Addressability** — *where*. Every service has a regional endpoint such as
  `https://dynamodb.us-east-1.amazonaws.com`, and every resource lives in one region.
- **Signing** — *proof*. AWS SigV4: a deterministic HMAC over the request, derived
  from the secret key. The secret is never sent. The server recomputes the signature
  and compares.

`boto3` is the Python SDK: a typed-ish wrapper that builds those requests, signs
them, retries them and parses the responses. `botocore` underneath does the real
work — including reading the service models that define every operation.

## The mental model

```
   your code
      │  client.put_object(Bucket=..., Key=...)
      ▼
   boto3  ── validates against a service model ──┐
      │                                          │  botocore
      ▼                                          │
   botocore ── builds request ── signs (SigV4) ──┘
      │
      ▼
   endpoint URL  ← the ONE variable that decides "real AWS" or "emulator"
```

That last box is the entire trick behind Floci, `moto` and LocalStack. The SDK does
not know it is talking to a fake, because a fake that implements the protocol *is*
the service as far as the client is concerned.

## The minimum you need

```python
import boto3

session = boto3.Session(region_name="us-east-1")
s3 = session.client("s3")                       # low-level: dicts in, dicts out
s3.list_buckets()

ddb = session.resource("dynamodb")              # high-level: objects and attributes
```

Two flavours, and the difference matters here. **`client`** is 1:1 with the API and
returns plain dicts. **`resource`** is a convenience object layer. This repository
uses `client` almost everywhere, because parity with the real API is worth more than
sugar, and because it works uniformly against emulators.

## In this project

### One factory, one endpoint decision

`aws.py` is the single place that constructs boto3 sessions and clients, and it is
Floci-aware: it reads `aws_endpoint_url` from settings and passes it as
`endpoint_url`. That means:

```bash
# Real AWS
BDA_AWS_ENDPOINT_URL=""            uv run bda pipeline run

# Floci emulator on localhost:4566
BDA_AWS_ENDPOINT_URL=http://localhost:4566 uv run bda pipeline run
```

...and **no other code changes.** This mirrors the engine seam in
[chapter 03](03-duckdb.md): one variable flips the world, and nothing above it
branches.

The credentials default to the literal string `"test"` in `config.py`. That is
deliberate and safe for two reasons: an emulator does not verify signatures, and
placeholder keys cannot accidentally authenticate against real AWS.

### The Floci endpoint

Floci is an MIT-licensed local AWS emulator on port **4566** with a health check at
`/_floci/health`. It emulates about 119 services, including everything this project
needs: S3, DynamoDB, Glue, Athena (with a DuckDB sidecar), Lake Formation, Step
Functions, Lambda, Kinesis, Cognito, KMS, Secrets Manager, CloudWatch Logs/Metrics,
Bedrock, Bedrock AgentCore and S3 Vectors.

Its one real cost: **it is a container**, so it needs a running Docker daemon. When
the daemon is down, this project does not fail — it falls back to `LocalLake` +
`DuckDBEngine` and the 2 Floci-dependent integration tests skip themselves. That
graceful degradation is a design decision:
[a green suite beats an ambiguous red one](../19-troubleshooting.md).

### Testing AWS without AWS: `moto`

`moto` monkey-patches botocore so that AWS calls are served in-process.

```python
from moto import mock_aws

@mock_aws
def test_bucket_written():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="bda-bronze")
```

This is how `tests/integration/test_aws_boundary.py` verifies the S3 boundary with
**no Docker and no network**. It is not a substitute for testing against Floci —
`moto` implements *its* view of the API — but it catches the 90% of bugs that are
"we wrote the wrong argument name". The remaining Floci cases exist precisely to
catch what `moto` cannot.

## Level up

**`moto`'s sharp edges.** Its Glue backend parses SQL with `pyparsing`, imported
lazily, so without it `mock_aws` raises at *call* time rather than import time. That
is why `pyparsing` sits in the dev group with an explanatory comment — the failure
otherwise looks like a bug in your own code.

**Throttling and retries.** The default botocore retry mode is `legacy`, which is
poor at concurrency. This project's `BedrockModel` config uses
`{"max_attempts": 4, "mode": "standard"}`. The same reasoning applies to every
client, and getting it wrong shows up as random failures under load.

**Costs are a correctness concern.** Scanning an S3 prefix or running an Athena
query has a price. `athena_bytes_cap_mb` in `config.py` is a hard guard on query
cost, and the CDK stack attaches a Budgets alarm. A platform that can silently spend
money is a production incident waiting for a quiet month.

**What to learn next:** AssumeRole and trust policies; SigV4 in enough detail to read
a `403 SignatureDoesNotMatch`; the difference between a *regional* and a *global*
endpoint (IAM, CloudFront, Bedrock's model access); and STS `GetCallerIdentity`,
which is the fastest way to answer "which identity am I actually using?".

## Try it

```bash
cd banking-data-agents

# See how the SDK is configured without touching AWS.
uv run python -c "
from banking_data_agents import aws
print(aws.describe_endpoint())
print('endpoint reachable:', aws.endpoint_reachable())
print('s3 client built:', type(aws.s3()).__name__)   # cached per process
"

# Prove AWS behaviour offline.
uv run pytest tests/integration -q
uv run bda floci status      # honest about whether the emulator is up
```
