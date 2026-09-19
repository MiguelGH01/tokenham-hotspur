"""Static clinic catalogue, loaded once from clinic.json (identical for the whole event)."""

import json
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent / "clinic.json"


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


def provider_names() -> list[str]:
    return [p["name"] for p in load_catalog()["providers"]]


def location_name(location_id: str) -> str:
    return next(loc["name"] for loc in load_catalog()["locations"] if loc["id"] == location_id)
