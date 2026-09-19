"""Static clinic catalogue, loaded once from clinic.json (identical for the whole event)."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_HERE = Path(__file__).resolve()
CATALOG_PATH = next(
    p for p in (_HERE.parents[2] / "clinic.json", _HERE.parents[1] / "clinic.json") if p.exists()
)

_TITLE = re.compile(r"\b(dra?|doctora|doctor|don|doña|dona|d)\.?\b", re.IGNORECASE)
GENERAL_COMPLAINT = frozenset({"general_practice", "paediatrics"})


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def fold(text: str) -> str:
    stripped = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in stripped if unicodedata.category(ch) != "Mn").lower()


def specialty_ids() -> list[str]:
    return [s["id"] for s in load_catalog()["specialties"]]


def location_ids() -> list[str]:
    return [loc["id"] for loc in load_catalog()["locations"]]


def location_name(location_id: str) -> str:
    return next(loc["name"] for loc in load_catalog()["locations"] if loc["id"] == location_id)


def specialty_by_id(specialty_id: str) -> dict:
    return next(s for s in load_catalog()["specialties"] if s["id"] == specialty_id)


def location_by_id(location_id: str) -> dict:
    return next(loc for loc in load_catalog()["locations"] if loc["id"] == location_id)


def provider_by_id(provider_id: str) -> dict:
    return next(p for p in load_catalog()["providers"] if p["id"] == provider_id)


def closure_days() -> set[str]:
    return set(load_catalog()["calendar"]["closure_days"])


def _name_tokens(spoken: str) -> list[str]:
    cleaned = _TITLE.sub(" ", fold(spoken))
    return [tok for tok in re.split(r"[^a-z]+", cleaned) if len(tok) > 1]


def match_providers(spoken: str, specialty_id: str | None = None) -> list[dict]:
    """Surname/token match against catalogue names. Exact folded tokens, not prefixes."""
    wanted = _name_tokens(spoken)
    if not wanted:
        return []
    hits = []
    for provider in load_catalog()["providers"]:
        have = _name_tokens(provider["name"])
        if all(tok in have for tok in wanted):
            hits.append(provider)
    if specialty_id:
        in_specialty = [p for p in hits if p["specialty_id"] == specialty_id]
        if in_specialty:
            return in_specialty
    return hits


def match_plan(spoken: str) -> str | None:
    wanted = fold(spoken).strip()
    if not wanted:
        return None
    plans = load_catalog()["plans"]
    for plan in plans:
        if fold(plan["id"]) == wanted or fold(plan["name"]) == wanted:
            return plan["id"]
    unique = [p for p in plans if wanted in fold(p["name"]) or fold(p["id"]) in wanted]
    if len(unique) == 1:
        return unique[0]["id"]
    return None


def age_months(date_of_birth: str, at: datetime) -> int:
    born = date.fromisoformat(date_of_birth)
    today = at.astimezone(MADRID).date()
    months = (today.year - born.year) * 12 + (today.month - born.month)
    if today.day < born.day:
        months -= 1
    return months


def specialty_age_ok(specialty_id: str, months: int) -> bool:
    spec = specialty_by_id(specialty_id)
    lo, hi = spec.get("min_age_months"), spec.get("max_age_months")
    if lo is not None and months < lo:
        return False
    if hi is not None and months > hi:
        return False
    return True


def remap_specialty(specialty_id: str, patient: dict, connected_at: datetime) -> str:
    """GP ↔ paediatrics for a general complaint. Named specialties stay as spoken."""
    dob = patient.get("date_of_birth")
    if not dob or specialty_id not in GENERAL_COMPLAINT:
        return specialty_id
    months = age_months(dob, connected_at)
    if specialty_age_ok(specialty_id, months):
        return specialty_id
    other = "paediatrics" if specialty_id == "general_practice" else "general_practice"
    return other if specialty_age_ok(other, months) else specialty_id


def plan_covers_specialty(plan_id: str, specialty_id: str) -> bool:
    spec = specialty_by_id(specialty_id)
    return plan_id not in {p["id"] for p in spec.get("not_covered_by") or []}


def plan_covers_location(plan_id: str, location_id: str) -> bool:
    loc = location_by_id(location_id)
    return plan_id not in {p["id"] for p in loc.get("not_covered_by") or []}


def provider_refuses_plan(provider: dict, plan_id: str) -> bool:
    return plan_id in {p["id"] for p in provider.get("refused_insurers") or []}


def holds_referral(patient: dict, specialty_id: str) -> bool:
    return specialty_id in (patient.get("referrals") or [])


def on_leave_on(provider: dict, day: date) -> bool:
    leave = provider.get("leave")
    if not leave:
        return False
    return date.fromisoformat(leave["start"]) <= day <= date.fromisoformat(leave["end"])


def is_closed_day(day: date, location_id: str | None = None) -> bool:
    if day.isoformat() in closure_days():
        return True
    if location_id:
        loc = location_by_id(location_id)
        weekday = WEEKDAYS[day.weekday()]
        hours = next((row for row in loc["hours"] if row["weekday"] == weekday), None)
        return hours is None or not hours.get("intervals")
    return all(is_closed_day(day, loc["id"]) for loc in load_catalog()["locations"])


def next_open_day(day: date, location_id: str | None, until: date) -> date | None:
    cursor = day
    while cursor <= until:
        if not is_closed_day(cursor, location_id):
            return cursor
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return None
