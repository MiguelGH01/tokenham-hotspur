"""Clínica Arenal receptionist — Pipecat cascade pipeline + Flows graph (see handlers.py).

Run the bot using::

    uv run bot.py            # all real transports
    uv run bot.py -t eval    # headless eval server for `pipecat eval run`
"""

import asyncio
import os
import uuid
from datetime import datetime
from types import SimpleNamespace

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.evals.transport import EvalTransportParams
from pipecat.flows import FlowManager
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
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
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.elevenlabs.dialogue.tts import ElevenLabsDialogueTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
from pipecat.services.soniox.stt import SonioxSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

try:
    from pipecat.transports.daily.transport import DailyParams
except ImportError:  # daily-python has no Windows wheels
    DailyParams = None

from affirmation_watch import AffirmationWatch
from booking import MADRID
from call_metrics import build_observer, call_ended, call_started
from clients.clinic_client import ClinicClient, DryRunSubmit
from clinic_catalog import load_catalog
from eval_judge import helmcode_judge
from flows.common import GREETING
from flows.reception import create_reception_node
from krisp_model import ensure_filter_model, existing_filter_model_path
from liveness import SilenceWatchdog
from llm_deadline import FirstTokenDeadlineLLM
from resolution import resolve_fallback
from submission import CallSubmission

load_dotenv(os.getenv("DOTENV_PATH") or ".env", override=True)
ensure_filter_model()

# The platform cuts a call after ~30-35s without audible agent audio (SC-silence-cut) and
# the simulated caller takes a median of 11s (max measured 42.5s) to answer. Counted from when
# the bot STOPS speaking; 16s here is ~20s from when it started asking.
REPROMPT_AFTER_SILENCE_SECS = 16.0


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
        if m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
        and m["content"].strip()
    ]
    return f"Sorry, are you still there? {spoken[-1]}" if spoken else GREETING


_CLOUDFLARE_DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"


def _cloudflare_messages_base_url(account_id: str) -> str:
    """Anthropic SDK posts to ``{base_url}/v1/messages``.

    Cloudflare's unified Messages endpoint is
    ``/accounts/{account_id}/ai/v1/messages``, so the base URL must stop at
    ``/ai`` — not ``/ai/v1``.
    """
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai"


class _CloudflareMessages:
    """Pipecat calls ``client.beta.messages.create``, which hits ``/v1/messages?beta=true``.

    Cloudflare's unified endpoint rejects that query string (``unrecognized_keys: query``).
    Route those calls to the stable Messages API instead.
    """

    def __init__(self, messages):
        self._messages = messages

    async def create(self, **kwargs):
        kwargs.pop("betas", None)
        return await self._messages.create(**kwargs)


