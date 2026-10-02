-- GOLD: customer_360 — one trusted row per customer.
--
-- This is the product that ends the "which number is right" argument. Segment and
-- risk definitions are fixed here rather than recomputed differently by every
-- consumer:
--
--   customer_segment  derived from total relationship balance
--                       Premium   >= 5,000,000
--                       HNI       >= 1,500,000
--                       Affluent  >=   300,000
--                       Mass      otherwise
--   risk_category     derived from the bureau score
--                       Low       >= 750
--                       Medium    650..749
--                       High      <  650
--                       Unknown   no bureau record
--
-- balance_anomaly_count and region_conflict_count are surfaced as first-class
-- columns: a consumer must be able to see that a figure was partly remediated.

WITH current_customers AS (
    SELECT * FROM silver.customers WHERE is_current = TRUE
),

latest_accounts AS (
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY snapshot_batch DESC) AS rn
        FROM silver.accounts
    ) ranked WHERE rn = 1
),

account_agg AS (
    SELECT
        customer_id,
        COUNT(*)                                                              AS account_count,
        SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END)                    AS active_account_count,
        SUM(COALESCE(balance, 0))                                             AS total_balance,
        SUM(CASE WHEN account_type = 'FIXED_DEPOSIT' THEN COALESCE(balance, 0) ELSE 0 END) AS total_deposits,
        SUM(COALESCE(overdraft_limit, 0))                                     AS total_overdraft_limit,
        SUM(CASE WHEN balance_anomaly_flag THEN 1 ELSE 0 END)                 AS balance_anomaly_count,
        SUM(CASE WHEN region_conflict_flag THEN 1 ELSE 0 END)                 AS region_conflict_count
    FROM latest_accounts
    GROUP BY customer_id
),

latest_loans AS (
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY loan_id ORDER BY snapshot_batch DESC) AS rn
        FROM silver.loans
    ) ranked WHERE rn = 1
),

loan_agg AS (
    SELECT
        customer_id,
        COUNT(*)                                                    AS loan_count,
        SUM(outstanding_principal)                                  AS loan_outstanding,
        SUM(principal)                                              AS total_loan_amount,
        MAX(dpd)                                                    AS dpd_max,
        SUM(CASE WHEN npa_flag THEN 1 ELSE 0 END)                    AS npa_count,
        SUM(CASE WHEN product = 'BUSINESS' THEN 1 ELSE 0 END)        AS business_loan_count
    FROM latest_loans
    GROUP BY customer_id
),

recent_txns AS (
    SELECT
        customer_id,
        COUNT(*)                                                      AS txn_count_90d,
        SUM(CASE WHEN channel_group = 'CARD' THEN amount ELSE 0 END)  AS card_spend_90d,
        MAX(transaction_ts)                                           AS last_transaction_at
    FROM silver.transactions
    WHERE transaction_date > DATE '{{as_of_90}}'
    GROUP BY customer_id
),

latest_credit AS (
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY snapshot_batch DESC) AS rn
        FROM silver.credit_records
    ) ranked WHERE rn = 1
)

SELECT
    c.customer_id,
    CASE WHEN COALESCE(l.business_loan_count, 0) > 0 THEN 'Business' ELSE 'Individual' END AS customer_type,
    c.age,
    c.age_group,
    c.region,
    c.city,
    c.state_code,
    c.marketing_segment,
    c.kyc_status,
    c.preferred_channel,
    COALESCE(a.account_count, 0)                AS account_count,
    COALESCE(a.active_account_count, 0)         AS active_accounts,
    -- Money is rounded to the cent at the product boundary. Besides being the
    -- only meaningful precision for currency, this removes the last source of
    -- run-to-run variation: parallel aggregation may sum in a different order,
    -- producing differences around 1e-10 that would otherwise make two identical
    -- pipeline runs compare unequal.
    ROUND(COALESCE(a.total_balance, 0), 2)          AS total_balance,
    ROUND(COALESCE(a.total_deposits, 0), 2)         AS total_deposits,
    ROUND(COALESCE(a.total_overdraft_limit, 0), 2)  AS total_overdraft_limit,
    COALESCE(l.loan_count, 0)                       AS loan_count,
    ROUND(COALESCE(l.loan_outstanding, 0), 2)       AS loan_outstanding,
    ROUND(COALESCE(l.total_loan_amount, 0), 2)      AS total_loan_amount,
    COALESCE(l.npa_count, 0)                        AS npa_count,
    ROUND(COALESCE(x.card_spend_90d, 0), 2)         AS card_spend_90d,
    COALESCE(x.txn_count_90d, 0)                    AS txn_count_90d,
    x.last_transaction_at,
    cr.credit_score,
    cr.score_band,
    CASE
        WHEN COALESCE(a.total_balance, 0) >= 5000000 THEN 'Premium'
        WHEN COALESCE(a.total_balance, 0) >= 1500000 THEN 'HNI'
        WHEN COALESCE(a.total_balance, 0) >=  300000 THEN 'Affluent'
        ELSE 'Mass'
    END                                             AS customer_segment,
    CASE
        WHEN cr.credit_score IS NULL THEN 'Unknown'
        WHEN cr.credit_score >= 750 THEN 'Low'
        WHEN cr.credit_score >= 650 THEN 'Medium'
        ELSE 'High'
    END                                         AS risk_category,
    COALESCE(a.balance_anomaly_count, 0)        AS balance_anomaly_count,
    COALESCE(a.region_conflict_count, 0)        AS region_conflict_count,
    c.valid_from_batch,
    TIMESTAMP '{{as_of_ts}}'                    AS _gold_updated_at
FROM current_customers c
LEFT JOIN account_agg     a ON a.customer_id = c.customer_id
LEFT JOIN loan_agg        l ON l.customer_id = c.customer_id
LEFT JOIN recent_txns     x ON x.customer_id = c.customer_id
LEFT JOIN latest_credit  cr ON cr.customer_id = c.customer_id
