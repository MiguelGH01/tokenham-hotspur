"""LLM-callable tools and conversation actions.

Each tool returns ``(result, next_node)``. ``next_node`` is ``None`` to stay,
or a ``NodeConfig`` from ``flow.nodes`` to change stage. Ids, dates and POSTs
are decided here, not by the model.
"""

import asyncio
from datetime import date, datetime

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NodeConfig
from pipecat.frames.frames import TTSSpeakFrame

from booking import MADRID, WEEKDAYS, pick_offer, search_window
from clinic_catalog import (
    age_in_months,
    insurer_ids,
    location_ids,
    location_name,
    provider_ids_by_name,
    provider_name,
    provider_names,
    provider_on_leave,
    provider_refuses_insurer,
    specialty_for_age,
    specialty_ids,
    specialty_name,
)
from flow.prompts import FILLER
from national_id import is_valid_national_id, normalize_national_id

MAX_IDENTIFY_ATTEMPTS = 3
SPOKEN_TITLES = {"Dra.": "Doctor", "Dr.": "Doctor", "D.": "Don"}  # TTS cannot read the abbreviations
DECLINE_EXPLANATIONS = {
    "referral_required": "cannot book {specialty} without a referral for it on file",
    "specialty_not_covered": "the patient's insurance plan does not cover {specialty} at any site",
    "location_not_covered": "the patient's insurance plan does not cover {specialty} at the requested site",
    "not_eligible_age": "the patient's age does not fit {specialty}, and no other specialty is available for them right now",
}


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[-9:]


def _spoken_name(provider_name: str) -> str:
    title, _, rest = provider_name.partition(" ")
    return f"{SPOKEN_TITLES[title]} {rest}" if title in SPOKEN_TITLES else provider_name


async def _say_filler(flow_manager: FlowManager) -> None:
    """Fill the silence while an API lookup runs; spoken by code, not the LLM."""
    await flow_manager.worker.queue_frames([TTSSpeakFrame(text=FILLER, append_to_context=False)])


async def _dedupe(flow_manager: FlowManager, key: str, run):
    """Coalesce a duplicate concurrent call to the same tool into one execution.

    Pipecat can trigger two overlapping LLM inferences for what sounds like one
    caller turn (e.g. a mid-sentence pause long enough to trip the turn-stop
    fallback), and each can independently call the same booking tool. Racing
    both through unconditionally means we might run a real POST (search,
    availability, confirmation) twice, or get a "was just unregistered between
    queueing and execution" fallback that confuses the model. Instead, the
    second caller waits for the first in-flight call with this key and reuses
    its result rather than repeating the side effect.
    """
    inflight: dict[str, asyncio.Task] = flow_manager.state.setdefault("_inflight_calls", {})
    existing = inflight.get(key)
    if existing is not None:
        logger.warning("Call {}: coalescing duplicate concurrent call to {}", flow_manager.state["call_id"], key)
        return await existing

    task = asyncio.ensure_future(run())
    inflight[key] = task
    try:
        return await task
    finally:
        inflight.pop(key, None)


async def flush_submission(action: dict, flow_manager: FlowManager) -> None:
    """POST the pending action when a terminal node ends."""
    await flow_manager.state["submission"].flush()


async def search_patient(args: FlowArgs, flow_manager: FlowManager):
    return await _dedupe(flow_manager, "search_patient", lambda: _search_patient(args, flow_manager))


