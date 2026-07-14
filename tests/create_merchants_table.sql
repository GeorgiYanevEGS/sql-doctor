-- Creates a small merchants lookup table matching the merchant values
-- already present in source.transactions (Kaufland, Lidl, Billa, OMV,
-- Shell, METRO, Amazon, eBay, Booking.com, Fantastico).
--
-- Deliberately NO index on merchant_code (only an unrelated surrogate
-- id as PK) — this is what forces the planner into a Nested Loop with a
-- repeated Seq Scan on the inner side, which is exactly the pattern
-- repeated_seq_scan_in_loop is designed to catch.

CREATE TABLE IF NOT EXISTS source.merchants (
    id             SERIAL PRIMARY KEY,
    merchant_code  VARCHAR(100),   -- intentionally NOT indexed/unique yet
    merchant_name  VARCHAR(200),
    category       VARCHAR(100)
);

TRUNCATE source.merchants;

INSERT INTO source.merchants (merchant_code, merchant_name, category) VALUES
    ('Kaufland',      'Kaufland Bulgaria EOOD',        'Retail'),
    ('Lidl',          'Lidl Bulgaria EOOD',            'Retail'),
    ('Billa',         'Billa Bulgaria EOOD',           'Retail'),
    ('Fantastico',    'Fantastico Retail JSC',         'Retail'),
    ('OMV',           'OMV Bulgaria OOD',              'Fuel'),
    ('Shell',         'Shell Bulgaria EAD',            'Fuel'),
    ('METRO',         'METRO Cash & Carry Bulgaria',   'Wholesale'),
    ('Amazon',        'Amazon EU S.a.r.l.',            'E-commerce'),
    ('eBay',          'eBay Inc.',                     'E-commerce'),
    ('Booking.com',   'Booking.com B.V.',              'Travel');

ANALYZE source.merchants;

-- Sanity check
SELECT COUNT(*) AS merchant_count FROM source.merchants;
SELECT * FROM source.merchants;

-- Confirm no index exists on merchant_code (only the PK on id)
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE schemaname = 'source' AND tablename = 'merchants';
