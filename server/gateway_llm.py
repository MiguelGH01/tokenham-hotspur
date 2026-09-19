"""OpenAI-compatible LLM service that survives a gateway stream stalling mid-response.

Pipecat's ``retry_on_timeout`` only guards *opening* the stream. Once the first chunk
arrives nothing bounds it: the SDK's default read timeout is 600s, so a gateway that goes
quiet mid-response leaves the bot mute for the rest of the call.
"""

import asyncio

import httpx
from loguru import logger
from openai import APITimeoutError
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.utils.http import TIMEOUT_EXCEPTIONS

# The OpenAI SDK wraps a mid-stream read timeout in APITimeoutError, which Pipecat's
# TIMEOUT_EXCEPTIONS (raw transport errors only) does not include.
STALL_EXCEPTIONS = (*TIMEOUT_EXCEPTIONS, APITimeoutError)

# Longest healthy gap between chunks measured on the gateway was 1.15s (reasoning streams).
STALL_TIMEOUT_SECS = 4.0


def _has_output(chunk) -> bool:
    """True once the model has started speaking or calling a tool; reasoning does not count."""
    return any(c.delta and (c.delta.content or c.delta.tool_calls) for c in chunk.choices)


class _ResponseSummary:
    """What one completion produced, for the log line that explains a mute turn."""

    def __init__(self):
        self.text_chars, self.tools, self.finish = 0, [], None

    def add(self, chunk) -> None:
        for choice in chunk.choices:
            self.finish = getattr(choice, "finish_reason", None) or self.finish
            if not choice.delta:
                continue
            self.text_chars += len(choice.delta.content or "")
            for call in choice.delta.tool_calls or []:
                name = getattr(getattr(call, "function", None), "name", None)
                if name:
                    self.tools.append(name)

    def __str__(self) -> str:
        return f"finish={self.finish} text_chars={self.text_chars} tools={self.tools}"


class StallGuardedLLMService(OpenAILLMService):
    def create_client(self, **kwargs):
        return super().create_client(**kwargs).with_options(timeout=httpx.Timeout(STALL_TIMEOUT_SECS))

    async def get_chat_completions(self, context):
        return self._restart_if_stalled(context)

    async def _restart_if_stalled(self, context):
        for attempt in (1, 2):
            stream = await super().get_chat_completions(context)
            has_output = False
            seen = _ResponseSummary()
            try:
                async for chunk in stream:
                    has_output = has_output or _has_output(chunk)
                    seen.add(chunk)
                    yield chunk
                # A turn that ends with neither speech nor a tool call leaves the bot mute.
                log = logger.debug if has_output else logger.warning
                log("{}: LLM response {}", self, seen)
                return
            except (asyncio.CancelledError, GeneratorExit):
                # Pipecat drops the tool call of a cancelled completion without logging anything.
                logger.warning("{}: LLM response cancelled mid-stream after {}", self, seen)
                raise
            except STALL_EXCEPTIONS:
                # Restarting after output would repeat speech or corrupt tool-call arguments.
                if has_output or attempt == 2:
                    raise
                logger.warning("{}: LLM stream stalled for {}s, retrying", self, STALL_TIMEOUT_SECS)
            finally:
                await stream.close()
