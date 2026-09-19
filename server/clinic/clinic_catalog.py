"""Clinic catalogue: doctors, sites, specialties, plans, closures, the wording of each rule.

The bot process fetches it from ``GET /v1/clinic`` once at start-up (``refresh_from_api``).
clinic.json is a snapshot of that same response, used when the API cannot be reached and by
the tests, which never touch the network.
"""

import json
import os
from collections.abc import Callable
from datetime import date
from functools import lru_cache
from pathlib import Path

import httpx
from loguru import logger

CATALOG_PATH = Path(__file__).resolve().parent.parent.parent / "clinic.json"
_live: dict | None = None


@lru_cache(maxsize=1)
def _snapshot() -> dict:
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_catalog() -> dict:
    return _live or _snapshot()


def _fetch_live() -> dict:
    response = httpx.get(
        os.environ["CLINIC_API_BASE_URL"].rstrip("/") + "/v1/clinic",
        headers={"X-Api-Key": os.environ["CLINIC_API_KEY"]},
        # Measured 2s to 15s. Generous on purpose: this runs once at boot, never during a call.
        timeout=20.0,
    )
    response.raise_for_status()
    return response.json()


def refresh_from_api(fetch: Callable[[], dict] = _fetch_live) -> bool:
    """Swap the snapshot for the live catalogue. Call once, before any call is answered."""
    global _live
    try:
        catalog = fetch()
        missing = set(_snapshot()) - set(catalog)
        if missing:
            raise ValueError(f"live catalogue lacks {sorted(missing)}")
    except Exception as exc:
        logger.warning("Clinic catalogue: API unavailable ({}), using the clinic.json snapshot", exc)
        return False
    _live = catalog
    logger.info("Clinic catalogue loaded from the API ({} providers)", len(catalog["providers"]))
    return True


def specialty_ids() -> list[str]:
    return [s["id"] for s in load_catalog()["specialties"]]


def general_care_specialties() -> list[str]:
    """Where a general complaint goes: every plan covers them and none needs a referral. Their
    age windows partition all ages, so exactly one fits any patient (CL-age-boundary)."""
    return [
        s["id"]
        for s in load_catalog()["specialties"]
        if not s["referral_required"] and not s["not_covered_by"]
    ]


def restriction_explanation(restriction_id: str) -> str:
    """The clinic's own wording for a rule, so the model explains it instead of inventing one."""
    return next(
        (r["explanation"] for r in load_catalog()["restrictions"] if r["id"] == restriction_id), ""
    )


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
