# Session logs

Full transcript of the AI coding session used to build this, as the brief
requires.

| file | what it is |
|---|---|
| `session-01-raw.jsonl` | The complete Claude Code session record, as written by the tool. Authoritative. |
| `session-01.md` | The same conversation rendered readable by `scripts/export_transcript.py` — turns, reasoning, tool calls, results. Tool output is truncated per call; the `.jsonl` has it in full. |

**One modification, disclosed:** a 126 KB base64 PDF payload was removed from
one record — the résumé attachment, not session content. It is replaced in
place with a note saying so. Nothing else in either file is altered, added or
reordered.

## What to look for

The turning points are mostly corrections, not clean progress:

- **The instrumentation bug.** `pool_wait_ms_p99` read 0.002ms under a saturated
  pool, which is impossible. The clock started before `store.buy()` while
  `pool.acquire()` happened inside it, so the wait was being charged to database
  time. The entire "where is the bottleneck" argument rests on that split, and
  it was wrong until it was caught.
- **The naive seller passing all four required invariants.** Not planned, not
  designed for. It is what motivated invariants I5–I7 and became the centre of
  the submission.
- **Discovering the load client was the bottleneck.** Every throughput number
  taken before that point measured the client rather than the seller — 3.6x
  was hidden under a single process.
- **A causal claim that was noise.** "The middleware halved throughput,
  774 → 386" survived one run and died on three. Repeated measurement put the
  real figure at 17%, and the write-up says so.
- **The ladder measuring the wrong thing.** Written single-process, which meant
  it was plotting the load client's saturation curve — the exact mistake
  `client_scaling.py` exists to catch. Caught and rerun before it reached the
  write-up.
- **Two Windows harness bugs.** `CTRL_BREAK_EVENT` killed the whole console
  process group including the test driver; `psql` as a subprocess hung twice,
  once mid-experiment. The second was resolved by removing the dependency rather
  than diagnosing it — a shortcut, and named as one in `DECISIONS.md`.
