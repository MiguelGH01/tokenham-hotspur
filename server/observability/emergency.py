"""Local emergency overlay for the doctor console.

Prosper scoring wants ``ESCALATE(medical_emergency)`` and no BOOK. The console
still needs a visible slot for the GP who will receive the patient, so we pick
a primary-care provider and reserve the *next* standing tick after now — local
overlay only, never ``/v1/submit/book``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from clinic_catalog import load_catalog
from observability.provider_calendar import MADRID, standing_slot_starts
from rules import age_in_months, provider_by_id

#: 14th birthday in complete months — same boundary as ``CL-age-boundary``.
GP_MIN_MONTHS = 168

#: How far ahead we look for the next standing tick / an on-duty GP.
_SEARCH_DAYS = 21


def primary_specialty_id(
    patient: dict[str, Any] | None,
    *,
    today: date | None = None,
) -> str:
    """GP for adults; paediatrics under 14. Unknown DOB → GP."""
    today = today or datetime.now(MADRID).date()
    if not patient:
        return "general_practice"
    born = patient.get("date_of_birth")
    if not born:
        return "general_practice"
    try:
        months = age_in_months(date.fromisoformat(str(born)[:10]), today)
    except ValueError:
        return "general_practice"
    if months < GP_MIN_MONTHS:
        return "paediatrics"
    return "general_practice"


def _provider_on_leave(provider: dict, day: date) -> bool:
    leave = provider.get("leave")
    if not leave:
        return False
    try:
        leave_start = date.fromisoformat(leave["start"])
        leave_end = date.fromisoformat(leave["end"])
    except (KeyError, TypeError, ValueError):
        return False
    return leave_start <= day <= leave_end


def _has_standing_hours(
    provider: dict,
    day: date,
    *,
    slot_minutes: int,
    closure_days: frozenset[str],
) -> bool:
    return bool(
        standing_slot_starts(
            provider, day, slot_minutes=slot_minutes, closure_days=closure_days
        )
    )


def on_duty_providers(
    catalogue: dict,
    specialty_id: str,
    *,
    day: date,
    slot_minutes: int | None = None,
    closure_days: frozenset[str] | None = None,
) -> list[dict]:
    """Providers of ``specialty_id`` with standing hours on ``day`` (not on leave)."""
    slot_minutes = slot_minutes or int(catalogue["calendar"]["slot_minutes"])
    if closure_days is None:
        closure_days = frozenset(catalogue["calendar"]["closure_days"])
    out: list[dict] = []
    for provider in catalogue["providers"]:
        if provider.get("specialty_id") != specialty_id:
            continue
        if _provider_on_leave(provider, day):
            continue
        if _has_standing_hours(
            provider, day, slot_minutes=slot_minutes, closure_days=closure_days
        ):
            out.append(provider)
    out.sort(key=lambda p: p["id"])
    return out


def pick_primary_care_provider(
    catalogue: dict | None = None,
    *,
    patient: dict[str, Any] | None = None,
    past_appointments: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Médico de cabecera: habit from past visits, else first on-duty of specialty."""
    catalogue = catalogue or load_catalog()
    now = (now or datetime.now(MADRID)).astimezone(MADRID)
    specialty_id = primary_specialty_id(patient, today=now.date())
    slot_minutes = int(catalogue["calendar"]["slot_minutes"])
    closure_days = frozenset(catalogue["calendar"]["closure_days"])

    past = past_appointments or []
    matched: list[tuple[dict[str, Any], dict]] = []
    for row in past:
        provider = provider_by_id(catalogue, row.get("provider_id"))
        if provider is None:
            continue
        if provider.get("specialty_id") != specialty_id:
            continue
        matched.append((row, provider))

    chosen: dict | None = None
    if matched:
        provider_ids = {p["id"] for _, p in matched}
        if len(provider_ids) == 1:
            chosen = matched[0][1]
        else:
            matched.sort(key=lambda pair: str(pair[0].get("start_time") or ""), reverse=True)
            chosen = matched[0][1]

    if chosen is not None:
        # Habit doctor on leave *today* → on-duty fallback (transfer cannot wait).
        if not _provider_on_leave(chosen, now.date()):
            for offset in range(_SEARCH_DAYS):
                day = now.date() + timedelta(days=offset)
                if _provider_on_leave(chosen, day):
                    continue
                if _has_standing_hours(
                    chosen, day, slot_minutes=slot_minutes, closure_days=closure_days
                ):
                    return chosen

    for offset in range(_SEARCH_DAYS):
        day = now.date() + timedelta(days=offset)
        duty = on_duty_providers(
            catalogue,
            specialty_id,
            day=day,
            slot_minutes=slot_minutes,
            closure_days=closure_days,
        )
        if duty:
            return duty[0]
    return None


