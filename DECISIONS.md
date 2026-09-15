# Decisions

## Architecture, and what I rejected

**Python / FastAPI / asyncpg / PostgreSQL.** What matters is not the language:
**every correctness decision is one SQL statement**, none in application code.
That decides the multi-instance case, the crash case, and the codebase's shape.

**Tickets are pre-seeded, one row each**, so "never sell more than exist" is a
cardinality property of the table rather than a rule anyone checks. No
`sold < capacity` guard exists in the hot path, because a guard is a
read-then-write and a read-then-write is a race. You cannot claim a 101st row
when 100 exist. Cost: `/reset` is O(n), paid at setup, not per request.

**Claiming is `UPDATE … WHERE ctid = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`.**
Buyers step over each other's in-flight rows instead of queueing behind them, so
no hot counter row serialises the sale. Idempotency is a partial unique index on
`(epoch, request_id)`; the preceding read is a fast path only and removing it
would not affect correctness. `epoch` lets `/reset` orphan the old sale rather
than delete rows under a live query.

| Rejected | Why |
|---|---|
| In-process `asyncio.Lock` | Correct on one instance, worthless on three. Multi-instance becomes a rewrite, not a deploy change. |
| Counter row + `SET sold = sold + 1` | Correct, but serialises every buyer through one row lock. `SKIP LOCKED` drops the convoy. |
| `SERIALIZABLE` | Moves the problem to retry storms exactly when load peaks. |
| Redis | Default persistence would *lose confirmed sales* on the kill — the thing I most wanted to test. |

## The thing I was not asked for

The brief names four invariants and says to verify them "by reading `/status`".
**My naive seller passes all four** — 100 sold against capacity 100, no
duplicate numbers, no request id holding two, count matching the list.

It had told **1,211 buyers they held a ticket.** 100 exist. 1,083 confirmed
sales were missing from `/status`, and every one of the 100 ticket numbers went
to more than one buyer over the wire, while `/status` showed zero duplicates
(`runs/02-naive-silently-wrong/`).

All four are properties of `/status`, and `/status` was self-consistent. The
failure lives between what buyers were *told* and what the server *kept*, where
nothing reading only `/status` can see it. So the buyer journals every attempt
and reconciles:

- **I5 — no phantom tickets.** Held by a request id never sent.
- **I6 — no confirmed sale lost or reassigned.** Server said "ticket 47 is
  yours"; `/status` disagrees.
- **I7 — buyers left in doubt** (advisory). Every attempt ended unknown; some
  hold a ticket they do not know about.

I6 is what makes the crash test mean anything: without it, "we survived" reduces
to "we did not oversell", which a service that dropped every sale also passes.
`tests/test_invariants.py` feeds the checker journals violating one invariant
each and requires it to fail that one only — several would pass a checker that
always returned green, which is their point.

## Where the bottleneck is, and how I know

Each layer bounds the next (`scripts/ceiling.py`): `/healthz` 196 req/s, `/buy`
154, **asyncpg direct 3850**. Postgres has ~25x headroom and never was the
bottleneck.

Splitting the same load across K client processes gave 186 → 320 → 465 → 746
req/s for K = 1, 2, 4, 8, flat through K = 24. **3.6x — my early measurements
were of my own client**, and every throughput number before that was wrong. The
seller's ceiling is ~800 req/s, CPU-bound at 96.9% median of *one* core of eight.

So the multi-instance exercise and the bottleneck fix are one change. Three
instances sharing nothing but Postgres — no leader, no lock, no sticky routing,
replays free to land elsewhere than their original — held all seven invariants,
spread 2748/2884/2776. Client-side round-robin, not a proxy: a single-process
Python proxy caps near 800 req/s, *below* the three instances combined.

| concurrency | req/s | p99 ms | pool-wait p99 | db p99 |
|---|---|---|---|---|
| 32 | 337.7 | 129 | **0.5** | 67.6 |
| 64 | **422.0** | 210 | **99.7** | 81.1 |
| 128 | 353.7 | 635 | **426.3** | 80.2 |
| 256 | 168.7 | 4921 | 3351.2 | 540.9 |
| 512 | 306.7 | 4844 | **4413.7** | 131.3 |

