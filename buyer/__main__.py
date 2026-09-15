"""Load client CLI.

    python -m buyer --requests 5000 --concurrency 500 --tickets 100

Exits non-zero if any non-advisory invariant fails, so a run is usable as a
test in CI rather than something a human has to eyeball.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from .invariants import check_all, summarise
from .loadgen import build_plan, fetch_metrics, fetch_status, reset_sale, run_load
from .model import Journal, load_journals

ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="buyer", description="attack the ticket seller")
    p.add_argument("--url", default="http://127.0.0.1:8000",
                   help="seller base URL, or several comma-separated to "
                        "round-robin across instances")
    p.add_argument("--requests", type=int, default=5000)
    p.add_argument("--concurrency", type=int, default=500)
    p.add_argument("--tickets", type=int, default=100,
                   help="capacity to /reset to; 0 to skip reset")
    p.add_argument("--users", type=int, default=2000)
    p.add_argument("--duplicate-pct", type=float, default=0.20,
                   help="fraction of the plan that replays an earlier request id")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--label", default="run")
    p.add_argument("--out", default=None, help="directory for run artefacts")
    p.add_argument("--no-reset", action="store_true",
                   help="do not call /reset (for multi-process runs)")
    p.add_argument("--journal-only", action="store_true",
                   help="write the journal and skip invariant checking "
                        "(shards defer checking to the coordinator)")
    p.add_argument("--procs", type=int, default=1,
                   help="split the load across N client processes. Required for "
                        "honest numbers: one asyncio process saturates a single "
                        "core well below this seller's ceiling -- see "
                        "scripts/client_scaling.py")
    return p.parse_args(argv)


def render(label: str, summary: dict, checks, metrics: dict) -> str:
    L = [f"\n=== {label} " + "=" * max(4, 58 - len(label))]
    lm = summary["latency_ms"]
    L.append(
        f"{summary['requests']} requests in {summary['wall_s']}s   "
        f"{summary['rps']} req/s"
    )
    L.append(
        f"latency ms   p50 {lm['p50']:>8}   p95 {lm['p95']:>8}   "
        f"p99 {lm['p99']:>8}   max {lm['max']:>8}"
    )
    L.append("outcomes     " + "  ".join(
        f"{k}={v}" for k, v in sorted(summary["outcomes"].items())))
    if metrics:
        L.append(
            f"server       pool {metrics.get('pool_size')}/{metrics.get('pool_max')}  "
            f"pool-wait p99 {metrics.get('pool_wait_ms_p99')}ms  "
            f"db p99 {metrics.get('db_ms_p99')}ms"
        )
    L.append("")
    for c in checks:
        mark = "  " if c.asked_for else " *"
        L.append(f"[{c.label:<4}]{mark}{c.key} {c.title}")
        L.append(f"          {c.detail}")
    L.append("")
    L.append("  * not required by the brief -- see buyer/invariants.py")
    return "\n".join(L)


def run_distributed(args: argparse.Namespace, outdir: Path) -> Journal:
    """Fan the plan across N processes and merge their journals.

    Each shard gets a disjoint request-id namespace (see build_plan), so the
    shards cannot collide with each other -- any duplicate request id the
    invariant checks find is the seller's doing, not the harness's.

    The shards are started before any of them is waited on, so they overlap.
    Aggregate throughput is total attempts over the coordinator's wall clock,
    never the sum of per-shard rates.
    """
    procs = []
    t0 = time.perf_counter()
    for shard in range(args.procs):
        cmd = [
            sys.executable, "-m", "buyer",
            "--url", args.url,
            "--requests", str(args.requests // args.procs),
            "--concurrency", str(max(1, args.concurrency // args.procs)),
            "--users", str(args.users),
            "--duplicate-pct", str(args.duplicate_pct),
            "--seed", str(args.seed),
            "--timeout", str(args.timeout),
            "--shard", str(shard),
            "--shards", str(args.procs),
            "--out", str(outdir),
            "--label", args.label,
            "--no-reset", "--journal-only",
        ]
        procs.append(subprocess.Popen(cmd, cwd=ROOT,
                                      stdout=subprocess.DEVNULL))
    for p in procs:
        p.wait()
    wall = time.perf_counter() - t0

    journal = load_journals(sorted(outdir.glob("journal-shard*.jsonl")))
    journal.started_at = 0.0
    journal.ended_at = wall
    return journal


async def amain(args: argparse.Namespace) -> int:
    primary = args.url.split(",")[0].strip()
    if args.tickets > 0 and not args.no_reset:
        info = await reset_sale(primary, args.tickets)
        print(f"reset: capacity={info['capacity']} epoch={info['epoch']} "
              f"mode={info['mode']}")

    outdir = Path(args.out) if args.out else ROOT / "runs" / f"{args.label}-{int(time.time())}"
    outdir.mkdir(parents=True, exist_ok=True)

    if args.procs > 1:
        for stale in outdir.glob("journal-shard*.jsonl"):
            stale.unlink()   # otherwise a smaller previous run leaks into this one
        print(f"firing {args.requests} requests across {args.procs} client "
              f"processes, total concurrency {args.concurrency} "
              f"({args.duplicate_pct:.0%} replays) -> {args.url}")
        journal = run_distributed(args, outdir)
    else:
        plan = build_plan(
            args.requests, args.users, args.duplicate_pct,
            args.seed, args.shard, args.shards,
        )
        print(f"firing {len(plan)} requests at concurrency {args.concurrency} "
              f"({args.duplicate_pct:.0%} replays) -> {args.url}")
        urls = [u.strip() for u in args.url.split(",") if u.strip()]
        journal = await run_load(urls, plan, args.concurrency, args.timeout)
        journal.write(outdir / f"journal-shard{args.shard}.jsonl")

    if args.journal_only:
        print(f"journal written to {outdir}/journal-shard{args.shard}.jsonl")
        return 0

    status = await fetch_status(primary)
    metrics = await fetch_metrics(primary)
    summary = summarise(journal)
    checks = check_all(journal, status)

    report = render(args.label, summary, checks, metrics)
    print(report)

    (outdir / "status.json").write_text(json.dumps(status, indent=2))
    (outdir / "report.txt").write_text(report)
    (outdir / "summary.json").write_text(json.dumps({
        "label": args.label,
        "args": vars(args),
        "summary": summary,
        "server_metrics": metrics,
        "checks": [
            {"key": c.key, "title": c.title, "passed": c.passed,
             "detail": c.detail, "asked_for": c.asked_for,
             "advisory": c.advisory}
            for c in checks
        ],
    }, indent=2))
    print(f"artefacts -> {outdir}")

    failed = [c for c in checks if not c.advisory and not c.passed]
    return 1 if failed else 0


def main() -> None:
    args = parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
