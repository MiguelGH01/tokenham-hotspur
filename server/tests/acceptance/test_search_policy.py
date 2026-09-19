import asyncio
from datetime import datetime, timedelta, timezone

from clinic.search import find_offer

CONNECTED = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=2)))


class Boom:
    async def availability(self, *args, **kwargs):
        raise AssertionError("availability should not be called")


def test_missing_referral_refuses_without_fetch():
    patient = {
        "patient_id": "P00004",
        "insurer": "asisa",
        "referrals": ["physiotherapy"],
        "date_of_birth": "1996-08-14",
    }
    result, offer = asyncio.run(find_offer(Boom(), patient, CONNECTED, specialty="dermatology"))
    assert offer is None
    assert result == {"status": "refused", "reason": "referral_required", "specialty": "dermatology"}


def test_uncovered_specialty_refuses_without_fetch():
    patient = {
        "patient_id": "P00015",
        "insurer": "adeslas",
        "referrals": [],
        "date_of_birth": "1967-03-20",
    }
    result, offer = asyncio.run(find_offer(Boom(), patient, CONNECTED, specialty="gynaecology"))
    assert offer is None
    assert result["reason"] == "specialty_not_covered"


def test_unknown_doctor_asks_before_refusing():
    patient = {"patient_id": "P018xx", "insurer": "asisa", "date_of_birth": "1980-01-01"}
    result, offer = asyncio.run(
        find_offer(
            Boom(),
            patient,
            CONNECTED,
            specialty="orthopaedics",
            provider_spoken="Dr. Fuentes",
        )
    )
    assert result["status"] == "provider_missing"
    refused, _ = asyncio.run(
        find_offer(
            Boom(),
            patient,
            CONNECTED,
            specialty="orthopaedics",
            provider_spoken="Dr. Fuentes",
            others_ok=False,
        )
    )
    assert refused == {"status": "refused", "reason": "provider_not_found"}


class FakeAvail:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def availability(self, date_from=None, date_to=None, *args, **kwargs):
        self.calls.append({"date_from": date_from, "date_to": date_to, **kwargs})
        return self.payload


def _slot(provider, location, kind, start, name):
    return {
        "provider_id": provider,
        "provider_name": name,
        "specialty_id": "dermatology",
        "location_id": location,
        "appointment_type_id": kind,
        "start_time": start,
        "duration_minutes": 15,
        "payable_with": ["dkv"],
    }


def test_named_provider_refusing_plan_redirects():
    client = FakeAvail(
        {
            "providers": [],
            "appointment_type": {},
            "blocked": [],
            "slots": [
                _slot("PR12", "norte", "dermatology_review", "2026-09-21T10:15:00+02:00", "Dr. Tomás Vilar"),
            ],
        }
    )
    patient = {
        "patient_id": "P00057",
        "insurer": "dkv",
        "referrals": ["dermatology"],
        "date_of_birth": "1980-01-01",
        "has_visited_before": True,
    }
    result, offer = asyncio.run(
        find_offer(
            client,
            patient,
            CONNECTED,
            specialty="dermatology",
            provider_spoken="Dra. Iglesias",
        )
    )
    assert result["status"] == "offer"
    assert offer["provider_id"] == "PR12"
    assert client.calls[0]["provider_id"] is None


def test_leave_keeps_site_drops_provider():
    client = FakeAvail(
        {
            "providers": [],
            "appointment_type": {},
            "blocked": [],
            "slots": [
                {
                    "provider_id": "PR07",
                    "provider_name": "Dra. Laura Benítez Roca",
                    "specialty_id": "general_practice",
                    "location_id": "norte",
                    "appointment_type_id": "review",
                    "start_time": "2026-09-22T09:00:00+02:00",
                    "duration_minutes": 15,
                    "payable_with": ["dkv"],
                }
            ],
        }
    )
    patient = {
        "patient_id": "P01843",
        "insurer": "dkv",
        "referrals": [],
        "date_of_birth": "1980-01-01",
        "has_visited_before": True,
    }
    result, offer = asyncio.run(
        find_offer(
            client,
            patient,
            CONNECTED,
            specialty="general_practice",
            site="norte",
            provider_spoken="Dr. Requena",
        )
    )
    assert offer["provider_id"] == "PR07"
    assert offer["location_id"] == "norte"
    assert client.calls[0]["provider_id"] is None
    assert client.calls[0]["location_id"] == "norte"


def test_named_october_day_slides_availability_window():
    client = FakeAvail({"providers": [], "appointment_type": {}, "blocked": [], "slots": []})
    patient = {
        "patient_id": "P00012",
        "insurer": "sanitas",
        "referrals": [],
        "date_of_birth": "1990-01-01",
        "has_visited_before": False,
    }
    asyncio.run(
        find_offer(
            client,
            patient,
            CONNECTED,
            specialty="general_practice",
            site="centro",
            when_text="first thing on Monday the twelfth of October",
        )
    )
    assert client.calls[0]["date_from"] == "2026-10-13"
    assert client.calls[0]["date_to"] >= "2026-10-13"
