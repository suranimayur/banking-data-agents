# 09 — Pydantic and configuration

**Level:** beginner → advanced · **You will be able to:** make misconfiguration
impossible to reach production, and validate every boundary in the system.

## The idea

Python is dynamically typed, and the places that hurt are always the boundaries:
environment variables arriving as strings, JSON off the wire, YAML on disk,
credentials from a secret store. A missing `BDA_GOLD_BUCKET` should fail **when you
configure the system**, not three hours into a pipeline run.

Pydantic is the standard answer. You declare the shape you want as a class with type
annotations; Pydantic validates, coerces and reports *where* the data was wrong.

## The mental model

```
   raw, untrusted input            declared model              trusted object
   ─────────────────────           ──────────────              ──────────────
   {"port": "8080"}      ──────►   port: int          ──────►  .port == 8080  (int)
   {"port": "abc"}                 port: int                     ✗ ValidationError
                                                                   with the field path
```

The value is the **failure**. A validated boundary converts a class of runtime
mystery into a startup message that names the offending field.

## The minimum you need

```python
from pydantic import BaseModel, Field, ValidationError

class Bucket(BaseModel):
    name: str = Field(min_length=3)
    versioned: bool = True

Bucket(name="bda-gold", versioned="true")   # coerces "true" → True
Bucket(name="x")                            # ValidationError, path: name
```

`pydantic-settings` extends this to the environment, and that is where the
`BDA_` prefix in this project comes from.

## In this project

`config.py` is **the one place that knows the environment**. It defines a
`Settings(BaseSettings)` class; the prefix means `bda_gold_bucket` reads
`BDA_GOLD_BUCKET`, and the `.env` file, real environment variables and constructor
arguments all feed the same validator.

The declarations read like documentation, because they are:

```python
bda_lake_backend: LakeBackend = "auto"        # local | s3 | auto  (a Literal)
aws_region: str = "us-east-1"
aws_endpoint_url: str | None = None           # None = real AWS
bda_bronze_bucket: str = "bda-bronze"
athena_workgroup: str = "bda-agents"
athena_bytes_cap_mb: int = 512
synthetic_seed: int = 20260928
bedrock_model_reasoner: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
agent_max_tool_calls: int = 8
agent_max_tokens: int = 60_000
session_budget_usd: float = 0.50
user_daily_budget_usd: float = 5.00
```

Four things that are *not* accidents:

- **`LakeBackend` is a `Literal`**, so `BDA_LAKE_BACKEND=ss3` fails at startup
  rather than being treated as an unknown string.
- **`aws_endpoint_url = None` means real AWS.** One nullable field decides the whole
  deployment target ([chapter 05](05-aws-foundations-and-boto3.md)).
- **Budgets are settings.** `session_budget_usd`, `user_daily_budget_usd` and
  `agent_max_tokens` are configuration a platform owner changes, not constants
  buried in code.
- **`synthetic_seed` is fixed**, so the dataset is deterministic and the README's
  numbers are reproducible ([chapter 03 of the playbook](../03-synthetic-data.md)).

### Derived values live on the model

`Settings` exposes properties rather than duplicating logic:

```python
@property
def bronze_uri(self) -> str: return f"s3://{self.bda_bronze_bucket}"

@property
def answer_table_name(self) -> str:
    return self.bda_answer_table or f"bda-{self.env}-answers"

@property
def buckets(self) -> list[str]: ...
```

`answer_table_name` shows the pattern: an explicit setting wins, and otherwise a name
is derived by the **one naming convention** the whole platform shares. That convention
is also what `infra/config.py::qualify(name, env)` implements for CloudFormation, so
code and infrastructure cannot drift.

### Caching the settings

```python
@lru_cache
def get_settings() -> Settings: ...
def reset_settings_cache() -> None: ...
```

Pydantic validation is not free, and settings are read constantly. The `lru_cache`
makes it once-per-process; `reset_settings_cache` exists so tests can switch
environments without a subprocess. Anything cached process-wide needs an explicit
reset, or your tests become order-dependent — this repository's `tests/conftest.py`
has `_reset_process_caches()` that clears settings, catalog, engines, models and
metrics together.

### Pydantic at every boundary, not just config

The same discipline is applied to the API (`AskRequest`/`AskResponse` in
`api/app.py`) and to data contracts, where the YAML in `contracts/` is validated into
a model rather than indexed as a dict. The rule is consistent: **untrusted input is
parsed once at the edge and trusted thereafter.**

## Level up

**Validation errors are an interface.** `str(ValidationError)` includes the field
path and the failing value. Emitting that at startup is a gift to the next operator;
emitting "configuration error" is not.

**Secrets and settings are different concerns.** A secret belongs in Secrets Manager
with rotation ([chapter 06](06-the-aws-analytics-stack.md)); a *setting* is a
non-sensitive knob that belongs in a template. Mixing them is how credentials end up
in git history.

**`env` naming deserves care.** `bda_answer_table` defaulting to
`bda-{env}-answers` means a developer who forgets `BDA_ENV` writes to the wrong
table — which is why the default is `local`, not `prod`.

**What to learn next:** `model_validator` and `field_validator` for cross-field rules;
`SecretStr` so a token cannot leak into a log; and JSON Schema generation, which
turns your models into documentation automatically.

## Try it

```bash
cd banking-data-agents

# Print the effective configuration.
uv run python -c "
from banking_data_agents.config import get_settings
s = get_settings()
print('env          :', s.env)
print('lake backend :', s.bda_lake_backend)
print('answer table :', s.answer_table_name)
print('buckets      :', ', '.join(s.buckets))
print('model        :', s.model_for('reasoner'))
"

# Make a typo, and watch it fail at startup instead of at 3am.
BDA_LAKE_BACKEND=ss3 uv run python -c "from banking_data_agents.config import get_settings; get_settings()" 2>&1 | tail -5
```
