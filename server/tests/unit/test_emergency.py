"""Primary-care picker and next standing tick for emergency overlays."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from clinic_catalog import load_catalog
from observability.emergency import (
    next_standing_slot,
    on_duty_providers,
    pick_primary_care_provider,
    plan_emergency_slot,
    primary_specialty_id,
)
from rules import provider_by_id

MADRID = ZoneInfo("Europe/Madrid")


def test_primary_specialty_adult_vs_child():
    assert primary_specialty_id({"date_of_birth": "1988-03-14"}, today=datetime(2026, 9, 19).date()) == "general_practice"
    assert primary_specialty_id({"date_of_birth": "2017-05-12"}, today=datetime(2026, 9, 19).date()) == "paediatrics"
    assert primary_specialty_id(None) == "general_practice"
    assert primary_specialty_id({}) == "general_practice"


def test_pick_unanimous_habit_gp():
    catalogue = load_catalog()
    past = [
        {"provider_id": "PR01", "start_time": "2026-08-01T10:00:00+02:00"},
        {"provider_id": "PR01", "start_time": "2026-07-01T10:00:00+02:00"},
    ]
    now = datetime(2026, 9, 21, 10, 7, tzinfo=MADRID)  # Monday, Requena on leave
    chosen = pick_primary_care_provider(
        catalogue,
        patient={"date_of_birth": "1980-01-01"},
        past_appointments=past,
        now=now,
    )
    assert chosen is not None
    assert chosen["id"] == "PR01"


def test_pick_most_recent_when_several_gps():
    catalogue = load_catalog()
    past = [
        {"provider_id": "PR01", "start_time": "2026-06-01T10:00:00+02:00"},
        {"provider_id": "PR07", "start_time": "2026-08-15T10:00:00+02:00"},
    ]
    now = datetime(2026, 9, 21, 10, 0, tzinfo=MADRID)
    chosen = pick_primary_care_provider(
        catalogue,
        patient={"date_of_birth": "1980-01-01"},
        past_appointments=past,
        now=now,
    )
    assert chosen is not None
    assert chosen["id"] == "PR07"


def test_requena_on_leave_skipped_for_on_duty():
    catalogue = load_catalog()
    now = datetime(2026, 9, 22, 10, 0, tzinfo=MADRID)  # Tuesday inside leave
    duty = on_duty_providers(catalogue, "general_practice", day=now.date())
    ids = [p["id"] for p in duty]
    assert "PR02" not in ids
    assert "PR01" in ids or "PR07" in ids or "PR03" in ids


def test_on_duty_fallback_when_no_history():
    catalogue = load_catalog()
    now = datetime(2026, 9, 21, 10, 0, tzinfo=MADRID)  # Monday
    chosen = pick_primary_care_provider(
        catalogue,
        patient={"date_of_birth": "1980-01-01"},
        past_appointments=[],
        now=now,
    )
    assert chosen is not None
    assert chosen["id"] != "PR02"  # on leave
    assert chosen["specialty_id"] == "general_practice"


def test_habit_requena_falls_through_while_on_leave():
    catalogue = load_catalog()
    past = [{"provider_id": "PR02", "start_time": "2026-08-01T10:00:00+02:00"}]
    now = datetime(2026, 9, 22, 10, 0, tzinfo=MADRID)
    chosen = pick_primary_care_provider(
        catalogue,
        patient={"date_of_birth": "1980-01-01"},
        past_appointments=past,
        now=now,
    )
    assert chosen is not None
    assert chosen["id"] != "PR02"


def test_next_standing_slot_is_tick_after_current():
    catalogue = load_catalog()
    provider = provider_by_id(catalogue, "PR01")
    now = datetime(2026, 9, 21, 10, 7, tzinfo=MADRID)
    nxt = next_standing_slot(provider, now=now, catalogue=catalogue)
    assert nxt is not None
    start, loc_id, _ = nxt
    assert start.strftime("%H:%M") == "10:15"
    assert loc_id == "centro"


def test_next_standing_slot_rolls_to_next_day():
    catalogue = load_catalog()
    provider = provider_by_id(catalogue, "PR01")
    # After last Monday tick (13:45–14:00)
    now = datetime(2026, 9, 21, 14, 5, tzinfo=MADRID)
    nxt = next_standing_slot(provider, now=now, catalogue=catalogue)
    assert nxt is not None
    start, _, _ = nxt
    assert start.date().isoformat() == "2026-09-22"
    assert start.strftime("%H:%M") == "09:00"


def test_plan_emergency_slot_shape():
    plan = plan_emergency_slot(
        patient={"date_of_birth": "1980-01-01"},
        past_appointments=[],
        now=datetime(2026, 9, 21, 10, 7, tzinfo=MADRID),
    )
    assert plan is not None
    assert plan["provider_id"].startswith("PR")
    assert "T10:15" in plan["slot"] or plan["slot"].endswith("10:15:00+02:00")
    assert plan["slot_minutes"] == 15
