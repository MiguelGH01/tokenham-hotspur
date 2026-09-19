"""Static clinic catalogue, loaded once from clinic.json (identical for the whole event)."""

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent.parent / "clinic.json"


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def specialty_ids() -> list[str]:
    return [s["id"] for s in load_catalog()["specialties"]]


def location_ids() -> list[str]:
    return [loc["id"] for loc in load_catalog()["locations"]]


def closure_days() -> frozenset[str]:
    return frozenset(load_catalog()["calendar"]["closure_days"])


def providers() -> list[dict]:
    return load_catalog()["providers"]


def provider_on_leave(provider_id: str, day: date) -> bool:
    """The API does not flag leave: `blocked` stays empty and post-leave slots are listed."""
    leave = next(p["leave"] for p in providers() if p["id"] == provider_id)
    return bool(leave) and leave["start"] <= day.isoformat() <= leave["end"]


def location_name(location_id: str) -> str:
    return next(loc["name"] for loc in load_catalog()["locations"] if loc["id"] == location_id)
