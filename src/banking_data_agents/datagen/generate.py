"""Write the synthetic source systems into the landing zone.

Layout (monthly batches, like a real bank's extracts):

```
data/landing/
  s_crm/customers/batch_month=2025-10/part-000.parquet
  s_core/transactions/batch_month=2025-10/part-000.parquet
  ...
  _holdout/fraud_labels/part-000.parquet      # ground truth, never uploaded
  _manifest.json                              # row counts + fingerprints
```

The manifest's fingerprints are order-independent hashes, so CI can assert that
two generations from the same seed are identical — which is what makes the
golden evaluation datasets meaningful.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from banking_data_agents.config import get_settings
from banking_data_agents.datagen.generators import SyntheticBank, add_ingest_metadata
from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)

HOLDOUT_DIR = "_holdout"
MANIFEST_NAME = "_manifest.json"

#: Source systems and their entities.
SOURCES: dict[str, tuple[str, ...]] = {
    "s_crm": ("customers",),
    "s_core": ("accounts", "transactions"),
    "s_loans": ("loans", "repayments"),
    "s_cards": ("cards", "card_transactions"),
    "s_bureau": ("credit_records",),
    "s_payments": ("payment_transactions",),
}


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def fingerprint(frame: pl.DataFrame) -> str:
    """Order-independent content hash of a frame.

    Row hashes are XOR-reduced, so re-sorting a table does not change its
    fingerprint but changing any value does.
    """
    if frame.height == 0:
        return "empty"
    columns = sorted(frame.columns)
    row_hashes = (
        frame.select(
            pl.concat_str(
                [pl.col(c).cast(pl.Utf8, strict=False).fill_null("<null>") for c in columns],
                separator="\u0001",
            )
            .hash(seed=42)
            .alias("h")
        )["h"]
        .to_numpy()
        .astype(np.uint64)
    )
    combined = int(np.bitwise_xor.reduce(row_hashes))
    payload = f"{frame.height}|{','.join(columns)}|{combined}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _write(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path, compression="zstd")


def _record(
    manifest: list[dict[str, Any]],
    *,
    source: str,
    entity: str,
    batch: str,
    frame: pl.DataFrame,
    path: Path,
) -> None:
    # Store a path relative to the landing root (``s_core/accounts/batch_month=...``)
    # so the manifest is portable across machines and CI runners.
    parts = path.parts
    relative = "/".join(parts[parts.index(source) :]) if source in parts else path.name
    manifest.append(
        {
            "source": source,
            "entity": entity,
            "batch": batch,
            "rows": frame.height,
            "columns": sorted(frame.columns),
            "fingerprint": fingerprint(frame),
            "path": relative,
        }
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def _build_bank(seed: int, scale: float, months: int | None = None) -> SyntheticBank:
    settings = get_settings()
    customers = max(10, int(settings.synthetic_customers * scale))
    return SyntheticBank(
        seed=seed,
        n_customers=customers,
        # Every customer must own at least one account.
        n_accounts=max(int(settings.synthetic_accounts * scale), customers),
        n_loans=max(5, int(settings.synthetic_loans * scale)),
        n_cards=max(8, int(settings.synthetic_cards * scale)),
        months=months or settings.synthetic_months,
        fraud_rate=settings.synthetic_fraud_rate,
    )


def _generate(
    bank: SyntheticBank,
    landing: Path,
    sources: Sequence[str],
    *,
    verbose: bool,
) -> dict[str, Any]:
    manifest: list[dict[str, Any]] = []
    all_labels: list[pl.DataFrame] = []

    for window in bank.windows:
        # --- dimension snapshots -------------------------------------------
        if "s_crm" in sources:
            frame = bank.customers_snapshot(window)
            path = landing / "s_crm" / "customers" / f"batch_month={window.label}" / "part-000.parquet"
            _write(frame, path)
            _record(manifest, source="s_crm", entity="customers", batch=window.label, frame=frame, path=path)

        if "s_core" in sources:
            frame = bank.accounts_snapshot(window)
            path = landing / "s_core" / "accounts" / f"batch_month={window.label}" / "part-000.parquet"
            _write(frame, path)
            _record(manifest, source="s_core", entity="accounts", batch=window.label, frame=frame, path=path)

        if "s_loans" in sources:
            frame = bank.loans_snapshot(window)
            path = landing / "s_loans" / "loans" / f"batch_month={window.label}" / "part-000.parquet"
            _write(frame, path)
            _record(manifest, source="s_loans", entity="loans", batch=window.label, frame=frame, path=path)

            frame = bank.repayments(window)
            path = landing / "s_loans" / "repayments" / f"batch_month={window.label}" / "part-000.parquet"
            _write(frame, path)
            _record(manifest, source="s_loans", entity="repayments", batch=window.label, frame=frame, path=path)

        if "s_cards" in sources:
            frame = bank.cards_snapshot(window)
            path = landing / "s_cards" / "cards" / f"batch_month={window.label}" / "part-000.parquet"
            _write(frame, path)
            _record(manifest, source="s_cards", entity="cards", batch=window.label, frame=frame, path=path)

        if "s_bureau" in sources:
            frame = bank.bureau_snapshot(window)
            path = landing / "s_bureau" / "credit_records" / f"batch_month={window.label}" / "part-000.parquet"
            _write(frame, path)
            _record(manifest, source="s_bureau", entity="credit_records", batch=window.label, frame=frame, path=path)

        # --- facts, with fraud injected before landing ----------------------
        core = bank.core_transactions(window) if "s_core" in sources else pl.DataFrame()
        card = bank.card_transactions(window) if "s_cards" in sources else pl.DataFrame()
        payments = bank.payment_transactions(window) if "s_payments" in sources else pl.DataFrame()

        core, card, payments, labels = bank.inject_fraud(window, core, card, payments)
        if labels.height:
            all_labels.append(labels)

        if "s_core" in sources:
            core = add_ingest_metadata(
                core,
                source="s_core",
                entity="transactions",
                batch_id=window.batch_id,
                ingested_at=window.end,
                source_file=f"s_core/transactions/batch_month={window.label}/part-000.parquet",
                key_columns=["transaction_id"],
            )
            path = landing / "s_core" / "transactions" / f"batch_month={window.label}" / "part-000.parquet"
            _write(core, path)
            _record(manifest, source="s_core", entity="transactions", batch=window.label, frame=core, path=path)

        if "s_cards" in sources:
            card = add_ingest_metadata(
                card,
                source="s_cards",
                entity="card_transactions",
                batch_id=window.batch_id,
                ingested_at=window.end,
                source_file=f"s_cards/card_transactions/batch_month={window.label}/part-000.parquet",
                key_columns=["transaction_id"],
            )
            path = landing / "s_cards" / "card_transactions" / f"batch_month={window.label}" / "part-000.parquet"
            _write(card, path)
            _record(manifest, source="s_cards", entity="card_transactions", batch=window.label, frame=card, path=path)

        if "s_payments" in sources:
            payments = add_ingest_metadata(
                payments,
                source="s_payments",
                entity="payment_transactions",
                batch_id=window.batch_id,
                ingested_at=window.end,
                source_file=f"s_payments/payment_transactions/batch_month={window.label}/part-000.parquet",
                key_columns=["transaction_id"],
            )
            path = landing / "s_payments" / "payment_transactions" / f"batch_month={window.label}" / "part-000.parquet"
            _write(payments, path)
            _record(
                manifest,
                source="s_payments",
                entity="payment_transactions",
                batch=window.label,
                frame=payments,
                path=path,
            )

        if verbose:
            batch_rows = sum(entry["rows"] for entry in manifest if entry["batch"] == window.label)
            print(f"  {window.label}  {batch_rows:>8,} rows")

    # --- holdout -----------------------------------------------------------
    holdout_path = landing / HOLDOUT_DIR / "fraud_labels" / "part-000.parquet"
    holdout = (
        pl.concat(all_labels, how="vertical_relaxed")
        if all_labels
        else pl.DataFrame(
            schema={
                "transaction_id": pl.Utf8,
                "is_fraud": pl.Boolean,
                "fraud_method": pl.Utf8,
                "source_system": pl.Utf8,
                "customer_id": pl.Utf8,
                "label_available_date": pl.Date,
            }
        )
    )
    _write(holdout, holdout_path)

    summary: dict[str, Any] = {
        "seed": bank.seed,
        "months": bank.months,
        "window_start": bank.first_window.start.date().isoformat(),
        "window_end": bank.last_window.end.date().isoformat(),
        "row_counts": {
            "customers": bank.n_customers,
            "accounts": bank.n_accounts,
            "loans": bank.n_loans,
            "cards": min(bank.n_cards, bank.n_customers),
        },
        "datasets": manifest,
        "holdout": {
            "rows": holdout.height,
            "fingerprint": fingerprint(holdout),
            "path": f"{HOLDOUT_DIR}/fraud_labels/part-000.parquet",
        },
    }
    return summary


def _manifest_path(landing: Path) -> Path:
    return landing / MANIFEST_NAME


def generate_into(
    landing: Path,
    *,
    seed: int,
    scale: float = 1.0,
    sources: Sequence[str] | None = None,
    verbose: bool = False,
    write_manifest: bool = True,
) -> dict[str, Any]:
    """Generate into an explicit directory, manifest included.

    The pipeline derives its time anchor from the manifest, so a landing zone
    without one is not usable — hence the manifest is written here too, not only
    in :func:`generate_all`.
    """
    landing.mkdir(parents=True, exist_ok=True)
    bank = _build_bank(seed, scale)
    summary = _generate(bank, landing, list(sources or SOURCES), verbose=verbose)
    if write_manifest:
        _manifest_path(landing).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def generate_all(seed: int | None = None, scale: float = 1.0, verbose: bool = False) -> int:
    """Generate every source system. Returns a process exit code."""
    settings = get_settings()
    resolved_seed = settings.synthetic_seed if seed is None else seed
    landing = settings.landing_dir

    if landing.exists():
        shutil.rmtree(landing)
    landing.mkdir(parents=True, exist_ok=True)

    bank = _build_bank(resolved_seed, scale)
    if verbose:
        print(
            f"[data] seed={resolved_seed} scale={scale} "
            f"window={bank.first_window.label}..{bank.last_window.label} "
            f"({bank.months} months)"
        )
        print(
            f"[data] customers={bank.n_customers:,} accounts={bank.n_accounts:,} "
            f"loans={bank.n_loans:,} cards={min(bank.n_cards, bank.n_customers):,}"
        )

    summary = _generate(bank, landing, list(SOURCES), verbose=verbose)
    _manifest_path(landing).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    total_rows = sum(entry["rows"] for entry in summary["datasets"])
    if verbose:
        print(f"[data] {len(summary['datasets'])} datasets · {total_rows:,} rows written to {landing}")
        print(f"[data] holdout: {summary['holdout']['rows']:,} fraud labels ({summary['holdout']['fingerprint']})")
        print("[data] NOTE: the holdout is never uploaded to bronze and must never be read by agents.")
    return 0


def generate_source(source: str, *, seed: int | None = None, scale: float = 1.0, verbose: bool = False) -> int:
    """Generate a single source system (keeps other sources' data intact)."""
    if source not in SOURCES:
        raise SystemExit(f"unknown source '{source}'; known: {', '.join(SOURCES)}")
    settings = get_settings()
    resolved_seed = settings.synthetic_seed if seed is None else seed
    landing = settings.landing_dir
    landing.mkdir(parents=True, exist_ok=True)

    bank = _build_bank(resolved_seed, scale)
    summary = _generate(bank, landing, [source], verbose=verbose)

    manifest_path = _manifest_path(landing)
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        kept = [e for e in existing.get("datasets", []) if e["source"] != source]
        summary["datasets"] = kept + summary["datasets"]
    manifest_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return 0


def load_manifest(landing: Path | None = None) -> dict[str, Any]:
    """Read the landing manifest (used by tests and the pipeline)."""
    path = _manifest_path(landing or get_settings().landing_dir)
    if not path.exists():
        raise FileNotFoundError(f"no manifest at {path}; run `make data` first")
    return json.loads(path.read_text(encoding="utf-8"))


def dataset_fingerprints(manifest: dict[str, Any] | None = None) -> dict[str, str]:
    """Map ``source/entity/batch`` -> fingerprint for determinism assertions."""
    data = manifest or load_manifest()
    return {f"{e['source']}/{e['entity']}/{e['batch']}": e["fingerprint"] for e in data["datasets"]}


__all__ = [
    "SOURCES",
    "dataset_fingerprints",
    "fingerprint",
    "generate_all",
    "generate_into",
    "generate_source",
    "load_manifest",
]
