"""Pipecat Flows graph for PR-01 Simple Booking.

Identify -> Consultar hueco -> Confirmar -> Despedida. The LLM only maps language to
enum values and reads summaries back; ids, dates and the submission never reach it.
Schemas restrict what the model can pass; handlers re-validate (schema guides, handler gates).
"""

from datetime import datetime

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NodeConfig

from booking import MADRID, WEEKDAYS, pick_offer, search_window
from clinic_catalog import location_ids, location_name, specialty_ids
from national_id import is_valid_national_id, normalize_national_id

MAX_IDENTIFY_ATTEMPTS = 3
GREETING = "Clínica Arenal, how can I help you?"

ROLE_MESSAGE = (
    "You are the receptionist for Clínica Arenal, on the phone. Your responses will be "
    "spoken aloud, so avoid emojis, bullet points, or other formatting that cannot be "
    "spoken. Keep replies to one or two short sentences. Answer in the caller's language. "
    "Never invent a slot, doctor, site, or rule: use only what the tools return. Never read "
    "out anyone's national id or phone number. Always use the available functions to move "
    "the conversation forward."
)


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[-9:]


# --- actions -------------------------------------------------------------------


async def flush_submission(action: dict, flow_manager: FlowManager) -> None:
    await flow_manager.state["submission"].flush()


# --- tools ---------------------------------------------------------------------


async def search_patient(args: FlowArgs, flow_manager: FlowManager):
    state = flow_manager.state
    id_type, id_value, stated_name = args["id_type"], args["id_value"], args["stated_name"]

    def failed(status: str):
        state["identify_attempts"] += 1
        if state["identify_attempts"] >= MAX_IDENTIFY_ATTEMPTS:
            return {"status": status, "attempts_left": 0}, create_giveup_node()
        return {"status": status, "attempts_left": MAX_IDENTIFY_ATTEMPTS - state["identify_attempts"]}, None

    if id_type == "national_id":
        if not is_valid_national_id(id_value):
            return failed("misheard_id")
        wanted = normalize_national_id(id_value)
        query = {"name": stated_name, "national_id": wanted}
        exact = lambda m: normalize_national_id(m["national_id"]) == wanted  # noqa: E731
    else:
        wanted = _phone_digits(id_value)
        query = {"name": stated_name, "phone": wanted}
        exact = lambda m: _phone_digits(m["phone"]) == wanted  # noqa: E731

    try:
        matches = [m for m in await state["client"].search_directory(**query) if exact(m)]
    except Exception as exc:
        logger.error("directory lookup failed: {}", exc)
        return {"status": "lookup_failed"}, None

    if len(matches) != 1:
        return failed("not_found")

    patient = matches[0]
    state["patient"] = patient
    visited = "a returning patient" if patient["has_visited_before"] else "a first-time patient"
    summary = f"Found {patient['given_name']} {patient['first_surname']}, {visited}."
    return {"status": "found", "patient_summary": summary}, create_slot_node(flow_manager)


async def get_earliest_slot(args: FlowArgs, flow_manager: FlowManager):
    state = flow_manager.state
    specialty, site = args["specialty"], args.get("site")
    weekday, part_of_day = args.get("weekday"), args.get("part_of_day")
    if specialty not in specialty_ids() or (site and site not in location_ids()):
        return {"status": "invalid", "specialties": specialty_ids(), "sites": location_ids()}, None

    connected_at: datetime = state["connected_at"]
    date_from, date_to = search_window(connected_at)
    try:
        availability = await state["client"].availability(
            date_from, date_to, specialty, state["patient"]["patient_id"], location_id=site
        )
    except Exception as exc:
        logger.error("availability lookup failed: {}", exc)
        return {"status": "lookup_failed"}, None

    offer = pick_offer(availability, state["patient"], connected_at, weekday, part_of_day)
    if offer is None:
        return {"status": "no_slots"}, None

    slot = next(s for s in availability["slots"] if s["provider_id"] == offer["provider_id"]
                and datetime.fromisoformat(s["start_time"]).astimezone(MADRID).isoformat() == offer["slot"])
    start = datetime.fromisoformat(offer["slot"])
    offer_id = f"offer-{len(state['offers']) + 1}"
    state["offers"][offer_id] = offer
    summary = (
        f"{slot['provider_name']} at {location_name(offer['location_id'])}, "
        f"{start.strftime('%A %d %B')} at {start.strftime('%H:%M')}"
    )
    logger.info("Offer {}: {}", offer_id, offer)
    return {"status": "offer", "offer_id": offer_id, "summary": summary}, create_confirm_node(flow_manager)


