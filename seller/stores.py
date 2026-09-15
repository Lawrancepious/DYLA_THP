"""The two seller implementations.

`NaiveStore` is the version written first, on purpose. It is not a strawman --
it is what a competent person writes before thinking about concurrency: read
the count, decide, then write. Both its allocation and its idempotency check
are read-then-write. The load client is supposed to break it.

`SafeStore` is the fix. The difference is not "added a lock". It is that every
decision moved into a single statement the database executes atomically.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

import asyncpg


class NoSaleError(Exception):
    """No sale has been started -- /reset has not been called yet."""


class Result:
    """Outcome of a /buy.

    `duplicate` marks an idempotent replay. The load client needs this to tell a
    correctly-deduplicated retry apart from a genuinely new sale; without it,
    "same ticket number returned twice" is ambiguous between correct idempotency
    and a duplicate-issue bug.
    """

    __slots__ = ("ticket_no", "duplicate")

    def __init__(self, ticket_no: int | None, duplicate: bool = False):
        self.ticket_no = ticket_no
        self.duplicate = duplicate


class BaseStore:
    mode = "base"
    preseed = True   # whether /reset materialises one row per ticket

    def __init__(self, pool: asyncpg.Pool, stats=None):
        self.pool = pool
        self.stats = stats

    @asynccontextmanager
    async def _conn(self):
        """Acquire a pooled connection, timing the wait separately from the work.

        The split matters. If total latency climbs while the wait stays flat,
        Postgres is the bottleneck. If the wait climbs in step with latency, our
        own pool is the bottleneck and the database is idle behind it. Timing
        them together -- which is what an earlier version of this did -- makes
        the two indistinguishable and turns the bottleneck claim into a guess.
        """
        t0 = time.perf_counter_ns()
        async with self.pool.acquire() as con:
            t1 = time.perf_counter_ns()
            try:
                yield con
            finally:
                if self.stats is not None:
                    self.stats.wait_ns.append(t1 - t0)
                    self.stats.db_ns.append(time.perf_counter_ns() - t1)

    async def reset(self, capacity: int) -> int:
        """Wipe state and start a fresh sale. Returns the new epoch.

        The epoch exists so /reset does not have to race in-flight buys from the
        previous sale. Old rows become unreachable the moment the sale row
        flips, rather than being deleted out from under a live query.
        """
        async with self.pool.acquire() as con:
            async with con.transaction():
                epoch = await con.fetchval(
                    """
                    INSERT INTO sale (id, epoch, capacity) VALUES (1, 1, $1)
                    ON CONFLICT (id) DO UPDATE
                        SET epoch = sale.epoch + 1,
                            capacity = $1,
                            created_at = now()
                    RETURNING epoch
                    """,
                    capacity,
                )
                await con.execute("DELETE FROM tickets WHERE epoch <> $1", epoch)
                await con.execute("ALTER SEQUENCE ticket_no_seq RESTART WITH 1")
                if self.preseed:
                    # COPY rather than generate_series: ~50x faster through the
                    # protocol than round-tripping inserts, and /reset is on the
                    # critical path of every experiment run.
                    await con.copy_records_to_table(
                        "tickets",
                        columns=["epoch", "ticket_no"],
                        records=((epoch, n) for n in range(1, capacity + 1)),
                    )
                return epoch

    @staticmethod
    async def _epoch(con: asyncpg.Connection) -> int:
        epoch = await con.fetchval("SELECT epoch FROM sale WHERE id = 1")
        if epoch is None:
            raise NoSaleError()
        return epoch

    async def status(self) -> dict:
        """Read the count and the holder list from ONE snapshot.

        Two separate queries could observe different moments and report a count
        that disagrees with the list -- invariant 4 failing inside the endpoint
        meant to demonstrate invariant 4. The count here is derived from the
        same rows that are returned, inside a repeatable-read transaction, so it
        cannot disagree with itself.
        """
        async with self._conn() as con:
            async with con.transaction(isolation="repeatable_read", readonly=True):
                epoch = await self._epoch(con)
                capacity = await con.fetchval("SELECT capacity FROM sale WHERE id = 1")
                rows = await con.fetch(
                    """
                    SELECT ticket_no, user_id, request_id
                    FROM tickets
                    WHERE epoch = $1 AND user_id IS NOT NULL
                    ORDER BY ticket_no
                    """,
                    epoch,
                )
        return {
            "sold": len(rows),
            "capacity": capacity,
            "epoch": epoch,
            "mode": self.mode,
            "tickets": [
                {
                    "ticket_no": r["ticket_no"],
                    "user_id": r["user_id"],
                    "request_id": r["request_id"],
                }
                for r in rows
            ],
        }

    async def buy(self, user_id: str, request_id: str) -> Result:
        raise NotImplementedError


class NaiveStore(BaseStore):
    """Deliberately broken. Kept in the repo as the control condition.

    Three races live here, and the load client finds all three:

      1. Oversell. The count is read, a decision is made, then a row is written.
         Between read and write, any number of other requests read the same
         count and reach the same decision.
      2. Duplicate ticket numbers. `sold + 1` computed from a stale read hands
         the same number to several concurrent buyers.
      3. Idempotency. SELECT-then-INSERT on request_id has the same TOCTOU
         window, so a replayed request id can buy twice.

    No artificial sleeps are inserted. The windows are the real network round
    trips between statements, which is the point: this is how the bug shows up
    in production, not how it shows up in a contrived demo.
    """

    mode = "naive"

    async def buy(self, user_id: str, request_id: str) -> Result:
        async with self._conn() as con:
            epoch = await self._epoch(con)

            # Race 3: check-then-act on idempotency.
            existing = await con.fetchval(
                "SELECT ticket_no FROM tickets WHERE epoch = $1 AND request_id = $2",
                epoch,
                request_id,
            )
            if existing is not None:
                return Result(existing, duplicate=True)

            # Race 1: check-then-act on capacity.
            sold = await con.fetchval(
                "SELECT count(*) FROM tickets WHERE epoch = $1 AND user_id IS NOT NULL",
                epoch,
            )
            capacity = await con.fetchval("SELECT capacity FROM sale WHERE id = 1")
            if sold >= capacity:
                return Result(None)

            # Race 2: the ticket number comes from a read that is already stale.
            ticket_no = sold + 1
            await con.execute(
                """
                UPDATE tickets
                SET user_id = $3, request_id = $4, sold_at = now()
                WHERE epoch = $1 AND ticket_no = $2
                """,
                epoch,
                ticket_no,
                user_id,
                request_id,
            )
            return Result(ticket_no)


class SafeStore(BaseStore):
    """The fix.

    Allocation is one statement. The inner SELECT takes a row lock with SKIP
    LOCKED, so concurrent buyers step over each other's in-flight rows instead
    of queueing behind them. There is no single hot counter row that every
    request must serialise through -- which is what keeps latency flat under
    load, and is also why three instances need no application-level lock.

    Idempotency is enforced by the partial unique index, not by the prior read.
    The read is a fast path only; correctness does not depend on it.
    """

    mode = "safe"

    async def buy(self, user_id: str, request_id: str) -> Result:
        async with self._conn() as con:
            epoch = await self._epoch(con)

            # Fast path. Purely an optimisation: avoids the cost of provoking
            # and catching a constraint violation on the common replay case.
            existing = await con.fetchval(
                "SELECT ticket_no FROM tickets WHERE epoch = $1 AND request_id = $2",
                epoch,
                request_id,
            )
            if existing is not None:
                return Result(existing, duplicate=True)

            try:
                ticket_no = await con.fetchval(
                    """
                    UPDATE tickets t
                    SET user_id = $2, request_id = $3, sold_at = now()
                    WHERE t.ctid = (
                        SELECT ctid FROM tickets
                        WHERE epoch = $1 AND user_id IS NULL
                        ORDER BY ticket_no
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING t.ticket_no
                    """,
                    epoch,
                    user_id,
                    request_id,
                )
            except asyncpg.UniqueViolationError:
                # A concurrent request with the same request_id won. Postgres
                # blocked us on the index until that transaction committed, so
                # the winner's row is visible by the time we get here. Our own
                # claim rolled back, releasing the row we had locked, so losing
                # this race leaks no ticket.
                ticket_no = await con.fetchval(
                    "SELECT ticket_no FROM tickets "
                    "WHERE epoch = $1 AND request_id = $2",
                    epoch,
                    request_id,
                )
                return Result(ticket_no, duplicate=True)

            # NULL means the inner SELECT found no unclaimed row: sold out.
            #
            # SKIP LOCKED makes this slightly pessimistic. Rows locked by
            # in-flight transactions are invisible to us, so a buyer can be told
            # sold-out while a concurrent transaction is about to abort and
            # release a ticket. That is a lost sale, never an oversell. The
            # trade is deliberate and is measured in runs/ -- see DECISIONS.md.
            return Result(ticket_no)


class NaiveSeqStore(BaseStore):
    """The other naive version, and the one that oversells literally.

    `NaiveStore` above shares SafeStore's pre-seeded table, and that turns out
    to protect it by accident: when ten buyers all compute ticket_no = 47 they
    all UPDATE the same existing row, so /status ends up with exactly one row 47
    and never exceeds capacity. It over-promises on the wire without ever
    oversells in the database.

    This variant removes that accident. Ticket numbers come from a sequence, so
    every concurrent buyer gets a distinct number and INSERTs a distinct row.
    Nothing collides, nothing raises, and the stale count() is the only thing
    holding the sale shut. It is not. /status ends up with more than capacity
    rows and invariant 1 fails outright.

    Keeping both is the point of the pair: the same category of bug shows up as
    a loud failure in one schema and an invisible one in the other, and only the
    loud one is caught by the checks the brief specifies.
    """

    mode = "naive-seq"
    preseed = False

    async def buy(self, user_id: str, request_id: str) -> Result:
        async with self._conn() as con:
            epoch = await self._epoch(con)

            existing = await con.fetchval(
                "SELECT ticket_no FROM tickets WHERE epoch = $1 AND request_id = $2",
                epoch,
                request_id,
            )
            if existing is not None:
                return Result(existing, duplicate=True)

            sold = await con.fetchval(
                "SELECT count(*) FROM tickets WHERE epoch = $1", epoch
            )
            capacity = await con.fetchval("SELECT capacity FROM sale WHERE id = 1")
            if sold >= capacity:
                return Result(None)

            ticket_no = await con.fetchval("SELECT nextval('ticket_no_seq')")
            await con.execute(
                """
                INSERT INTO tickets (epoch, ticket_no, user_id, request_id, sold_at)
                VALUES ($1, $2, $3, $4, now())
                """,
                epoch,
                int(ticket_no),
                user_id,
                request_id,
            )
            return Result(int(ticket_no))
