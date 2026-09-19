"""Catalogue-driven slot search: named doctor, when, leave, coverage. No Flow nodes."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from booking import MADRID, pick_offer, search_window
from clinic.clinic_catalog import (
    age_months,
    holds_referral,
    load_catalog,
    location_ids,
    match_providers,
    next_open_day,
    on_leave_on,
    plan_covers_location,
    plan_covers_specialty,
    provider_refuses_plan,
    remap_specialty,
    specialty_age_ok,
    specialty_by_id,
    specialty_ids,
)
from dates import parse_when


def _calendar_end() -> date:
    return date.fromisoformat(load_catalog()["calendar"]["ends"])


def _span_days() -> int:
    return int(load_catalog()["calendar"]["max_span_days"]) - 1


def _window_dates(connected_at: datetime, around: date | None = None) -> tuple[date, date]:
    """14-day span the API accepts. Slide it forward when the caller named a later day."""
    earliest = date.fromisoformat(search_window(connected_at)[0])
    ends = _calendar_end()
    start = around if around and around > earliest else earliest
    if start > ends:
        start = ends
    return start, min(start + timedelta(days=_span_days()), ends)


def _reason_from_blocked(blocked: list, provider_id: str | None = None) -> str | None:
    if not blocked:
        return None
    if provider_id:
        named = [row["restriction"] for row in blocked if row.get("provider_id") == provider_id]
        if named:
            return named[0]
    reasons = {row["restriction"] for row in blocked if row.get("restriction")}
    if len(reasons) == 1:
        return next(iter(reasons))
    return None


def _policy_refuse(patient: dict, specialty_id: str, site: str | None, connected_at: datetime) -> str | None:
    spec = specialty_by_id(specialty_id)
    if spec.get("referral_required") and not holds_referral(patient, specialty_id):
        return "referral_required"
    plan = patient.get("insurer")
    if plan and not plan_covers_specialty(plan, specialty_id):
        return "specialty_not_covered"
    if plan and site and not plan_covers_location(plan, site):
        return "location_not_covered"
    dob = patient.get("date_of_birth")
    if dob and not specialty_age_ok(specialty_id, age_months(dob, connected_at)):
        return "not_eligible_age"
    return None


async def find_offer(
    client,
    patient: dict,
    connected_at: datetime,
    *,
    specialty: str,
    site: str | None = None,
    provider_spoken: str | None = None,
    when_text: str | None = None,
    weekday: str | None = None,
    part_of_day: str | None = None,
    others_ok: bool = True,
) -> tuple[dict, dict | None]:
    if specialty not in specialty_ids() or (site and site not in location_ids()):
        return {"status": "invalid", "specialties": specialty_ids(), "sites": location_ids()}, None

    specialty = remap_specialty(specialty, patient, connected_at)
    when = parse_when(when_text, connected_at)
    target = when.target_date
    # A named calendar day in what the caller said beats a weekday the model guessed.
    if target:
        weekday = None
    else:
        weekday = weekday or when.weekday
    part_of_day = part_of_day or when.part_of_day
    if when.first_thing and not part_of_day:
        part_of_day = "morning"

    named = match_providers(provider_spoken, specialty) if provider_spoken else []
    if provider_spoken and not named:
        named = match_providers(provider_spoken)
        if len(named) == 1:
            specialty = remap_specialty(named[0]["specialty_id"], patient, connected_at)
    if provider_spoken and not named:
        if others_ok is False:
            return {"status": "refused", "reason": "provider_not_found"}, None
        return {"status": "provider_missing", "ask_others": True}, None
    if len(named) > 1:
        return {"status": "ambiguous_provider", "candidates": [p["name"] for p in named]}, None

    provider = named[0] if named else None
    keep_provider_site = bool(provider and site)
    plan = patient.get("insurer")

    refuse = _policy_refuse(patient, specialty, site, connected_at)
    if refuse:
        return {"status": "refused", "reason": refuse, "specialty": specialty}, None

    if provider and plan and provider_refuses_plan(provider, plan):
        provider = None
        keep_provider_site = False
        site = None

    earliest = date.fromisoformat(search_window(connected_at)[0])
    if provider and on_leave_on(provider, earliest):
        if not site:
            site = provider["schedules"][0]["location_id"]
        provider = None
        keep_provider_site = False

    if target:
        rolled = next_open_day(target, site, min(_calendar_end(), target + timedelta(days=_span_days())))
        if rolled is None or rolled < earliest:
            return {"status": "no_slots", "blocked": []}, None
        if rolled != target:
            weekday = None
        target = rolled

    date_from, date_to = _window_dates(connected_at, around=target)

    try:
        availability = await client.availability(
            date_from.isoformat(),
            date_to.isoformat(),
            specialty,
            patient["patient_id"],
            location_id=site,
            provider_id=provider["id"] if provider else None,
        )
    except Exception as exc:
        return {"status": "lookup_failed", "error": str(exc)}, None

    blocked = availability.get("blocked") or []
    offer = pick_offer(
        availability,
        patient,
        connected_at,
        weekday,
        part_of_day,
        provider_ids=[provider["id"]] if provider else None,
        location_id=site,
        target_date=target,
    )

    if offer is None and keep_provider_site:
        offer = pick_offer(
            availability,
            patient,
            connected_at,
            weekday=None,
            part_of_day=None,
            provider_ids=[provider["id"]] if provider else None,
            location_id=site,
            target_date=None,
        )

    if offer is None:
        reason = _reason_from_blocked(blocked, provider["id"] if provider else None)
        if reason:
            return {
                "status": "refused",
                "reason": reason,
                "blocked": blocked,
                "specialty": specialty,
            }, None
        return {"status": "no_slots", "blocked": blocked, "specialty": specialty}, None

    slot = next(
        s
        for s in availability["slots"]
        if s["provider_id"] == offer["provider_id"]
        and datetime.fromisoformat(s["start_time"]).astimezone(MADRID).isoformat() == offer["slot"]
    )
    return {"status": "offer", "specialty": specialty, "blocked": blocked, "slot_meta": slot}, offer
