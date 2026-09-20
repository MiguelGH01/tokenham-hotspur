"""on_client_disconnected must not race an in-flight decision.

Pipecat runs transport event handlers as their own task, uncoordinated with
the pipeline's current turn: a caller's "no" can still be in STT/LLM/tool
flight when the socket drops. ``_needs_disconnect_grace`` is the condition
bot.py uses to decide whether to give that turn a bounded moment before
``submission.close()`` reads whatever the call last had prepared.
"""

from bot import _needs_disconnect_grace
from submission import CallSubmission


class _Client:
    async def post_submission(self, payload):
        return payload


def test_a_live_undecided_proposal_needs_grace():
    submission = CallSubmission("c", _Client())
    state = {"submission": submission, "proposal": {"key": "offer-1", "revision": 0}}
    assert _needs_disconnect_grace(state)


def test_no_proposal_needs_no_grace():
    submission = CallSubmission("c", _Client())
    assert not _needs_disconnect_grace({"submission": submission})


def test_a_request_that_already_decided_needs_no_grace():
    """Nothing left to race: close() will just deliver what was already decided."""
    submission = CallSubmission("c", _Client())
    submission.set_book({"patient_id": "P1"})
    state = {"submission": submission, "proposal": {"key": "offer-1", "revision": 0}}
    assert not _needs_disconnect_grace(state)


def test_a_later_open_request_does_not_wait_on_an_earlier_decided_one():
    """A multi-intent call reassigns state["submission"] per request (begin_request);
    the check must read the *active* request, not the root call's aggregate plan."""
    root = CallSubmission("c", _Client())
    root.set_cancel("A1")
    second = root.new_request()
    state = {"submission": second, "proposal": {"key": "offer-1", "revision": 0}}
    assert _needs_disconnect_grace(state)


def test_missing_submission_needs_no_grace():
    assert not _needs_disconnect_grace({})
