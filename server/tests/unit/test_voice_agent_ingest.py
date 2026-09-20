"""Observability ingest for the elevenagent sidecar."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability.hub import get_hub, reset_hub
from observability.store import reset_store
from voice_agent import IngestEventBody, ingest_obs_event, mount_voice_agent


@pytest.fixture
def store_and_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_DB", str(tmp_path / "console.sqlite"))
    monkeypatch.setenv("VOICE_AGENT", "carloslabs")  # do not spawn sidecar in unit tests
    monkeypatch.setenv("CONSOLE_AUTH_SECRET", "test-secret")

    async def _prep():
        store = await reset_store(tmp_path / "console.sqlite")
        reset_hub(store)
        return store

    store = asyncio.run(_prep())
    app = FastAPI()

    @app.post("/internal/obs/events")
    async def _ingest(body: IngestEventBody):
        return await ingest_obs_event(body)

    with TestClient(app) as client:
        yield store, client

    async def _teardown():
        await get_hub().aclose()

    asyncio.run(_teardown())


def test_ingest_call_started_transcript_and_submit(store_and_token):
    _store, client = store_and_token

    r = client.post(
        "/internal/obs/events",
        json={
            "kind": "call.started",
            "call_id": "CA-ea-1",
            "payload": {"transport": "twilio", "from_number": "+34600111222"},
        },
    )
    assert r.status_code == 200

    r = client.post(
        "/internal/obs/events",
        json={
            "kind": "transcript.user",
            "call_id": "CA-ea-1",
            "payload": {"text": "Hola, soy Ana", "final": True},
        },
    )
    assert r.status_code == 200

    r = client.post(
        "/internal/obs/events",
        json={
            "kind": "transcript.bot",
            "call_id": "CA-ea-1",
            "payload": {"text": "Buenos días, ¿en qué puedo ayudarle?", "final": True},
        },
    )
    assert r.status_code == 200

    r = client.post(
        "/internal/obs/events",
        json={
            "kind": "tool.called",
            "call_id": "CA-ea-1",
            "payload": {"name": "search-patient", "args": {"name": "Ana"}},
        },
    )
    assert r.status_code == 200

    r = client.post(
        "/internal/obs/events",
        json={
            "kind": "action.queued",
            "call_id": "CA-ea-1",
            "payload": {
                "action": "BOOK",
                "seq": 1,
                "payload": {
                    "action": "BOOK",
                    "patient_id": "P1",
                    "provider_id": "PR01",
                    "slot": "2026-09-22T10:00:00+02:00",
                },
            },
        },
    )
    assert r.status_code == 200

    r = client.post(
        "/internal/obs/events",
        json={
            "kind": "submit.posted",
            "call_id": "CA-ea-1",
            "payload": {"action": "BOOK", "http_status": 200, "ok": True},
        },
    )
    assert r.status_code == 200

    r = client.post(
        "/internal/obs/events",
        json={"kind": "call.ended", "call_id": "CA-ea-1", "payload": {}},
    )
    assert r.status_code == 200

    hub = get_hub()
    snap = hub.get_call("CA-ea-1")
    assert snap is not None
    assert snap["status"] == "ended"
    assert snap["transport"] == "twilio"
    assert snap["from_number"] == "+34600111222"
    assert snap["submitted"] is True
    assert snap["primary_action"] == "BOOK"
    assert snap["current_node"] == "identify"  # from search-patient tool.called
    kinds = [e["kind"] for e in snap["events"]]
    assert "call.started" in kinds
    assert "node.entered" in kinds  # reception + identify
    assert "transcript.user" in kinds
    assert "transcript.bot" in kinds
    assert "tool.called" in kinds
    assert "action.queued" in kinds
    assert "submit.posted" in kinds
    assert "call.ended" in kinds


def test_ingest_cancel_fans_out_doctor_notification(store_and_token):
    store, client = store_and_token

    client.post(
        "/internal/obs/events",
        json={
            "kind": "call.started",
            "call_id": "CA-cancel-ea",
            "payload": {"transport": "twilio"},
        },
    )
    client.post(
        "/internal/obs/events",
        json={
            "kind": "state.patched",
            "call_id": "CA-cancel-ea",
            "payload": {"patient_name": "Ana López", "patient_id": "P1"},
        },
    )
    client.post(
        "/internal/obs/events",
        json={
            "kind": "action.queued",
            "call_id": "CA-cancel-ea",
            "payload": {
                "action": "CANCEL",
                "seq": 1,
                "payload": {
                    "action": "CANCEL",
                    "appointment_id": "A1",
                    "provider_id": "PR01",
                    "slot": "2026-09-22T10:00:00+02:00",
                },
            },
        },
    )
    client.post(
        "/internal/obs/events",
        json={
            "kind": "submit.posted",
            "call_id": "CA-cancel-ea",
            "payload": {"action": "CANCEL", "http_status": 200, "ok": True},
        },
    )

    async def _check():
        return await store.list_notifications(provider_id="PR01")

    notes = asyncio.run(_check())
    assert len(notes) >= 1
    assert notes[0]["kind"] == "cancel"


def test_ingest_rejects_unknown_kind(store_and_token):
    _store, client = store_and_token
    r = client.post(
        "/internal/obs/events",
        json={"kind": "not.a.real.event", "call_id": "CA-x", "payload": {}},
    )
    assert r.status_code == 400


def test_mount_voice_agent_skips_when_carloslabs(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_AGENT", "carloslabs")
    monkeypatch.setenv("OBSERVABILITY_DB", str(tmp_path / "x.sqlite"))
    app = FastAPI()
    mount_voice_agent(app)
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/internal/obs/events" not in paths
    assert not any(p and p.startswith("/tools") for p in paths)
