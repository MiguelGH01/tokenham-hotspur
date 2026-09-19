"""The LLM deadline: a stalled request must not become dead air.

A silent call is attributed to the agent and can cost the case outright, so the
turn has to end in something audible. What is asserted here is the timing rule
itself — abandon an attempt that produces no *first chunk*, retry, never cut an
answer that is already streaming — without waiting on a real clock.
"""

import asyncio

import pytest

import llm_deadline
from llm_deadline import FirstChunkDeadline, FirstTokenDeadlineLLM


class Stream:
    """A stream that yields ``chunks``, then stalls for ``stall_after`` if told to."""

    def __init__(self, chunks, *, stall_after=None, close_records=None):
        self.chunks = list(chunks)
        self.stall_after = stall_after
        self.closed = False
        self._close_records = close_records

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for index, chunk in enumerate(self.chunks):
            if self.stall_after is not None and index == self.stall_after:
                await asyncio.sleep(30)  # never returns inside the test's deadline
            yield chunk
        if self.stall_after == len(self.chunks):
            await asyncio.sleep(30)

    async def close(self):
        self.closed = True
        if self._close_records is not None:
            self._close_records.append(self)


def run(coro):
    return asyncio.run(coro)


def collect(stream) -> list[str]:
    async def drain():
        return [chunk async for chunk in stream]

    return run(drain())


def test_a_healthy_stream_is_passed_through_untouched():
    async def open_stream():
        return Stream(["a", "b", "c"])

    stream = FirstChunkDeadline(open_stream)
    assert collect(stream) == ["a", "b", "c"]


def test_a_stalled_first_chunk_is_retried_and_the_retry_wins():
    attempts = []

    async def open_stream():
        attempts.append(True)
        if len(attempts) == 1:
            return Stream([], stall_after=0)
        return Stream(["recovered"])

    stalls = []
    stream = FirstChunkDeadline(open_stream, timeout_secs=lambda: 0.01, on_stall=stalls.append)
    assert collect(stream) == ["recovered"]
    assert stalls == [1]
    assert len(attempts) == 2


def test_a_slow_but_streaming_answer_is_never_cut():
    """The deadline covers the first chunk only: the rest may take as long as it takes."""
    slices = 3

    async def open_stream():
        async def slow():
            for index in range(slices):
                await asyncio.sleep(0.05 if index else 0)
                yield f"chunk-{index}"

        class SlowStream:
            def __aiter__(self):
                return slow()

        return SlowStream()

    stream = FirstChunkDeadline(open_stream, timeout_secs=lambda: 0.01)
    assert collect(stream) == ["chunk-0", "chunk-1", "chunk-2"]


def test_every_attempt_stalling_ends_in_the_holding_line():
    spoken = []

    async def open_stream():
        return Stream([], stall_after=0)

    stream = FirstChunkDeadline(
        open_stream,
        timeout_secs=lambda: 0.01,
        max_attempts=lambda: 3,
        on_stall=lambda attempt: spoken.append(("stall", attempt)),
        on_exhausted=lambda: spoken.append(("holding", None)),
    )
    assert collect(stream) == []
    assert spoken == [("stall", 1), ("stall", 2), ("stall", 3), ("holding", None)]


def test_a_stalled_stream_is_closed_before_the_retry():
    """An abandoned request must not leave its socket open."""
    released = []
    attempts = []

    async def open_stream():
        attempts.append(True)
        if len(attempts) == 1:
            return Stream([], stall_after=0, close_records=released)
        return Stream(["ok"])

    stream = FirstChunkDeadline(open_stream, timeout_secs=lambda: 0.01)
    assert collect(stream) == ["ok"]
    assert [s.closed for s in released] == [True]


def test_an_empty_answer_is_not_treated_as_a_stall():
    attempts = []

    async def open_stream():
        attempts.append(True)
        return Stream([])

    stalls = []
    stream = FirstChunkDeadline(open_stream, on_stall=stalls.append)
    assert collect(stream) == []
    assert stalls == []
    assert len(attempts) == 1


def test_the_service_wraps_its_own_stream_when_the_guard_is_on(monkeypatch):
    monkeypatch.setenv("LLM_FIRST_TOKEN_GUARD", "1")
    service = FirstTokenDeadlineLLM(api_key="test", call_id="call-1")
    calls = []

    async def open_stream(context):
        calls.append(context)
        return Stream(["hello"])

    monkeypatch.setattr(service, "open_stream", open_stream)
    stream = run(service.get_chat_completions(context="ctx"))
    assert isinstance(stream, FirstChunkDeadline)
    assert collect(stream) == ["hello"]
    assert calls == ["ctx"]


def test_the_guard_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("LLM_FIRST_TOKEN_GUARD", "0")
    service = FirstTokenDeadlineLLM(api_key="test", call_id="call-1")
    seen = {}

    async def parent_get_chat_completions(context):
        seen["context"] = context
        return "the built-in path"

    monkeypatch.setattr(
        "pipecat.services.openai.llm.OpenAILLMService.get_chat_completions",
        lambda self, context: parent_get_chat_completions(context),
    )
    assert run(service.get_chat_completions(context="ctx")) == "the built-in path"
    assert seen == {"context": "ctx"}


def test_the_holding_line_is_spoken_as_the_agent_turn(monkeypatch):
    monkeypatch.setenv("LLM_HOLDING_LINE", "Un momento, por favor.")
    pushed = []
    service = FirstTokenDeadlineLLM(api_key="test", call_id="call-1")

    async def capture(frame):
        pushed.append(frame)

    monkeypatch.setattr(service, "push_frame", capture)
    run(service.speak_holding_line())
    assert [frame.text for frame in pushed] == ["Un momento, por favor."]
    assert pushed[0].__class__.__name__ == "LLMTextFrame"


@pytest.mark.parametrize("attempts_value,expected", [("1", 1), ("3", 3), ("0", 1)])
def test_attempts_never_drop_below_one(monkeypatch, attempts_value, expected):
    monkeypatch.setenv("LLM_ATTEMPTS", attempts_value)
    assert llm_deadline.attempts() == expected
