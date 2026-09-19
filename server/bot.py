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
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.evals.transport import EvalTransportParams
from pipecat.flows import FlowManager
from pipecat.frames.frames import LLMMessagesAppendFrame
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
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner



from booking import MADRID
from clients.clinic_client import ClinicClient
from flows.common import GREETING
from flows.reception import create_reception_node
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


def _is_twilio_session(runner_args: RunnerArguments) -> bool:
    return (
        isinstance(runner_args, WebSocketRunnerArguments)
        and getattr(runner_args, "transport_type", None) == "twilio"
    )


def _call_id(runner_args: RunnerArguments) -> str:
    call_data = getattr(runner_args, "call_data", None)
    if call_data and call_data.call_id:
        return call_data.call_id
    # The platform validates call_id as a UUID before anything else, so the
    # local (eval/no-call) fallback must be a well-formed UUID too.
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
            settings=OpenAILLMService.Settings(
                model=os.getenv("HELMCODE_MODEL", "deepseek-v4-flash")
            ),
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


def build_eval_judge_llm(config: dict | None = None):
    """Factory for the eval harness judge (`judge.eval.factory:` in a scenario).

    Reuses the bot's LLM service so the judge does not depend on a local Ollama,
    but pins the judge model to ``EVAL_JUDGE_MODEL`` (default ``gpt-5.1``): the
    bot judging its own replies makes verdicts drift with every bot-model swap.
    A scenario's ``model`` key still wins.
    """
    llm = build_llm()
    override = (config or {}).get("model") or os.getenv("EVAL_JUDGE_MODEL", "gpt-5.1")
    if override and hasattr(llm, "settings"):
        llm.settings.model = override
    return llm


def build_turn_completion_config():
    """Turn-completion marker rules tuned for this LLM.

    The default protocol occasionally makes the model emit a bare marker (or
    an empty reply), which stalls the turn: no llm_response ever arrives and
    the caller waits on silence. These rules keep the ●/◐/○ machinery but
    make a marker-only reply impossible and nail down the two cases seen in
    evals: a fully stated identifier/phone number is a complete turn, and "
    "there is always a sentence to say after ●.
    """
    from pipecat.turns.user_turn_completion_mixin import UserTurnCompletionConfig

    return UserTurnCompletionConfig(
        instructions="""
TURN COMPLETION PROTOCOL (mandatory):
Decide whether the caller's turn is complete, then start your response with exactly one marker as its very first character:

●  complete: answer them now. ● must always be followed by your full spoken reply — a tool call or a question. Never write ● on its own and never write it again inside the reply.
◐  the caller was cut off mid-phrase and continues in seconds. Write ◐ and nothing else.
○  the caller asked for time to think ("hold on", "let me see"). Write ○ and nothing else.

Deciding:
- A caller who has just given the exact thing you asked for — a full name, a complete ID number with its letter, a complete phone number, an email, a specialty, a yes or a no — has finished their turn. Answer it with ●, even if they said more than you asked for.
- A number or name is complete once the sentence around it ends. Do not wait for more digits or guess that more words are coming.
- Only write ◐ when words actually stopped mid-phrase (a conjunction, a preposition, a trailing list). Only write ○ for an explicit request to wait.
- If a tool call is the right response, the turn is complete: make the call.
- If you would otherwise have nothing to say, still reply with one short complete sentence after ●.

Format rules:
- The marker is the first character. Exactly one marker per response.
- After ◐ or ○ output nothing else. After ● always output your reply.
""",
    )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    call_id = _call_id(runner_args)
    logger.info("Starting bot for call {}", call_id)

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramTTSService.Settings(
            voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-helena-en")
        ),
    )
    llm = build_llm()

    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),

            # Evita meter turns incompletos al contexto.
            filter_incomplete_user_turns=True,

            # Marker rules tuned for deepseek: a bare marker (or an empty
            # reply after ●) stalls the turn — no llm_response ever arrives.
            user_turn_completion_config=build_turn_completion_config(),

            user_turn_strategies=UserTurnStrategies(
                # IMPORTANTE:
                # No interrumpir simplemente porque VAD detecte sonido.
                start=[
                    MinWordsUserTurnStartStrategy(
                        min_words=3,
                    )
                ],

                # SmartTurn decide cuándo realmente ha acabado
                # de hablar el usuario.
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=LocalSmartTurnAnalyzerV3()
                    )
                ],
            ),
        ),
    )

    user_aggregator = context_aggregator.user()
    assistant_aggregator = context_aggregator.assistant()


    @user_aggregator.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy):
        logger.warning(
            "USER TURN STARTED | strategy={} | bot_speaking={}",
            type(strategy).__name__ if strategy else "unknown",
            getattr(transport.output(), "_bot_speaking", "unknown"),
        )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
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

    flow_started = False

    async def start_flow():
        nonlocal flow_started
        if flow_started:
            return
        flow_started = True
        await flow_manager.initialize(create_reception_node())
        # Greeting is LLM-composed (developer message + run) so it flows through
        # the normal response path: TTS on voice calls, llm_response on eval.
        await worker.queue_frames(
            [
                LLMMessagesAppendFrame(
                    messages=[
                        {
                            "role": "developer",
                            "content": f"Open the call with this greeting, then wait for the caller: {GREETING}",
                        }
                    ],
                    run_llm=True,
                )
            ]
        )

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
        await submission.flush()
        await runner.cancel()

    try:
        await runner.run()
    finally:
        await submission.flush()


def _audio_kwargs() -> dict:
    return {
        "audio_in_enabled": True,
        "audio_out_enabled": True,
        "audio_in_filter": _audio_in_filter(),
    }


async def _twilio_transport(runner_args: WebSocketRunnerArguments):
    """Twilio Media Streams transport without REST credentials.

    The pipecat runner builds TwilioFrameSerializer with ``auto_hang_up=True``,
    which requires TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN to terminate the call
    through Twilio's REST API. The scoring dashboard dials through its own
    Twilio account (CR-twilio-shape: no credentials on our side), and with
    Media Streams the call ends when the WebSocket closes — so auto_hang_up is
    disabled instead. Mirrors pipecat.runner.utils._create_telephony_transport
    otherwise.
    """
    from pipecat.runner.utils import parse_telephony_websocket
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport

    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    runner_args.transport_type = transport_type
    runner_args.call_data = call_data

    params = FastAPIWebsocketParams(**_audio_kwargs())
    params.add_wav_header = False
    params.serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
        params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
    )
    return FastAPIWebsocketTransport(websocket=runner_args.websocket, params=params)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""
    transport_params = {
        "daily": lambda: DailyParams(**_audio_kwargs()),
        "webrtc": lambda: TransportParams(**_audio_kwargs()),
        "twilio": lambda: FastAPIWebsocketParams(**_audio_kwargs()),
        "eval": lambda: EvalTransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    if isinstance(runner_args, WebSocketRunnerArguments) and runner_args.transport_type != "websocket":
        transport = await _twilio_transport(runner_args)
    else:
        transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
