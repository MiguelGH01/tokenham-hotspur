from datetime import datetime, timezone, timedelta

from booking import pick_offer

MADRID = timezone(timedelta(hours=2))
CONNECTED = datetime(2026, 9, 18, 10, 0, tzinfo=MADRID)  # Friday


def slot(provider, location, kind, start):
    return {"provider_id": provider, "provider_name": provider, "specialty_id": "gp",
            "location_id": location, "appointment_type_id": kind, "start_time": start,
            "duration_minutes": 15, "payable_with": ["mapfre"]}


def availability(*slots):
    return {"providers": [], "appointment_type": {}, "slots": list(slots), "blocked": []}


def patient(pid, insurer, visited):
    return {"patient_id": pid, "given_name": "X", "first_surname": "Y", "second_surname": "Z",
            "has_visited_before": visited, "insurer": insurer}


def test_josefa_skips_same_day():
    av = availability(slot("PR01", "centro", "review", "2026-09-18T11:00:00+02:00"),
                      slot("PR01", "centro", "review", "2026-09-19T11:00:00+02:00"))
    assert pick_offer(av, patient("P00001", "mapfre", True), CONNECTED) == {
        "patient_id": "P00001", "provider_id": "PR01", "location_id": "centro",
        "appointment_type_id": "review", "slot": "2026-09-19T11:00:00+02:00", "policy_id": "mapfre"}


def test_amelia_first_visit():
    av = availability(slot("PR07", "centro", "first_visit", "2026-09-18T12:00:00+02:00"),
                      slot("PR07", "centro", "first_visit", "2026-09-21T11:45:00+02:00"))
    assert pick_offer(av, patient("P00012", "sanitas", False), CONNECTED) == {
        "patient_id": "P00012", "provider_id": "PR07", "location_id": "centro",
        "appointment_type_id": "first_visit", "slot": "2026-09-21T11:45:00+02:00", "policy_id": "sanitas"}


def test_ignacio_orthopaedic():
    av = availability(slot("PR10", "sur", "orthopaedic_review", "2026-09-18T09:00:00+02:00"),
                      slot("PR10", "sur", "orthopaedic_review", "2026-09-21T09:30:00+02:00"))
    assert pick_offer(av, patient("P00005", "cigna", True), CONNECTED) == {
        "patient_id": "P00005", "provider_id": "PR10", "location_id": "sur",
        "appointment_type_id": "orthopaedic_review", "slot": "2026-09-21T09:30:00+02:00", "policy_id": "cigna"}


def test_chloe_monday_morning():
    av = availability(slot("PR03", "sur", "review", "2026-09-18T09:00:00+02:00"),   # same day
                      slot("PR03", "sur", "review", "2026-09-19T09:00:00+02:00"),   # Saturday
                      slot("PR03", "sur", "review", "2026-09-21T15:00:00+02:00"),   # Monday afternoon
                      slot("PR03", "sur", "review", "2026-09-21T09:00:00+02:00"))
    assert pick_offer(av, patient("P00011", "mapfre", True), CONNECTED,
                      weekday="monday", part_of_day="morning") == {
        "patient_id": "P00011", "provider_id": "PR03", "location_id": "sur",
        "appointment_type_id": "review", "slot": "2026-09-21T09:00:00+02:00", "policy_id": "mapfre"}


def test_tie_prefers_less_loaded_provider():
    av = availability(slot("PR01", "centro", "review", "2026-09-19T11:00:00+02:00"),
                      slot("PR01", "centro", "review", "2026-09-19T11:15:00+02:00"),
                      slot("PR02", "centro", "review", "2026-09-19T11:00:00+02:00"))
    assert pick_offer(av, patient("P00001", "mapfre", True), CONNECTED)["provider_id"] == "PR02"


def test_no_slots_returns_none():
    assert pick_offer(availability(), patient("P00001", "mapfre", True), CONNECTED) is None


def test_skips_empty_payable_and_sunday():
    av = availability(
        slot("PR01", "centro", "review", "2026-09-20T09:00:00+02:00"),
        {**slot("PR01", "centro", "review", "2026-09-21T09:15:00+02:00"), "payable_with": []},
        slot("PR01", "centro", "review", "2026-09-21T09:30:00+02:00"),
    )
    offer = pick_offer(av, patient("P00011", "mapfre", True), CONNECTED)
    assert offer["slot"] == "2026-09-21T09:30:00+02:00"
