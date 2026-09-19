"""Doctor inbox fan-out: CANCEL notify, ESCALATE overlay, idempotent retries."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability import mount_observability_routes
from observability.events import make_event
from observability.hub import get_hub, reset_hub
from observability.store import reset_store

MADRID = ZoneInfo("Europe/Madrid")


@pytest.fixture
def store_and_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_DB", str(tmp_path / "console.sqlite"))
    monkeypatch.setenv("CONSOLE_AUTH_SECRET", "test-secret")

    async def _prep():
        store = await reset_store(tmp_path / "console.sqlite")
        reset_hub(store)
        return store

    store = asyncio.run(_prep())
    app = FastAPI()
    mount_observability_routes(app)
    with TestClient(app) as client:
        yield store, client

    async def _teardown():
        await get_hub().aclose()

    asyncio.run(_teardown())


def _login_provider(client, key="PR01"):
    r = client.post("/auth/login", json={"key": key})
    assert r.status_code == 200
    return r


def test_cancel_posts_notification_to_appointment_provider(store_and_client):
    store, client = store_and_client

    async def _run():
        hub = get_hub()
        await hub.emit(
            make_event(
                "call.started",
                "CA-cancel-1",
                transport="twilio",
                from_number="+34600",
            )
        )
        await hub.emit(
            make_event(
                "state.patched",
                "CA-cancel-1",
                patient_name="Ana López",
                patient_id="P1",
            )
        )
        await hub.emit(
            make_event(
                "action.queued",
                "CA-cancel-1",
                action="CANCEL",
                seq=1,
                payload={
                    "action": "CANCEL",
                    "appointment_id": "A1",
                    "provider_id": "PR01",
                    "slot": "2026-09-22T10:00:00+02:00",
                },
            )
        )
        await hub.emit(
            make_event(
                "submit.posted",
                "CA-cancel-1",
                action="CANCEL",
                http_status=200,
                ok=True,
            )
        )
        await hub.emit(
            make_event(
                "submit.posted",
                "CA-cancel-1",
                action="CANCEL",
                http_status=200,
                ok=True,
            )
        )
        notes = await store.list_notifications("PR01")
        assert len(notes) == 1
        assert notes[0]["kind"] == "cancel"
        assert "Ana López" in notes[0]["body"]
        assert notes[0]["unread"] is True
        assert await store.list_notifications("PR07") == []

    asyncio.run(_run())

    _login_provider(client, "PR01")
    inbox = client.get("/auth/me/inbox")
    assert inbox.status_code == 200
    body = inbox.json()
    assert body["unread"] == 1
    assert body["notifications"][0]["kind"] == "cancel"
    nid = body["notifications"][0]["id"]
    read = client.post(f"/auth/me/inbox/{nid}/read")
    assert read.status_code == 200
    assert read.json()["unread"] == 0


def test_escalate_creates_overlay_and_inbox(store_and_client, monkeypatch):
    store, client = store_and_client
    from observability import emergency as emergency_mod

    real_plan = emergency_mod.plan_emergency

    async def _plan(**kwargs):
        kwargs["now"] = datetime(2026, 9, 21, 10, 7, tzinfo=MADRID)
        kwargs["past_appointments"] = []
        return await real_plan(**kwargs)

    monkeypatch.setattr(emergency_mod, "plan_emergency", _plan)

    async def _fake_free(provider_id, date_from, date_to):
        return [], "availability_error:test"

    monkeypatch.setattr(
        "observability.provider_calendar.fetch_provider_free_slots",
        _fake_free,
    )

    async def _run():
        hub = get_hub()
        await hub.emit(make_event("call.started", "CA-esc-1", transport="twilio"))
        await hub.emit(
            make_event(
                "state.patched",
                "CA-esc-1",
                patient_name="Luis Ortega",
                patient_id="P77",
            )
        )
        await hub.emit(
            make_event(
                "action.queued",
                "CA-esc-1",
                action="ESCALATE",
                seq=1,
                reason="medical_emergency",
                payload={"action": "ESCALATE", "reason": "medical_emergency"},
            )
        )
        await hub.emit(
            make_event(
                "submit.posted",
                "CA-esc-1",
                action="ESCALATE",
                http_status=200,
                ok=True,
                reason="medical_emergency",
            )
        )
        await hub.emit(
            make_event(
                "submit.posted",
                "CA-esc-1",
                action="ESCALATE",
                http_status=200,
                ok=True,
                reason="medical_emergency",
            )
        )

        notes = await store.list_notifications("PR01")
        assert len(notes) == 1
        assert notes[0]["kind"] == "emergency"
        overlays = await store.list_provider_overlays(
            "PR01", date_from="2026-09-21", date_to="2026-09-21"
        )
        assert len(overlays) == 1
        assert "10:15" in overlays[0]["slot"]

    asyncio.run(_run())

    _login_provider(client, "PR01")
    cal = client.get("/auth/me/calendar?week_start=2026-09-21")
    assert cal.status_code == 200
    monday = next(d for d in cal.json()["days"] if d["date"] == "2026-09-21")
    emergencies = [b for b in monday["blocks"] if b["kind"] == "emergency"]
    assert any(b["start"] == "10:15" for b in emergencies)
    assert any(b.get("source") == "overlay" for b in emergencies)