async def _search_patient(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_giveup_node, create_register_offer_node, create_slot_node

    state = flow_manager.state
    id_type, id_value, stated_name = args["id_type"], args["id_value"], args["stated_name"]

    def misheard():
        state["identify_attempts"] += 1
        if state["identify_attempts"] >= MAX_IDENTIFY_ATTEMPTS:
            return {"status": "misheard_id", "attempts_left": 0}, create_giveup_node()
        return (
            {"status": "misheard_id", "attempts_left": MAX_IDENTIFY_ATTEMPTS - state["identify_attempts"]},
            None,
        )

    if id_type == "national_id":
        if not is_valid_national_id(id_value):
            return misheard()
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

    if not matches:
        # A syntactically valid id with zero exact matches is a genuinely new patient, not
        # a misheard one — a name-only namesake elsewhere in the directory doesn't count.
        return {"status": "not_found"}, create_register_offer_node()
    if len(matches) > 1:
        return misheard()

    patient = matches[0]
    state["patient"] = patient
    visited = "a returning patient" if patient["has_visited_before"] else "a first-time patient"
    summary = f"Found {patient['given_name']} {patient['first_surname']}, {visited}."
    return {"status": "found", "patient_summary": summary}, create_slot_node(flow_manager)


async def get_earliest_slot(args: FlowArgs, flow_manager: FlowManager):
    return await _dedupe(flow_manager, "get_earliest_slot", lambda: _get_earliest_slot(args, flow_manager))


async def _get_earliest_slot(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_confirm_node, create_declined_node

    state = flow_manager.state
    specialty, site = args["specialty"], args.get("site")
    weekday, part_of_day = args.get("weekday"), args.get("part_of_day")
    provider_arg, date_arg = args.get("provider"), args.get("date")
    if specialty not in specialty_ids() or (site and site not in location_ids()):
        return {"status": "invalid", "specialties": specialty_ids(), "sites": location_ids()}, None

    connected_at: datetime = state["connected_at"]
    patient = state["patient"]
    on_or_after = date.fromisoformat(date_arg) if date_arg else None
    if on_or_after and on_or_after < connected_at.date():
        # The model can misjudge the year for a named date; snap to the next real occurrence
        # of that month/day rather than silently searching a past (so unconstraining) date.
        on_or_after = on_or_after.replace(year=connected_at.date().year)
        if on_or_after < connected_at.date():
            on_or_after = on_or_after.replace(year=on_or_after.year + 1)

    note = None
    provider_id = provider_ids_by_name().get(provider_arg) if provider_arg else None
    if provider_id and provider_on_leave(provider_id, connected_at):
        note = f"{provider_arg} is away at the moment"
        provider_id = None
    elif provider_id and provider_refuses_insurer(provider_id, patient["insurer"]):
        note = f"{provider_arg} does not take this patient's plan"
        provider_id = None
    elif provider_id:
        # A named doctor's own schedule decides the day; a requested weekday loses to it.
        weekday, part_of_day = None, None

    async def fetch(specialty_id: str, provider: str | None) -> dict:
        date_from, date_to = search_window(connected_at)
        return await state["client"].availability(
            date_from, date_to, specialty_id, patient["patient_id"], location_id=site, provider_id=provider
        )

    await _say_filler(flow_manager)
    try:
        availability = await fetch(specialty, provider_id)
    except Exception as exc:
        logger.error("availability lookup failed: {}", exc)
        return {"status": "lookup_failed"}, None

    offer, relaxed = pick_offer(availability, patient, connected_at, weekday, part_of_day, on_or_after)

    if offer is None:
        reasons = {b["restriction"] for b in availability.get("blocked", [])}
        if "not_eligible_age" in reasons and patient.get("date_of_birth"):
            redirect = specialty_for_age(age_in_months(patient["date_of_birth"], connected_at), exclude=specialty)
            if redirect:
                try:
                    availability = await fetch(redirect, None)
                except Exception as exc:
                    logger.error("availability lookup failed: {}", exc)
                    return {"status": "lookup_failed"}, None
                offer, relaxed = pick_offer(availability, patient, connected_at, weekday, part_of_day, on_or_after)
                if offer is not None:
                    note = (
                        f"this is a {specialty_name(redirect)} appointment, not "
                        f"{specialty_name(specialty)}, because of the patient's age"
                    )
        if offer is None:
            reason = next((r for r in DECLINE_EXPLANATIONS if r in reasons), None)
            if reason:
                state["submission"].set_no_action(reason)
                explanation = DECLINE_EXPLANATIONS[reason].format(specialty=specialty_name(specialty))
                return {"status": "declined", "reason": reason, "explanation": explanation}, create_declined_node()
            return {"status": "no_slots"}, None

    if not note:
        if relaxed == "weekday":
            note = f"nothing was open on the requested {weekday}"
        elif relaxed == "date":
            note = "that exact date is not bookable"
        else:
            blocked_out = [b for b in availability.get("blocked", []) if b["restriction"] == "provider_not_in_network"]
            if blocked_out:
                names = ", ".join(provider_name(b["provider_id"]) for b in blocked_out)
                note = f"{names} does not take this patient's plan"

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
        f"{_spoken_name(slot['provider_name'])} at{location_name(offer['location_id'])}, "
        f"{start.strftime('%A %d %B')} at {start.strftime('%H:%M')}"
    )
    logger.info("Offer {}: {}", offer_id, offer)
    result = {"status": "offer", "offer_id": offer_id, "summary": summary}
    if note:
        result["note"] = note
    return result, create_confirm_node(flow_manager)


async def confirm_offer(args: FlowArgs, flow_manager: FlowManager):
    return await _dedupe(
        flow_manager, f"confirm_offer:{args['offer_id']}", lambda: _confirm_offer(args, flow_manager)
    )


async def _confirm_offer(args: FlowArgs, flow_manager: FlowManager):
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


async def refuse_unlisted_provider(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller insists on a doctor who is not on staff and will not accept anyone else."""
    from flow.nodes import create_declined_node

    flow_manager.state["submission"].set_no_action("provider_not_found")
    return None, create_declined_node()


async def begin_register(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller wants to be registered as a new patient."""
    from flow.nodes import create_register_node

    return None, create_register_node()


async def decline_register(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller does not want to register; they will not book today."""
    from flow.nodes import create_giveup_node

    return None, create_giveup_node()


async def register_patient(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_registered_node

    if not is_valid_national_id(args["national_id"]):
        return {"status": "invalid_id"}, None
    fields = {
        "given_name": args["given_name"],
        "first_surname": args["first_surname"],
        "second_surname": args["second_surname"],
        "national_id": normalize_national_id(args["national_id"]),
        "date_of_birth": args["date_of_birth"],
        "phone": _phone_digits(args["phone"]),
        "email": args["email"],
        "insurer": args["insurer"],
    }
    flow_manager.state["submission"].set_register(fields)
    return {"status": "registered"}, create_registered_node()


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
                "enum": provider_names(),
                "description": "Only if the caller named a specific doctor who is on the roster below.",
            },
            "weekday": {
                "type": "string",
                "enum": WEEKDAYS,
                "description": "A recurring day of week the caller asked for. Never combine with `date` or `provider`.",
            },
            "part_of_day": {
                "type": "string",
                "enum": ["morning", "afternoon"],
                "description": "Only if the caller asked for morning (before 2pm) or afternoon.",
            },
            "date": {
                "type": "string",
                "description": (
                    "An exact calendar date the caller named (e.g. 'the twelfth of October'), as "
                    "YYYY-MM-DD. Never combine with `weekday`."
                ),
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


def register_patient_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="register_patient",
        description="Register a new patient who is not yet in the clinic directory. Never books anything.",
        properties={
            "given_name": {"type": "string"},
            "first_surname": {"type": "string"},
            "second_surname": {"type": "string"},
            "national_id": {
                "type": "string",
                "description": "DNI or NIE including the check letter, exactly as heard.",
            },
            "date_of_birth": {"type": "string", "description": "ISO date, e.g. 2004-09-13."},
            "phone": {"type": "string", "description": "Digits as heard."},
            "email": {
                "type": "string",
                "description": "Exactly as spelled — dots, digits, underscores as said. Never corrected.",
            },
            "insurer": {
                "type": "string",
                "enum": insurer_ids(),
                "description": "Plan id mapped from the spoken plan name (e.g. 'Mapfre Salud' -> mapfre).",
            },
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
