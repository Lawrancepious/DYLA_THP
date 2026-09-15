"""Where does the throughput ceiling actually come from?

The measured 110 req/s could be Postgres, the connection pool, the ASGI stack,
or the load client itself. Asserting one of them would be a guess. This walks
down the stack and measures each layer with the SAME client machinery, so each
number bounds the one below it:

  1. client -> /healthz          no database at all. Bounds the HTTP path.
  2. client -> /buy              the real thing.
  3. direct asyncpg, no HTTP     bounds Postgres alone.

If (1) is close to (2), the database is not the problem and the HTTP path or
the client is. If (3) is far above (2), the seller is wasting the headroom the
database is giving it.

    python scripts/ceiling.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/tickets")


async def hammer_http(url: str, n: int, concurrency: int, post: dict | None = None):
    lat: list[float] = []
    gate = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(n):
        queue.put_nowait(i)

    async def worker(client: httpx.AsyncClient):
        await gate.wait()
        while True:
            try:
                i = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            t = time.perf_counter()
            try:
                if post is None:
                    await client.get(url)
                else:
                    await client.post(url, json={
                        "user_id": f"u{i}", "request_id": f"ceil-{time.time_ns()}-{i}"})
            except httpx.HTTPError:
                pass
            lat.append((time.perf_counter() - t) * 1000)

    limits = httpx.Limits(max_connections=concurrency,
                          max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:
        tasks = [asyncio.create_task(worker(client)) for _ in range(concurrency)]
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        gate.set()
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - t0
    return len(lat) / wall, statistics.median(lat) if lat else 0.0


async def hammer_db(n: int, concurrency: int):
    """The same claim query the safe seller runs, with no HTTP in the way."""
    pool = await asyncpg.create_pool(DB, min_size=concurrency, max_size=concurrency)
    async with pool.acquire() as con:
        epoch = await con.fetchval("SELECT epoch FROM sale WHERE id = 1")
    lat: list[float] = []
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(n):
        queue.put_nowait(i)

    async def worker():
        while True:
            try:
                i = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            t = time.perf_counter()
            async with pool.acquire() as con:
                await con.fetchval(
                    "SELECT ticket_no FROM tickets WHERE epoch = $1 AND request_id = $2",
                    epoch, f"probe-{i}")
            lat.append((time.perf_counter() - t) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    wall = time.perf_counter() - t0
    await pool.close()
    return len(lat) / wall, statistics.median(lat)


async def main() -> None:
    url = os.environ.get("SELLER_URL", "http://127.0.0.1:8000")
    conc = int(os.environ.get("CEIL_CONC", "100"))
    n = int(os.environ.get("CEIL_N", "2000"))

    print(f"concurrency={conc}  requests={n}  url={url}\n")
    rows = []

    rps, p50 = await hammer_http(f"{url}/healthz", n, conc)
    rows.append(("HTTP /healthz (no db)", rps, p50))

    await httpx.AsyncClient(timeout=60).post(
        f"{url}/reset", json={"ticket_count": n * 2})
    rps, p50 = await hammer_http(f"{url}/buy", n, conc, post={})
    rows.append(("HTTP /buy (full path)", rps, p50))

    rps, p50 = await hammer_db(n, min(conc, 60))
    rows.append(("asyncpg direct (no http)", rps, p50))

    print(f"{'layer':<28}{'req/s':>10}{'p50 ms':>10}")
    print("-" * 48)
    for name, rps, p50 in rows:
        print(f"{name:<28}{rps:>10.0f}{p50:>10.2f}")


if __name__ == "__main__":
    asyncio.run(main())
