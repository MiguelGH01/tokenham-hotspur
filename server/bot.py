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
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from affirmation_watch import AffirmationWatch
from booking import MADRID
from clients.clinic_client import ClinicClient
from clinic_catalog import load_catalog
from flows.common import GREETING
from flows.reception import create_reception_node
from krisp_model import ensure_filter_model, existing_filter_model_path
from liveness import SilenceWatchdog
from llm_deadline import FirstTokenDeadlineLLM
from resolution import resolve_fallback
from submission import CallSubmission

load_dotenv(os.getenv("DOTENV_PATH") or ".env", override=True)
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


#: How long an inference may produce nothing before it is abandoned and
#: re-issued. Pipecat defaults ``retry_on_timeout`` to ``False``, so an
#: unresponsive stream used to be waited on until the platform cut the call for
#: silence — a 246 s stall is on record. The re-issue pipecat performs is itself
#: unbounded, so this bounds the *first* attempt only: dead air, then one fresh
#: request. The watchdog's deferred re-run is the backstop beyond that.
LLM_RETRY_TIMEOUT_SECS = float(os.getenv("LLM_RETRY_TIMEOUT_SECS", "8"))


def build_llm(call_id: str | None = None):
    """The service that answers the phone.

    Latency here is not a comfort question. A call is capped at three minutes
    and cut off when the agent produces no audible audio, and both signals are
    attributed to us, so a reasoning model's extra seconds per turn are scored
    as failure. The default is therefore a fast model with thinking off
    (``LLM_REASONING_EFFORT=none``): the turn has to be short, not thorough.

    The OpenAI-backed services are also given ``retry_on_timeout=True`` with
    ``LLM_RETRY_TIMEOUT_SECS``: an unresponsive stream is re-issued instead of
    becoming dead air, which the scorer attributes to us outright. The
    chat-completions path goes one step further and uses
    :class:`llm_deadline.FirstTokenDeadlineLLM`, because pipecat's own guard
    stops at the response headers and re-issues without any deadline at all
    (``llm_deadline.py`` has the post-mortem).
    """
    provider = os.getenv("LLM_PROVIDER", "helmcode")
    if provider == "helmcode":  # OpenAI-compatible gateway (chat completions)
        return FirstTokenDeadlineLLM(
            api_key=os.environ["HELMCODE_API_KEY"],
            base_url=os.getenv("HELMCODE_BASE_URL", "https://api.helmcode.com/v1"),
            retry_timeout_secs=LLM_RETRY_TIMEOUT_SECS,
            retry_on_timeout=True,
            call_id=call_id,
            settings=OpenAILLMService.Settings(
                model=os.getenv("HELMCODE_MODEL", "deepseek-v4-flash"),
                temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", "512")),
                extra={"reasoning_effort": os.getenv("LLM_REASONING_EFFORT", "none")},
            ),
        )
    if provider == "gemini":
        return GoogleLLMService(
            api_key=os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"],
            settings=GoogleLLMService.Settings(model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash")),
        )
    # Reasoning off explicitly, and the low-latency tier, both measured under
    # load rather than assumed: 20 concurrent calls to gpt-5.1 answered 20/20
    # with and without the tier, and the tier moved p50 from 1.04 s to 0.74 s
    # and the worst call from 1.86 s to 0.95 s. Turn tails are what the silence
    # window and the three-minute cap actually punish.
    #
    # ``temperature`` is only accepted while reasoning is off — a positive
    # effort rejects it — so the two are set together or not at all.
    effort = os.getenv("LLM_REASONING_EFFORT", "none")
    openai_settings: dict = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1"),
        # A spoken turn is a sentence or two (observed: 7-79 tokens). The cap
        # bounds a runaway answer, which is dead air with a deadline on it.
        "max_completion_tokens": int(os.getenv("LLM_MAX_TOKENS", "400")),
        "reasoning": OpenAIResponsesLLMService.ReasoningConfig(effort=effort),
    }
    if effort == "none":
        openai_settings["temperature"] = float(os.getenv("LLM_TEMPERATURE", "0.2"))
    return OpenAIResponsesLLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        retry_timeout_secs=LLM_RETRY_TIMEOUT_SECS,
        retry_on_timeout=True,
        service_tier=os.getenv("OPENAI_SERVICE_TIER", "fast") or None,
        settings=OpenAIResponsesLLMService.Settings(**openai_settings),
    )


