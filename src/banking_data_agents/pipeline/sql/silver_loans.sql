-- SILVER: conformed loan records, one row per loan per monthly extract.
--
-- Derives the debt-service vocabulary (LTV, coverage, DPD band) once, here, so
-- every consumer of a loan figure uses the same definitions.

WITH loans AS (
    SELECT * FROM bronze.s_loans_loans
)

SELECT
    loan_id,
    customer_id,
    product,
    CAST(principal AS DOUBLE)              AS principal,
    CAST(interest_rate AS DOUBLE)          AS interest_rate,
    CAST(tenor_months AS INTEGER)          AS tenor_months,
    CAST(emi_amount AS DOUBLE)             AS emi_amount,
    CAST(disbursed_date AS DATE)           AS disbursed_date,
    CAST(outstanding_principal AS DOUBLE)  AS outstanding_principal,
    CAST(outstanding_principal AS DOUBLE) / NULLIF(CAST(principal AS DOUBLE), 0) AS outstanding_ratio,
    CAST(dpd AS INTEGER)                   AS dpd,
    CASE
        WHEN CAST(dpd AS INTEGER) = 0    THEN 'CURRENT'
        WHEN CAST(dpd AS INTEGER) <= 30  THEN 'DPD_1_30'
        WHEN CAST(dpd AS INTEGER) <= 60  THEN 'DPD_31_60'
        WHEN CAST(dpd AS INTEGER) <= 90  THEN 'DPD_61_90'
        ELSE 'DPD_90_PLUS'
    END                                    AS dpd_band,
    CASE WHEN CAST(dpd AS INTEGER) >= 90 THEN TRUE ELSE FALSE END AS npa_flag,
    status,
    collateral_type,
    CAST(collateral_value AS DOUBLE)       AS collateral_value,
    -- Collateral coverage: how many times the outstanding is covered.
    CAST(collateral_value AS DOUBLE) / NULLIF(CAST(outstanding_principal AS DOUBLE), 0) AS collateral_coverage_ratio,
    _batch_id                              AS snapshot_batch,
    TIMESTAMP '{{as_of_ts}}'               AS _silver_updated_at
FROM loans
