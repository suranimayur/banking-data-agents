# 03 · Synthetic data

## Why generate data at all

Most demonstration platforms ship a tidy CSV and then quietly avoid the questions
that would break it. That is the wrong demo, because the interesting part of a data
platform is what it does when the data is *wrong* — a negative balance that is
physically impossible, a customer record that arrives twice from two systems, a
loan booked after it was repaid.

So `datagen/` generates a bank-shaped dataset **with the incidents deliberately
baked in**, and the rest of the platform is built to detect and describe them.

## What it produces

`datagen/generate.py::generate_all` writes **108 datasets**, **1,220,122 rows**, in
about **2.9 seconds** at the default scale. Everything is deterministic: a seed
reproduces the exact dataset, which is what makes an evaluation score comparable
between two runs.

Verified at `SYNTHETIC_MONTHS=12`:

| Zone | Table | Rows |
|------|-------|-----:|
| silver | `customers` | 2,327 |
| silver | `accounts` | 38,400 |
| silver | `transactions` | 1,083,322 |
| silver | `cards` | 1,800 |
| silver | `loans` | 14,400 |
| silver | `credit_records` | 24,000 |
| silver | `repayments` | 14,400 |
| gold | `customer_360` | 2,000 |
| gold | `transaction` | 1,083,322 |
| gold | `credit_risk` | 2,000 |

The holdout labelled set `ops.fraud_ground_truth` carries **595 labels** and a
fingerprint of `257e53682e13abe7360b5478a6770a09`; it is the thing the fraud agent
must never see ([08](08-sql-guardrails.md)).

## The sources

Landing data is written per batch under `data/landing/`, mimicking daily and
monthly feeds rather than one flat export. `datagen/reference.py` holds the
dimension reference data (regions, states, product codes) so that the same value
is spelled the same way everywhere — with one deliberate exception described below.

| Source prefix | Feed | Cadence |
|---------------|------|---------|
| `s_crm_*` | core banking CRM | monthly snapshot |
| `s_core_*` | accounts, balances | daily |
| `s_txn_*` | card and transfer transactions | daily |
| `s_card_*` | card lifecycle | daily |
| `s_loan_*` | loans, repayments | monthly |
| `s_credit_*` | credit bureau records | monthly |

## The incidents that are on purpose

These are not bugs. Each one exists to exercise a rule.

1. **Impossible negative balances.** Some bronze balance rows are negative.
   Detected by `bronze_impossible_negative_balances`, which reports a **WARNING**
   over **123 values** at the default scale — a WARNING rather than a FAIL because
   an overdrawn account is legal and the rule cannot tell the two apart.
2. **Duplicated customers.** The CRM and the core system both produce a customer
   record for the same person with different effective dates. Silver has to pick one
   current row per customer (`silver_customer_one_current`) or every downstream
   aggregate double-counts.
3. **Schema drift.** Adding an attribute partway through the history means the
   column exists in later batches only. `pipeline/lake.py::normalise_frames` widens
   absent columns to typed NULLs at ingestion so a short history window still
   queries cleanly.
4. **Region vs state code.** Gold `customer_360.region` holds the **state name**
   (`Maharashtra`) while `state_code` holds the abbreviation (`MH`). Get this
   backwards and every regional answer is wrong in a way that still looks
   plausible, which is the worst kind of wrong.
5. **Fraud label boundary.** Disputes have a window; a transaction just outside it
   must not be labelled fraud. This is what makes the fraud agent's precision
   testable rather than anecdotal.

## Region distribution

Verified counts in gold `customer_360`:

| State | Code | Customers |
|-------|------|----------:|
| Maharashtra | MH | 372 |
| Karnataka | KA | 245 |
| Delhi | DL | 203 |
| Tamil Nadu | TN | 200 |
| Gujarat | GJ | 195 |
| Uttar Pradesh | UP | 193 |
| West Bengal | WB | 176 |
| Telangana | TS | 164 |
| Rajasthan | RJ | 129 |
| Kerala | KL | 123 |

This table is the fixture behind several evaluation cases and the planner tests;
it is the ground truth that "How many customers do we have by region?" is checked
against.

## Running it

```bash
uv run bda data generate                    # default seed and scale
uv run bda data generate --seed 99          # a different, still reproducible dataset
uv run bda data generate --scale 0.1        # a tenth of the rows, for a fast loop
```

In tests the generator is called with small counts
(`SYNTHETIC_CUSTOMERS=120`, `SYNTHETIC_MONTHS=4`) and into a `tmp_path`, so unit
tests never touch `data/`.

## Configuring it

| Setting | Default | Meaning |
|---------|--------:|---------|
| `synthetic_seed` | 20260928 | Reproducibility |
| `synthetic_customers` | 2,000 | |
| `synthetic_accounts` | 3,200 | |
| `synthetic_loans` | 1,200 | |
| `synthetic_cards` | 1,800 | |
| `synthetic_months` | 12 | History depth; drives whether incidents are reachable |
| `synthetic_fraud_rate` | 0.004 | Base fraud prevalence |

## Where this lands in production

None of this runs in production. The generator is a **development and test asset**:
it is what makes the pipeline, the contracts, the data-quality rules and the agents
testable without a production replica. In a deployed environment the same bronze
contracts are fed by real feeds; the pipeline code is identical, which is the
point.

## Invariants a change must not break

1. A given seed reproduces a byte-identical dataset.
2. `ops.fraud_ground_truth` exists and is *not* derivable from gold — otherwise the
   fraud agent is training on the answer key.
3. The incident list above stays true. If a rule exists, some data must exercise
   it; a check with no failing input is decoration.
4. Row counts stay in the same order of magnitude, or the evaluation thresholds and
   the ~3 s generation budget stop meaning anything.
