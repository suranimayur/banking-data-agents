# 07 · The query planner

## Concept

The model's job is to choose **tools**, not to write SQL. But "turn this sentence
into a metric, a grouping and a window" is still a real problem — and it is a
*deterministic* problem, so it should be solved deterministically.

`semantic/planner.py` is a parser over the question and the metric vocabulary. It
is the reason two identical questions produce byte-identical SQL, which is what
makes evaluation and caching possible at all.

## What it resolves

| Element | Example | Result |
|---------|---------|--------|
| Metric | "how many customers" | `metric.customer_count@1.0.0` |
| Grouping | "by region" | `GROUP BY "region"` |
| Superlative / top-N | "the 5 regions with the most customers" | `ORDER BY … DESC LIMIT 5` |
| Entity-scoped top-N | "which customers hold the most accounts" | top-N over the entity, not the metric |
| Time window | "last 3 months" | a predicate, **not** a grouping key |
| Ambiguity | "what is our exposure?" | a clarification request |

## The decisions that took the longest

These are worth recording because they look like details and are actually the
design.

**`last 3 months` is not a grouping key.** It reads like one and a naive parser
will `GROUP BY` it, silently producing a different query from the one the analyst
asked for. The planner treats temporal phrases as filters.

**Non-contiguous metric phrases.** "How many customers do we have **by region**
with more than two accounts" puts the grouping between two parts of the metric
phrase. The parser resolves the metric first and removes its span before looking
for the grouping.

**Duplicate `COUNT` dedup.** "How many customers, and how many customers per
region" would otherwise produce two aggregates over the same fragment. Identical
fragments are collapsed.

**Bare entity nouns.** "Who spends the most on cards?" has a metric but no explicit
aggregate phrase; the entity noun resolves to the product's transaction count.

**"consumed" and similar.** A token that appears in a synonym but not at a word
boundary once caused a mis-parse; the fix was to match on boundaries and to keep
the synonym list honest rather than to special-case the token.

**Ambiguity is preserved, not resolved by guessing.** "What is our exposure?" has
several defensible readings. The planner returns a clarification request with the
candidates, and the envelope's outcome is `NEEDS_CLARIFICATION`. An agent that
guesses here is an agent that will be confidently wrong, and a confidently wrong
number is worse than no number.

**The anchor date comes from the data, not the clock.** `tools/context.py::data_anchor`
resolves "last 3 months" against the maximum `transaction_date` present in gold.
A dataset generated last quarter still answers questions about *its own* most
recent quarter, which is what makes the tests time-independent.

## Determinism is the property

Two runs of the same question produce the same SQL. That gives:

- **Testability.** Evaluation cases can assert on SQL shape.
- **Explainability.** The generated SQL is shown to the analyst.
- **Auditability.** `bda answers get <trace>` reproduces exactly what was run.
- **Safety.** The planner can only emit shapes it knows how to emit, which is a
  much smaller space than "SQL".

## Where it sits in the flow

```
question
  └─ resolve_metric      → metric fragments + owners
  └─ generate_sql        → the planner composes one statement
       └─ validate_sql   → tools/sqlguard.py ([08])
            └─ execute_sql → DuckDB or Athena
```

Note that `validate_sql` is a separate tool the agent must call. If it does not,
`execute_sql` validates anyway — the guard is not optional
([08 SQL guardrails](08-sql-guardrails.md)).

## Testing it

`tests/unit/test_planner.py` holds the phrasing table: each entry is a sentence and
the query it must produce. The cases that matter most are the ones that were once
wrong — the temporal-phrase, non-contiguous and bare-entity cases above are all
regression tests, not illustrations.

```bash
uv run pytest tests/unit/test_planner.py -q
```

## Where this lands in production

The planner runs inside the agent process, so its behaviour is versioned with the
image. The evaluation gate ([11](11-evaluation-and-release-gates.md)) is what stops
a planner regression from shipping: a phrasing that used to resolve and now
clarifies will drop the score, and the gate fails.

## Invariants a change must not break

1. Deterministic: same question in, same SQL out.
2. The planner never emits SQL that `validate_sql` would reject.
3. Every metric that reaches a query comes from YAML, with its version and owner.
4. Ambiguity produces a clarification, never a guess.
5. Relative time windows resolve against the data anchor, not the wall clock.
