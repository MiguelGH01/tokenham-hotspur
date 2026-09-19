"""A gateway stream that stalls mid-response must fail fast and be retried once."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError
from pipecat.services.openai.llm import OpenAILLMService

from gateway_llm import STALL_TIMEOUT_SECS, StallGuardedLLMService


def _chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


REASONING = _chunk()  # reasoning deltas carry neither content nor tool calls


# The OpenAI SDK wraps a mid-stream read timeout in its own error; other SDK builds
# surface the raw transport exception. Both must count as a stall.
SDK_STALL = APITimeoutError(request=httpx.Request("POST", "http://gateway/v1/chat/completions"))
RAW_STALL = httpx.ReadTimeout("no bytes from the gateway")


class FakeStream:
    def __init__(self, chunks, stalls=False, error=SDK_STALL):
        self._chunks, self._stalls, self._error, self.closed = chunks, stalls, error, False

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._stalls:
            raise self._error

    async def close(self):
        self.closed = True


def _collect(monkeypatch, streams):
    pending = list(streams)

    async def next_stream(self, context):
        return pending.pop(0)

    monkeypatch.setattr(OpenAILLMService, "get_chat_completions", next_stream)
    llm = StallGuardedLLMService(api_key="test")

    async def run():
        return [chunk async for chunk in await llm.get_chat_completions(context=None)]

    return asyncio.run(run())


@pytest.mark.parametrize("error", [SDK_STALL, RAW_STALL])
def test_stall_before_any_output_is_retried(monkeypatch, error):
    stalled = FakeStream([REASONING], stalls=True, error=error)
    answer = _chunk(content="● One moment.")

    chunks = _collect(monkeypatch, [stalled, FakeStream([answer])])

    assert chunks[-1] is answer
    assert stalled.closed


def test_stall_after_spoken_output_is_not_retried(monkeypatch):
    # Retrying here would make the bot repeat what it already said.
    streams = [FakeStream([_chunk(content="● Thanks, ")], stalls=True), FakeStream([])]

    with pytest.raises(APITimeoutError):
        _collect(monkeypatch, streams)


def test_stall_mid_tool_call_is_not_retried(monkeypatch):
    streams = [FakeStream([_chunk(tool_calls=[object()])], stalls=True), FakeStream([])]

    with pytest.raises(APITimeoutError):
        _collect(monkeypatch, streams)


def test_second_stall_gives_up(monkeypatch):
    streams = [FakeStream([REASONING], stalls=True), FakeStream([REASONING], stalls=True)]

    with pytest.raises(APITimeoutError):
        _collect(monkeypatch, streams)


def test_client_read_timeout_is_bounded():
    llm = StallGuardedLLMService(api_key="test")

    assert llm._client.timeout.read == STALL_TIMEOUT_SECS
