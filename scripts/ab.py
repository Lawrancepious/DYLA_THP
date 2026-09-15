"""Repeated A/B measurement.

Written because three consecutive runs of the same three-instance experiment
reported 774, 386 and 243 req/s. A single run on this machine is not a
measurement, and a causal claim built on one -- "the middleware halved
throughput" -- would have been noise dressed up as a finding.

What this does differently:
  * alternates A and B rather than running all of A then all of B, so drift in
    machine state (thermal, background work) hits both arms equally;
  * discards the first round as warm-up;
  * reports median and full range, not a mean, because the noise is not
    symmetric -- slow outliers are common, fast ones are impossible.

    python scripts/ab.py
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PY = sys.executable

from scripts.client_scaling import run_fanout  # noqa: E402


def start_seller(mw: str, port: int) -> subprocess.Popen:
    env = {**os.environ, "TICKETS_MODE": "safe", "TICKETS_MW": mw,
           "TICKETS_INSTANCE": f"ab-{mw}", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "seller.app:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return proc
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.25)
    raise RuntimeError(f"seller ({mw}) never came up")


def measure(mw: str, port: int, n: int, procs: int, conc: int) -> float:
    proc = start_seller(mw, port)
    try:
        r = run_fanout(f"http://127.0.0.1:{port}/healthz", procs,
                       n // procs, max(1, conc // procs))
        return r["aggregate_rps"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    arms = os.environ.get("AB_ARMS", "none,asgi,basehttp").split(",")
    rounds = int(os.environ.get("AB_ROUNDS", "5"))
    n = int(os.environ.get("AB_N", "4000"))
    procs = int(os.environ.get("AB_PROCS", "8"))
    conc = int(os.environ.get("AB_CONC", "200"))

    print(f"arms={arms}  rounds={rounds} (first discarded as warm-up)  "
          f"requests={n}  client procs={procs}\n")

    samples: dict[str, list[float]] = {a: [] for a in arms}
    for rnd in range(rounds):
        row = []
        for i, arm in enumerate(arms):
            rps = measure(arm, 8010 + i, n, procs, conc)
            if rnd > 0:
                samples[arm].append(rps)
            row.append(f"{arm}={rps:>7.1f}")
        tag = "warmup " if rnd == 0 else f"round{rnd} "
        print(f"  {tag}" + "   ".join(row))

    print(f"\n{'arm':<12}{'median':>10}{'min':>10}{'max':>10}{'spread':>10}")
    print("-" * 52)
    out = {}
    for arm in arms:
        xs = samples[arm]
        if not xs:
            continue
        med = statistics.median(xs)
        out[arm] = {"median": round(med, 1), "min": round(min(xs), 1),
                    "max": round(max(xs), 1), "samples": [round(x, 1) for x in xs]}
        print(f"{arm:<12}{med:>10.1f}{min(xs):>10.1f}{max(xs):>10.1f}"
              f"{max(xs) / min(xs):>9.2f}x")

    if "none" in out and "asgi" in out:
        base = out["none"]["median"]
        for arm in arms:
            if arm in out and arm != "none":
                d = (out[arm]["median"] - base) / base * 100
                print(f"\n  {arm} vs none: {d:+.1f}% median throughput")
    (ROOT / "runs" / "middleware-ab.json").write_text(json.dumps(out, indent=2))
    print(f"\n  -> runs/middleware-ab.json")


if __name__ == "__main__":
    main()
