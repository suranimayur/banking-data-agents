# 04 — SQLGlot and the SQL guardrail

**Level:** intermediate → advanced · **You will be able to:** explain why the safest
way to inspect SQL is to parse it, and read this repository's guardrail as a
consequence of that idea.

## The idea

Suppose you must stop a user from deleting rows. The tempting implementation is a
string search:

```python
if "drop" in sql.lower():
    raise ValueError("not allowed")
```

This is defeated by `dRoP`, by a comment containing `dr` + `op`, by `/*drop*/`, by
`DROP` inside a string literal, by a Unicode homoglyph, and by any dialect that
spells it differently. A denylist on text is an arms race you lose, because SQL has
a **grammar** and you are matching characters instead of grammar.

SQLGlot parses SQL into an **abstract syntax tree** (AST): a typed, nested object
model of the query. Once you have a tree, "is this a `SELECT`?" is a property of the
root node, not a substring hunt. That distinction is the whole chapter.

## The mental model

```
"SELECT region, COUNT(*) FROM gold.customer_360 GROUP BY region"
                         │
                    sqlglot.parse_one(...)
                         ▼
        Select                                  ← the node type IS the answer
        ├── expressions: [Column(region), Count(*)]
        ├── from: Table(this=gold, db=customer_360)
        └── group: [Column(region)]
```

To ask "is this read-only?", inspect `isinstance(node, exp.Select)`. To ask "which
tables does this touch?", walk the tree collecting `exp.Table` nodes — and you get
the real identifier, not whatever quoting a caller used. To ask "does it have a
limit?", look for the `limit` child. Every question becomes structural.

SQLGlot also **transpiles** between dialects (`sqlglot.transpile(sql, read="duckdb",
write="athena")`), which is exactly what makes the dual-engine design in
[chapter 03](03-duckdb.md) defensible.

## The minimum you need

```python
import sqlglot
from sqlglot import exp

tree = sqlglot.parse_one("SELECT a FROM gold.t LIMIT 10", read="duckdb")

print(type(tree))                                   # <class 'sqlglot.expressions.Select'>
print(isinstance(tree, exp.Select))                 # True  → read-only
print({t.name for t in tree.find_all(exp.Table)})   # {'t'}
print(tree.args.get("limit") is not None)           # True
```

A parse failure raises — and **that is a feature**. If it does not parse, we do not
execute it.

## In this project

`tools/sqlguard.py::validate_sql` returns a `ValidationResult`, and it is called
**inside** `execute_sql`, not in the prompt. The checks, all AST-based:

| Rule | How it is enforced |
|------|--------------------|
| Exactly one statement | parse many; reject if `!= 1` — blocks stacked-query injection |
| Must be a `SELECT` | root node type; blocks DDL/DML entirely |
| Only `gold` and `ops` schemas | `ALLOWED_SCHEMAS` compared against parsed table qualifiers |
| Never the fraud holdout | `FORBIDDEN_DATASETS` contains `ops.fraud_ground_truth` |
| No `SELECT *` | no bare `exp.Star` in projections — forces explicit columns |
| Must have a `LIMIT` | `LIMIT` required, ceiling **1000** |
| No dangerous tokens | `FORBIDDEN_TOKENS`, as defence in depth |

Failures carry a stable `POLICY_VIOLATION_CODES` value, which matters more than it
looks: a *code* is testable and alertable, whereas a prose error message is neither.
The evaluation suite asserts on outcomes, and `tests/unit/test_sqlguard.py` covers
36 cases.

### Why "the guardrail, not the prompt"

Every agent's system prompt also says "only read gold, never write". That is
**defence in depth, and it is free**. But the prompt is not what the tests assert
on, because a prompt is a *request* to a stochastic system. The guard is
deterministic code on the only path that can touch data. This is the difference
between *instruction* and *enforcement*, and it recurs everywhere in this project:

- `agents/base.py::BaseAgent.forbidden_dimensions` — columns an agent may never
  group or rank by (the credit agent's behavioural columns), enforced by the
  guardrail on every query, so it holds whatever the model decides to do.
- `api/app.py` — the same `ToolContext` policy per agent, so the HTTP surface cannot
  be used to escape the agent surface.
- The eval suite has a case per refusal, so a refactor that quietly removes an
  enforcement path turns the gate red.

## Level up

**Parsing is not sandboxing.** SQLGlot gives you structure; it does not give you
safety. The guard reduces the blast radius (read-only, bounded, no holdout) and the
*identity* is still what enforces access — IAM on Athena, Lake Formation grants. The
guard is a correctness and governance tool, not a substitute for permissions. See
[the security chapter](../17-security-and-compliance.md).

**Time-of-check/time-of-use.** Validate and execute must be the same query object.
If you validate a string and then re-serialise it, you have introduced a gap. This
repository validates the produced SQL and executes that same SQL.

**What to learn next:** `sqlglot.optimizer` (qualify, pushdown, simplify),
dialect differences waiting to bite you (Athena/Trino's `LIMIT` vs `FETCH`,
DuckDB's `QUALIFY`), and why `exp.Table` may have a `catalog`, `db` **and** `this`
part — the source of most "but it looked fine" schema bugs.

## Try it

```bash
cd banking-data-agents
uv run python -c "
from banking_data_agents.tools.sqlguard import validate_sql
for sql in [
    'SELECT region, COUNT(*) FROM gold.customer_360 GROUP BY region LIMIT 10',
    'SELECT * FROM gold.customer_360 LIMIT 10',
    'SELECT COUNT(*) FROM ops.fraud_ground_truth LIMIT 10',
    'DROP TABLE gold.customer_360',
    'SELECT region FROM gold.customer_360',
]:
    r = validate_sql(sql)
    print(('OK  ' if r.ok else 'DENY'), (r.code if not r.ok else ''), '|', sql[:60])
"
```
