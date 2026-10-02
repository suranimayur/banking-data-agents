# 19 · Troubleshooting

The failures that actually happen, roughly in the order a new developer meets them.
If a symptom is not here, it is probably in
[18 Operations runbook](18-operations-runbook.md).

---

## Setup

### `python` is 3.14 and something breaks

`uv` provisions CPython 3.12.12 regardless of the system `python`. Always run
through `uv run`. If you must invoke Python directly, use `uv run python`.

### Unicode errors on Windows

```
uv run bda ask "…"
```

printing an em-dash or a regional character crashes the console. Prefix the command:

```bash
PYTHONIOENCODING=utf-8 uv run bda ask "…"
```

### `ModuleNotFoundError: pyparsing` in a Glue test

moto's Glue backend parses SQL with `pyparsing`, and it is missing from a stale
virtualenv.

```bash
uv sync
```

It is declared in the dev dependency group; a stale `.venv` is the only way to see
this.

---

## Floci and Docker

### `lake_fallback_local` warning and everything reads the filesystem

The Docker daemon is not running. The platform detects an unreachable endpoint and
falls back deliberately, so `make pipeline` stays useful without Docker.

This is fine for development. It is **not** fine if you meant to test the S3/Athena
path — in that case the test is not testing what you think.

```bash
docker info                 # is the daemon up?
make floci-up
uv run bda floci status
```

On Windows the emulator reports an `npipe` error when Docker Desktop is not started;
starting it is the whole fix.

### `endpoint_reachable` is true but S3 calls fail

`AWS_ENDPOINT_URL` is set but the emulator does not serve that service, or the
credentials are wrong. `bda floci status` reports health and the provisioned
resources; compare them against what you expect.

---

## The pipeline and the lake

### `FileNotFoundError: gold.customer_360`

The pipeline never ran, or it ran against a different data directory.

```bash
uv run bda data generate
uv run bda pipeline run
```

If you set `BDA_DATA_DIR` or `BDA_LANDING_DIR`, remember that a *new* process gets
the *new* directory; a running engine is cached (`tools/context.py::reset_engines`).

### Stale answers after a pipeline run

The query engine is cached per process because registering the medallion views is
the expensive part of a query. After `bda pipeline run`, a long-lived process may
still hold the old engine.

In the CLI this does not arise (each invocation is a new process). In a service or a
notebook, call:

```python
from banking_data_agents.tools.context import reset_engines
reset_engines()
```

`pipeline/engine.py` has **no** `reset_engine_cache`; the real reset is
`tools/context.py::reset_engines`.

### A short history window breaks a query

An additive upstream change means a column exists only in later batches. Bronze
normalises this at ingestion (`normalise_frames`) — if a short window fails, the
table was written before normalisation or something bypassed it.

---

## Agents and tools

### `bda ask --json` output is not valid JSON

Something wrote to **stdout** that was not the command's own output. stdout *is* the
answer for that command.

Diagnostics belong on the logger. There is a regression test for exactly this
(`logging_setup.py` + `tests/unit/test_agents.py`); if you are seeing it, a new
`print` was added.

### The model "ignores" a tool result

A tool returned a dict containing a non-JSON value — a `Decimal`, a `date`, a
`datetime`. Strands falls back to `str()` and the model receives something it cannot
read as data.

`tools/impl.py::_jsonable` handles `Decimal`, `date` and `datetime`. Any new tool
must return JSON-safe payloads; the safe way is to pass values through the same
helper rather than to rely on the model being forgiving.

### The agent says it does not know about a product that clearly exists

Check `data/` is the lake the agent is using. A test that ran earlier may have left
the process pointing at a temporary lake; `tests/conftest.py` resets every
process-wide cache before and after building the shared lake for this reason.

### An answer is `INCOMPLETE`

The loop hit a budget or a timeout:

| Setting | Default |
|---------|--------:|
| `agent_max_tool_calls` | 8 |
| `agent_max_tokens` | 60,000 |
| `agent_max_wall_clock_s` | 45 |
| `session_budget_usd` | 0.50 |

`INCOMPLETE` is correct behaviour, not a crash. If it happens often, the question is
probably under-specified — check whether the planner wanted to clarify.

### A refusal where you expected an answer

Read `governance.source`:

- `contract` → a `not_allowed_use` clause. Read it; it is a deliberate decision
  ([17](17-security-and-compliance.md)).
- `guardrail` → a per-agent restriction, e.g. the `fraud` agent refusing to rank
  individuals. Try `copilot` if the question is legitimate.
- otherwise → the SQL guard. The violation code is in the envelope.

---

## CLIs and retention

### `bda answers get` says there is no retained answer

Retention is **opt-in**. Locally there is no DynamoDB table unless you ask for one:

```bash
export BDA_ANSWER_TABLE=bda-local-answers
uv run bda bootstrap          # or the emulator's provisioning path
```

The command tells you when retention is off rather than printing nothing — if it
printed a lookup result, that is a bug.

### An answer is missing its preview after retention

The envelope exceeded ~400 KB and was trimmed
(`truncated_for_retention: true`) rather than dropped. Losing the preview is
survivable; losing the answer is not ([10](10-the-evidence-envelope.md)).

---

## Infrastructure

### `Cross stack references are only supported for stacks deployed to the same environment`

The message is about cross-stack references; the cause is almost always a stack
constructed **without `env=`**. Every stack in `infra/synth.py` is passed an
explicit `env`; a new one that omits it produces this error
([15](15-infrastructure.md)).

### A dependency cycle between stacks

Somebody added a cross-stack `grant_*`. Keep grants local to each stack and pass
plain names and ARNs across boundaries. It is less elegant and it always
synthesises.

### `TaskInput.from_object` rejects a list

It takes a Mapping. A pipeline with several stages needs **one task definition per
stage**.

---

## Tests

### Integration tests are skipped

`require_floci` skips when the emulator does not answer. That is intentional: a
laptop without Docker gets a green suite instead of an ambiguous one rather than a
false failure.

```bash
make floci-up
uv run pytest tests/integration -q
```

### The full suite is slow

Most of the runtime is the session-scoped `pipelined_lake` fixture, which builds a
12-month lake **once** for the whole session. It is deliberate: rebuilding per
module would triple the runtime for no extra confidence.

```bash
uv run pytest -m "not slow" -q      # the fast loop
uv run pytest -m smoke -q           # post-deploy journeys
```

### A test passes alone and fails in the suite

Almost always a process-wide cache. `tests/conftest.py::_reset_process_caches`
resets settings, the catalog, engines, models and metrics; a new cache that is not
in that list is the bug.
