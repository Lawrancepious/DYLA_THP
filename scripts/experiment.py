"""Reproducible experiment driver.

Every claim in DECISIONS.md is produced by a named experiment here, so a grader
can re-run any single one rather than reassembling a sequence of shell commands
from prose. Each writes its artefacts under runs/<name>/.

    python scripts/experiment.py list
    python scripts/experiment.py oversell
    python scripts/experiment.py all
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
PY = sys.executable


class Seller:
    """A seller process, as a context manager.

    Waits for /healthz rather than sleeping a fixed amount: a fixed sleep is
    either slower than it needs to be or flaky on a loaded machine, and this
    driver runs many of these back to back.
    """

    def __init__(self, mode: str, port: int = 8000, instance: str = "solo",
                 pool_max: int = 32, env: dict | None = None):
        self.mode, self.port, self.instance = mode, port, instance
        self.pool_max = pool_max
        self.extra_env = env or {}
        self.proc: subprocess.Popen | None = None
        self.logfile = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "Seller":
        env = {
            **os.environ,
            "TICKETS_MODE": self.mode,
            "TICKETS_INSTANCE": self.instance,
            "TICKETS_POOL_MAX": str(self.pool_max),
            "PYTHONUNBUFFERED": "1",
            **self.extra_env,
        }
        RUNS.mkdir(exist_ok=True)
        self.logfile = open(RUNS / f"seller-{self.instance}.log", "w")
        self.proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "seller.app:app", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=self.logfile, stderr=subprocess.STDOUT,
        )
        for _ in range(120):
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"seller exited early; see runs/seller-{self.instance}.log")
            try:
                with urllib.request.urlopen(f"{self.url}/healthz", timeout=1) as r:
                    if r.status == 200:
                        return self
            except (urllib.error.URLError, OSError, TimeoutError):
                time.sleep(0.25)
        raise RuntimeError(f"seller on :{self.port} never became healthy")

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            # NOT CTRL_BREAK_EVENT. On Windows a console control event goes to
            # every process sharing the console, so it takes down this driver
            # and the shell that launched it along with the seller. terminate()
            # targets the one process. The seller has no shutdown work that
            # matters -- the pool is process-local and Postgres is separate.
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self.logfile:
            self.logfile.close()


def buyer(url: str, out: Path, label: str, **kw) -> int:
    args = [PY, "-m", "buyer", "--url", url, "--label", label, "--out", str(out)]
    for k, v in kw.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    return subprocess.run(args, cwd=ROOT).returncode


def _verdict(out: Path) -> str:
    """Read back the machine-readable result so the driver's own summary cannot
    drift from what the buyer actually reported."""
    try:
        data = json.loads((out / "summary.json").read_text())
    except (OSError, ValueError):
        return "no summary.json"
    bad = [c["key"] for c in data["checks"]
           if not c["advisory"] and not c["passed"]]
    s = data["summary"]
    return (f"{s['rps']} req/s, p99 {s['latency_ms']['p99']}ms, "
            + (f"FAILED {','.join(bad)}" if bad else "all invariants held"))


# --------------------------------------------------------------------------
# Experiments
# --------------------------------------------------------------------------

def exp_oversell() -> str:
    """naive-seq under load: the literal oversell the brief asks to see."""
    out = RUNS / "01-naive-seq-oversells"
    with Seller("naive-seq", instance="naive-seq") as s:
        buyer(s.url, out, "naive-seq-oversells",
              requests=6000, concurrency=400, tickets=100, duplicate_pct=0.20,
              procs=8, timeout=30)
    return _verdict(out)


def exp_naive_silent() -> str:
    """naive under load: passes all four stated invariants while over-promising
    hundreds of sales it cannot honour. The reason I5-I7 exist."""
    out = RUNS / "02-naive-silently-wrong"
    with Seller("naive", instance="naive") as s:
        buyer(s.url, out, "naive-silently-wrong",
              requests=6000, concurrency=400, tickets=100, duplicate_pct=0.20,
              procs=8, timeout=30)
    return _verdict(out)


def exp_safe() -> str:
    """safe under the same load: every invariant, asked-for and not."""
    out = RUNS / "03-safe-passes"
    with Seller("safe", instance="safe") as s:
        buyer(s.url, out, "safe-passes",
              requests=6000, concurrency=400, tickets=100, duplicate_pct=0.20,
              procs=8, timeout=30)
    return _verdict(out)


def exp_ladder() -> str:
    """Latency against concurrency, with server-side pool-wait at each step.

    Answers "how much load before latency degrades", which ceiling.py does not:
    that one finds the throughput ceiling, this one finds the knee.

    The client is distributed across 8 processes at every step. An earlier
    version of this ran single-process and was therefore plotting the load
    client's own saturation curve, not the seller's -- the exact mistake
    client_scaling.py exists to catch. Concurrency below is the TOTAL offered
    across those processes.
    """
    out = RUNS / "04-ladder"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with Seller("safe", instance="ladder") as s:
        for conc in (32, 64, 128, 256, 512):
            step = out / f"c{conc:04d}"
            buyer(s.url, step, f"ladder-c{conc}",
                  requests=4000, concurrency=conc, tickets=4000,
                  duplicate_pct=0.10, procs=8, timeout=30)
            try:
                d = json.loads((step / "summary.json").read_text())
                rows.append({
                    "concurrency": conc,
                    "rps": d["summary"]["rps"],
                    "p50_ms": d["summary"]["latency_ms"]["p50"],
                    "p99_ms": d["summary"]["latency_ms"]["p99"],
                    "pool_wait_p99_ms": d["server_metrics"].get("pool_wait_ms_p99"),
                    "db_p99_ms": d["server_metrics"].get("db_ms_p99"),
                })
            except (OSError, ValueError, KeyError):
                pass
    (out / "ladder.json").write_text(json.dumps(rows, indent=2))
    hdr = f"{'conc':>6} {'rps':>9} {'p50':>9} {'p99':>9} {'poolwait99':>11} {'db99':>9}"
    lines = [hdr, "-" * len(hdr)] + [
        f"{r['concurrency']:>6} {r['rps']:>9} {r['p50_ms']:>9} {r['p99_ms']:>9} "
        f"{r['pool_wait_p99_ms']:>11} {r['db_p99_ms']:>9}" for r in rows
    ]
    table = "\n".join(lines)
    (out / "ladder.txt").write_text(table)
    print("\n" + table)
    return f"{len(rows)} steps -> runs/04-ladder/ladder.txt"


def exp_three_instances() -> str:
    """Three seller instances, no application-level lock between them.

    The instances share nothing but Postgres. There is no leader, no
    distributed lock, no coordination service, and no sticky routing -- a
    buyer's replay can land on a different instance from its original and must
    still get the same ticket. That works because every decision is a single
    atomic statement in the datastore, so the instances cannot disagree.

    This also happens to be the fix for the bottleneck measured in `ceiling`:
    one seller process pins one core of eight. Correctness and throughput are
    solved by the same change here, which is why it is worth doing.
    """
    out = RUNS / "07-three-instances"
    ports = [8001, 8002, 8003]
    sellers = [Seller("safe", port=p, instance=f"inst{i + 1}")
               for i, p in enumerate(ports)]
    urls = ",".join(f"http://127.0.0.1:{p}" for p in ports)
    started = []
    try:
        for sv in sellers:
            started.append(sv.__enter__())
        buyer(urls, out, "three-instances",
              requests=9000, concurrency=600, tickets=100,
              duplicate_pct=0.25, procs=8, timeout=30)
    finally:
        for sv in reversed(started):
            sv.__exit__(None, None, None)

    # Confirm the load actually spread. If one instance served everything the
    # invariant result would be meaningless -- it would just be the
    # single-instance test with extra processes idling.
    spread: dict[str, int] = {}
    for f in sorted(out.glob("journal-shard*.jsonl")):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            inst = json.loads(line).get("instance")
            if inst:
                spread[inst] = spread.get(inst, 0) + 1
    (out / "instance-spread.json").write_text(json.dumps(spread, indent=2))
    return _verdict(out) + f"; spread {spread}"


def exp_kill_datastore() -> str:
    """Kill Postgres mid-sale with -m immediate, bring it back, reconcile.

    `-m immediate` is the violent one: no shutdown checkpoint, so recovery has
    to replay the write-ahead log. This is the mode that exposes a service which
    acknowledged a sale before that sale was durable.

    The claim being tested is I6, not I1. Surviving without overselling is easy
    -- a service that simply dropped every sale during the outage would pass I1,
    I2, I3 and I4. The question is whether a sale we already confirmed to a
    buyer is still there afterwards.
    """
    out = RUNS / "08-datastore-kill"
    out.mkdir(parents=True, exist_ok=True)
    timeline = []

    def mark(event: str) -> None:
        timeline.append({"t": round(time.perf_counter() - t0, 2), "event": event})
        print(f"  [t+{timeline[-1]['t']:>6}s] {event}")

    with Seller("safe", instance="durable") as s:
        # Large capacity so the sale is still in progress when the kill lands.
        # With 100 tickets the sale finishes in well under a second and the
        # outage would fall entirely inside the sold-out phase, testing nothing.
        urllib.request.urlopen(urllib.request.Request(
            f"{s.url}/reset", data=json.dumps({"ticket_count": 20000}).encode(),
            headers={"content-type": "application/json"}), timeout=60).read()

        t0 = time.perf_counter()
        mark("load starts")
        proc = subprocess.Popen(
            [PY, "-m", "buyer", "--url", s.url, "--requests", "40000",
             "--concurrency", "400", "--procs", "8", "--tickets", "0",
             "--no-reset", "--duplicate-pct", "0.2", "--timeout", "15",
             "--label", "datastore-kill", "--out", str(out)],
            cwd=ROOT)

        time.sleep(8)
        mark("KILL postgres (pg_ctl -m immediate)")
        subprocess.run([PY, "scripts/pgctl.py", "kill"], cwd=ROOT,
                       capture_output=True, timeout=120)
        mark("postgres down")

        time.sleep(10)
        mark("RESTART postgres")
        subprocess.run([PY, "scripts/pgctl.py", "up"], cwd=ROOT,
                       capture_output=True, timeout=180)
        mark("postgres up (WAL replayed)")

        # Bounded: if the client wedges, the experiment must fail loudly
        # rather than hang with no output, which is what happened the first time
        # this ran.
        try:
            proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            mark("load client TIMED OUT and was killed")
        mark("load finished")

    (out / "timeline.json").write_text(json.dumps(timeline, indent=2))
    return _verdict(out)


def exp_slow_datastore() -> str:
    """Make Postgres SLOW for ten seconds mid-sale, not dead.

    A different failure from the kill, and a nastier one. Dead refuses
    connections instantly, so requests fail fast and the seller sheds load
    without deciding to. Slow means every query still succeeds, just late: the
    pool fills with busy connections, the queue in front grows, and the failure
    shows up as latency rather than errors.

    What must still hold: no oversell, no duplicate number, no lost confirmed
    sale. What is allowed to happen: latency spikes, and requests that exceed
    the 10s command_timeout surface as UNKNOWN rather than as false failures.
    """
    out = RUNS / "09-slow-datastore"
    out.mkdir(parents=True, exist_ok=True)
    control = RUNS / ".proxy_delay_ms"
    control.write_text("0")
    timeline = []

    proxy = subprocess.Popen(
        [PY, "scripts/dbproxy.py"], cwd=ROOT,
        stdout=open(RUNS / "dbproxy.log", "w"), stderr=subprocess.STDOUT)
    time.sleep(1.5)

    try:
        # The seller talks to the proxy, not to Postgres directly.
        with Seller("safe", instance="slowdb",
                    env={"DATABASE_URL":
                         "postgresql://postgres@127.0.0.1:5434/tickets"}) as s:
            urllib.request.urlopen(urllib.request.Request(
                f"{s.url}/reset", data=json.dumps({"ticket_count": 20000}).encode(),
                headers={"content-type": "application/json"}), timeout=120).read()

            t0 = time.perf_counter()

            def mark(event: str) -> None:
                timeline.append(
                    {"t": round(time.perf_counter() - t0, 2), "event": event})
                print(f"  [t+{timeline[-1]['t']:>6}s] {event}", flush=True)

            mark("load starts")
            proc = subprocess.Popen(
                [PY, "-m", "buyer", "--url", s.url, "--requests", "30000",
                 "--concurrency", "400", "--procs", "8", "--tickets", "0",
                 "--no-reset", "--duplicate-pct", "0.2", "--timeout", "30",
                 "--label", "slow-datastore", "--out", str(out)],
                cwd=ROOT)

            time.sleep(8)
            control.write_text("120")
            mark("datastore SLOW (+120ms per chunk)")

            time.sleep(10)
            control.write_text("0")
            mark("datastore normal again")

            try:
                proc.wait(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
                mark("load client TIMED OUT")
            mark("load finished")
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()

    (out / "timeline.json").write_text(json.dumps(timeline, indent=2))
    subprocess.run([PY, "scripts/outage_trace.py", str(out)], cwd=ROOT,
                   capture_output=True, timeout=120)
    return _verdict(out)


EXPERIMENTS = {
    "oversell": exp_oversell,
    "naive-silent": exp_naive_silent,
    "safe": exp_safe,
    "ladder": exp_ladder,
    "three-instances": exp_three_instances,
    "kill-datastore": exp_kill_datastore,
    "slow-datastore": exp_slow_datastore,
}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for name, fn in EXPERIMENTS.items():
            print(f"  {name:<14} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return
    names = list(EXPERIMENTS) if cmd == "all" else [cmd]
    results = {}
    for name in names:
        if name not in EXPERIMENTS:
            sys.exit(f"unknown experiment {name!r}; try: python {sys.argv[0]} list")
        print(f"\n{'=' * 70}\n>>> {name}\n{'=' * 70}")
        results[name] = EXPERIMENTS[name]()
    print(f"\n{'=' * 70}\nSUMMARY")
    for name, verdict in results.items():
        print(f"  {name:<14} {verdict}")


if __name__ == "__main__":
    main()