async def confirm_offer(args: FlowArgs, flow_manager: FlowManager):
    offer = flow_manager.state["offers"].get(args["offer_id"])
    if offer is None:
        return {"status": "expired"}, None
    flow_manager.state["submission"].set_book(offer)
    return {"status": "confirmed"}, create_goodbye_node()


async def revise_search(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller wants a different specialty, site, day or time."""
    return None, create_slot_node(flow_manager)


# --- schemas (built per call so enums reflect real state) --------------------------


def _search_patient_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="search_patient",
        description="Look the patient up in the clinic records by name plus one exact identifier.",
        properties={
            "stated_name": {"type": "string", "description": "Patient's full name as the caller said it."},
            "id_type": {"type": "string", "enum": ["national_id", "phone"]},
            "id_value": {
                "type": "string",
                "description": "DNI/NIE including the final letter, or the phone number, digits as heard.",
            },
        },
        required=["stated_name", "id_type", "id_value"],
        handler=search_patient,
    )


def _get_earliest_slot_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="get_earliest_slot",
        description="Find the earliest bookable appointment for the identified patient.",
        properties={
            "specialty": {"type": "string", "enum": specialty_ids()},
            "site": {"type": "string", "enum": location_ids(), "description": "Only if the caller asked for a site."},
            "weekday": {"type": "string", "enum": WEEKDAYS, "description": "Only if the caller asked for a weekday."},
            "part_of_day": {
                "type": "string",
                "enum": ["morning", "afternoon"],
                "description": "Only if the caller asked for morning (before 2pm) or afternoon.",
            },
        },
        required=["specialty"],
        handler=get_earliest_slot,
    )


def _confirm_offer_schema(flow_manager: FlowManager) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="confirm_offer",
        description="The caller accepted the offered appointment.",
        properties={"offer_id": {"type": "string", "enum": list(flow_manager.state["offers"])}},
        required=["offer_id"],
        handler=confirm_offer,
    )


# --- nodes ---------------------------------------------------------------------


def create_identify_node() -> NodeConfig:
    return NodeConfig(
        name="identify",
        role_message=ROLE_MESSAGE,
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Establish the patient's full name and ONE exact identifier: their DNI or NIE "
                    "including the letter, or their phone number. Ask for whatever is missing, one "
                    "short question at a time, then call search_patient. Never search by name "
                    "alone. If the result is misheard_id or not_found, say you could not find them "
                    "and ask them to repeat the identifier slowly, digit by digit."
                ),
            }
        ],
        # The fixed greeting is queued by bot.py after initialize(): as a tts_say pre_action,
        # a caller barging into it drops Flows' ActionFinishedFrame and the node never loads.
        respond_immediately=False,
        functions=[_search_patient_schema()],
    )


def create_slot_node(flow_manager: FlowManager) -> NodeConfig:
    patient = flow_manager.state["patient"]
    return NodeConfig(
        name="find_slot",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"The patient is {patient['given_name']} {patient['first_surname']}. Find out "
                    "which specialty they need, mapping their words to one of the allowed values. "
                    "Only pass site, weekday or part_of_day if the caller asked for them. Then call "
                    "get_earliest_slot. If it returns no_slots, say nothing is available for that "
                    "request and ask whether they would drop a constraint. If lookup_failed, "
                    "apologise and ask them to call back shortly."
                ),
            }
        ],
        functions=[_get_earliest_slot_schema()],
    )


def create_confirm_node(flow_manager: FlowManager) -> NodeConfig:
    return NodeConfig(
        name="confirm",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Offer the appointment from the summary the tool returned: doctor, site, day "
                    "and time. Ask if that works. Nothing is booked until they say yes. On yes, "
                    "call confirm_offer. If they want something different, call revise_search."
                ),
            }
        ],
        functions=[_confirm_offer_schema(flow_manager), revise_search],
    )


def create_goodbye_node() -> NodeConfig:
    return NodeConfig(
        name="goodbye",
        task_messages=[
            {
                "role": "developer",
                "content": "Confirm the appointment is booked and say goodbye, in one short sentence.",
            }
        ],
        post_actions=[{"type": "function", "handler": flush_submission}, {"type": "end_conversation"}],
    )


def create_giveup_node() -> NodeConfig:
    return NodeConfig(
        name="giveup",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Apologise that you could not find them in the clinic records, suggest they "
                    "call back with their document at hand, and say goodbye. One or two sentences."
                ),
            }
        ],
        post_actions=[{"type": "function", "handler": flush_submission}, {"type": "end_conversation"}],
    )
