"""Lake storage.

Zones map to directories locally and to buckets remotely:

===========  ================================  ==============================
Zone         Local (``data/lake/``)            Remote (S3)
===========  ================================  ==============================
bronze       ``bronze/<table>/*.parquet``      ``s3://bda-bronze/<table>/``
silver       ``silver/<table>/*.parquet``      ``s3://bda-silver/<table>/``
gold         ``gold/<table>/*.parquet``        ``s3://bda-gold/<table>/``
ops          ``ops/<table>/*.parquet``         ``s3://bda-ops/<table>/``
===========  ================================  ==============================

Both backends expose the same five operations, so no transformation SQL needs to
know where it is running.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal, Protocol

import polars as pl

from banking_data_agents.config import get_settings
from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)

Zone = Literal["bronze", "silver", "gold", "ops"]
ZONES: tuple[Zone, ...] = ("bronze", "silver", "gold", "ops")

#: Zone -> settings attribute holding the bucket name (remote backend).
_ZONE_BUCKET_ATTR: dict[str, str] = {
    "bronze": "bda_bronze_bucket",
    "silver": "bda_silver_bucket",
    "gold": "bda_gold_bucket",
    "ops": "bda_ops_bucket",
}


class Lake(Protocol):
    """Minimal storage contract shared by the local and S3 backends."""

    def zone_uri(self, zone: Zone) -> str: ...
    def table_uri(self, zone: Zone, table: str) -> str: ...
    def glob(self, zone: Zone, table: str) -> str: ...
    def read(self, zone: Zone, table: str) -> pl.DataFrame: ...
    def write(self, zone: Zone, table: str, frame: pl.DataFrame) -> str: ...
    def tables(self, zone: Zone) -> list[str]: ...
    def exists(self, zone: Zone, table: str) -> bool: ...
    def copy_tree(self, zone: Zone, table: str, source_dir: Path) -> int: ...


# ---------------------------------------------------------------------------
# Bronze schema normalisation
# ---------------------------------------------------------------------------
def normalise_frames(frames: list[pl.DataFrame]) -> list[pl.DataFrame]:
    """Give every batch of one source the same column set.

    An additive upstream change (the CRM adds ``preferred_language`` in month 7)
    only affects the batches that arrive after it. Without normalisation the
    column exists in the *table* but not in every *file*, and any downstream SQL
    referencing it fails on datasets that predate the change — which is exactly
    what happens when you run a short window of history.

    Bronze keeps every value verbatim; it only widens absent columns to typed
    NULLs, which is standard schema evolution rather than mutation.
    """
    if len(frames) < 2:
        return frames

    dtypes: dict[str, pl.DataType] = {}
    for frame in frames:
        for name, dtype in frame.schema.items():
            dtypes.setdefault(name, dtype)
    columns = sorted(dtypes)

    normalised: list[pl.DataFrame] = []
    for frame in frames:
        missing = [name for name in columns if name not in frame.columns]
        if missing:
            frame = frame.with_columns([pl.lit(None, dtype=dtypes[name]).alias(name) for name in missing])
        normalised.append(frame.select(columns))
    return normalised


# ---------------------------------------------------------------------------
# Local filesystem backend
# ---------------------------------------------------------------------------
class LocalLake:
    """Parquet on the local filesystem. The default for development and CI."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def zone_uri(self, zone: Zone) -> str:
        return (self.root / zone).as_posix()

    def table_uri(self, zone: Zone, table: str) -> str:
        return (self.root / zone / table).as_posix()

    def glob(self, zone: Zone, table: str) -> str:
        return (self.root / zone / table / "*.parquet").as_posix()

    def exists(self, zone: Zone, table: str) -> bool:
        return any((self.root / zone / table).glob("*.parquet"))

    def read(self, zone: Zone, table: str) -> pl.DataFrame:
        files = sorted((self.root / zone / table).glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"{zone}.{table} not found under {self.root}")
        return (
            pl.read_parquet(files[0])
            if len(files) == 1
            else pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
        )

    def write(self, zone: Zone, table: str, frame: pl.DataFrame) -> str:
        target = self.root / zone / table
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        path = target / "part-000.parquet"
        frame.write_parquet(path, compression="zstd")
        logger.debug("lake_write", zone=zone, table=table, rows=frame.height)
        return path.as_posix()

    def tables(self, zone: Zone) -> list[str]:
        base = self.root / zone
        if not base.exists():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir() and any(p.glob("*.parquet")))

    def copy_tree(self, zone: Zone, table: str, source_dir: Path, *, normalise_schema: bool = True) -> int:
        """Ingest a parquet tree, flattened and schema-normalised.

        The batch directory structure is deliberately dropped: ``_batch_id``
        already carries the batch, and a flat layout lets every query engine read
        the whole table with a single ``*.parquet`` glob.
        """
        target = self.root / zone / table
        if target.exists():
            shutil.rmtree(target)
        if not source_dir.exists():
            raise FileNotFoundError(f"source directory {source_dir} does not exist")
        target.mkdir(parents=True, exist_ok=True)

        files = sorted(source_dir.rglob("*.parquet"))
        if not files:
            return 0

        frames = [pl.read_parquet(path) for path in files]
        if normalise_schema:
            frames = normalise_frames(frames)

        for index, frame in enumerate(frames):
            frame.write_parquet(target / f"part-{index:04d}.parquet", compression="zstd")
        return len(frames)


