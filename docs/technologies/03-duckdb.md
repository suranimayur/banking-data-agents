# 03 — DuckDB: the in-process OLAP engine

**Level:** beginner → advanced · **You will be able to:** run real analytical SQL
with no server, and explain how that lets the same code target a laptop and Athena.

## The idea

For thirty years, analytical SQL required a *server*. You installed PostgreSQL or
Snowflake or Redshift, loaded data into it, and connected over a socket. This gave
you concurrency and scale, and it charged you in operational complexity: a service
to run, a schema to migrate, a network to secure, and a cold start before the first
query.

Most analytical work does not need a server. A data engineer exploring 2 GB on a
laptop needs an engine, not a platform. DuckDB is that engine: an **in-process OLAP
database** — no daemon, no port, no users, no `CREATE DATABASE` ceremony.

## The mental model

Compare it to SQLite, then replace the word "transactional" with "analytical".

| | SQLite | DuckDB |
|---|---|---|
| Shape | in-process library | in-process library |
| Workload | many small writes, row lookups | few large reads, aggregations |
| Storage | its own file | its own file **or Parquet/CSV directly** |
| Concurrency | many readers/one writer | many readers/one writer |
| Best at | "get row 42" | "GROUP BY over 50M rows" |

That last row is the whole reason DuckDB exists. It uses the same **vectorised,
columnar, pushdown** execution model as Parquet and Arrow from
[chapter 02](02-columnar-data-polars-arrow-parquet.md), which is why it can read a
Parquet file and answer a `GROUP BY` without importing the data first.

The killer property for this project:

```sql
-- No load step. The Parquet files ARE the table.
SELECT region, COUNT(*) FROM 'data/lake/gold/customer_360/*.parquet' GROUP BY 1;
```

DuckDB scans globs of Parquet directly. There is no "import the data" phase, so
there is nothing to keep in sync and nothing to forget to refresh.

## The minimum you need

```python
import duckdb

con = duckdb.connect()                       # in-memory; nothing to clean up
rows = con.execute("""
    SELECT region, COUNT(*) AS customers
    FROM 'data/lake/gold/customer_360/*.parquet'
    GROUP BY region
    ORDER BY customers DESC
""").fetchall()

# A reusable view over the lake, so SQL can say `gold.customer_360`.
con.execute("CREATE VIEW gold.customer_360 AS SELECT * FROM 'data/lake/gold/customer_360/*.parquet'")
```

In the CLI it is one binary:

```bash
duckdb -c "SELECT count(*) FROM 'data/lake/gold/transaction/*.parquet'"
```

## In this project

`pipeline/engine.py` defines the seam that makes the whole repository dual-target:

```python
class QueryEngine(Protocol):
    name: str
    def register_all(self) -> None: ...
    def query(self, sql: str) -> ...: ...

class DuckDBEngine:   # name = "duckdb"  — local, in-process
class AthenaEngine:   # name = "athena"  — cloud, over S3 + Glue

def get_engine(lake=None) -> QueryEngine: ...
```

Read that Protocol carefully, because it is the design decision the rest of the
project is built on. **Everything above this line speaks SQL and receives rows.**
Agents, tools, the guardrail, the API and the console never know which engine is
underneath. `get_engine` is one of only three places in the codebase that branch on
the backend (`get_lake` and `get_settings` are the others).

This is why the same 372 tests can run without Docker: `DuckDBEngine` is a complete,
honest implementation, not a mock. When Floci is up, `AthenaEngine` takes its place
and the *tests do not change* — they just exercise different bytes.

A second, subtler benefit: the **SQL guardrail in
[chapter 04](04-sqlglot-and-the-sql-guardrail.md) validates before execution**, so a
query that would be rejected by Athena is rejected locally too. You cannot pass
locally and fail in production on a syntax or dialect issue, because the check is
static.

## Level up

**DuckDB is not "production".** It is single-process and single-writer. It is the
right answer for local development, CI and single-node analytics up to a few hundred
GB. It is the wrong answer for a concurrent API serving a thousand analysts. This
project uses it exactly where it belongs — never as the deployed engine.

**`register_all` is a testability decision.** By materialising views once and
reusing them, the engine avoids re-globbing S3 or the filesystem on every query, and
it gives the guardrail a stable set of schemas (`gold`, `ops`) to allow.

**Connected tables vs file functions.** `read_parquet('a.parquet')` and a DuckDB
*table* behave differently under concurrent writes and on schema drift. Views over
globs are the middle ground used here.

**What to learn next:** the DuckDB extensibility model (Iceberg, Delta, Postgres
scanner), `EXPLAIN ANALYZE` to see pushdown happening, and Athena's engine
difference — Athena is *Presto/Trino*, which is not DuckDB, so dialect care matters
and the project's
[SQLLint via SQLGlot](04-sqlglot-and-the-sql-guardrail.md) is the guard.

## Try it

```bash
cd banking-data-agents
uv run bda pipeline run

# The same numbers the API returns, straight from DuckDB.
uv run python -c "
from banking_data_agents.pipeline.engine import get_engine
e = get_engine()
e.register_all()
for row in e.query('SELECT region, COUNT(*) c FROM gold.customer_360 GROUP BY 1 ORDER BY c DESC LIMIT 5'):
    print(row)
print('engine:', e.name)
"
```
