"""Pins the reception notices contract: a JSON file the receptionist edits changes what the next call offers and says."""

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

import reception_notices
from clinic_catalog import load_base_catalog, load_catalog
from dates import closure_days
from flows.common import ROLE_MESSAGE, role_message
from rules import check_provider_rules
from submission import CallSubmission

CALL_ID = "call-notices"
PROVIDER_NAMES = {
    "PR01": "Dra. Carmen Ortiz Vidal",
    "PR03": "Dr. Martín Sáez",
    "PR07": "Dra. Laura Benítez Roca",
}
ABSENCE = {
    "id": "n1",
    "kind": "provider_absent",
    "provider_id": "PR03",
    "from": "2026-09-22",
    "until": "2026-09-24",
}
LIFT = {
    "id": "n4",
    "kind": "spoken",
    "text": "El ascensor está averiado.",
    "location_id": "centro",
    "from": "2026-09-19",
    "until": "2026-09-24",
}


def _write(tmp_path, monkeypatch, content):
    path = tmp_path / "notices.json"
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("RECEPTION_NOTICES_PATH", str(path))
    return path


def _slot(provider_id, day, location_id="centro"):
    return dict(
        provider_id=provider_id,
        provider_name=PROVIDER_NAMES[provider_id],
        location_id=location_id,
        appointment_type_id="review",
        start_time=f"{day}T09:00:00+02:00",
        payable_with=["sanitas"],
    )


def _manager(slots):
    class Client:
        async def availability(self, *args, **kwargs):
            return {"slots": [dict(s) for s in slots], "blocked": []}

    c = Client()
    return SimpleNamespace(
        state={
            "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
            "client": c,
            "submission": CallSubmission("x", c),
            "patient": {"patient_id": "P", "insurer": "sanitas"},
            "offers": {},
            "call_id": CALL_ID,
        }
    )


def _events(audit_dir, event):
    lines = (audit_dir / f"audit-{CALL_ID}.ndjson").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == event]


def _starts(availability):
    return [(s["provider_id"], s["start_time"][:10]) for s in availability["slots"]]


def test_no_file_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEPTION_NOTICES_PATH", str(tmp_path / "missing.json"))
    availability = {"slots": [_slot("PR03", "2026-09-23"), _slot("PR07", "2026-09-23")], "blocked": []}

    assert load_catalog() == load_base_catalog()
    shown, ids = reception_notices.hide_absent(availability, reception_notices.load_notices())
    assert shown["slots"] == availability["slots"]
    assert ids == []


def test_absent_provider_hides_only_their_slots_on_those_days(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"tone": None, "notices": [ABSENCE]})
    availability = {
        "slots": [
            _slot("PR03", "2026-09-22"),
            _slot("PR03", "2026-09-23"),
            _slot("PR03", "2026-09-24"),
            _slot("PR03", "2026-09-25"),
            _slot("PR07", "2026-09-23"),
        ],
        "blocked": [],
    }
    before = json.loads(json.dumps(availability))

    shown, ids = reception_notices.hide_absent(availability, reception_notices.load_notices())

    assert sorted(_starts(shown)) == [("PR03", "2026-09-25"), ("PR07", "2026-09-23")]
    assert ids == ["n1"]
    assert availability == before


def test_closed_day_is_added_without_mutating_base(tmp_path, monkeypatch):
    closed = {"id": "n2", "kind": "clinic_closed", "from": "2026-09-25", "until": "2026-09-25"}
    _write(tmp_path, monkeypatch, {"tone": None, "notices": [closed]})

    days = closure_days()

    assert "2026-09-25" in days
    assert "2026-10-12" in days
    assert load_base_catalog()["calendar"]["closure_days"] == ["2026-10-12"]


def test_dropped_insurer_bites_only_while_active(tmp_path, monkeypatch):
    # insurer_dropped is judged against the real clock, so the dates are built around it.
    today = datetime.now(ZoneInfo("Europe/Madrid")).date()

    def dropped(start, until):
        return {
            "tone": None,
            "notices": [
                {
                    "id": "n3",
                    "kind": "insurer_dropped",
                    "provider_id": "PR07",
                    "insurer_id": "dkv",
                    "from": start.isoformat(),
                    "until": until.isoformat(),
                }
            ],
        }

    def verdict():
        plan = next(p for p in load_catalog()["plans"] if p["id"] == "dkv")
        return check_provider_rules(
            provider_id="PR07", specialty_id="general_practice", plan=plan, today=today
        )

    _write(tmp_path, monkeypatch, dropped(today - timedelta(days=1), today + timedelta(days=30)))
    active = verdict()
    assert active is not None
    assert active.reason == "provider_not_in_network"

    _write(tmp_path, monkeypatch, dropped(today - timedelta(days=30), today - timedelta(days=1)))
    expired = verdict()
    assert expired is None or expired.reason != "provider_not_in_network"


def test_spoken_notice_matches_site_and_day(tmp_path, monkeypatch):
    from handlers import get_earliest_slot

    _write(tmp_path, monkeypatch, {"tone": None, "notices": [LIFT]})
    notices = reception_notices.load_notices()

    assert reception_notices.spoken_for(notices, "centro", date(2026, 9, 22)) == [LIFT["text"]]
    assert reception_notices.spoken_for(notices, "norte", date(2026, 9, 22)) == []
    assert reception_notices.spoken_for(notices, "centro", date(2026, 9, 25)) == []

    result, _ = asyncio.run(
        get_earliest_slot(
            {"specialty": "general_practice", "site": "centro"},
            _manager([_slot("PR01", "2026-09-22")]),
        )
    )
    assert result["status"] == "offer"
    assert LIFT["text"] in result["spoken_notices"]


