-- SILVER: conformed account records, one row per account per monthly batch.
--
-- History is preserved (rather than "latest wins") so the data quality incident
-- in month 9 remains visible and explainable long after it was remediated.
--
-- Two documented survivorship rules live here:
--
-- 1. REGION. The CRM owns the customer's *resident* region; core banking owns the
--    *branch* region. About 4% legitimately differ because a customer banks at a
--    branch in another state. Both are retained and the disagreement is flagged
--    rather than silently resolved — the Copilot must be able to explain it.
--
-- 2. NEGATIVE BALANCES. The month 9 incident produced impossible negative
--    balances on deposit accounts. The raw value is preserved in balance_raw,
--    the conformed balance is nulled, and an anomaly flag is raised.

WITH customer_region AS (
    SELECT
        customer_id,
        region,
        ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY _batch_id DESC) AS rn
    FROM bronze.s_crm_customers
),

accounts AS (
    SELECT * FROM bronze.s_core_accounts
)

SELECT
    a.account_id,
    a.customer_id,
    a.account_type,
    a.branch_id,
    COALESCE(a.branch_region, c.region) AS branch_region,
    a.branch_region                      AS branch_region_raw,
    CASE WHEN a.branch_region IS NULL THEN TRUE ELSE FALSE END AS branch_region_missing_flag,
    c.region                             AS customer_region,
    CASE
        WHEN a.branch_region IS NOT NULL AND a.branch_region IS DISTINCT FROM c.region THEN TRUE
        ELSE FALSE
    END                                  AS region_conflict_flag,
    a.currency,
    a.open_date,
    a.status,
    CAST(a.balance AS DOUBLE)            AS balance_raw,
    CASE
        WHEN a.account_type IN ('SAVINGS', 'CURRENT', 'SALARY') AND CAST(a.balance AS DOUBLE) < 0 THEN NULL
        ELSE CAST(a.balance AS DOUBLE)
    END                                  AS balance,
    CASE
        WHEN a.account_type IN ('SAVINGS', 'CURRENT', 'SALARY') AND CAST(a.balance AS DOUBLE) < 0 THEN TRUE
        ELSE FALSE
    END                                  AS balance_anomaly_flag,
    CAST(a.overdraft_limit AS DOUBLE)    AS overdraft_limit,
    CAST(a.interest_rate AS DOUBLE)      AS interest_rate,
    a._batch_id                          AS snapshot_batch,
    TIMESTAMP '{{as_of_ts}}'             AS _silver_updated_at
FROM accounts a
LEFT JOIN customer_region c
       ON c.customer_id = a.customer_id
      AND c.rn = 1
