"""The silence watchdog: the rule that decides to speak, and the two stages.

Agent silence is the one failure the scorer blames on us outright, so the rule
that decides whether to speak is pinned here rather than left to the pipeline.
The pipeline wiring is pinned too, because the rule can be right while the
wiring makes one of its stages unreachable.
"""

import asyncio

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMRunFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from liveness import SilenceWatchdog, decide_nudge


def run(coro):
    return asyncio.run(coro)


def _decide(**overrides):
    kwargs = dict(
        armed_at=100.0,
        now=110.0,
        bot_speaking=False,
        silence_secs=6.0,
        fillers_sent=0,
        max_fillers=3,
    )
    kwargs.update(overrides)
    return decide_nudge(**kwargs)


def test_not_armed_never_speaks():
    assert _decide(armed_at=None).nudge is False


def test_within_grace_leaves_the_llm_alone():
    assert _decide(now=105.0).nudge is False


def test_armed_and_silent_speaks():
    decision = _decide()
    assert decision.nudge is True
    assert decision.reason == "silent"


def test_never_interrupts_the_bot_mid_sentence():
    assert _decide(bot_speaking=True).nudge is False


def test_stops_re_asking_once_the_budget_is_spent():
    assert _decide(fillers_sent=1, reruns_sent=2, now=200.0).nudge is False
    assert _decide(fillers_sent=1, reruns_sent=1, now=200.0).nudge is True
    assert _decide(fillers_sent=3, reruns_sent=1, now=200.0).nudge is False


def test_the_first_nudge_only_speaks_and_does_not_rerun_the_llm():
    """Re-asking the model while a slow answer is still coming duplicates it."""
    decision = _decide(now=110.0, rerun_after_secs=18.0)
    assert decision.nudge is True
    assert decision.rerun is False


def test_a_long_silence_reruns_the_llm():
    """Past the window the first request is not coming back, so ask again."""
    decision = _decide(fillers_sent=1, now=125.0, rerun_after_secs=18.0)
    assert decision.nudge is True
    assert decision.rerun is True
    assert decision.reason == "still_silent"


def test_the_rerun_cadence_is_measured_from_the_end_of_the_caller_turn():
    """A filler must not push the re-run further away."""
    assert _decide(fillers_sent=1, now=117.9, rerun_after_secs=18.0).rerun is False
    assert _decide(fillers_sent=1, now=118.0, rerun_after_secs=18.0).rerun is True
    # The second re-ask is one cadence further on, still from the same instant.
    assert _decide(fillers_sent=2, reruns_sent=1, now=135.9, rerun_after_secs=18.0).rerun is False
    assert _decide(fillers_sent=2, reruns_sent=1, now=136.0, rerun_after_secs=18.0).rerun is True


class _Clock:
    """A clock the test drives, so a stage deadline costs no wall-clock time."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _watchdog(clock, **kwargs):
    """A watchdog with no pipeline behind it: what it pushes is what it said."""
    watch = SilenceWatchdog(clock=clock, **kwargs)
    pushed = []

    async def record(frame, direction=None):
        pushed.append(frame)

    watch.push_frame = record
    return watch, pushed


def _caller_finished_a_turn(watch):
    frame = TranscriptionFrame(text="the GP please", user_id="caller", timestamp="0")
    return watch.process_frame(frame, FrameDirection.DOWNSTREAM)


def _filler_count(pushed) -> int:
    return sum(1 for frame in pushed if isinstance(frame, TTSSpeakFrame))


def _rerun_count(pushed) -> int:
    return sum(1 for frame in pushed if isinstance(frame, LLMRunFrame))


def test_the_filler_the_watchdog_spoke_does_not_cancel_its_own_rerun():
    """The second stage must survive the first one's voice.

    ``BotStartedSpeakingFrame`` is how the watchdog learns the bot said
    something, and the filler it pushes raises one of those too. Disarming on it
    collapsed the two stages into one: the caller heard a single holding line and
    the request that never came back was never asked for again.
    """
    clock = _Clock()
    watch, pushed = _watchdog(clock, silence_secs=6.0, rerun_after_secs=18.0)

    run(_caller_finished_a_turn(watch))
    clock.advance(6.1)
    run(watch._maybe_nudge())
    assert _filler_count(pushed) == 1

    # The TTS speaks the filler: the bot is talking, and then it is not.
    run(watch.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM))
    run(watch.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM))

    clock.advance(12.0)  # T+18: the model still has produced nothing
    run(watch._maybe_nudge())
    assert _rerun_count(pushed) == 1


def test_a_second_voice_is_the_answer_and_stops_the_watchdog():
    """Once the model does speak, the turn is answered and nothing is re-run."""
    clock = _Clock()
    watch, pushed = _watchdog(clock, silence_secs=6.0, rerun_after_secs=18.0)

    run(_caller_finished_a_turn(watch))
    clock.advance(6.1)
    run(watch._maybe_nudge())
    run(watch.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM))
    run(watch.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM))

    # The model's answer arrives and is spoken: this is the second episode.
    clock.advance(3.0)
    run(watch.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM))
    run(watch.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM))

    clock.advance(20.0)
    run(watch._maybe_nudge())
    assert _rerun_count(pushed) == 0
    assert _filler_count(pushed) == 1


def test_the_filler_is_spoken_once_per_caller_turn():
    """A filler repeated back to back is spent turn budget, not liveness.

    With a nudge *count* the rule stayed due on every poll and spoke the same
    sentence twice within a second, spending the whole budget before the re-run
    window opened.
    """
    clock = _Clock()
    watch, pushed = _watchdog(clock, silence_secs=6.0, rerun_after_secs=18.0, max_reruns=2)

    run(_caller_finished_a_turn(watch))
    for _ in range(40):
        clock.advance(1.0)
        run(watch._maybe_nudge())
    # One at T+6, then one before each of the two re-asks: never two together.
    assert _filler_count(pushed) == 3


def test_a_turn_the_model_never_answers_is_asked_for_again():
    """The whole schedule for one unanswered turn, at 1 s granularity."""
    clock = _Clock()
    watch, pushed = _watchdog(clock, silence_secs=6.0, rerun_after_secs=18.0, max_reruns=2)

    run(_caller_finished_a_turn(watch))
    for _ in range(60):
        clock.advance(1.0)
        run(watch._maybe_nudge())
    assert _filler_count(pushed) == 3
    assert _rerun_count(pushed) == 2
