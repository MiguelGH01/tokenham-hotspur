"""The catalogue comes from the live API at start-up; clinic.json is only the fallback."""

import pytest

from clinic import clinic_catalog
from clinic.clinic_catalog import load_catalog, provider_on_leave, refresh_from_api


@pytest.fixture(autouse=True)
def _back_to_the_snapshot():
    yield
    clinic_catalog._live = None


def test_without_a_refresh_the_snapshot_on_disk_is_used():
    assert load_catalog()["clinic_name"] and "PR02" in [p["id"] for p in load_catalog()["providers"]]


def test_a_live_catalogue_replaces_the_snapshot():
    # The organisers end Requena's leave: only the live catalogue knows.
    live = {**load_catalog(), "providers": [{**p, "leave": None} for p in load_catalog()["providers"]]}
    day = __import__("datetime").date(2026, 9, 18)
    assert provider_on_leave("PR02", day) is True

    assert refresh_from_api(fetch=lambda: live) is True

    assert provider_on_leave("PR02", day) is False


@pytest.mark.parametrize("broken", [RuntimeError("network down"), {"clinic_name": "half a catalogue"}])
def test_an_unreachable_or_incomplete_api_keeps_the_snapshot(broken):
    def fetch():
        if isinstance(broken, Exception):
            raise broken
        return broken

    assert refresh_from_api(fetch=fetch) is False
    assert "PR02" in [p["id"] for p in load_catalog()["providers"]]
