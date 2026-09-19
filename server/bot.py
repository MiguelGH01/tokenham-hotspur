"""Clínica Arenal receptionist — Pipecat cascade pipeline + Flows graph (see handlers.py).

Run the bot using::

    uv run bot.py            # all real transports
    uv run bot.py -t eval    # headless eval server for `pipecat eval run`
"""

import os
import uuid
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.evals.transport import EvalTransportParams
from pipecat.flows import FlowManager
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import EvalRunnerArguments, RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport, parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

try:
    from pipecat.transports.daily.transport import DailyParams
except ImportError:  # daily-python has no Windows wheels
    DailyParams = None
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from booking import MADRID
from clinic.clinic_client import ClinicClient
from flow import GREETING, create_identify_node
from gateway_llm import StallGuardedLLMService
from submission import CallSubmission

load_dotenv(override=True)

# The platform cuts a call after ~30-35s without audible agent audio (SC-silence-cut) and
# the simulated caller takes a median of 11s (max measured 42.5s) to answer. Counted from when the bot
# STOPS speaking; 16s here is ~20s from when it started asking.
REPROMPT_AFTER_SILENCE_SECS = 16.0


def _is_twilio_session(runner_args: RunnerArguments) -> bool:
    return (
        isinstance(runner_args, WebSocketRunnerArguments)
        and getattr(runner_args, "transport_type", None) == "twilio"
    )


def _call_id(runner_args: RunnerArguments) -> str:
    call_data = getattr(runner_args, "call_data", None)
    if call_data and call_data.call_id:
        return call_data.call_id
    return f"local-{uuid.uuid4().hex[:12]}"


def _connected_at(allow_override: bool) -> datetime:
    """CALL_CLOCK_OVERRIDE pins the clock for evals only: left exported in a shell, it would
    make every scored call search from a past date and book the wrong slot."""
    override = os.getenv("CALL_CLOCK_OVERRIDE")
    if override and allow_override:
        return datetime.fromisoformat(override)
    if override:
        logger.warning("Ignoring CALL_CLOCK_OVERRIDE={}: not an eval session", override)
    return datetime.now(MADRID)


def _reprompt(context: LLMContext) -> str:
    """Repeat the bot's last message so silence never outlasts the platform's window.

    The whole message, not just its closing question: a caller who missed the offer cannot
    answer a bare "Does that work for you?".
    """
    spoken = [
        m["content"]
        for m in context.get_messages()
        if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip()
    ]
    return f"Sorry, are you still there? {spoken[-1]}" if spoken else GREETING


