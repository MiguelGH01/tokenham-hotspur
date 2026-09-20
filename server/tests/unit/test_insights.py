"""Conversation insights: CRUD, Jev extraction, hang-up scheduling."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability import mount_observability_routes
from observability.auth import COOKIE_NAME
from observability.events import make_event
from observability.hub import get_hub, reset_hub
from observability.insights import (
    extract_call_insights,
    validate_insight_body,
)
from observability.store import reset_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_DB", str(tmp_path / "insights.sqlite"))
    monkeypatch.setenv("CONSOLE_AUTH_SECRET", "test-secret")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    async def _prep():
        store = await reset_store(tmp_path / "insights.sqlite")
        reset_hub(store)
        return store

    asyncio.run(_prep())

    app = FastAPI()
    mount_observability_routes(app)
    with TestClient(app) as c:
        yield c

    async def _teardown():
        await get_hub().aclose()

    asyncio.run(_teardown())


def _admin(client: TestClient) -> TestClient:
    r = client.post("/auth/login", json={"key": "admin"})
    assert r.status_code == 200
    assert COOKIE_NAME in r.cookies
    return client


# ------------------------------------------------------------------ validate


def test_validate_rejects_one_value():
    ok, err = validate_insight_body(
        {"name": "x", "description": "Is it?", "values": ["si"]}
    )
    assert ok is None
    assert err and "at least 2" in err


def test_validate_rejects_bad_name():
    ok, err = validate_insight_body(
        {"name": "persona mayor", "description": "?", "values": ["si", "no"]}
    )
    assert ok is None
    assert err and err.startswith("name:")


def test_validate_ok():
    ok, err = validate_insight_body(
        {
            "name": "persona_mayor",
            "description": "Is the caller elderly?",
            "values": ["si", "no", "unknown"],
        }
    )
    assert err is None
    assert ok == {
        "name": "persona_mayor",
        "description": "Is the caller elderly?",
        "values": ["si", "no", "unknown"],
    }


# ------------------------------------------------------------------ CRUD


def test_insight_crud_and_validation(client):
    _admin(client)
    assert client.get("/observability/insights").json() == {"insights": []}

    bad = client.post(
        "/observability/insights",
        json={"name": "x", "description": "q", "values": ["only"]},
    )
    assert bad.status_code == 422
    assert "at least 2" in bad.json()["detail"]

    slug = client.post(
        "/observability/insights",
        json={"name": "bad name", "description": "q", "values": ["a", "b"]},
    )
    assert slug.status_code == 422

    created = client.post(
        "/observability/insights",
        json={
            "name": "persona_mayor",
            "description": "¿Es persona mayor?",
            "values": ["si", "no"],
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["name"] == "persona_mayor"
    assert body["values"] == ["si", "no"]
    insight_id = body["id"]

    dup = client.post(
        "/observability/insights",
        json={
            "name": "persona_mayor",
            "description": "again",
            "values": ["si", "no"],
        },
    )
    assert dup.status_code == 422
    assert "already exists" in dup.json()["detail"]

    listed = client.get("/observability/insights").json()["insights"]
    assert len(listed) == 1
    assert listed[0]["id"] == insight_id

    gone = client.delete(f"/observability/insights/{insight_id}")
    assert gone.status_code == 200
    assert client.get("/observability/insights").json() == {"insights": []}

    missing = client.delete(f"/observability/insights/{insight_id}")
    assert missing.status_code == 404


def test_clear_keeps_insight_defs(tmp_path):
    async def run():
        store = await reset_store(tmp_path / "keep.sqlite")
        await store.create_insight_def(
            name="tone", description="Tone?", values=["calm", "angry"]
        )
        await store.clear()
        defs = await store.list_insight_defs()
        assert len(defs) == 1
        assert defs[0]["name"] == "tone"
        await store.close()

    asyncio.run(run())


# ------------------------------------------------------------------ extract


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def aclose(self) -> None:
        pass

    async def classify_choice(self, *, state, question_id, instructions, values):
        self.calls.append(
            {
                "state": state,
                "question_id": question_id,
                "instructions": instructions,
                "values": list(values),
            }
        )
        return {
            "choice": values[0],
            "probabilities": {v: (1.0 if i == 0 else 0.0) for i, v in enumerate(values)},
            "confidence": 0.91,
        }


def test_extractor_one_request_per_def(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def run():
        store = await reset_store(tmp_path / "extract.sqlite")
        hub = reset_hub(store)
        await hub.ensure_ready()

        await store.create_insight_def(
            name="persona_mayor", description="Elderly?", values=["si", "no"]
        )
        await store.create_insight_def(
            name="urgencia", description="Urgent?", values=["si", "no", "unknown"]
        )

        await hub.start_call("CA-1", transport="webrtc", is_test=True)
        await hub.emit(
            make_event("transcript.bot", "CA-1", text="Buenos días", final=True)
        )
        await hub.emit(
            make_event("transcript.user", "CA-1", text="Tengo 80 años", final=True)
        )

        fake = _FakeClient()
        await extract_call_insights("CA-1", store=store, emitter=hub, client=fake)

        assert len(fake.calls) == 2
        ids = {c["question_id"] for c in fake.calls}
        assert ids == {"persona_mayor", "urgencia"}
        for c in fake.calls:
            assert c["state"] == {
                "conversation": [
                    {"role": "assistant", "text": "Buenos días"},
                    {"role": "user", "text": "Tengo 80 años"},
                ]
            }

        events = await store.list_events("CA-1")
        kinds = [e["kind"] for e in events]
        assert kinds.count("insight.pending") == 2
        assert kinds.count("insight.extracted") == 2
        extracted = [e for e in events if e["kind"] == "insight.extracted"]
        assert {e["payload"]["choice"] for e in extracted}  # non-empty
        assert all(e["payload"]["confidence"] == 0.91 for e in extracted)

        await hub.aclose()

    asyncio.run(run())


def test_unconfigured_emits_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    async def run():
        store = await reset_store(tmp_path / "noconf.sqlite")
        hub = reset_hub(store)
        await hub.ensure_ready()
        await store.create_insight_def(
            name="x", description="?", values=["a", "b"]
        )
        await hub.start_call("CA-2", transport="webrtc")
        await extract_call_insights("CA-2", store=store, emitter=hub, client=None)
        events = await store.list_events("CA-2")
        failed = [e for e in events if e["kind"] == "insight.failed"]
        assert len(failed) == 1
        assert failed[0]["payload"]["error"] == "typesafe_unconfigured"
        await hub.aclose()

    asyncio.run(run())


def test_eval_transport_skips_jev(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def run():
        store = await reset_store(tmp_path / "eval.sqlite")
        hub = reset_hub(store)
        await hub.ensure_ready()
        await store.create_insight_def(
            name="x", description="?", values=["a", "b"]
        )
        await hub.start_call("CA-eval", transport="eval")
        fake = _FakeClient()
        await extract_call_insights("CA-eval", store=store, emitter=hub, client=fake)
        assert fake.calls == []
        kinds = [e["kind"] for e in await store.list_events("CA-eval")]
        assert "insight.pending" not in kinds
        await hub.aclose()

    asyncio.run(run())


def test_end_call_schedules_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def run():
        store = await reset_store(tmp_path / "end.sqlite")
        hub = reset_hub(store)
        await hub.ensure_ready()
        await store.create_insight_def(
            name="x", description="?", values=["a", "b"]
        )
        await hub.start_call("CA-end", transport="webrtc")
        await hub.emit(
            make_event("transcript.user", "CA-end", text="hola", final=True)
        )

        # Patch schedule so we can count without racing real Jev.
        scheduled: list[str] = []

        def _fake_schedule(call_id, *, store, emitter, client=None):
            scheduled.append(call_id)

            async def _noop():
                return None

            return asyncio.create_task(_noop())

        import observability.insights as insights_mod

        monkeypatch.setattr(insights_mod, "schedule_extract", _fake_schedule)

        first = await hub.end_call("CA-end")
        assert first is not None
        second = await hub.end_call("CA-end")
        assert second is None
        assert scheduled == ["CA-end"]
        await hub.aclose()

    asyncio.run(run())


def test_end_call_runs_extract_with_mock(tmp_path, monkeypatch):
    """Integration: end_call → schedule_extract → fake client."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    fake = _FakeClient()

    async def run():
        store = await reset_store(tmp_path / "live.sqlite")
        hub = reset_hub(store)
        await hub.ensure_ready()
        await store.create_insight_def(
            name="persona_mayor", description="Elderly?", values=["si", "no"]
        )
        await hub.start_call("CA-live", transport="webrtc")
        await hub.emit(
            make_event("transcript.user", "CA-live", text="soy mayor", final=True)
        )

        import observability.insights as insights_mod
        from observability.insights import schedule_extract as real_schedule

        def _schedule(call_id, *, store, emitter, client=None):
            return real_schedule(call_id, store=store, emitter=emitter, client=fake)

        monkeypatch.setattr(insights_mod, "schedule_extract", _schedule)

        await hub.end_call("CA-live")
        # Let the background task finish.
        pending = [t for t in asyncio.all_tasks() if t.get_name().startswith("insights:")]
        if pending:
            await asyncio.gather(*pending)

        assert len(fake.calls) == 1
        assert fake.calls[0]["question_id"] == "persona_mayor"
        kinds = [e["kind"] for e in await store.list_events("CA-live")]
        assert "insight.extracted" in kinds
        await hub.aclose()

    asyncio.run(run())


