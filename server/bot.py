"""Clínica Arenal receptionist — cascade voice + three-node Flows (identify / act / close).

Run the bot using::

    uv run bot.py            # all real transports
    uv run bot.py -t eval    # headless eval server for `pipecat eval run`
"""

import asyncio
import os
import sys
import threading
import uuid
import wave
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger
from pipecat.evals.transport import EvalTransportParams
from pipecat.flows import FlowManager
from pipecat.frames.frames import (
    BotSpeakingFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserSpeakingFrame,
    UserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
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

from audio_models import CallSileroVADAnalyzer, CallSmartTurnAnalyzer, warm_audio_models
from booking import MADRID
from clinic.clinic_client import ClinicClient
from clinic.speech import chat_role, chat_text
from flow import FILLER, GREETING, RAILS, create_identify_node
from flow.speak import speak_as_llm
from gateway_llm import StallGuardedLLMService
from submission import CallSubmission
from ws_probes import quiet_empty_websocket_probes

load_dotenv(override=True)
quiet_empty_websocket_probes()

# The platform cuts a call after ~30-35s without audible agent audio (SC-silence-cut).
# The idle re-prompt is counted from when the bot STOPS speaking.
REPROMPT_AFTER_SILENCE_SECS = 10.0
# Harness wall-clock is 180s (SC-three-minutes) and is a fail even with a good record.
# Commit and hang up before that.
WALL_CLOCK_COMMIT_SECS = 150.0

# pipecat.runner.run.main() defaults the stderr sink to DEBUG (TRACE with -v), which logs
# every live STT transcription and TTS utterance — i.e. patient name/DNI/phone/health reason
# in plaintext. Real calls default to INFO; opt into DEBUG explicitly when triaging locally.
# The eval Makefile targets set LOG_LEVEL=DEBUG themselves since eval personas are synthetic.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Opt-in: recording a live call is a compliance decision, not just a debugging convenience,
# so it defaults off. Recordings are call audio only (post-STT/TTS raw PCM), never touched by
# the LOG_LEVEL redaction above or below — treat this directory like patient data at rest.
RECORD_CALLS = os.getenv("RECORD_CALLS", "").strip().lower() in {"1", "true", "yes"}
RECORDINGS_DIR = os.getenv("RECORDINGS_DIR", "run-logs/recordings")


def _save_call_recording(call_id: str, audio: bytes, sample_rate: int, num_channels: int) -> None:
    if not audio:
        return
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    path = os.path.join(RECORDINGS_DIR, f"{call_id}.wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(2)  # pipecat audio frames are 16-bit PCM
        wf.setframerate(sample_rate)
        wf.writeframes(audio)
    logger.info("Call {}: saved recording to {}", call_id, path)


def _is_twilio_session(runner_args: RunnerArguments) -> bool:
    return (
        isinstance(runner_args, WebSocketRunnerArguments)
        and getattr(runner_args, "transport_type", None) == "twilio"
    )


def _is_eval_session(runner_args: RunnerArguments) -> bool:
    return isinstance(runner_args, EvalRunnerArguments)


def _session_state(call_id: str, submission: CallSubmission) -> dict:
    return {
        "call_id": call_id,
        "connected_at": _connected_at(),
        "client": submission._client,
        "submission": submission,
        "patient": None,
        "offers": {},
        "identify_attempts": 0,
        "language": None,
        "final_intent": None,
    }


def _call_id(runner_args: RunnerArguments) -> str:
    call_data = getattr(runner_args, "call_data", None)
    if call_data and call_data.call_id:
        return call_data.call_id
    return f"local-{uuid.uuid4().hex[:12]}"


def _connected_at() -> datetime:
    override = os.getenv("CALL_CLOCK_OVERRIDE")
    return datetime.fromisoformat(override) if override else datetime.now(MADRID)


def _drop_probe_logs(record) -> bool:
    return "did not receive a valid HTTP request" not in record["message"]


def _reprompt(context: LLMContext) -> str:
    """Repeat the bot's last message so silence never outlasts the platform's window.

    The whole message, not just its closing question: a caller who missed the offer cannot
    answer a bare "Does that work for you?".
    """
    spoken = [
        text
        for m in context.get_messages()
        if chat_role(m) == "assistant" and (text := chat_text(m))
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
    # pipecat.runner.run.main() already set its own sink (DEBUG/TRACE) before this runs;
    # override it once, process-wide, per LOG_LEVEL/RECORD_CALLS policy above.
    logger.remove()
    logger.add(sys.stderr, level=LOG_LEVEL, filter=_drop_probe_logs)

    call_id = _call_id(runner_args)
    logger.info("Starting bot for call {}", call_id)
    if str(call_id).startswith("local-"):
        logger.error("Call has no start.callSid; submissions will 404")

    audio_recorder = None
    if RECORD_CALLS:
        audio_recorder = AudioBufferProcessor(num_channels=2, auto_start_recording=True)

        @audio_recorder.event_handler("on_audio_data")
        async def on_audio_data(processor, audio, sample_rate, num_channels):
            await asyncio.to_thread(_save_call_recording, call_id, audio, sample_rate, num_channels)

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramTTSService.Settings(voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-helena-en")),
    )
    llm = build_llm()

    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(service, function_calls):
        try:
            names = {
                getattr(call, "function_name", None)
                or getattr(call, "name", None)
                or (call.get("name") if isinstance(call, dict) else None)
                for call in function_calls
            }
        except TypeError:
            names = set()
        if names and names <= {"confirm_offer"}:
            return
        await service.push_frame(TTSSpeakFrame(text=FILLER, append_to_context=False))

    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=CallSileroVADAnalyzer(),
            user_idle_timeout=REPROMPT_AFTER_SILENCE_SECS,
            # filter_incomplete_user_turns stays off: the LLM judged a bare "Hello." as an
            # unfinished turn and went silent for 10s (16 of 21 calls in the first Run All).
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=CallSmartTurnAnalyzer())]
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
            *([audio_recorder] if audio_recorder is not None else []),
            context_aggregator.assistant(),
        ]
    )

    pipeline_params = {"enable_metrics": True, "enable_usage_metrics": True}
    if _is_twilio_session(runner_args):
        pipeline_params["audio_in_sample_rate"] = 8000
        pipeline_params["audio_out_sample_rate"] = 8000

    worker_kwargs = {
        "params": PipelineParams(**pipeline_params),
        "idle_timeout_secs": runner_args.pipeline_idle_timeout_secs,
        "observers": [],
    }
    if _is_eval_session(runner_args):
        # Text eval uses skip_tts, so BotSpeakingFrame never arrives. Count LLM
        # text as activity, and never cancel the eval server on idle.
        worker_kwargs["cancel_runner_on_idle_timeout"] = False
        worker_kwargs["idle_timeout_frames"] = (
            BotSpeakingFrame,
            InterimTranscriptionFrame,
            TranscriptionFrame,
            UserSpeakingFrame,
            UserStartedSpeakingFrame,
            LLMTextFrame,
            LLMFullResponseEndFrame,
        )
    worker = PipelineWorker(pipeline, **worker_kwargs)
    # Do not install SIGINT per session: on Windows each call would overwrite the
    # process handler, and a burst of 20 would cancel the last runner on Ctrl+C.
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)

    flow_manager = FlowManager(
        worker=worker,
        llm=llm,
        context_aggregator=context_aggregator,
        transport=transport,
        global_functions=RAILS,
    )
    submission = CallSubmission(call_id, ClinicClient())
    flow_manager.state.update(_session_state(call_id, submission))

    # Submit a real answer before the 180s cap. Do not POST the unused
    # patient_not_found default — that locks a mismatch and the call still
    # runs to the wall-clock fail (SC-three-minutes).
    committed_answer = False

    async def _commit_before_wall_clock():
        nonlocal committed_answer
        await asyncio.sleep(WALL_CLOCK_COMMIT_SECS)
        if submission._flushed:
            committed_answer = True
            return
        if not submission.ready_to_submit():
            logger.warning("Call {}: 150s elapsed with no bookable record; not submitting default", call_id)
            return
        logger.warning("Call {}: committing before the wall-clock cap", call_id)
        await submission.flush()
        committed_answer = True
        await worker.queue_frames(
            [TTSSpeakFrame(text="I've noted that. Thank you, goodbye.", append_to_context=False)]
        )

    @context_aggregator.user().event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        if committed_answer or submission._flushed:
            return
        text = _reprompt(context)
        logger.info("Call {}: {}s of silence, re-prompting: {}", call_id, REPROMPT_AFTER_SILENCE_SECS, text)
        await worker.queue_frames([TTSSpeakFrame(text=text, append_to_context=False)])

    @context_aggregator.user().event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message):
        from clinic.speech import accepts_offer
        from flow.tools import confirm_offer

        text = getattr(message, "content", None) or ""
        if submission._flushed or not submission.offered or not accepts_offer(text):
            return
        logger.info("Call {}: caller accepted, committing without waiting for the LLM", call_id)
        _, nxt = await confirm_offer({}, flow_manager)
        if nxt is not None:
            await flow_manager.set_node_from_config(nxt)

    flow_started = False

    async def start_flow(*, reset: bool = False):
        nonlocal flow_started, call_id, submission, committed_answer
        if flow_started and not reset:
            return
        if reset:
            logger.info("Eval client reused the pipeline; resetting session")
            call_id = _call_id(runner_args)
            submission = CallSubmission(call_id, ClinicClient())
            committed_answer = False
            flow_manager.state.clear()
            flow_manager.state.update(_session_state(call_id, submission))
            context.set_messages([])
            await flow_manager.set_node_from_config(create_identify_node())
            await speak_as_llm(llm, GREETING, in_context=True)
            return
        flow_started = True
        if _is_twilio_session(runner_args):
            worker.create_task(_commit_before_wall_clock(), name="wall-clock-commit")
        await flow_manager.initialize(create_identify_node())
        # Text-mode eval asserts `response` → `llm_response`. TTSSpeakFrame only
        # emits TTS events, which skip_tts drops, so turn 0 would wait 60s.
        await speak_as_llm(llm, GREETING, in_context=True)

    # RTVI clients (webrtc, daily, eval) send client-ready after connecting, which
    # interrupts and drops anything queued earlier; telephony has no RTVI client.
    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await start_flow(reset=flow_started and _is_eval_session(runner_args))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        if _is_twilio_session(runner_args):
            await start_flow()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await submission.flush()
        # Eval reuses this pipeline across scenarios; cancelling the runner
        # kills the server (and idle then cannot recover).
        if _is_twilio_session(runner_args):
            await runner.cancel()

    try:
        await runner.run()
    finally:
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
    # Consume the Twilio handshake before loading models. Warming ONNX first
    # left `connected`/`start` sitting unread; the harness then drops us
    # ("connection lost") and we POST the default NO_ACTION ("record mismatch").
    if isinstance(runner_args, WebSocketRunnerArguments):
        transport = await _telephony_transport(runner_args, transport_params_twilio())
        await asyncio.to_thread(warm_audio_models)
    else:
        await asyncio.to_thread(warm_audio_models)
        transport = await create_transport(runner_args, transport_params())
    await run_bot(transport, runner_args)


def _audio_kwargs() -> dict:
    return {"audio_in_enabled": True, "audio_out_enabled": True}


def transport_params_twilio() -> FastAPIWebsocketParams:
    return FastAPIWebsocketParams(**_audio_kwargs())


def transport_params() -> dict:
    params = {
        "webrtc": lambda: TransportParams(**_audio_kwargs()),
        "twilio": lambda: FastAPIWebsocketParams(**_audio_kwargs()),
        "eval": lambda: EvalTransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    if DailyParams is not None:
        params["daily"] = lambda: DailyParams(**_audio_kwargs())
    return params


threading.Thread(target=warm_audio_models, daemon=True, name="warm-audio").start()


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
