"""Static reference data for the synthetic bank.

Reference data lives here rather than inside the generators so the same
vocabularies can be imported by the semantic layer, the policy corpus, and the
tests. A test asserting "every gold ``region`` is a known state" is only
meaningful if both sides read the same list.
"""

from __future__ import annotations

from typing import Final

# --- Geography --------------------------------------------------------------
#: state code -> (state name, cities, pincode prefix)
STATES: Final[dict[str, tuple[str, tuple[str, ...], str]]] = {
    "MH": ("Maharashtra", ("Mumbai", "Pune", "Nagpur", "Nashik"), "4"),
    "KA": ("Karnataka", ("Bengaluru", "Mysuru", "Mangaluru"), "5"),
    "TN": ("Tamil Nadu", ("Chennai", "Coimbatore", "Madurai"), "6"),
    "DL": ("Delhi", ("New Delhi", "Dwarka", "Rohini"), "1"),
    "GJ": ("Gujarat", ("Ahmedabad", "Surat", "Vadodara"), "3"),
    "TS": ("Telangana", ("Hyderabad", "Warangal"), "5"),
    "WB": ("West Bengal", ("Kolkata", "Howrah"), "7"),
    "RJ": ("Rajasthan", ("Jaipur", "Jodhpur"), "3"),
    "UP": ("Uttar Pradesh", ("Lucknow", "Noida", "Kanpur"), "2"),
    "KL": ("Kerala", ("Kochi", "Thiruvananthapuram"), "6"),
}

STATE_CODES: Final[tuple[str, ...]] = tuple(STATES)

#: Relative population weight per state (drives realistic regional distribution).
STATE_WEIGHTS: Final[dict[str, float]] = {
    "MH": 0.18,
    "KA": 0.12,
    "TN": 0.11,
    "DL": 0.10,
    "GJ": 0.09,
    "TS": 0.09,
    "WB": 0.08,
    "RJ": 0.07,
    "UP": 0.09,
    "KL": 0.07,
}

REGIONS_BY_STATE: Final[dict[str, str]] = {code: name for code, (name, _, _) in STATES.items()}

# --- Customer behaviour clusters -------------------------------------------
#: Each cluster drives income, balance, transaction frequency, amount shape and
#: channel mix. Without this structure the synthetic data is i.i.d. noise and
#: every downstream analysis is trivially correct — which would make the project
#: prove nothing.
CLUSTERS: Final[tuple[str, ...]] = (
    "SALARIED_STABLE",
    "GIG_VARIABLE",
    "HNI_LOW_ACTIVITY",
    "NEAR_PRIME_REVOLVING",
    "DORMANT",
)

CLUSTER_WEIGHTS: Final[dict[str, float]] = {
    "SALARIED_STABLE": 0.38,
    "GIG_VARIABLE": 0.24,
    "HNI_LOW_ACTIVITY": 0.08,
    "NEAR_PRIME_REVOLVING": 0.20,
    "DORMANT": 0.10,
}

CLUSTER_PROFILE: Final[dict[str, dict[str, float]]] = {
    # monthly_income_mean, income_sigma, balance_mean, txns_per_month, txn_amount_mean, card_util
    "SALARIED_STABLE": {
        "income_mean": 95_000,
        "income_sigma": 0.30,
        "balance_mean": 240_000,
        "txns_per_month": 22,
        "txn_amount_mean": 3_200,
        "card_util": 0.22,
        "credit_score_mean": 762,
    },
    "GIG_VARIABLE": {
        "income_mean": 38_000,
        "income_sigma": 0.62,
        "balance_mean": 42_000,
        "txns_per_month": 34,
        "txn_amount_mean": 1_150,
        "card_util": 0.41,
        "credit_score_mean": 694,
    },
    "HNI_LOW_ACTIVITY": {
        "income_mean": 620_000,
        "income_sigma": 0.45,
        "balance_mean": 4_800_000,
        "txns_per_month": 7,
        "txn_amount_mean": 42_000,
        "card_util": 0.14,
        "credit_score_mean": 801,
    },
    "NEAR_PRIME_REVOLVING": {
        "income_mean": 52_000,
        "income_sigma": 0.35,
        "balance_mean": 26_000,
        "txns_per_month": 41,
        "txn_amount_mean": 2_050,
        "card_util": 0.74,
        "credit_score_mean": 641,
    },
    "DORMANT": {
        "income_mean": 28_000,
        "income_sigma": 0.40,
        "balance_mean": 9_500,
        "txns_per_month": 2,
        "txn_amount_mean": 1_400,
        "card_util": 0.05,
        "credit_score_mean": 668,
    },
}

