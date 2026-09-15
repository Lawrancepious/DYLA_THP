"""Invariant checks.

I1-I4 are the four the brief asks for. They are all statements about /status.

I5-I7 are not asked for. They exist because I1-I4 can all pass on a system that
is visibly wrong to its buyers, and the brief's own load-client spec ("verify
the four invariants afterwards by reading /status") cannot detect that by
construction. The gap is the in-doubt window: the server commits a sale, the
connection dies before the response, and the buyer walks away believing they
failed. /status is self-consistent. The buyer is wrong. Nobody notices.

I6 in particular is the check that makes the datastore-kill experiment mean
something. Without it, "we survived the kill" reduces to "we did not oversell",
which a service that simply lost every confirmed sale would also pass.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import Journal, Outcome


@dataclass(slots=True)
class Check:
    key: str
    title: str
    passed: bool
    detail: str
    asked_for: bool = True      # was this one of the four in the brief
    advisory: bool = False      # counted and reported, but not a pass/fail gate

    @property
    def label(self) -> str:
        if self.advisory:
            return "INFO"
        return "PASS" if self.passed else "FAIL"


def _dupes(xs):
    seen, dup = set(), set()
    for x in xs:
        (dup if x in seen else seen).add(x)
    return dup


def check_all(journal: Journal, status: dict) -> list[Check]:
    tickets = status.get("tickets", [])
    capacity = status.get("capacity")
    reported = status.get("sold")

    ticket_nos = [t["ticket_no"] for t in tickets]
    by_request: dict[str, list[dict]] = {}
    for t in tickets:
        by_request.setdefault(t["request_id"], []).append(t)

    checks: list[Check] = []

    # ---- I1: never sell more tickets than exist -------------------------
    over = len(tickets) - capacity
    checks.append(Check(
        "I1", "Never oversell",
        over <= 0,
        f"{len(tickets)} sold against capacity {capacity}"
        + (f" -- OVERSOLD BY {over}" if over > 0 else ""),
    ))

    # ---- I2: never issue the same ticket number twice --------------------
    # Checked in two places: within /status, and against what buyers were told.
    # A server can keep /status clean and still have handed the same number to
    # two buyers over the wire, so the second half is not redundant.
    dup_in_status = _dupes(ticket_nos)
    granted = journal.granted()
    wire: dict[int, set[str]] = {}
    for rid, atts in granted.items():
        for a in atts:
            wire.setdefault(a.ticket_no, set()).add(rid)
    dup_on_wire = {n: rids for n, rids in wire.items() if len(rids) > 1}
    checks.append(Check(
        "I2", "Never issue a ticket number twice",
        not dup_in_status and not dup_on_wire,
        f"{len(dup_in_status)} duplicated in /status"
        f"{' ' + str(sorted(dup_in_status)[:8]) if dup_in_status else ''}; "
        f"{len(dup_on_wire)} handed to multiple request ids over the wire"
        f"{' ' + str(sorted(dup_on_wire)[:8]) if dup_on_wire else ''}",
    ))

    # ---- I3: a replayed request id yields one ticket, not two ------------
    multi = {rid: ts for rid, ts in by_request.items() if len(ts) > 1}
    # And the server must have told every replay the SAME number. Returning two
    # different numbers for one request id is an idempotency failure even if
    # only one of them was ultimately persisted.
    inconsistent = {
        rid: sorted({a.ticket_no for a in atts})
        for rid, atts in granted.items()
        if len({a.ticket_no for a in atts}) > 1
    }
    replayed = sum(1 for atts in granted.values() if len(atts) > 1)
    checks.append(Check(
        "I3", "Duplicate request id buys once",
        not multi and not inconsistent,
        f"{replayed} request ids were replayed and answered more than once; "
        f"{len(multi)} hold multiple tickets in /status"
        f"{' ' + str(list(multi)[:4]) if multi else ''}; "
        f"{len(inconsistent)} were told different numbers on different attempts"
        f"{' ' + str(list(inconsistent.items())[:4]) if inconsistent else ''}",
    ))

    # ---- I4: reported count matches issued tickets -----------------------
    checks.append(Check(
        "I4", "/status count matches issued tickets",
        reported == len(tickets),
        f"/status reports sold={reported}, list contains {len(tickets)}",
    ))

    # ---- I5: no phantom tickets (NOT asked for) --------------------------
    # A ticket held by a request id this client never sent. There is no benign
    # explanation: it means state leaked across /reset, or the server invented
    # a holder.
    sent = journal.sent_request_ids()
    phantom = [t for t in tickets if t["request_id"] not in sent]
    checks.append(Check(
        "I5", "No phantom tickets",
        not phantom,
        f"{len(phantom)} tickets held by request ids never sent"
        + (f" e.g. {[t['request_id'] for t in phantom[:4]]}" if phantom else ""),
        asked_for=False,
    ))

    # ---- I6: no lost confirmed sales (NOT asked for) ---------------------
    # The server told a buyer "ticket 47 is yours" and /status does not agree.
    # This is the invariant the datastore-kill experiment is really testing, and
    # it is the one a /status-only check cannot see.
    status_by_rid = {t["request_id"]: t["ticket_no"] for t in tickets}
    lost, reassigned = [], []
    for rid, atts in granted.items():
        promised = atts[0].ticket_no
        actual = status_by_rid.get(rid)
        if actual is None:
            lost.append((rid, promised))
        elif actual != promised:
            reassigned.append((rid, promised, actual))
    checks.append(Check(
        "I6", "No confirmed sale is lost or reassigned",
        not lost and not reassigned,
        f"{len(lost)} confirmed sales missing from /status"
        f"{' e.g. ' + str(lost[:4]) if lost else ''}; "
        f"{len(reassigned)} tickets reassigned to a different number"
        f"{' e.g. ' + str(reassigned[:4]) if reassigned else ''}",
        asked_for=False,
    ))

    # ---- I7: in-doubt buyers (advisory, NOT a pass/fail) -----------------
    # Buyers whose every attempt ended UNKNOWN. Some of them own a ticket and do
    # not know it. This is not a correctness violation -- it is unavoidable if
    # the process dies between commit and response -- but it is the real cost of
    # a crash, and reporting it as a number is more honest than declaring
    # victory because I1-I6 held.
    in_doubt = journal.in_doubt_request_ids()
    holding = [rid for rid in in_doubt if rid in status_by_rid]
    checks.append(Check(
        "I7", "Buyers left in doubt",
        True,
        f"{len(in_doubt)} request ids ended with no definite answer; "
        f"{len(holding)} of them actually hold a ticket they do not know about",
        asked_for=False,
        advisory=True,
    ))

    return checks


def summarise(journal: Journal) -> dict:
    """Throughput and latency. Percentiles are computed over every attempt that
    got an HTTP response, including sold-out and errors -- excluding failures
    would flatter the numbers exactly when the system is struggling."""
    lat = sorted(a.latency_ms for a in journal.attempts)
    n = len(lat)

    def pct(q: float) -> float:
        return lat[min(n - 1, int(q * n))] if n else 0.0

    counts: dict[str, int] = {}
    for a in journal.attempts:
        counts[a.outcome.value] = counts.get(a.outcome.value, 0) + 1

    return {
        "requests": n,
        "wall_s": round(journal.wall_s, 3),
        "rps": round(n / journal.wall_s, 1),
        "latency_ms": {
            "p50": round(pct(0.50), 2),
            "p95": round(pct(0.95), 2),
            "p99": round(pct(0.99), 2),
            "max": round(lat[-1], 2) if n else 0.0,
        },
        "outcomes": counts,
    }