def test_tone_appends_one_sentence(tmp_path, monkeypatch):
    today = date(2026, 9, 19)
    tone_text = "Habla de tú, cercano."

    monkeypatch.setenv("RECEPTION_NOTICES_PATH", str(tmp_path / "missing.json"))
    assert role_message(today) == ROLE_MESSAGE

    _write(tmp_path, monkeypatch, {"tone": {"text": tone_text, "until": "2026-09-30"}, "notices": []})
    message = role_message(today)
    assert message.startswith(ROLE_MESSAGE)
    assert tone_text in message
    assert len(message) > len(ROLE_MESSAGE)

    # Same file, read the day after the tone ran out.
    assert role_message(date(2026, 10, 1)) == ROLE_MESSAGE


def test_invalid_entries_are_skipped(tmp_path, monkeypatch):
    raw = {
        "tone": None,
        "notices": [
            ABSENCE,
            {"id": "bad_until", "kind": "provider_absent", "provider_id": "PR07", "from": "2026-09-22"},
            {
                "id": "bad_provider",
                "kind": "provider_absent",
                "provider_id": "PR99",
                "from": "2026-09-22",
                "until": "2026-09-24",
            },
            {
                "id": "bad_text",
                "kind": "spoken",
                "text": "x" * 201,
                "location_id": "centro",
                "from": "2026-09-19",
                "until": "2026-09-24",
            },
        ],
    }
    _write(tmp_path, monkeypatch, raw)
    availability = {"slots": [_slot("PR03", "2026-09-23"), _slot("PR07", "2026-09-23")], "blocked": []}

    notices = reception_notices.load_notices()
    shown, ids = reception_notices.hide_absent(availability, notices)

    assert _starts(shown) == [("PR07", "2026-09-23")]
    assert ids == ["n1"]
    assert reception_notices.spoken_for(notices, "centro", date(2026, 9, 22)) == []
    errors = reception_notices.validate_notices(raw)[1]
    assert sorted(e["id"] for e in errors) == ["bad_provider", "bad_text", "bad_until"]

    _write(tmp_path, monkeypatch, "{not json")
    broken = reception_notices.load_notices()
    shown, ids = reception_notices.hide_absent(availability, broken)
    assert shown["slots"] == availability["slots"]
    assert ids == []
    assert reception_notices.spoken_for(broken, "centro", date(2026, 9, 22)) == []


def test_applied_notice_is_audited(tmp_path, monkeypatch):
    from handlers import get_earliest_slot

    # The suite switches auditing off by default; this test reads the log.
    audit_dir = tmp_path / "audit"
    monkeypatch.setenv("AUDIT_DIR", str(audit_dir))
    _write(tmp_path, monkeypatch, {"tone": None, "notices": [ABSENCE, LIFT]})

    reception_notices.record_active_notices(CALL_ID)
    active = _events(audit_dir, "notices_active")
    assert len(active) == 1
    assert sorted(active[0]["ids"]) == ["n1", "n4"]

    asyncio.run(
        get_earliest_slot(
            {"provider_name": "Martín Sáez", "site": "centro"},
            _manager([_slot("PR03", "2026-09-23"), _slot("PR07", "2026-09-23")]),
        )
    )
    applied = _events(audit_dir, "notice_applied")
    assert applied
    assert "n1" in applied[-1]["notice_ids"]


def test_applied_notice_reaches_why_panel(tmp_path, monkeypatch):
    from handlers import get_earliest_slot

    _write(tmp_path, monkeypatch, {"tone": None, "notices": [ABSENCE]})

    result, _ = asyncio.run(
        get_earliest_slot(
            {"provider_name": "Martín Sáez", "site": "centro"},
            _manager([_slot("PR03", "2026-09-23"), _slot("PR07", "2026-09-23")]),
        )
    )

    assert isinstance(result["justification"], str)
    assert "n1" in result["justification"]


def test_notices_routes_roundtrip_and_reject_invalid(tmp_path, monkeypatch):
    path = tmp_path / "notices.json"
    monkeypatch.setenv("RECEPTION_NOTICES_PATH", str(path))
    app = FastAPI()
    reception_notices.mount_notices_routes(app)
    client = TestClient(app)

    assert client.get("/notices").json() == {"tone": None, "notices": []}

    body = {"tone": {"text": "Habla de tú, cercano.", "until": "2026-09-30"}, "notices": [ABSENCE, LIFT]}
    assert client.put("/notices", json=body).status_code == 200
    assert client.get("/notices").json() == body
    saved = path.read_text(encoding="utf-8")

    invalid = {
        "tone": None,
        "notices": [
            ABSENCE,
            {"id": "bad", "kind": "provider_absent", "provider_id": "PR07", "from": "2026-09-22"},
        ],
    }
    rejected = client.put("/notices", json=invalid)
    assert rejected.status_code == 422
    detail = rejected.json()["detail"]
    assert [e["id"] for e in detail] == ["bad"]
    assert all("error" in e for e in detail)
    assert path.read_text(encoding="utf-8") == saved
    assert client.get("/notices").json() == body