**The knee is 64.** Past it, pool-wait goes 0.5ms → 4.4s while db time stays
flat at 70–130ms: extra concurrency buys queue depth, not work. That split is
why `/metrics` times the two halves apart. The 256 row is out of line on both
counts; I call it noise rather than invent a story I cannot evidence.

**A measurement that was noise.** A header added via `@app.middleware("http")`
appeared to halve throughput, 774 → 386. I nearly wrote that down. Three runs of
the *identical* experiment gave 774, 386, 243 — the spread was the machine.
Measured properly (`scripts/ab.py`, alternating arms, warm-up discarded): none
636 req/s (597–735), raw ASGI 650 (619–815), `BaseHTTPMiddleware` 527 (510–540).
Raw ASGI is free; `BaseHTTPMiddleware` costs **17%, not 50%** — credible only
because its range does not overlap the baseline's. **No single-run number here
is load-bearing.**

## Two datastore failures, which fail oppositely

**Dead** — `pg_ctl -m immediate`, so recovery replays the WAL; 40k requests
against 20k tickets. All seven held, **zero confirmed sales lost**, which
follows from committing before acknowledging with `fsync` and
`synchronous_commit` on. The cost is I7: **3,482 buyers ended with no definite
answer, 13 holding a ticket they do not know about** — unavoidable when a
process dies between commit and response, and invisible to all four stated
invariants. Unplanned: **a 10s outage cost ~40s of degraded service**, dominated
by re-establishing 32 pooled connections, not by Postgres, which accepted
connections long before throughput returned.

**Slow** — `scripts/dbproxy.py`, +120ms per chunk for 10s. Killing a process
cannot produce this case, which is why the proxy exists.

| | dead | slow |
|---|---|---|
| UNKNOWN outcomes | 4,475 | **0** |
| buyers left in doubt | 3,482 (13 holding) | **0** |
| pool-wait p99 | 2,910ms | **6,435ms** |
| sales during the event | **stopped entirely** | continued, degraded |

Dead refuses connections instantly, so the seller sheds load without deciding
to. Slow means every query still succeeds, late: the pool fills with *busy*
rather than broken connections and the failure presents as **6.4s of pure
queueing at a zero error rate**. All seven held and no buyer was left in doubt —
but any health check watching errors would have called the service perfectly
healthy throughout. That is the harder one to detect.

## Where it breaks

- **`SKIP LOCKED` is pessimistic about sold-out.** Rows locked by in-flight
  transactions are invisible, so a buyer can be told sold-out while a concurrent
  transaction aborts and releases a ticket. A lost sale, never an oversell — a
  deliberate trade that under-sells slightly near the last few tickets.
- **Recovery is 4x the outage.** No pool warm-up or circuit breaker.
- **`/status` is O(n) and unpaginated.** At 20k tickets it serialises every row.
- **One Postgres.** Durable against a crash, not against losing the disk.
- **All numbers come from one Windows laptop**, ~1.2x run-to-run spread even
  after controlling for it — internally comparable, not absolute.
- **Two Windows harness bugs:** `CTRL_BREAK_EVENT` killed the whole console
  process group including the test driver; `psql` as a subprocess hung twice,
  once mid-experiment. I fixed the second by removing the dependency rather than
  diagnosing it. That is a shortcut and I still do not know the cause.

## With two more weeks

1. **Close the in-doubt window.** Those 13 buyers are the most interesting
   failure here. `GET /buy/{request_id}` would let a client that lost its answer
   discover whether it owns a ticket — cheap, and it closes the only correctness
   gap I could not.
2. **Alert on saturation, not errors.** The slow-datastore run looks healthy by
   every error-rate signal the service exposes.
3. **Make recovery proportional to the outage** — pool health checks and
   bounded reconnect.
4. **Waitlist with 30s reservation expiry** — the state machine I chose not to
   attempt rather than attempt badly.
