"""Retry and status handling of the clinic API client, against an in-memory transport."""

import asyncio

import httpx
import pytest

from clinic import clinic_client
from clinic.clinic_client import ClinicClient


def _client(monkeypatch, responses):
    """A ClinicClient whose transport answers with `responses` in order, counting calls."""
    monkeypatch.setattr(clinic_client, "RETRY_DELAY_SECS", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status, body = responses[len(calls) - 1]
        return httpx.Response(status, json=body)

    client = ClinicClient(base_url="http://clinic/api", api_key="pk-test", transport=httpx.MockTransport(handler))
    return client, calls


def test_409_means_already_accepted_not_an_error(monkeypatch):
    client, calls = _client(monkeypatch, [(409, {"detail": "already accepted"})])

    result = asyncio.run(client.post_submission({"action": "NO_ACTION", "call_id": "c", "reason": "out_of_scope"}))

    assert result == {"detail": "already accepted"} and len(calls) == 1


def test_5xx_is_retried_once(monkeypatch):
    client, calls = _client(monkeypatch, [(503, {}), (200, {"matches": [{"patient_id": "P1"}]})])

    assert asyncio.run(client.search_directory(national_id="12345678Z")) == [{"patient_id": "P1"}]
    assert len(calls) == 2


def test_4xx_is_not_retried(monkeypatch):
    client, calls = _client(monkeypatch, [(422, {"detail": "bad"}), (200, {})])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.search_directory(national_id="x"))
    assert len(calls) == 1


def test_api_key_header_and_submit_body(monkeypatch):
    client, calls = _client(monkeypatch, [(200, {"ok": True})])

    asyncio.run(client.post_submission({"action": "BOOK", "call_id": "c", "slot": "s"}))

    assert calls[0].headers["X-Api-Key"] == "pk-test"
    assert calls[0].url.path == "/api/v1/submit/book"
    assert b'"action"' not in calls[0].content  # the route carries the action, not the body


def test_one_http_client_per_call_and_it_closes(monkeypatch):
    client, _ = _client(monkeypatch, [(200, {"matches": []}), (200, {"matches": []})])

    async def run():
        first = client._http
        await client.search_directory(name="a")
        await client.search_directory(name="b")
        assert client._http is first  # no new connection (TLS handshake) per request
        await client.aclose()

    asyncio.run(run())
    assert client._http.is_closed
