-- SILVER: conformed bureau credit records, one row per customer per monthly extract.
--
-- The bureau reports a score and its band. The band is re-derived here from a
-- single published threshold set so that "POOR" means the same thing across every
-- downstream product, regardless of what the bureau's own labelling said.

WITH records AS (
    SELECT * FROM bronze.s_bureau_credit_records
)

SELECT
    bureau_id,
    customer_id,
    CAST(credit_score AS INTEGER)                                  AS credit_score,
    CASE
        WHEN CAST(credit_score AS INTEGER) >= 800 THEN 'EXCELLENT'
        WHEN CAST(credit_score AS INTEGER) >= 750 THEN 'GOOD'
        WHEN CAST(credit_score AS INTEGER) >= 700 THEN 'FAIR'
        WHEN CAST(credit_score AS INTEGER) >= 650 THEN 'POOR'
        ELSE 'VERY_POOR'
    END                                                            AS score_band,
    CAST(enquiries_6m AS INTEGER)                                  AS enquiries_6m,
    CAST(delinquencies_24m AS INTEGER)                             AS delinquencies_24m,
    CAST(credit_vintage_months AS INTEGER)                         AS credit_vintage_months,
    CAST(total_exposure AS DOUBLE)                                 AS total_exposure,
    CAST(active_tradelines AS INTEGER)                             AS active_tradelines,
    CAST(as_of_date AS DATE)                                       AS as_of_date,
    _batch_id                                                      AS snapshot_batch,
    TIMESTAMP '{{as_of_ts}}'                                       AS _silver_updated_at
FROM records
