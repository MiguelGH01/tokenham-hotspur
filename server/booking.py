"""Pure slot selection over a real /availability response. No LLM, no network."""

from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from clinic_catalog import closure_days

MADRID = ZoneInfo("Europe/Madrid")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
AFTERNOON_STARTS_AT = 14


def search_window(connected_at: datetime) -> tuple[str, str]:
    """Earliest means from the day after the call; the API caps a span at 14 days."""
    date_from = connected_at.astimezone(MADRID).date() + timedelta(days=1)
    return date_from.isoformat(), (date_from + timedelta(days=13)).isoformat()


def _matches_part_of_day(start: datetime, part_of_day: str | None) -> bool:
    if part_of_day == "morning" and start.hour >= AFTERNOON_STARTS_AT:
        return False
    if part_of_day == "afternoon" and start.hour < AFTERNOON_STARTS_AT:
        return False
    return True


def _next_occurrence(earliest: date, weekday: str) -> date:
    days_ahead = (WEEKDAYS.index(weekday) - earliest.weekday()) % 7
    return earliest + timedelta(days=days_ahead)


def pick_offer(
    availability: dict,
    patient: dict,
    connected_at: datetime,
    weekday: str | None = None,
    part_of_day: str | None = None,
    on_or_after: date | None = None,
) -> tuple[dict | None, str | None]:
    """Earliest slot the patient's plan actually pays for.

    Filtering on the patient's own insurer prices correctly and drops any row a plan
    doesn't cover. Rows on a network-wide closure day (a Fiesta) are dropped outright —
    the live API may still list them. A cyclic ``weekday`` is resolved to its next
    calendar occurrence up front, then rolls forward exactly like an explicit
    ``on_or_after`` date if that day turns out closed or fully booked — never backward to
    an earlier open day, which would answer a different day than the one asked for.
    "relaxed" reports that the target day couldn't be honoured and rolled to the next one.
    """
    call_day = connected_at.astimezone(MADRID).date()
    shut_days = closure_days()
    load = Counter(slot["provider_id"] for slot in availability["slots"])

    target_date = on_or_after
    if weekday and not target_date:
        target_date = _next_occurrence(call_day + timedelta(days=1), weekday)

    found = []
    for slot in availability["slots"]:
        if patient["insurer"] not in slot.get("payable_with", []):
            continue
        start = datetime.fromisoformat(slot["start_time"]).astimezone(MADRID)
        if start.date() <= call_day or start.date() in shut_days:
            continue
        if target_date and start.date() < target_date:
            continue
        if not _matches_part_of_day(start, part_of_day):
            continue
        found.append((start, load[slot["provider_id"]], slot))
    if not found:
        return None, None

    start, _, slot = min(found, key=lambda c: (c[0], c[1]))
    relaxed = None
    if target_date and start.date() != target_date:
        relaxed = "weekday" if weekday else "date"
    offer = {
        "patient_id": patient["patient_id"],
        "provider_id": slot["provider_id"],
        "location_id": slot["location_id"],
        "appointment_type_id": slot["appointment_type_id"],
        "slot": start.isoformat(),
        "policy_id": patient["insurer"],
    }
    return offer, relaxed
