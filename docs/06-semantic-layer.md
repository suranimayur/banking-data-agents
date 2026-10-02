# 06 · The semantic layer

## Concept

"Total balance" means something specific at this bank: which accounts count, how
remediated NULLs are treated, whether it is gross or net. If that definition lives
in an analyst's head, it lives in a hundred spreadsheets. If it lives in a prompt,
the model will paraphrase it. It has to live in **one versioned fragment of SQL**
with a named owner, so that when a number is disputed there is something to point
at.

`semantic/metrics/*.yaml` is that place. **28 metrics** across the three products,
loaded by `semantic/metrics.py`.

## The shape of a metric

```yaml
product: customer_360
metrics:
  - name: total_balance
    version: 2.1.0
    owner: customer-insights@bank.example
    definition: >-
      Sum of balances across a customer's accounts. Balances that were nulled by
      remediation are treated as zero, so a customer with a data incident is
      understated rather than inflated.
    unit: INR
    aggregate: sum
    sql_fragment: SUM(total_balance)
    synonyms: [balance, total balance, deposits, balances]
    caveats:
      - Nulled balances count as zero; accounts under dispute are excluded.
```

Every field is load-bearing:

| Field | Used by |
|-------|---------|
| `name`, `version` | The answer's `metrics` list: `metric.customer_count@1.0.0` |
| `owner` | Who to ask when the definition is wrong |
| `definition` | Returned to the analyst; the reason the number is defensible |
| `unit` | Formatting and sanity checks |
| `aggregate` | Whether a grouping is legal at all |
| `sql_fragment` | The **only** aggregate text that reaches the query |
| `synonyms` | The planner's vocabulary ([07](07-the-query-planner.md)) |
| `caveats` | Carried into the answer so the caveat cannot be lost |

## Why the agent never writes a formula

The tool `resolve_metric` takes a phrase and returns metric fragments. The tool
`generate_sql` composes: `SELECT <grouping>, <fragment> FROM gold.<product> GROUP
BY <grouping> LIMIT <ceiling>`, with the fragment taken verbatim from the YAML.

The consequences are worth stating plainly:

- A wrong number is traceable to one metric version and one owner.
- A metric can be **deprecated** by removing it, and every agent loses it at once.
- The metric inventory is enumerable — `/products/{product}` shows it — so "what
  can this thing actually answer?" has an answer.
- No amount of adversarial phrasing produces a `SUM` over a column nobody defined.

## The metrics, by product

| Product | Examples | Notes |
|---------|----------|-------|
| `customer_360` | `customer_count`, `account_count`, `total_balance` | Balance semantics carry a caveat about remediation |
| `transaction` | transaction count/volume/average | Time-windowed |
| `credit_risk` | `dti`, `obligation_monthly`, `total_outstanding` | `dti` is a **baseline**, excluding any proposed facility |

## Running and inspecting

```bash
uv run bda catalog list                  # products and their metrics
uv run bda catalog customer_360          # one product in detail
```

From Python:

```python
from banking_data_agents.semantic.metrics import reload_metrics, resolve_metric
```

`reload_metrics()` exists so tests can change the metric set and have the change
take effect within the process; it is also part of the shared test cache reset.

## Adding a metric

1. Add the block to `semantic/metrics/<product>.yaml` with a version and an owner.
2. If the phrase an analyst would use is not already a synonym, add it — but be
   careful: synonyms are global enough to cause collisions, and the planner
   resolves ambiguity by *asking*, not by guessing.
3. Run `uv run bda ask "<the phrasing you expect>"` and check that the answer cites
   the new metric.
4. Add an evaluation case for at least one phrasing ([11](11-evaluation-and-release-gates.md)).
5. `uv run bda eval`.

## Where this lands in production

Metric YAML is source-controlled and deployed with the application image, so the
metric set an environment can answer from is exactly the metric set in the commit
that built it. `EVAL_SCORE` and the evidence envelope's `metrics` list make it
possible to see, from telemetry, which metrics are actually being used — which is
how a metric gets retired instead of lingering.

## Invariants a change must not break

1. `sql_fragment` is a fragment, never a whole statement, and never contains a
   `FROM`, a `;`, or a subquery.
2. Removing a metric is the deprecation mechanism; do not keep a metric "just in
   case" with a formula nobody stands behind.
3. Two metrics in one product must not share a synonym unless the ambiguity is
   intentional and the planner can ask about it.
4. Every metric has a non-empty `definition`; a number with no definition is not
   governed.
