-- Ticket Stampede schema.
--
-- Design note that drives everything else: tickets are PRE-SEEDED, one row per
-- ticket that exists. "Never sell more tickets than exist" is therefore a
-- cardinality property of the table, not a rule the application checks. There
-- is no `sold < capacity` guard anywhere in the hot path, because a guard is a
-- read-then-write and a read-then-write is a race. You cannot claim a 101st row
-- when only 100 were created.
--
-- The cost of this choice is that /reset with a large ticket count does O(n)
-- inserts. That is a one-off setup cost, not a per-request cost, so it is the
-- right side of the trade.

CREATE TABLE IF NOT EXISTS sale (
    id         int         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    epoch      bigint      NOT NULL,
    capacity   int         NOT NULL CHECK (capacity >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tickets (
    epoch      bigint      NOT NULL,
    ticket_no  int         NOT NULL,
    user_id    text,
    request_id text,
    sold_at    timestamptz,
    PRIMARY KEY (epoch, ticket_no)
);

-- Idempotency. A partial unique index: unsold rows have request_id NULL and are
-- exempt, sold rows must be unique per (epoch, request_id). This is what makes
-- a duplicate request id physically unable to consume two tickets -- the second
-- claim aborts on the index, releasing the row it had locked.
CREATE UNIQUE INDEX IF NOT EXISTS tickets_request_uniq
    ON tickets (epoch, request_id)
    WHERE request_id IS NOT NULL;

-- The claim query scans for the first unsold row. Without this it degrades to a
-- seq scan over every already-sold ticket, which is the difference between flat
-- and quadratic as the sale fills up.
CREATE INDEX IF NOT EXISTS tickets_unsold
    ON tickets (epoch, ticket_no)
    WHERE user_id IS NULL;

-- Used only by the naive-seq store. A sequence hands out unique numbers without
-- blocking, which is exactly why that variant oversells: every buyer gets a
-- distinct ticket number, so nothing collides and nothing raises -- the only
-- thing standing between the sale and unbounded oversell is a count() read that
-- is stale the moment it returns.
CREATE SEQUENCE IF NOT EXISTS ticket_no_seq;
