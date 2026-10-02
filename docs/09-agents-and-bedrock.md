# 09 · Agents and Bedrock

## Concept

An agent here is a **small, closed loop**: a system prompt that describes a role,
ten read-only tools, a hard budget, and an evidence assembler that turns whatever
happened into an auditable envelope. The agent cannot write SQL, cannot write to
the lake, and cannot exceed its budget. It is a reasoning layer over a governed
query surface, not a general-purpose assistant.

## The three agents

| Agent | Role | Notable restriction |
|-------|------|---------------------|
| `copilot` | Answer governed questions about the data products | Full ten-tool surface |
| `fraud` | Population-level fraud analysis | **Forbidden dimension `customer_id`** — no individual ranking |
| `credit` | Credit-risk analysis | Restricted to the `credit_risk` metrics |

`agents/registry.py::AGENTS` maps names to classes; the same names are used on the
command line, in the API and in evaluation suites, deliberately: a name that means
different things in different surfaces is how a governance test ends up testing
nothing.

## The tool surface

```
search_catalog · list_products · get_contract · resolve_metric ·
check_allowed_use · generate_sql · validate_sql · execute_sql ·
explain_quality · trace_lineage
```

`tools/registry.py::TOOL_NAMES` is the list and `READ_ONLY_TOOLS` is asserted equal
to it in tests: every tool an agent can reach is read-only, and that is a property
the suite checks rather than a claim the documentation makes.

Every tool is built by `build_tools(ctx)`, bound to one `ToolContext` per
conversation, and every result goes through one `emit()` helper. That helper is
where two real bugs were fixed:

- **JSON-ness is enforced.** A tool that returns a dict with a non-JSON value (raw
  `datetime` from an SLA computation) makes Strands fall back to `str()`, and the
  model then receives something it cannot read as data. `tools/impl.py::_jsonable`
  converts `Decimal`, `date` and `datetime` before anything leaves the tool.
- **Nothing prints to stdout.** A stray `print("Creating Strands tool…")` corrupted
  `bda ask --json`, because stdout *is* the answer for that command. Diagnostic
  output goes through `logging_setup.py`, never `print`.

Both are documented here because both are the kind of mistake that only shows up in
one invocation mode, and both have regression tests.

## Strands, and the details that bite

The runtime is **Strands Agents 1.57.x**:

- `@tool` has the signature `(func=None, description=None, inputSchema=None, name=None, context=False)`.
- The decorated function is a `strands.tools.decorator.DecoratedFunctionTool` with
  `.tool_name` and `.tool_spec`, and is directly callable — which is how the unit
  tests exercise tools without a model.
- **Tool results are delivered to the model as `user` messages.** This surprises
  people writing prompt tests; assert on the agent's *answer*, not on a transcript
  that assumes assistant messages.
- `Agent` returns an `AgentResult` with `message`, `metrics`, `state`,
  `stop_reason`, `structured_output`, `context_size` and `interrupts`. The evidence
  envelope reads `message`, `metrics` and `stop_reason`.
- `BedrockModel.__init__` accepts `boto_session`, `boto_client_config`,
  `region_name`, `endpoint_url`, `api_key` and `**model_config` — which is how the
  same agent talks to real Bedrock or to Floci's Bedrock emulation with no code
  change.

## The provider abstraction

`LLM_PROVIDER` decides which model backs an agent:

| Value | What runs | When |
|-------|-----------|------|
| `stub` | `llm/stub.py::StubModel`, a deterministic scripted provider | Tests and CI — free, reproducible, no network |
| `floci` | `BedrockModel` pointed at the emulator's `endpoint_url` | Local end-to-end with an emulated Bedrock |
| `bedrock` | `BedrockModel` against real Bedrock | A deployed environment |

`llm/factory.py` builds and caches models, with `reset_models()` for tests. The
agents above this seam never know which provider is in play — which is why the
evaluation suite can run in CI for free and still be meaningful.

## Model tiers

`Settings.model_for(tier)` resolves three logical tiers to concrete Bedrock models:

| Tier | Default model | Used for |
|------|---------------|----------|
| `router` | Claude 3.5 Haiku | Intent classification, cheap turns |
| `reasoner` | Claude 3.5 Sonnet | The main tool-using loop |
| `escalation` | Claude 3 Opus | The hard cases, deliberately rare |

Tiers exist so cost can be managed by moving a tier, not by rewriting prompts.

## Budgets and stop conditions

| Setting | Default | Effect |
|---------|--------:|--------|
| `agent_max_tool_calls` | 8 | A loop that keeps calling tools is stopped |
| `agent_max_tokens` | 60,000 | Context ceiling |
| `agent_max_wall_clock_s` | 45 | A slow answer is a failed answer |
| `session_budget_usd` | 0.50 | Per-conversation spend |
| `user_daily_budget_usd` | 5.00 | Per-user daily spend |

A budget stop produces `INCOMPLETE`, not a plausible half-answer. That distinction
is enforced in `agents/evidence.py` and is one of the evaluated behaviours.

## Running an agent

```bash
uv run bda ask "How many customers do we have by region?"
uv run bda ask "How many customers do we have by region?" --json
uv run bda ask "Who spends the most on cards?" --agent fraud   # refused by policy
```

## Where this lands in production

- `infra/stacks/agent_runtime.py` runs the agents as a container, one endpoint per
  environment's `live` version.
- `infra/stacks/gateway.py` exposes the API; `guardrails.py` adds input/output
  guardrails.
- Promotion and rollback are canary-based ([11](11-evaluation-and-release-gates.md)).
- Bedrock calls are the only external dependency of an answer; Floci's Bedrock
  emulation means the same path is exercised without a live account.

## Invariants a change must not break

1. Tool results are JSON-serialisable. No exceptions, no `str()` fallbacks.
2. Nothing writes to stdout except the command's own output.
3. Every agent's tool list is read-only, and the tests compare it to `TOOL_NAMES`.
4. A budget or timeout stop yields `INCOMPLETE`, never a partial answer presented
   as complete.
5. The provider is chosen by configuration; no agent code branches on it.
