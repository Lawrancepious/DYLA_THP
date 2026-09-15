"""Sample a process's CPU as a percentage of one core.

Used to turn "throughput went flat, so the server must be saturated" into a
measurement. A flat curve alone is also consistent with a lock, a fixed-size
queue, or a network limit; CPU pinned at ~100% of a single core distinguishes
"this process is compute-bound and single-threaded" from all of those.
"""
import json, sys, time, psutil  # noqa: E401


def main() -> None:
    pid, secs = int(sys.argv[1]), float(sys.argv[2])
    p = psutil.Process(pid)
    samples = []
    t_end = time.time() + secs
    p.cpu_percent(None)
    while time.time() < t_end:
        time.sleep(0.5)
        samples.append(p.cpu_percent(None))
    samples = [s for s in samples if s > 0]
    print(json.dumps({
        "pid": pid,
        "n_samples": len(samples),
        "peak_pct_of_one_core": round(max(samples), 1) if samples else 0,
        "median_pct_of_one_core": round(sorted(samples)[len(samples) // 2], 1) if samples else 0,
        "n_cores": psutil.cpu_count(),
    }, indent=2))


if __name__ == "__main__":
    main()
