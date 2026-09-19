"""Regression coverage for the duplicate-tool-call bug seen in run-logs.

Pipecat can trigger two overlapping LLM inferences for one caller turn (a
mid-sentence pause long enough to trip the speech-timeout turn-stop
fallback), and each independently calls the same booking tool. Before the
`_dedupe` guard, the second call either raced the real handler (risking a
duplicate lookup/booking) or hit pipecat's "was just unregistered between
queueing and execution" fallback and confused the model. See
run-logs/twilio-20260919-105912.log:296-325 and :458-510 for a live example
(duplicate get_earliest_slot and confirm_offer calls in the same turn).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from flow.tools import _dedupe, confirm_offer, get_earliest_slot

MADRID = timezone(timedelta(hours=2))


def _flow_manager(**state):
    async def queue_frames(_frames):
        pass

    return SimpleNamespace(state={"call_id": "call-1", **state}, worker=SimpleNamespace(queue_frames=queue_frames))


def test_dedupe_coalesces_concurrent_calls_with_same_key():
    calls = 0

    async def run():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"status": "ok", "n": calls}

    async def scenario():
        fm = _flow_manager()
        first = asyncio.ensure_future(_dedupe(fm, "get_earliest_slot", run))
        await asyncio.sleep(0)  # let the first call register itself as in-flight
        second = asyncio.ensure_future(_dedupe(fm, "get_earliest_slot", run))
        return await asyncio.gather(first, second)

    results = asyncio.run(scenario())
    assert calls == 1
    assert results[0] == results[1] == {"status": "ok", "n": 1}


def test_dedupe_runs_separately_for_different_keys():
    calls = []

    async def run(tag):
        calls.append(tag)
        return tag

    async def scenario():
        fm = _flow_manager()
        a = asyncio.ensure_future(_dedupe(fm, "get_earliest_slot:1", lambda: run("a")))
        b = asyncio.ensure_future(_dedupe(fm, "get_earliest_slot:2", lambda: run("b")))
        return await asyncio.gather(a, b)

    results = asyncio.run(scenario())
    assert sorted(calls) == ["a", "b"]
    assert set(results) == {"a", "b"}


def test_dedupe_allows_sequential_calls_with_same_key():
    """A later, genuinely separate call (e.g. after revise_search) must still run fresh."""
    calls = 0

    async def run():
        nonlocal calls
        calls += 1
        return calls

    async def scenario():
        fm = _flow_manager()
        first = await _dedupe(fm, "search_patient", run)
        second = await _dedupe(fm, "search_patient", run)
        return first, second

    first, second = asyncio.run(scenario())
    assert (first, second) == (1, 2)
    assert calls == 2


class FakeClient:
    def __init__(self, availability):
        self._availability = availability
        self.availability_calls = 0

    async def availability(self, date_from, date_to, specialty_id, patient_id, location_id=None, provider_id=None):
        self.availability_calls += 1
        return self._availability


def _availability():
    return {
        "providers": [],
        "appointment_type": {},
        "blocked": [],
        "slots": [
            {
                "provider_id": "PR03",
                "provider_name": "Dr. Martín Sáez",
                "specialty_id": "general_practice",
                "location_id": "sur",
                "appointment_type_id": "first_visit",
                "start_time": "2026-09-21T09:00:00+02:00",
                "duration_minutes": 15,
                "payable_with": ["sanitas"],
            }
        ],
    }


def test_concurrent_get_earliest_slot_calls_hit_the_backend_once():
    client = FakeClient(_availability())
    fm = _flow_manager(
        connected_at=datetime(2026, 9, 19, 10, 0, tzinfo=MADRID),
        patient={"patient_id": "P00012", "insurer": "sanitas", "date_of_birth": None},
        offers={},
        client=client,
    )
    args = {"specialty": "general_practice"}

    async def scenario():
        first = asyncio.ensure_future(get_earliest_slot(args, fm))
        second = asyncio.ensure_future(get_earliest_slot(args, fm))
        return await asyncio.gather(first, second)

    (result_a, _), (result_b, _) = asyncio.run(scenario())
    assert client.availability_calls == 1
    assert result_a == result_b
    assert result_a["status"] == "offer"
    assert len(fm.state["offers"]) == 1


def test_concurrent_confirm_offer_calls_book_once():
    class FakeSubmission:
        def __init__(self):
            self.booked = []

        def set_book(self, offer):
            self.booked.append(offer)

    submission = FakeSubmission()
    fm = _flow_manager(offers={"offer-1": {"provider_id": "PR03"}}, submission=submission)
    args = {"offer_id": "offer-1"}

    async def scenario():
        first = asyncio.ensure_future(confirm_offer(args, fm))
        second = asyncio.ensure_future(confirm_offer(args, fm))
        return await asyncio.gather(first, second)

    (result_a, _), (result_b, _) = asyncio.run(scenario())
    assert result_a == result_b == {"status": "confirmed"}
    assert len(submission.booked) == 1
