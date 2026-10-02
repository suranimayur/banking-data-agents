# 10 — FastAPI and ASGI

**Level:** beginner → advanced · **You will be able to:** explain what ASGI is, why
concurrency dictates this service's design, and why the API returns exactly what the
CLI prints.

## The idea

WSGI — the interface Flask and Django were built on — has a shape that assumes one
thing: `function(request) -> response`, blocking, one at a time. It has no vocabulary
for "I am waiting on a network call, go do something else". Every database query
therefore occupies a worker thread that could have served ten other requests.

**ASGI** is the asynchronous successor: `async def app(scope, receive, send)`. It
models a request as a *stream of events*, so a coroutine can await a slow dependency
and yield the worker. A framework that speaks ASGI can hold thousands of concurrent
in-flight connections per CPU.

**FastAPI** is a framework on top of ASGI (via Starlette) with three properties that
matter here: it is built on the type system, so Pydantic models **are** the request
and response schemas; it generates OpenAPI documentation from them; and it is
dependency-injection shaped.

## The mental model

```
   uvicorn (ASGI server)
      │  accepts the socket, parses HTTP, hands over an event stream
      ▼
   Starlette
      │  routes + middleware
      ▼
   FastAPI
      │  validates the body against a Pydantic model
      ▼
   your async function ── await ──►  anything slow
      │
      ▼
   response model ──► serialised ──► JSON
```

The performance point is that the *server*, not your code, owns the concurrency. A
blocking call inside `async def` is the classic disaster: it stalls the event loop
for everyone. The fix is either a genuinely async client or an explicit
thread offload — a choice, not an oversight.

## The minimum you need

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class Ask(BaseModel):
    question: str

@app.post("/ask")
async def ask(body: Ask) -> dict:
    return {"question": body.question}
```

```bash
uvicorn module:app --port 8000
```

Because `Ask` is a Pydantic model, a malformed body produces a **422 with a
field path** before your code runs. The validation and the documentation come from
the same source.

## In this project

`api/app.py` defines the service, and its design is deliberately narrow.

### The contracts

```python
class AskRequest(BaseModel):  question: str; agent: str = "copilot"; ...
class AskResponse(BaseModel): ... the evidence envelope ...
```

The response is **the evidence envelope** ([chapter 10 of the playbook](../10-the-evidence-envelope.md)),
not a shaved-down `{"answer": "…"}`. That is the point of the service: HTTP transport
that **loses and invents nothing**. A caller gets the SQL, the metric versions, the
contract versions, the row preview, the quality state, the lineage, the governance
decision and the trace — the same object the CLI prints and the console renders.

### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness plus a *real* inventory: env, provider, lake, product count, degraded products, metric count, contract digest, agents, answer store |
| `GET /products` / `GET /products/{product}` | The catalogue and per-product contracts |
| `GET /quality` | Data-quality state across datasets |
| `GET /agents` | Each agent and its **resolved tool allowlist** |
| `POST /ask` | Ask a question; returns the envelope |
| `GET /answers/{trace_id}` | Replay a retained answer |

That `/health` is worth studying. A health check that only returns `{"status":"ok"}`
tells you the process is up. This one tells you **what it is serving** — the contract
digest, the metric count, and whether any product is `degraded`. A digest that
changes between deploys is a detectable event, not a mystery.

### The two indirections that keep it testable

```python
class AnswerStore(Protocol): ...
class InMemoryAnswerStore: ...          # local/CI
def default_answer_store():             # DynamoDB when configured, else in-process
```

and

```python
class AgentPool:                        # one guarded agent per name, reused
```

Both are Protocols for the same reason the `QueryEngine` in
[chapter 03](03-duckdb.md) is: **the API never learns which backend it is on.** The
DynamoDB store and the in-memory store satisfy one interface, so `/answers/{trace_id}`
is testable with no cloud and identical in behaviour.

`AgentPool` matters for a subtler reason: agents are **not** cheap to construct, and
they hold conversation history. Pooling one guarded agent per name means the tool
surface and guardrail policy are fixed at construction and cannot drift between
requests.

### Request-id middleware

Every request gets an id, and it is attached to the log records for that request.
Without it, concurrent traffic produces an unreadable log; with it, one `grep` gives
you the whole story of one call.

## Level up

**The event loop is a shared resource.** This service's work is CPU-light and I/O-
heavy (DuckDB query, DynamoDB write), so ASGI is a good fit. If an agent call were
genuinely blocking and slow, it belongs in a thread pool (`run_in_threadpool`) or a
separate worker — otherwise one slow request degrades every concurrent user.

**Statelessness is the deployment requirement.** The container must hold nothing that
is not reconstructible, because ECS will replace it without warning. Everything
durable lives in S3/DynamoDB. This is exactly why answer retention moved into a
store rather than a module-level dict.

**Idempotency keys** are the standard fix for "the client retried POST /ask and paid
for two model calls". Worth knowing before you need it.

**What to learn next:** dependency injection (`Depends`) for auth; `lifespan` for
startup/shutdown rather than import side effects; OpenAPI generation and what it
buys your consumers; and structured concurrency limits when a single request fans
out to several services.

## Try it

```bash
cd banking-data-agents
uv run bda serve --port 8137 &

curl -s localhost:8137/health
curl -s localhost:8137/agents
curl -s -X POST localhost:8137/ask -H 'Content-Type: application/json' \
  -d '{"question":"How many active customers do we have by state?"}'

# See the auto-generated docs.
#   open http://localhost:8137/docs
uv run pytest tests/unit/test_api.py -q
```
