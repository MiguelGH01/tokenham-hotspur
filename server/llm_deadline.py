"""A first-token deadline: the line must never go mute on a stalled request.

Post-mortem of our own scored run (`odd/tasks/pr01-06-record-and-liveness.md`,
evidence 2): two of twenty calls went silent for ~36 s and the platform cut them.
``OpenAILLMService#18 processing time 37.216 s`` with no TTFB at all — the
provider accepted the request and never streamed a token. Nothing in the
pipeline was watching for that, so the caller heard nothing at all and the agent
was attributed the silence.

Why not pipecat's own guard. ``retry_on_timeout`` / ``retry_timeout_secs`` wrap
only the await that ends when the response *headers* arrive — before a single
token — and the one retry it performs is re-issued with no deadline at all, so a
stall in the body is not covered by it. So the guard lives here:

- one attempt opens the stream **and** pulls its first chunk under a single
  deadline, so a stall on the headers and a stall in the body both count as
  "no first token";
- a stalled attempt is abandoned (its stream is closed, the socket released) and
  re-issued, up to ``LLM_ATTEMPTS``;
- once the first chunk is in hand the deadline is gone: a slow but *streaming*
  answer is never cut, which is why this is not a request timeout;
- when every attempt stalls, the agent speaks one short line, so the turn ends
  audibly instead of in silence, and the line goes into the context as the
  assistant's reply.

Set ``LLM_FIRST_TOKEN_GUARD=0`` to turn the guard off and fall back to pipecat's
own ``retry_on_timeout`` behaviour.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable

from loguru import logger
from pipecat.frames.frames import LLMTextFrame
from pipecat.services.openai.base_llm import BaseOpenAILLMService
from pipecat.services.openai.llm import OpenAILLMService

import audit


def first_token_timeout_secs() -> float:
    """How long one attempt may produce nothing before it is abandoned."""
    return float(os.getenv("LLM_FIRST_TOKEN_TIMEOUT_SECS", "8"))


def attempts() -> int:
    """How many tries a stalled turn gets, the first one included."""
    return max(1, int(os.getenv("LLM_ATTEMPTS", "2")))


def holding_line() -> str:
    """What the caller hears when every attempt has stalled."""
    return os.getenv("LLM_HOLDING_LINE", "One moment, please.")


def guard_enabled() -> bool:
    return os.getenv("LLM_FIRST_TOKEN_GUARD", "1") != "0"


async def _release(iterator, stream) -> None:
    """Let go of a stalled stream so its socket is not left open.

    Closing the iterator first cascades the cleanup through httpx' internals,
    which is the same order pipecat's own ``_process_context`` closes them in.
    """
    for closer in (
        getattr(iterator, "aclose", None),
        getattr(stream, "close", None) or getattr(stream, "aclose", None),
    ):
        if closer is None:
            continue
        try:
            await closer()
        except Exception:  # a stream that cannot be closed is still abandoned
            logger.debug("stalled stream could not be closed cleanly")


class FirstChunkDeadline:
    """A stream that abandons an attempt which produces no first chunk in time.

    Written against a plain ``open_stream`` callable rather than against the
    service, so the deadline, the retry and the "never cut a streaming answer"
    rule can be exercised without a model, a socket or a clock we have to wait
    on in real time.
    """

    def __init__(
        self,
        open_stream: Callable[[], AsyncIterator],
        *,
        on_stall: Callable[[int], None] | None = None,
        on_exhausted: Callable[[], object] | None = None,
        timeout_secs: Callable[[], float] = first_token_timeout_secs,
        max_attempts: Callable[[], int] = attempts,
    ):
        self._open_stream = open_stream
        self._on_stall = on_stall
        self._on_exhausted = on_exhausted
        self._timeout_secs = timeout_secs
        self._max_attempts = max_attempts

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        timeout = self._timeout_secs()
        for attempt in range(1, self._max_attempts() + 1):
            # Opening the stream sits under the same deadline as its first chunk: a
            # provider that never sends response headers hung here with no deadline at all.
            stream = iterator = None

            async def open_and_pull():
                nonlocal stream, iterator
                stream = await self._open_stream()
                iterator = stream.__aiter__()
                return await iterator.__anext__()

            try:
                first = await asyncio.wait_for(open_and_pull(), timeout)
            except TimeoutError:
                await _release(iterator, stream)
                if self._on_stall is not None:
                    self._on_stall(attempt)
                continue
            except StopAsyncIteration:  # an empty answer is not a stall
                return
            yield first
            # No deadline from here on: the answer is arriving, however slowly.
            async for chunk in iterator:
                yield chunk
            return
        if self._on_exhausted is not None:
            result = self._on_exhausted()
            if asyncio.iscoroutine(result):
                await result

    # pipecat closes the stream it iterated; there is nothing of ours to free.
    async def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class FirstTokenDeadlineLLM(OpenAILLMService):
    """An OpenAI-compatible service whose first chunk must arrive within the deadline."""

    def __init__(self, *args, call_id: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._call_id = call_id or "unknown"

    async def open_stream(self, context):
        """One request with no deadline of its own: this guard owns the deadline.

        pipecat's built-in retry is switched off for that one call, because its
        retry re-issues without any deadline — the 37-second hang again.
        """
        previous = self._retry_on_timeout
        self._retry_on_timeout = False
        try:
            return await BaseOpenAILLMService.get_chat_completions(self, context)
        finally:
            self._retry_on_timeout = previous

    async def get_chat_completions(self, context):
        if not guard_enabled():
            return await super().get_chat_completions(context)
        return FirstChunkDeadline(
            lambda: self.open_stream(context),
            on_stall=self.note_stall,
            on_exhausted=self.speak_holding_line,
        )

    def note_stall(self, attempt: int) -> None:
        """One abandoned attempt: logged, and audited against the call."""
        logger.error(
            "LLM produced no first token within {}s (attempt {})",
            first_token_timeout_secs(),
            attempt,
        )
        audit.audit(
            self._call_id,
            "llm_timeout",
            attempt=attempt,
            timeout_secs=first_token_timeout_secs(),
        )

    async def speak_holding_line(self) -> None:
        """Speak instead of leaving the line mute, and record it as the turn.

        An ``LLMTextFrame`` inside the response window is spoken by the TTS and
        collected by the assistant aggregator, so the caller hears a sentence
        and the next turn starts from a context that knows the agent spoke.
        """
        text = holding_line()
        logger.warning("LLM stalled: speaking the holding line instead of silence")
        audit.audit(self._call_id, "llm_holding_line", text=text)
        await self.push_frame(LLMTextFrame(text))
