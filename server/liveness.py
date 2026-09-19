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
so that is spent budget, not cosmetics. So the first stage only speaks — the TTS
handles ``TTSSpeakFrame`` directly and the LLM is never asked twice — and a much
longer silence re-runs the model, which is the case where the request genuinely
never came back.

The two stages are separated by **time from the end of the caller's turn**, not
by a nudge count, because a count is exhausted by the first stage's own
repetition: with a count, the watchdog spoke the filler at T+6 and again at
T+7 (the rule fired on every poll while it was due) and was spent long before
the re-run window opened, so the re-run never happened at all. What the second
stage must survive is the *filler it spoke itself*: ``BotStartedSpeakingFrame``
is how the watchdog learns the bot said something, and the filler raises one of
those too, so that one frame is claimed as our own voice rather than read as the
model answering.
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
#: generation, and the cadence of every re-ask after that. Long on purpose: by
#: then the first request is not coming back.
DEFAULT_RERUN_AFTER_SECS = 18.0

#: How many times one unanswered turn is asked for again. Each re-ask is
#: preceded by a holding line, so a turn the model never answers still ends in
#: at most ``1 + DEFAULT_MAX_RERUNS`` fillers rather than in dead air.
DEFAULT_MAX_RERUNS = 2

#: Spoken when the pipeline stalls. One sentence: it is filler, not content,
#: and it must not push the call toward the wall-clock limit.
DEFAULT_FILLER = "One moment please."


@dataclass(frozen=True)
class NudgeDecision:
    """Whether to speak a holding line now, whether to re-ask the model, and why."""

    nudge: bool
    reason: str
    rerun: bool = False


def decide_nudge(
    *,
    armed_at: float | None,
    now: float,
    bot_speaking: bool,
    silence_secs: float,
    fillers_sent: int,
    max_fillers: int,
    rerun_after_secs: float = DEFAULT_RERUN_AFTER_SECS,
    reruns_sent: int = 0,
    max_reruns: int = DEFAULT_MAX_RERUNS,
) -> NudgeDecision:
    """The watchdog's rule, isolated from the pipeline.

    Armed means the caller finished a turn and the bot owes them a reply. It is
    disarmed the moment the model answers, so the bot is never interrupted while
    the caller is simply thinking.

    The schedule for one turn is fixed and measured from the end of the caller's
    turn: a holding line at ``silence_secs``, and then a holding line plus a
    fresh generation every ``rerun_after_secs``, at most ``max_reruns`` times.
    """
    if armed_at is None:
        return NudgeDecision(False, "not_armed")
    if bot_speaking:
        return NudgeDecision(False, "bot_speaking")
    elapsed = now - armed_at
    if fillers_sent == 0:
        if elapsed < silence_secs:
            return NudgeDecision(False, "within_grace")
        # A first check that already lands past the re-run window asks again in
        # the same breath, so a slow poll loop cannot swallow the second stage.
        return NudgeDecision(True, "silent", rerun=elapsed >= rerun_after_secs)
    if reruns_sent >= max_reruns or fillers_sent >= max_fillers:
        return NudgeDecision(False, "exhausted")
    if elapsed < rerun_after_secs * (reruns_sent + 1):
        return NudgeDecision(False, "within_grace")
    return NudgeDecision(True, "still_silent", rerun=True)


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
        max_reruns: int = DEFAULT_MAX_RERUNS,
        rerun_after_secs: float = DEFAULT_RERUN_AFTER_SECS,
        poll_secs: float = 0.5,
        is_active: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        super().__init__(**kwargs)
        #: Injected so the two stages can be driven to their deadlines in a test
        #: instead of waiting on the wall clock.
        self._clock = clock
        self._silence_secs = silence_secs
        self._filler = filler
        self._max_reruns = max_reruns
        self._rerun_after_secs = rerun_after_secs
        self._poll_secs = poll_secs
        self._is_active = is_active or (lambda: True)
        self._armed_at: float | None = None
        self._bot_speaking = False
        self._fillers_sent = 0
        self._reruns_sent = 0
        self._awaiting_filler_voice = False
        self._stopped = False
        self._monitor_task: asyncio.Task | None = None

    @property
    def nudges_sent(self) -> int:
        return self._fillers_sent

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._start_monitor()
        elif isinstance(frame, (EndFrame, CancelFrame, StopFrame)):
            await self._stop_monitor()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            if self._awaiting_filler_voice:
                # This episode is the holding line the watchdog pushed itself,
                # not the model answering: the turn is still unanswered.
                self._awaiting_filler_voice = False
            else:
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
        self._armed_at = self._clock()
        self._fillers_sent = 0
        self._reruns_sent = 0
        self._awaiting_filler_voice = False

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
            now=self._clock(),
            bot_speaking=self._bot_speaking,
            silence_secs=self._silence_secs,
            fillers_sent=self._fillers_sent,
            max_fillers=1 + self._max_reruns,
            rerun_after_secs=self._rerun_after_secs,
            reruns_sent=self._reruns_sent,
            max_reruns=self._max_reruns,
        )
        if not decision.nudge:
            return
        logger.warning(
            "Silence watchdog {}: holding line {} of {}, {}{}",
            decision.reason,
            self._fillers_sent + 1,
            1 + self._max_reruns,
            "re-asking the model" if decision.rerun else "speaking only",
            f" (re-run {self._reruns_sent + 1} of {self._max_reruns})" if decision.rerun else "",
        )
        # The armed instant is deliberately not reset: the re-run cadence means
        # "this long with nothing from the bot", not "this long since the
        # last filler".
        self._fillers_sent += 1
        self._awaiting_filler_voice = True
        await self.push_frame(TTSSpeakFrame(text=self._filler))
        if decision.rerun:
            self._reruns_sent += 1
            await self.push_frame(LLMRunFrame())

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
