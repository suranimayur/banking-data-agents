"""Data product contracts: load, validate, and enforce.

A contract that is only documentation drifts from reality within a month. These
contracts are loaded at runtime by the agent tools and validated against the gold
tables in CI, so a contract that lies about its own columns fails the build.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from banking_data_agents.config import get_settings
from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)


def _prohibited_use(prohibited: list[str], use: str, use_tokens: set[str]) -> str | None:
    """The prohibited rule that ``use`` falls under, if any.

    Exact match, then containment: every token of a prohibited rule appearing in
    the requested use means the request is a specific form of that prohibition.
    """
    normalised = {entry.lower().replace(" ", "_"): entry for entry in prohibited}
    if use in normalised:
        return normalised[use]
    for key, entry in normalised.items():
        tokens = {token for token in key.split("_") if token}
        if tokens and tokens <= use_tokens:
            return entry
    return None


class ColumnContract(BaseModel):
    """One published column, with its semantics and its permitted uses."""

    name: str
    type: str
    description: str = ""
    definition: str | None = None
    unit: str | None = None
    enum: list[str] | None = None
    range: list[float] | None = None
    default: object | None = None
    source: str | None = None
    metric_ref: str | None = None
    pii: bool = False
    caveat: str | None = None
    note: str | None = None
    #: Uses forbidden for *this column*, on top of the product-level list.
    allowed_use: list[str] = Field(default_factory=list)
    not_allowed_use: list[str] = Field(default_factory=list)

    @property
    def metric_name(self) -> str | None:
        """``metric.total_balance@2.1.0`` -> ``total_balance``."""
        if not self.metric_ref:
            return None
        return self.metric_ref.split(".", 1)[-1].split("@", 1)[0]


class SLA(BaseModel):
    freshness_hours: int = 24
    availability: str = "99.5% monthly"
    row_count_floor: int = 0


class Refresh(BaseModel):
    schedule: str = "daily"
    job: str = ""


class ProductContract(BaseModel):
    """A published data product."""

    product: str
    version: str
    domain: str
    owner: str
    steward: str = ""
    description: str
    grain: str
    primary_key: list[str]
    classification: str = "internal"
    refresh: Refresh = Field(default_factory=Refresh)
    sla: SLA = Field(default_factory=SLA)
    allowed_use: list[str] = Field(default_factory=list)
    not_allowed_use: list[str] = Field(default_factory=list)
    columns: list[ColumnContract] = Field(default_factory=list)
    consumers: list[str] = Field(default_factory=list)

    # -- helpers -------------------------------------------------------------
    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]

    @property
    def dataset(self) -> str:
        """The fully qualified lake table, e.g. ``gold.customer_360``."""
        return f"gold.{self.product}"

    def column(self, name: str) -> ColumnContract | None:
        return next((c for c in self.columns if c.name == name), None)

    def usability(self, column_name: str, intended_use: str) -> tuple[bool, str]:
        """Whether ``intended_use`` is permitted for ``column_name``.

        Returns ``(allowed, reason)``. The reason is written to be shown to a user
        verbatim, because a refusal the user cannot understand is just an error.

        Prohibition is matched by *containment*, not equality. A contract that
        forbids ``marketing_targeting`` also forbids
        ``marketing_targeting_on_credit_score`` — a narrower, more specific name
        for the same prohibited activity. Matching only on the exact string would
        let the more specific name walk straight through the control, which is
        how governance rules quietly stop working.
        """
        use = intended_use.strip().lower().replace(" ", "_")
        use_tokens = {token for token in use.split("_") if token}

        product_hit = _prohibited_use(self.not_allowed_use, use, use_tokens)
        if product_hit:
            qualifier = (
                "" if product_hit.lower().replace(" ", "_") == use else f" because it is a form of '{product_hit}'"
            )
            return False, (
                f"'{intended_use}' is prohibited for the whole {self.product} product{qualifier} "
                f"(contract {self.product}@{self.version}, field not_allowed_use)."
            )

        column = self.column(column_name)
        if column is None:
            return False, f"column '{column_name}' is not part of {self.product}@{self.version}."

        column_hit = _prohibited_use(column.not_allowed_use, use, use_tokens)
        if column_hit:
            return False, (
                f"'{intended_use}' is prohibited for column '{column_name}' "
                f"because it is a form of '{column_hit}' "
                f"(contract {self.product}@{self.version}, column not_allowed_use)."
            )

        if column.allowed_use and use not in {u.lower().replace(" ", "_") for u in column.allowed_use}:
            return False, (
                f"column '{column_name}' is restricted to {column.allowed_use}; "
                f"'{intended_use}' is not among them (contract {self.product}@{self.version})."
            )

        return True, "permitted"

    def to_prompt(self) -> str:
        """Compact rendering handed to the model when generating SQL.

        Deliberately terse: every token here is prompt surface that must be paid
        for on each call.
        """
        lines = [f"PRODUCT {self.product}@{self.version} ({self.grain})"]
        for column in self.columns:
            bits = [f"  {column.name}:{column.type}"]
            if column.unit:
                bits.append(f"[{column.unit}]")
            if column.enum:
                bits.append("one of " + "|".join(column.enum[:8]))
            if column.metric_ref:
                bits.append(f"-> {column.metric_ref}")
            lines.append(" ".join(bits))
        return "\n".join(lines)


def contracts_dir() -> Path:
    return get_settings().contracts_dir


@lru_cache(maxsize=1)
def load_contracts(directory: Path | None = None) -> dict[str, ProductContract]:
    """Load every contract. Cached; call :func:`reload_contracts` to refresh."""
    base = directory or contracts_dir()
    if not base.exists():
        raise FileNotFoundError(f"no contracts directory at {base}")

    contracts: dict[str, ProductContract] = {}
    for path in sorted(base.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        contract = ProductContract.model_validate(payload)
        if contract.product in contracts:
            raise ValueError(f"duplicate contract for product '{contract.product}' ({path.name})")
        contracts[contract.product] = contract

    if not contracts:
        raise ValueError(f"no contracts found in {base}")
    logger.info("contracts_loaded", count=len(contracts), products=sorted(contracts))
    return contracts


def reload_contracts() -> dict[str, ProductContract]:
    load_contracts.cache_clear()
    return load_contracts()


def validate_contracts(lake: object) -> list[str]:
    """Check every contract against the gold tables it describes.

    Returns a list of problem descriptions; empty means the contracts and the
    published data agree. This runs in CI, so a contract cannot silently drift.
    """
    problems: list[str] = []
    contracts = load_contracts()

    for contract in contracts.values():
        try:
            frame = lake.read("gold", contract.product)  # type: ignore[attr-defined]
        except FileNotFoundError:
            problems.append(f"{contract.product}: gold table missing")
            continue

        published = set(frame.columns)
        declared = set(contract.column_names)

        missing = sorted(declared - published)
        if missing:
            problems.append(f"{contract.product}: contract declares columns that do not exist: {missing}")

        undocumented = sorted(published - declared)
        if undocumented:
            problems.append(f"{contract.product}: published columns missing from the contract: {undocumented}")

        for key in contract.primary_key:
            if key not in published:
                problems.append(f"{contract.product}: primary key '{key}' not published")
                continue
            if frame[key].null_count():
                problems.append(f"{contract.product}: primary key '{key}' contains nulls")

        if contract.sla.row_count_floor and frame.height < contract.sla.row_count_floor:
            problems.append(
                f"{contract.product}: {frame.height} rows is below the declared floor {contract.sla.row_count_floor}"
            )

        # A metric reference must resolve, or the agent has a dangling pointer.
        from banking_data_agents.semantic.metrics import load_metrics

        known = load_metrics()
        for column in contract.columns:
            if column.metric_ref and column.metric_ref.split("@")[0].split(".", 1)[-1] not in known:
                problems.append(f"{contract.product}.{column.name}: unresolved metric_ref '{column.metric_ref}'")

    return problems


def contract_digest() -> str:
    """Stable fingerprint of the contract set, recorded in the evidence envelope."""
    import hashlib

    contracts = load_contracts()
    payload = "|".join(f"{name}@{c.version}:{','.join(c.column_names)}" for name, c in sorted(contracts.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "SLA",
    "ColumnContract",
    "ProductContract",
    "Refresh",
    "contract_digest",
    "contracts_dir",
    "load_contracts",
    "reload_contracts",
    "validate_contracts",
]
