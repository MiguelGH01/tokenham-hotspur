"""PR-03: a named doctor. Which constraint is dropped when they cannot be had as asked."""

import asyncio

from flow.tools import end_without_booking, get_earliest_slot
from tests.unit.test_flow_tools import PATIENT, FakeClinic, _flow


def _slot(provider_id, start, site="centro"):
    return {"provider_id": provider_id, "provider_name": f"Dr. {provider_id}", "location_id": site,
            "appointment_type_id": "review", "start_time": start}


def _search(flow, **args):
    return asyncio.run(get_earliest_slot({"specialty": "general_practice", **args}, flow))


def test_named_doctor_is_kept_even_when_a_colleague_is_free_sooner():
    clinic = FakeClinic(slots=[_slot("PR07", "2026-09-21T09:00:00+02:00"), _slot("PR01", "2026-09-22T11:00:00+02:00")])
    flow = _flow(clinic, patient=PATIENT)

    result, node = _search(flow, provider="PR01")

    assert result["status"] == "offer" and "note" not in result and node["name"] == "confirm"
    assert flow.state["offers"]["offer-1"]["provider_id"] == "PR01"


def test_doctor_on_leave_falls_back_to_a_colleague_not_to_their_post_leave_slots():
    # Live shape: `blocked` is empty and Requena (PR02, on leave to 30 Sep) is listed from 1 Oct.
    clinic = FakeClinic(slots=[_slot("PR02", "2026-10-01T09:00:00+02:00", "norte"),
                               _slot("PR07", "2026-09-22T09:00:00+02:00", "norte")])
    flow = _flow(clinic, patient=PATIENT)

    result, _ = _search(flow, provider="PR02", site="norte")

    assert result["status"] == "offer" and result["note"] == "on_leave"
    assert flow.state["offers"]["offer-1"]["provider_id"] == "PR07"


def test_wrong_weekday_keeps_the_doctor_and_drops_the_day():
    # Sáez (PR03) holds Centro hours on Fridays only; a colleague is there on Monday.
    clinic = FakeClinic(slots=[_slot("PR07", "2026-09-21T09:00:00+02:00"), _slot("PR03", "2026-09-25T09:30:00+02:00")])
    flow = _flow(clinic, patient=PATIENT)

    result, _ = _search(flow, provider="PR03", site="centro", weekday="monday")

    assert result["status"] == "offer" and result["note"] == "other_day"
    assert flow.state["offers"]["offer-1"]["slot"] == "2026-09-25T09:30:00+02:00"


def test_unknown_doctor_books_nothing_and_says_why():
    clinic = FakeClinic(slots=[_slot("PR06", "2026-09-22T08:00:00+02:00")])
    flow = _flow(clinic, patient=PATIENT)

    result, node = _search(flow, provider="not_listed")
    _, close = asyncio.run(end_without_booking(flow))
    asyncio.run(flow.state["submission"].flush())

    assert result == {"status": "provider_not_found"} and node is None
    assert close["name"] == "close" and flow.state["offers"] == {}
    assert clinic.posted == [{"call_id": "c1", "action": "NO_ACTION", "reason": "provider_not_found"}]
