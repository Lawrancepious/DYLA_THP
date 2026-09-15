"""HTTP surface for the ticket seller.

Run:
    python -m uvicorn seller.app:app --port 8000
    TICKETS_MODE=naive python -m uvicorn seller.app:app --port 8000

The mode is chosen at startup from TICKETS_MODE so that the naive and safe
implementations are the same service behind the same endpoints. The load client
does not know which one it is attacking, which is the only way the "my client
catches my own bug" claim means anything.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .stores import NaiveSeqStore, NaiveStore, NoSaleError, SafeStore

STORES = {"naive": NaiveStore, "naive-seq": NaiveSeqStore, "safe": SafeStore}

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/tickets"
)
MODE = os.environ.get("TICKETS_MODE", "safe")
POOL_MIN = int(os.environ.get("TICKETS_POOL_MIN", "10"))
POOL_MAX = int(os.environ.get("TICKETS_POOL_MAX", "32"))
INSTANCE = os.environ.get("TICKETS_INSTANCE", "solo")
# asgi | basehttp | none -- exists so the middleware cost can be A/B tested
# against itself rather than argued about. See scripts/ab.py.
MW = os.environ.get("TICKETS_MW", "asgi")


class PoolStats:
    """Pool-wait instrumentation.

    The point of this is the brief's "how do you know rather than guess". If
    total request latency rises but time spent waiting for a pooled connection
    stays flat, the bottleneck is Postgres. If wait time rises in step with
    latency, the bottleneck is our own pool. Measuring both separates them
    instead of leaving it to inference.
    """

    def __init__(self) -> None:
        self.wait_ns: list[int] = []
        self.db_ns: list[int] = []

    def reset(self) -> None:
        self.wait_ns.clear()
        self.db_ns.clear()

    def snapshot(self) -> dict:
        def pct(xs: list[int], q: float) -> float:
            if not xs:
                return 0.0
            s = sorted(xs)
            return s[min(len(s) - 1, int(q * len(s)))] / 1e6

        return {
            "samples": len(self.wait_ns),
            "pool_wait_ms_p50": round(pct(self.wait_ns, 0.50), 3),
            "pool_wait_ms_p99": round(pct(self.wait_ns, 0.99), 3),
            "db_ms_p50": round(pct(self.db_ns, 0.50), 3),
            "db_ms_p99": round(pct(self.db_ns, 0.99), 3),
        }


stats = PoolStats()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A bounded pool is a deliberate choice, not a default. It is the queue that
    # protects Postgres from 50k concurrent connections; without it, load that
    # should degrade gracefully instead takes the database down.
    app.state.pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=POOL_MIN,
        max_size=POOL_MAX,
        command_timeout=10,
        max_inactive_connection_lifetime=300,
    )
    async with app.state.pool.acquire() as con:
        await con.execute(SCHEMA)
    if MODE not in STORES:
        raise RuntimeError(f"TICKETS_MODE={MODE!r}; expected one of {sorted(STORES)}")
    app.state.store = STORES[MODE](app.state.pool, stats)
    yield
    await app.state.pool.close()


app = FastAPI(title="ticket-stampede", lifespan=lifespan)


class InstanceHeader:
    """Stamp every response with the instance that served it.

    Two earlier versions of this were wrong, and both are worth recording.

    First it was set on the /buy success path via the injected Response object,
    which does nothing when the handler returns its own JSONResponse. Only
    successful buys carried the tag, so the three-instance spread looked like
    129 requests instead of 9000 and evidenced nothing.

    Then it became a Starlette @app.middleware("http") function. That fixed the
    coverage, and single runs suggested it had halved throughput (774 -> 386
    req/s). That claim was wrong: three consecutive runs of the identical
    experiment gave 774, 386 and 243, so the spread was machine noise and the
    "halved" reading was me reading a causal effect into it.

    Measured properly -- alternating arms, five rounds, warm-up discarded, see
    scripts/ab.py and runs/middleware-ab.json -- the real numbers are:

        none      636 req/s median   (597-735)
        asgi      650 req/s median   (619-815)   +2.2%, within noise
        basehttp  527 req/s median   (510-540)   -17.2%

    So this ASGI version is free, and BaseHTTPMiddleware genuinely costs about
    17%. The 17% is trustworthy because its range does not overlap the
    baseline's; the +2.2% is not a speed-up, it is zero.

    It costs nothing because it does the same job at the raw ASGI level: mutate
    the response-start message in flight. No task group, no memory object
    streams, no Request or Response object constructed per call.
    """

    def __init__(self, app, instance: str):
        self.app = app
        self.header = (b"x-instance", instance.encode())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(self.header)
            await send(message)

        await self.app(scope, receive, send_wrapper)


if MW == "asgi":
    app.add_middleware(InstanceHeader, instance=INSTANCE)
elif MW == "basehttp":
    @app.middleware("http")
    async def _tag(request, call_next):        # noqa: ANN001
        response = await call_next(request)
        response.headers["x-instance"] = INSTANCE
        return response


class ResetIn(BaseModel):
    ticket_count: int = Field(ge=0, le=5_000_000)


class BuyIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)


@app.post("/reset")
async def reset(body: ResetIn):
    epoch = await app.state.store.reset(body.ticket_count)
    stats.reset()
    return {"ok": True, "capacity": body.ticket_count, "epoch": epoch, "mode": MODE}


@app.post("/buy")
async def buy(body: BuyIn):
    try:
        result = await app.state.store.buy(body.user_id, body.request_id)
    except NoSaleError:
        return JSONResponse(
            {"status": "error", "error": "no_sale", "detail": "call /reset first"},
            status_code=409,
        )
    except asyncpg.PostgresError as exc:
        # Surfaced rather than swallowed. A 503 here is the datastore being
        # unavailable; the buyer must treat it as UNKNOWN, not as a failed sale,
        # because we genuinely do not know whether the commit landed.
        return JSONResponse(
            {"status": "error", "error": type(exc).__name__, "detail": str(exc)[:200]},
            status_code=503,
        )

    if result.ticket_no is None:
        return JSONResponse(
            {"status": "sold_out", "user_id": body.user_id,
             "request_id": body.request_id},
            status_code=409,
        )
    return {
        "status": "ok",
        "ticket_no": result.ticket_no,
        "user_id": body.user_id,
        "request_id": body.request_id,
        "duplicate": result.duplicate,
    }


@app.get("/status")
async def status():
    try:
        return await app.state.store.status()
    except NoSaleError:
        return JSONResponse(
            {"status": "error", "error": "no_sale", "detail": "call /reset first"},
            status_code=409,
        )


@app.get("/metrics")
async def metrics():
    """Server-side timings. Not part of the brief -- it is how the bottleneck
    claim in DECISIONS.md is evidenced rather than asserted."""
    pool = app.state.pool
    return {
        "mode": MODE,
        "instance": INSTANCE,
        "pool_size": pool.get_size(),
        "pool_idle": pool.get_idle_size(),
        "pool_max": POOL_MAX,
        **stats.snapshot(),
    }


@app.get("/healthz")
async def healthz():
    """Liveness for the load balancer. Deliberately does NOT touch Postgres:
    during the datastore-kill experiment the process is healthy and correctly
    returning 503s, and we do not want the balancer to eject it for that."""
    return {"ok": True, "instance": INSTANCE, "mode": MODE}
