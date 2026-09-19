"""Clínica Arenal receptionist — Pipecat cascade pipeline + Flows graph (see handlers.py).

Run the bot using::

    uv run bot.py            # all real transports
    uv run bot.py -t eval    # headless eval server for `pipecat eval run`
"""

import asyncio
import os
import uuid
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter
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
from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport, parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService
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
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from booking import MADRID
from clinic.clinic_client import ClinicClient
from flow import GREETING, create_identify_node
from submission import CallSubmission

load_dotenv(override=True)

FORCED_SUBMIT_AFTER_SECS = 150  # the harness caps calls at 3 minutes


def _is_twilio_session(runner_args: RunnerArguments) -> bool:
    return (
        isinstance(runner_args, WebSocketRunnerArguments)
        and getattr(runner_args, "transport_type", None) == "twilio"
    )


def _call_id(runner_args: RunnerArguments) -> str:
    call_data = getattr(runner_args, "call_data", None)
    if call_data and call_data.call_id:
        return call_data.call_id
    # The submit API's CallId type requires a well-formed UUID; a non-UUID placeholder
    # (e.g. a "local-" prefixed hex string) makes every POST for this call 422.
    return str(uuid.uuid4())


def _connected_at() -> datetime:
    override = os.getenv("CALL_CLOCK_OVERRIDE")
    return datetime.fromisoformat(override) if override else datetime.now(MADRID)


def build_llm():
    provider = os.getenv("LLM_PROVIDER", "helmcode")
    if provider == "helmcode":  # OpenAI-compatible gateway (chat completions)
        return OpenAILLMService(
            api_key=os.environ["HELMCODE_API_KEY"],
            base_url=os.getenv("HELMCODE_BASE_URL", "https://api.helmcode.com/v1"),
            # `extra.timeout` is a per-request httpx read timeout: it fires on a gap between
            # chunks, not on total response length, so it only catches a stalled stream —
            # `retry_on_timeout` below only guards the initial connect (~8% of gateway
            # requests hang there with no response at all; normal TTFB is ~0.55s), not a
            # stream that opens fine and then goes silent mid-generation.
            settings=OpenAILLMService.Settings(
                model=os.getenv("HELMCODE_MODEL", "deepseek-v4-flash"),
                # Bounds a runaway/visibly-reasoning response to a few sentences instead of
                # unbounded rambling; normal replies (including a full registration
                # readback) stay well under this in practice (~150-290 completion tokens).
                max_completion_tokens=400,
                extra={"timeout": 10.0},
            ),
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
            filter_incomplete_user_turns=True,
            user_turn_strategies=UserTurnStrategies(
                # First to fire wins: smart-turn normally decides end-of-turn from audio +
                # transcript; the speech-timeout strategy is a fast fallback for when it
                # doesn't (real telephony audio can leave it unable to decide), so a stall
                # there costs ~1.2s of silence instead of the full 5s on_user_turn_stop_timeout
                # backstop.
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3()),
                    SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=1.2),
                ]
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

    @llm.event_handler("on_completion_timeout")
    async def _on_completion_timeout(service):
        # A stalled completion produced no text and no tool call, so the caller is
        # sitting in silence; ask them to repeat rather than leave the call dead.
        logger.warning("Call {}: LLM completion stalled, prompting caller to repeat", call_id)
        await worker.queue_frames(
            [TTSSpeakFrame(text="Sorry, could you say that again?", append_to_context=True)]
        )

    flow_manager = FlowManager(
        worker=worker,
        llm=llm,
        context_aggregator=context_aggregator,
        transport=transport,
    )
    submission = CallSubmission(call_id, ClinicClient())
    flow_manager.state.update(
        {
            "call_id": call_id,
            "connected_at": _connected_at(),
            "client": submission._client,
            "submission": submission,
            "patient": None,
            "offers": {},
            "identify_attempts": 0,
        }
    )

    async def forced_submit():
        await asyncio.sleep(FORCED_SUBMIT_AFTER_SECS)
        logger.warning("Call {} hit {}s: forcing submission", call_id, FORCED_SUBMIT_AFTER_SECS)
        await submission.flush()

    timer: asyncio.Task | None = None

    flow_started = False

    async def start_flow():
        nonlocal flow_started, timer
        if flow_started:
            return
        flow_started = True
        timer = asyncio.create_task(forced_submit())  # 150s from call start, not process start
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
        if _is_twilio_session(runner_args):
            await start_flow()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        if timer:
            timer.cancel()
        await submission.flush()
        await runner.cancel()

    try:
        await runner.run()
    finally:
        if timer:
            timer.cancel()
        await submission.flush()


async def _telephony_transport(runner_args: WebSocketRunnerArguments, params) -> BaseTransport:
    """Twilio-shaped WebSocket transport without Twilio credentials.

    create_transport() builds the serializer with auto_hang_up=True, which raises when
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are empty. The harness speaks Twilio Media
    Streams but is not Twilio: it hangs up on its own, so no REST hang-up is needed.
    """
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
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

    def _audio_kwargs() -> dict:
        return {
            "audio_in_enabled": True,
            "audio_out_enabled": True,
            # RNNoiseFilter disabled: pinned pyrnnoise~=0.4.3 calls audiolab's Graph(rate=...),
            # but the resolved audiolab (and, transitively, av) no longer accept that kwarg —
            # it crashes the audio input task on connect and silently kills STT for the whole
            # call. Re-enable once pipecat-ai's `rnnoise` extra pins a compatible audiolab/av.
            # "audio_in_filter": RNNoiseFilter(),
        }

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
