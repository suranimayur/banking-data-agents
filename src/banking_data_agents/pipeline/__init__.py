"""Medallion pipeline: bronze -> silver -> gold -> publish.

Transformations are plain SQL files under ``sql/``, executed by whichever engine
the configuration implies (DuckDB locally, Athena on Floci or AWS). There is one
copy of the business logic.
"""

from banking_data_agents.pipeline.engine import AthenaEngine, DuckDBEngine, get_engine
from banking_data_agents.pipeline.lake import LocalLake, S3Lake, get_lake
from banking_data_agents.pipeline.runner import run_pipeline

__all__ = [
    "AthenaEngine",
    "DuckDBEngine",
    "LocalLake",
    "S3Lake",
    "get_engine",
    "get_lake",
    "run_pipeline",
]
