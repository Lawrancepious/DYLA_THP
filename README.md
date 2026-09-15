# Ticket Stampede

A service that sells exactly N tickets to a crowd that all arrives at once, and
a load client built to break it.

The load client is the more interesting half. It catches a failure mode the
four required invariants cannot see — see [Why there are seven invariants](#why-there-are-seven-invariants).

---

## Run it in five minutes

**Needs:** Python 3.11+ and PostgreSQL 14+ binaries (`initdb`, `pg_ctl`, `psql`)
on `PATH`, or installed at `C:\Program Files\PostgreSQL\*` on Windows. Nothing
else — no Docker, no Redis, no admin rights.

```bash
git clone <this repo> && cd DYLA_THP
python -m pip install -r requirements.txt

python scripts/pgctl.py up          # private cluster on :5433  (~60s first run)
python -m pytest tests/ -q          # 14 tests, <1s

# the headline result: the same load against both implementations
python scripts/experiment.py oversell        # naive  -> FAILS invariant 1
python scripts/experiment.py safe            # safe   -> all invariants hold
```

`pgctl up` creates a project-local cluster in `.pgdata/` with `trust` auth on
loopback only. It never touches a system Postgres. To use your own database
instead, set `DATABASE_URL` and skip `pgctl` entirely:

```bash
DATABASE_URL=postgresql://user:pw@host:5432/db python scripts/experiment.py safe
```

**Tear down:** `python scripts/pgctl.py down && rm -rf .pgdata`

<details>
<summary>If <code>pgctl up</code> seems to hang on the first run</summary>

`initdb` takes 60–90s on Windows with Defender active. It is not stuck. Later
runs take about two seconds. `python scripts/pgctl.py status` reports the
cluster state.
</details>

---

## The two halves

### Part A — the seller

FastAPI + asyncpg + PostgreSQL. Three endpoints, plus two for diagnostics.

| | |
|---|---|
| `POST /reset` | `{"ticket_count": N}` — wipes state, starts a fresh sale |
| `POST /buy` | `{"user_id", "request_id"}` — a ticket number, or `sold_out` |
| `GET /status` | count sold, and which user holds which ticket number |
| `GET /metrics` | pool-wait vs database time — not required, see below |
| `GET /healthz` | liveness; deliberately does not touch Postgres |

Three implementations behind the same HTTP surface, chosen at startup by
`TICKETS_MODE`, so the load client cannot tell which one it is attacking:

- **`naive-seq`** — ticket numbers from a sequence, capacity enforced by a
  `count(*)` read. Oversells. The control condition.
- **`naive`** — the same read-then-write logic against pre-seeded rows. Does
  *not* oversell in the database, and over-promises hundreds of sales it cannot
  honour. See below.
- **`safe`** — one atomic statement per decision. `FOR UPDATE SKIP LOCKED` to
  claim a row, a partial unique index for idempotency, no application lock.

```bash
TICKETS_MODE=safe python -m uvicorn seller.app:app --port 8000
```

### Part B — the buyer

```bash
python -m buyer --requests 6000 --concurrency 400 --tickets 100 --procs 8
```

Fires concurrent buys, replays duplicate request ids, then reconciles its own
journal of every attempt against `/status` and reports pass/fail per invariant,
throughput, and p50/p95/p99. Exits non-zero on any failure, so it works as a CI
gate rather than something a human eyeballs.

`--procs` matters. One asyncio process saturates a single core at roughly 190
req/s, well below what the seller serves — so a single-process run measures the
client. See [Measuring the right thing](#measuring-the-right-thing).

---

## Why there are seven invariants

The brief asks for four, and specifies verifying them "by reading `/status`".

**The `naive` seller passes all four.** `/status` reports exactly 100 tickets
sold against a capacity of 100, no duplicate numbers in it, no request id
holding two, count matching the list. A load client written to that spec
reports a clean run.

It had told **1,211 buyers** they held a ticket. 100 exist.

```
[PASS]  I1 Never oversell              100 sold against capacity 100
[PASS]  I3 Duplicate request id buys once
[PASS]  I4 /status count matches issued tickets
[FAIL]  I2 Never issue a ticket number twice
          0 duplicated in /status; 100 handed to multiple request ids over the wire
[FAIL] *I6 No confirmed sale is lost or reassigned
          1083 confirmed sales missing from /status
```
<sub>`runs/02-naive-silently-wrong/report.txt`</sub>

Every stated invariant is a property of `/status` alone, and `/status` is
perfectly self-consistent here. The failure lives in the gap between what
buyers were told and what the server kept. So the buyer keeps its own book:

| | | |
|---|---|---|
| **I1–I4** | as specified | properties of `/status` |
| **I5** | No phantom tickets | a ticket held by a request id never sent |
| **I6** | No confirmed sale lost or reassigned | the server promised ticket 47 and `/status` disagrees |
| **I7** | Buyers left in doubt | *advisory* — every attempt ended unknown; some hold a ticket they do not know about |

### Two datastore failures fail differently

| | dead (`kill-datastore`) | slow (`slow-datastore`) |
|---|---|---|
| UNKNOWN outcomes | 4,475 | **0** |
| buyers left in doubt | 3,482 (13 holding a ticket) | **0** |
| pool-wait p99 | 2,910ms | **6,435ms** |
| sales during the event | **stopped entirely** | continued, degraded |

Both held all seven invariants. Dead refuses connections instantly, so the
seller sheds load whether it means to or not. Slow means every query still
succeeds, late — the pool fills with *busy* rather than broken connections and
the failure presents as **6.4s of queueing at a zero error rate**. A health
check watching errors would have called the service healthy throughout.
`scripts/dbproxy.py` exists because killing a process cannot produce that case.

I6 is what makes the datastore-kill run mean anything. Without it, "we survived
the outage" reduces to "we did not oversell" — which a service that dropped
every sale during the outage also passes.

`tests/test_invariants.py` feeds the checker journals that violate exactly one
invariant each and requires it to fail that one and no others. A checker that
never fires is as useless as a test suite with no assertions.

---

## Measuring the right thing

`scripts/ceiling.py` walks down the stack with the same client machinery, so
each layer bounds the one below it:

```
layer                            req/s    p50 ms
HTTP /healthz (no db)              196    257.87
HTTP /buy (full path)              154    378.61
asyncpg direct (no http)          3850     15.26
```

Postgres has ~25x headroom over the HTTP path. The datastore is not the
bottleneck. `scripts/client_scaling.py` then splits the same load across K
processes:

```
K=1   185.8 req/s     K=8   745.8 req/s
K=2   320.3 req/s     K=16  789.3 req/s
K=4   464.7 req/s     K=24  743.0 req/s
```

3.6x from K=1 to K=8, flat after. **The first 110 req/s I measured was my own
client.** The seller's real ceiling is ~800 req/s, and it is CPU-bound:
96.9% median, 100% peak of *one* core on an 8-core machine
(`runs/seller-cpu.json`). Single-threaded Python in the ASGI stack.

Which makes the three-instance deployment the fix for the bottleneck, not just
a correctness exercise — same change, both problems.

The concurrency ladder finds the knee at **64** — past that, pool-wait climbs
from 0.5ms to 4.4s while database time stays flat at 70-130ms, so extra
concurrency buys queue depth rather than work (`runs/04-ladder/ladder.txt`).

```bash
python scripts/experiment.py ladder            # latency vs concurrency
python scripts/experiment.py three-instances   # 3 instances, no app-level lock
python scripts/experiment.py kill-datastore    # kill Postgres mid-sale, recover
python scripts/experiment.py slow-datastore    # make Postgres slow for 10s
python scripts/ab.py                           # repeated A/B, 5 rounds
```

Single runs on this machine vary by 3x. `scripts/ab.py` alternates arms,
discards a warm-up round, and reports medians and ranges — after one
single-run measurement led me to a confident claim that was noise.
[DECISIONS.md](DECISIONS.md) has that story.

---

## Layout

```
seller/     app.py  stores.py  schema.sql      three implementations, one surface
buyer/      loadgen.py  invariants.py  model.py  __main__.py
scripts/    pgctl.py  experiment.py  ceiling.py  client_scaling.py  ab.py
tests/      test_invariants.py                  tests for the checker itself
runs/       recorded output of every experiment
logs/       AI session transcripts
DECISIONS.md
```

Every number quoted in this README and in DECISIONS.md is reproducible by a
named experiment in `scripts/experiment.py`, and its raw output is in `runs/`.
