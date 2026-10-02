# 13 — Infrastructure as code with CDK

**Level:** beginner → advanced · **You will be able to:** express cloud
infrastructure in Python, keep three environments consistent, and describe what
CloudFormation actually does on your behalf.

## The idea

Clicking in the AWS console produces infrastructure that exists, and a description of
it that exists only in someone's memory. The consequences arrive later: nobody can
rebuild the environment, nobody can diff staging against production, and a manual
change is indistinguishable from a bug.

**Infrastructure as code** fixes this by making the environment a *program input*.
AWS CloudFormation is the underlying engine: you hand it a JSON/YAML **template**, it
computes a **plan**, and it applies it with rollback on failure.

Writing CloudFormation by hand is miserable — it is verbose, order-sensitive, and
full of repetition. **AWS CDK** is a synthesizer: you write Python, and CDK emits
CloudFormation. You get types, loops, functions, IDE completion and unit tests, and
the deployable artifact is still standard CloudFormation.

## The mental model

```
   Python (your constructs)
        │  cdk synth
        ▼
   CloudFormation templates   ← what you review, diff, and commit
        │  cdk deploy
        ▼
   a plan (change set)  ──►  apply  ──►  real AWS resources
                                ├── on failure: roll back
                                └── unless --require-approval says otherwise
```

Terminology worth getting right, because it is asked in interviews:

| Term | Meaning |
|------|---------|
| **Construct** | A reusable cloud component. L1 = raw resource, L2 = opinionated, L3 = a pattern |
| **Stack** | A unit of deployment. Everything in one stack fails or succeeds together |
| **App** | A collection of stacks, possibly across accounts and regions |
| **Synth** | Python → CloudFormation. No AWS calls |

## The minimum you need

```python
from aws_cdk import App, Stack, aws_s3 as s3
from constructs import Construct

class Storage(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)
        self.bucket = s3.Bucket(self, "Gold", versioned=True)

app = App()
Storage(app, "Storage", env=...)
app.synth()
```

The important habit from day one: **never `cdk deploy` without reading the synth
output.** `bda infra synth` writes templates you can diff and review before anything
touches AWS.

## In this project

### The naming rule and the environment abstraction

`infra/config.py` defines a `StageConfig` per environment and one function that
matters:

```python
def qualify(name: str, env: str) -> str:
    return f"bda-{env}-{name}"
```

That is the **single naming convention** behind every physical resource name. It is
why `dev`, `staging` and `prod` can coexist in one account, why the application's
settings can derive the answers table name the same way
([chapter 09](09-pydantic-and-configuration.md)), and why you can always tell which
environment a resource belongs to from its name alone.

### Eleven stacks

`infra/stacks/` holds `network`, `storage_lake`, `catalog`, `identity`,
`guardrails`, `pipelines`, `agent_runtime`, `gateway`, `observability`, `ui`, plus
`common`. The split is by **rate of change and blast radius**: storage survives
everything, while the agent runtime is redeployed constantly. Putting them in one
stack would mean a storage rollback every time an agent version changed.

`infra/synth.py::build_app(stage_name=, image_tag=, outdir=)` is the entry point, and
`infra_cli.py` wires it to `bda infra {synth|deploy|destroy}`.

### Four lessons that are encoded, not just learned

1. **Forward `env=` into every stack.** If a stack does not receive the environment,
   CDK raises *"Cross stack references are only supported for stacks deployed to the
   same environment."* This is the most common CDK error in a multi-environment app,
   and it is why `ApiStack` and `ConsoleStack` explicitly pass `env=`.
2. **Keep `grant_*` calls inside one stack.** A `bucket.grant_read(role)` where the
   bucket and the role live in different stacks creates a dependency **cycle**. The
   pattern used here is to pass plain names and ARNs across boundaries and grant
   locally. This is the CDK equivalent of avoiding circular imports.
3. **`TaskInput.from_object` takes a Mapping — there is no list variant.** That small
   API fact is why the pipelines stack defines one task definition per stage rather
   than one parameterised task.
4. **Not everything has an L2 construct.** The Bedrock guardrail uses raw
   `CfnGuardrail(scope, id, blocked_input_messaging=…, blocked_outputs_messaging=…,
   name=…)`. Dropping to L1 is normal and not a failure.

### The canary and rollback model

`StageConfig.live_agent_versions: dict[str, str]` pins the `live` AgentCore endpoint
to **the exact agent version the canary served**. Promotion advances the pin;
**rollback is a re-pin** — no rebuild, no image push, no container wait. Making
rollback this cheap is what turns it from a plan into a procedure someone will
actually run under pressure.

### A one-time bootstrap

`infra/bootstrap/` is separate because it has a chicken-and-egg problem: the pipeline
that deploys everything needs a role, and that role must exist first. It provisions
the **OIDC deploy roles**, the **state bucket**, and a **permissions boundary** — the
guardrail that stops a deploy role from granting itself anything it likes.

## Level up

**Synthesised ≠ deployable.** Synth checks your *model*; only deploy checks IAM,
quotas, model access and regions. A green `cdk synth` is a lint pass, not a guarantee.

**Stack dependencies are a graph, and cycles are design errors.** When CDK refuses a
reference, the fix is usually to invert who owns what, not to add an escape hatch.

**Bootstrap once per account/region.** `cdk bootstrap` creates the staging bucket and
roles CDK needs. Every error saying "environment aws://… is not bootstrapped" means
exactly that and nothing more.

**What to learn next:** CDK Aspects for applying a policy across every resource
(useful for enforcing encryption and tagging), custom resources for things
CloudFormation cannot express, and `cdk diff` in CI — the highest-value single
command in a deploy pipeline, because it shows a human exactly what will change.

## Try it

```bash
cd banking-data-agents
uv run bda infra synth --env dev          # 11 stacks, no AWS calls

ls cdk.out/*.template.json                 # the generated CloudFormation
grep -c '"Type"' cdk.out/BdaDevStorageLake.template.json

# Compare environments.
uv run bda infra synth --env prod
uv run pytest tests/unit/test_infra.py -q  # needs the optional `infra` group
```
