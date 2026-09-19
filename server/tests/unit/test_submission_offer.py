import asyncio

from submission import CallSubmission

OFFER = {"patient_id": "P00005", "provider_id": "PR10", "location_id": "sur",
         "appointment_type_id": "orthopaedic_review", "slot": "2026-09-21T09:30:00+02:00", "policy_id": "cigna"}
OTHER = {**OFFER, "provider_id": "PR06", "slot": "2026-09-22T10:00:00+02:00"}


class FakeClient:
    def __init__(self):
        self.posted = []

    async def post_submission(self, action):
        self.posted.append(action)


def flushed(sub, client):
    asyncio.run(sub.flush())
    assert len(client.posted) == 1
    return client.posted[0]


def test_unconfirmed_offer_is_submitted_when_call_ends():
    client = FakeClient(); sub = CallSubmission("c1", client)
    sub.set_offer(OFFER)
    sent = flushed(sub, client)
    assert sent["action"] == "BOOK" and sent["slot"] == OFFER["slot"] and sent["call_id"] == "c1"


def test_declined_offer_is_not_submitted():
    client = FakeClient(); sub = CallSubmission("c2", client)
    sub.set_offer(OFFER); sub.clear_offer()
    assert flushed(sub, client)["action"] == "NO_ACTION"


def test_confirmed_booking_wins_over_a_later_offer():
    client = FakeClient(); sub = CallSubmission("c3", client)
    sub.set_offer(OFFER); sub.set_book(OFFER); sub.set_offer(OTHER)
    assert flushed(sub, client)["slot"] == OFFER["slot"]


def test_offer_after_asking_about_another_doctor_is_submitted():
    client = FakeClient(); sub = CallSubmission("c5", client)
    sub.set_no_action("provider_not_found")
    sub.set_offer(OFFER)
    sent = flushed(sub, client)
    assert sent["action"] == "BOOK" and sent["slot"] == OFFER["slot"]


def test_explicit_no_action_is_not_overridden_by_an_offer():
    client = FakeClient(); sub = CallSubmission("c4", client)
    sub.set_offer(OFFER); sub.set_no_action("no_availability")
    sent = flushed(sub, client)
    assert sent["action"] == "NO_ACTION" and sent["reason"] == "no_availability"
