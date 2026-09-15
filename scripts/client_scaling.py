"""Is the measured throughput the seller's limit, or my own client's?

Every load number in this repo is worthless if the client is the thing that
saturates first, and you cannot tell from a single-process run -- a client
pinned at 100% of one core looks exactly like a server that will not go faster.

The test: run the same total offered load split across K independent client
processes and watch aggregate throughput.

    aggregate rises with K   ->  the client was the limit
    aggregate stays flat     ->  the server is the limit

/healthz is the target because it removes Postgres from the question entirely.
Whatever this measures is the pure HTTP ceiling of the pair.

    python scripts/client_scaling.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

WORKER = r'''
import asyncio, json, sys, time
import httpx

url, n, conc = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

async def main():
    q = asyncio.Queue()
    for i in range(n): q.put_nowait(i)
    gate = asyncio.Event()
    done = [0]
    async def w(c):
        await gate.wait()
        while True:
            try: q.get_nowait()
            except asyncio.QueueEmpty: return
            try:
                await c.get(url); done[0] += 1
            except httpx.HTTPError: pass
    lim = httpx.Limits(max_connections=conc, max_keepalive_connections=conc)
    async with httpx.AsyncClient(limits=lim, timeout=30) as c:
        ts = [asyncio.create_task(w(c)) for _ in range(conc)]
        await asyncio.sleep(0.05)
        t0 = time.perf_counter(); gate.set()
        await asyncio.gather(*ts)
        wall = time.perf_counter() - t0
    print(json.dumps({"done": done[0], "wall": wall, "rps": done[0]/wall}))

asyncio.run(main())
'''


def run_fanout(url: str, procs: int, per_proc_requests: int, per_proc_conc: int):
    """Launch K workers as near-simultaneously as possible and aggregate.

    Aggregate rps is computed as total requests over the WALL CLOCK of the
    slowest worker, not as the sum of per-worker rps. Summing per-worker rates
    would silently inflate the result whenever the workers do not overlap.
    """
    script = ROOT / "runs" / "_fanout_worker.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(WORKER)

    t0 = time.perf_counter()
    procs_list = [
        subprocess.Popen(
            [PY, str(script), url, str(per_proc_requests), str(per_proc_conc)],
            stdout=subprocess.PIPE, text=True, cwd=ROOT)
        for _ in range(procs)
    ]
    results = []
    for p in procs_list:
        out, _ = p.communicate()
        try:
            results.append(json.loads(out.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            pass
    wall = time.perf_counter() - t0
    total = sum(r["done"] for r in results)
    return {
        "procs": procs,
        "total_requests": total,
        "wall_s": round(wall, 2),
        "aggregate_rps": round(total / wall, 1),
        "sum_of_worker_rps": round(sum(r["rps"] for r in results), 1),
    }


def main() -> None:
    url = os.environ.get("SELLER_URL", "http://127.0.0.1:8000") + "/healthz"
    total_conc = int(os.environ.get("SCALE_CONC", "200"))
    total_req = int(os.environ.get("SCALE_N", "4000"))

    print(f"target {url}   total offered concurrency {total_conc}, "
          f"{total_req} requests, split across K processes\n")
    rows = []
    ks = [int(x) for x in os.environ.get("SCALE_K", "1,2,4,8").split(",")]
    for k in ks:
        r = run_fanout(url, k, total_req // k, max(1, total_conc // k))
        rows.append(r)
        print(f"  K={k:<2} aggregate {r['aggregate_rps']:>8} req/s   "
              f"(wall {r['wall_s']}s, {r['total_requests']} reqs)")

    base = rows[0]["aggregate_rps"] or 1
    best = max(r["aggregate_rps"] for r in rows)
    print(f"\n  1 process: {base} req/s     best: {best} req/s"
          f"     speedup {best / base:.2f}x")
    print("  speedup near 1.0x -> server-limited;  well above 1.0x -> "
          "the single-process client was the limit")
    (ROOT / "runs" / "client-scaling.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
