"""The yes that books, without spending a model turn on it.

The confirmation the caller gives is the moment the case is decided, so it is
also the moment to submit: waiting for the model to call ``confirm_offer`` costs
a turn of a three-minute clock, and sometimes loses the case to it entirely.
What is asserted here is that the watcher only ever fires on a plan the call had
already read back, and that the model's own later confirm is still a retry.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

from affirmation_watch import AffirmationWatch
from flows.requests import prepare_proposal
from submission import CallSubmission

READBACK = "Dra. Carmen Ortiz Vidal at Arenal Centro, Monday 21 September at 09:00. Does that work?"
OFFER = {
    "patient_id": "P1",
    "provider_id": "PR01",
    "location_id": "centro",
    "appointment_type_id": "review",
    "slot": "2026-09-21T09:00:00+02:00",
    "policy_id": "sanitas",
}


class Client:
    def __init__(self):
        self.posted = []

    async def post_submission(self, payload):
        self.posted.append(payload)


def _manager(*, user_turns, assistant_turns=(), proposal_key="offer-1", **state_extra):
    """A call whose last caller turn is the affirmation.

    The proposal is prepared the way the flow prepares it — before the caller
    answers, snapshotting the turns heard so far — because that snapshot is what
    makes a confirmation a *later* turn.
    """
    messages = []
    for index, text in enumerate(user_turns[:-1]):
        messages.append({"role": "user", "content": text})
        if index < len(assistant_turns):
            messages.append({"role": "assistant", "content": assistant_turns[index]})
    client = Client()
    state = {
        "call_id": "call-1",
        "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
        "intent": "book",
        "client": client,
        "submission": CallSubmission("call-1", client),
        "patient": {"patient_id": "P1", "insurer": "sanitas", "given_name": "Ana"},
        "offers": {"offer-1": dict(OFFER)},
        "revision": 0,
    }
    state.update(state_extra)
    manager = SimpleNamespace(state=state, get_current_context=lambda: messages)
    if proposal_key:
        prepare_proposal(manager, proposal_key)
    messages.append({"role": "user", "content": user_turns[-1]})
    return manager, client


def _watch(manager) -> AffirmationWatch:
    watch = AffirmationWatch()
    watch.bind(manager)
    return watch


def test_a_clean_yes_on_a_readback_submits_the_offer():
    manager, client = _manager(
        user_turns=["I need a review, please.", "Yes, that one please."],
        assistant_turns=[READBACK],
    )
    watch = _watch(manager)
    handler = watch.pending_submission()
    assert handler is not None
    asyncio.run(watch.submit_confirmation(handler))
    assert [p["action"] for p in client.posted] == ["BOOK"]
    assert client.posted[0]["provider_id"] == "PR01"


def test_a_qualified_yes_is_not_a_confirmation():
    manager, client = _manager(
        user_turns=["A review please.", "Yes, but can you check another time?"],
        assistant_turns=[READBACK],
    )
    watch = _watch(manager)
    assert watch.pending_submission() is None
    assert client.posted == []


def test_a_yes_that_answers_no_readback_does_not_confirm():
    """The agent's last turn has to be the question, not a statement."""
    manager, client = _manager(
        user_turns=["A review please.", "Yes."],
        assistant_turns=["Let me look at what is free."],
    )
    watch = _watch(manager)
    assert watch.pending_submission() is None
    assert client.posted == []


def test_nothing_prepared_means_nothing_to_confirm():
    manager, client = _manager(
        user_turns=["A review please.", "Yes, that one please."],
        assistant_turns=[READBACK],
        offers={},
        proposal_key=None,
    )
    watch = _watch(manager)
    assert watch.pending_submission() is None
    assert client.posted == []


