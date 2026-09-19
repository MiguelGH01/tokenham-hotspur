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
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
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
from pipecat.runner.utils import create_transport
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
from pipecat.services.soniox.stt import SonioxSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from booking import MADRID
from clinic_client import ClinicClient
from handlers import GREETING, create_identify_node
from krisp_model import ensure_filter_model, existing_filter_model_path
from submission import CallSubmission

load_dotenv(override=True)
ensure_filter_model()


def _audio_in_filter() -> BaseAudioFilter | None:
    """Krisp VIVA noise-reduction on inbound audio.

    The ``.kef`` is resolved at process startup (see ``ensure_filter_model``).
    """
    model_path = existing_filter_model_path()
    api_key = os.getenv("KRISP_VIVA_API_KEY")
    if not model_path:
        return None

    from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

    kwargs: dict = {}
    if model_path:
        kwargs["model_path"] = model_path
    if api_key:
        kwargs["api_key"] = api_key
    level = os.getenv("KRISP_NOISE_SUPPRESSION_LEVEL")
    if level:
        kwargs["noise_suppression_level"] = int(level)
    return KrispVivaFilter(**kwargs)


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
    return f"local-{uuid.uuid4().hex[:12]}"


def _connected_at() -> datetime:
    override = os.getenv("CALL_CLOCK_OVERRIDE")
    return datetime.fromisoformat(override) if override else datetime.now(MADRID)


def build_llm():
    provider = os.getenv("LLM_PROVIDER", "helmcode")
    if provider == "helmcode":  # OpenAI-compatible gateway (chat completions)
        return OpenAILLMService(
            api_key=os.environ["HELMCODE_API_KEY"],
            base_url=os.getenv("HELMCODE_BASE_URL", "https://api.helmcode.com/v1"),
            settings=OpenAILLMService.Settings(model=os.getenv("HELMCODE_MODEL", "deepseek-v4-flash")),
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


def build_stt():
    provider = os.getenv("STT_PROVIDER", "soniox")
    if provider == "deepgram":
        return DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    return SonioxSTTService(
        api_key=os.environ["SONIOX_API_KEY"],
        settings=SonioxSTTService.Settings(
            model=os.getenv("SONIOX_MODEL", "stt-rt-v5"),
            language_hints=[Language.ES, Language.CA, Language.EN, Language.EU, Language.GL],
            enable_language_identification=True,
        ),
    )


def build_tts():
    provider = os.getenv("TTS_PROVIDER", "elevenlabs")
    if provider == "deepgram":
        return DeepgramTTSService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            settings=DeepgramTTSService.Settings(
                voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-helena-en")
            ),
        )
    return ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        settings=ElevenLabsTTSService.Settings(
            voice=os.environ["ELEVENLABS_VOICE_ID"],
            model=os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            language=os.getenv("ELEVENLABS_LANGUAGE", "es"),
        ),
    )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    call_id = _call_id(runner_args)
    logger.info("Starting bot for call {}", call_id)

    stt = build_stt()
    tts = build_tts()
    llm = build_llm()

    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            filter_incomplete_user_turns=True,
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


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    def _audio_kwargs() -> dict:
        return {
            "audio_in_enabled": True,
            "audio_out_enabled": True,
            "audio_in_filter": _audio_in_filter(),
        }

    transport_params = {
        "daily": lambda: DailyParams(**_audio_kwargs()),
        "webrtc": lambda: TransportParams(**_audio_kwargs()),
        "twilio": lambda: FastAPIWebsocketParams(**_audio_kwargs()),
        "eval": lambda: EvalTransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
