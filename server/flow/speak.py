"""Push spoken text as an LLM response so text-mode eval sees `llm_response`.

``TTSSpeakFrame`` only produces TTS events. In text eval those are dropped
(``skip_tts``), so a greeting spoken that way never matches turn 0.
"""

from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection


async def speak_as_llm(sink, text: str, *, in_context: bool = True) -> None:
    start = LLMFullResponseStartFrame()
    body = LLMTextFrame(text=text)
    body.append_to_context = in_context
    end = LLMFullResponseEndFrame()
    push = getattr(sink, "push_frame", None)
    if push is not None:
        await push(start, FrameDirection.DOWNSTREAM)
        await push(body, FrameDirection.DOWNSTREAM)
        await push(end, FrameDirection.DOWNSTREAM)
        return
    await sink.queue_frames([start, body, end])
