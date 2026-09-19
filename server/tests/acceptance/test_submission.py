"""The delivered payload: what a call submits, and how often it may try.

A call with no accepted record scores nothing, so the shape of the body and the
immutability of the plan between attempts are both scored behaviour, not
bookkeeping. ``call_id`` is required on every submit request and must be the id
the platform dialled with.
"""

import asyncio

import pytest

from submission import CallSubmission

FALLBACK = {"action": "NO_ACTION", "reason": "out_of_scope"}
OFFER = {"patient_id": "P00001", "provider_id": "PR01", "location_id": "centro",
         "appointment_type_id": "review", "slot": "2026-09-19T11:00:00+02:00", "policy_id": "mapfre"}


class FakeClient:
    def __init__(self):
        self.posted = []

    async def post_submission(self, action):
        self.posted.append(action)


def test_default_is_no_action():
    client = FakeClient()
    sub = CallSubmission("call-1", client)
    assert sub.pending == FALLBACK
    asyncio.run(sub.close())
    assert len(client.posted) == 1
    assert client.posted[0]["call_id"] == "call-1"
    assert all(client.posted[0][k] == v for k, v in FALLBACK.items())


def test_flush_never_invents_a_decision():
    """Only close() may fall back to a refusal; a retry loop uses flush()."""
    client = FakeClient()
    sub = CallSubmission("call-1", client)
    asyncio.run(sub.flush())
    assert client.posted == []


def test_set_book_then_flush():
    client = FakeClient()
    sub = CallSubmission("call-2", client)
    sub.set_book(OFFER)
    asyncio.run(sub.flush())
    assert len(client.posted) == 1
    assert client.posted[0]["action"] == "BOOK"
    assert client.posted[0]["call_id"] == "call-2"
    assert all(client.posted[0][k] == v for k, v in OFFER.items())


def test_flush_is_idempotent():
    client = FakeClient()
    sub = CallSubmission("call-3", client)
    asyncio.run(sub.close())
    asyncio.run(sub.close())
    assert len(client.posted) == 1


def test_multi_action_is_delivered_in_order():
    """PR-08 and PR-18 need two verbs in one call, in the order decided."""
    client = FakeClient()
    sub = CallSubmission("call-4", client)
    sub.set_cancel("A000123")
    sub.add_action({"action": "BOOK", **OFFER})
    asyncio.run(sub.flush())
    assert [a["action"] for a in client.posted] == ["CANCEL", "BOOK"]


def test_partially_delivered_plan_resumes_where_it_stopped(monkeypatch):
    """A flush spends its bounded attempts; a later flush resumes, not replays.

    The backoff is zeroed so the bounded retries of one flush cost no wall time:
    resumption across flushes is the property under test, not the retry loop.
    """
    monkeypatch.setenv("SUBMIT_DELIVERY_BACKOFF_SECS", "0")

    class FlakyClient(FakeClient):
        """Fails the first three BOOK postings, then accepts every action."""

        def __init__(self):
            super().__init__()
            self.book_failures = 0

        async def post_submission(self, action):
            if action["action"] == "BOOK" and self.book_failures < 3:
                self.book_failures += 1
                self.posted.append(action)
                raise OSError("offline")
            await super().post_submission(action)

    client = FlakyClient()
    sub = CallSubmission("call-5", client)
    sub.set_cancel("A000123")
    sub.add_action({"action": "BOOK", **OFFER})
    # First flush: CANCEL accepted; BOOK burns all three attempts and fails.
    assert asyncio.run(sub.flush()) is False
    assert [a["action"] for a in client.posted] == ["CANCEL", "BOOK", "BOOK", "BOOK"]
    # A later flush resumes where it stopped: only BOOK is retried, CANCEL is
    # never replayed, and this time the platform takes it.
    assert asyncio.run(sub.flush()) is True
    assert [a["action"] for a in client.posted][-1] == "BOOK"
    assert sub._delivered == 2


def test_a_provisional_refusal_is_not_delivered_by_flush():
    """A refusal the conversation can still revise is not the call's answer yet.

    Delivering it here is what let the platform record `NO_ACTION` five seconds
    into a call that went on to book an appointment.
    """
    client = FakeClient()
    sub = CallSubmission("call-6", client)
    sub.set_no_action("no_availability")
    assert asyncio.run(sub.flush()) is True
    assert client.posted == []


def test_a_provisional_refusal_is_delivered_when_the_call_ends():
    client = FakeClient()
    sub = CallSubmission("call-7", client)
    sub.set_no_action("no_availability")
    asyncio.run(sub.flush())
    asyncio.run(sub.close())
    assert [a["action"] for a in client.posted] == ["NO_ACTION"]
    assert client.posted[0]["reason"] == "no_availability"


def test_a_booking_after_a_provisional_refusal_replaces_it():
    """The regression for the five-second freeze.

    Ambiguous doctor, then clarified, then booked: the record must be the
    booking, and the refusal must never reach the platform.
    """
    client = FakeClient()
    sub = CallSubmission("call-8", client)
    sub.set_no_action("provider_not_found")
    assert asyncio.run(sub.flush()) is True
    sub.set_book(OFFER)
    assert asyncio.run(sub.flush()) is True
    assert [a["action"] for a in client.posted] == ["BOOK"]
    assert client.posted[0]["provider_id"] == OFFER["provider_id"]


def test_decide_promotes_the_pending_refusal():
    """A terminal node says the refusal out loud, so it must reach the platform."""
    client = FakeClient()
    sub = CallSubmission("call-9", client)
    sub.set_no_action("referral_required")
    sub.decide()
    asyncio.run(sub.flush())
    assert [a["action"] for a in client.posted] == ["NO_ACTION"]


def test_a_provisional_refusal_does_not_let_a_later_action_overtake_it():
    """Ordering is the record: nothing behind an undecided action may go first."""
    client = FakeClient()
    sub = CallSubmission("call-10", client)
    sub.set_no_action("no_availability")
    sub.add_action({"action": "CANCEL", "appointment_id": "A000123"})
    asyncio.run(sub.flush())
    assert client.posted == []
    asyncio.run(sub.close())
    assert [a["action"] for a in client.posted] == ["NO_ACTION", "CANCEL"]


def test_a_delivered_plan_is_never_rewritten():
    client = FakeClient()
    sub = CallSubmission("call-11", client)
    sub.set_book(OFFER)
    asyncio.run(sub.flush())
    with pytest.raises(RuntimeError):
        sub.set_no_action("no_availability")


def test_the_plan_exposes_no_internal_booking_fields():
    sub = CallSubmission("call-12", FakeClient())
    sub.set_no_action("no_availability")
    assert sub.pending == {"action": "NO_ACTION", "reason": "no_availability"}
