-- GOLD: transaction — the conformed, cross-rail transaction product.
--
-- Confirmed-fraud labelling is deliberately *time-gated*. A dispute takes about
-- 90 days to resolve, so:
--
--   fraud_flag = NULL   the transaction is still inside the dispute window and
--                       the bank does not yet know whether it was fraud
--   fraud_flag = TRUE   confirmed fraud
--   fraud_flag = FALSE  the window closed with no fraud confirmed
--
-- This is what makes offline precision/recall measurement honest: the fraud
-- agent triages recent transactions, whose labels are not present in this table
-- at all (they live only in ops.fraud_ground_truth, which agent tools must never
-- read). Without the gate, the agent would be reading the answers.

WITH confirmed AS (
    SELECT
        transaction_id,
        fraud_method,
        CAST(label_available_date AS DATE) AS label_available_date
    FROM ops.fraud_ground_truth
)

SELECT
    t.transaction_id,
    t.customer_id,
    t.account_id,
    t.transaction_ts,
    t.transaction_date,
    t.transaction_month,
    t.transaction_hour,
    t.amount,
    t.currency,
    t.debit_credit,
    t.signed_amount,
    t.channel,
    t.channel_group,
    t.merchant_id,
    t.merchant_category,
    t.merchant_country,
    t.is_international,
    t.card_id,
    t.device_id,
    CASE
        WHEN c.transaction_id IS NOT NULL THEN TRUE
        WHEN t.transaction_date <= DATE '{{as_of_90}}' THEN FALSE
        ELSE NULL
    END                                        AS fraud_flag,
    c.fraud_method                             AS confirmed_fraud_method,
    CASE
        WHEN c.transaction_id IS NOT NULL THEN 'CONFIRMED'
        WHEN t.transaction_date <= DATE '{{as_of_90}}' THEN 'CLEARED'
        ELSE 'PENDING'
    END                                        AS fraud_label_status,
    t.source_system,
    TIMESTAMP '{{as_of_ts}}'                   AS _gold_updated_at
FROM silver.transactions t
LEFT JOIN confirmed c
       ON c.transaction_id = t.transaction_id
      AND c.label_available_date <= DATE '{{as_of}}'
