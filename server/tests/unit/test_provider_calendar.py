"""Provider calendar: schedule ticks, free vs booked assembly."""

from __future__ import annotations

from datetime import date

from clinic_catalog import load_catalog
from observability.provider_calendar import (
    assemble_calendar,
    build_day_blocks,
    calendar_window,
    free_start_keys,
    standing_slot_starts,
)
from rules import provider_by_id


def test_calendar_window_aligns_to_monday():
    start, end = calendar_window(anchor=date(2026, 9, 24), days=6)  # Thursday
    assert start == date(2026, 9, 21)  # Monday
    assert end == date(2026, 9, 26)  # Saturday


def test_standing_slots_pr01_monday():
    provider = provider_by_id(load_catalog(), "PR01")
    ticks = standing_slot_starts(
        provider,
        date(2026, 9, 21),
        slot_minutes=15,
        closure_days=frozenset(),
    )
    assert ticks
    assert all(t[1] == "centro" for t in ticks)
    assert ticks[0][0].strftime("%H:%M") == "09:00"
    assert ticks[-1][0].strftime("%H:%M") == "13:45"


def test_leave_suppresses_pr02_slots():
    provider = provider_by_id(load_catalog(), "PR02")
    ticks = standing_slot_starts(
        provider,
        date(2026, 9, 22),  # inside leave window
        slot_minutes=15,
        closure_days=frozenset(),
    )
    assert ticks == []


def test_assemble_marks_free_and_inferred_booked():
    provider = provider_by_id(load_catalog(), "PR01")
    free = [
        {
            "provider_id": "PR01",
            "start_time": "2026-09-21T09:00:00+02:00",
            "duration_minutes": 15,
            "location_id": "centro",
        },
        {
            "provider_id": "PR01",
            "start_time": "2026-09-21T09:15:00+02:00",
            "duration_minutes": 15,
            "location_id": "centro",
        },
    ]
    cal = assemble_calendar(
        provider,
        date_from=date(2026, 9, 21),
        date_to=date(2026, 9, 21),
        free_slots=free,
        bot_bookings=[
            {
                "slot": "2026-09-21T10:00:00+02:00",
                "patient_name": "Ana López",
                "appointment_type_id": "review",
            }
        ],
        source="availability",
    )
    day = cal["days"][0]
    assert any(b["kind"] == "free" and b["start"] == "09:00" for b in day["blocks"])
    bot = next(b for b in day["blocks"] if b.get("source") == "bot")
    assert bot["patient_name"] == "Ana López"
    assert bot["start"] == "10:00"
    assert any(b["kind"] == "booked" and b.get("source") == "inferred" for b in day["blocks"])


def test_cancel_frees_bot_booking_and_slot():
    from observability.provider_calendar import apply_cancellations

    bookings = [
        {
            "slot": "2026-09-21T10:00:00+02:00",
            "patient_name": "Ana López",
            "provider_id": "PR01",
        }
    ]
    cancellations = [
        {
            "slot": "2026-09-21T10:00:00+02:00",
            "appointment_id": "A1",
            "provider_id": "PR01",
        }
    ]
    kept, freed = apply_cancellations(bookings, cancellations)
    assert kept == []
    assert "2026-09-21T10:00" in freed

    provider = provider_by_id(load_catalog(), "PR01")
    cal = assemble_calendar(
        provider,
        date_from=date(2026, 9, 21),
        date_to=date(2026, 9, 21),
        free_slots=[],
        bot_bookings=bookings,
        cancellations=cancellations,
        source="availability",
    )
    # Cancelled tick must not stay as a named bot cita; it is free.
    day = cal["days"][0]
    assert not any(b.get("source") == "bot" for b in day["blocks"])
    assert any(
        b["kind"] == "free" and b["start"] == "10:00"
        for b in day["blocks"]
    )


def test_emergency_overlay_wins_over_free_and_inferred():
    provider = provider_by_id(load_catalog(), "PR01")
    free = [
        {
            "provider_id": "PR01",
            "start_time": "2026-09-21T10:15:00+02:00",
            "duration_minutes": 15,
            "location_id": "centro",
        },
    ]
    cal = assemble_calendar(
        provider,
        date_from=date(2026, 9, 21),
        date_to=date(2026, 9, 21),
        free_slots=free,
        bot_bookings=[],
        overlays=[
            {
                "slot": "2026-09-21T10:15:00+02:00",
                "patient_name": "Luis Ortega",
                "kind": "emergency",
            }
        ],
        source="availability",
    )
    day = cal["days"][0]
    block = next(b for b in day["blocks"] if b["start"] == "10:15")
    assert block["kind"] == "emergency"
    assert block["source"] == "overlay"
    assert "Luis" in block["label"]


def test_without_availability_shows_hours_as_free():
    provider = provider_by_id(load_catalog(), "PR01")
    cal = assemble_calendar(
        provider,
        date_from=date(2026, 9, 21),
        date_to=date(2026, 9, 21),
        free_slots=[],
        bot_bookings=[],
        source="availability_error:ProxyError",
    )
    blocks = cal["days"][0]["blocks"]
    assert blocks
    assert all(b["kind"] == "free" for b in blocks)


def test_free_start_keys_normalise_offsets():
    keys = free_start_keys(
        [
            {"start_time": "2026-09-21T09:00:00+02:00"},
            {"start_time": "2026-09-21T07:00:00+00:00"},
        ]
    )
    assert "2026-09-21T09:00" in keys
    assert len(keys) == 1


def test_build_day_blocks_merges_consecutive():
    provider = provider_by_id(load_catalog(), "PR01")
    blocks = build_day_blocks(
        day=date(2026, 9, 21),
        provider=provider,
        free_starts=set(),
        bot_by_start={},
        slot_minutes=15,
        closure_days=frozenset(),
        infer_busy=True,
    )
    assert blocks
    # Fully booked day collapses to fewer blocks than raw 15-min ticks.
    assert len(blocks) < 20
    assert blocks[0]["start"] == "09:00"
    assert blocks[0]["end"] == "14:00"
