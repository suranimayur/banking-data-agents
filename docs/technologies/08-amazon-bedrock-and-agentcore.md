# 08 — Amazon Bedrock and AgentCore

**Level:** beginner → advanced · **You will be able to:** explain model selection,
guardrails and where a production agent actually runs.

## The idea

Two problems get in the way of putting a model in a bank.

**Procurement.** You want to call a state-of-the-art model, but the model is not on
your infrastructure, may not be allowed to see your data, and may change underneath
you. **Bedrock** solves this by offering models *as a managed AWS service*: one API,
one auth model, one region, one bill — and, critically, no data leaves your AWS
boundary and no traffic goes to a third-party endpoint.

**Operations.** A model in a notebook is not an agent in production. It needs a
runtime with sessions, scaling, identity, memory and observability. **AgentCore** is
that runtime.

## The mental model

```
   your agent code
        │  Strands Model protocol
        ▼
   BedrockModel ──► bedrock-runtime ──► the model
        │                                  │
        │  before/after every call:        │
        └────► Bedrock Guardrails ─────────┘   (PII, denied topics, grounding)

   deployed: AgentCore runtime  ──► the same agent code, with sessions + scaling
```

The important property: **the model is an implementation detail behind one
interface.** Strands defines a `Model` protocol; `BedrockModel` and `StubModel` both
satisfy it. That is why this repository's whole test suite runs with no cloud and no
API key.

## In this project

### Model tiers, not one model

```python
bedrock_model_router     = "anthropic.claude-3-5-haiku-20241022-v1:0"
bedrock_model_reasoner   = "anthropic.claude-3-5-sonnet-20241022-v2:0"
bedrock_model_escalation = "anthropic.claude-3-opus-20240229-v1:0"
```

Three logical tiers, one per kind of work:

- **router** — classification and routing. Cheap, small, fast.
- **reasoner** — the analyst-facing Copilot and the specialists. The default.
- **escalation** — long-horizon work: root-causing a data incident, drafting a
  contract change.

`Settings.model_for(tier)` is the switch. Splitting by *tier* rather than per agent
means a change of model is a change of configuration, not of code, and it makes the
cost conversation concrete: a classification call has no business running on the
most expensive model available.

### Constructing the client, and why it is fiddly

```python
session = boto3.Session(
    region_name=settings.aws_region,
    aws_access_key_id=settings.aws_access_key_id or None,
    ...
)
kwargs = {
    "boto_session": session,
    "model_id": settings.model_for(tier),
    "boto_client_config": BotoConfig(
        retries={"max_attempts": 4, "mode": "standard"},
        connect_timeout=5,      # connecting should be fast
        read_timeout=120,       # reasoning models take a while to answer
    ),
    "temperature": 0.0,
    "max_tokens": 4096,
}
if provider in {"floci"} and settings.aws_endpoint_url:
    kwargs["endpoint_url"] = settings.aws_endpoint_url
```

Three decisions are encoded there, and each was learned the hard way:

1. **A session is passed explicitly.** The `.env` file is not exported into
   `os.environ`, so without this the SDK signs with whatever credentials happen to
   be on the machine — a confusing class of "works for me" bug.
2. **Connect timeout ≠ read timeout.** Long reads are normal for a reasoning model;
   slow connects are a network problem and should fail fast.
3. **`endpoint_url` is only applied to emulator providers.** Pointing at Floci and
   silently receiving a fake answer from what you thought was production is a
   failure mode worth designing out.

### Guardrails

Bedrock Guardrails filter content at the API boundary — blocked input/output
messaging, denied topics, PII detection, and grounding checks. The CDK stack uses
the raw `CfnGuardrail` construct (there is no L2), configured with
`blocked_input_messaging`, `blocked_outputs_messaging` and `name`.

Two practical notes that cost real time:

- **There is no `IN_AADHAR` entity type.** Aadhaar is India's national ID and is not
  in the managed PII recogniser list, so a regex does the work in `guardrails.py`.
  Assume nothing about which identifier schemes are covered — check.
- **A guardrail is not a substitute for the SQL guard.** Bedrock Guardrails inspect
  *text*. They cannot know that `ops` holds fraud labels. That is what
  `tools/sqlguard.py` is for. Layered, they cover each other's blind spots.

### AgentCore

AgentCore provides the deployed runtime: managed sessions, scaling, identity binding,
and — in this project — the concept that makes its release story work:

```
StageConfig.live_agent_versions: dict[str, str]
```

The `live` AgentCore endpoint is pinned to **the exact version the canary served**.
Promotion is a version bump; **rollback is a re-pin**. There is no rebuild, no
redeploy, no waiting for a container — which is the difference between a rollback you
will actually perform and one you will talk about. See
[the CI/CD chapter](../16-cicd-and-environments.md).

## Level up

**Model access is per-account and per-region.** A model listed in the docs may need
explicit enablement, and enabling it in `us-east-1` does nothing for `eu-west-1`.
This is the single most common first-deploy failure.

**Grounding and citations.** For regulated answers, "the model said so" is not
acceptable provenance. This project's answer to that is the evidence envelope
([chapter 10 of the playbook](../10-the-evidence-envelope.md)), built from
deterministic tool output rather than from model prose.

**What to learn next:** cross-region inference profiles (for capacity and latency),
prompt caching (large cost savings when a system prompt is stable — which it is
here), Knowledge Bases and S3 Vectors for retrieval, and the difference between
`InvokeModel` and the Converse API when you want provider-agnostic messages.

## Try it

```bash
cd banking-data-agents

# What the factory would build, without building it.
uv run python -c "
from banking_data_agents.llm.factory import describe_model
from banking_data_agents.config import get_settings
s = get_settings()
print('provider:', s.llm_provider)
for tier in ('router','reasoner','escalation'):
    print(f'{tier:11s}', s.model_for(tier))
print(describe_model('reasoner'))
"

# To exercise the real path you need credentials and model access:
#   BDA_LLM_PROVIDER=bedrock AWS_REGION=us-east-1 uv run bda ask "…"
# With Docker up, the emulated path needs no credentials:
#   BDA_LLM_PROVIDER=floci uv run bda ask "…"
```
