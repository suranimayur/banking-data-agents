# 10 · The evidence envelope

## The idea

In most analytics products the answer is the row of numbers and everything else is
a log line. Here the answer is a **record**: the numbers plus the reason to believe
them. `agents/evidence.py` is the module that builds it, and it is the single most
important type in the repository.

If a feature cannot be explained by the envelope, it is not finished.

## The outcome enum

Every answer has exactly one outcome:

| Outcome | Meaning |
|---------|---------|
| `ANSWERED_FROM_DATA` | A governed query ran and returned rows |
| `ANSWERED_FROM_METADATA` | The answer is about the catalog, not the data (e.g. "which products exist?") |
| `NEEDS_CLARIFICATION` | The question is genuinely ambiguous; candidates are returned |
| `NO_GOVERNED_METRIC` | Understood, but nothing sanctioned answers it |
| `REFUSED` | A governance decision said no |
| `INCOMPLETE` | The loop hit a budget, timeout or unrecoverable tool failure |

The enum is the API contract. `REFUSED` and `NEEDS_CLARIFICATION` are **successful
answers to bad questions**, not errors — they return 200 with `sql: null`, because a
refusal that carries the SQL it would have run is a leak with better manners.

`is_complete` is the derived predicate CI keys on: a run that ends in `INCOMPLETE`
exits non-zero from `bda ask`.

## What the envelope contains

| Group | Fields | Why it is there |
|-------|--------|-----------------|
| Identity | `trace_id`, `asked_at`, `question`, `agent`, `provider`, `model_id` | Reproducibility and attribution |
| Answer | `answer`, `sql`, `columns`, `row_count`, `preview`, `truncated` | What was returned |
| Provenance | `products`, `contract_versions`, `metrics`, `tables`, `engine` | What stood behind it |
| Governance | `governance` (allowed, source, reason), `refused`, `not_allowed_use` | Why it was permitted, or not |
| Quality | `quality` (total, passed, warned, failed), freshness, `sla_status` | Whether the data was healthy |
| Lineage | `lineage` edges (`depth`, `via`) | Where it came from |
| Cost | `latency_ms`, `input_tokens`, `output_tokens`, `cost_usd` | What it cost |
| Audit | `trace` — every tool call with its arguments | Exactly how it was reached |
| Integrity | `contract_digest` | Which catalog produced it |

`render()` is for humans, `to_dict()` for the API, `to_json()` for the CLI's
`--json` and for retention.

## Why each non-obvious field exists

- **`contract_digest`** — two environments, or two days, can be compared; "the
  number changed" has a mechanical first question.
- **`trace`** — `tools/context.py` keeps an append-only record of every tool call.
  The API test asserts `[entry["tool"] for entry in trace] == tools_called`, so the
  envelope cannot claim a step it did not take.
- **`preview` and `truncated`** — an answer is often a *glimpse* of a larger result;
  saying so is part of being honest.
- **`quality`** — a WARNING is carried into the answer, so an analyst sees "123
  impossible negative balances" beside the number it might affect.
- **`sla_status`** — staleness is visible rather than assumed.
- **`cost_usd`** — governance that cannot see cost cannot control it.

## Retention

The envelope is only worth assembling if it survives the request.
`audit.py::DynamoDBAnswerStore` writes it to `bda-<env>-answers`, keyed by
`trace_id` (partition) and `asked_at` (sort), with a TTL. Then
`bda answers get <trace_id>` and `GET /answers/{trace_id}` reproduce exactly what a
caller was told — months later, from a different process.

Three decisions in that module are deliberate:

1. **The payload is one JSON string.** DynamoDB rejects floats, and a preview full
   of amounts is full of floats. Storing the envelope's own JSON is lossless by
   construction; a handful of attributes (`outcome`, `agent`, `asked_at`) sit
   alongside it so the table is queryable without parsing.
2. **Writes are best effort, reads are exact.** Retention failing must never fail an
   answer that already reached the caller, so `put` logs and returns; `get` raises
   on an operational error, because a silent 404 on an audit lookup is worse.
3. **Oversized answers are trimmed, not dropped.** A preview and trace that push
   past ~400 KB are replaced with `truncated_for_retention: true`; losing the
   preview is survivable, losing the answer is not.

Retention is **opt-in** (`BDA_ANSWER_TABLE`). Locally the API uses a bounded
in-process LRU; the CLI says plainly that retention is off rather than printing an
empty result that looks like a lost answer.

```bash
uv run bda answers list
uv run bda answers get 4f2c9a1e… --json
```

## Tests

`tests/unit/test_agents.py` builds envelopes from hand-written tool histories, so
the provenance rules are tested without a model or a lake: a refusal must carry the
contract that caused it, a clarification must not carry SQL it never ran, a product
must be cited with its version. The end-to-end wiring is covered in
`tests/smoke/test_smoke.py`.

## Where this lands in production

- The envelope is the HTTP response body, the audit record, and the evaluation
  input. One shape, three uses.
- Answered envelopes are retained to DynamoDB; `/answers/{trace_id}` replays them.
- Telemetry emits `ANSWERS`, `REFUSALS`, `NEEDS_CLARIFICATION`, `EVIDENCE_COMPLETE`,
  `ANSWER_LATENCY_MS`, `TOOL_CALLS`, token counts and `EVAL_SCORE`
  ([14](14-observability.md)).

## Invariants a change must not break

1. Every answer has a `trace_id` and a `contract_digest`; without them it is not
   auditable and does not count as complete.
2. `REFUSED` carries no SQL.
3. `NEEDS_CLARIFICATION` carries no SQL it never executed.
4. `is_complete` is false for `INCOMPLETE` — no partial answer masquerading as one.
5. A tool call in `trace` means a tool call actually happened.
