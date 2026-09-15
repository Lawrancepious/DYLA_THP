"""The load generator.

Two things here are deliberate and worth reading.

First, the request plan is built entirely before the first request is sent.
Deciding what to send while sending it means the client's own scheduling jitter
changes the workload, so two runs are not comparable. A pre-built plan makes a
run reproducible from its seed.

Second, all workers wait on a single barrier and start together. Without that,
the ramp-up is spread over however long it takes to spawn N tasks, which is the
difference between a stampede and a trickle -- and the stampede is the entire
premise of the problem.
"""
from __future__ import annotations

import asyncio
import random
import time

import httpx

from .model import Attempt, Journal, Outcome


def build_plan(
    n_requests: int,
    n_users: int,
    duplicate_pct: float,
    seed: int,
    shard: int = 0,
    shards: int = 1,
) -> list[tuple[str, str]]:
    """Produce the (user_id, request_id) sequence for one shard.

    `duplicate_pct` of the plan is replays of an earlier request id -- the same
    id sent again, as a retrying client would. They are shuffled back in at a
    random position rather than sent immediately after the original, because
    back-to-back replay only exercises the easy case where the first has already
    committed. Interleaving them puts some replays genuinely in flight at the
    same time as their original, which is the case that breaks naive
    idempotency.

    Shards are disjoint by construction: request ids carry the shard index, so
    several client processes can attack the same sale without colliding.
    """
    rng = random.Random(seed + shard * 100_003)
    n_unique = max(1, int(round(n_requests * (1 - duplicate_pct))))

    plan: list[tuple[str, str]] = [
        (f"u{rng.randrange(n_users)}", f"s{shard}-r{i}")
        for i in range(n_unique)
    ]
    # Replays reuse both the request id and its original user id: a real retry
    # comes from the same buyer.
    originals = {rid: uid for uid, rid in plan}
    for _ in range(n_requests - n_unique):
        rid = rng.choice(list(originals))
        plan.append((originals[rid], rid))

    # Shuffle only the tail so replays land after at least one original exists,
    # but not necessarily long after it.
    head, tail = plan[: n_unique // 2], plan[n_unique // 2 :]
    rng.shuffle(tail)
    return head + tail


async def _worker(
    client: httpx.AsyncClient,
    base_urls: list[str],
    queue: asyncio.Queue,
    journal: Journal,
    start_gate: asyncio.Event,
    t_zero: float,
    worker_ix: int,
) -> None:
    await start_gate.wait()
    seq = worker_ix
    while True:
        try:
            user_id, request_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        # Round-robin across instances, offset by worker index so the workers
        # do not all hit the same instance in lockstep.
        base_url = base_urls[seq % len(base_urls)]
        seq += 1

        t0 = time.perf_counter()
        att = Attempt(
            request_id=request_id,
            user_id=user_id,
            outcome=Outcome.UNKNOWN,
            latency_ms=0.0,
            t_offset_s=round(t0 - t_zero, 4),
        )
        try:
            r = await client.post(
                f"{base_url}/buy",
                json={"user_id": user_id, "request_id": request_id},
            )
            att.http_status = r.status_code
            att.instance = r.headers.get("x-instance")
            body = r.json()
            if r.status_code == 200 and body.get("status") == "ok":
                att.outcome = Outcome.OK
                att.ticket_no = body["ticket_no"]
                att.duplicate = bool(body.get("duplicate"))
            elif body.get("status") == "sold_out":
                att.outcome = Outcome.SOLD_OUT
            elif 400 <= r.status_code < 500:
                att.outcome = Outcome.REJECTED
                att.error = str(body.get("error"))[:120]
            else:
                # 5xx. We genuinely do not know whether the write landed, so
                # this stays UNKNOWN rather than being recorded as a failure.
                att.error = str(body.get("error"))[:120]
        except (httpx.HTTPError, ValueError) as exc:
            att.error = f"{type(exc).__name__}: {exc}"[:160]
        finally:
            att.latency_ms = (time.perf_counter() - t0) * 1000
            journal.attempts.append(att)
            queue.task_done()


async def run_load(
    base_url: str | list[str],
    plan: list[tuple[str, str]],
    concurrency: int,
    timeout_s: float = 10.0,
) -> Journal:
    """`base_url` may be a list, in which case requests are round-robined
    across the instances. This is client-side load balancing rather than a
    proxy in front: a single-process Python reverse proxy tops out around
    800 req/s on this machine (scripts/client_scaling.py), which is below the
    aggregate the three instances can serve, so putting one in the path would
    measure the proxy instead of the sellers."""
    base_urls = [base_url] if isinstance(base_url, str) else list(base_url)
    queue: asyncio.Queue = asyncio.Queue()
    for item in plan:
        queue.put_nowait(item)

    journal = Journal()
    start_gate = asyncio.Event()

    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    async with httpx.AsyncClient(
        limits=limits,
        timeout=httpx.Timeout(timeout_s, connect=timeout_s),
        http2=False,
    ) as client:
        t_zero = time.perf_counter()
        workers = [
            asyncio.create_task(
                _worker(client, base_urls, queue, journal, start_gate, t_zero, i)
            )
            for i in range(concurrency)
        ]
        # Let every worker reach the gate before releasing them, so the load
        # arrives as a step function rather than a ramp.
        await asyncio.sleep(0.05)
        journal.started_at = time.perf_counter()
        start_gate.set()
        await asyncio.gather(*workers)
        journal.ended_at = time.perf_counter()

    return journal


async def fetch_status(base_url: str, timeout_s: float = 60.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.get(f"{base_url}/status")
        r.raise_for_status()
        return r.json()


async def reset_sale(base_url: str, ticket_count: int, timeout_s: float = 60.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(
            f"{base_url}/reset", json={"ticket_count": ticket_count}
        )
        r.raise_for_status()
        return r.json()


async def fetch_metrics(base_url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{base_url}/metrics")
            return r.json()
    except httpx.HTTPError:
        return {}