def build_llm():
    provider = os.getenv("LLM_PROVIDER", "helmcode")
    if provider == "helmcode":  # OpenAI-compatible gateway (chat completions)
        return StallGuardedLLMService(
            api_key=os.environ["HELMCODE_API_KEY"],
            base_url=os.getenv("HELMCODE_BASE_URL", "https://api.helmcode.com/v1"),
            settings=StallGuardedLLMService.Settings(model=os.getenv("HELMCODE_MODEL", "deepseek-v4-flash")),
            # ~8% of gateway requests hang with no response; normal TTFB is ~0.55s. This covers
            # opening the stream; StallGuardedLLMService covers a stream that dies mid-response.
            retry_on_timeout=True,
            retry_timeout_secs=3.0,
        )
    if provider == "gemini":
        return GoogleLLMService(
            api_key=os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"],
            settings=GoogleLLMService.Settings(model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash")),
        )
    return OpenAIResponsesLLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAIResponsesLLMService.Settings(model=os.getenv("OPENAI_MODEL", "gpt-4.1")),
    )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    call_id = _call_id(runner_args)
    logger.info("Starting bot for call {}", call_id)

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramTTSService.Settings(voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-helena-en")),
    )
    llm = build_llm()

    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_idle_timeout=REPROMPT_AFTER_SILENCE_SECS,
            # filter_incomplete_user_turns stays off: the LLM judged a bare "Hello." as an
            # unfinished turn and went silent for 10s (16 of 21 calls in the first Run All).
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]
            ),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    pipeline_params = {"enable_metrics": True, "enable_usage_metrics": True}
    if _is_twilio_session(runner_args):
        pipeline_params["audio_in_sample_rate"] = 8000
        pipeline_params["audio_out_sample_rate"] = 8000

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(**pipeline_params),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
        observers=[],
    )
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    flow_manager = FlowManager(
        worker=worker,
        llm=llm,
        context_aggregator=context_aggregator,
        transport=transport,
    )
    client = ClinicClient()
    submission = CallSubmission(call_id, client)
    flow_manager.state.update(
        {
            "call_id": call_id,
            "connected_at": _connected_at(allow_override=isinstance(runner_args, EvalRunnerArguments)),
            "client": client,
            "submission": submission,
            "patient": None,
            "offers": {},
            "identify_attempts": 0,
        }
    )

    # No forced submit on a timer: flush() runs once, so a flush at 150s threw away the last
    # 30s of the call (9 of 28 calls in Run All #2 were flushed as NO_ACTION that way; none was
    # seen confirming afterwards, so the gain is unproven). The submit window stays open 30s
    # after the socket closes (CR-window) and the harness closed capped calls at 182-186s in
    # Run All #1, so flushing on disconnect is in time. Not covered: a socket that never
    # closes on our side (tunnel drop) submits nothing.

    @context_aggregator.user().event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        text = _reprompt(context)
        logger.info("Call {}: {}s of silence, re-prompting: {}", call_id, REPROMPT_AFTER_SILENCE_SECS, text)
        await worker.queue_frames([TTSSpeakFrame(text=text, append_to_context=False)])

    flow_started = False

    async def start_flow():
        nonlocal flow_started
        if flow_started:
            return
        flow_started = True
        await flow_manager.initialize(create_identify_node())
        await worker.queue_frames([TTSSpeakFrame(text=GREETING, append_to_context=True)])

    # RTVI clients (webrtc, daily, eval) send client-ready after connecting, which
    # interrupts and drops anything queued earlier; telephony has no RTVI client.
    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await start_flow()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        # Any telephony socket, not just one detected as "twilio": with no RTVI client-ready,
        # nothing else would ever start the flow and the bot would stay silent until cut off.
        if isinstance(runner_args, WebSocketRunnerArguments):
            await start_flow()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await submission.flush()
        await runner.cancel()

    try:
        await runner.run()
    finally:
        await submission.flush()
        await client.aclose()


async def _telephony_transport(runner_args: WebSocketRunnerArguments, params) -> BaseTransport:
    """Twilio-shaped WebSocket transport without Twilio credentials.

    create_transport() builds the serializer with auto_hang_up=True, which raises when
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are empty. The harness speaks Twilio Media
    Streams but is not Twilio: it hangs up on its own, so no REST hang-up is needed.
    """
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    if transport_type != "twilio":
        # Sample rates and the call id both hang off this detection; fail loudly, not silently.
        logger.error("Telephony handshake detected as {!r}, expected 'twilio': {}", transport_type, call_data)
    runner_args.transport_type = transport_type
    runner_args.call_data = call_data
    params.add_wav_header = False
    params.serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
        params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
    )
    return FastAPIWebsocketTransport(websocket=runner_args.websocket, params=params)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    # Noise suppression is deliberately off: not needed until the noisy-audio problems
    # (PR-12). When it is, add `"audio_in_filter": <filter>` here — and test it on a real
    # call first: RNNoiseFilter with pyrnnoise 0.4.3 + av 17 crashes on the first frame,
    # which kills audio input and leaves the bot deaf for the whole call.
    def _audio_kwargs() -> dict:
        return {"audio_in_enabled": True, "audio_out_enabled": True}

    transport_params = {
        "webrtc": lambda: TransportParams(**_audio_kwargs()),
        "twilio": lambda: FastAPIWebsocketParams(**_audio_kwargs()),
        "eval": lambda: EvalTransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    if DailyParams is not None:
        transport_params["daily"] = lambda: DailyParams(**_audio_kwargs())
    if isinstance(runner_args, WebSocketRunnerArguments):
        transport = await _telephony_transport(runner_args, transport_params["twilio"]())
    else:
        transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
