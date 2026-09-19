"""The retry of a failed delivery: a decided case must not die on one POST.

A call with no accepted record scores nothing, so ``_deliver`` retries the same
frozen payload a bounded number of times before giving up. The contract here:
transient failures recover, permanent refusals stop quickly, the ordered list
never reorders, and every attempt leaves one audit line so a scored run can be
dissected afterwards.
"""

import asyncio
import json

import pytest

from clients.clinic_client import ClinicApiError, RETRYABLE_STATUSES
from submission import CallSubmission

OFFER = {"patient_id": "P00001", "provider_id": "PR01", "location_id": "centro",
         "appointment_type_id": "review", "slot": "2026-09-19T11:00:00+02:00", "policy_id": "mapfre"}


@pytest.fixture(autouse=True)
def fast_delivery(monkeypatch, tmp_path):
    """No real sleeps, and an audit file the test can read afterwards."""
    monkeypatch.setenv("SUBMIT_DELIVERY_BACKOFF_SECS", "0")
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    return tmp_path


def audit_events(tmp_path, call_id):
    path = tmp_path / f"audit-{call_id}.ndjson"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()]


class FlakyClient:
    """Fails the first N postings of any action, then accepts everything."""

    def __init__(self, failures=1, error=None):
        self.posted = []
        self.failures = failures
        self.error = error or OSError("offline")

    async def post_submission(self, action):
        if len(self.posted) < self.failures:
            self.posted.append(action)
            raise self.error
        self.posted.append(action)


class DeadClient:
    """Every posting fails with the given error."""

    def __init__(self, error):
        self.posted = []
        self.error = error

    async def post_submission(self, action):
        self.posted.append(action)
        raise self.error


def test_transient_failure_is_retried_on_the_same_payload(tmp_path):
    client = FlakyClient(failures=1)
    sub = CallSubmission("retry-1", client)
    sub.set_book(OFFER)
    assert asyncio.run(sub.flush()) is True
    assert len(client.posted) == 2
    # The retried payload is byte-identical: the plan is frozen once attempted.
    assert client.posted[0] == client.posted[1]
    assert sub._delivered == 1
    attempts = [e for e in audit_events(tmp_path, "retry-1") if e["event"] == "submission_attempt"]
    assert len(attempts) == 2
    assert [a["attempt"] for a in attempts] == [1, 2]


def test_permanent_4xx_stops_within_the_bounded_attempts(tmp_path):
    monkey_error = ClinicApiError("POST", "/v1/submit/book", 400, "bad payload")
    client = DeadClient(monkey_error)
    sub = CallSubmission("retry-2", client)
    sub.set_book(OFFER)
    assert asyncio.run(sub.flush()) is False
    assert len(client.posted) == 3  # SUBMIT_DELIVERY_ATTEMPTS default, not forever
    failures = [e for e in audit_events(tmp_path, "retry-2")
                if e["event"] == "submission_result" and e["ok"] is False]
    assert [f["status"] for f in failures] == [400, 400, 400]


def test_permanently_failed_action_blocks_the_ordered_rest():
    """A cancel before a book cannot be swapped, so a dead cancel blocks the book."""
    client = DeadClient(ClinicApiError("POST", "/v1/submit/cancel", 404, "unknown appointment"))
    sub = CallSubmission("retry-3", client)
    sub.set_cancel("A000123")
    sub.add_action({"action": "BOOK", **OFFER})
    assert asyncio.run(sub.flush()) is False
    assert [a["action"] for a in client.posted] == ["CANCEL"] * 3
    assert sub._delivered == 0


def test_close_recovers_a_transient_failure(tmp_path):
    """The audit-622ace24 shape: the plan failed once and the call was ending."""
    client = FlakyClient(failures=1)
    sub = CallSubmission("retry-4", client)
    sub.set_book(OFFER)
    assert asyncio.run(sub.close()) is True
    assert len(client.posted) == 2
    finals = [e for e in audit_events(tmp_path, "retry-4") if e.get("final")]
    assert len(finals) == 2  # attempt and result both marked final


def test_retry_attempts_are_env_bounded(monkeypatch):
    monkeypatch.setenv("SUBMIT_DELIVERY_ATTEMPTS", "2")
    client = DeadClient(OSError("offline"))
    sub = CallSubmission("retry-5", client)
    sub.set_book(OFFER)
    assert asyncio.run(sub.flush()) is False
    assert len(client.posted) == 2


def test_retryable_status_vocabulary_covers_rate_limits():
    """A Run All dials twenty calls at once; 429 and 408 must be retried."""
    assert 429 in RETRYABLE_STATUSES
    assert 408 in RETRYABLE_STATUSES
    # A payload refusal is still not worth a second attempt.
    assert 400 not in RETRYABLE_STATUSES
    assert 404 not in RETRYABLE_STATUSES
