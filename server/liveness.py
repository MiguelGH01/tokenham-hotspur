"""Liveness guards for the voice path.

Two failure modes cost more score than any reasoning error, and both are
attributed to the agent by the scorer:

- ``Agent silence`` — no audible audio for the silence window, so the call is
  cut. Streaming silence is not speaking.
- ``Wall clock`` — the call is capped at three minutes and a late record is no
  record.

The guards live here rather than in the prompt because a prompt cannot promise
audio. ``SilenceWatchdog`` watches the pipeline and, when the caller has
finished a turn and the bot has produced nothing since, speaks a short holding
line. Its decision rule is a pure function so it can be tested without a
pipeline.

The holding line and the re-run are deliberately **two stages**. Re-running the
LLM asks for a second generation, and when the model was merely slow the first
one arrives anyway: the pipeline then says the same thing two or three times and
the caller answers the filler ("Okay, I'll wait."). Turns are capped per case,
so that is spent budget, not cosmetics. So the first nudge only speaks — the TTS
handles ``TTSSpeakFrame`` directly and the LLM is never asked twice — and only a
much longer silence re-runs the model, which is the case where the request
genuinely never came back.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    LLMRunFrame,
    StartFrame,
    StopFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

#: Short enough to fit several times inside a three-minute call, long enough
#: that normal LLM latency (a second or two) never trips it.
DEFAULT_SILENCE_SECS = 6.0

#: How long the bot may be silent before the LLM is asked for a second
#: generation. Long on purpose: by then the first request is not coming back.
DEFAULT_RERUN_AFTER_SECS = 18.0

#: One holding line, and one re-run if the silence outlasts it.
DEFAULT_MAX_NUDGES = 2

#: Spoken when the pipeline stalls. One sentence: it is filler, not content,
#: and it must not push the call toward the wall-clock limit.
DEFAULT_FILLER = "One moment please."


@dataclass(frozen=True)
class NudgeDecision:
    """Whether to speak a holding line now, whether to re-run the LLM, and why."""

    nudge: bool
    reason: str
    rerun: bool = False


def decide_nudge(
    *,
    armed_at: float | None,
    now: float,
    bot_speaking: bool,
    silence_secs: float,
    nudges_sent: int,
    max_nudges: int,
    rerun_after_secs: float = DEFAULT_RERUN_AFTER_SECS,
) -> NudgeDecision:
    """The watchdog's rule, isolated from the pipeline.

    Armed means the caller finished a turn and the bot owes them a reply. It is
    disarmed the moment the bot starts speaking, so the bot is never interrupted
    while the caller is simply thinking.

    ``elapsed`` is measured from the end of the caller's turn and is **not**
    reset by a nudge, so the re-run window means "this long with nothing from
    the bot", not "this long since the last filler".
    """
    if armed_at is None:
        return NudgeDecision(False, "not_armed")
    if bot_speaking:
        return NudgeDecision(False, "bot_speaking")
    if nudges_sent >= max_nudges:
        return NudgeDecision(False, "exhausted")
    elapsed = now - armed_at
    if elapsed < silence_secs:
        return NudgeDecision(False, "within_grace")
    return NudgeDecision(True, "silent", rerun=elapsed >= rerun_after_secs)


class SilenceWatchdog(FrameProcessor):
    """Speak a holding line when the bot stalls after the caller finishes.

    Placed between the user aggregator and the LLM so that both frames it emits
    travel the right way: :class:`~pipecat.frames.frames.TTSSpeakFrame` passes
    through the LLM to the TTS, and
    :class:`~pipecat.frames.frames.LLMRunFrame` reaches the LLM.
    """

    def __init__(
        self,
        *,
        silence_secs: float = DEFAULT_SILENCE_SECS,
        filler: str = DEFAULT_FILLER,
        max_nudges: int = DEFAULT_MAX_NUDGES,
        rerun_after_secs: float = DEFAULT_RERUN_AFTER_SECS,
        poll_secs: float = 0.5,
        is_active: Callable[[], bool] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._silence_secs = silence_secs
        self._filler = filler
        self._max_nudges = max_nudges
        self._rerun_after_secs = rerun_after_secs
        self._poll_secs = poll_secs
        self._is_active = is_active or (lambda: True)
        self._armed_at: float | None = None
        self._bot_speaking = False
        self._nudges_sent = 0
        self._stopped = False
        self._monitor_task: asyncio.Task | None = None

    @property
    def nudges_sent(self) -> int:
        return self._nudges_sent

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._start_monitor()
        elif isinstance(frame, (EndFrame, CancelFrame, StopFrame)):
            await self._stop_monitor()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._armed_at = None
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, (UserStoppedSpeakingFrame, TranscriptionFrame)):
            self._arm()

        await self.push_frame(frame, direction)

    def _arm(self) -> None:
        """The caller has finished a turn, so the bot owes them a reply."""
        if self._stopped or not self._is_active():
            return
        self._armed_at = time.monotonic()

    async def _start_monitor(self) -> None:
        self._monitor_task = self.create_task(self._monitor(), name="silence-watchdog")

    async def _stop_monitor(self) -> None:
        """The pipeline is closing: say nothing more and let the task finish.

        Without this the monitor outlived the pipeline and kept pushing frames —
        it reopened the TTS websocket to speak a filler to a closed socket after
        the call was already over.
        """
        self._stopped = True
        self._armed_at = None
        if self._monitor_task is not None:
            await self.cancel_task(self._monitor_task)
            self._monitor_task = None

    async def _monitor(self) -> None:
        while not self._stopped:
            await self._maybe_nudge()
            await self._sleep(self._poll_secs)

    async def _maybe_nudge(self) -> None:
        if self._stopped:
            return
        decision = decide_nudge(
            armed_at=self._armed_at,
            now=time.monotonic(),
            bot_speaking=self._bot_speaking,
            silence_secs=self._silence_secs,
            nudges_sent=self._nudges_sent,
            max_nudges=self._max_nudges,
            rerun_after_secs=self._rerun_after_secs,
        )
        if not decision.nudge:
            return
        logger.warning(
            "Silence watchdog nudge {} of {} ({}{})",
            self._nudges_sent + 1,
            self._max_nudges,
            decision.reason,
            ", rerunning the LLM" if decision.rerun else "",
        )
        self._nudges_sent += 1
        # The armed instant is deliberately not reset: the re-run window means
        # "this long with nothing from the bot", not "this long since the last
        # filler".
        await self.push_frame(TTSSpeakFrame(text=self._filler))
        if decision.rerun:
            await self.push_frame(LLMRunFrame())

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
