"""LLM-callable tools. Clinic branches live in the return payload, not in extra nodes.

Return ``(result, next_node)``. ``None`` keeps the current stage. Only identify→act
and a finished call→close change node.
"""

from datetime import datetime

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema

from booking import WEEKDAYS
from clinic.clinic_catalog import location_ids, location_name, match_plan, specialty_ids
from clinic.search import find_offer
from national_id import is_valid_national_id, normalize_national_id

MAX_IDENTIFY_ATTEMPTS = 3


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[-9:]


def _norm_email(value: str) -> str:
    return "".join(value.split()).lower()


async def flush_submission(action: dict, flow_manager: FlowManager) -> None:
    await flow_manager.state["submission"].flush()


async def search_patient(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_act_node

    state = flow_manager.state
    id_type, id_value, stated_name = args["id_type"], args["id_value"], args["stated_name"]

    def failed(status: str):
        state["identify_attempts"] += 1
        left = max(0, MAX_IDENTIFY_ATTEMPTS - state["identify_attempts"])
        return {"status": status, "attempts_left": left, "can_register": True}, None

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
    return {"status": "found", "patient_summary": summary}, create_act_node(flow_manager)


async def register_patient(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_close_node

    nid = args["national_id"]
    if not is_valid_national_id(nid):
        return {"status": "misheard_id"}, None
    insurer = match_plan(args["insurer"])
    if not insurer:
        return {"status": "unknown_plan", "hint": "map the spoken insurer to a catalogue plan"}, None
    fields = {
        "given_name": args["given_name"].strip(),
        "first_surname": args["first_surname"].strip(),
        "second_surname": args["second_surname"].strip(),
        "national_id": normalize_national_id(nid),
        "date_of_birth": args["date_of_birth"].strip(),
        "phone": _phone_digits(args["phone"]),
        "email": _norm_email(args["email"]),
        "insurer": insurer,
    }
    flow_manager.state["submission"].set_register(fields)
    return {"status": "registered"}, create_close_node("registered")


async def get_earliest_slot(args: FlowArgs, flow_manager: FlowManager):
    """Search real availability. Stay on act; the model offers whatever this returns."""
    from flow.nodes import create_close_node

    state = flow_manager.state
    if not state.get("patient"):
        return {"status": "need_patient"}, None

    result, offer = await find_offer(
        state["client"],
        state["patient"],
        state["connected_at"],
        specialty=args["specialty"],
        site=args.get("site"),
        provider_spoken=args.get("provider"),
        when_text=args.get("when"),
        weekday=args.get("weekday"),
        part_of_day=args.get("part_of_day"),
        others_ok=args.get("others_ok", True),
    )
    if result["status"] == "lookup_failed":
        logger.error("availability lookup failed: {}", result.get("error"))
        return {"status": "lookup_failed"}, None
    if result["status"] == "refused":
        state["submission"].set_no_action(result["reason"])
        if result["reason"] == "provider_not_found":
            return result, create_close_node("refused")
        return result, None
    if offer is None:
        return result, None

    slot = result.pop("slot_meta")
    start = datetime.fromisoformat(offer["slot"])
    offer_id = f"offer-{len(state['offers']) + 1}"
    state["offers"][offer_id] = offer
    summary = (
        f"{slot['provider_name']} at {location_name(offer['location_id'])}, "
        f"{start.strftime('%A %d %B')} at {start.strftime('%H:%M')}"
    )
    logger.info("Offer {}: {}", offer_id, offer)
    return {
        "status": "offer",
        "offer_id": offer_id,
        "summary": summary,
        "specialty": result.get("specialty"),
        "blocked": result.get("blocked") or [],
    }, None


async def confirm_offer(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_close_node

    offer = flow_manager.state["offers"].get(args["offer_id"])
    if offer is None:
        return {"status": "expired"}, None
    flow_manager.state["submission"].set_book(offer)
    return {"status": "confirmed"}, create_close_node("booked")


async def decline_other_providers(flow_manager: FlowManager):
    """Named doctor does not exist and the caller will not see anyone else."""
    from flow.nodes import create_close_node

    flow_manager.state["submission"].set_no_action("provider_not_found")
    return {"status": "refused", "reason": "provider_not_found"}, create_close_node("refused")


async def revise_search(flow_manager: FlowManager):
    """Caller wants a different specialty, site, day or time. Stay in act and search again."""
    return {"status": "revise"}, None


async def flag_emergency(flow_manager: FlowManager):
    """Caller describes a published medical emergency. Do not book."""
    from flow.nodes import create_close_node

    flow_manager.state["submission"].set_escalate("medical_emergency")
    return {"status": "escalated"}, create_close_node("emergency")


async def decline_out_of_scope(flow_manager: FlowManager):
    """Caller asks for another patient's data, medical advice, injection, or a sales pitch."""
    from flow.nodes import create_close_node

    flow_manager.state["submission"].set_no_action("out_of_scope")
    return {"status": "declined"}, create_close_node("out_of_scope")


async def pin_language(flow_manager: FlowManager, language: str):
    """Caller is not in English or switched mid-call. language: es, ca, en, or similar."""
    flow_manager.state["language"] = language
    return {"status": "pinned", "language": language}, None


async def record_final_intent(flow_manager: FlowManager, summary: str):
    """Caller corrected themselves or changed their mind. summary: their latest ask."""
    flow_manager.state["final_intent"] = summary
    return {"status": "noted", "summary": summary}, None


def search_patient_schema() -> FlowsFunctionSchema:
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


def register_patient_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="register_patient",
        description=(
            "Directory miss: register this person. Do not book. Pass names as spoken "
            "(keep accents), the full DNI/NIE with letter, ISO date of birth, phone, "
            "email exactly as dictated, and the spoken insurer name."
        ),
        properties={
            "given_name": {"type": "string"},
            "first_surname": {"type": "string"},
            "second_surname": {"type": "string"},
            "national_id": {"type": "string"},
            "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "insurer": {"type": "string", "description": "Spoken plan, e.g. Mapfre Salud."},
        },
        required=[
            "given_name",
            "first_surname",
            "second_surname",
            "national_id",
            "date_of_birth",
            "phone",
            "email",
            "insurer",
        ],
        handler=register_patient,
    )


def get_earliest_slot_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="get_earliest_slot",
        description=(
            "Find a real bookable slot for the identified patient. Pass the spoken doctor "
            "name and when-phrase; the clinic engine resolves leave, closed days, and coverage."
        ),
        properties={
            "specialty": {"type": "string", "enum": specialty_ids()},
            "site": {"type": "string", "enum": location_ids(), "description": "Only if the caller asked for a site."},
            "provider": {"type": "string", "description": "Spoken doctor name if they named one."},
            "when": {
                "type": "string",
                "description": "Spoken when, e.g. tomorrow, this coming Thursday, Saturday morning.",
            },
            "weekday": {"type": "string", "enum": WEEKDAYS, "description": "Only if they named a weekday and not a date phrase."},
            "part_of_day": {
                "type": "string",
                "enum": ["morning", "afternoon"],
                "description": "Only if the caller asked for morning (before 2pm) or afternoon.",
            },
            "others_ok": {
                "type": "boolean",
                "description": "False when they already said they will not see any other doctor.",
            },
        },
        required=["specialty"],
        handler=get_earliest_slot,
    )


def confirm_offer_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="confirm_offer",
        description="Caller accepted the last offered appointment. Pass the offer_id the search tool returned.",
        properties={"offer_id": {"type": "string", "description": "Id from get_earliest_slot, e.g. offer-1."}},
        required=["offer_id"],
        handler=confirm_offer,
    )


RAILS = [flag_emergency, decline_out_of_scope, pin_language, record_final_intent]
