"""Deterministic generators for the six synthetic source systems.

Design notes that matter:

* **Behavioural clusters drive everything.** Each customer belongs to one of five
  clusters that set income, balance, transaction frequency, amount shape and card
  utilisation. Without this structure the data is i.i.d. noise and every
  downstream analysis is trivially correct — which would make the project prove
  nothing.
* **The truth is knowable but hidden.** Fraud is injected deliberately and its
  labels are written to a *holdout* table that agent code must never read, so
  precision and recall can be measured honestly offline.
* **The data is deliberately messy.** A schema drift arrives in month 7, the core
  banking extract starts arriving damaged in month 9, and 4% of accounts have a
  region that disagrees with the CRM. Those are features, not bugs: they are what
  the quality and lineage narratives have to explain.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from banking_data_agents.datagen import reference as ref

# ---------------------------------------------------------------------------
# Deterministic primitives
# ---------------------------------------------------------------------------


def rng_for(seed: int, *parts: str) -> np.random.Generator:
    """A generator whose stream depends only on ``seed`` and ``parts``.

    Deriving from a hash rather than sharing one generator keeps entities
    independent: adding a new entity cannot shift the numbers produced for an
    existing one, so golden fixtures survive refactors.
    """
    material = "|".join((str(seed), *parts)).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def weighted_choice(
    rng: np.random.Generator,
    labels: Sequence[str],
    weights: dict[str, float] | None,
    size: int,
) -> np.ndarray:
    """Sample from ``labels`` honouring ``weights`` (uniform when omitted)."""
    if size == 0:
        return np.empty(0, dtype=object)
    population = np.array(list(labels), dtype=object)
    probabilities = None
    if weights is not None:
        raw = np.array([float(weights.get(str(label), 0.0)) for label in population])
        total = raw.sum()
        if total <= 0:
            raise ValueError("weights must contain at least one positive value")
        probabilities = raw / total
    return rng.choice(population, size=size, p=probabilities)


def random_timestamps(rng: np.random.Generator, start: datetime, end: datetime, size: int) -> np.ndarray:
    """Uniform timestamps in ``[start, end)`` as ``datetime64[us]``."""
    if size == 0:
        return np.empty(0, dtype="datetime64[us]")
    span = max(int((end - start).total_seconds()), 1)
    offsets = rng.integers(0, span, size=size).astype("timedelta64[s]")
    return (np.datetime64(start, "s") + offsets).astype("datetime64[us]")


def lognormal_amounts(rng: np.random.Generator, mean: float, sigma: float, size: int) -> np.ndarray:
    """Positive, right-skewed amounts — the shape real transaction values have."""
    if size == 0:
        return np.empty(0, dtype=float)
    mu = np.log(max(mean, 1.0))
    values = rng.lognormal(mean=mu, sigma=sigma, size=size)
    return np.round(values, 2)


def clamp_to_band(values: np.ndarray, low: float, high: float) -> np.ndarray:
    return np.clip(values, low, high).round(2)


# ---------------------------------------------------------------------------
# Time window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthWindow:
    """One monthly batch. Sources land as monthly extracts, like a real bank."""

    index: int  # 1-based
    label: str  # "2026-01"
    start: datetime
    end: datetime  # exclusive

    @property
    def batch_id(self) -> str:
        return f"{self.label}-01"

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def month_windows(months: int, start: date) -> list[MonthWindow]:
    """Contiguous calendar-month windows beginning at ``start``."""
    windows: list[MonthWindow] = []
    year, month = start.year, start.month
    for i in range(months):
        nxt_year, nxt_month = (year + 1, 1) if month == 12 else (year, month + 1)
        windows.append(
            MonthWindow(
                index=i + 1,
                label=f"{year:04d}-{month:02d}",
                start=datetime(year, month, 1),
                end=datetime(nxt_year, nxt_month, 1),
            )
        )
        year, month = nxt_year, nxt_month
    return windows


#: Seasonal multiplier by calendar month — festive quarter runs hot.
SEASONALITY: dict[int, float] = {
    1: 0.92,
    2: 0.88,
    3: 1.05,
    4: 0.98,
    5: 1.00,
    6: 0.95,
    7: 0.97,
    8: 1.02,
    9: 1.08,
    10: 1.28,
    11: 1.22,
    12: 1.12,
}


# ---------------------------------------------------------------------------
# Ingestion metadata
# ---------------------------------------------------------------------------


def add_ingest_metadata(
    frame: pl.DataFrame,
    *,
    source: str,
    entity: str,
    batch_id: str,
    ingested_at: datetime,
    source_file: str,
    key_columns: Sequence[str],
) -> pl.DataFrame:
    """Attach the columns every bronze record must carry.

    ``_record_hash`` is computed over the natural key so the silver layer can
    deduplicate re-delivered batches without relying on row order.
    """
    hash_expr = (
        pl.concat_str([pl.col(c).cast(pl.Utf8, strict=False) for c in key_columns], separator="\u0001")
        .hash(seed=0)
        .cast(pl.Utf8)
        .alias("_record_hash")
    )
    return frame.with_columns(
        pl.lit(ingested_at).alias("_ingested_at"),
        pl.lit(f"{source}/{entity}").alias("_source_system"),
        pl.lit(source_file).alias("_source_file"),
        pl.lit(batch_id).alias("_batch_id"),
        hash_expr,
    )


# ---------------------------------------------------------------------------
# The bank
# ---------------------------------------------------------------------------


@dataclass
class SyntheticBank:
    """Generates every synthetic source system from one seed."""

    seed: int = 20260928
    n_customers: int = 2_000
    n_accounts: int = 3_200
    n_loans: int = 1_200
    n_cards: int = 1_800
    months: int = 12
    fraud_rate: float = 0.004
    start_date: date = date(2025, 10, 1)

    windows: list[MonthWindow] = field(default_factory=list, init=False)
    _customers: pl.DataFrame | None = field(default=None, init=False, repr=False)
    _accounts: pl.DataFrame | None = field(default=None, init=False, repr=False)
    _cards: pl.DataFrame | None = field(default=None, init=False, repr=False)
    _loans: pl.DataFrame | None = field(default=None, init=False, repr=False)
    _bureau: pl.DataFrame | None = field(default=None, init=False, repr=False)
    _basis: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = field(default=None, init=False, repr=False)
    _amount_scale: np.ndarray | None = field(default=None, init=False, repr=False)
    _txn_rate: np.ndarray | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.windows = month_windows(self.months, self.start_date)

    # -- shared derived arrays ----------------------------------------------
    @staticmethod
    def _owner_index(frame: pl.DataFrame, column: str = "customer_id") -> np.ndarray:
        """Extract the numeric customer index from a ``C0000123`` identifier."""
        return frame[column].str.slice(1).cast(pl.Int64).to_numpy().astype(int)

    def _customer_amount_scale(self) -> np.ndarray:
        """Mean transaction amount per customer index."""
        if self._amount_scale is None:
            clusters, *_ = self._customer_basis()
            self._amount_scale = np.array(
                [ref.CLUSTER_PROFILE[str(c)]["txn_amount_mean"] for c in clusters], dtype=float
            )
        return self._amount_scale

    def _customer_txn_rate(self) -> np.ndarray:
        """Expected transactions per month per customer index."""
        if self._txn_rate is None:
            clusters, *_ = self._customer_basis()
            self._txn_rate = np.array([ref.CLUSTER_PROFILE[str(c)]["txns_per_month"] for c in clusters], dtype=float)
        return self._txn_rate

    # -- convenience ---------------------------------------------------------
    @property
    def first_window(self) -> MonthWindow:
        return self.windows[0]

    @property
    def last_window(self) -> MonthWindow:
        return self.windows[-1]

    # -- dimensions ----------------------------------------------------------
    def _customer_basis(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Cluster rollout, state code, credit score and index — shared by every dim.

        Cached: this is called from a dozen places and the score loop is O(n).
        """
        if self._basis is not None:
            return self._basis
        rng = rng_for(self.seed, "customers", "basis")
        clusters = weighted_choice(rng, ref.CLUSTERS, ref.CLUSTER_WEIGHTS, self.n_customers)
        state_weights = {code: ref.STATE_WEIGHTS[code] for code in ref.STATE_CODES}
        states = weighted_choice(rng, ref.STATE_CODES, state_weights, self.n_customers)
        means = np.array([ref.CLUSTER_PROFILE[str(c)]["credit_score_mean"] for c in clusters], dtype=float)
        scores = np.clip(rng.normal(means, 45.0), 300.0, 900.0)
        self._basis = (clusters, states, scores, np.arange(self.n_customers))
        return self._basis

    def customers_base(self) -> pl.DataFrame:
        """Build the customer dimension once; monthly snapshots derive from it."""
        if self._customers is not None:
            return self._customers

        rng = rng_for(self.seed, "customers", "attrs")
        clusters, state_codes, _scores, _idx = self._customer_basis()
        n = self.n_customers

        cities = np.array([rng.choice(ref.STATES[str(code)][1]) for code in state_codes], dtype=object)
        pincodes = np.array(
            [f"{ref.STATES[str(code)][2]}{rng.integers(100000, 999999)}" for code in state_codes],
            dtype=object,
        )
        first = weighted_choice(rng, ref.FIRST_NAMES, None, n)
        last = weighted_choice(rng, ref.LAST_NAMES, None, n)

        ages = np.clip(rng.normal(41, 12.5, n), 21, 74).astype(int)
        onboarded = date(self.start_date.year - 6, 1, 1)
        onboard_offsets = rng.integers(0, 365 * 6, n)
        onboarded_dates = [onboarded + timedelta(days=int(d)) for d in onboard_offsets]

        # Marketing vocabulary deliberately differs from the gold product's
        # customer_segment, so the semantic layer has a real job to do.
        marketing = weighted_choice(
            rng,
            ("Retail", "Preferred", "Prime", "Wealth"),
            {"Retail": 0.62, "Preferred": 0.24, "Prime": 0.10, "Wealth": 0.04},
            n,
        )
        # Declared annual income, captured at KYC. Drives affordability ratios in
        # the credit-risk product.
        annual_income = np.round(
            np.array([ref.CLUSTER_PROFILE[str(c)]["income_mean"] for c in clusters], dtype=float)
            * 12.0
            * rng.lognormal(mean=0.0, sigma=0.18, size=n),
            -3,
        )
        kyc = weighted_choice(
            rng,
            ("VERIFIED", "PENDING", "RE_KYC_DUE"),
            {"VERIFIED": 0.88, "PENDING": 0.06, "RE_KYC_DUE": 0.06},
            n,
        )
        preferred = weighted_choice(
            rng,
            ref.CHANNEL_PREFERENCES,
            {"MOBILE_APP": 0.58, "INTERNET_BANKING": 0.19, "BRANCH": 0.13, "ATM": 0.10},
            n,
        )

        frame = pl.DataFrame(
            {
                "customer_id": [f"C{i:07d}" for i in range(n)],
                "first_name": first,
                "last_name": last,
                "date_of_birth": [
                    date(self.start_date.year - int(a), int(m), int(d))
                    for a, m, d in zip(ages, rng.integers(1, 13, n), rng.integers(1, 29, n), strict=True)
                ],
                "email": [
                    f"{f.lower()}.{surname.lower()}{i}@example.in"
                    for i, (f, surname) in enumerate(zip(first, last, strict=True))
                ],
                "phone": [f"+91{int(p):010d}" for p in rng.integers(6000000000, 9999999999, n)],
                "address_line": [
                    f"{int(no)} {name} {kind}"
                    for no, name, kind in zip(
                        rng.integers(1, 400, n),
                        last,
                        rng.choice(["Marg", "Road", "Nagar", "Colony", "Layout"], n),
                        strict=True,
                    )
                ],
                "city": cities,
                "state_code": state_codes,
                "region": [ref.REGIONS_BY_STATE[str(code)] for code in state_codes],
                "pincode": pincodes,
                "marketing_segment": marketing,
                "annual_income": annual_income,
                "kyc_status": kyc,
                "preferred_channel": preferred,
                "onboarded_date": onboarded_dates,
                "relationship_manager_id": [f"RM{int(i) % 240:04d}" for i in rng.integers(0, 240, n)],
            }
        )
        self._customers = frame
        return frame

    def accounts_base(self) -> pl.DataFrame:
        """Account dimension with a deliberate CRM/core region conflict."""
        if self._accounts is not None:
            return self._accounts

        rng = rng_for(self.seed, "accounts")
        customers = self.customers_base()
        clusters, _states, _scores, _idx = self._customer_basis()
        n = self.n_accounts
        n_customers = self.n_customers

        # Guarantee every customer owns at least one account.
        owner = np.empty(n, dtype=int)
        owner[:n_customers] = np.arange(n_customers)
        if n > n_customers:
            owner[n_customers:] = rng.integers(0, n_customers, n - n_customers)
        owner = np.sort(owner)

        account_type = weighted_choice(rng, ref.ACCOUNT_TYPES, ref.ACCOUNT_TYPE_WEIGHTS, n)

        balances = np.empty(n, dtype=float)
        for i, cluster in enumerate(clusters):
            mask = owner == i
            count = int(mask.sum())
            if count == 0:
                continue
            profile = ref.CLUSTER_PROFILE[str(cluster)]
            balances[mask] = lognormal_amounts(rng, profile["balance_mean"], 0.55, count)
        # Fixed deposits hold materially more than transactional accounts.
        balances *= np.where(account_type == "FIXED_DEPOSIT", 3.4, 1.0)
        balances *= np.where(account_type == "CURRENT", 1.6, 1.0)

        customer_region = customers["region"].to_numpy()
        branch_region: np.ndarray = np.array([customer_region[i] for i in owner], dtype=object)
        # 4% of accounts were opened at a branch in a different state.
        conflict = rng.random(n) < ref.REGION_CONFLICT_RATE
        for position in np.flatnonzero(conflict):
            options = [r for r in ref.REGIONS_BY_STATE.values() if r != branch_region[position]]
            branch_region[position] = rng.choice(options)

        states = customers["state_code"].to_numpy()
        branch_state = np.array([states[i] for i in owner], dtype=object)

        status = weighted_choice(
            rng, ref.ACCOUNT_STATUSES, {"ACTIVE": 0.90, "DORMANT": 0.06, "CLOSED": 0.03, "FROZEN": 0.01}, n
        )
        open_offsets = rng.integers(0, 365 * 9, n)
        base_open = date(self.start_date.year - 9, 1, 1)

        frame = pl.DataFrame(
            {
                "account_id": [f"A{i:08d}" for i in range(n)],
                "customer_id": [f"C{i:07d}" for i in owner],
                "account_type": account_type,
                "branch_id": [
                    f"BR{st}{int(b) % 900:03d}" for st, b in zip(branch_state, rng.integers(0, 900, n), strict=True)
                ],
                "branch_region": branch_region,
                "currency": "INR",
                "open_date": [base_open + timedelta(days=int(d)) for d in open_offsets],
                "status": status,
                "balance": balances,
                "overdraft_limit": np.where(
                    account_type == "CURRENT", np.round(rng.uniform(50_000, 2_000_000, n), 2), 0.0
                ),
                "interest_rate": np.where(
                    account_type == "SAVINGS", 3.5, np.where(account_type == "FIXED_DEPOSIT", 7.1, 0.0)
                ),
            }
        )
        self._accounts = frame
        return frame

    def cards_base(self) -> pl.DataFrame:
        if self._cards is not None:
            return self._cards

        rng = rng_for(self.seed, "cards")
        # Built for its side effect of warming the customer basis this depends on;
        # the frame itself is read through ``_customer_basis`` below.
        _customers = self.customers_base()
        clusters, _states, _scores, _idx = self._customer_basis()
        n = min(self.n_cards, self.n_customers)
        holder = rng.choice(self.n_customers, size=n, replace=False)

        card_type = weighted_choice(rng, ref.CARD_TYPES, {"CREDIT": 0.44, "DEBIT": 0.56}, n)
        limits = np.array(
            [
                max(25_000.0, ref.CLUSTER_PROFILE[str(clusters[i])]["income_mean"] * 0.9 * rng.uniform(0.4, 2.1))
                for i in holder
            ]
        ).round(-3)

        frame = pl.DataFrame(
            {
                "card_id": [f"CD{i:07d}" for i in range(n)],
                "customer_id": [f"C{i:07d}" for i in holder],
                "card_type": card_type,
                "network": weighted_choice(rng, ref.CARD_NETWORKS, ref.CARD_NETWORK_WEIGHTS, n),
                "masked_number": [f"XXXX-XXXX-XXXX-{int(v):04d}" for v in rng.integers(0, 9999, n)],
                "credit_limit": np.where(card_type == "CREDIT", limits, 0.0),
                "issue_date": [
                    self.first_window.start.date() - timedelta(days=int(d)) for d in rng.integers(30, 2_000, n)
                ],
                "expiry_date": [
                    self.last_window.end.date() + timedelta(days=int(d)) for d in rng.integers(200, 1_400, n)
                ],
                "status": weighted_choice(
                    rng,
                    ("ACTIVE", "BLOCKED", "EXPIRED", "CLOSED"),
                    {"ACTIVE": 0.93, "BLOCKED": 0.03, "EXPIRED": 0.02, "CLOSED": 0.02},
                    n,
                ),
            }
        )
        self._cards = frame
        return frame

    def bureau_base(self) -> pl.DataFrame:
        """One bureau extract per month, one row per customer."""
        if self._bureau is not None:
            return self._bureau

        rng = rng_for(self.seed, "bureau")
        _clusters, _states, scores, _idx = self._customer_basis()
        n = self.n_customers

        frame = pl.DataFrame(
            {
                "bureau_id": [f"B{i:07d}" for i in range(n)],
                "customer_id": [f"C{i:07d}" for i in range(n)],
                "credit_score": np.clip(scores, 300, 900).round().astype(int),
                "enquiries_6m": rng.poisson(1.6, n),
                "delinquencies_24m": rng.poisson(0.35, n),
                "credit_vintage_months": np.clip(rng.normal(74, 43, n), 3, 340).astype(int),
                "total_exposure": lognormal_amounts(rng, 480_000, 0.85, n),
                "active_tradelines": np.clip(rng.poisson(2.4, n), 0, 18),
            }
        )
        self._bureau = frame
        return frame

    def loans_base(self) -> pl.DataFrame:
        if self._loans is not None:
            return self._loans

        rng = rng_for(self.seed, "loans")
        clusters, _states, _scores, _idx = self._customer_basis()
        n = self.n_loans
        borrower = rng.integers(0, self.n_customers, n)

        products = weighted_choice(rng, tuple(ref.LOAN_PRODUCTS), ref.LOAN_PRODUCT_WEIGHTS, n)
        principals = np.array(
            [ref.CLUSTER_PROFILE[str(clusters[b])]["income_mean"] * rng.uniform(0.6, 6.5) for b in borrower]
        ).round(-2)

        rates = np.array([rng.uniform(*ref.LOAN_PRODUCTS[str(p)][:2]) for p in products]).round(2)
        tenors = np.array([int(rng.choice(ref.LOAN_PRODUCTS[str(p)][2])) for p in products])

        # EMI from the standard amortisation formula.
        monthly_rate = rates / 12.0 / 100.0
        emi = np.where(
            monthly_rate > 0,
            principals * monthly_rate / (1 - (1 + monthly_rate) ** (-tenors)),
            principals / tenors,
        ).round(2)

        # 6% of borrowers are engineered toward delinquency.
        default_prone = rng.random(n) < 0.06
        dpd = np.where(default_prone, rng.choice([32, 45, 62, 91, 128], n), 0)
        # Near-prime borrowers carry more stress.
        stressed = np.array([clusters[b] == "NEAR_PRIME_REVOLVING" for b in borrower])
        dpd = np.where(stressed & (rng.random(n) < 0.18), rng.choice([31, 35, 48], n), dpd)

        outstanding = np.where(
            dpd >= 90, principals * rng.uniform(0.55, 0.95, n), principals * rng.uniform(0.05, 0.85, n)
        ).round(2)

        status = np.select(
            [dpd >= 90, dpd > 0, outstanding < principals * 0.02],
            ["DEFAULT", "DELINQUENT", "CLOSED"],
            default="ACTIVE",
        )

        collateral_type = np.array([ref.COLLATERAL_TYPES.get(str(p), "NONE") for p in products], dtype=object)
        cover = np.array([ref.LOAN_PRODUCTS[str(p)][4] for p in products])
        collateral_value = np.where(
            collateral_type == "NONE",
            0.0,
            (principals * cover * rng.uniform(0.75, 1.25, n)).round(2),
        )

        frame = pl.DataFrame(
            {
                "loan_id": [f"L{i:07d}" for i in range(n)],
                "customer_id": [f"C{i:07d}" for i in borrower],
                "product": products,
                "principal": principals,
                "interest_rate": rates,
                "tenor_months": tenors,
                "emi_amount": emi,
                "disbursed_date": [
                    self.first_window.start.date() - timedelta(days=int(d)) for d in rng.integers(60, 2_400, n)
                ],
                "outstanding_principal": outstanding,
                "dpd": dpd.astype(int),
                "status": status,
                "collateral_type": collateral_type,
                "collateral_value": collateral_value,
            }
        )
        self._loans = frame
        return frame

    # -- monthly snapshots ---------------------------------------------------
    def customers_snapshot(self, window: MonthWindow) -> pl.DataFrame:
        """Monthly CRM extract. Month 7 onwards carries an additive schema drift."""
        base = self.customers_base()
        rng = rng_for(self.seed, "customers", "snapshot", window.label)

        frame = base.clone()
        # A small number of customers change KYC state each month. Build the full
        # replacement column rather than broadcasting a short series into `then`.
        churn = rng.random(self.n_customers) < 0.015
        if churn.any():
            kyc = frame["kyc_status"].to_numpy().astype(object)
            kyc[churn] = weighted_choice(
                rng,
                ref.KYC_STATUSES,
                {"VERIFIED": 0.5, "PENDING": 0.2, "RE_KYC_DUE": 0.2, "DOCS_PENDING": 0.1},
                int(churn.sum()),
            )
            frame = frame.with_columns(pl.Series("kyc_status", kyc))

        if window.index >= ref.SCHEMA_DRIFT_MONTH:
            # Upstream CRM adds a column. The silver layer must evolve, not break.
            frame = frame.with_columns(
                pl.Series(
                    "preferred_language",
                    weighted_choice(
                        rng,
                        ref.PREFERRED_LANGUAGES,
                        {
                            "EN": 0.42,
                            "HI": 0.22,
                            "MR": 0.09,
                            "TA": 0.08,
                            "KN": 0.06,
                            "TE": 0.06,
                            "BN": 0.04,
                            "GU": 0.03,
                        },
                        self.n_customers,
                    ),
                )
            )

        file_name = f"s_crm/customers/batch_month={window.label}/part-000.parquet"
        return add_ingest_metadata(
            frame,
            source="s_crm",
            entity="customers",
            batch_id=window.batch_id,
            ingested_at=window.end,
            source_file=file_name,
            key_columns=["customer_id"],
        )

    def accounts_snapshot(self, window: MonthWindow) -> pl.DataFrame:
        """Monthly core-banking account extract. Month 9 arrives damaged."""
        base = self.accounts_base()
        rng = rng_for(self.seed, "accounts", "snapshot", window.label)
        n = base.height
        frame = base.clone()

        # Normal month-to-month balance drift.
        drift = rng.normal(1.0, 0.045, n)
        frame = frame.with_columns((pl.col("balance") * pl.Series(drift)).round(2).alias("balance"))

        if window.index == ref.DQ_INCIDENT_MONTH:
            # A three-day upstream incident: missing branch region on a slice of
            # rows, and impossible negative balances on savings accounts.
            missing = rng.random(n) < 0.15
            negative = (rng.random(n) < 0.07) & (frame["account_type"] == "SAVINGS").to_numpy()
            frame = frame.with_columns(
                pl.when(pl.Series(missing))
                .then(pl.lit(None, dtype=pl.Utf8))
                .otherwise(pl.col("branch_region"))
                .alias("branch_region"),
                pl.when(pl.Series(negative)).then(-pl.col("balance")).otherwise(pl.col("balance")).alias("balance"),
            )

        file_name = f"s_core/accounts/batch_month={window.label}/part-000.parquet"
        return add_ingest_metadata(
            frame,
            source="s_core",
            entity="accounts",
            batch_id=window.batch_id,
            ingested_at=window.end,
            source_file=file_name,
            key_columns=["account_id"],
        )

    def loans_snapshot(self, window: MonthWindow) -> pl.DataFrame:
        base = self.loans_base()
        rng = rng_for(self.seed, "loans", "snapshot", window.label)
        n = base.height
        # Amortise: outstanding reduces, DPD evolves.
        factor = 1 - (0.0055 + rng.normal(0, 0.0006, n))
        frame = base.with_columns(
            (pl.col("outstanding_principal") * pl.Series(factor))
            .clip(lower_bound=0)
            .round(2)
            .alias("outstanding_principal")
        )
        file_name = f"s_loans/loans/batch_month={window.label}/part-000.parquet"
        return add_ingest_metadata(
            frame,
            source="s_loans",
            entity="loans",
            batch_id=window.batch_id,
            ingested_at=window.end,
            source_file=file_name,
            key_columns=["loan_id"],
        )

    def cards_snapshot(self, window: MonthWindow) -> pl.DataFrame:
        base = self.cards_base()
        file_name = f"s_cards/cards/batch_month={window.label}/part-000.parquet"
        return add_ingest_metadata(
            base.clone(),
            source="s_cards",
            entity="cards",
            batch_id=window.batch_id,
            ingested_at=window.end,
            source_file=file_name,
            key_columns=["card_id"],
        )

    def bureau_snapshot(self, window: MonthWindow) -> pl.DataFrame:
        base = self.bureau_base()
        rng = rng_for(self.seed, "bureau", "snapshot", window.label)
        n = base.height
        # Scores wander month to month; a few customers deteriorate.
        delta = rng.normal(0, 9, n)
        frame = base.with_columns(
            pl.Series("credit_score", np.clip(base["credit_score"].to_numpy() + delta, 300, 900).round().astype(int)),
            pl.lit(window.end.date()).alias("as_of_date"),
        )
        frame = frame.with_columns(
            pl.when(pl.col("credit_score") >= 800)
            .then(pl.lit("EXCELLENT"))
            .when(pl.col("credit_score") >= 750)
            .then(pl.lit("GOOD"))
            .when(pl.col("credit_score") >= 700)
            .then(pl.lit("FAIR"))
            .when(pl.col("credit_score") >= 650)
            .then(pl.lit("POOR"))
            .otherwise(pl.lit("VERY_POOR"))
            .alias("score_band")
        )
        file_name = f"s_bureau/credit_records/batch_month={window.label}/part-000.parquet"
        return add_ingest_metadata(
            frame,
            source="s_bureau",
            entity="credit_records",
            batch_id=window.batch_id,
            ingested_at=window.end,
            source_file=file_name,
            key_columns=["bureau_id"],
        )

    # -- monthly facts -------------------------------------------------------
    def _account_activity_targets(self, window: MonthWindow) -> np.ndarray:
        """Expected transaction count per account for this month."""
        rng = rng_for(self.seed, "txn_counts", window.label)
        accounts = self.accounts_base()
        owner_idx = self._owner_index(accounts)
        per_customer = self._customer_txn_rate()[owner_idx]
        accounts_per_customer = np.bincount(owner_idx, minlength=self.n_customers)
        per_account = per_customer / np.maximum(accounts_per_customer[owner_idx], 1)
        seasonal = SEASONALITY[window.start.month]
        counts = rng.poisson(np.maximum(per_account * seasonal, 0.05))
        # Closed/dormant accounts are far quieter.
        status = accounts["status"].to_numpy()
        counts = np.where(status == "ACTIVE", counts, (counts * 0.15).astype(int))
        return counts.astype(int)

    def core_transactions(self, window: MonthWindow) -> pl.DataFrame:
        rng = rng_for(self.seed, "core_txn", window.label)
        accounts = self.accounts_base()
        counts = self._account_activity_targets(window)
        total = int(counts.sum())
        if total == 0:
            return self._empty_core_transactions()

        idx = np.repeat(np.arange(accounts.height), counts)
        channel = weighted_choice(rng, ref.CORE_CHANNELS, ref.CORE_CHANNEL_WEIGHTS, total)

        # Vectorised amount shaping: per-customer scale x lognormal noise,
        # clamped to each rail's plausible band.
        scale = self._customer_amount_scale()[self._owner_index(accounts)[idx]]
        shape = rng.lognormal(mean=0.0, sigma=1.05, size=total)
        band_low = np.array([ref.CHANNEL_AMOUNT_BAND[str(c)][0] for c in channel])
        band_high = np.array([ref.CHANNEL_AMOUNT_BAND[str(c)][1] for c in channel])
        amounts = np.clip(scale * shape, band_low, band_high).round(2)

        debit = rng.random(total) < 0.6
        ts = random_timestamps(rng, window.start, window.end, total)
        tx_ids = [f"T{window.index:02d}{i:010d}" for i in range(total)]

        frame = pl.DataFrame(
            {
                "transaction_id": tx_ids,
                "account_id": accounts["account_id"].to_numpy()[idx],
                "customer_id": accounts["customer_id"].to_numpy()[idx],
                "transaction_ts": ts,
                "amount": amounts,
                "currency": "INR",
                "debit_credit": np.where(debit, "DEBIT", "CREDIT"),
                "channel": channel,
                "narration": [f"{c} txn" for c in channel],
                "counterparty_ref": [f"CP{int(v):08d}" for v in rng.integers(0, 99_999_999, total)],
                "branch_id": accounts["branch_id"].to_numpy()[idx],
            }
        )
        file_name = f"s_core/transactions/batch_month={window.label}/part-000.parquet"
        return add_ingest_metadata(
            frame,
            source="s_core",
            entity="transactions",
            batch_id=window.batch_id,
            ingested_at=window.end,
            source_file=file_name,
            key_columns=["transaction_id"],
        )

    def _empty_core_transactions(self) -> pl.DataFrame:
        return pl.DataFrame(
            schema={
                "transaction_id": pl.Utf8,
                "account_id": pl.Utf8,
                "customer_id": pl.Utf8,
                "transaction_ts": pl.Datetime("us"),
                "amount": pl.Float64,
                "currency": pl.Utf8,
                "debit_credit": pl.Utf8,
                "channel": pl.Utf8,
                "narration": pl.Utf8,
                "counterparty_ref": pl.Utf8,
                "branch_id": pl.Utf8,
            }
        )

    def card_transactions(self, window: MonthWindow) -> pl.DataFrame:
        rng = rng_for(self.seed, "card_txn", window.label)
        cards = self.cards_base()
        holder_idx = self._owner_index(cards)

        seasonal = SEASONALITY[window.start.month]
        counts = rng.poisson(np.maximum(7.5 * seasonal, 0.05), cards.height)
        total = int(counts.sum())
        if total == 0:
            return pl.DataFrame(schema={"transaction_id": pl.Utf8})

        idx = np.repeat(np.arange(cards.height), counts)
        channel = weighted_choice(rng, ref.CARD_CHANNELS, ref.CARD_CHANNEL_WEIGHTS, total)
        category = weighted_choice(rng, ref.MERCHANT_CATEGORIES, ref.MERCHANT_CATEGORY_WEIGHTS, total)

        merchant_ids = np.array([f"M{int(v):06d}" for v in rng.integers(0, 500_000, total)], dtype=object)
        scale = self._customer_amount_scale()[holder_idx[idx]]
        amounts = np.abs(scale * rng.lognormal(mean=0.0, sigma=1.2, size=total))
        band_low = np.array([ref.CHANNEL_AMOUNT_BAND[str(c)][0] for c in channel])
        band_high = np.array([ref.CHANNEL_AMOUNT_BAND[str(c)][1] for c in channel])
        amounts = np.clip(amounts, band_low, band_high).round(2)

        is_intl = np.array([str(c) == "INTERNATIONAL" for c in channel])
        ts = random_timestamps(rng, window.start, window.end, total)

        frame = pl.DataFrame(
            {
                "transaction_id": [f"T{window.index:02d}9{i:09d}" for i in range(total)],
                "card_id": cards["card_id"].to_numpy()[idx],
                "customer_id": cards["customer_id"].to_numpy()[idx],
                "transaction_ts": ts,
                "amount": amounts,
                "currency": np.where(is_intl, "USD", "INR"),
                "channel": channel,
                "merchant_id": merchant_ids,
                "merchant_category": category,
                "merchant_country": np.where(is_intl, "SGP", "IND"),
                "is_international": is_intl,
                "device_id": [f"D{int(v):08d}" for v in rng.integers(0, 99_999_999, total)],
                "debit_credit": np.where(rng.random(total) < 0.985, "DEBIT", "CREDIT"),
            }
        )
        file_name = f"s_cards/card_transactions/batch_month={window.label}/part-000.parquet"
        return add_ingest_metadata(
            frame,
            source="s_cards",
            entity="card_transactions",
            batch_id=window.batch_id,
            ingested_at=window.end,
            source_file=file_name,
            key_columns=["transaction_id"],
        )

    def payment_transactions(self, window: MonthWindow) -> pl.DataFrame:
        rng = rng_for(self.seed, "pay_txn", window.label)
        accounts = self.accounts_base()
        owner_idx = self._owner_index(accounts)
        seasonal = SEASONALITY[window.start.month]
        per_account = self._customer_txn_rate()[owner_idx] / 3.1
        counts = rng.poisson(np.maximum(per_account * seasonal, 0.05))
        total = int(counts.sum())
        if total == 0:
            return pl.DataFrame(schema={"transaction_id": pl.Utf8})

        idx = np.repeat(np.arange(accounts.height), counts)
        channel = weighted_choice(rng, ref.PAYMENT_CHANNELS, ref.PAYMENT_CHANNEL_WEIGHTS, total)
        scale = self._customer_amount_scale()[owner_idx[idx]]
        amounts = np.abs(scale * rng.lognormal(mean=-0.3, sigma=1.25, size=total))
        band_low = np.array([ref.CHANNEL_AMOUNT_BAND[str(c)][0] for c in channel])
        band_high = np.array([ref.CHANNEL_AMOUNT_BAND[str(c)][1] for c in channel])
        amounts = np.clip(amounts, band_low, band_high).round(2)

        frame = pl.DataFrame(
            {
                "transaction_id": [f"T{window.index:02d}7{i:09d}" for i in range(total)],
                "account_id": accounts["account_id"].to_numpy()[idx],
                "customer_id": accounts["customer_id"].to_numpy()[idx],
                "transaction_ts": random_timestamps(rng, window.start, window.end, total),
                "amount": amounts,
                "currency": "INR",
                "channel": channel,
                "debit_credit": np.where(rng.random(total) < 0.62, "DEBIT", "CREDIT"),
                "beneficiary_vpa": [f"user{int(v)}@okaxis" for v in rng.integers(0, 9_999_999, total)],
                "beneficiary_bank": weighted_choice(rng, ("HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YES"), None, total),
                "narration": [f"{c} payment" for c in channel],
            }
        )
        file_name = f"s_payments/payment_transactions/batch_month={window.label}/part-000.parquet"
        return add_ingest_metadata(
            frame,
            source="s_payments",
            entity="payment_transactions",
            batch_id=window.batch_id,
            ingested_at=window.end,
            source_file=file_name,
            key_columns=["transaction_id"],
        )

    def repayments(self, window: MonthWindow) -> pl.DataFrame:
        rng = rng_for(self.seed, "repayments", window.label)
        loans = self.loans_base()
        n = loans.height
        due = window.start.date().replace(day=5)
        paid_offset = np.where(
            loans["dpd"].to_numpy() > 0,
            rng.integers(1, 130, n),
            rng.integers(-3, 4, n).clip(min=0),
        )
        frame = pl.DataFrame(
            {
                "repayment_id": [f"R{window.index:02d}{i:07d}" for i in range(n)],
                "loan_id": loans["loan_id"],
                "customer_id": loans["customer_id"],
                "due_date": due,
                "paid_date": [due + timedelta(days=int(d)) for d in paid_offset],
                "amount_due": loans["emi_amount"],
                "amount_paid": (loans["emi_amount"] * np.where(loans["dpd"].to_numpy() > 0, 0.0, 1.0)).round(2),
                "dpd_at_payment": np.where(loans["dpd"].to_numpy() > 0, paid_offset, 0),
            }
        )
        file_name = f"s_loans/repayments/batch_month={window.label}/part-000.parquet"
        return add_ingest_metadata(
            frame,
            source="s_loans",
            entity="repayments",
            batch_id=window.batch_id,
            ingested_at=window.end,
            source_file=file_name,
            key_columns=["repayment_id"],
        )

    # -- fraud injection -----------------------------------------------------
    def inject_fraud(
        self,
        window: MonthWindow,
        core: pl.DataFrame,
        card: pl.DataFrame,
        payments: pl.DataFrame,
    ) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
        """Create genuinely anomalous transactions and record their labels.

        This *generates* the fraud, it does not merely label rows that already
        exist. Each modus operandi is designed to be detectable from the feature
        set the fraud agent consumes — otherwise the exercise would prove
        nothing.

        Returns ``(core, card, payments, labels)``. The source systems carry no
        fraud label: at decision time the bank does not know. The labels land in
        a holdout table that agent code must never read, so precision and recall
        are measured honestly.
        """
        rng = rng_for(self.seed, "fraud", window.label)
        cards = self.cards_base()
        accounts = self.accounts_base()
        amount_scale = self._customer_amount_scale()

        # Cases scale with the customer base, so the fraud rate stays constant
        # as the dataset grows.
        n_cases = max(2, round(self.n_customers * self.fraud_rate * 2))

        core_rows: list[dict[str, Any]] = []
        card_rows: list[dict[str, Any]] = []
        payment_rows: list[dict[str, Any]] = []
        labels: list[dict[str, Any]] = []
        counter = 0

        def _case_start() -> datetime:
            return window.start + timedelta(
                days=int(rng.integers(0, 27)), hours=int(rng.integers(0, 23)), minutes=int(rng.integers(0, 59))
            )

        def _txn_id() -> str:
            nonlocal counter
            counter += 1
            return f"F{window.index:02d}{counter:08d}"

        def _label(txn_id: str, method: str, source: str, customer_id: str) -> None:
            labels.append(
                {
                    "transaction_id": txn_id,
                    "is_fraud": True,
                    "fraud_method": method,
                    "source_system": source,
                    "customer_id": customer_id,
                }
            )

        for case in range(n_cases):
            method = ref.FRAUD_METHODS[case % len(ref.FRAUD_METHODS)]
            ci = int(rng.integers(0, self.n_customers))
            customer_id = f"C{ci:07d}"
            own_cards = cards.filter(pl.col("customer_id") == customer_id)
            own_accounts = accounts.filter(pl.col("customer_id") == customer_id)
            if own_cards.height == 0 or own_accounts.height == 0:
                continue

            card_id = str(own_cards["card_id"][int(rng.integers(0, own_cards.height))])
            account_id = str(own_accounts["account_id"][int(rng.integers(0, own_accounts.height))])
            branch_id = str(own_accounts["branch_id"][int(rng.integers(0, own_accounts.height))])
            typical = max(float(amount_scale[ci]), 500.0)
            start = _case_start()

            # --- 1. Card-not-present burst: many large e-commerce charges, minutes apart
            if method == "CARD_NOT_PRESENT_BURST":
                device = f"DF{int(rng.integers(0, 99_999_999)):08d}"
                offset = 0
                for _ in range(int(rng.integers(5, 9))):
                    offset += int(rng.integers(40, 260))
                    txn_id = _txn_id()
                    card_rows.append(
                        {
                            "transaction_id": txn_id,
                            "card_id": card_id,
                            "customer_id": customer_id,
                            "transaction_ts": start + timedelta(seconds=offset),
                            "amount": round(typical * float(rng.uniform(9, 26)), 2),
                            "currency": "INR",
                            "channel": "ECOM",
                            "merchant_id": f"M{int(rng.integers(0, 500_000)):06d}",
                            "merchant_category": "Ecommerce",
                            "merchant_country": "IND",
                            "is_international": False,
                            "device_id": device,
                            "debit_credit": "DEBIT",
                        }
                    )
                    _label(txn_id, method, "s_cards", customer_id)

            # --- 2. Geo jump: a domestic charge, then an international one very soon after
            elif method == "GEO_JUMP":
                domestic_id = _txn_id()
                card_rows.append(
                    {
                        "transaction_id": domestic_id,
                        "card_id": card_id,
                        "customer_id": customer_id,
                        "transaction_ts": start,
                        "amount": round(typical * float(rng.uniform(0.8, 2.5)), 2),
                        "currency": "INR",
                        "channel": "POS",
                        "merchant_id": f"M{int(rng.integers(0, 500_000)):06d}",
                        "merchant_category": "Restaurant",
                        "merchant_country": "IND",
                        "is_international": False,
                        "device_id": f"D{int(rng.integers(0, 99_999_999)):08d}",
                        "debit_credit": "DEBIT",
                    }
                )
                jump_id = _txn_id()
                card_rows.append(
                    {
                        "transaction_id": jump_id,
                        "card_id": card_id,
                        "customer_id": customer_id,
                        "transaction_ts": start + timedelta(minutes=int(rng.integers(45, 150))),
                        "amount": round(typical * float(rng.uniform(12, 34)), 2),
                        "currency": "USD",
                        "channel": "INTERNATIONAL",
                        "merchant_id": f"M{int(rng.integers(0, 500_000)):06d}",
                        "merchant_category": "Electronics",
                        "merchant_country": "SGP",
                        "is_international": True,
                        "device_id": f"DF{int(rng.integers(0, 99_999_999)):08d}",
                        "debit_credit": "DEBIT",
                    }
                )
                _label(jump_id, method, "s_cards", customer_id)

            # --- 3. Micro-then-macro: a tiny probe, then a large outbound transfer
            elif method == "MICRO_THEN_MACRO":
                probe_id = _txn_id()
                payment_rows.append(
                    {
                        "transaction_id": probe_id,
                        "account_id": account_id,
                        "customer_id": customer_id,
                        "transaction_ts": start,
                        "amount": float(rng.integers(1, 6)),
                        "currency": "INR",
                        "channel": "UPI",
                        "debit_credit": "DEBIT",
                        "beneficiary_vpa": f"probe{int(rng.integers(0, 9_999_999))}@okaxis",
                        "beneficiary_bank": "AXIS",
                        "narration": "UPI payment",
                    }
                )
                macro_id = _txn_id()
                payment_rows.append(
                    {
                        "transaction_id": macro_id,
                        "account_id": account_id,
                        "customer_id": customer_id,
                        "transaction_ts": start + timedelta(minutes=int(rng.integers(3, 13))),
                        "amount": round(typical * float(rng.uniform(22, 65)), 2),
                        "currency": "INR",
                        "channel": "IMPS",
                        "debit_credit": "DEBIT",
                        "beneficiary_vpa": f"mule{int(rng.integers(0, 9_999_999))}@okhdfcbank",
                        "beneficiary_bank": "HDFC",
                        "narration": "IMPS payment",
                    }
                )
                _label(probe_id, method, "s_payments", customer_id)
                _label(macro_id, method, "s_payments", customer_id)

            # --- 4. Merchant collusion: a ring charging many cards at one merchant
            elif method == "MERCHANT_COLLUSION":
                ring_merchant = f"MR{int(rng.integers(0, 99999)):05d}"
                ring_size = int(rng.integers(3, 6))
                for _ in range(ring_size):
                    other = int(rng.integers(0, cards.height))
                    txn_id = _txn_id()
                    card_rows.append(
                        {
                            "transaction_id": txn_id,
                            "card_id": str(cards["card_id"][other]),
                            "customer_id": str(cards["customer_id"][other]),
                            # Suspiciously round, near-identical amounts.
                            "transaction_ts": start + timedelta(minutes=int(rng.integers(0, 120))),
                            "amount": 9_999.0,
                            "currency": "INR",
                            "channel": "POS",
                            "merchant_id": ring_merchant,
                            "merchant_category": "Jewellery",
                            "merchant_country": "IND",
                            "is_international": False,
                            "device_id": f"D{int(rng.integers(0, 99_999_999)):08d}",
                            "debit_credit": "DEBIT",
                        }
                    )
                    _label(txn_id, method, "s_cards", str(cards["customer_id"][other]))

            # --- 5. Account takeover: odd-hour test credit, then a large transfer out
            else:
                odd_hour = start.replace(hour=int(rng.integers(1, 5)), minute=int(rng.integers(0, 59)))
                test_credit_id = _txn_id()
                core_rows.append(
                    {
                        "transaction_id": test_credit_id,
                        "account_id": account_id,
                        "customer_id": customer_id,
                        "transaction_ts": odd_hour,
                        "amount": 1.0,
                        "currency": "INR",
                        "debit_credit": "CREDIT",
                        "channel": "INTERNAL_TRANSFER",
                        "narration": "INTERNAL_TRANSFER txn",
                        "counterparty_ref": f"CP{int(rng.integers(0, 99_999_999)):08d}",
                        "branch_id": branch_id,
                    }
                )
                drain_id = _txn_id()
                core_rows.append(
                    {
                        "transaction_id": drain_id,
                        "account_id": account_id,
                        "customer_id": customer_id,
                        "transaction_ts": odd_hour + timedelta(minutes=int(rng.integers(4, 21))),
                        "amount": round(typical * float(rng.uniform(30, 90)), 2),
                        "currency": "INR",
                        "debit_credit": "DEBIT",
                        "channel": "INTERNAL_TRANSFER",
                        "narration": "INTERNAL_TRANSFER txn",
                        "counterparty_ref": f"CP{int(rng.integers(0, 99_999_999)):08d}",
                        "branch_id": branch_id,
                    }
                )
                _label(test_credit_id, method, "s_core", customer_id)
                _label(drain_id, method, "s_core", customer_id)

        def _augment(base: pl.DataFrame, rows: list[dict[str, Any]]) -> pl.DataFrame:
            """Append injected rows, tolerating the metadata columns the base has
            and the injected rows do not (the caller re-applies provenance)."""
            if not rows:
                return base
            extra = pl.DataFrame(rows)
            if base.height == 0:
                return extra
            return pl.concat([base, extra], how="diagonal_relaxed")

        label_frame = pl.DataFrame(
            labels,
            schema={
                "transaction_id": pl.Utf8,
                "is_fraud": pl.Boolean,
                "fraud_method": pl.Utf8,
                "source_system": pl.Utf8,
                "customer_id": pl.Utf8,
            },
            strict=False,
        )
        if label_frame.height:
            label_frame = label_frame.unique(subset=["transaction_id"], keep="first").with_columns(
                # A label only becomes known once the dispute window closes.
                (pl.lit(self.last_window.end.date()) - pl.duration(days=90)).alias("label_available_date")
            )

        return (
            _augment(core, core_rows),
            _augment(card, card_rows),
            _augment(payments, payment_rows),
            label_frame,
        )


__all__ = [
    "MonthWindow",
    "SyntheticBank",
    "add_ingest_metadata",
    "clamp_to_band",
    "lognormal_amounts",
    "month_windows",
    "random_timestamps",
    "rng_for",
    "weighted_choice",
]
