-- SILVER: conformed transactions across all three rails.
--
-- The bank has three systems that all record "a transaction": core banking
-- (branch/ATM/transfers), card management (POS/e-commerce/international) and the
-- payments switch (UPI/NEFT/RTGS/IMPS). Gold needs one row per transaction, so
-- they are conformed here into a single shape with a shared vocabulary.
--
-- channel_group is the conformed vocabulary: ATM | BRANCH | TRANSFER | CARD |
-- PAYMENT. Analytics that group by raw `channel` fragment across rails; analytics
-- that group by channel_group do not.

WITH core AS (
    SELECT
        transaction_id,
        customer_id,
        account_id,
        CAST(transaction_ts AS TIMESTAMP) AS transaction_ts,
        CAST(amount AS DOUBLE)            AS amount,
        currency,
        debit_credit,
        channel,
        CASE
            WHEN channel = 'ATM' THEN 'ATM'
            WHEN channel IN ('BRANCH', 'CASH_DEPOSIT', 'CHEQUE') THEN 'BRANCH'
            ELSE 'TRANSFER'
        END                               AS channel_group,
        CAST(NULL AS VARCHAR)             AS merchant_id,
        CAST(NULL AS VARCHAR)             AS merchant_category,
        CAST(NULL AS VARCHAR)             AS merchant_country,
        FALSE                             AS is_international,
        CAST(NULL AS VARCHAR)             AS card_id,
        CAST(NULL AS VARCHAR)             AS device_id,
        narration,
        's_core'                          AS source_system,
        _batch_id
    FROM bronze.s_core_transactions
),

card AS (
    SELECT
        transaction_id,
        customer_id,
        CAST(NULL AS VARCHAR)             AS account_id,
        CAST(transaction_ts AS TIMESTAMP) AS transaction_ts,
        CAST(amount AS DOUBLE)            AS amount,
        currency,
        debit_credit,
        channel,
        'CARD'                            AS channel_group,
        merchant_id,
        merchant_category,
        merchant_country,
        CAST(is_international AS BOOLEAN) AS is_international,
        card_id,
        device_id,
        CAST(NULL AS VARCHAR)             AS narration,
        's_cards'                         AS source_system,
        _batch_id
    FROM bronze.s_cards_card_transactions
),

payments AS (
    SELECT
        transaction_id,
        customer_id,
        account_id,
        CAST(transaction_ts AS TIMESTAMP) AS transaction_ts,
        CAST(amount AS DOUBLE)            AS amount,
        currency,
        debit_credit,
        channel,
        'PAYMENT'                         AS channel_group,
        CAST(NULL AS VARCHAR)             AS merchant_id,
        CAST(NULL AS VARCHAR)             AS merchant_category,
        CAST(NULL AS VARCHAR)             AS merchant_country,
        FALSE                             AS is_international,
        CAST(NULL AS VARCHAR)             AS card_id,
        CAST(NULL AS VARCHAR)             AS device_id,
        narration,
        's_payments'                      AS source_system,
        _batch_id
    FROM bronze.s_payments_payment_transactions
),

unioned AS (
    SELECT * FROM core
    UNION ALL SELECT * FROM card
    UNION ALL SELECT * FROM payments
)

SELECT
    transaction_id,
    customer_id,
    account_id,
    transaction_ts,
    CAST(transaction_ts AS DATE)                                       AS transaction_date,
    DATE_TRUNC('month', CAST(transaction_ts AS TIMESTAMP))             AS transaction_month,
    HOUR(CAST(transaction_ts AS TIMESTAMP))                            AS transaction_hour,
    amount,
    currency,
    debit_credit,
    CASE WHEN debit_credit = 'DEBIT' THEN -amount ELSE amount END      AS signed_amount,
    channel,
    channel_group,
    merchant_id,
    merchant_category,
    merchant_country,
    COALESCE(is_international, FALSE)                                  AS is_international,
    card_id,
    device_id,
    narration,
    source_system,
    _batch_id,
    TIMESTAMP '{{as_of_ts}}'                                           AS _silver_updated_at
FROM unioned
WHERE transaction_id IS NOT NULL
