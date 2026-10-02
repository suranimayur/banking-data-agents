-- GOLD: credit_risk — one row per customer as of the pipeline anchor date.
--
-- The product exposes affordability and delinquency traits plus a transparent
-- risk scorecard. Every term of risk_score is explainable to an underwriter,
-- which is a regulatory requirement, not a nicety:
--
--   score_points   GREATEST(0, 750 - credit_score) * 0.25     (0 .. 112)
--   dpd_points     LEAST(dpd_max_12m, 180)           * 0.25   (0 ..  45)
--   dti_points     GREATEST(0, dti - 0.30)           * 100    (0 ..  70)
--   npa_points     40 when any facility is 90+ days past due
--   risk_score     LEAST(100, score + dpd + dti + npa)
--
--   risk_band      LOW < 25  <= MEDIUM < 50 <= HIGH < 75 <= VERY_HIGH
--
-- dti is existing monthly obligations over monthly income. foir is the baseline
-- fixed-obligation ratio *before* any proposed new facility; the underwriting
-- agent adds the proposed EMI to it at decision time.

WITH current_customers AS (
    SELECT customer_id, region, annual_income
    FROM silver.customers
    WHERE is_current = TRUE
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
        COUNT(*)                                                          AS active_loan_count,
        SUM(principal)                                                    AS total_loan_amount,
        SUM(outstanding_principal)                                        AS total_outstanding,
        SUM(emi_amount)                                                   AS obligation_monthly,
        SUM(collateral_value)                                             AS collateral_value,
        MAX(CASE WHEN npa_flag THEN 1 ELSE 0 END)                         AS has_npa,
        MAX(CAST(dpd AS INTEGER))                                         AS dpd_current
    FROM latest_loans
    WHERE status <> 'CLOSED'
    GROUP BY customer_id
),

dpd_history AS (
    -- Delinquency across the whole stored history, not just the latest snapshot.
    SELECT customer_id, MAX(CAST(dpd AS INTEGER)) AS dpd_max_12m
    FROM silver.loans
    GROUP BY customer_id
),

latest_credit AS (
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY snapshot_batch DESC) AS rn
        FROM silver.credit_records
    ) ranked WHERE rn = 1
),

scored AS (
    SELECT
        c.customer_id,
        c.region,
        cr.credit_score,
        cr.score_band,
        ROUND(COALESCE(c.annual_income, 0) / 12.0, 2)                     AS income_monthly,
        ROUND(COALESCE(l.obligation_monthly, 0), 2)                       AS obligation_monthly,
        COALESCE(l.active_loan_count, 0)                                  AS active_loan_count,
        -- Rounded to the cent: see the note in gold_customer_360.sql on why
        -- monetary aggregates are rounded at the product boundary.
        ROUND(COALESCE(l.total_loan_amount, 0), 2)                        AS total_loan_amount,
        ROUND(COALESCE(l.total_outstanding, 0), 2)                        AS total_outstanding,
        COALESCE(l.dpd_current, 0)                                        AS dpd_current,
        COALESCE(h.dpd_max_12m, 0)                                        AS dpd_max_12m,
        CASE WHEN COALESCE(h.dpd_max_12m, 0) >= 90 THEN TRUE ELSE FALSE END AS default_history_flag,
        ROUND(COALESCE(l.collateral_value, 0), 2)                         AS collateral_value,
        cr.enquiries_6m                                                   AS bureau_enquiries_6m,
        cr.credit_vintage_months                                          AS credit_vintage_months,
        cr.delinquencies_24m                                              AS bureau_delinquencies_24m,
        COALESCE(l.has_npa, 0)                                            AS has_npa
    FROM current_customers c
    LEFT JOIN loan_agg      l ON l.customer_id = c.customer_id
    LEFT JOIN dpd_history   h ON h.customer_id = c.customer_id
    LEFT JOIN latest_credit cr ON cr.customer_id = c.customer_id
)

SELECT
    customer_id,
    DATE '{{as_of}}'                                                      AS as_of_date,
    region,
    credit_score,
    score_band,
    income_monthly,
    obligation_monthly,
    -- Debt-to-income: existing obligations over verified monthly income.
    CASE WHEN income_monthly > 0 THEN ROUND(obligation_monthly / income_monthly, 4) END AS dti,
    -- Baseline fixed-obligation ratio, before any proposed new facility.
    CASE WHEN income_monthly > 0 THEN ROUND(obligation_monthly / income_monthly, 4) END AS foir,
    active_loan_count,
    total_loan_amount,
    total_outstanding,
    dpd_current,
    dpd_max_12m,
    default_history_flag,
    collateral_value,
    CASE WHEN total_outstanding > 0 THEN ROUND(collateral_value / total_outstanding, 4) END
                                                                          AS collateral_coverage_ratio,
    bureau_enquiries_6m,
    credit_vintage_months,
    bureau_delinquencies_24m,
    ROUND(
        LEAST(100.0,
              GREATEST(0.0, 750 - COALESCE(credit_score, 700)) * 0.25
            + LEAST(COALESCE(dpd_max_12m, 0), 180) * 0.25
            + GREATEST(0.0, (CASE WHEN income_monthly > 0 THEN obligation_monthly / income_monthly ELSE 0 END) - 0.30) * 100
            + CASE WHEN has_npa = 1 THEN 40 ELSE 0 END
        ), 2)                                                             AS risk_score,
    CASE
        WHEN LEAST(100.0,
              GREATEST(0.0, 750 - COALESCE(credit_score, 700)) * 0.25
            + LEAST(COALESCE(dpd_max_12m, 0), 180) * 0.25
            + GREATEST(0.0, (CASE WHEN income_monthly > 0 THEN obligation_monthly / income_monthly ELSE 0 END) - 0.30) * 100
            + CASE WHEN has_npa = 1 THEN 40 ELSE 0 END
            ) < 25  THEN 'LOW'
        WHEN LEAST(100.0,
              GREATEST(0.0, 750 - COALESCE(credit_score, 700)) * 0.25
            + LEAST(COALESCE(dpd_max_12m, 0), 180) * 0.25
            + GREATEST(0.0, (CASE WHEN income_monthly > 0 THEN obligation_monthly / income_monthly ELSE 0 END) - 0.30) * 100
            + CASE WHEN has_npa = 1 THEN 40 ELSE 0 END
            ) < 50  THEN 'MEDIUM'
        WHEN LEAST(100.0,
              GREATEST(0.0, 750 - COALESCE(credit_score, 700)) * 0.25
            + LEAST(COALESCE(dpd_max_12m, 0), 180) * 0.25
            + GREATEST(0.0, (CASE WHEN income_monthly > 0 THEN obligation_monthly / income_monthly ELSE 0 END) - 0.30) * 100
            + CASE WHEN has_npa = 1 THEN 40 ELSE 0 END
            ) < 75  THEN 'HIGH'
        ELSE 'VERY_HIGH'
    END                                                                    AS risk_band,
    TIMESTAMP '{{as_of_ts}}'                                               AS _gold_updated_at
FROM scored
