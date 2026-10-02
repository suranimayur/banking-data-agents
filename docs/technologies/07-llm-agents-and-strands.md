# 07 — LLM agents and Strands

**Level:** beginner → advanced · **You will be able to:** explain what a tool call
actually is, and why this project puts its guardrails below the model.

## The idea

A language model predicts tokens. That is all it does. It cannot count, it cannot
look anything up, and it cannot know your schema — so asking it directly for "revenue
last quarter" produces a plausible number with no relationship to reality.

The fix is not a better model. It is to stop asking the model for **answers** and
start asking it for **decisions**. Given a description of available tools, the model
outputs a structured request — *call `execute_sql` with this SQL* — and your
deterministic code does the actual work. The model chooses; your code computes.

That is an **agent**: a loop of (model proposes action → runtime executes → result is
returned → model proposes again) until it stops.

## The mental model

```
    ┌──────────────────────────────────────────────────────────┐
    │  the loop                                                │
    │                                                          │
    │   question ──► MODEL ──► "call generate_sql(…)"          │
    │                  ▲              │                        │
    │                  │              ▼                        │
    │              result  ◄──── YOUR CODE (deterministic)     │
    │                  │              │                        │
    │                  └──────────────┘                        │
    │                                                          │
    │            … repeat until the model stops asking …       │
    └──────────────────────────────────────────────────────────┘
```

The critical insight: **the model is on the untrusted side of the loop.** Everything
it produces is input to be validated, exactly like a request from the internet.
`execute_sql` does not trust the SQL the model chose — it runs
`validate_sql` on it first.

## The minimum you need

```python
from strands import Agent, tool

@tool
def add(a: int, b: int) -> int:
    """Add two integers."""      # ← the docstring IS the API contract for the model
    return a + b

agent = Agent(tools=[add], system_prompt="Use tools for arithmetic.")
result = agent("What is 2 + 2?")
print(result.message)
```

Three things to internalise:

1. **The docstring is the interface.** The model never sees your source — it sees the
   name, the docstring and the JSON schema. Vague docstrings cause wrong tool choice.
2. **Tools must return JSON-serialisable data.** A tool returning a `Decimal` or a
   `datetime` makes the framework fall back to `str()`, and the model then cannot
   parse it reliably.
3. **The loop is bounded.** Without a limit, a confused model can call tools forever,
   at real cost. Budgets are not optional.

## In this project

### The tool surface

`tools/registry.py` declares ten read-only tools, deliberately ordered as a good
answer's path:

```
search_catalog → list_products → get_contract → resolve_metric → check_allowed_use
             → generate_sql → validate_sql → execute_sql → explain_quality → trace_lineage
```

`READ_ONLY_TOOLS` equals `TOOL_NAMES` — asserted in tests, because a *read-only*
agent has no business writes.

`build_tools(ctx, names)` narrows the surface per agent. **A smaller surface is a
smaller attack surface**, and it keeps a fraud investigator out of credit questions.
The narrowing is a property of the *configuration* (`tools_enabled` on each
`BaseAgent` subclass), not of the prompt.

### One machine, three job descriptions

`agents/base.py::BaseAgent` is the whole shared mechanism: a Strands `Agent` over a
narrowed tool surface, run under a budget, whose output is wrapped in an evidence
envelope. Putting it in one place is what makes the README's claims checkable rather
than aspirational:

- budgets are enforced in `_limits()`, via Strands' `Limits(turns=…, total_tokens=…)`
  — the tool-call budget plus one answering turn;
- the envelope is built here, so **no agent can forget provenance**;
- the tool subset is chosen here, so "the fraud agent cannot read the labels" is
  configuration rather than good intentions.

`copilot`, `fraud` and `credit` are the three instances
(`agents/registry.py::AGENTS`). They differ in prompt, tier, tool subset and
`forbidden_dimensions` — and nothing else.

### The bug worth knowing

A tool returning a dict with non-JSON-serialisable values (SLA timestamps) made
Strands fall back to `str()`, and the model could not parse the result. The fix is
`tools/impl.py::_jsonable`, which converts `Decimal`, `date` and `datetime` before
anything leaves a tool. **Any non-trivial agent framework will hit this**, and the
symptom is a model that "seems confused", not an exception.

A second one worth internalising: Strands delivers **tool results to the model as
`user` messages**. Reading a transcript without knowing that makes the conversation
look like the user is talking to themselves.

### The provider behind the model

`llm/factory.py` is the seam:

| Provider | What it is | Cost |
|----------|-----------|------|
| `stub` | `StubModel` — deterministic, offline | free |
| `floci` | Bedrock Runtime as emulated by Floci | free |
| `bedrock` | Real Amazon Bedrock | real tokens |

Nothing else in the codebase branches on provider. A change of provider changes
**which bytes leave the process**, not which code runs — which is the only way
"tested locally, runs in production" is an honest claim. CI and the evaluation suite
run on `stub`, so the gate is deterministic: the same question cannot produce two
different pass/fail verdicts.

## Level up

**Structural over instructional.** The recurring principle of this whole project:
anything that *must not* happen is enforced below the model, never requested in a
prompt. Prompts still say it — defence in depth is free — but the tests assert on the
guard. See [chapter 04](04-sqlglot-and-the-sql-guardrail.md) and
[the SQL guardrails chapter](../08-sql-guardrails.md).

**Temperature zero is a governance decision.** These agents must be reproducible and
auditable; the same question must not produce two different numbers for two people.
That is why `_MODEL_CONFIG` sets `temperature: 0.0`.

**Tool-call budgets interact with turn budgets.** Strands counts turns; a tool call
and the resulting model turn are separate. Getting the arithmetic wrong produces an
agent that always stops one step early.

**What to learn next:** the Model Context Protocol (MCP) as a way to expose tools
across processes; structured output / constrained decoding as a stronger alternative
to tool calling; and evaluation-driven prompt iteration, which is the subject of
[chapter 11 of the playbook](../11-evaluation-and-release-gates.md).

## Try it

```bash
cd banking-data-agents
uv run bda eval                     # 31/31 deterministic outcomes on the stub model
uv run bda ask "How many customers do we have by region?" --json

# Watch which tools the model chose, in order.
uv run python -c "
from banking_data_agents.agents import get_agent
e = get_agent('copilot').ask('How many customers do we have by region?')
print('outcome:', e.outcome)
for t in e.tools_called: print(' -', t)
"
```