# ---------------------------------------------------------------------------
# S3 backend (Floci or real AWS)
# ---------------------------------------------------------------------------
class S3Lake:
    """Parquet in S3. Identical semantics; used when ``AWS_ENDPOINT_URL`` is set."""

    def __init__(self) -> None:
        from banking_data_agents import aws

        self._s3 = aws.s3()
        self._settings = get_settings()

    def _bucket(self, zone: Zone) -> str:
        return getattr(self._settings, _ZONE_BUCKET_ATTR[zone])

    def zone_uri(self, zone: Zone) -> str:
        return f"s3://{self._bucket(zone)}"

    def table_uri(self, zone: Zone, table: str) -> str:
        return f"s3://{self._bucket(zone)}/{table}"

    def glob(self, zone: Zone, table: str) -> str:
        return f"{self.table_uri(zone, table)}/*.parquet"

    def exists(self, zone: Zone, table: str) -> bool:
        response = self._s3.list_objects_v2(Bucket=self._bucket(zone), Prefix=f"{table}/", MaxKeys=1)
        return bool(response.get("Contents"))

    def read(self, zone: Zone, table: str) -> pl.DataFrame:
        import io

        bucket = self._bucket(zone)
        keys = [
            obj["Key"]
            for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{table}/")
            for obj in page.get("Contents", [])
            if obj["Key"].endswith(".parquet")
        ]
        if not keys:
            raise FileNotFoundError(f"{zone}.{table} not found in {bucket}")
        frames = [
            pl.read_parquet(io.BytesIO(self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()))
            for key in sorted(keys)
        ]
        return frames[0] if len(frames) == 1 else pl.concat(frames, how="diagonal_relaxed")

    def write(self, zone: Zone, table: str, frame: pl.DataFrame) -> str:
        import io

        bucket = self._bucket(zone)
        for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{table}/"):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                self._s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})

        buffer = io.BytesIO()
        frame.write_parquet(buffer, compression="zstd")
        key = f"{table}/part-000.parquet"
        self._s3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())
        logger.debug("lake_write", zone=zone, table=table, rows=frame.height, uri=f"s3://{bucket}/{key}")
        return f"s3://{bucket}/{key}"

    def tables(self, zone: Zone) -> list[str]:
        bucket = self._bucket(zone)
        prefixes: set[str] = set()
        for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Delimiter="/"):
            for prefix in page.get("CommonPrefixes", []):
                prefixes.add(prefix["Prefix"].rstrip("/"))
        return sorted(prefixes)

    def copy_tree(self, zone: Zone, table: str, source_dir: Path, *, normalise_schema: bool = True) -> int:
        """Upload a parquet tree, flattened and schema-normalised."""
        import io

        bucket = self._bucket(zone)
        for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{table}/"):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                self._s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})

        files = sorted(source_dir.rglob("*.parquet"))
        if not files:
            return 0

        frames = [pl.read_parquet(path) for path in files]
        if normalise_schema:
            frames = normalise_frames(frames)

        for index, frame in enumerate(frames):
            buffer = io.BytesIO()
            frame.write_parquet(buffer, compression="zstd")
            self._s3.put_object(Bucket=bucket, Key=f"{table}/part-{index:04d}.parquet", Body=buffer.getvalue())
        return len(frames)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_lake(*, force_local: bool = False) -> Lake:
    """Return the lake backend the configuration implies.

    Selection order:

    * ``BDA_LAKE_BACKEND=local`` or ``force_local`` -> local filesystem.
    * ``BDA_LAKE_BACKEND=s3``                        -> S3 (Floci or AWS).
    * ``auto`` (default): a deployed ``env`` uses S3; a local ``env`` uses S3 only
      when the emulator actually answers, otherwise it falls back to the
      filesystem with a warning.

    The fallback is deliberate: `make pipeline` should stay useful when Docker is
    not running, but the warning ensures nobody believes they wrote to S3.
    """
    settings = get_settings()
    if force_local or settings.bda_lake_backend == "local":
        return local_lake()
    if settings.bda_lake_backend == "s3":
        return S3Lake()

    if settings.env != "local":
        return S3Lake()

    if settings.aws_endpoint_url:
        from banking_data_agents.aws import endpoint_reachable

        if endpoint_reachable():
            return S3Lake()
        logger.warning(
            "lake_fallback_local",
            reason="configured emulator endpoint is not reachable",
            endpoint=settings.aws_endpoint_url,
        )
    return local_lake()


