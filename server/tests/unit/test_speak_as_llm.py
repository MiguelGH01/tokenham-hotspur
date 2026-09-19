import asyncio

from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection

from flow.speak import speak_as_llm


class FakeLLM:
    def __init__(self):
        self.frames = []

    async def push_frame(self, frame, direction=None):
        self.frames.append((type(frame), getattr(frame, "text", None), direction, getattr(frame, "append_to_context", None)))


def test_speak_as_llm_emits_a_full_llm_response():
    llm = FakeLLM()
    asyncio.run(speak_as_llm(llm, "Clínica Arenal, how can I help?", in_context=True))
    kinds = [row[0] for row in llm.frames]
    assert kinds == [LLMFullResponseStartFrame, LLMTextFrame, LLMFullResponseEndFrame]
    assert llm.frames[1][1] == "Clínica Arenal, how can I help?"
    assert llm.frames[1][2] == FrameDirection.DOWNSTREAM
    assert llm.frames[1][3] is True