# --- Products ---------------------------------------------------------------
ACCOUNT_TYPES: Final[tuple[str, ...]] = ("SAVINGS", "CURRENT", "SALARY", "NRE", "FIXED_DEPOSIT")

ACCOUNT_TYPE_WEIGHTS: Final[dict[str, float]] = {
    "SAVINGS": 0.62,
    "CURRENT": 0.11,
    "SALARY": 0.19,
    "NRE": 0.03,
    "FIXED_DEPOSIT": 0.05,
}

ACCOUNT_STATUSES: Final[tuple[str, ...]] = ("ACTIVE", "DORMANT", "CLOSED", "FROZEN")

CARD_NETWORKS: Final[tuple[str, ...]] = ("VISA", "MASTERCARD", "RUPAY")
CARD_NETWORK_WEIGHTS: Final[dict[str, float]] = {"VISA": 0.42, "MASTERCARD": 0.33, "RUPAY": 0.25}
CARD_TYPES: Final[tuple[str, ...]] = ("CREDIT", "DEBIT")

# --- Transaction channels ---------------------------------------------------
#: Core banking rails (branch/ATM/back-office initiated).
CORE_CHANNELS: Final[tuple[str, ...]] = (
    "ATM",
    "BRANCH",
    "CASH_DEPOSIT",
    "INTERNAL_TRANSFER",
    "STANDING_INSTRUCTION",
    "CHEQUE",
)
CORE_CHANNEL_WEIGHTS: Final[dict[str, float]] = {
    "ATM": 0.38,
    "BRANCH": 0.11,
    "CASH_DEPOSIT": 0.09,
    "INTERNAL_TRANSFER": 0.24,
    "STANDING_INSTRUCTION": 0.13,
    "CHEQUE": 0.05,
}

#: Card rails.
CARD_CHANNELS: Final[tuple[str, ...]] = ("POS", "ECOM", "CONTACTLESS", "INTERNATIONAL")
CARD_CHANNEL_WEIGHTS: Final[dict[str, float]] = {
    "POS": 0.48,
    "ECOM": 0.31,
    "CONTACTLESS": 0.17,
    "INTERNATIONAL": 0.04,
}

#: Payments switch rails.
PAYMENT_CHANNELS: Final[tuple[str, ...]] = ("UPI", "NEFT", "RTGS", "IMPS", "BILLPAY")
PAYMENT_CHANNEL_WEIGHTS: Final[dict[str, float]] = {
    "UPI": 0.66,
    "NEFT": 0.13,
    "RTGS": 0.03,
    "IMPS": 0.12,
    "BILLPAY": 0.06,
}

#: Indicative amount bands per rail (INR), used to keep rail amounts plausible.
CHANNEL_AMOUNT_BAND: Final[dict[str, tuple[float, float]]] = {
    "ATM": (500, 25_000),
    "BRANCH": (1_000, 500_000),
    "CASH_DEPOSIT": (1_000, 300_000),
    "INTERNAL_TRANSFER": (500, 750_000),
    "STANDING_INSTRUCTION": (1_000, 90_000),
    "CHEQUE": (5_000, 1_500_000),
    "POS": (120, 45_000),
    "ECOM": (199, 120_000),
    "CONTACTLESS": (60, 5_000),
    "INTERNATIONAL": (1_500, 350_000),
    "UPI": (20, 40_000),
    "NEFT": (1_000, 900_000),
    "RTGS": (200_000, 8_000_000),
    "IMPS": (1_000, 400_000),
    "BILLPAY": (200, 60_000),
}