def test_turns_from_events_keeps_speech_only():
    from observability.insights import turns_from_events

    turns = turns_from_events(
        [
            {"kind": "call.started", "payload": {}},
            {"kind": "transcript.bot", "payload": {"text": "Hola"}},
            {"kind": "tool.called", "payload": {"name": "search_patient"}},
            {"kind": "transcript.user", "payload": {"text": "Quiero cita"}},
            {"kind": "transcript.bot", "payload": {"text": "  "}},
        ]
    )
    assert turns == [
        {"role": "assistant", "text": "Hola"},
        {"role": "user", "text": "Quiero cita"},
    ]


def test_extract_accepts_turns_when_store_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def run():
        store = await reset_store(tmp_path / "el.sqlite")
        hub = reset_hub(store)
        await hub.ensure_ready()
        await store.create_insight_def(
            name="persona_mayor", description="Elderly?", values=["si", "no"]
        )
        fake = _FakeClient()
        status = await extract_call_insights(
            "conv_only",
            store=store,
            emitter=hub,
            client=fake,
            turns=[{"role": "user", "text": "tengo 80"}],
        )
        assert status == "ok"
        assert fake.calls[0]["state"]["conversation"] == [
            {"role": "user", "text": "tengo 80"}
        ]
        kinds = [e["kind"] for e in await store.list_events("conv_only")]
        assert "insight.extracted" in kinds
        await hub.aclose()

    asyncio.run(run())


