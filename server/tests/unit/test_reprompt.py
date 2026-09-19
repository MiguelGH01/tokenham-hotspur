"""The silence re-prompt must make sense to a caller who did not hear the last message."""

from pipecat.processors.aggregators.llm_context import LLMContext, LLMSpecificMessage

from bot import _is_eval_session, _reprompt
from flow import GREETING
from pipecat.runner.types import EvalRunnerArguments, RunnerArguments

OFFER = "Doctor Martín Sáez at Arenal Sur, Monday 21 September at 09:00. Does that work for you?"


def _context(*assistant_messages: str) -> LLMContext:
    messages = [{"role": "developer", "content": "node instructions"}]
    for text in assistant_messages:
        messages += [{"role": "assistant", "content": text}, {"role": "user", "content": "..."}]
    return LLMContext(messages=messages)


def test_reprompt_repeats_the_offer_not_just_the_closing_question():
    text = _reprompt(_context(GREETING, OFFER))

    assert "Martín Sáez" in text and "09:00" in text and "Does that work for you?" in text


def test_reprompt_uses_the_latest_message():
    text = _reprompt(_context(GREETING, "Could you give me your DNI or NIE?"))

    assert "DNI or NIE" in text and "how can I help" not in text


def test_reprompt_before_anything_was_said_is_the_greeting():
    assert _reprompt(_context()) == GREETING


def test_reprompt_skips_tool_call_messages_without_text():
    context = _context(OFFER)
    context.add_message({"role": "assistant", "content": None, "tool_calls": []})

    assert "Martín Sáez" in _reprompt(context)


def test_reprompt_reads_llm_specific_messages():
    context = LLMContext(
        messages=[
            LLMSpecificMessage(
                llm="google",
                message={"role": "assistant", "content": "Could you give me your DNI or NIE?"},
            )
        ]
    )
    assert "DNI or NIE" in _reprompt(context)


def test_eval_runner_args_are_detected():
    assert _is_eval_session(EvalRunnerArguments())
    assert not _is_eval_session(RunnerArguments())
