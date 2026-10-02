-- SILVER: conformed customer dimension (SCD2) with PII tokenization.
--
-- Direct identifiers never leave this layer. Name, email, phone, address and
-- date of birth are replaced by deterministic tokens whose plaintext lives only
-- in ops.pii_vault. Age is banded so a birth date cannot be reconstructed from
-- the analytics layer.
--
-- SCD2 rather than "latest row wins": credit and fraud decisions must be
-- reproducible against the customer's attributes *at the time of the decision*.

WITH snapshot AS (
    SELECT
        customer_id,
        region,
        state_code,
        city,
        pincode,
        marketing_segment,
        kyc_status,
        preferred_channel,
        onboarded_date,
        relationship_manager_id,
        CAST(annual_income AS DOUBLE) AS annual_income,
        date_of_birth,
        -- The CRM adds preferred_language part-way through the history (deliberate
        -- additive drift). The placeholder resolves to the real column when the
        -- source has shipped it, and to a typed default when it has not — so the
        -- transform works on a 30-month history and on a 3-month CI window.
        {{crm_preferred_language}} AS preferred_language,
        _batch_id
    FROM bronze.s_crm_customers
),

with_prev AS (
    SELECT
        *,
        LAG(region)            OVER w AS p_region,
        LAG(city)              OVER w AS p_city,
        LAG(marketing_segment) OVER w AS p_segment,
        LAG(kyc_status)        OVER w AS p_kyc
    FROM snapshot
    WINDOW w AS (PARTITION BY customer_id ORDER BY _batch_id)
),

flagged AS (
    SELECT
        *,
        CASE
            WHEN p_region IS NULL                     THEN 1
            WHEN region            IS DISTINCT FROM p_region  THEN 1
            WHEN city              IS DISTINCT FROM p_city    THEN 1
            WHEN marketing_segment IS DISTINCT FROM p_segment THEN 1
            WHEN kyc_status        IS DISTINCT FROM p_kyc     THEN 1
            ELSE 0
        END AS is_new_version
    FROM with_prev
),

versioned AS (
    SELECT
        *,
        SUM(is_new_version) OVER (
            PARTITION BY customer_id ORDER BY _batch_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS version_no
    FROM flagged
),

versions AS (
    SELECT
        *,
        LEAD(_batch_id) OVER (PARTITION BY customer_id ORDER BY _batch_id) AS next_batch_id
    FROM versioned
    WHERE is_new_version = 1
)

SELECT
    customer_id,
    md5(customer_id || '|NAME')    AS name_token,
    md5(customer_id || '|EMAIL')   AS email_token,
    md5(customer_id || '|PHONE')   AS phone_token,
    md5(customer_id || '|ADDRESS') AS address_token,
    md5(customer_id || '|DOB')     AS dob_token,
    region,
    state_code,
    city,
    pincode,
    marketing_segment,
    kyc_status,
    preferred_channel,
    preferred_language,
    onboarded_date,
    relationship_manager_id,
    CAST(annual_income AS DOUBLE)       AS annual_income,
    CAST(annual_income AS DOUBLE) / 12.0 AS income_monthly,
    date_diff('year', date_of_birth, DATE '{{as_of}}') AS age,
    CASE
        WHEN date_diff('year', date_of_birth, DATE '{{as_of}}') < 26 THEN '18-25'
        WHEN date_diff('year', date_of_birth, DATE '{{as_of}}') < 36 THEN '26-35'
        WHEN date_diff('year', date_of_birth, DATE '{{as_of}}') < 46 THEN '36-45'
        WHEN date_diff('year', date_of_birth, DATE '{{as_of}}') < 56 THEN '46-55'
        WHEN date_diff('year', date_of_birth, DATE '{{as_of}}') < 66 THEN '56-65'
        ELSE '65+'
    END AS age_group,
    _batch_id                     AS valid_from_batch,
    COALESCE(next_batch_id, '9999-12') AS valid_to_batch,
    CASE WHEN next_batch_id IS NULL THEN TRUE ELSE FALSE END AS is_current,
    version_no,
    TIMESTAMP '{{as_of_ts}}'      AS _silver_updated_at
FROM versions
