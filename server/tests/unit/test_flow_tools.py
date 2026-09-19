"""The tool handlers: identification attempts, exact-id filtering, and what gets pending."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

from booking import MADRID
from flow.tools import confirm_offer, get_earliest_slot, search_patient
from submission import NO_OUTCOME_REASON, CallSubmission

VALID_DNI = "12345678Z"
PATIENT = {"patient_id": "P00001", "given_name": "Josefa", "first_surname": "Domínguez",
           "national_id": VALID_DNI, "phone": "600111222", "has_visited_before": True, "insurer": "mapfre"}


def _slot(start):
    return {"provider_id": "PR01", "provider_name": "Dra. Ana Ortiz", "location_id": "centro",
            "appointment_type_id": "review", "start_time": start}


class FakeClinic:
    def __init__(self, matches=(), slots=(), blocked=()):
        self.matches, self.slots, self.directory_calls, self.posted = list(matches), list(slots), 0, []
        self.blocked = list(blocked)

    async def search_directory(self, **query):
        self.directory_calls += 1
        return self.matches

    async def availability(self, *args, **kwargs):
        return {"slots": self.slots, "blocked": self.blocked}

    async def post_submission(self, action):
        self.posted.append(action)


def _flow(clinic, patient=None):
    async def queue_frames(frames):
        pass

    state = {"client": clinic, "submission": CallSubmission("c1", clinic), "patient": patient,
             "offers": {}, "identify_attempts": 0,
             "connected_at": datetime(2026, 9, 18, 10, 0, tzinfo=MADRID)}
    return SimpleNamespace(state=state, worker=SimpleNamespace(queue_frames=queue_frames))


def _identify(flow, id_value, id_type="national_id"):
    args = {"stated_name": "Josefa Dominguez", "id_type": id_type, "id_value": id_value}
    return asyncio.run(search_patient(args, flow))


def test_misheard_id_is_rejected_locally_without_an_api_call():
    clinic = FakeClinic(matches=[PATIENT])
    result, node = _identify(_flow(clinic), "12345678A")  # wrong check letter

    assert result == {"status": "misheard_id", "attempts_left": 2} and node is None
    assert clinic.directory_calls == 0


def test_third_failed_identification_gives_up():
    flow = _flow(FakeClinic(matches=[]))

    outcomes = [_identify(flow, VALID_DNI) for _ in range(3)]

    assert [node for _, node in outcomes[:2]] == [None, None]
    assert outcomes[2][0]["attempts_left"] == 0 and outcomes[2][1]["name"] == "giveup"


def test_found_patient_moves_to_the_slot_node():
    flow = _flow(FakeClinic(matches=[PATIENT]))

    result, node = _identify(flow, " 12345678-z ")

    assert result["status"] == "found" and node["name"] == "find_slot"
    assert flow.state["patient"] == PATIENT


def test_only_the_exact_identifier_counts_as_a_match():
    namesake = {**PATIENT, "patient_id": "P00099", "national_id": "87654321X"}
    flow = _flow(FakeClinic(matches=[namesake, PATIENT]))

    assert _identify(flow, VALID_DNI)[0]["status"] == "found"
    assert flow.state["patient"]["patient_id"] == "P00001"


def test_no_slots_records_no_availability_as_the_reason():
    clinic = FakeClinic(slots=[])
    flow = _flow(clinic, patient=PATIENT)

    result, node = asyncio.run(get_earliest_slot({"specialty": "general_practice"}, flow))
    asyncio.run(flow.state["submission"].flush())

    assert result == {"status": "no_slots"} and node is None
    assert clinic.posted[0]["action"] == "NO_ACTION" and clinic.posted[0]["reason"] == "no_availability"


def test_no_slots_reports_the_restriction_the_api_blocked_on():
    clinic = FakeClinic(slots=[], blocked=[{"provider_id": "PR01", "restriction": "referral_required"}])
    flow = _flow(clinic, patient=PATIENT)

    asyncio.run(get_earliest_slot({"specialty": "general_practice"}, flow))
    asyncio.run(flow.state["submission"].flush())

    assert clinic.posted[0]["reason"] == "referral_required"


def test_offer_is_never_on_a_closure_day():
    # The API lists Fiesta Nacional slots (LIVE-01); clinic.json marks 2026-10-12 closed.
    clinic = FakeClinic(slots=[_slot("2026-10-12T09:00:00+02:00"), _slot("2026-10-13T09:45:00+02:00")])
    flow = _flow(clinic, patient=PATIENT)
    flow.state["connected_at"] = datetime(2026, 10, 9, 10, 0, tzinfo=MADRID)

    result, node = asyncio.run(get_earliest_slot({"specialty": "general_practice"}, flow))

    assert result["status"] == "offer" and node["name"] == "confirm"
    assert flow.state["offers"]["offer-1"]["slot"] == "2026-10-13T09:45:00+02:00"


def test_found_patient_who_then_vanishes_is_not_reported_as_not_found():
    clinic = FakeClinic(matches=[PATIENT])
    flow = _flow(clinic)

    _identify(flow, VALID_DNI)
    asyncio.run(flow.state["submission"].flush())

    assert clinic.posted[0]["action"] == "NO_ACTION" and clinic.posted[0]["reason"] == NO_OUTCOME_REASON


def test_only_the_latest_offer_can_be_confirmed():
    clinic = FakeClinic(slots=[_slot("2026-09-21T09:00:00+02:00")])
    flow = _flow(clinic, patient=PATIENT)
    asyncio.run(get_earliest_slot({"specialty": "general_practice"}, flow))
    asyncio.run(get_earliest_slot({"specialty": "general_practice"}, flow))  # caller revised: offer-2

    stale, node = asyncio.run(confirm_offer({"offer_id": "offer-1"}, flow))
    assert stale == {"status": "expired"} and node is None

    fresh, node = asyncio.run(confirm_offer({"offer_id": "offer-2"}, flow))
    assert fresh == {"status": "confirmed"} and node["name"] == "goodbye"
