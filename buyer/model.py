"""What the buyer records, and what it is allowed to conclude from it.

The central idea of this load client is that it keeps its own book. Every
attempt is journalled with the outcome the buyer actually observed. The
invariant checks then reconcile that journal against /status.

This matters because the four invariants in the brief are all seller-side
statements about /status. /status can be perfectly self-consistent while the
buyers hold a completely different view of what they bought. Checking only
/status cannot see that class of failure at all.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum


class Outcome(str, Enum):
    OK = "ok"                # server returned a ticket number
    SOLD_OUT = "sold_out"    # server said sold out, definitively
    REJECTED = "rejected"    # 4xx that is not sold_out, e.g. no sale started
    UNKNOWN = "unknown"      # 5xx, timeout, connection reset

    @property
    def is_definite(self) -> bool:
        """Whether the buyer knows what happened.

        UNKNOWN is the interesting one. A 503 or a dropped connection does not
        mean the sale failed -- the commit may well have landed. Treating
        UNKNOWN as failure is the single most common bug in clients like this,
        and it is what produces double-buying on retry in real systems.
        """
        return self is not Outcome.UNKNOWN


@dataclass(slots=True)
class Attempt:
    request_id: str
    user_id: str
    outcome: Outcome
    latency_ms: float
    http_status: int | None = None
    ticket_no: int | None = None
    duplicate: bool = False
    instance: str | None = None
    error: str | None = None
    # Wall-clock offset from run start. Used to line failures up against the
    # moment the datastore was killed.
    t_offset_s: float = 0.0

    def to_json(self) -> dict:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        return d


@dataclass(slots=True)
class Journal:
    """The buyer's own record of the run."""

    attempts: list[Attempt] = field(default_factory=list)
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def wall_s(self) -> float:
        return max(self.ended_at - self.started_at, 1e-9)

    def granted(self) -> dict[str, list[Attempt]]:
        """request_id -> attempts where the server handed us a ticket number."""
        out: dict[str, list[Attempt]] = {}
        for a in self.attempts:
            if a.outcome is Outcome.OK and a.ticket_no is not None:
                out.setdefault(a.request_id, []).append(a)
        return out

    def sent_request_ids(self) -> set[str]:
        return {a.request_id for a in self.attempts}

    def in_doubt_request_ids(self) -> set[str]:
        """Request ids whose every attempt ended UNKNOWN.

        These buyers cannot tell whether they own a ticket. They are not
        necessarily a bug -- they are the honest cost of a crash mid-flight --
        but they must be counted, because a system that quietly produces
        thousands of them is not one you would run a real sale on.
        """
        by_id: dict[str, list[Attempt]] = {}
        for a in self.attempts:
            by_id.setdefault(a.request_id, []).append(a)
        return {
            rid for rid, atts in by_id.items()
            if all(not a.outcome.is_definite for a in atts)
        }

    def write(self, path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            for a in self.attempts:
                fh.write(json.dumps(a.to_json()) + "\n")


def load_journals(paths) -> Journal:
    """Merge shard journals written by separate client processes.

    Wall-clock timing is NOT taken from these files. Each shard only knows its
    own start and finish, and summing or averaging those misreports aggregate
    throughput whenever the shards do not overlap perfectly. The coordinator
    measures the true wall clock and sets it on the merged journal.
    """
    import json as _json

    merged = Journal()
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = _json.loads(line)
                d["outcome"] = Outcome(d["outcome"])
                merged.attempts.append(Attempt(**d))
    merged.attempts.sort(key=lambda a: a.t_offset_s)
    return merged