def _cloudflare_llm() -> AnthropicLLMService:
    """Claude (default Sonnet 4.6) via Cloudflare's Anthropic-compatible API.

    Billed with AI Gateway Unified Billing credits — Workers Paid unlocks
    Workers AI hosted models, not third-party Claude tokens. Token needs
    Account > Workers AI > Read.
    """
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not token:
        raise RuntimeError(
            "LLM_PROVIDER=cloudflare needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN "
            "(Account > Workers AI > Read). Claude is billed with AI Gateway Unified "
            "Billing credits, not Workers Paid neurons."
        )
    model = os.getenv("CLOUDFLARE_LLM_MODEL", _CLOUDFLARE_DEFAULT_MODEL)
    headers: dict[str, str] = {}
    gateway_id = os.getenv("CLOUDFLARE_AI_GATEWAY_ID")
    if gateway_id:
        headers["cf-aig-gateway-id"] = gateway_id
    inner = AsyncAnthropic(
        auth_token=token,
        base_url=_cloudflare_messages_base_url(account_id),
        default_headers=headers or None,
    )
    client = SimpleNamespace(beta=SimpleNamespace(messages=_CloudflareMessages(inner.messages)))
    return AnthropicLLMService(
        api_key=token,
        client=client,
        settings=AnthropicLLMService.Settings(
            model=model,
            # Voice: don't wait on extended thinking before the first spoken token.
            thinking=AnthropicLLMService.ThinkingConfig(type="disabled"),
        ),
    )


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
    provider = os.getenv("LLM_PROVIDER", "cloudflare")
    if provider in {"cloudflare", "claude"}:
        return _cloudflare_llm()
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

    Without an ``OPENAI_API_KEY`` the scenarios could not run at all, so the judge
    falls back to the Helmcode gateway (``eval_judge.helmcode_judge``). Verdicts from
    the two judges are not comparable; the log line says which one ran.
    """
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("Eval judge: no OPENAI_API_KEY, judging with Helmcode instead of gpt-5.1")
        return helmcode_judge(config or {})
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
        return UserTurnStrategies(
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]
        )
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


def build_stt():
    provider = os.getenv("STT_PROVIDER", "soniox")
    if provider == "deepgram":
        return DeepgramSTTService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            settings=_stt_settings(),
        )
    return SonioxSTTService(
        api_key=os.environ["SONIOX_API_KEY"],
        settings=SonioxSTTService.Settings(
            model=os.getenv("SONIOX_MODEL", "stt-rt-v5"),
            language_hints=[Language.ES, Language.CA, Language.EN, Language.EU, Language.GL],
            enable_language_identification=True,
        ),
    )


_ELEVENLABS_V3_MODELS = frozenset({"eleven_v3", "eleven_v3_conversational"})


def build_tts(*, telephony: bool = False):
    provider = os.getenv("TTS_PROVIDER", "elevenlabs")
    if provider == "deepgram":
        return DeepgramTTSService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            # Telephony is 8 kHz end to end, so asking the synthesiser for 8 kHz
            # avoids a resample per call — one less thing competing for the CPU when
            # a Run All holds ten calls open at once.
            sample_rate=int(os.getenv("DEEPGRAM_TTS_SAMPLE_RATE", "8000" if telephony else "0"))
            or None,
            settings=DeepgramTTSService.Settings(
                voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-helena-en"),
                speed=float(os.getenv("DEEPGRAM_TTS_SPEED", "1.0")),
            ),
        )
    model = os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5")
    voice = os.environ["ELEVENLABS_VOICE_ID"]
    language = os.getenv("ELEVENLABS_LANGUAGE", "es")
    # v3 only speaks through Text-to-Dialogue. Names like eleven_flash_v3 are not
    # a real model: the classic TTS WebSocket accepts the socket then returns no audio.
    use_dialogue = model in _ELEVENLABS_V3_MODELS or ("v3" in model and "ttv" not in model)
    if use_dialogue:
        if model not in _ELEVENLABS_V3_MODELS:
            logger.warning(
                "ELEVENLABS_MODEL={} is not a TTS WebSocket model; using "
                "ElevenLabsDialogueTTSService with eleven_v3_conversational. "
                "Set eleven_v3 or eleven_v3_conversational explicitly.",
                model,
            )
            model = "eleven_v3_conversational"
        logger.info("TTS: ElevenLabs Text-to-Dialogue ({})", model)
        return ElevenLabsDialogueTTSService(
            api_key=os.environ["ELEVENLABS_API_KEY"],
            settings=ElevenLabsDialogueTTSService.Settings(
                voice=voice,
                model=model,
                language=language,
            ),
        )
    logger.info("TTS: ElevenLabs WebSocket ({})", model)
    return ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        settings=ElevenLabsTTSService.Settings(
            voice=voice,
            model=model,
            language=language,
        ),
    )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    call_id = _call_id(runner_args)
    logger.info("Starting bot for call {}", call_id)
    telephony = _is_twilio_session(runner_args)

    stt = build_stt()
    tts = build_tts(telephony=telephony)
    llm = build_llm(call_id)

    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_idle_timeout=REPROMPT_AFTER_SILENCE_SECS,
            # filter_incomplete_user_turns stays off: the LLM judged a bare "Hello." as an
            # unfinished turn and went silent for 10s (16 of 21 calls in the first Run All).
            user_turn_strategies=_user_turn_strategies(),
        ),
    )

    user_aggregator = context_aggregator.user()
    assistant_aggregator = context_aggregator.assistant()

    # Speaks a holding line instead of leaving dead air when the bot stalls.
    # Placed before the LLM so both frames it emits travel downstream correctly.
    watchdog = SilenceWatchdog(
        silence_secs=float(os.getenv("SILENCE_GUARD_SECS", "6")),
        rerun_after_secs=float(os.getenv("SILENCE_RERUN_SECS", "18")),
        max_reruns=int(os.getenv("SILENCE_MAX_RERUNS", "2")),
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
        observers=[build_observer(call_id)],
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
    client = (
        DryRunSubmit(ClinicClient())
        if isinstance(runner_args, EvalRunnerArguments)
        else ClinicClient()
    )
    submission = CallSubmission(
        call_id,
        # The eval lane is dialled by the harness, not by the platform, so the
        # platform refuses its minted call_id with a 404 and every booking in the
        # lane "fails" for a reason that has nothing to do with the bot. Its writes
        # are answered locally instead; reads still go to the real clinic API.
        client,
        # Only asked when the call ends having decided nothing: the best ending
        # it can still stand behind beats the ``out_of_scope`` that matches no
        # published case. See ``resolution.py``.
        fallback=lambda: resolve_fallback(flow_manager.state),
    )
    flow_manager.state.update(
        {
            "call_id": call_id,
            "connected_at": _connected_at(
                allow_override=isinstance(runner_args, EvalRunnerArguments)
            ),
            "client": client,
            "submission": submission,
            "patient": None,
            "offers": {},
            "identify_attempts": 0,
        }
    )

    @user_aggregator.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        text = _reprompt(context)
        logger.info(
            "Call {}: {}s of silence, re-prompting: {}", call_id, REPROMPT_AFTER_SILENCE_SECS, text
        )
        await worker.queue_frames([TTSSpeakFrame(text=text, append_to_context=False)])

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
        call_started(call_id)
        # Any telephony socket, not just one detected as "twilio": with no RTVI client-ready,
        # nothing else would ever start the flow and the bot would stay silent until cut off.
        if isinstance(runner_args, WebSocketRunnerArguments):
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
        call_ended(call_id)
        if _start_task is not None:
            _start_task.cancel()
        _deliver_task.cancel()
        await submission.close()
        inner = getattr(client, "_client", client)
        if hasattr(inner, "aclose"):
            await inner.aclose()


def _audio_kwargs() -> dict:
    return {
        "audio_in_enabled": True,
        "audio_out_enabled": True,
        "audio_in_filter": _audio_in_filter(),
    }


async def _telephony_transport(runner_args: WebSocketRunnerArguments, params) -> BaseTransport:
    """Twilio-shaped WebSocket transport without Twilio credentials.

    create_transport() builds the serializer with auto_hang_up=True, which raises when
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are empty. The harness speaks Twilio Media
    Streams but is not Twilio: it hangs up on its own, so no REST hang-up is needed.
    """
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    if transport_type != "twilio":
        # Sample rates and the call id both hang off this detection; fail loudly, not silently.
        logger.error(
            "Telephony handshake detected as {!r}, expected 'twilio': {}", transport_type, call_data
        )
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
