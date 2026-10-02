"""Central configuration.

Everything that varies between a laptop, CI, and production is resolved here and
nowhere else. The same code path serves Floci (``AWS_ENDPOINT_URL`` set) and real
AWS (``AWS_ENDPOINT_URL`` unset) — there is no ``if local`` branching in the
business logic, only in this module.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "dev", "staging", "prod"]
LakeBackend = Literal["auto", "local", "s3"]
LLMProvider = Literal["stub", "floci", "bedrock"]
ModelTier = Literal["router", "reasoner", "escalation"]


def _project_root() -> Path:
    """Repository root of the banking_data_agents project (contains pyproject.toml)."""
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Resolved runtime settings.

    Field names map case-insensitively onto environment variables, so
    ``bda_gold_bucket`` is read from ``BDA_GOLD_BUCKET``.
    """

    model_config = SettingsConfigDict(
        env_file=os.environ.get("BDA_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Identity ----------------------------------------------------------
    project: str = "banking-data-agents"
    env: Environment = "local"

    # --- Filesystem overrides (tests, CI) ----------------------------------
    #: Replaces ``<repo>/data`` when set.
    bda_data_dir: str | None = None
    #: Replaces ``<repo>/data/landing`` when set.
    bda_landing_dir: str | None = None
    #: Where the lake lives. ``auto`` infers it; ``local`` and ``s3`` pin it.
    bda_lake_backend: LakeBackend = "auto"

    # --- AWS / Floci -------------------------------------------------------
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"
    aws_session_token: str | None = None
    #: When set (e.g. ``http://localhost:4566``) every boto3 client is pointed at
    #: the local emulator. Unset for real AWS.
    aws_endpoint_url: str | None = None

    # --- Object storage ----------------------------------------------------
    bda_bronze_bucket: str = "bda-bronze"
    bda_silver_bucket: str = "bda-silver"
    bda_gold_bucket: str = "bda-gold"
    bda_ops_bucket: str = "bda-ops"
    bda_artifacts_bucket: str = "bda-artifacts"

    # --- Glue catalog databases -------------------------------------------
    bda_bronze_db: str = "bda_bronze"
    bda_silver_db: str = "bda_silver"
    bda_gold_db: str = "bda_gold"
    bda_ops_db: str = "bda_ops"

    # --- Answer retention --------------------------------------------------
    #: The DynamoDB table answers are retained in. Unset means "do not retain":
    #: the API keeps a bounded in-process store and the CLI says nothing was
    #: written. The CDK stacks set this explicitly (``bda-<env>-answers``), so a
    #: deployed process never has to guess and a laptop never writes by accident.
    bda_answer_table: str | None = None

    # --- Athena ------------------------------------------------------------
    athena_workgroup: str = "bda-agents"
    athena_bytes_cap_mb: int = 512
    athena_query_timeout_s: int = 30

    # --- Synthetic data ----------------------------------------------------
    synthetic_seed: int = 20260928
    synthetic_customers: int = 2_000
    synthetic_accounts: int = 3_200
    synthetic_loans: int = 1_200
    synthetic_cards: int = 1_800
    synthetic_months: int = 12
    synthetic_fraud_rate: float = 0.004

    # --- Models ------------------------------------------------------------
    llm_provider: LLMProvider = "stub"
    bedrock_model_router: str = "anthropic.claude-3-5-haiku-20241022-v1:0"
    bedrock_model_reasoner: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    bedrock_model_escalation: str = "anthropic.claude-3-opus-20240229-v1:0"
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str | None = None

    # --- Agent budgets -----------------------------------------------------
    agent_max_tool_calls: int = 8
    agent_max_tokens: int = 60_000
    agent_max_wall_clock_s: int = 45
    session_budget_usd: float = 0.50
    user_daily_budget_usd: float = 5.00

    # ----------------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_local(self) -> bool:
        """True when every client should talk to the local emulator."""
        return bool(self.aws_endpoint_url)

    @property
    def bronze_uri(self) -> str:
        return f"s3://{self.bda_bronze_bucket}"

    @property
    def silver_uri(self) -> str:
        return f"s3://{self.bda_silver_bucket}"

    @property
    def gold_uri(self) -> str:
        return f"s3://{self.bda_gold_bucket}"

    @property
    def athena_output_uri(self) -> str:
        return f"s3://{self.bda_artifacts_bucket}/athena-results/"

    @property
    def all_buckets(self) -> list[str]:
        return [
            self.bda_bronze_bucket,
            self.bda_silver_bucket,
            self.bda_gold_bucket,
            self.bda_ops_bucket,
            self.bda_artifacts_bucket,
        ]

    @property
    def data_dir(self) -> Path:
        """Local scratch space for generated data, lake and emulator state.

        Overridable so tests and CI runs get a hermetic lake instead of writing
        into the developer's working tree.
        """
        if self.bda_data_dir:
            return Path(self.bda_data_dir)
        return _project_root() / "data"

    @property
    def landing_dir(self) -> Path:
        if self.bda_landing_dir:
            return Path(self.bda_landing_dir)
        return self.data_dir / "landing"

    @property
    def contracts_dir(self) -> Path:
        return _project_root() / "contracts"

    @property
    def metrics_dir(self) -> Path:
        return _project_root() / "semantic" / "metrics"

    @property
    def answer_table_name(self) -> str:
        """The retention table: the configured one, or this environment's default.

        The default mirrors the CDK naming (``bda-<env>-answers``) so that a local
        emulator, a test and a deployment all agree on what the table is called.
        """
        return self.bda_answer_table or f"bda-{self.env}-answers"

    def model_for(self, tier: ModelTier) -> str:
        """Resolve a logical model tier to a concrete Bedrock model ID."""
        return {
            "router": self.bedrock_model_router,
            "reasoner": self.bedrock_model_reasoner,
            "escalation": self.bedrock_model_escalation,
        }[tier]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests that patch the environment)."""
    get_settings.cache_clear()