# --- Merchants --------------------------------------------------------------
MERCHANT_CATEGORIES: Final[tuple[str, ...]] = (
    "Grocery",
    "Restaurant",
    "Fuel",
    "Travel",
    "Electronics",
    "Apparel",
    "Healthcare",
    "Education",
    "Utilities",
    "Entertainment",
    "Jewellery",
    "Ecommerce",
    "Telecom",
    "Insurance",
    "Gaming",
)

MERCHANT_CATEGORY_WEIGHTS: Final[dict[str, float]] = {
    "Grocery": 0.20,
    "Restaurant": 0.15,
    "Fuel": 0.11,
    "Travel": 0.07,
    "Electronics": 0.06,
    "Apparel": 0.09,
    "Healthcare": 0.07,
    "Education": 0.04,
    "Utilities": 0.06,
    "Entertainment": 0.04,
    "Jewellery": 0.02,
    "Ecommerce": 0.06,
    "Telecom": 0.05,
    "Insurance": 0.02,
    "Gaming": 0.01,
}

#: Base chargeback/risk propensity per merchant category (0..1).
MERCHANT_CATEGORY_RISK: Final[dict[str, float]] = {
    "Grocery": 0.02,
    "Restaurant": 0.03,
    "Fuel": 0.04,
    "Travel": 0.18,
    "Electronics": 0.22,
    "Apparel": 0.09,
    "Healthcare": 0.03,
    "Education": 0.05,
    "Utilities": 0.02,
    "Entertainment": 0.11,
    "Jewellery": 0.26,
    "Ecommerce": 0.19,
    "Telecom": 0.06,
    "Insurance": 0.03,
    "Gaming": 0.34,
}

# --- Names ------------------------------------------------------------------
# Deterministic name pools rather than a locale-dependent generator, so the same
# seed produces the same names on every machine and in CI.
FIRST_NAMES: Final[tuple[str, ...]] = (
    "Aarav",
    "Aditi",
    "Advait",
    "Aisha",
    "Ajay",
    "Akash",
    "Alka",
    "Amit",
    "Ananya",
    "Anil",
    "Anjali",
    "Arjun",
    "Aryan",
    "Ashwin",
    "Avni",
    "Bhavna",
    "Chetan",
    "Deepa",
    "Deepak",
    "Divya",
    "Farhan",
    "Ganesh",
    "Gauri",
    "Harsh",
    "Ishita",
    "Jaya",
    "Kabir",
    "Kavya",
    "Kiran",
    "Lakshmi",
    "Manish",
    "Meera",
    "Mohit",
    "Nandini",
    "Neha",
    "Nikhil",
    "Nisha",
    "Pallavi",
    "Pooja",
    "Pranav",
    "Priya",
    "Rahul",
    "Rajesh",
    "Rakesh",
    "Ravi",
    "Rhea",
    "Rohan",
    "Rohit",
    "Saanvi",
    "Sameer",
    "Sandeep",
    "Sanjay",
    "Sara",
    "Shreya",
    "Siddharth",
    "Sneha",
    "Sunita",
    "Tanvi",
    "Varun",
    "Vikram",
    "Vinay",
    "Yash",
    "Zara",
    "Ishaan",
    "Kavita",
    "Lalit",
    "Maya",
    "Naveen",
    "Payal",
    "Ritu",
)

LAST_NAMES: Final[tuple[str, ...]] = (
    "Agarwal",
    "Bansal",
    "Bhat",
    "Chandra",
    "Chatterjee",
    "Chopra",
    "Desai",
    "Deshpande",
    "Dubey",
    "Gandhi",
    "Ganguly",
    "Gupta",
    "Hegde",
    "Iyer",
    "Jain",
    "Joshi",
    "Kapoor",
    "Kaur",
    "Khan",
    "Khanna",
    "Kulkarni",
    "Kumar",
    "Lal",
    "Malhotra",
    "Menon",
    "Mehta",
    "Mishra",
    "Mukherjee",
    "Nair",
    "Naidu",
    "Pandey",
    "Patel",
    "Patil",
    "Pillai",
    "Rao",
    "Reddy",
    "Roy",
    "Saxena",
    "Shah",
    "Sharma",
    "Shetty",
    "Singh",
    "Sinha",
    "Srinivasan",
    "Swamy",
    "Tiwari",
    "Trivedi",
    "Varma",
    "Verma",
    "Yadav",
)

