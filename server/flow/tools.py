"""LLM-callable tools and conversation actions.

Each tool returns ``(result, next_node)``. ``next_node`` is ``None`` to stay,
or a ``NodeConfig`` from ``flow.nodes`` to change stage. Ids, dates and POSTs
are decided here, not by the model.
"""

from datetime import datetime

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NodeConfig

from booking import MADRID, WEEKDAYS, pick_offer, search_window
from clinic_catalog import location_ids, location_name, specialty_ids
from national_id import is_valid_national_id, normalize_national_id

MAX_IDENTIFY_ATTEMPTS = 3


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[-9:]


async def flush_submission(action: dict, flow_manager: FlowManager) -> None:
    """POST the pending action when a terminal node ends."""
    await flow_manager.state["submission"].flush()


async def search_patient(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_giveup_node, create_slot_node

    state = flow_manager.state
    id_type, id_value, stated_name = args["id_type"], args["id_value"], args["stated_name"]

    def failed(status: str):
        state["identify_attempts"] += 1
        if state["identify_attempts"] >= MAX_IDENTIFY_ATTEMPTS:
            return {"status": status, "attempts_left": 0}, create_giveup_node()
        return (
            {"status": status, "attempts_left": MAX_IDENTIFY_ATTEMPTS - state["identify_attempts"]},
            None,
        )

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
    from flow.nodes import create_confirm_node

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

    slot = next(
        s
        for s in availability["slots"]
        if s["provider_id"] == offer["provider_id"]
        and datetime.fromisoformat(s["start_time"]).astimezone(MADRID).isoformat() == offer["slot"]
    )
    start = datetime.fromisoformat(offer["slot"])
    offer_id = f"offer-{len(state['offers']) + 1}"
    state["offers"][offer_id] = offer
    summary = (
        f"{slot['provider_name']} at {location_name(offer['location_id'])}, "
        f"{start.strftime('%A %d %B')} at {start.strftime('%H:%M')}"
    )
    logger.info("Offer {}: {}", offer_id, offer)
    return {"status": "offer", "offer_id": offer_id, "summary": summary}, create_confirm_node(
        flow_manager
    )


async def confirm_offer(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_goodbye_node

    offer = flow_manager.state["offers"].get(args["offer_id"])
    if offer is None:
        return {"status": "expired"}, None
    flow_manager.state["submission"].set_book(offer)
    return {"status": "confirmed"}, create_goodbye_node()


async def revise_search(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller wants a different specialty, site, day or time."""
    from flow.nodes import create_slot_node

    return None, create_slot_node(flow_manager)


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


def get_earliest_slot_schema() -> FlowsFunctionSchema:
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


def confirm_offer_schema(flow_manager: FlowManager) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="confirm_offer",
        description="The caller accepted the offered appointment.",
        properties={"offer_id": {"type": "string", "enum": list(flow_manager.state["offers"])}},
        required=["offer_id"],
        handler=confirm_offer,
    )