def test_one_caller_turn_submits_once():
    """The frames of a single utterance must not each fire a submission."""
    manager, client = _manager(
        user_turns=["A review please.", "Yes, that one please."],
        assistant_turns=[READBACK],
    )
    watch = _watch(manager)
    handler = watch.pending_submission()
    assert handler is not None
    assert watch.pending_submission() is None
    asyncio.run(watch.submit_confirmation(handler))
    assert len(client.posted) == 1


def test_the_models_own_confirm_afterwards_is_a_retry():
    from flows.booking import confirm_offer

    manager, client = _manager(
        user_turns=["A review please.", "Yes, that one please."],
        assistant_turns=[READBACK],
    )
    watch = _watch(manager)
    asyncio.run(watch.submit_confirmation(watch.pending_submission()))
    result, node = asyncio.run(confirm_offer({"offer_id": "offer-1"}, manager))
    assert result["status"] == "accepted"
    assert node["name"] == "request_complete"
    assert [p["action"] for p in client.posted] == ["BOOK"]


def test_a_frozen_plan_is_left_alone():
    manager, client = _manager(
        user_turns=["A review please.", "Yes, that one please."],
        assistant_turns=[READBACK],
    )
    manager.state["submission"].set_book(OFFER)
    asyncio.run(manager.state["submission"].flush())
    watch = _watch(manager)
    assert watch.pending_submission() is None
    assert len(client.posted) == 1


def test_a_registration_readback_confirms_the_draft():
    draft = {
        "given_name": "Ana",
        "first_surname": "Test",
        "second_surname": "Example",
        "national_id": "12345678Z",
        "date_of_birth": "1990-01-02",
        "phone": "612345678",
        "email": "ana@example.com",
        "insurer": "sanitas",
    }
    manager, client = _manager(
        user_turns=["I am new here.", "Yes, all of that is right."],
        assistant_turns=["Is everything correct?"],
        intent="register",
        offers={},
        proposal_key="registration",
        registration_draft=draft,
    )
    watch = _watch(manager)
    asyncio.run(watch.submit_confirmation(watch.pending_submission()))
    assert [p["action"] for p in client.posted] == ["REGISTER"]


def test_the_caller_turn_arrives_with_the_aggregator_event():
    """The trigger carries the caller's words, and the write precedes it.

    The user aggregator writes the turn to the context and *then* raises
    ``on_user_turn_message_added``, so the event's text and the context agree —
    which is what lets the deterministic gate see the yes as a later turn.
    """
    manager, client = _manager(
        user_turns=["A review please.", "Yes, that one please."],
        assistant_turns=[READBACK],
    )
    watch = _watch(manager)
    handler = watch.pending_submission("Yes, that one please.")
    assert handler is not None
    asyncio.run(watch.submit_confirmation(handler))
    assert [p["action"] for p in client.posted] == ["BOOK"]


def test_the_same_turn_is_never_confirmed_twice():
    manager, client = _manager(
        user_turns=["A review please.", "Yes, that one please."],
        assistant_turns=[READBACK],
    )

    async def run() -> AffirmationWatch:
        watch = _watch(manager)
        watch.consider("Yes, that one please.")
        assert watch.pending_submission("Yes, that one please.") is None
        for task in list(watch._tasks):
            await task
        return watch

    asyncio.run(run())
    assert [p["action"] for p in client.posted] == ["BOOK"]


def test_a_second_readback_gets_its_own_confirmation():
    """The same words after a new read-back are a new consent, not a duplicate."""
    manager, client = _manager(
        user_turns=["A review please.", "Yes, that one please."],
        assistant_turns=[READBACK],
    )
    watch = _watch(manager)
    assert watch.pending_submission("Yes, that one please.") is not None
    manager.get_current_context().append({"role": "assistant", "content": "Is 11:45 alright?"})
    assert watch.pending_submission("Yes, that one please.") is not None


def test_an_unbound_watcher_does_nothing():
    watch = AffirmationWatch()
    assert watch.pending_submission() is None
    watch.consider("Yes.")  # no conversation, no crash
