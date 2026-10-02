# 02 — Columnar data: Polars, Arrow and Parquet

**Level:** beginner → advanced · **You will be able to:** explain why a 1.08M-row
aggregation finishes in under two seconds, and read a Parquet file's metadata to
prove it.

## The idea

A CSV file is a sequence of rows. When a computer reads it, it reads *every field of
every row* in order, even if you only want one column. That is fine for 5,000 rows
and hopeless for 500 million.

Three inventions, stacked, fixed this:

1. **Columnar storage (Parquet).** Store values column-by-column, not row-by-row.
2. **A columnar memory layout (Arrow).** Keep them in that shape in RAM too.
3. **A vectorised engine (Polars).** Operate on whole columns at once, in optimised
   native code, instead of one Python-level value at a time.

They are separate concerns, which is why they are separate chapters-worth of ideas
even though they arrive as one experience: reading a wide CSV, filtering it, and
getting an answer before the coffee cools.

## The mental model

**Rows are how humans read data. Columns are how machines should.**

```
Row-oriented (CSV)              Column-oriented (Parquet)
+----+-------+--------+         region:  [MH, MH, KA, ...]   ← read this alone
| id | region| amount |         amount:  [120, 340, 90, ...] ← one contiguous block
+----+-------+--------+
|  1 |  MH   |  120   |
|  2 |  MH   |  340   |
|  3 |  KA   |   90   |
```

`SELECT region, SUM(amount) GROUP BY region` touches two columns. In the columnar
file it reads two contiguous blocks and never sees the other twenty-two. In the CSV
it reads all twenty-four and throws away twenty-two. That is the whole performance
story, and it is why the medallion lake in this repository stores **Parquet, not
CSV**, at every layer.

Two properties of Parquet do the heavy lifting beyond layout:

- **Column statistics (min/max per row-group).** The reader can skip a whole 128 MB
  chunk if `region`'s range there is `KA..KL` and you asked for `MH`. This is called
  *predicate pushdown* and it is why partition columns matter.
- **Schema embedded in the file.** No more guessing whether `amount` is a string.

**Arrow** is the in-memory sibling. Its real contribution is not speed but
*zero-copy interchange*: Polars, DuckDB, Pandas and PyArrow can all point at **the
same memory buffer**, so moving data between libraries costs nothing. Before Arrow
every transfer was a serialize/deserialize pair.

## The minimum you need

```python
import polars as pl

# Eager: a DataFrame, computed now.
df = pl.read_parquet("data/lake/gold/customer_360/*.parquet")

# Lazy: a query plan, computed when you ask. This is the important one.
result = (
    pl.scan_parquet("data/lake/gold/*.parquet")   # no file is read yet
    .filter(pl.col("region") == "MH")             # pushed down to the reader
    .group_by("segment")
    .agg(pl.col("customer_id").n_unique().alias("customers"))
    .sort("customers", descending=True)
    .collect()                                    # now it reads, and only what it needs
)
```

`.lazy()` / `scan_*` is the single highest-value habit. Eager Polars reads
everything then filters; lazy Polars builds a plan, applies **projection pushdown**
(don't read unused columns) and **predicate pushdown** (don't read filtered-out
rows), and only then reads. Same API, structurally less I/O.

## In this project

`pipeline/lake.py` is where Arrow and Parquet meet. Its signature move is
`normalise_frames`, which coerces every frame to a declared schema **before** it is
written, so no downstream consumer has to guess. The pipeline writes Parquet at
bronze, silver and gold.

The sizes make the point. The generator produces **1,220,122 rows across 108
datasets** in about **2.9 seconds**, and the full medallion run (bronze → silver →
gold → publish, including data quality and lineage) finishes in about **9.5
seconds** on this laptop. Doing that in row-oriented Python would be minutes, and
the difference is almost entirely columnar layout plus vectorised execution.

Polars also gives the project something subtler than speed: **expressions that are
data, not strings**. `pl.col("region") == "MH"` is a typed expression object that
the engine can inspect, reorder and optimise. That property is what makes the same
idea work later in [04 — SQLGlot](04-sqlglot-and-the-sql-guardrail.md), where SQL is
parsed into a tree the platform can reason about.

## Level up

**Why partition columns are chosen so carefully.** Writing
`region=Maharashtra/part-0.parquet` turns a full scan into reading one directory.
The cost is that you can never cheaply rewrite a single customer across regions. In
this repository the transaction table is the only one with real partition pressure
(1.08M rows), and the choice is documented in
[the lake chapter](../04-the-lake-and-medallion.md).

**Small files are a real production problem.** Every Parquet file has overhead — a
footer, statistics, an S3 GET. Ten thousand 4 KB files are dramatically slower than
ten 4 MB files, and Athena bills per byte scanned, so file size is a *cost* lever,
not just a speed one.

**Arrow is an interface, not a format.** The Arrow IPC format exists, but the
valuable part is the in-memory spec. Recognising "this library speaks Arrow" is how
you know two tools can interoperate without a conversion step.

**What to learn next:** Parquet row groups and page-level statistics; dictionary
encoding; why `pyarrow` is in the dependency list even though Polars has its own
reader (interop and `moto`/Athena paths); DuckDB's Parquet reader, which is the
subject of the next chapter.

## Try it

```bash
cd banking-data-agents
uv run bda data generate           # writes ~33 MiB across 108 datasets
uv run bda pipeline run            # bronze → silver → gold, ~9.5 s

# Look at what Parquet actually stored, without loading the data.
uv run python -c "
import pyarrow.parquet as pq
f = pq.ParquetFile('data/lake/gold/transaction/part-0.parquet')
print('rows:', f.metadata.num_rows)
print('row groups:', f.metadata.num_row_groups)
print('columns:', f.metadata.num_columns)
print('size MiB:', round(f.metadata.serialized_size / 1e6, 2))
"
```
