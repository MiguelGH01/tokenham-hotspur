"""Static clinic catalogue, loaded once from clinic.json (identical for the whole event)."""

import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent.parent / "clinic.json"


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def specialty_ids() -> list[str]:
    return [s["id"] for s in load_catalog()["specialties"]]


def specialty_name(specialty_id: str) -> str:
    return next(s["name"] for s in load_catalog()["specialties"] if s["id"] == specialty_id)


def location_ids() -> list[str]:
    return [loc["id"] for loc in load_catalog()["locations"]]


def location_name(location_id: str) -> str:
    return next(loc["name"] for loc in load_catalog()["locations"] if loc["id"] == location_id)


def closure_days() -> set[date]:
    """Network-wide shut days (e.g. Fiesta Nacional) — /availability may still list rows for them."""
    return {date.fromisoformat(d) for d in load_catalog()["calendar"]["closure_days"]}


def insurer_ids() -> list[str]:
    return [p["id"] for p in load_catalog()["plans"]]


def provider_names() -> list[str]:
    return [p["name"] for p in load_catalog()["providers"]]


def provider_ids_by_name() -> dict[str, str]:
    return {p["name"]: p["id"] for p in load_catalog()["providers"]}


def provider_name(provider_id: str) -> str:
    return next(p["name"] for p in load_catalog()["providers"] if p["id"] == provider_id)


def provider_roster() -> str:
    """One line per specialty, listing its providers by name — for the LLM to recognise a named doctor."""
    specialty_names = {s["id"]: s["name"] for s in load_catalog()["specialties"]}
    by_specialty: dict[str, list[str]] = {}
    for p in load_catalog()["providers"]:
        by_specialty.setdefault(p["specialty_id"], []).append(p["name"])
    return "; ".join(f"{specialty_names[sid]}: {', '.join(names)}" for sid, names in by_specialty.items())


def provider_on_leave(provider_id: str, at: datetime) -> bool:
    leave = next(p["leave"] for p in load_catalog()["providers"] if p["id"] == provider_id)
    if not leave:
        return False
    return date.fromisoformat(leave["start"]) <= at.date() <= date.fromisoformat(leave["end"])


def provider_refuses_insurer(provider_id: str, insurer: str) -> bool:
    provider = next(p for p in load_catalog()["providers"] if p["id"] == provider_id)
    return insurer in {i["id"] for i in provider["refused_insurers"]}


def age_in_months(date_of_birth: str, at: datetime) -> int:
    dob = date.fromisoformat(date_of_birth)
    at_date = at.date()
    months = (at_date.year - dob.year) * 12 + (at_date.month - dob.month)
    return months - 1 if at_date.day < dob.day else months


def specialty_for_age(age_months: int, exclude: str) -> str | None:
    """The specialty (other than ``exclude``) whose age window covers ``age_months``, if any."""
    for s in load_catalog()["specialties"]:
        if s["id"] == exclude:
            continue
        lo = s["min_age_months"] or 0
        hi = s["max_age_months"]
        if lo <= age_months and (hi is None or age_months <= hi):
            return s["id"]
    return None
