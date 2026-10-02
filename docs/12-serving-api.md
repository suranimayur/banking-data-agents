# 12 · The serving API

## Concept

The API is a **transport**. Its job is to lose nothing and invent nothing between
the agent and the caller. Every design decision below follows from that: the
response body *is* the evidence envelope, a refusal is a 200, and the only state it
holds is the retention store and one guarded agent per name.

`api/app.py`, FastAPI + Uvicorn.

## Endpoints

| Method | Path | Returns |
|--------|------|---------|
| GET | `/health` | Liveness, provider, product count, agents, lake, catalog digest, answer store, retention count |
| GET | `/products` | Every published product with row count |
| GET | `/products/{product}` | One contract: version, primary key, columns, quality, SLA, `not_allowed_use` |
| GET | `/quality` | Platform-wide quality and freshness |
| GET | `/agents` | Agents, their tool surfaces, and their restrictions |
| POST | `/ask` | Ask an agent; returns the envelope |
| GET | `/answers/{trace_id}` | Replay a retained answer |

Every response carries an `x-request-id` header, assigned by middleware, so an HTTP
request and an answer trace can be correlated in logs.

```bash
uv run bda serve --port 8000

curl localhost:8000/health
curl -X POST localhost:8000/ask \
  -H 'content-type: application/json' \
  -d '{"question":"How many customers do we have by region?"}'
```

## The request

```json
{
  "question": "How many customers do we have by region?",
  "agent": "copilot"
}
```

Pydantic validates it: an empty question is a **422**, an unknown agent is a
**400** with `unknown agent` in the detail — a default agent would be a silent
downgrade of a governance decision, which is exactly the wrong behaviour.

## The response

The body is the envelope ([10](10-the-evidence-envelope.md)). The two things to
notice:

**A refusal is a 200.** `outcome: "REFUSED"`, `sql: null`, `governance.allowed:
false`. A 4xx would imply the caller did something wrong; they asked a reasonable
question to which the answer is "no, and here is the clause".

**`agent` is stamped on the payload.** The `AgentPool` records which agent produced
the answer, and `cli_ask.py` does the same, so a retained answer knows its
provenance even when the caller did not name an agent explicitly.

## State

`AgentPool` builds **one agent per name**, lazily, behind a per-name lock. A Strands
agent holds conversation state, so a pool of one-per-name-with-a-lock is the
smallest correct concurrency model: `acquire(name)` returns the instance *and its
lock*, and the caller holds the lock for the duration of the turn. Tests assert that
two acquires return the same object and the same lock.

`AnswerStore` is a seam:

- `InMemoryAnswerStore` — a bounded LRU (`OrderedDict` with move-to-end on read),
  used locally and in tests. Reading an answer keeps it alive; that is deliberate,
  because the thing being asked about is the thing worth keeping.
- `DynamoDBAnswerStore` — used when `BDA_ANSWER_TABLE` is set
  ([10](10-the-evidence-envelope.md)).

`default_answer_store()` chooses, and `/health` reports which one is in force, so a
deployment that silently lost its audit trail is visible from the outside.

`create_app()` is a factory so tests get isolated stores and pools; a module-level
singleton would make the suite order-dependent.

## Tests

`tests/unit/test_api.py` covers the transport's honesty:

- the envelope that goes out is the one the agent produced;
- `[entry["tool"] for entry in trace] == tools_called`;
- a refusal is a refusal, not an error;
- replaying by trace id returns the same SQL, answer and digest;
- the holdout cannot be reached **over HTTP** either;
- the pool reuses one instance per name;
- retention is bounded and is LRU, not FIFO.

`tests/smoke/test_smoke.py` adds the full journey: ask over HTTP, then replay.

## Where this lands in production

- `infra/stacks/gateway.py` fronts the service; `agent_runtime.py` runs it.
- Health and latency feed the CloudWatch dashboards ([14](14-observability.md)).
- The request id ties an HTTP log line, a retained answer, and an agent trace
  together — which is the whole point of having one.

## Invariants a change must not break

1. The response body is the envelope. No reshaping, no summarising.
2. A refusal is a 200 with `sql: null`.
3. An unknown agent is a 400; there is no default agent.
4. `/health` reports which answer store is active.
5. One agent instance per name, guarded by one lock.
