-- Generates synthetic test data into source.transaction for load testing
-- sql-doctor's skill detection. Matches the REAL schema confirmed via
-- \d source.transaction:
--   transaction_id  integer      NOT NULL  (PK, serial)
--   card_id         integer
--   amount          numeric
--   currency        varchar
--   txn_date        timestamp without time zone
--   txn_type        varchar
--   merchant        varchar
--   status          varchar

DO $$
DECLARE
    target_rows INT := 200000;
BEGIN
    INSERT INTO source.transaction (
        card_id,
        amount,
        currency,
        txn_date,
        txn_type,
        merchant,
        status
    )
    SELECT
        (random() * 50000)::int,                          -- card_id: simulates ~50k distinct cards

        round((random() * 5000 - 500)::numeric, 2),        -- amount: -500..4500

        (ARRAY['BGN','EUR','USD'])[1 + floor(random()*3)::int],

        NOW() - (random() * interval '730 days'),          -- spread over ~2 years

        -- Realistic skew: 60% OPER, 25% FEE, 10% REFUND, 5% ADJUST.
        -- The skew matters for the stale_statistics skill: an uneven
        -- distribution is exactly what makes planner row-estimates go
        -- wrong if statistics targets are left at default.
        CASE
            WHEN random() < 0.60 THEN 'OPER'
            WHEN random() < 0.85 THEN 'FEE'
            WHEN random() < 0.95 THEN 'REFUND'
            ELSE 'ADJUST'
        END,

        (ARRAY['Kaufland','Lidl','Billa','Fantastico','OMV','Shell','METRO',
               'Amazon','eBay','Booking.com'])[1 + floor(random()*10)::int],

        CASE
            WHEN random() < 0.92 THEN 'COMPLETED'
            WHEN random() < 0.97 THEN 'PENDING'
            ELSE 'FAILED'
        END
    FROM generate_series(1, target_rows);

    RAISE NOTICE 'Inserted % rows into source.transaction', target_rows;
END $$;

-- Refresh planner statistics so EXPLAIN reflects the new data volume/skew
ANALYZE source.transaction;

-- Sanity checks
SELECT COUNT(*) AS total_rows FROM source.transaction;

SELECT txn_type, COUNT(*) 
FROM source.transaction 
GROUP BY txn_type 
ORDER BY 2 DESC;

-- Confirm which indexes currently exist (should be just the PK on
-- transaction_id — that's the point, so missing_index / txn_type filters
-- have something real to catch)
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE schemaname = 'source' AND tablename = 'transaction';
