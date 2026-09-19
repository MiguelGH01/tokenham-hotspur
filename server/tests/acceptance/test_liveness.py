"""The silence watchdog's decision rule.

Agent silence is the one failure the scorer blames on us outright, so the rule
that decides whether to speak is pinned here rather than left to the pipeline.
"""

from liveness import decide_nudge


def _decide(**overrides):
    kwargs = dict(
        armed_at=100.0,
        now=110.0,
        bot_speaking=False,
        silence_secs=6.0,
        nudges_sent=0,
        max_nudges=4,
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


def test_stops_nudging_once_exhausted():
    assert _decide(nudges_sent=4).nudge is False
    assert _decide(nudges_sent=3).nudge is True


def test_the_first_nudge_only_speaks_and_does_not_rerun_the_llm():
    """Re-running the model while a slow answer is still coming duplicates it."""
    decision = _decide(now=110.0, rerun_after_secs=18.0)
    assert decision.nudge is True
    assert decision.rerun is False


def test_a_long_silence_reruns_the_llm():
    """Past the window the first request is not coming back, so ask again."""
    decision = _decide(now=125.0, rerun_after_secs=18.0)
    assert decision.nudge is True
    assert decision.rerun is True
    assert decision.reason == "silent"


def test_the_rerun_window_is_measured_from_the_end_of_the_caller_turn():
    """A filler must not push the re-run further away."""
    assert _decide(now=117.9, rerun_after_secs=18.0).rerun is False
    assert _decide(now=118.0, rerun_after_secs=18.0).rerun is True
