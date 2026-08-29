-- Receiving reloads must use immutable reconciliation evidence, never repeat
-- a nullable/duplicate ISBN join independently for every order row.
CREATE TABLE delivery_row_order_allocations (
    delivery_row_id TEXT NOT NULL REFERENCES delivery_rows(id) ON DELETE RESTRICT,
    order_row_id TEXT NOT NULL REFERENCES order_rows(id) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    match_basis TEXT NOT NULL CHECK (
        match_basis IN ('ISBN', 'ISBN_TITLE', 'TITLE', 'TITLE_AUTHOR', 'TITLE_EDITION')
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (delivery_row_id, order_row_id)
);

CREATE INDEX idx_delivery_allocations_order_row
    ON delivery_row_order_allocations(order_row_id, delivery_row_id);

CREATE TRIGGER delivery_row_order_allocation_scope_insert
BEFORE INSERT ON delivery_row_order_allocations
FOR EACH ROW WHEN
    NOT EXISTS (
        SELECT 1
        FROM delivery_rows AS delivered
        JOIN delivery_batches AS batch ON batch.id = delivered.delivery_batch_id
        JOIN order_rows AS ordered
          ON ordered.id = NEW.order_row_id
         AND ordered.order_revision_id = batch.order_revision_id
        WHERE delivered.id = NEW.delivery_row_id
    )
    OR NEW.quantity > (
        SELECT delivered.quantity FROM delivery_rows AS delivered
        WHERE delivered.id = NEW.delivery_row_id
    )
    OR NEW.quantity + COALESCE((
        SELECT SUM(existing.quantity)
        FROM delivery_row_order_allocations AS existing
        WHERE existing.delivery_row_id = NEW.delivery_row_id
    ), 0) > (
        SELECT delivered.quantity FROM delivery_rows AS delivered
        WHERE delivered.id = NEW.delivery_row_id
    )
BEGIN SELECT RAISE(ABORT, 'delivery row allocation scope mismatch'); END;

CREATE TRIGGER immutable_delivery_row_order_allocation_update
BEFORE UPDATE ON delivery_row_order_allocations
BEGIN SELECT RAISE(ABORT, 'delivery row allocation is immutable'); END;

CREATE TRIGGER immutable_delivery_row_order_allocation_delete
BEFORE DELETE ON delivery_row_order_allocations
BEGIN SELECT RAISE(ABORT, 'delivery row allocation is immutable'); END;

-- Backfill only historical rows with one unambiguous bibliographic target.
-- Ambiguous rows intentionally remain unlinked and therefore cannot inflate
-- reload totals.
INSERT INTO delivery_row_order_allocations (
    delivery_row_id, order_row_id, quantity, match_basis, created_at
)
SELECT delivered.id,
       ordered.id,
       delivered.quantity,
       CASE
           WHEN delivered.isbn13 IS NOT NULL AND ordered.isbn13 = delivered.isbn13
                AND suseoro_normalize_key(ordered.title) =
                    suseoro_normalize_key(delivered.title)
             THEN 'ISBN_TITLE'
           WHEN delivered.isbn13 IS NOT NULL AND ordered.isbn13 = delivered.isbn13
             THEN 'ISBN'
           WHEN suseoro_normalize_key(ordered.author) =
                suseoro_normalize_key(delivered.author)
             THEN 'TITLE_AUTHOR'
           WHEN suseoro_normalize_key(ordered.edition) =
                suseoro_normalize_key(delivered.edition)
             THEN 'TITLE_EDITION'
           ELSE 'TITLE'
       END,
       delivered.created_at
FROM delivery_rows AS delivered
JOIN delivery_batches AS batch ON batch.id = delivered.delivery_batch_id
JOIN order_rows AS ordered ON ordered.order_revision_id = batch.order_revision_id
WHERE batch.sealed_at IS NOT NULL
  AND (
        (
            delivered.isbn13 IS NOT NULL
            AND ordered.isbn13 = delivered.isbn13
            AND (
                1 = (
                    SELECT COUNT(*) FROM order_rows AS same_isbn
                    WHERE same_isbn.order_revision_id = batch.order_revision_id
                      AND same_isbn.isbn13 = delivered.isbn13
                )
                OR suseoro_normalize_key(ordered.title) =
                   suseoro_normalize_key(delivered.title)
            )
        )
        OR (
            (delivered.isbn13 IS NULL OR NOT EXISTS (
                SELECT 1 FROM order_rows AS same_isbn
                WHERE same_isbn.order_revision_id = batch.order_revision_id
                  AND same_isbn.isbn13 = delivered.isbn13
            ))
            AND suseoro_normalize_key(ordered.title) =
                suseoro_normalize_key(delivered.title)
        )
      )
GROUP BY delivered.id
HAVING COUNT(ordered.id) = 1;

-- Rebuild the active delivery comparison set from the newly durable links.
-- A prior disposition survives only when kind, order row, and canonical JSON
-- facts are identical. Historical per-delivery references are rebound to the
-- allocation-specific reference before that fingerprint comparison.
CREATE TEMP TABLE task9_desired_delivery_differences (
    school_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    order_revision_id TEXT NOT NULL,
    order_row_id TEXT,
    kind TEXT NOT NULL,
    reference_key TEXT NOT NULL,
    legacy_reference_key TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO task9_desired_delivery_differences
SELECT batch.school_id,
       batch.workspace_id,
       batch.order_revision_id,
       NULL,
       'UNORDERED',
       'delivery:unordered:' || delivered.id,
       'delivery:unordered:' || delivered.id,
       json_object('isbn13', delivered.isbn13, 'title', delivered.title),
       delivered.created_at
FROM delivery_rows AS delivered
JOIN delivery_batches AS batch ON batch.id = delivered.delivery_batch_id
LEFT JOIN delivery_row_order_allocations AS allocation
  ON allocation.delivery_row_id = delivered.id
WHERE batch.sealed_at IS NOT NULL
  AND allocation.delivery_row_id IS NULL;

INSERT INTO task9_desired_delivery_differences
SELECT batch.school_id,
       batch.workspace_id,
       batch.order_revision_id,
       ordered.id,
       'ISBN',
       'delivery:isbn:' || delivered.id || ':' || ordered.id,
       'delivery:isbn:' || delivered.id,
       json_object('expected', ordered.isbn13, 'received', delivered.isbn13),
       delivered.created_at
FROM delivery_row_order_allocations AS allocation
JOIN delivery_rows AS delivered ON delivered.id = allocation.delivery_row_id
JOIN delivery_batches AS batch ON batch.id = delivered.delivery_batch_id
JOIN order_rows AS ordered ON ordered.id = allocation.order_row_id
WHERE batch.sealed_at IS NOT NULL
  AND delivered.isbn13 IS NOT ordered.isbn13;

INSERT INTO task9_desired_delivery_differences
SELECT batch.school_id,
       batch.workspace_id,
       batch.order_revision_id,
       ordered.id,
       'UNIT_PRICE',
       'delivery:price:' || delivered.id || ':' || ordered.id,
       'delivery:price:' || delivered.id,
       json_object('expected', ordered.unit_price, 'received', delivered.unit_price),
       delivered.created_at
FROM delivery_row_order_allocations AS allocation
JOIN delivery_rows AS delivered ON delivered.id = allocation.delivery_row_id
JOIN delivery_batches AS batch ON batch.id = delivered.delivery_batch_id
JOIN order_rows AS ordered ON ordered.id = allocation.order_row_id
WHERE batch.sealed_at IS NOT NULL
  AND delivered.unit_price IS NOT NULL
  AND delivered.unit_price != ordered.unit_price;

INSERT INTO task9_desired_delivery_differences
SELECT batch.school_id,
       batch.workspace_id,
       batch.order_revision_id,
       ordered.id,
       'EDITION',
       'delivery:edition:' || delivered.id || ':' || ordered.id,
       'delivery:edition:' || delivered.id,
       json_object('expected', ordered.edition, 'received', delivered.edition),
       delivered.created_at
FROM delivery_row_order_allocations AS allocation
JOIN delivery_rows AS delivered ON delivered.id = allocation.delivery_row_id
JOIN delivery_batches AS batch ON batch.id = delivered.delivery_batch_id
JOIN order_rows AS ordered ON ordered.id = allocation.order_row_id
WHERE batch.sealed_at IS NOT NULL
  AND delivered.edition IS NOT NULL
  AND length(delivered.edition) > 0
  AND ordered.edition IS NOT NULL
  AND length(ordered.edition) > 0
  AND delivered.edition != ordered.edition;

WITH received AS (
    SELECT ordered.id AS order_row_id,
           ordered.order_revision_id,
           ordered.quantity AS expected,
           COALESCE(SUM(allocation.quantity), 0) AS received
    FROM order_rows AS ordered
    LEFT JOIN (
        SELECT sealed_allocation.order_row_id, sealed_allocation.quantity
        FROM delivery_row_order_allocations AS sealed_allocation
        JOIN delivery_rows AS sealed_row
          ON sealed_row.id = sealed_allocation.delivery_row_id
        JOIN delivery_batches AS sealed_batch
          ON sealed_batch.id = sealed_row.delivery_batch_id
         AND sealed_batch.sealed_at IS NOT NULL
    ) AS allocation
      ON allocation.order_row_id = ordered.id
    WHERE EXISTS (
        SELECT 1 FROM delivery_batches AS batch
        WHERE batch.order_revision_id = ordered.order_revision_id
          AND batch.sealed_at IS NOT NULL
    )
    GROUP BY ordered.id
)
INSERT INTO task9_desired_delivery_differences
SELECT revision.school_id,
       revision.workspace_id,
       revision.id,
       received.order_row_id,
       CASE
           WHEN received.received = 0 THEN 'MISSING'
           WHEN received.received < received.expected THEN 'QUANTITY'
           ELSE 'OVER'
       END,
       'delivery:' || CASE
           WHEN received.received = 0 THEN 'missing'
           WHEN received.received < received.expected THEN 'quantity'
           ELSE 'over'
       END || ':' || received.order_row_id,
       'delivery:' || CASE
           WHEN received.received = 0 THEN 'missing'
           WHEN received.received < received.expected THEN 'quantity'
           ELSE 'over'
       END || ':' || received.order_row_id,
       json_object('expected', received.expected, 'received', received.received),
       revision.created_at
FROM received
JOIN order_revisions AS revision ON revision.id = received.order_revision_id
WHERE received.received != received.expected;

UPDATE receiving_differences
SET reference_key = (
    SELECT desired.reference_key
    FROM task9_desired_delivery_differences AS desired
    WHERE desired.school_id = receiving_differences.school_id
      AND desired.workspace_id = receiving_differences.workspace_id
      AND desired.order_revision_id = receiving_differences.order_revision_id
      AND desired.order_row_id IS receiving_differences.order_row_id
      AND desired.kind = receiving_differences.kind
      AND desired.legacy_reference_key = receiving_differences.reference_key
      AND desired.details_json = receiving_differences.details_json
      AND desired.reference_key != desired.legacy_reference_key
    LIMIT 1
)
WHERE EXISTS (
    SELECT 1
    FROM task9_desired_delivery_differences AS desired
    WHERE desired.school_id = receiving_differences.school_id
      AND desired.workspace_id = receiving_differences.workspace_id
      AND desired.order_revision_id = receiving_differences.order_revision_id
      AND desired.order_row_id IS receiving_differences.order_row_id
      AND desired.kind = receiving_differences.kind
      AND desired.legacy_reference_key = receiving_differences.reference_key
      AND desired.details_json = receiving_differences.details_json
      AND desired.reference_key != desired.legacy_reference_key
);

UPDATE receiving_differences
SET active = 0,
    row_version = row_version + 1,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE active = 1 AND reference_key LIKE 'delivery:%';

UPDATE receiving_differences
SET active = 1,
    row_version = row_version + 1,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE id IN (
    SELECT (
        SELECT existing.id
        FROM receiving_differences AS existing
        WHERE existing.school_id = desired.school_id
          AND existing.workspace_id = desired.workspace_id
          AND existing.order_revision_id = desired.order_revision_id
          AND existing.order_row_id IS desired.order_row_id
          AND existing.kind = desired.kind
          AND existing.reference_key = desired.reference_key
          AND existing.details_json = desired.details_json
        ORDER BY existing.updated_at DESC, existing.created_at DESC, existing.id DESC
        LIMIT 1
    )
    FROM task9_desired_delivery_differences AS desired
);

INSERT INTO receiving_differences (
    id, school_id, workspace_id, order_revision_id, order_row_id,
    kind, reference_key, details_json, created_at, updated_at
)
SELECT lower(
           substr(hex(randomblob(16)), 1, 8) || '-' ||
           substr(hex(randomblob(16)), 1, 4) || '-4' ||
           substr(hex(randomblob(16)), 1, 3) || '-a' ||
           substr(hex(randomblob(16)), 1, 3) || '-' ||
           substr(hex(randomblob(16)), 1, 12)
       ),
       desired.school_id,
       desired.workspace_id,
       desired.order_revision_id,
       desired.order_row_id,
       desired.kind,
       desired.reference_key,
       desired.details_json,
       desired.created_at,
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM task9_desired_delivery_differences AS desired
WHERE NOT EXISTS (
    SELECT 1
    FROM receiving_differences AS existing
    WHERE existing.school_id = desired.school_id
      AND existing.workspace_id = desired.workspace_id
      AND existing.order_revision_id = desired.order_revision_id
      AND existing.order_row_id IS desired.order_row_id
      AND existing.kind = desired.kind
      AND existing.reference_key = desired.reference_key
      AND existing.details_json = desired.details_json
);

DROP TABLE task9_desired_delivery_differences;
