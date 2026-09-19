"""LLM-callable tools and conversation actions.

Each tool returns ``(result, next_node)``. ``next_node`` is ``None`` to stay,
or a ``NodeConfig`` from ``flow.nodes`` to change stage. Ids, dates and POSTs
are decided here, not by the model.
"""

from datetime import datetime

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NodeConfig
from pipecat.frames.frames import TTSSpeakFrame

from booking import MADRID, WEEKDAYS, pick_offer, search_window
from clinic.clinic_catalog import (
    closure_days,
    location_ids,
    location_name,
    provider_on_leave,
    providers,
    specialty_ids,
)
from flow.prompts import FILLER
from national_id import is_valid_national_id, normalize_national_id

MAX_IDENTIFY_ATTEMPTS = 3
NOT_LISTED = "not_listed"  # the caller named a doctor who is not on the clinic's staff
SPOKEN_TITLES = {"Dra.": "Doctor", "Dr.": "Doctor", "D.": "Don"}  # TTS cannot read the abbreviations


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[-9:]


def _spoken_name(provider_name: str) -> str:
    title, _, rest = provider_name.partition(" ")
    return f"{SPOKEN_TITLES[title]} {rest}" if title in SPOKEN_TITLES else provider_name


def _provider_options() -> str:
    return "; ".join(f"{p['id']} = {p['name']} ({p['specialty_id']})" for p in providers())


async def _say_filler(flow_manager: FlowManager) -> None:
    """Fill the silence while an API lookup runs; spoken by code, not the LLM."""
    await flow_manager.worker.queue_frames([TTSSpeakFrame(text=FILLER, append_to_context=False)])


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

    await _say_filler(flow_manager)
    try:
        matches = [m for m in await state["client"].search_directory(**query) if exact(m)]
    except Exception as exc:
        logger.error("directory lookup failed: {}", exc)
        return {"status": "lookup_failed"}, None

    if len(matches) != 1:
        return failed("not_found")

    patient = matches[0]
    state["patient"] = patient
    state["submission"].set_no_outcome()  # from here on "patient_not_found" would be false
    visited = "a returning patient" if patient["has_visited_before"] else "a first-time patient"
    summary = f"Found {patient['given_name']} {patient['first_surname']}, {visited}."
    return {"status": "found", "patient_summary": summary}, create_slot_node(flow_manager)


async def get_earliest_slot(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_confirm_node

    state = flow_manager.state
    specialty, site = args["specialty"], args.get("site")
    weekday, part_of_day = args.get("weekday"), args.get("part_of_day")
    provider = args.get("provider")
    if specialty not in specialty_ids() or (site and site not in location_ids()):
        return {"status": "invalid", "specialties": specialty_ids(), "sites": location_ids()}, None
    if provider == NOT_LISTED:
        # The truthful reason if the caller will see nobody else; a later offer clears it.
        state["submission"].set_no_action("provider_not_found")
        return {"status": "provider_not_found"}, None
    if provider and provider not in {p["id"] for p in providers()}:
        return {"status": "invalid", "providers": _provider_options()}, None

    connected_at: datetime = state["connected_at"]
    date_from, date_to = search_window(connected_at)
    await _say_filler(flow_manager)
    try:
        availability = await state["client"].availability(
            date_from, date_to, specialty, state["patient"]["patient_id"], location_id=site
        )
    except Exception as exc:
        logger.error("availability lookup failed: {}", exc)
        return {"status": "lookup_failed"}, None

    def pick(weekday, provider_id):
        return pick_offer(
            availability, state["patient"], connected_at, weekday, part_of_day,
            closed_days=closure_days(), provider_id=provider_id,
        )  # fmt: skip

    # A named doctor: which constraint gives way is the clinic's rule, not the model's call.
    note = None
    if provider and provider_on_leave(provider, connected_at.astimezone(MADRID).date()):
        provider, note = None, "on_leave"  # keep specialty + site, drop the doctor
    offer = pick(weekday, provider)
    if offer is None and provider and weekday:
        offer, note = pick(None, provider), "other_day"  # keep doctor + site, drop the day
    if offer is None:
        # If the call ends here this is the truthful reason; a later offer clears it.
        # `blocked` names the standing restriction that stopped a provider (API-avail-blocked);
        # its values mirror OutcomeReason one to one.
        restriction = next((b["restriction"] for b in availability.get("blocked", [])), None)
        state["submission"].set_no_action(restriction or "no_availability")
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
    state["submission"].set_no_outcome()  # a slot exists: an earlier "no_availability" is stale
    summary = (
        f"{_spoken_name(slot['provider_name'])} at {location_name(offer['location_id'])}, "
        f"{start.strftime('%A %d %B')} at {start.strftime('%H:%M')}"
    )
    logger.info("Offer {}: {}", offer_id, offer)
    result = {"status": "offer", "offer_id": offer_id, "summary": summary}
    if note:
        result["note"] = note
    return result, create_confirm_node(flow_manager, note)


async def confirm_offer(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_goodbye_node

    offers = flow_manager.state["offers"]
    # Only the offer on the table: an earlier one was declined when the caller revised the search.
    if not offers or args["offer_id"] != list(offers)[-1]:
        return {"status": "expired"}, None
    offer = offers[args["offer_id"]]
    flow_manager.state["submission"].set_book(offer)
    return {"status": "confirmed"}, create_goodbye_node()


async def revise_search(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller wants a different specialty, site, day or time."""
    from flow.nodes import create_slot_node

    return None, create_slot_node(flow_manager)


async def end_without_booking(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller does not want any appointment you can offer; end the call without booking."""
    from flow.nodes import create_close_node

    return None, create_close_node()


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
            "provider": {
                "type": "string",
                "enum": [p["id"] for p in providers()] + [NOT_LISTED],
                "description": (
                    "Only if the caller named a doctor. Staff: " + _provider_options() + ". Surnames "
                    "one letter apart are different people: tell them apart by the specialty the "
                    f"caller wants, and ask if unsure. Use {NOT_LISTED} if nobody on staff matches."
                ),
            },
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
        properties={"offer_id": {"type": "string", "enum": list(flow_manager.state["offers"])[-1:]}},
        required=["offer_id"],
        handler=confirm_offer,
    )
