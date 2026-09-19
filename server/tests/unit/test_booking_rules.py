"""Search window, closure days, and the test-only call clock."""

from datetime import datetime

from booking import MADRID, pick_offer, search_window
from bot import _connected_at

PATIENT = {"patient_id": "P00001", "insurer": "mapfre"}
CONNECTED = datetime(2026, 10, 9, 10, 0, tzinfo=MADRID)  # Friday before Fiesta Nacional


def _slot(start):
    return {"provider_id": "PR01", "location_id": "centro", "appointment_type_id": "review", "start_time": start}


def test_search_window_starts_the_day_after_the_call_and_spans_14_days():
    assert search_window(datetime(2026, 9, 18, 23, 30, tzinfo=MADRID)) == ("2026-09-19", "2026-10-02")


def test_closed_day_is_skipped_for_the_next_open_one():
    availability = {"slots": [_slot("2026-10-12T09:00:00+02:00"), _slot("2026-10-13T09:45:00+02:00")]}

    offer = pick_offer(availability, PATIENT, CONNECTED, closed_days=frozenset({"2026-10-12"}))

    assert offer["slot"] == "2026-10-13T09:45:00+02:00"


def test_without_closed_days_nothing_is_filtered():
    availability = {"slots": [_slot("2026-10-12T09:00:00+02:00")]}

    assert pick_offer(availability, PATIENT, CONNECTED, closed_days=frozenset())["slot"] == "2026-10-12T09:00:00+02:00"


def test_clock_override_applies_only_where_it_is_allowed(monkeypatch):
    monkeypatch.setenv("CALL_CLOCK_OVERRIDE", "2026-09-18T10:00:00+02:00")

    assert _connected_at(allow_override=True).date().isoformat() == "2026-09-18"
    assert _connected_at(allow_override=False).year >= 2026
    assert _connected_at(allow_override=False).date().isoformat() != "2026-09-18"
