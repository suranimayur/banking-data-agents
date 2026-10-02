# 15 — Testing with pytest and `moto`

**Level:** beginner → advanced · **You will be able to:** write tests that are fast,
deterministic and honest about what they do not cover.

## The idea

`unittest` came from Java, and it shows: class hierarchies, `self.assert*` methods,
setup boilerplate. pytest inverted the design — **plain functions, plain `assert`,
and fixtures as dependency injection**. It also made a strong wager that has paid off:
a test failure should tell you *why*, so it rewrites the assertion in the traceback
with actual values instead of a line number.

The second half of the chapter is about a harder problem: **how do you test code whose
job is to talk to AWS?** The answer here is three layers, because each catches what
the others cannot.

## The mental model

```
   ┌─────────────────────────────────────────────────────────────┐
   │ unit        real code, fake I/O, milliseconds               │
   │             "is the logic correct?"                         │
   ├─────────────────────────────────────────────────────────────┤
   │ integration `moto` (in-process AWS) or Floci (a container)   │
   │             "did we call the API correctly?"                │
   ├─────────────────────────────────────────────────────────────┤
   │ smoke       a deployed system                               │
   │             "does it actually work end to end?"             │
   └─────────────────────────────────────────────────────────────┘
```

The cardinal rule: **a test must fail for exactly one reason.** If a unit test fails
because the network was down, it is not a unit test.

## The minimum you need

```python
import pytest

@pytest.fixture
def settings():
    return Settings(env="local")            # inject what the test needs

@pytest.mark.parametrize("sql,ok", [("SELECT a FROM gold.t LIMIT 1", True),
                                    ("DROP TABLE gold.t", False)])
def test_guard(sql, ok):
    assert validate_sql(sql).ok is ok

def test_raises():
    with pytest.raises(ValueError):
        get_agent("nope")
```

Then the AWS half:

```python
from moto import mock_aws

@mock_aws
def test_bucket():
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="bda-bronze")
```

## In this project

**372 tests pass** with the `infra` group installed. Three layers:

| Marker | Command | Requires |
|--------|---------|----------|
| *(unit)* | `uv run pytest tests/unit -q` | nothing |
| `integration` | `uv run pytest tests/integration -q` | nothing (`moto`) or Docker (Floci) |
| `smoke` | `uv run pytest -m smoke -q` | nothing locally; a deployment when run in CI |

### `tests/conftest.py` — the most important file in the suite

It forces a **hermetic environment at import time**, before any test can accidentally
read a developer's real `.env`:

```python
BDA_ENV_FILE = ".env.test-nonexistent"     # no real config can leak in
LLM_PROVIDER = "stub"                       # no API keys, fully deterministic
BDA_LAKE_BACKEND = "local"                  # no S3, no Docker
# small synthetic counts → fast generation
```

Session fixtures then provide the shared, expensive things once:
`repo_root`, `settings`, `floci_available`, `pipelined_lake` (a session-scoped
full 12-month lake, built once for the whole run), `agent_env`, `tool_context`,
`temp_landing`. The function-scoped `require_floci` fixture **skips** its test when
the emulator is not answering.

That skip-vs-fail decision is a deliberate one, and it is the honest option: a
machine without Docker is a legitimate way to work on this repository, and the
correct signal is "not applicable here", not "broken". What makes it safe is that the
docs say exactly which tests skip and why.

### Process-wide caches need a reset

`_reset_process_caches()` clears settings, catalog, engines, models and metrics
between tests that switch environment. This is the recurring cost of the caching
strategy in [chapters 03](03-duckdb.md) and [09](09-pydantic-and-configuration.md):
anything cached process-wide needs an explicit reset, or your suite becomes
order-dependent, which is the worst kind of flaky.

### `moto` and its sharp edge

`moto` intercepts botocore in-process, so `tests/integration/test_aws_boundary.py`
verifies the S3 boundary with no Docker and no network. It has one trap worth
knowing: **moto's Glue backend parses SQL with `pyparsing`**, imported lazily.

```toml
"moto>=5.0",
# moto's Glue backend parses SQL with pyparsing; without it `mock_aws` for
# Glue raises ModuleNotFoundError at call time, not at import time.
"pyparsing>=3.1",
```

The comment exists because the failure mode is confusing: the import error surfaces
*inside* your own test, looking like a bug in your code.

### Marker discipline

```toml
markers = ["integration: requires a running Floci emulator or real AWS",
           "smoke: post-deploy assertions run by the pipeline",
           "slow: takes more than a few seconds"]
```

`--strict-markers` in `addopts` means a typo'd marker is a **test failure**, not a
silently ignored decoration. Silently-ignored markers are how a suite quietly stops
testing anything.

## Level up

**Fixtures are dependency injection, and scope is a performance decision.** A
session-scoped `pipelined_lake` is what keeps a 372-test suite under four minutes
instead of regenerating 1.2M rows per test. Broad scope for expensive immutable
things; function scope for anything a test mutates.

**Determinism is a feature you have to build.** The stub model
([chapter 07](07-llm-agents-and-strands.md)) exists so the eval gate cannot flake.
A test that fails 1 in 20 runs is worse than no test, because it trains people to
re-run CI.

**Test the guard, not the prompt.** Every safety claim in this repository has a test
that would fail if the *enforcement* were removed — not one that checks a sentence in
a system prompt.

**Coverage is a diagnostic, not a target.** `ui/` is deliberately omitted from
coverage in `pyproject.toml`, because chasing a number on presentation code
incentivises tests that assert nothing.

**What to learn next:** `pytest-xdist` for parallelism (`-n auto`), property-based
testing with Hypothesis for the planner (a natural fit for "this phrasing should
never produce free-form SQL"), golden-file tests for the SQL the planner emits, and
mutation testing to find tests that cannot fail.

## Try it

```bash
cd banking-data-agents

uv run pytest tests/unit -q                 # fast, no I/O
uv run pytest -m integration -rs -v         # shows exactly what skipped, and why
uv run pytest -m smoke -q                   # post-deploy journeys
uv run pytest -q -rs                        # everything, with skip reasons

uv run pytest --collect-only -q | tail -3   # count before running
```
