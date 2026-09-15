# Session logs

Full transcripts of the AI coding sessions used to build this, as required by
the brief.

## What to look for

The submission's turning points are all in here, and most of them are
corrections rather than clean progress:

- **The instrumentation bug.** `pool_wait_ms_p99` read 0.002ms under a saturated
  pool, which is impossible. The clock was started before `store.buy()` while
  `pool.acquire()` happened inside it, so the wait was being charged to database
  time. The entire "where is the bottleneck" argument rests on that split.
- **The naive seller passing all four required invariants.** Not planned. It is
  what motivated invariants I5-I7 and became the centre of the submission.
- **Discovering the load client was the bottleneck.** Every throughput number
  taken before that point was measuring the client, not the seller.
- **A causal claim that was noise.** "The middleware halved throughput,
  774 -> 386" survived one run and died on three. Repeated measurement put the
  real figure at 17%.
- **Two Windows harness bugs**: `CTRL_BREAK_EVENT` killing the whole console
  process group including the test driver, and `psql` as a subprocess hanging
  twice — the second resolved by removing the dependency rather than diagnosing
  it, which is a shortcut and is named as one in DECISIONS.md.
