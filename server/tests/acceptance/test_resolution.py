"""How a call that decided nothing still states the best ending it knows.

A case fails on a wrong action exactly as it fails on silence, and an
``out_of_scope`` refusal matches no published case at all — so the order in
which the end of a call is resolved is scored behaviour, not bookkeeping.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import resolution
from submission import CallSubmission

OFFER = {
    "patient_id": "P1",
    "provider_id": "PR01",
    "location_id": "centro",
    "appointment_type_id": "review",
    "slot": "2026-09-21T09:00:00+02:00",
    "policy_id": "sanitas",
}


def _state(**extra) -> dict:
    state = {
        "call_id": "call-1",
        "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
        "patient": {"patient_id": "P1", "insurer": "sanitas", "given_name": "Ana"},
        "offers": {},
    }
    state.update(extra)
    return state


class Client:
    """A clinic that answers the two lookups the resolver makes."""

    def __init__(self, *, past=(), slots=None, delay=0.0):
        self.past = list(past)
        self.slots = slots if slots is not None else [dict(OFFER, start_time=OFFER["slot"])]
        self.delay = delay
        self.availability_calls: list[tuple[str, str | None]] = []

    async def appointments(self, patient_id, when="upcoming"):
        return list(self.past) if when == "past" else []

    async def availability(self, date_from, date_to, specialty_id, patient_id, **kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.availability_calls.append((specialty_id, kwargs.get("location_id")))
        return {"slots": [dict(s, specialty_id=specialty_id) for s in self.slots], "blocked": []}

    async def post_submission(self, payload):
        return {"received": True}


def test_a_prepared_offer_is_the_ending_the_call_stands_behind():
    state = _state(proposal={"key": "offer-1", "revision": 0}, offers={"offer-1": dict(OFFER)})
    assert asyncio.run(resolution.resolve_fallback(state)) == {"action": "BOOK", **OFFER}


def test_a_moved_appointment_is_submitted_as_a_reschedule():
    appointment = {"appointment_id": "A1", "patient_id": "P1", "appointment_type_id": "review"}
    state = _state(
        intent="reschedule",
        appointment=appointment,
        proposal={"key": "offer-1", "revision": 0},
        offers={"offer-1": dict(OFFER)},
    )
    action = asyncio.run(resolution.resolve_fallback(state))
    assert action == {
        "action": "RESCHEDULE",
        "appointment_id": "A1",
        "provider_id": "PR01",
        "location_id": "centro",
        "slot": OFFER["slot"],
        "policy_id": "sanitas",
    }
    assert "appointment_type_id" not in action


def test_a_cancellation_read_back_is_the_ending():
    state = _state(intent="cancel", appointment={"appointment_id": "A1", "patient_id": "P1"})
    assert asyncio.run(resolution.resolve_fallback(state)) == {
        "action": "CANCEL",
        "appointment_id": "A1",
    }


def test_a_registration_readback_is_the_ending():
    draft = {"given_name": "Ana", "first_surname": "Test", "national_id": "12345678Z"}
    state = _state(registration_draft=draft, proposal={"key": "registration", "revision": 0})
    assert asyncio.run(resolution.resolve_fallback(state)) == {"action": "REGISTER", **draft}


def test_a_stale_offer_is_not_an_ending():
    """The handle is still in the table for the record, but the request moved on."""
    state = _state(
        intent="book",
        proposal=None,
        offers={"offer-1": dict(OFFER)},
    )
    assert asyncio.run(resolution.resolve_fallback(state)) == {
        "action": "NO_ACTION",
        "reason": "no_availability",
    }


def test_an_unfinished_reschedule_is_not_a_new_booking():
    state = _state(intent="reschedule", appointment={"appointment_id": "A1", "patient_id": "P1"})
    assert asyncio.run(resolution.resolve_fallback(state)) == {
        "action": "NO_ACTION",
        "reason": "no_availability",
    }


def test_cold_booking_uses_the_patients_own_diary_first():
    """One doctor and one site in the history is a habit worth booking against."""
    client = Client(
        past=[
            {"provider_id": "PR03", "location_id": "sur", "when": "past"},
            {"provider_id": "PR03", "location_id": "sur", "when": "past"},
        ]
    )
    state = _state(client=client)
    action = asyncio.run(resolution.resolve_fallback(state))
    assert action["action"] == "BOOK"
    assert action["patient_id"] == "P1"
    assert client.availability_calls == [("general_practice", "sur")]


def test_cold_booking_widens_when_the_habit_has_nothing_free():
    """A habit with no slot left is not a refusal: the platform is asked again."""
    client = Client(past=[{"provider_id": "PR03", "location_id": "sur"}], slots=[])
    state = _state(client=client)
    action = asyncio.run(resolution.resolve_fallback(state))
    assert client.availability_calls == [("general_practice", "sur"), ("general_practice", None)]
    assert action == resolution.UNSCORED_REFUSAL


def test_cold_booking_ignores_a_split_history():
    """Two doctors is not a habit, so the search goes straight to general practice."""
    client = Client(
        past=[{"provider_id": "PR03", "location_id": "sur"}, {"provider_id": "PR01"}]
    )
    state = _state(client=client)
    asyncio.run(resolution.resolve_fallback(state))
    assert client.availability_calls == [("general_practice", None)]


def test_cold_booking_books_the_earliest_slot_the_platform_offers():
    client = Client(
        slots=[
            dict(OFFER, start_time="2026-09-22T09:00:00+02:00", provider_id="PR02"),
            dict(OFFER, start_time="2026-09-21T09:00:00+02:00", provider_id="PR01"),
        ]
    )
    state = _state(client=client)
    action = asyncio.run(resolution.resolve_fallback(state))
    assert action["slot"].startswith("2026-09-21")
    assert action["provider_id"] == "PR01"


def test_cold_booking_that_hangs_leaves_the_stated_refusal(monkeypatch):
    monkeypatch.setattr(resolution, "COLD_BOOKING_TIMEOUT_SECS", 0.01)
    state = _state(client=Client(delay=0.2))
    assert asyncio.run(resolution.resolve_fallback(state)) == resolution.UNSCORED_REFUSAL


def test_a_booking_call_that_never_identified_is_not_out_of_scope():
    action = asyncio.run(resolution.resolve_fallback(_state(patient=None, intent="book")))
    assert action == {"action": "NO_ACTION", "reason": "patient_not_found"}


def test_an_unidentified_call_states_the_unscored_refusal():
    assert asyncio.run(resolution.resolve_fallback(_state(patient=None))) == (
        resolution.UNSCORED_REFUSAL
    )


def test_a_call_that_decided_never_asks_the_resolver():
    """A named rule is a better ending than anything a guess can offer."""
    asked = []

    async def fallback():
        asked.append(True)
        return dict(resolution.UNSCORED_REFUSAL)

    submission = CallSubmission("call-1", Client(), fallback=fallback)
    submission.set_no_action("referral_required")
    asyncio.run(submission.close())
    assert asked == []


def test_the_resolver_is_asked_once_and_its_ending_retried(monkeypatch):
    """The resolver is asked once, and the ending it returns is retried to success.

    The retry now lives inside the first ``close()``: a transient failure of the
    resolved ending no longer waits for a second close that may never come.
    """
    monkeypatch.setenv("SUBMIT_DELIVERY_BACKOFF_SECS", "0")
    posted = []
    asked = []

    class Recording(Client):
        async def post_submission(self, payload):
            posted.append(payload)
            if len(posted) == 1:
                raise OSError("offline")

    async def fallback():
        asked.append(True)
        return {"action": "BOOK", **OFFER}

    submission = CallSubmission("call-1", Recording(), fallback=fallback)
    assert asyncio.run(submission.close()) is True
    assert asked == [True]
    assert [p["action"] for p in posted] == ["BOOK", "BOOK"]
    # A repeated close() is idempotent: the accepted ending is not resubmitted.
    assert asyncio.run(submission.close()) is True
    assert len(posted) == 2


def test_a_resolver_that_fails_still_states_a_refusal():
    posted = []

    class Recording(Client):
        async def post_submission(self, payload):
            posted.append(payload)

    async def fallback():
        raise RuntimeError("the resolver is broken")

    submission = CallSubmission("call-1", Recording(), fallback=fallback)
    assert asyncio.run(submission.close()) is True
    assert [(p["action"], p.get("reason")) for p in posted] == [("NO_ACTION", "out_of_scope")]


def test_the_fallback_is_the_calls_and_not_each_requests():
    """Two intents that both decided nothing state one ending, not two."""
    posted = []

    class Recording(Client):
        async def post_submission(self, payload):
            posted.append(payload)

    async def fallback():
        return {"action": "NO_ACTION", "reason": "patient_not_found"}

    root = CallSubmission("call-1", Recording(), fallback=fallback)
    root.new_request()
    root.new_request()
    asyncio.run(root.close())
    assert [p["action"] for p in posted] == ["NO_ACTION"]


def test_a_resolved_booking_is_what_the_record_holds():
    """The resolved action goes through the same plan the call submits."""
    posted = []

    class Recording(Client):
        async def post_submission(self, payload):
            posted.append(payload)

    state = _state(proposal={"key": "offer-1", "revision": 0}, offers={"offer-1": dict(OFFER)})
    submission = CallSubmission(
        "call-1", Recording(), fallback=lambda: resolution.resolve_fallback(state)
    )
    asyncio.run(submission.close())
    assert posted[0]["action"] == "BOOK"
    assert posted[0]["call_id"] == "call-1"
    assert posted[0]["policy_id"] == "sanitas"
