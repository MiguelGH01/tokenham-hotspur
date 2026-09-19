"""What each service consumed on a call must land in that call's audit trail."""

import asyncio
import json

from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import (
    LLMTokenUsage,
    LLMUsageMetricsData,
    STTUsage,
    STTUsageMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

import call_cost
from call_metrics import build_observer, call_ended, call_started


def _push(observer, *metrics):
    frame = MetricsFrame(data=list(metrics))
    pushed = FramePushed(
        source=None, destination=None, frame=frame,
        direction=FrameDirection.DOWNSTREAM, timestamp=0,
    )
    asyncio.run(observer.on_push_frame(pushed))


def _events(tmp_path, call_id):
    path = tmp_path / f"audit-{call_id}.ndjson"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_usage_and_latency_are_written_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    _push(
        build_observer("call-a"),
        STTUsageMetricsData(processor="stt#0", model="stt-rt-v5", value=STTUsage(audio_seconds=2.5)),
        LLMUsageMetricsData(
            processor="llm#0", model="gemini-3.6-flash",
            value=LLMTokenUsage(prompt_tokens=1200, completion_tokens=40, total_tokens=1240),
        ),
        TTSUsageMetricsData(processor="tts#0", model="eleven_v3", value=88),
        TTFBMetricsData(processor="llm#0", model="gemini-3.6-flash", value=0.42),
    )

    events = _events(tmp_path, "call-a")
    usage = {e["kind"]: e for e in events if e["event"] == "service_usage"}
    assert usage["stt"]["audio_seconds"] == 2.5 and usage["stt"]["model"] == "stt-rt-v5"
    assert usage["llm"]["prompt_tokens"] == 1200 and usage["llm"]["completion_tokens"] == 40
    assert usage["tts"]["characters"] == 88 and usage["tts"]["processor"] == "tts#0"
    assert "characters" not in usage["llm"]  # unset fields are left out, not written as null

    (latency,) = [e for e in events if e["event"] == "service_latency"]
    assert latency["kind"] == "ttfb" and latency["seconds"] == 0.42


def test_what_the_bot_writes_is_what_call_cost_reads(tmp_path, monkeypatch):
    """The seam: rename an event or a field on one side and this is the test that fails."""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    call_started("call-a")
    _push(build_observer("call-a"), TTSUsageMetricsData(processor="tts#0", model="eleven_v3", value=1000))
    call_ended("call-a")

    (call,) = call_cost.load(tmp_path)
    assert call.eur > 0 and call.duration_s is not None and call.unpriced == ()


def test_calls_do_not_share_a_trail(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    _push(build_observer("call-a"), TTSUsageMetricsData(processor="tts#0", value=10))
    _push(build_observer("call-b"), TTSUsageMetricsData(processor="tts#0", value=20))
    assert [e["characters"] for e in _events(tmp_path, "call-a")] == [10]
    assert [e["characters"] for e in _events(tmp_path, "call-b")] == [20]


def test_an_unwritable_audit_dir_never_reaches_the_pipeline(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("AUDIT_DIR", str(blocker))
    _push(build_observer("call-a"), TTSUsageMetricsData(processor="tts#0", value=10))  # must not raise
