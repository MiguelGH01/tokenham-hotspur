"""Booking conversation and catalogue-backed provider selection."""

import unicodedata
from datetime import datetime
from difflib import SequenceMatcher

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NodeConfig

from booking import MADRID, WEEKDAYS, pick_offer, search_window
from clinic_catalog import load_catalog, location_ids, location_name, specialty_ids
from flows.common import create_goodbye_node


def _fold(value):
    return "".join(
        c for c in unicodedata.normalize("NFD", value.lower()) if not unicodedata.combining(c)
    ).replace(".", "")


def resolve_provider(name, specialty=None):
    tokens = [t for t in _fold(name).split() if t not in ("dr", "dra", "doctor", "doctora")]
    matches = []
    for provider in load_catalog()["providers"]:
        if specialty and provider["specialty_id"] != specialty:
            continue
        words = _fold(provider["name"]).split()
        if tokens and all(
            any(SequenceMatcher(None, t, w).ratio() >= 0.85 for w in words) for t in tokens
        ):
            matches.append(provider)
    return matches


async def get_earliest_slot(args: FlowArgs, flow_manager: FlowManager):
    state = flow_manager.state
    specialty, site = args.get("specialty"), args.get("site")
    provider_id = None
    if args.get("provider_name"):
        providers = resolve_provider(args["provider_name"], specialty)
        if len(providers) != 1:
            state["submission"].set_no_action("provider_not_found")
            return {
                "status": "ambiguous_provider" if providers else "provider_not_found",
                "candidates": [
                    {"name": p["name"], "specialty": p["specialty_id"]} for p in providers
                ],
                "instruction": "Clarify full name and specialty. Never guess. If no doctor exists and alternatives are declined, finish_without_booking.",
            }, None
        provider_id = providers[0]["id"]
        specialty = providers[0]["specialty_id"]
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
        logger.error("availability lookup failed: {}", type(exc).__name__)
        return {"status": "lookup_failed"}, None

    restrictions = [
        b.get("restriction")
        for b in availability.get("blocked", [])
        if not provider_id or b.get("provider_id") == provider_id
    ]
    reason = next((r for r in restrictions if isinstance(r, str)), "no_availability")
    availability = {
        **availability,
        "slots": [
            s
            for s in availability["slots"]
            if (not site or s["location_id"] == site)
            and s.get("specialty_id", specialty) == specialty
        ],
    }
    if provider_id:
        requested = {
            **availability,
            "slots": [s for s in availability["slots"] if s["provider_id"] == provider_id],
        }
        offer = (
            None
            if reason == "provider_on_leave"
            else pick_offer(requested, state["patient"], connected_at, weekday, part_of_day)
        )
        if offer is None and reason != "provider_on_leave":
            offer = pick_offer(requested, state["patient"], connected_at, None, part_of_day)
        if offer is None and args.get("allow_alternative") is not True:
            state["submission"].set_no_action(reason)
            return {
                "status": "provider_unavailable",
                "blocked": availability.get("blocked", []),
                "instruction": "Ask consent for another doctor at the SAME site and specialty. Never silently relax constraints.",
            }, None
        if offer is None:
            alternatives = {
                **availability,
                "slots": [s for s in availability["slots"] if s["provider_id"] != provider_id],
            }
            offer = pick_offer(alternatives, state["patient"], connected_at, weekday, part_of_day)
    else:
        offer = pick_offer(availability, state["patient"], connected_at, weekday, part_of_day)
    if offer is None:
        state["submission"].set_no_action(reason)
        return {"status": "no_slots", "blocked": availability.get("blocked", [])}, None

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
    logger.info("Offer prepared")
    return {"status": "offer", "offer_id": offer_id, "summary": summary}, create_confirm_node(
        flow_manager
    )


async def confirm_offer(args: FlowArgs, flow_manager: FlowManager):
    offer = flow_manager.state["offers"].get(args["offer_id"])
    if offer is None:
        return {"status": "expired"}, None
    submission = flow_manager.state["submission"]
    if not submission.delivery_started:
        submission.set_book(offer)
    accepted = await submission.flush()
    return {"status": "accepted" if accepted else "delivery_failed"}, create_goodbye_node(accepted)


async def revise_search(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller wants a different specialty, site, day or time."""
    return None, create_slot_node(flow_manager)


def _get_earliest_slot_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="get_earliest_slot",
        description="Find the earliest bookable appointment for the identified patient.",
        properties={
            "specialty": {"type": "string", "enum": specialty_ids()},
            "provider_name": {
                "type": "string",
                "description": "Doctor name as spoken; do not invent IDs.",
            },
            "allow_alternative": {
                "type": "boolean",
                "description": "Only true after explicit consent to another doctor at the same site/specialty.",
            },
            "site": {
                "type": "string",
                "enum": location_ids(),
                "description": "Only if the caller asked for a site.",
            },
            "weekday": {
                "type": "string",
                "enum": WEEKDAYS,
                "description": "Only if the caller asked for a weekday.",
            },
            "part_of_day": {
                "type": "string",
                "enum": ["morning", "afternoon"],
                "description": "Only if the caller asked for morning (before 2pm) or afternoon.",
            },
        },
        required=[],
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


def create_slot_node(flow_manager: FlowManager) -> NodeConfig:
    patient = flow_manager.state["patient"]
    return NodeConfig(
        name="find_slot",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"The patient is {patient['given_name']} {patient['first_surname']}. Find out "
                    "which specialty or named doctor they need. Preserve named doctor and site. Clarify ambiguous surnames and obtain consent before fallback. "
                    "When the caller states or confirms a specialty — for example 'the GP' — pass "
                    "specialty, not provider_name; pass provider_name only when no specialty is "
                    "known and the caller insists on one named doctor, and never pass both. "
                    "Only pass site, weekday or part_of_day if the caller asked for them. Then call "
                    "get_earliest_slot. If it returns no_slots, say nothing is available for that "
                    "request and ask whether they would drop a constraint. If lookup_failed, "
                    "apologise and ask them to call back shortly."
                ),
            }
        ],
        functions=[_get_earliest_slot_schema(), finish_without_booking],
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


async def finish_without_booking(flow_manager: FlowManager):
    """The caller declines alternatives or ends without an appointment."""
    submission = flow_manager.state["submission"]
    if submission.pending["action"] != "NO_ACTION":
        return {"status": "already_confirmed"}, None
    await submission.flush()
    return {"status": "no_booking"}, NodeConfig(
        name="no_booking",
        task_messages=[
            {
                "role": "developer",
                "content": "No appointment was booked. Explain the reason and say goodbye.",
            }
        ],
        post_actions=[{"type": "end_conversation"}],
    )