def test_recompute_endpoint_runs_jev(client, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    fake = _FakeClient()

    from observability.insights import extract_call_insights as real_extract
    import observability.routes as routes_mod

    async def _extract(call_id, *, store, emitter, client=None, turns=None):
        return await real_extract(
            call_id, store=store, emitter=emitter, client=fake, turns=turns
        )

    monkeypatch.setattr(routes_mod, "extract_call_insights", _extract)

    _admin(client)
    created = client.post(
        "/observability/insights",
        json={
            "name": "persona_mayor",
            "description": "Elderly?",
            "values": ["si", "no"],
        },
    )
    assert created.status_code == 200

    async def seed():
        hub = get_hub()
        await hub.start_call("CA-re", transport="webrtc")
        await hub.emit(
            make_event("transcript.user", "CA-re", text="soy mayor", final=True)
        )

    asyncio.run(seed())

    r = client.post("/observability/calls/CA-re/insights")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    kinds = [e["kind"] for e in body["events"]]
    assert "insight.extracted" in kinds
    assert fake.calls and fake.calls[-1]["question_id"] == "persona_mayor"


def test_recompute_requires_defs(client):
    _admin(client)
    r = client.post("/observability/calls/CA-missing/insights")
    assert r.status_code == 422
    assert r.json()["detail"] == "no_insights_defined"
