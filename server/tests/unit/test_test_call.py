"""Place-test-call from the console must stay off the scored mix.

Remote testers send the same Twilio-shaped /ws as the harness, so the
distinction is customParameters.is_test, not the socket type.
"""

from types import SimpleNamespace

from bot import _is_test_call
from pipecat.runner.types import SmallWebRTCRunnerArguments


def test_webrtc_runner_is_always_a_test_call():
    args = SmallWebRTCRunnerArguments(webrtc_connection=object())
    assert _is_test_call(args) is True


def test_twilio_custom_params_mark_a_console_test_call():
    args = SimpleNamespace(call_data=SimpleNamespace(body={"is_test": "true"}))
    assert _is_test_call(args) is True


def test_a_real_twilio_call_is_not_a_test():
    args = SimpleNamespace(call_data=SimpleNamespace(body={}))
    assert _is_test_call(args) is False


def test_missing_call_data_is_not_a_test():
    assert _is_test_call(SimpleNamespace()) is False
