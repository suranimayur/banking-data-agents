# 12 — Terminal UX, logging and resilience

**Level:** intermediate → advanced · **You will be able to:** build a CLI people
enjoy, keep machine-readable output clean, and make flaky calls survivable — each
with the right tool.

Four small libraries, one shared theme: **the same program has two audiences.**

```
   a human at a terminal          a pipeline reading stdout
   ───────────────────────        ────────────────────────
   colour, tables, spinners       one JSON object, nothing else
```

Every mistake in this chapter comes from forgetting the second audience. The others
all follow from getting that boundary right.

## Rich — human output

**The idea.** `print("field: value")` does not align, wrap, colour or paginate. Rich
turns text into *renderables*: tables, panels, trees, progress bars, syntax-
highlighted code, and it degrades gracefully when output is not a TTY.

```python
from rich.console import Console
from rich.table import Table

console = Console()
table = Table(title="Data products")
table.add_column("Product"); table.add_column("Rows", justify="right")
table.add_row("customer_360", "2,000")
console.print(table)
```

Rich is a **presentation** library. It decides nothing and it must never be in the
path of data.

## structlog — machine output

**The idea.** `logging.info("user %s asked %s", u, q)` produces a string. Structured
logging produces an **event with named fields**, which is the difference between
grepping a log and querying one.

```python
from banking_data_agents.logging_setup import get_logger
log = get_logger(__name__)

log.info("agent_answered", agent="copilot", trace_id="714301eb8603",
         rows=10, outcome="ANSWERED_FROM_DATA", latency_ms=3310)
```

That line is parseable, filterable and aggregatable. In this repository it is what
makes `grep -v "debug    \]"` a useful development habit and CloudWatch Insights a
useful production one.

## Tenacity — retries that are honest

**The idea.** Networks fail transiently. A naive retry loop hides the difference
between "slow down" and "this will never work". Tenacity gives you declarative
policy: which exceptions, how many attempts, what backoff, what to do on exhaustion.

```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=0.5, max=8),
       retry=retry_if_exception_type(ThrottlingException), reraise=True)
def call_model(): ...
```

The three rules worth internalising: **retry only transient errors** (never a
validation failure), **always use backoff with jitter** (synchronised retries are a
self-inflicted DDoS), and **bound the total time**, not just the attempts.

## httpx — one client for HTTP

**The idea.** `requests` is synchronous and unmaintained-ish; `httpx` offers the same
ergonomics with async support and HTTP/2, which matters inside an ASGI service
([chapter 10](10-fastapi-and-asgi.md)). This project uses it for the emulator health
probe in `aws.py::_probe`, where a **short timeout cached for the process lifetime**
means an emulator that is down costs one timeout rather than one per call.

## In this project — and the bug that defines the chapter

Here is the failure that ties Rich and structlog together, and it is a real one that
happened in this codebase:

> A `print("Creating Strands tool...")` inside library code corrupted
> `bda ask --json`. The JSON on stdout was no longer parseable by a consuming
> program. The fix was to route that message to the logger.

The lesson generalises into the rule the whole project follows:

> **stdout is a data channel; stderr and the log are diagnostics channels.**

Consequences, visible throughout the code:

- Diagnostics go through `logging_setup.py` to the log, with the level configurable.
  `--json` output therefore remains byte-clean.
- `agents/base.py` constructs the Strands `Agent` with `callback_handler=None`, with
  the comment: *"Streaming prints from inside a library make an API response
  unreadable."* A library deciding to print is a library breaking its consumers.
- `bda ask` exits **non-zero** on an incomplete answer, so CI treats "the agent
  shrugged" as a failure rather than a log line nobody reads. Exit codes are an
  interface too.

That last point is a design principle in its own right: **a command's exit status is
part of its API**, and the most useful exit status is the one that makes a bad
outcome noisy.

## Level up

**Respect the TTY.** Rich detects non-interactive output and strips colour; do not
fight it, and never print progress bars into a log file.

**Redaction belongs in the logging config**, not at each call site. Configure once,
so no future developer can leak a token by forgetting a keyword argument.

**Retry budgets beat retry counts.** "Four attempts with 8s cap" can still exceed a
request deadline. Bound wall-clock time, and let the caller's deadline win.

**Correlation ids everywhere.** `trace_id` is per *answer* here; a request id is per
*HTTP request* ([chapter 10](10-fastapi-and-asgi.md)). Both are needed, and neither
substitutes for the other.

**What to learn next:** OpenTelemetry for traces that span services (the natural
successor to per-request ids), idempotency keys for safe retries of writes, and
circuit breakers, which are what you want *after* retries have repeatedly failed.

## Try it

```bash
cd banking-data-agents

# Human output (Rich).
uv run bda catalog list

# Machine output — must be pure JSON on stdout.
uv run bda ask "How many customers do we have by region?" --json | python -c "import json,sys; d=json.load(sys.stdin); print('parsed ok:', d['outcome'])"

# Diagnostics go to the log, not stdout.
uv run bda ask "How many customers do we have by region?" 2>/dev/null | head -3
```
