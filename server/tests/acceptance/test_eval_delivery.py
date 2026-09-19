"""The local eval lane's own delivery.

The lane is dialled by the harness, not by the platform, so its ``call_id`` is a
UUID we minted and every submit route answers ``404 unknown call``. The bot then
tells the caller, honestly, that the booking did not go through — and a lane
where every booking fails cannot tell a wrong answer from an undeliverable one.
So the lane's writes are answered locally while its reads stay real, and the
payload is still recorded, because that record is what the offline oracle scores.
"""

import asyncio
import json

import httpx

from clients.clinic_client import ClinicClient, DryRunSubmit
from submission import CallSubmission


def run(coro):
    return asyncio.run(coro)


OFFER = {
    "patient_id": "P00001",
    "provider_id": "PR01",
    "location_id": "centro",
    "appointment_type_id": "review",
    "slot": "2026-09-19T11:00:00+02:00",
    "policy_id": "mapfre",
}


def _recording_client(monkeypatch, posted):
    """A real client whose POSTs are captured instead of sent."""
    real = httpx.AsyncClient

    def handle(request):
        posted.append((request.url.path, request.content))
        return httpx.Response(404, json={"detail": "unknown call"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(handle)),
    )
    return ClinicClient("https://example.invalid/api", "test")


def _events(tmp_path, call_id):
    path = tmp_path / f"audit-{call_id}.ndjson"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_dry_run_delivery_records_the_payload_and_posts_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    posted = []
    client = DryRunSubmit(_recording_client(monkeypatch, posted))
    submission = CallSubmission("call-eval", client)
    submission.set_book(OFFER)
    submission.decide()

    assert run(submission.flush()) is True
    assert posted == []

    attempts = [e for e in _events(tmp_path, "call-eval") if e["event"] == "submission_attempt"]
    assert [a["verb"] for a in attempts] == ["BOOK"]
    assert attempts[-1]["payload"]["provider_id"] == "PR01"


def test_reads_still_go_to_the_real_clinic_api(tmp_path, monkeypatch):
    """The lane is not a simulation of the clinic: only its writes are local."""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    seen = []
    real = httpx.AsyncClient

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"matches": [{"patient_id": "P00001"}]})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(handle)),
    )
    client = DryRunSubmit(ClinicClient("https://example.invalid/api", "test"))

    assert run(client.search_directory(name="Josefa")) == [{"patient_id": "P00001"}]
    assert seen == ["/api/v1/directory"]


def test_the_lane_does_not_report_a_delivery_failure_to_the_caller(tmp_path, monkeypatch):
    """What the caller hears is the point: 404 on a minted call_id is not the bot's fault.

    Without the dry run the same submission raises, ``flush`` returns False, and
    the conversation turns into an apology the judge then marks against the bot.
    """
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    posted = []
    real_client = _recording_client(monkeypatch, posted)
    submission = CallSubmission("call-eval", DryRunSubmit(real_client))
    submission.set_book(OFFER)
    submission.decide()
    assert run(submission.flush()) is True

    real_post = CallSubmission("call-eval", real_client)
    real_post.set_book(OFFER)
    real_post.decide()
    assert run(real_post.flush()) is False
