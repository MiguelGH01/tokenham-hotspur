import asyncio

from submission import NO_OUTCOME_REASON, CallSubmission

OFFER = {"patient_id": "P00005", "provider_id": "PR10", "location_id": "sur",
         "appointment_type_id": "orthopaedic_review", "slot": "2026-09-21T09:30:00+02:00", "policy_id": "cigna"}


class FakeClient:
    def __init__(self):
        self.posted = []

    async def post_submission(self, action):
        self.posted.append(action)


def flushed(sub, client):
    asyncio.run(sub.flush())
    assert len(client.posted) == 1
    return client.posted[0]


def test_an_offer_nobody_accepted_is_never_booked():
    client = FakeClient(); sub = CallSubmission("c1", client)
    sub.set_no_outcome()
    sent = flushed(sub, client)
    assert sent == {"call_id": "c1", "action": "NO_ACTION", "reason": NO_OUTCOME_REASON}


def test_nobody_identified_stays_patient_not_found():
    client = FakeClient(); sub = CallSubmission("c2", client)
    assert flushed(sub, client)["reason"] == "patient_not_found"


def test_confirmed_booking_wins_over_a_later_offer():
    client = FakeClient(); sub = CallSubmission("c3", client)
    sub.set_no_outcome(); sub.set_book(OFFER); sub.set_no_outcome()
    sent = flushed(sub, client)
    assert sent["action"] == "BOOK" and sent["slot"] == OFFER["slot"]


def test_explicit_no_action_is_not_overridden_by_an_earlier_offer():
    client = FakeClient(); sub = CallSubmission("c4", client)
    sub.set_no_outcome(); sub.set_no_action("no_availability")
    sent = flushed(sub, client)
    assert sent["action"] == "NO_ACTION" and sent["reason"] == "no_availability"