def build_eval_judge_llm(config: dict | None = None):
    """Factory for the eval harness judge (`judge.eval.factory:` in a scenario).

    The judge is pinned to its own model **and its own provider**. It used to
    reuse the bot's service, which coupled two decisions that have nothing to do
    with each other: putting the phone on a gateway that does not serve
    ``gpt-5.1`` would have silently changed every verdict, or failed them, while
    looking like a bot regression. Verdicts must not move when the bot's model
    does.

    A scenario's ``model`` key still wins over ``EVAL_JUDGE_MODEL``.
    """
    override = (config or {}).get("model") or os.getenv("EVAL_JUDGE_MODEL", "gpt-5.1")
    return OpenAIResponsesLLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAIResponsesLLMService.Settings(model=override),
    )


def _user_turn_strategies() -> UserTurnStrategies:
    """Turn detection for the voice path.

    Two deliberate choices, both taken after reading the installed pipecat
    1.11.0 source rather than from memory:

    - The turn-completion marker protocol (marker-then-reply) is **not** used.
      Its parameter pair is deprecated since 1.2.0 and removed in 2.0.0, and an
      incomplete verdict suppresses the whole response while the pipeline waits
      5-10 s before re-prompting. On a call capped at three minutes and cut on
      silence, that is a silence generator, and ``Agent silence`` was the
      largest single source of lost points.
    - Barge-in damping is opt-in, because the damping itself costs turns.
      ``MinWordsUserTurnStartStrategy`` discards the aggregation for anything
      shorter than ``min_words`` while the bot speaks, so the one-word answers
      this agent lives on — "yes", "morning", "Tuesday" — are exactly the ones
      it drops. The library default (VAD plus transcription) keeps them.

    Set ``BARGE_IN_MIN_WORDS`` to reinstate the damping if a noisy line makes
    the bot interrupt itself; it is a manner trade-off, and manner is not
    scored, while a dropped turn is.
    """
    min_words = int(os.getenv("BARGE_IN_MIN_WORDS", "0"))
    if min_words <= 1:
        return UserTurnStrategies()
    return UserTurnStrategies(
        start=[MinWordsUserTurnStartStrategy(min_words=min_words)],
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
    )


#: Words a general-purpose recogniser gets wrong and the scorer compares exactly.
#: The scored transcript has "Doctor Alina Iglesias" where the caller said Elena
#: Iglesias, and a dictated DNI read back one digit out — both are a scored
#: failure, while a clarification turn costs seconds the call does not have. The
#: roster's own names, the sites and the plan labels are the vocabulary that
#: matters, so they are boosted rather than hoped for.
_STT_TITLES = {"Dr.", "Dra.", "D."}


def _stt_keyterms() -> list[str]:
    """Proper nouns worth boosting: provider names, sites, plan labels, ids.

    ``keyterm`` prompting is a nova-3 feature, so :func:`_stt_settings` only
    passes it for those models; an older model would reject the parameter.
    """
    catalogue = load_catalog()
    names = [
        token
        for provider in catalogue["providers"]
        for token in provider["name"].split()
        if token not in _STT_TITLES
    ]
    sites = [loc["name"] for loc in catalogue["locations"]]
    plans = [plan["name"] for plan in catalogue["plans"]]
    return sorted({*names, *sites, *plans, "DNI", "NIE"})