def current_tick_start(now: datetime, slot_minutes: int) -> datetime:
    """Floor ``now`` to the tick that contains it (Europe/Madrid)."""
    local = now.astimezone(MADRID)
    minute = (local.minute // slot_minutes) * slot_minutes
    return local.replace(minute=minute, second=0, microsecond=0)


def next_standing_slot(
    provider: dict,
    *,
    now: datetime | None = None,
    catalogue: dict | None = None,
) -> tuple[datetime, str, str] | None:
    """The standing tick *after* the one that contains ``now`` (transfer lag)."""
    catalogue = catalogue or load_catalog()
    now = (now or datetime.now(MADRID)).astimezone(MADRID)
    slot_minutes = int(catalogue["calendar"]["slot_minutes"])
    closure_days = frozenset(catalogue["calendar"]["closure_days"])
    current = current_tick_start(now, slot_minutes)

    for offset in range(_SEARCH_DAYS):
        day = now.date() + timedelta(days=offset)
        for start, loc_id, loc_name in standing_slot_starts(
            provider, day, slot_minutes=slot_minutes, closure_days=closure_days
        ):
            if start > current:
                return start, loc_id, loc_name
    return None


def plan_emergency_slot(
    *,
    patient: dict[str, Any] | None = None,
    past_appointments: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    catalogue: dict | None = None,
) -> dict[str, Any] | None:
    """Pick GP + next tick. Returns overlay fields or ``None`` if nobody is free."""
    catalogue = catalogue or load_catalog()
    now = (now or datetime.now(MADRID)).astimezone(MADRID)
    provider = pick_primary_care_provider(
        catalogue,
        patient=patient,
        past_appointments=past_appointments,
        now=now,
    )
    if provider is None:
        return None
    nxt = next_standing_slot(provider, now=now, catalogue=catalogue)
    if nxt is None:
        return None
    start, loc_id, loc_name = nxt
    slot_minutes = int(catalogue["calendar"]["slot_minutes"])
    end = start + timedelta(minutes=slot_minutes)
    return {
        "provider_id": provider["id"],
        "provider_name": provider.get("name"),
        "location_id": loc_id,
        "location_name": loc_name,
        "slot": start.isoformat(),
        "end": end.isoformat(),
        "slot_minutes": slot_minutes,
        "specialty_id": provider.get("specialty_id"),
    }


async def fetch_past_appointments(patient_id: str | None) -> list[dict[str, Any]]:
    """Best-effort past diary; empty on missing id or clinic errors."""
    if not patient_id:
        return []
    try:
        from clients.clinic_client import ClinicClient

        return await ClinicClient().appointments(patient_id, when="past")
    except Exception:
        return []


async def plan_emergency(
    *,
    patient_id: str | None = None,
    patient_name: str | None = None,
    date_of_birth: str | None = None,
    past_appointments: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    catalogue: dict | None = None,
) -> dict[str, Any] | None:
    """Resolve cabecera + next slot, optionally loading past visits from the clinic."""
    patient: dict[str, Any] = {}
    if patient_id:
        patient["patient_id"] = patient_id
    if patient_name:
        patient["patient_name"] = patient_name
    if date_of_birth:
        patient["date_of_birth"] = date_of_birth

    past = past_appointments
    if past is None:
        past = await fetch_past_appointments(patient_id)

    plan = plan_emergency_slot(
        patient=patient or None,
        past_appointments=past,
        now=now,
        catalogue=catalogue,
    )
    if plan is None:
        return None
    plan["patient_id"] = patient_id
    plan["patient_name"] = patient_name
    return plan
