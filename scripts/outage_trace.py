"""Bucket a run's attempts over time, to see the outage in the data.

The invariant table says the datastore-kill run passed. It does not show what
the sale actually looked like while Postgres was gone, and the shape matters:
the recovery is markedly slower than the outage that caused it.

    python scripts/outage_trace.py runs/08-datastore-kill
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def main() -> None:
    run = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/08-datastore-kill")
    bucket_s = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    buckets: dict[int, collections.Counter] = collections.defaultdict(
        collections.Counter)
    for f in sorted(run.glob("journal-shard*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            buckets[int(d["t_offset_s"] // bucket_s) * bucket_s][d["outcome"]] += 1

    lines = [f"{'t+s':>5} {'ok':>7} {'sold_out':>9} {'unknown':>8}  {'':<22}",
             "-" * 55]
    for t in sorted(buckets):
        c = buckets[t]
        note = ""
        if c["unknown"] and not c["ok"]:
            note = "datastore unreachable"
        elif c["unknown"] and c["ok"]:
            note = "recovering"
        lines.append(f"{t:>5} {c['ok']:>7} {c['sold_out']:>9} "
                     f"{c['unknown']:>8}  {note:<22}")
    table = "\n".join(lines)
    print(table)
    (run / "outage-trace.txt").write_text(table)
    print(f"\n-> {run / 'outage-trace.txt'}")


if __name__ == "__main__":
    main()