def local_lake() -> LocalLake:
    """Always-local lake, used by tests that do not require an emulator."""
    return LocalLake(get_settings().data_dir / "lake")


#: Zone -> settings attribute holding the Glue database name.
_ZONE_DB_ATTR: dict[str, str] = {
    "bronze": "bda_bronze_db",
    "silver": "bda_silver_db",
    "gold": "bda_gold_db",
    "ops": "bda_ops_db",
}


def ensure_glue_databases() -> list[str]:
    """Create every zone's Glue database if missing, and return the names.

    Idempotent by construction: the pipeline calls this on every run, and a
    second ``create_database`` for a database that already exists would raise
    ``AlreadyExistsException`` halfway through a run. Re-reading the existing
    set first is what makes the call safe to repeat, which is the whole
    contract an integration test can hold it to.
    """
    settings = get_settings()
    if not settings.aws_endpoint_url and settings.env == "local":
        return []
    from banking_data_agents import aws

    client = aws.glue()
    existing = {db["Name"] for db in client.get_databases().get("DatabaseList", [])}
    created: list[str] = []
    for zone in ZONES:
        name = getattr(settings, _ZONE_DB_ATTR[zone])
        if name not in existing:
            client.create_database(DatabaseInput={"Name": name})
            created.append(name)
    return created


def ensure_buckets() -> list[str]:
    """Create every BDA bucket if missing. No-op for a purely local run."""
    settings = get_settings()
    # Real AWS with no emulator: the CDK stack provisioned the buckets, and a
    # local env has nothing to provision against.
    if not settings.aws_endpoint_url and settings.env == "local":
        return []
    from banking_data_agents import aws

    client = aws.s3()
    existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
    created: list[str] = []
    for bucket in settings.all_buckets:
        if bucket not in existing:
            client.create_bucket(Bucket=bucket)
            created.append(bucket)
    return created


__all__ = [
    "ZONES",
    "Lake",
    "LocalLake",
    "S3Lake",
    "Zone",
    "ensure_buckets",
    "ensure_glue_databases",
    "get_lake",
    "local_lake",
    "normalise_frames",
]
