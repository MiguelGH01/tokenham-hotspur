"""A submission that failed to POST must still be sendable; a stale refusal must not stick."""

import asyncio

from submission import CallSubmission

OFFER = {"patient_id": "P00001", "provider_id": "PR01", "location_id": "centro",
         "appointment_type_id": "review", "slot": "2026-09-19T11:00:00+02:00", "policy_id": "mapfre"}


class FlakyClient:
    """Fails the first `failures` POSTs, then accepts."""

    def __init__(self, failures: int):
        self.failures, self.attempts, self.posted = failures, 0, []

    async def post_submission(self, action):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ConnectionError("network blip at hang-up")
        self.posted.append(action)


def test_failed_post_is_retried_by_the_next_flush():
    client = FlakyClient(failures=1)
    sub = CallSubmission("c1", client)
    sub.set_book(OFFER)

    asyncio.run(sub.flush())  # on_client_disconnected: fails
    asyncio.run(sub.flush())  # run_bot's finally: must try again

    assert len(client.posted) == 1 and client.posted[0]["action"] == "BOOK"


def test_successful_post_is_still_sent_only_once():
    client = FlakyClient(failures=0)
    sub = CallSubmission("c2", client)

    asyncio.run(sub.flush())
    asyncio.run(sub.flush())

    assert client.attempts == 1


def test_offer_after_a_no_availability_clears_the_stale_refusal():
    # no_slots -> caller drops a constraint -> a slot is offered -> the call is cut off.
    client = FlakyClient(failures=0)
    sub = CallSubmission("c3", client)
    sub.set_no_action("no_availability")
    sub.set_offer(OFFER)

    asyncio.run(sub.flush())

    assert client.posted[0]["action"] == "BOOK"
