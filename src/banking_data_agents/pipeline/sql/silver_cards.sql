-- SILVER: conformed card records, latest state per card.
--
-- Cards are slow-moving attributes, so unlike accounts this layer keeps only the
-- most recent extract per card. The natural key is the card, not (card, batch).

WITH ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY card_id ORDER BY _batch_id DESC) AS rn
    FROM bronze.s_cards_cards
)

SELECT
    card_id,
    customer_id,
    card_type,
    network,
    masked_number,
    CAST(credit_limit AS DOUBLE)                                     AS credit_limit,
    CAST(issue_date AS DATE)                                         AS issue_date,
    CAST(expiry_date AS DATE)                                        AS expiry_date,
    status,
    CASE WHEN status = 'ACTIVE' THEN TRUE ELSE FALSE END             AS is_active,
    CASE WHEN card_type = 'CREDIT' THEN TRUE ELSE FALSE END          AS is_credit,
    snapshot_batch,
    TIMESTAMP '{{as_of_ts}}'                                         AS _silver_updated_at
FROM (
    SELECT *, _batch_id AS snapshot_batch FROM ranked WHERE rn = 1
) latest
