"""Console auth: admin key, provider ids, cookie session, route guards."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability import mount_observability_routes
from observability.auth import COOKIE_NAME, resolve_key, session_response
from observability.hub import get_hub, reset_hub
from observability.store import reset_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_DB", str(tmp_path / "console.sqlite"))
    monkeypatch.setenv("CONSOLE_AUTH_SECRET", "test-secret")

    async def _prep():
        store = await reset_store(tmp_path / "console.sqlite")
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


def test_resolve_admin_case_insensitive():
    assert resolve_key("admin") == {"role": "admin"}
    assert resolve_key("ADMIN") == {"role": "admin"}
    assert resolve_key("  Admin  ") == {"role": "admin"}


def test_resolve_provider_normalises_case():
    assert resolve_key("pr01") == {"role": "provider", "id": "PR01"}
    assert resolve_key("PR02") == {"role": "provider", "id": "PR02"}


def test_resolve_unknown_key():
    assert resolve_key("nope") is None
    assert resolve_key("") is None
    assert resolve_key("   ") is None


def test_session_response_includes_leave_for_pr02():
    body = session_response({"role": "provider", "id": "PR02"})
    assert body["role"] == "provider"
    assert body["provider"]["id"] == "PR02"
    assert body["provider"]["name"].startswith("Dr. Pablo Requena")
    assert body["provider"]["leave"] is not None
    assert body["provider"]["leave"]["start"] == "2026-09-14"
    assert body["provider"]["schedules"]


def test_login_cookie_is_secure_behind_https_proxy(client):
    r = client.post(
        "/auth/login",
        json={"key": "admin"},
        headers={"x-forwarded-proto": "https"},
    )
    assert r.status_code == 200
    header = r.headers.get("set-cookie") or ""
    assert "Secure" in header
    assert "samesite=none" in header.lower()


def test_ws_ticket_requires_admin(client):
    assert client.get("/auth/ws-ticket").status_code == 401
    client.post("/auth/login", json={"key": "PR01"})
    assert client.get("/auth/ws-ticket").status_code == 401


def test_ws_ticket_returns_signed_session(client):
    client.post("/auth/login", json={"key": "admin"})
    r = client.get("/auth/ws-ticket")
    assert r.status_code == 200
    ticket = r.json()["ticket"]
    assert ticket
    client.cookies.clear()
    with client.websocket_connect("/observability/live?ticket=" + ticket) as ws:
        msg = ws.receive_json()
        assert msg["kind"] == "snapshot"


def test_live_socket_rejects_anonymous(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/observability/live"):
            pass


def test_login_cookie_is_not_forced_secure_on_http(client):
    r = client.post("/auth/login", json={"key": "admin"})
    assert r.status_code == 200
    assert r.json() == {"role": "admin"}
    assert COOKIE_NAME in r.cookies
    header = r.headers.get("set-cookie") or ""
    assert "Secure" not in header


def test_login_provider_lowercase(client):
    r = client.post("/auth/login", json={"key": "pr01"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "provider"
    assert body["provider"]["id"] == "PR01"
    assert "Ortiz" in body["provider"]["name"]


def test_login_pr02_returns_leave(client):
    r = client.post("/auth/login", json={"key": "PR02"})
    assert r.status_code == 200
    assert r.json()["provider"]["leave"]["reason"] == "sick leave"


def test_login_invalid(client):
    r = client.post("/auth/login", json={"key": "hackerman"})
    assert r.status_code == 401
    assert COOKIE_NAME not in r.cookies


def test_me_requires_session(client):
    assert client.get("/auth/me").status_code == 401


def test_me_after_login(client):
    client.post("/auth/login", json={"key": "admin"})
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json() == {"role": "admin"}


def test_provider_cannot_read_observability(client):
    client.post("/auth/login", json={"key": "PR01"})
    assert client.get("/observability/shift").status_code == 401
    assert client.get("/observability/health").status_code == 401
    assert client.get("/observability/calls").status_code == 401


def test_admin_can_read_observability(client):
    client.post("/auth/login", json={"key": "admin"})
    r = client.get("/observability/shift")
    assert r.status_code == 200
    body = r.json()
    assert "calls" in body
    assert body["period"] == "today"
    assert body["shift_start"] is not None
    assert "volume" in body
    assert client.get("/observability/health").status_code == 200


def test_admin_shift_accepts_period(client):
    client.post("/auth/login", json={"key": "admin"})
    week = client.get("/observability/shift?period=week")
    assert week.status_code == 200
    assert week.json()["period"] == "week"
    all_time = client.get("/observability/shift?period=all")
    assert all_time.status_code == 200
    assert all_time.json()["period"] == "all"
    assert all_time.json()["shift_start"] is None
    assert client.get("/observability/calls?period=month").status_code == 200
    assert client.get("/observability/shift?period=forever").status_code == 422


def test_logout_clears_session(client):
    client.post("/auth/login", json={"key": "admin"})
    assert client.get("/auth/me").status_code == 200
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert client.get("/auth/me").status_code == 401
    assert client.get("/observability/shift").status_code == 401


def test_provider_calendar_endpoint(client, monkeypatch):
    async def fake_free(provider_id, date_from, date_to):
        return (
            [
                {
                    "provider_id": "PR01",
                    "start_time": f"{date_from.isoformat()}T09:00:00+02:00",
                    "duration_minutes": 15,
                    "location_id": "centro",
                }
            ],
            "availability",
        )

    monkeypatch.setattr(
        "observability.provider_calendar.fetch_provider_free_slots",
        fake_free,
    )
    client.post("/auth/login", json={"key": "PR01"})
    r = client.get("/auth/me/calendar", params={"week_start": "2026-09-21"})
    assert r.status_code == 200
    body = r.json()
    assert body["provider_id"] == "PR01"
    assert body["source"] == "availability"
    assert body["days"]
    assert body["date_from"] == "2026-09-21"


def test_admin_cannot_read_provider_calendar(client):
    client.post("/auth/login", json={"key": "admin"})
    assert client.get("/auth/me/calendar").status_code == 401


def test_origin_serves_healthcheck_favicon(client):
    for path in ("/favicon.ico", "/favicon.svg"):
        r = client.get(path)
        assert r.status_code == 200
        assert "svg" in r.headers["content-type"]
        assert b"#FF522E" in r.content
