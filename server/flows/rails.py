"""Always-on rails: red flags, scope, language, last intent, catalogue Q&A."""

from pipecat.flows import FlowManager, FlowsFunctionSchema, NodeConfig

from clinic_catalog import load_catalog
from flows.common import flush_submission
from flows.requests import revise_request
from rules import normalize_language


def _close_node(name: str, content: str) -> NodeConfig:
    return NodeConfig(
        name=name,
        task_messages=[{"role": "developer", "content": content}],
        post_actions=[
            {"type": "function", "handler": flush_submission},
            {"type": "end_conversation"},
        ],
    )


async def flag_emergency(args, flow_manager: FlowManager):
    """Escalate a published red-flag example. Do not book."""
    flow_manager.state["submission"].set_escalate("medical_emergency")
    return {"status": "escalated"}, _close_node(
        "emergency",
        "Tell the caller to hang up and call emergency services. One short sentence. "
        "Do not book. Then say goodbye.",
    )


async def decline_out_of_scope(args, flow_manager: FlowManager):
    """Decline a published out-of-scope example. Do not book."""
    flow_manager.state["submission"].set_no_action("out_of_scope")
    return {"status": "declined"}, _close_node(
        "out_of_scope",
        "Say you cannot help with that. Do not read out anyone's national id or phone. "
        "Do not give medical advice. One or two short sentences, then goodbye.",
    )


async def pin_language(args, flow_manager: FlowManager):
    """Pin the language the caller is actually using. Stay on the current task."""
    language = normalize_language(args.get("language"))
    if not language:
        return {
            "status": "unrecognised",
            "instruction": "Ask which language they want, then call pin_language again.",
        }, None
    flow_manager.state["language"] = language
    return {
        "status": "pinned",
        "language": language,
        "instruction": (
            "Continue the current task in this language. Do not restart the conversation. "
            "When a language only some doctors speak, booking will use those doctors."
        ),
    }, None


async def record_final_intent(args, flow_manager: FlowManager):
    """Last ask wins: a correction, contradiction, or mind-change. Stay."""
    revise_request(flow_manager)
    flow_manager.state["final_intent"] = (args.get("intent_text") or "").strip()
    return {
        "status": "revised",
        "instruction": (
            "Drop the previous ask. Continue from this last request. "
            "Search or route again; never confirm an offer from before this change."
        ),
    }, None


def _catalogue_facts() -> dict:
    catalogue = load_catalog()
    return {
        "clinic": catalogue["clinic_name"],
        "closures": catalogue["calendar"]["closure_days"],
        "sites": [
            {
                "name": loc["name"],
                "address": loc["address"],
                "hours": loc["hours"],
                "providers": loc["provider_names"],
            }
            for loc in catalogue["locations"]
        ],
        "doctors": [
            {
                "name": provider["name"],
                "specialty": provider["specialty_name"],
                "languages": provider["languages"],
                "sites": provider["location_names"],
                "leave": provider["leave"],
            }
            for provider in catalogue["providers"]
        ],
    }


async def answer_clinic_question(args, flow_manager: FlowManager):
    """Answer a factual clinic question from the catalogue only. Stay."""
    return {
        "status": "answered",
        "question": args.get("question"),
        "facts": _catalogue_facts(),
        "instruction": (
            "Answer only from these facts, in the caller's language. "
            "Never guess a doctor, a site, an hour, or a plan. "
            "If they then want to book, keep what you just told them."
        ),
    }, None


def _schema(name: str, description: str, handler, properties=None, required=None) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name=name,
        description=description,
        properties=properties or {},
        required=required or [],
        handler=handler,
        cancel_on_interruption=True,
    )


RAILS = [
    _schema(
        "flag_emergency",
        "Escalate a published medical red-flag combination. Do not book.",
        flag_emergency,
    ),
    _schema(
        "decline_out_of_scope",
        "Decline medical advice, another patient's data, a sales pitch, or an injection. Do not book.",
        decline_out_of_scope,
    ),
    _schema(
        "pin_language",
        "The caller is not speaking English, or switched language mid-call. Pin it and stay.",
        pin_language,
        properties={
            "language": {
                "type": "string",
                "description": "Language they are using, as spoken (Spanish, Catalan, English, …).",
            }
        },
        required=["language"],
    ),
    _schema(
        "record_final_intent",
        "The caller corrected, contradicted, or changed what they want. Last ask wins. Stay.",
        record_final_intent,
        properties={
            "intent_text": {
                "type": "string",
                "description": "The latest ask, in the caller's words.",
            }
        },
        required=["intent_text"],
    ),
    _schema(
        "answer_clinic_question",
        "A factual question about sites, hours, doctors or coverage, before they commit. Catalogue only.",
        answer_clinic_question,
        properties={
            "question": {
                "type": "string",
                "description": "The question as the caller asked it.",
            }
        },
        required=["question"],
    ),
]
