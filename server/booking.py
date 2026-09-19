"""Pure slot selection over a real /availability response. No LLM, no network."""

from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
AFTERNOON_STARTS_AT = 14


def search_window(connected_at: datetime) -> tuple[str, str]:
    """Earliest means from the day after the call; the API caps a span at 14 days."""
    date_from = connected_at.astimezone(MADRID).date() + timedelta(days=1)
    return date_from.isoformat(), (date_from + timedelta(days=13)).isoformat()


def _matches(
    start: datetime,
    weekday: str | None,
    part_of_day: str | None,
    target_date: date | None,
) -> bool:
    if target_date and start.date() != target_date:
        return False
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
    *,
    provider_ids: list[str] | None = None,
    location_id: str | None = None,
    target_date: date | None = None,
    skip_closed: bool = True,
) -> dict | None:
    from clinic.clinic_catalog import is_closed_day

    call_day = connected_at.astimezone(MADRID).date()
    load = Counter(slot["provider_id"] for slot in availability["slots"])
    allowed = set(provider_ids) if provider_ids else None

    candidates = []
    for slot in availability["slots"]:
        if allowed and slot["provider_id"] not in allowed:
            continue
        if location_id and slot["location_id"] != location_id:
            continue
        if not slot.get("payable_with"):
            continue
        start = datetime.fromisoformat(slot["start_time"]).astimezone(MADRID)
        if start.date() <= call_day or not _matches(start, weekday, part_of_day, target_date):
            continue
        if skip_closed and is_closed_day(start.date(), slot["location_id"]):
            continue
        candidates.append((start, load[slot["provider_id"]], slot))
    if not candidates:
        return None

    start, _, slot = min(candidates, key=lambda c: (c[0], c[1]))
    return {
        "patient_id": patient["patient_id"],
        "provider_id": slot["provider_id"],
        "location_id": slot["location_id"],
        "appointment_type_id": slot["appointment_type_id"],
        "slot": start.isoformat(),
        "policy_id": patient["insurer"],
    }
