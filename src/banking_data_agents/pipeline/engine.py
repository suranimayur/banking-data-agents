"""Query engines.

Transformations are plain SQL referencing ``<zone>.<table>`` (for example
``silver.transactions``). Two engines implement that contract:

* :class:`DuckDBEngine` — local, in-process, reads parquet from the filesystem.
* :class:`AthenaEngine` — S3 + Glue + Athena, used against Floci or real AWS.

Because Floci's Athena is DuckDB-backed, the SQL dialect is the same in both
places; there is no second copy of the business logic.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import polars as pl

from banking_data_agents.config import get_settings
from banking_data_agents.logging_setup import get_logger
from banking_data_agents.pipeline.lake import ZONES, Lake, LocalLake, Zone

logger = get_logger(__name__)


class QueryEngine(Protocol):
    """Executes warehouse-style SQL over the lake."""

    name: str

    def register_all(self) -> None: ...
    def query(self, sql: str) -> pl.DataFrame: ...


# ---------------------------------------------------------------------------
# DuckDB (local)
# ---------------------------------------------------------------------------
class DuckDBEngine:
    """In-process analytical SQL. Reads parquet; needs no cloud."""

    name = "duckdb"

    def __init__(self, lake: LocalLake) -> None:
        import duckdb

        self._lake = lake
        self._conn = duckdb.connect(database=":memory:")
        # Deterministic and thread-safe for our single-process use.
        self._conn.execute("SET preserve_insertion_order=false")

    def register_all(self) -> None:
        for zone in ZONES:
            self._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {zone}")
            for table in self._lake.tables(zone):
                glob = self._lake.glob(zone, table)
                # The path is inlined rather than bound: DuckDB cannot prepare a
                # DDL statement with a parameter. Paths are engine-controlled
                # (from the lake), and quotes are escaped defensively.
                escaped = glob.replace("'", "''")
                # union_by_name keeps the pipeline working across the CRM schema
                # drift in month 7 instead of erroring on a column mismatch.
                self._conn.execute(
                    f'CREATE OR REPLACE VIEW {zone}."{table}" '
                    f"AS SELECT * FROM read_parquet('{escaped}', union_by_name=true)"
                )

    def query(self, sql: str) -> pl.DataFrame:
        started = time.perf_counter()
        try:
            result = self._conn.execute(sql).pl()
        except Exception:
            logger.error("query_failed", engine=self.name, sql=sql[:400])
            raise
        logger.debug(
            "query_ok", engine=self.name, ms=round((time.perf_counter() - started) * 1000, 1), rows=result.height
        )
        return result

    def scalar(self, sql: str) -> Any:
        row = self._conn.execute(sql).fetchone()
        if row is None:
            return None
        return row[0]

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Athena (Floci or AWS)
# ---------------------------------------------------------------------------
class AthenaEngine:
    """S3 + Glue + Athena. Used when the platform talks to Floci or real AWS."""

    name = "athena"

    def __init__(self) -> None:
        from banking_data_agents import aws

        self._settings = get_settings()
        self._athena = aws.athena()
        self._glue = aws.glue()
        self._s3 = aws.s3()
        # Set lazily to avoid an import cycle between the engine and the lake.
        self._lake: Lake | None = None

    # -- catalog ------------------------------------------------------------
    def _zone_database(self, zone: Zone) -> str:
        return {
            "bronze": self._settings.bda_bronze_db,
            "silver": self._settings.bda_silver_db,
            "gold": self._settings.bda_gold_db,
            "ops": self._settings.bda_ops_db,
        }[zone]

    def ensure_databases(self) -> None:
        existing = {db["Name"] for db in self._glue.get_databases().get("DatabaseList", [])}
        for zone in ZONES:
            name = self._zone_database(zone)
            if name not in existing:
                self._glue.create_database(DatabaseInput={"Name": name})

    def _table_location(self, zone: Zone, table: str) -> str:
        from banking_data_agents.pipeline.lake import S3Lake

        return (self._lake or S3Lake()).table_uri(zone, table)

    def ensure_table(self, zone: Zone, table: str) -> None:
        """Create an external parquet table in Glue if it does not exist yet."""
        database = self._zone_database(zone)
        try:
            self._glue.get_table(DatabaseName=database, Name=table)
            return
        except Exception:
            pass
        self._glue.create_table(
            DatabaseName=database,
            TableInput={
                "Name": table,
                "TableType": "EXTERNAL_TABLE",
                "Parameters": {
                    "classification": "parquet",
                    "has_encrypted_data": "false",
                    # The agent workgroup must be present so the emulator knows
                    # which storage descriptor to use.
                    "EXTERNAL": "TRUE",
                },
                "StorageDescriptor": {
                    "Columns": [],
                    "Location": self._table_location(zone, table),
                    "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    "SerdeInfo": {
                        "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                    },
                },
            },
        )
        logger.debug("glue_table_created", database=database, table=table)

    def register_all(self) -> None:
        """Ensure every zone database and table exists so SQL can reference them."""
        from banking_data_agents.pipeline.lake import S3Lake

        lake = self._lake or S3Lake()
        self._lake = lake
        self.ensure_databases()
        for zone in ZONES:
            for table in lake.tables(zone):
                self.ensure_table(zone, table)

    # -- execution ----------------------------------------------------------
    def query(self, sql: str) -> pl.DataFrame:
        started = time.perf_counter()
        execution = self._athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": self._settings.bda_gold_db},
            WorkGroup=self._settings.athena_workgroup,
            ResultConfiguration={"OutputLocation": self._settings.athena_output_uri},
        )
        execution_id = execution["QueryExecutionId"]
        rows = self._await(execution_id)
        result = self._to_frame(execution_id, rows)
        logger.debug(
            "query_ok",
            engine=self.name,
            ms=round((time.perf_counter() - started) * 1000, 1),
            rows=result.height,
            execution_id=execution_id,
        )
        return result

    def _await(self, execution_id: str) -> list[dict[str, Any]]:
        deadline = time.time() + self._settings.athena_query_timeout_s
        while True:
            state = self._athena.get_query_execution(QueryExecutionId=execution_id)
            status = state["QueryExecution"]["Status"]
            if status["State"] == "SUCCEEDED":
                return self._athena.get_query_results(QueryExecutionId=execution_id)["ResultSet"]["Rows"]
            if status["State"] in {"FAILED", "CANCELLED"}:
                raise RuntimeError(
                    f"Athena query {execution_id} {status['State']}: {status.get('StateChangeReason', '')}"
                )
            if time.time() > deadline:
                raise TimeoutError(f"Athena query {execution_id} exceeded {self._settings.athena_query_timeout_s}s")
            time.sleep(0.2)

    @staticmethod
    def _to_frame(execution_id: str, rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Convert an Athena ResultSet into a polars DataFrame."""
        from banking_data_agents import aws

        metadata = aws.athena().get_query_results(QueryExecutionId=execution_id, MaxResults=1)["ResultSet"][
            "ResultSetMetadata"
        ]["ColumnInfo"]
        columns = [c["Name"] for c in metadata]
        types = [c.get("Type", "varchar") for c in metadata]
        if not rows:
            return pl.DataFrame(schema=dict.fromkeys(columns, pl.Utf8))

        header = [cell.get("VarCharValue", "") for cell in rows[0]["Data"]]
        body = rows[1:] if header == columns else rows
        data: dict[str, list[Any]] = {name: [] for name in columns}
        for row in body:
            cells = row["Data"]
            for i, name in enumerate(columns):
                data[name].append(cells[i].get("VarCharValue") if i < len(cells) else None)

        frame = pl.DataFrame(data)
        casts = []
        for name, type_name in zip(columns, types, strict=False):
            if type_name in {"integer", "int", "bigint"}:
                casts.append(pl.col(name).cast(pl.Int64, strict=False))
            elif type_name in {"double", "float", "decimal"}:
                casts.append(pl.col(name).cast(pl.Float64, strict=False))
            elif type_name in {"boolean"}:
                casts.append(pl.col(name).cast(pl.Boolean, strict=False))
        return frame.with_columns(casts) if casts else frame


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_engine(lake: Any | None = None) -> QueryEngine:
    """Pick the engine that matches the configured backend."""
    from banking_data_agents.pipeline.lake import LocalLake, get_lake

    resolved = lake or get_lake()
    if isinstance(resolved, LocalLake):
        return DuckDBEngine(resolved)
    return AthenaEngine()


__all__ = ["AthenaEngine", "DuckDBEngine", "QueryEngine", "get_engine"]
