"""What happens when the clinic's own API fails, rather than answering.

Two different things produce "we could not find you": a directory that does not
know this patient, and a directory that is not answering. They must not look
alike, because they call for opposite behaviour. A missing patient is a fact the
caller can correct; a platform fault is not, and the agent that reads one as the
other asks a caller to repeat an identifier they gave correctly and never
identifies anybody (observed 19 Sep: ``/v1/directory`` answered ``502`` and then
timed out, twice in fifteen local calls).
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from clients import clinic_client
from clients.clinic_client import ClinicApiError, ClinicClient
from submission import CallSubmission


def run(coro):
    return asyncio.run(coro)


def _client(monkeypatch, handle, *, base_url="https://example.invalid/api"):
    """A client whose transport is scripted by ``handle``, and with no backoff."""
    real = httpx.AsyncClient
    monkeypatch.setattr(clinic_client, "RETRY_DELAY_SECS", 0.0)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(handle)),
    )
    return ClinicClient(base_url, "test")


def test_a_read_is_retried_past_the_first_two_failures(monkeypatch):
    """A lookup that gives up loses the case outright, so it is given more room.

    ``502`` then a timeout is the shape the platform actually produces under a
    scored run's ten concurrent calls; the third attempt is the one that lands.
    """
    attempts = []

    def handle(request):
        attempts.append(request.url.path)
        if len(attempts) <= 2:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"matches": [{"patient_id": "P1"}]})

    matches = run(_client(monkeypatch, handle).search_directory(name="Josefa"))
    assert matches == [{"patient_id": "P1"}]
    assert len(attempts) == 3


def test_a_write_keeps_its_shorter_budget(monkeypatch):
    """``submission._deliver`` already retries the POST; this loop must not multiply it."""
    attempts = []

    def handle(request):
        attempts.append(request.url.path)
        return httpx.Response(502, text="bad gateway")

    client = _client(monkeypatch, handle)
    with pytest.raises(ClinicApiError) as raised:
        run(client.post_submission({"action": "NO_ACTION", "reason": "out_of_scope"}))
    assert raised.value.status == 502
    assert len(attempts) == clinic_client.ATTEMPTS
    assert len(attempts) < clinic_client.READ_ATTEMPTS


def test_a_read_that_never_recovers_raises_instead_of_reporting_no_match(monkeypatch):
    """An empty result is a fact about the directory. A fault is not."""

    def handle(request):
        return httpx.Response(503, text="unavailable")

    with pytest.raises(ClinicApiError) as raised:
        run(_client(monkeypatch, handle).search_directory(name="Josefa"))
    assert raised.value.status == 503


def test_a_read_timeout_is_retried_like_a_server_error(monkeypatch):
    attempts = []

    def handle(request):
        attempts.append(request.url.path)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"matches": []})

    assert run(_client(monkeypatch, handle).search_directory(name="Josefa")) == []
    assert len(attempts) == 2


class _FailingDirectory:
    async def search_directory(self, **kwargs):
        raise httpx.ReadTimeout("timed out")

    async def post_submission(self, action):
        return {"received": True}


def _identify_manager(client):
    return SimpleNamespace(
        state={
            "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
            "client": client,
            "submission": CallSubmission("call-x", client),
            "identify_attempts": 0,
            "intent": "book",
        },
        get_current_context=lambda: [],
    )


def test_a_directory_fault_is_not_reported_as_a_missing_patient():
    """The tool result is where the model learns which of the two happened."""
    from flows.identification import search_patient

    result, node = run(
        search_patient(
            {
                "stated_name": "Josefa Domínguez Navarro",
                "id_type": "national_id",
                "id_value": "48064716Y",
            },
            _identify_manager(_FailingDirectory()),
        )
    )
    assert result["status"] == "lookup_failed"
    assert node is None
    instruction = result["instruction"].lower()
    assert "not a missing patient" in instruction
    assert "not in the system" in instruction
    # It must ask for the same lookup again, not for a re-spoken identifier.
    assert "call search_patient again" in instruction


def test_a_directory_fault_does_not_spend_an_identification_attempt():
    """Three mistakes by the caller end the call; the platform failing is not one."""
    from flows.identification import search_patient

    manager = _identify_manager(_FailingDirectory())
    for _ in range(5):
        run(
            search_patient(
                {
                    "stated_name": "Josefa Domínguez Navarro",
                    "id_type": "national_id",
                    "id_value": "48064716Y",
                },
                manager,
            )
        )
    assert manager.state["identify_attempts"] == 0


class _FailingDiary:
    async def availability(self, *args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    async def post_submission(self, action):
        return {"received": True}


def test_a_diary_fault_is_not_an_answer_about_availability():
    """``no_slots`` is a fact about the diary, and would be submitted as a rule."""
    from flows.booking import get_earliest_slot

    client = _FailingDiary()
    manager = SimpleNamespace(
        state={
            "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
            "client": client,
            "submission": CallSubmission("call-x", client),
            "patient": {"patient_id": "P1", "insurer": "sanitas"},
            "offers": {},
        },
        get_current_context=lambda: [],
    )
    result, node = run(get_earliest_slot({"specialty": "general_practice"}, manager))
    assert result["status"] == "lookup_failed"
    assert node is None
    instruction = result["instruction"].lower()
    assert "not an answer about availability" in instruction
    assert "do not say that nothing is free" in instruction
    assert "call get_earliest_slot again" in instruction