def _stt_settings() -> DeepgramSTTService.Settings:
    """Speech recognition tuned for what is actually compared.

    ``language=multi`` rather than ``en``, measured rather than assumed. The
    clinic is Spanish and its callers are not: the published cases have
    English-speaking callers reading out Spanish proper nouns, and the private
    pool reaches into Spanish and Catalan (PR-11). Transcribing the same audio
    through both settings on 19 Sep 2026:

    ==================== ===================== =====================
    model / language      English caller        Spanish caller
    ==================== ===================== =====================
    ``nova-3-general/en`` ``15750638P``          *(empty transcript)*
    ``nova-3-general/multi`` ``157-50-638P``     ``15750638P``
    ==================== ===================== =====================

    ``multi`` is the only one that hears Spanish at all and it is no worse on
    English — it produced ``Dr. Elena Iglesias`` where ``en`` produced the
    stray ``Doctor. Elena Iglesias``. The hyphens ``smart_format`` adds to a
    digit run are harmless: ``national_id.normalize_national_id`` strips ``-``
    and whitespace before anything compares them.

    ``numerals`` is what turns "six five zero" into digits before the model has
    to do arithmetic on words, and ``keyterm`` is what turned a misheard ``DKB``
    into the right ``DKV`` — a scored field.
    """
    model = os.getenv("DEEPGRAM_STT_MODEL", "nova-3-general")
    options: dict = {
        "model": model,
        "language": os.getenv("DEEPGRAM_STT_LANGUAGE", "multi"),
        "numerals": True,
        "smart_format": True,
        "punctuate": True,
    }
    if model.startswith("nova-3") and os.getenv("DEEPGRAM_STT_KEYTERMS", "1") != "0":
        options["keyterm"] = _stt_keyterms()
    return DeepgramSTTService.Settings(**options)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    call_id = _call_id(runner_args)
    logger.info("Starting bot for call {}", call_id)
    telephony = _is_twilio_session(runner_args)

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=_stt_settings(),
    )
    tts = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        # Telephony is 8 kHz end to end, so asking the synthesiser for 8 kHz
        # avoids a resample per call — one less thing competing for the CPU when
        # a Run All holds ten calls open at once.
        sample_rate=int(os.getenv("DEEPGRAM_TTS_SAMPLE_RATE", "8000" if telephony else "0"))
        or None,
        settings=DeepgramTTSService.Settings(
            voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-helena-en"),
            # Manner is not scored, but the three-minute cap is: speaking a
            # little faster is a little more room before the wall clock bites.
            speed=float(os.getenv("DEEPGRAM_TTS_SPEED", "1.0")),
        ),
    )
    llm = build_llm(call_id)

    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=_user_turn_strategies(),
        ),
    )

    user_aggregator = context_aggregator.user()
    assistant_aggregator = context_aggregator.assistant()

    # Speaks a holding line instead of leaving dead air when the bot stalls.
    # Placed before the LLM so both frames it emits travel downstream correctly.
    watchdog = SilenceWatchdog(
        silence_secs=float(os.getenv("SILENCE_GUARD_SECS", "6")),
        max_nudges=int(os.getenv("SILENCE_MAX_NUDGES", "2")),
        rerun_after_secs=float(os.getenv("SILENCE_RERUN_SECS", "18")),
        is_active=lambda: flow_started,
    )

    # Submits a plan the caller has just agreed to, without waiting for the
    # model's own confirm turn (see affirmation_watch.py). Triggered by the
    # aggregator's own turn event, wired just below.
    affirmation_watch = AffirmationWatch()

    @user_aggregator.event_handler("on_user_turn_message_added")
    async def on_user_turn_message_added(aggregator, message):
        """The caller's finalized turn is now in the context.

        This is the moment a yes becomes actionable. The user aggregator
        consumes the final ``TranscriptionFrame`` rather than forwarding it, so a
        processor further down the pipeline never sees the turn a watcher would
        exist for.
        """
        affirmation_watch.consider(getattr(message, "content", None))

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
            watchdog,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    pipeline_params = {"enable_metrics": True, "enable_usage_metrics": True}
    if telephony:
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
    affirmation_watch.bind(flow_manager)
    submission = CallSubmission(
        call_id,
        ClinicClient(),
        # Only asked when the call ends having decided nothing: the best ending
        # it can still stand behind beats the ``out_of_scope`` that matches no
        # published case. See ``resolution.py``.
        fallback=lambda: resolve_fallback(flow_manager.state),
    )
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
    _start_task: asyncio.Task | None = None

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

    async def start_flow_after_grace():
        """Greet anyway if client-ready never arrives.

        A call whose greeting is never queued is dead air from the first second,
        and the scorer attributes that silence to the agent. ``start_flow`` is
        idempotent, so the normal path makes this a no-op.
        """
        await asyncio.sleep(float(os.getenv("FLOW_START_GRACE_SECS", "8")))
        if not flow_started:
            logger.warning("Client-ready never arrived; starting the flow anyway")
            await start_flow()

    # RTVI clients (webrtc, daily, eval) send client-ready after connecting, which
    # interrupts and drops anything queued earlier; telephony has no RTVI client.
    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await start_flow()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal _start_task
        logger.info("Client connected")
        if _is_twilio_session(runner_args):
            await start_flow()
            return
        if not flow_started:
            _start_task = asyncio.create_task(start_flow_after_grace())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await submission.close()
        await runner.cancel()

    async def deliver_until_accepted():
        """Keep re-offering a decided plan for as long as the call lives.

        A call with no accepted record scores nothing, and the platform can
        refuse a submission transiently. ``flush`` only ever sends actions the
        call has actually decided, so this cannot freeze a plan prematurely.
        """
        interval = float(os.getenv("SUBMIT_RETRY_SECS", "5"))
        while True:
            await asyncio.sleep(interval)
            if submission.needs_delivery:
                await submission.flush()

    _deliver_task = asyncio.create_task(deliver_until_accepted())

    try:
        await runner.run()
    finally:
        if _start_task is not None:
            _start_task.cancel()
        _deliver_task.cancel()
        await submission.close()


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
