"""Pure slot selection over a real /availability response. No LLM, no network."""

from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
AFTERNOON_STARTS_AT = 14


def search_window(connected_at: datetime) -> tuple[str, str]:
    """Earliest means from the day after the call; the API caps a span at 14 days."""
    date_from = connected_at.astimezone(MADRID).date() + timedelta(days=1)
    return date_from.isoformat(), (date_from + timedelta(days=13)).isoformat()


def _matches(start: datetime, weekday: str | None, part_of_day: str | None) -> bool:
    if weekday and WEEKDAYS[start.weekday()] != weekday:
        return False
    if part_of_day == "morning" and start.hour >= AFTERNOON_STARTS_AT:
        return False
    if part_of_day == "afternoon" and start.hour < AFTERNOON_STARTS_AT:
        return False
    return True


def pick_offer(
    availability: dict,
    patient: dict,
    connected_at: datetime,
    weekday: str | None = None,
    part_of_day: str | None = None,
    closed_days: frozenset[str] = frozenset(),
    provider_id: str | None = None,
) -> dict | None:
    """`closed_days`: ISO dates the clinic is shut. The API still lists slots on them (LIVE-01),
    payable ones when a patient_id is passed, so only the calendar can rule them out."""
    call_day = connected_at.astimezone(MADRID).date()
    load = Counter(slot["provider_id"] for slot in availability["slots"])

    candidates = []
    for slot in availability["slots"]:
        if provider_id and slot["provider_id"] != provider_id:
            continue
        start = datetime.fromisoformat(slot["start_time"]).astimezone(MADRID)
        if start.date() <= call_day or not _matches(start, weekday, part_of_day):
            continue
        if start.date().isoformat() in closed_days:
            continue
        candidates.append((start, load[slot["provider_id"]], slot))
    if not candidates:
        return None

    # Tie on start time -> the provider with MORE free slots (the API lists only free ones),
    # i.e. the least loaded.
    start, _, slot = min(candidates, key=lambda c: (c[0], -c[1]))
    return {
        "patient_id": patient["patient_id"],
        "provider_id": slot["provider_id"],
        "location_id": slot["location_id"],
        "appointment_type_id": slot["appointment_type_id"],
        "slot": start.isoformat(),
        "policy_id": patient["insurer"],
    }
