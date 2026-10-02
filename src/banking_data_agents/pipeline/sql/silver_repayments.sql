-- SILVER: conformed loan repayment records (append-only fact).

WITH repayments AS (
    SELECT * FROM bronze.s_loans_repayments
)

SELECT
    repayment_id,
    loan_id,
    customer_id,
    CAST(due_date AS DATE)                                  AS due_date,
    CAST(paid_date AS DATE)                                 AS paid_date,
    CAST(amount_due AS DOUBLE)                              AS amount_due,
    CAST(amount_paid AS DOUBLE)                             AS amount_paid,
    CAST(amount_due AS DOUBLE) - CAST(amount_paid AS DOUBLE) AS amount_shortfall,
    CASE
        WHEN CAST(amount_paid AS DOUBLE) >= CAST(amount_due AS DOUBLE) THEN TRUE
        ELSE FALSE
    END                                                     AS paid_in_full,
    CAST(dpd_at_payment AS INTEGER)                         AS dpd_at_payment,
    CASE
        WHEN CAST(dpd_at_payment AS INTEGER) = 0 THEN 'ON_TIME'
        WHEN CAST(dpd_at_payment AS INTEGER) <= 30 THEN 'LATE_1_30'
        ELSE 'LATE_30_PLUS'
    END                                                     AS punctuality_band,
    _batch_id                                               AS snapshot_batch,
    TIMESTAMP '{{as_of_ts}}'                                AS _silver_updated_at
FROM repayments
