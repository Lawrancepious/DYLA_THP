"""Tests for the invariant checker itself.

The checker is the instrument every claim in this repo is measured with. If it
silently fails to fire, every run comes back green and the whole submission is
worthless -- the equivalent of a test suite with no assertions.

So each check is fed a journal and a /status that violate exactly one
invariant, and is required to fail that one and only that one. Several of these
tests would have passed against a checker that returned "all good"
unconditionally, which is why the negative cases matter more than the positive.

    python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buyer.invariants import check_all, summarise          # noqa: E402
from buyer.model import Attempt, Journal, Outcome          # noqa: E402


def att(rid, uid="u1", outcome=Outcome.OK, ticket=None, latency=1.0):
    return Attempt(request_id=rid, user_id=uid, outcome=outcome,
                   latency_ms=latency, ticket_no=ticket)


def journal(*attempts) -> Journal:
    j = Journal(attempts=list(attempts))
    j.started_at, j.ended_at = 0.0, 1.0
    return j


def status(tickets, capacity=100, sold=None):
    return {"sold": len(tickets) if sold is None else sold,
            "capacity": capacity, "epoch": 1, "mode": "test",
            "tickets": [{"ticket_no": n, "user_id": u, "request_id": r}
                        for n, u, r in tickets]}


def by_key(checks):
    return {c.key: c for c in checks}


# --------------------------------------------------------------------------
# The clean case: nothing should fire.
# --------------------------------------------------------------------------

def test_clean_run_passes_everything():
    j = journal(att("r1", ticket=1), att("r2", ticket=2))
    c = by_key(check_all(j, status([(1, "u1", "r1"), (2, "u1", "r2")])))
    assert all(x.passed for x in c.values()), {k: v.detail for k, v in c.items()}


# --------------------------------------------------------------------------
# One violation each. Every test asserts the OTHER checks stay green, so a
# checker that fails everything at once cannot pass this file.
# --------------------------------------------------------------------------

def test_i1_detects_oversell():
    tickets = [(n, "u", f"r{n}") for n in range(1, 4)]
    j = journal(*[att(f"r{n}", ticket=n) for n in range(1, 4)])
    c = by_key(check_all(j, status(tickets, capacity=2)))
    assert not c["I1"].passed
    assert "OVERSOLD BY 1" in c["I1"].detail
    assert c["I4"].passed and c["I5"].passed and c["I6"].passed


def test_i2_detects_duplicate_number_in_status():
    j = journal(att("r1", ticket=1), att("r2", ticket=1))
    c = by_key(check_all(j, status([(1, "u1", "r1"), (1, "u2", "r2")])))
    assert not c["I2"].passed


def test_i2_detects_duplicate_on_the_wire_only():
    """The case a /status-only checker cannot see.

    /status is immaculate -- one row, one number. But the server handed ticket 1
    to two different request ids over the wire. This is exactly what the naive
    seller does, and it is why I2 inspects the journal and not just /status.
    """
    j = journal(att("r1", ticket=1), att("r2", ticket=1))
    c = by_key(check_all(j, status([(1, "u1", "r1")])))
    assert not c["I2"].passed
    assert "wire" in c["I2"].detail


def test_i3_detects_replay_holding_two_tickets():
    j = journal(att("r1", ticket=1), att("r1", ticket=2))
    c = by_key(check_all(j, status([(1, "u1", "r1"), (2, "u1", "r1")])))
    assert not c["I3"].passed


def test_i3_detects_inconsistent_answers_to_one_request_id():
    """Only one ticket persisted, so /status looks fine -- but the buyer was
    told 1 on one attempt and 2 on another. That is an idempotency failure the
    buyer can see and /status cannot."""
    j = journal(att("r1", ticket=1), att("r1", ticket=2))
    c = by_key(check_all(j, status([(1, "u1", "r1")])))
    assert not c["I3"].passed
    assert "different numbers" in c["I3"].detail


def test_i3_allows_correct_idempotent_replay():
    j = journal(att("r1", ticket=1), att("r1", ticket=1), att("r1", ticket=1))
    c = by_key(check_all(j, status([(1, "u1", "r1")])))
    assert c["I3"].passed


def test_i4_detects_count_disagreeing_with_list():
    j = journal(att("r1", ticket=1))
    c = by_key(check_all(j, status([(1, "u1", "r1")], sold=7)))
    assert not c["I4"].passed


def test_i5_detects_phantom_ticket():
    """A ticket held by a request id the client never sent."""
    j = journal(att("r1", ticket=1))
    c = by_key(check_all(j, status([(1, "u1", "r1"), (2, "u9", "never-sent")])))
    assert not c["I5"].passed
    assert c["I1"].passed


def test_i6_detects_lost_confirmed_sale():
    """The server said "ticket 2 is yours" and /status has no such ticket.

    This is the datastore-kill invariant. Note that I1-I4 all pass here: a
    service that confirmed a sale and then lost it is invisible to every check
    the brief asks for.
    """
    j = journal(att("r1", ticket=1), att("r2", ticket=2))
    c = by_key(check_all(j, status([(1, "u1", "r1")])))
    assert not c["I6"].passed
    assert c["I1"].passed and c["I2"].passed and c["I3"].passed and c["I4"].passed


def test_i6_detects_reassigned_ticket():
    j = journal(att("r1", ticket=1))
    c = by_key(check_all(j, status([(9, "u1", "r1")])))
    assert not c["I6"].passed
    assert "reassigned" in c["I6"].detail


def test_i7_counts_in_doubt_buyers_without_failing():
    """In-doubt buyers are reported, never treated as a failure: a process that
    dies between commit and response produces them unavoidably."""
    j = journal(att("r1", outcome=Outcome.UNKNOWN))
    c = by_key(check_all(j, status([(1, "u1", "r1")])))
    assert c["I7"].advisory and c["I7"].passed
    assert "1 of them actually hold a ticket" in c["I7"].detail


def test_in_doubt_ignores_request_ids_with_any_definite_answer():
    """A request that failed once and succeeded on retry is NOT in doubt."""
    j = journal(att("r1", outcome=Outcome.UNKNOWN), att("r1", ticket=1))
    assert j.in_doubt_request_ids() == set()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_percentiles_include_failures():
    """A slow failure must count. Excluding non-200s would flatter the latency
    numbers precisely when the system is in trouble."""
    j = journal(*([att(f"r{i}", ticket=i, latency=1.0) for i in range(99)]
                  + [att("slow", outcome=Outcome.UNKNOWN, latency=9999.0)]))
    assert summarise(j)["latency_ms"]["max"] == 9999.0
    assert summarise(j)["outcomes"]["unknown"] == 1
