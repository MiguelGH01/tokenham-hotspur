"""A red flag escalates even when the model does not notice it.

``flag_emergency`` is a tool the model has to choose to call. This watcher asks
Jev about every caller turn on its own, and on a yes runs that same tool. What is
asserted here: a yes escalates once, down the model's own path; a no, a slow Jev
or a broken Jev changes nothing about the call.
"""

import asyncio

import httpx
from pipecat.frames.frames import InterruptionFrame

import emergency_watch
from emergency_watch import EmergencyWatch, ask_jev


class Submission:
    def __init__(self):
        self.escalated = None

    def set_escalate(self, reason):
        self.escalated = reason


class Manager:
    def __init__(self, *turns):
        self.state = {"submission": Submission(), "call_id": "c1"}
        self.nodes = []
        self._turns = turns

    def get_current_context(self):
        return [{"role": "user", "content": text} for text in self._turns]

    async def set_node_from_config(self, node):
        self.nodes.append(node["name"])


class Worker:
    def __init__(self):
        self.frames = []

    async def queue_frames(self, frames):
        self.frames.extend(frames)


def _watch(ask, *turns):
    manager, worker = Manager(*turns), Worker()
    watch = EmergencyWatch("c1", ask=ask)
    watch.bind(manager, worker)
    return watch, manager, worker


def _answering(probability):
    async def ask(text):
        return probability

    return ask


def test_a_red_flag_escalates_down_the_models_own_path():
    watch, manager, worker = _watch(_answering(0.97), "me aprieta el pecho y me cuesta respirar")
    asyncio.run(watch.check())
    assert manager.state["submission"].escalated == "medical_emergency"
    assert manager.nodes == ["emergency"]
    assert isinstance(worker.frames[0], InterruptionFrame)  # the reply in flight is cut


def test_an_ordinary_complaint_changes_nothing():
    watch, manager, worker = _watch(_answering(0.07), "me duele el pecho al toser, quería cita")
    asyncio.run(watch.check())
    assert manager.state["submission"].escalated is None
    assert manager.nodes == [] and worker.frames == []


def test_jev_being_down_never_breaks_the_call():
    async def ask(text):
        raise RuntimeError("boom")

    for broken in (ask, _answering(None)):
        watch, manager, _ = _watch(broken, "me aprieta el pecho y me cuesta respirar")
        asyncio.run(watch.check())
        assert manager.state["submission"].escalated is None


def test_it_escalates_once():
    watch, manager, _ = _watch(_answering(0.97), "no puedo respirar")
    asyncio.run(watch.check())
    asyncio.run(watch.check())
    assert manager.nodes == ["emergency"]


def test_it_stands_down_when_the_model_already_escalated():
    watch, manager, worker = _watch(_answering(0.97), "no puedo respirar")
    manager.current_node = "emergency"
    asyncio.run(watch.check())
    assert manager.nodes == [] and worker.frames == []


def test_symptoms_split_over_two_turns_are_asked_together():
    heard = []

    async def ask(text):
        heard.append(text)
        return 0.0

    watch, _, _ = _watch(ask, "me aprieta el pecho", "y me cuesta respirar")
    asyncio.run(watch.check())
    assert heard == ["me aprieta el pecho y me cuesta respirar"]


def test_without_a_key_the_watcher_is_off(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    watch = EmergencyWatch("c1")
    assert not watch.enabled
    watch.consider()  # no loop, no client, no error


def _jev(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://jev.test")


def test_ask_jev_reads_the_probability_off_the_wire():
    def handler(request):
        assert request.url.path == "/v1/systemone"
        return httpx.Response(200, json={"answers": {"emergency": {"type": "noul", "noul": 0.97}}})

    assert asyncio.run(ask_jev(_jev(handler), "x")) == 0.97


def test_ask_jev_answers_nothing_when_jev_fails_or_is_slow():
    def failing(request):
        return httpx.Response(500)

    def slow(request):
        raise httpx.ReadTimeout("slow")

    assert asyncio.run(ask_jev(_jev(failing), "x")) is None
    assert asyncio.run(ask_jev(_jev(slow), "x")) is None


def test_the_question_names_the_five_published_red_flags():
    text = emergency_watch.QUESTION["instructions"].lower()
    for cue in ("chest", "face droop", "breathless", "bleeding", "head"):
        assert cue in text