MERCHANT_NAME_PREFIXES: Final[tuple[str, ...]] = (
    "Sri",
    "Shree",
    "Metro",
    "City",
    "Royal",
    "Global",
    "Prime",
    "Unity",
    "Sagar",
    "Krishna",
    "Sunrise",
    "Om",
)

MERCHANT_NAME_SUFFIXES: Final[tuple[str, ...]] = (
    "Mart",
    "Stores",
    "Traders",
    "Enterprises",
    "Associates",
    "Retail",
    "Emporium",
    "Outlet",
    "Sales",
    "Distributors",
)

# --- Loans ------------------------------------------------------------------
#: product -> (rate_low, rate_high, tenor_months_choices, needs_collateral, collateral_cover)
LOAN_PRODUCTS: Final[dict[str, tuple[float, float, tuple[int, ...], bool, float]]] = {
    "HOME": (8.35, 9.75, (120, 180, 240, 300), True, 1.35),
    "PERSONAL": (10.5, 16.5, (12, 24, 36, 48, 60), False, 0.0),
    "AUTO": (8.9, 11.4, (36, 48, 60, 84), True, 1.20),
    "GOLD": (9.25, 12.0, (12, 24, 36), True, 1.50),
    "BUSINESS": (11.0, 18.0, (36, 60, 84, 120), True, 1.15),
    "EDUCATION": (9.5, 13.5, (60, 84, 120), False, 0.0),
}

LOAN_PRODUCT_WEIGHTS: Final[dict[str, float]] = {
    "HOME": 0.22,
    "PERSONAL": 0.34,
    "AUTO": 0.16,
    "GOLD": 0.11,
    "BUSINESS": 0.10,
    "EDUCATION": 0.07,
}

COLLATERAL_TYPES: Final[dict[str, str]] = {
    "HOME": "PROPERTY",
    "AUTO": "VEHICLE",
    "GOLD": "GOLD_ORNAMENT",
    "BUSINESS": "PROPERTY",
}

LOAN_STATUSES: Final[tuple[str, ...]] = ("ACTIVE", "CLOSED", "DELINQUENT", "DEFAULT", "WRITTEN_OFF")

# --- Customer segmentation --------------------------------------------------
CUSTOMER_SEGMENTS: Final[tuple[str, ...]] = ("Mass", "Affluent", "HNI", "Premium")

KYC_STATUSES: Final[tuple[str, ...]] = ("VERIFIED", "PENDING", "RE_KYC_DUE", "DOCS_PENDING")

CHANNEL_PREFERENCES: Final[tuple[str, ...]] = ("MOBILE_APP", "INTERNET_BANKING", "BRANCH", "ATM")

PREFERRED_LANGUAGES: Final[tuple[str, ...]] = ("EN", "HI", "MR", "TA", "KN", "TE", "BN", "GU")

# --- Fraud modus operandi ---------------------------------------------------
#: The five injected fraud patterns. The labels live in a holdout table that
#: agent code must never read, so precision/recall can be measured offline.
FRAUD_METHODS: Final[tuple[str, ...]] = (
    "CARD_NOT_PRESENT_BURST",
    "GEO_JUMP",
    "MICRO_THEN_MACRO",
    "MERCHANT_COLLUSION",
    "ACCOUNT_TAKEOVER",
)

# --- Deliberate data quality events ----------------------------------------
#: Month index (1-based, from the start of the generated window) at which an
#: additive schema change appears in the CRM extract.
SCHEMA_DRIFT_MONTH: Final[int] = 7
#: Month index at which the core banking extract starts arriving damaged.
DQ_INCIDENT_MONTH: Final[int] = 9
#: Fraction of source-account rows with a conflicting region between CRM and core.
REGION_CONFLICT_RATE: Final[float] = 0.04

__all__ = [name for name in dir() if name.isupper()]
