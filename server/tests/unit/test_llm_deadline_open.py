"""The first-token deadline must also cover a request that never opens.

Seen live on 19-Sep with Helmcode: 37 s with no response headers at all, and the
guard never fired, because its deadline only started once the stream was open.
"""

import asyncio

from llm_deadline import FirstChunkDeadline


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def close(self):
        self.closed = True


def _collect(stream) -> list[str]:
    async def drain():
        return [chunk async for chunk in stream]

    return asyncio.run(drain())


def test_a_request_that_never_opens_is_retried():
    attempts = []

    async def open_stream():
        attempts.append(True)
        if len(attempts) == 1:
            await asyncio.sleep(30)  # the provider accepts the request and says nothing
        return Stream(["recovered"])

    stalls = []
    stream = FirstChunkDeadline(open_stream, timeout_secs=lambda: 0.01, on_stall=stalls.append)
    assert _collect(stream) == ["recovered"]
    assert stalls == [1]
    assert len(attempts) == 2


def test_every_attempt_failing_to_open_ends_in_the_holding_line():
    async def open_stream():
        await asyncio.sleep(30)

    exhausted = []
    stream = FirstChunkDeadline(
        open_stream,
        timeout_secs=lambda: 0.01,
        max_attempts=lambda: 2,
        on_exhausted=lambda: exhausted.append(True),
    )
    assert _collect(stream) == []
    assert exhausted == [True]
