"""LLM-callable tools. Clinic branches live in the return payload, not in extra nodes.

Return ``(result, next_node)``. ``None`` keeps the current stage. Only identify→act
and a finished call→close change node.
"""

from datetime import datetime

from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NO_RESPONSE
from pipecat.flows.types import FlowsDirectFunctionWrapper
from pipecat.frames.frames import LLMSetToolsFrame

from booking import WEEKDAYS
from clinic.clinic_catalog import (
    chart_policy,
    location_ids,
    location_name,
    match_plan,
    plan_literals,
    specialty_ids,
)
from clinic.search import find_offer
from clinic.speech import (
    accepts_offer,
    parse_register,
    register_complete,
    resolve_slot_query,
    user_speech,
    wants_register,
    wants_soonest,
)
from flow.prompts import TRIAGE_EXAMPLES
from flow.speak import speak_as_llm
from national_id import is_valid_national_id, normalize_national_id

MAX_IDENTIFY_ATTEMPTS = 3
SPOKEN_TITLES = {"Dra.": "Doctor", "Dr.": "Doctor", "D.": "Don"}


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[-9:]


def _norm_email(value: str) -> str:
    return "".join(value.split()).lower()


def _spoken_name(provider_name: str) -> str:
    title, _, rest = provider_name.partition(" ")
    return f"{SPOKEN_TITLES[title]} {rest}" if title in SPOKEN_TITLES else provider_name


_HARD_REFUSE = frozenset(
    {
        "referral_required",
        "specialty_not_covered",
        "location_not_covered",
        "not_eligible_age",
    }
)
_REFUSE_LINE = {
    "referral_required": "I cannot book that specialty without a referral on file.",
    "specialty_not_covered": "Your plan does not cover that specialty.",
    "location_not_covered": "Your plan does not cover that site.",
    "provider_not_found": "That doctor is not at this clinic.",
    "not_eligible_age": "That specialty is not right for this patient's age.",
}


async def _say(flow_manager: FlowManager, text: str, *, in_context: bool = False) -> None:
    llm = getattr(flow_manager, "_llm", None)
    await speak_as_llm(llm if llm is not None else flow_manager.worker, text, in_context=in_context)


def _last_offer(state: dict) -> tuple[str, dict] | tuple[None, None]:
    offers = state.get("offers") or {}
    if not offers:
        return None, None
    offer_id = state.get("last_offer_id") or next(reversed(offers))
    offer = offers.get(offer_id)
    if offer is None:
        return None, None
    return offer_id, offer


async def flush_submission(action: dict, flow_manager: FlowManager) -> None:
    await flow_manager.state["submission"].flush()


async def speak_close_line(action: dict, flow_manager: FlowManager) -> None:
    await _say(flow_manager, action["text"], in_context=False)


async def search_patient(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_act_node

    state = flow_manager.state
    if state.get("hard_refuse"):
        return {"status": "refused", "reason": state["hard_refuse"]}, None
    if state.get("patient"):
        result, nxt = await get_earliest_slot({}, flow_manager)
        payload = {"status": "already_identified", "search": result}
        if nxt is not None and nxt is not NO_RESPONSE:
            return payload, nxt
        return payload, None
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
        await _say(flow_manager, "I'm having trouble with the directory. Could you repeat that?")
        return {"status": "lookup_failed"}, NO_RESPONSE

    if len(matches) != 1:
        spoken = user_speech(flow_manager)
        if wants_register(spoken):
            fields = parse_register(
                {"stated_name": stated_name, "given_name": "", "first_surname": "", "second_surname": ""},
                spoken,
            )
            # Names often arrive as one stated_name; split if the tool did not.
            if not fields["given_name"] and stated_name.strip():
                parts = stated_name.strip().split()
                if len(parts) >= 3:
                    fields["given_name"], fields["first_surname"], fields["second_surname"] = (
                        parts[0],
                        parts[1],
                        " ".join(parts[2:]),
                    )
            if register_complete(fields) and match_plan(fields["insurer"]):
                return await _commit_register(flow_manager, fields)
        return failed("not_found")

    patient = matches[0]
    state["patient"] = patient
    visited = "a returning patient" if patient["has_visited_before"] else "a first-time patient"
    summary = f"Found {patient['given_name']} {patient['first_surname']}, {visited}."
    # Same turn already named a specialty/doctor: search now so we do not burn another LLM round.
    if resolve_slot_query({}, user_speech(flow_manager))["specialty"]:
        result, nxt = await get_earliest_slot({}, flow_manager)
        if result.get("status") != "need_specialty":
            payload = {"status": "found", "patient_summary": summary, "search": result}
            if nxt is not None and nxt is not NO_RESPONSE:
                return payload, nxt
            return payload, create_act_node(flow_manager)
    return {"status": "found", "patient_summary": summary}, create_act_node(flow_manager)


async def _commit_register(flow_manager: FlowManager, fields: dict):
    from flow.nodes import create_close_node

    insurer = match_plan(fields["insurer"])
    if not insurer:
        return {"status": "unknown_plan", "hint": "map the spoken insurer to a catalogue plan"}, None
    payload = {
        "given_name": fields["given_name"].strip(),
        "first_surname": fields["first_surname"].strip(),
        "second_surname": fields["second_surname"].strip(),
        "national_id": normalize_national_id(fields["national_id"]),
        "date_of_birth": fields["date_of_birth"].strip(),
        "phone": _phone_digits(fields["phone"]),
        "email": _norm_email(fields["email"]),
        "insurer": insurer,
    }
    sub = flow_manager.state["submission"]
    sub.clear_offer()
    sub.set_register(payload)
    await sub.flush()
    await _say(flow_manager, "You're on the clinic records. Goodbye.")
    return {"status": "registered"}, create_close_node("registered")


async def register_patient(args: FlowArgs, flow_manager: FlowManager):
    if flow_manager.state.get("patient"):
        return {"status": "already_on_file"}, None
    fields = parse_register(args, user_speech(flow_manager))
    nid = fields["national_id"]
    if not is_valid_national_id(nid):
        await _say(flow_manager, "I did not catch the full document number. Could you repeat it slowly?")
        return {"status": "misheard_id"}, NO_RESPONSE
    if not register_complete(fields):
        await _say(flow_manager, "I still need both surnames, date of birth, phone, email, and insurer.")
        return {"status": "need_fields", "have": {k: bool(fields.get(k)) for k in fields}}, NO_RESPONSE
    return await _commit_register(flow_manager, fields)


async def get_earliest_slot(args: FlowArgs, flow_manager: FlowManager):
    """Search real availability. Stay on act; the model offers whatever this returns."""
    from flow.nodes import create_close_node

    state = flow_manager.state
    if not state.get("patient"):
        return {"status": "need_patient"}, None

    query = resolve_slot_query(args, user_speech(flow_manager))
    specialty = query["specialty"]
    if not specialty:
        await _say(flow_manager, "Which specialty did you need?")
        return {"status": "need_specialty", "specialties": specialty_ids()}, NO_RESPONSE

    result, offer = await find_offer(
        state["client"],
        state["patient"],
        state["connected_at"],
        specialty=specialty,
        site=query["site"],
        provider_spoken=query["provider"],
        when_text=query["when_text"],
        weekday=query["weekday"],
        part_of_day=query["part_of_day"],
        others_ok=query["others_ok"],
    )
    if result["status"] == "lookup_failed":
        logger.error("availability lookup failed: {}", result.get("error"))
        await _say(flow_manager, "I'm having trouble checking that. Could you repeat the request?")
        return {"status": "lookup_failed"}, NO_RESPONSE
    if result["status"] == "invalid":
        await _say(flow_manager, "Which specialty did you need?")
        return result, NO_RESPONSE
    if result["status"] == "provider_missing":
        state["submission"].set_no_action("provider_not_found")
        await _say(flow_manager, "That doctor is not at this clinic. Would you see someone else?")
        return result, NO_RESPONSE
    if result["status"] == "ambiguous_provider":
        names = " or ".join(result["candidates"])
        await _say(flow_manager, f"I have more than one. {names}. Which one?")
        return result, NO_RESPONSE
    if result["status"] == "refused":
        state["submission"].clear_offer()
        state["submission"].set_no_action(result["reason"])
        await _say(flow_manager, _REFUSE_LINE.get(result["reason"], "I cannot book that request."))
        # Policy dead-ends are the answer. Leave booking tools up and the model
        # books GP instead of gynae / derm (PR-06 record mismatch).
        if result["reason"] in _HARD_REFUSE or result["reason"] == "provider_not_found":
            state["hard_refuse"] = result["reason"]
            await state["submission"].flush()
            return result, create_close_node("refused")
        return result, NO_RESPONSE
    if offer is None:
        await _say(flow_manager, "Nothing is free in that window. Could we drop a day or site?")
        return result, NO_RESPONSE

    slot = result.pop("slot_meta")
    start = datetime.fromisoformat(offer["slot"])
    offer_id = f"offer-{len(state['offers']) + 1}"
    state["offers"][offer_id] = offer
    state["last_offer_id"] = offer_id
    state["submission"].set_offer(offer)
    summary = (
        f"{_spoken_name(slot['provider_name'])} at {location_name(offer['location_id'])}, "
        f"{start.strftime('%A %d %B')} at {start.strftime('%H:%M')}"
    )
    logger.info("Offer {}: {}", offer_id, offer)
    await refresh_act_tools(flow_manager)
    await _say(flow_manager, f"{summary}. Does that work?", in_context=True)
    spoken = user_speech(flow_manager)
    if wants_soonest(spoken) or accepts_offer(spoken):
        return await confirm_offer({"offer_id": offer_id}, flow_manager)
    return {
        "status": "offer",
        "offer_id": offer_id,
        "summary": summary,
        "specialty": result.get("specialty"),
        "blocked": result.get("blocked") or [],
    }, NO_RESPONSE


async def confirm_offer(args: FlowArgs, flow_manager: FlowManager):
    from flow.nodes import create_close_node

    state = flow_manager.state
    offer_id = args.get("offer_id")
    offer = state["offers"].get(offer_id) if offer_id else None
    if offer is None:
        offer_id, offer = _last_offer(state)
    if offer is None:
        return {"status": "expired"}, None
    sub = state["submission"]
    sub.set_book(offer)
    await sub.flush()
    return {"status": "confirmed", "offer_id": offer_id}, create_close_node("booked")


async def decline_other_providers(flow_manager: FlowManager):
    """Named doctor does not exist and the caller will not see anyone else."""
    from flow.nodes import create_close_node

    sub = flow_manager.state["submission"]
    sub.clear_offer()
    sub.set_no_action("provider_not_found")
    await sub.flush()
    return {"status": "refused", "reason": "provider_not_found"}, create_close_node("refused")


async def revise_search(flow_manager: FlowManager):
    """Caller wants a different specialty, site, day or time. Stay in act and search again."""
    flow_manager.state["submission"].clear_offer()
    return {"status": "revise"}, None


async def flag_emergency(flow_manager: FlowManager):
    """Escalate a published red-flag example (see role prompt). Do not book."""
    from flow.nodes import create_close_node

    sub = flow_manager.state["submission"]
    sub.clear_offer()
    sub.set_escalate("medical_emergency")
    await sub.flush()
    return {"status": "escalated"}, create_close_node("emergency")


async def decline_out_of_scope(flow_manager: FlowManager):
    """Decline a published out-of-scope example (see role prompt). Do not book."""
    from flow.nodes import create_close_node

    sub = flow_manager.state["submission"]
    sub.clear_offer()
    sub.set_no_action("out_of_scope")
    await sub.flush()
    return {"status": "declined"}, create_close_node("out_of_scope")


async def pin_language(flow_manager: FlowManager, language: str):
    """Caller is not in English or switched mid-call. language: es, ca, en, or similar."""
    flow_manager.state["language"] = language
    return {"status": "pinned", "language": language}, None


async def record_final_intent(flow_manager: FlowManager, summary: str):
    """Caller corrected themselves or changed their mind. summary: their latest ask."""
    flow_manager.state["final_intent"] = summary
    flow_manager.state["submission"].clear_offer()
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
            "insurer": {
                "type": "string",
                "enum": plan_literals(),
                "description": "Catalogue plan id or name, e.g. mapfre or Mapfre Salud.",
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


def get_earliest_slot_schema(flow_manager: FlowManager | None = None) -> FlowsFunctionSchema:
    policy_note = (
        "If they named a specialty or GP, pass that id. If they only described symptoms, "
        "pass the specialty from the triage examples. Omit site, doctor, weekday, and "
        "part of day unless they said them. Do not guess a site or doctor from the enum. "
        "Do not substitute another specialty when the chart refuses this one. "
        + TRIAGE_EXAMPLES
    )
    state = getattr(flow_manager, "state", None) or {}
    patient = state.get("patient")
    connected_at = state.get("connected_at")
    if patient and connected_at is not None:
        chart = chart_policy(patient, connected_at)
        bits = [f"plan {chart['plan'] or 'unknown'}", f"general complaint books {chart['general_specialty']}"]
        if chart["uncovered_specialties"]:
            bits.append("uncovered: " + ", ".join(chart["uncovered_specialties"]))
        if chart["missing_referrals"]:
            bits.append("referral missing: " + ", ".join(chart["missing_referrals"]))
        policy_note += " Chart: " + "; ".join(bits) + "."
    return FlowsFunctionSchema(
        name="get_earliest_slot",
        description="Find a real bookable slot. " + policy_note,
        properties={
            "specialty": {
                "type": "string",
                "enum": specialty_ids(),
                "description": "Catalogue specialty id they named, or the triage example mapping.",
            },
            "site": {
                "type": "string",
                "enum": location_ids(),
                "description": "Only if they named Arenal Centro, Norte, or Sur.",
            },
            "provider": {"type": "string", "description": "Spoken doctor name if they named one. Omit otherwise."},
            "when": {
                "type": "string",
                "description": "Spoken when, e.g. tomorrow, this coming Thursday, Saturday morning.",
            },
            "weekday": {
                "type": "string",
                "enum": WEEKDAYS,
                "description": "Only if they named that weekday.",
            },
            "part_of_day": {
                "type": "string",
                "enum": ["morning", "afternoon"],
                "description": "Only if they asked for morning (before 2pm) or afternoon.",
            },
            "others_ok": {
                "type": "boolean",
                "description": "False when they already said they will not see any other doctor.",
            },
        },
        required=[],
        handler=get_earliest_slot,
    )


def confirm_offer_schema(flow_manager: FlowManager | None = None) -> FlowsFunctionSchema:
    offers = list((getattr(flow_manager, "state", {}) or {}).get("offers") or {})
    offer_field: dict = {
        "type": "string",
        "description": "Id returned by get_earliest_slot. Omit to confirm the last offer.",
    }
    if offers:
        offer_field["enum"] = offers
    return FlowsFunctionSchema(
        name="confirm_offer",
        description=(
            "Caller accepted the appointment. Call this as soon as they say yes. "
            "offer_id must be one the availability search actually returned."
        ),
        properties={"offer_id": offer_field},
        required=[],
        handler=confirm_offer,
    )


def act_functions(flow_manager: FlowManager):
    state = getattr(flow_manager, "state", None) or {}
    if state.get("hard_refuse"):
        return []
    tools = [get_earliest_slot_schema(flow_manager)]
    if state.get("offers"):
        tools.append(confirm_offer_schema(flow_manager))
    tools.extend([revise_search, decline_other_providers])
    return tools


async def refresh_act_tools(flow_manager: FlowManager) -> None:
    """Point confirm_offer at live offer ids from /availability without changing node."""
    worker = getattr(flow_manager, "_worker", None)
    create = getattr(flow_manager, "_create_function_schema", None)
    if worker is None or create is None:
        return
    standard = []
    for func in list(getattr(flow_manager, "_global_functions", []) or []) + act_functions(flow_manager):
        tool = FlowsDirectFunctionWrapper(function=func) if callable(func) else func
        standard.append(await create(tool))
    await worker.queue_frames([LLMSetToolsFrame(tools=ToolsSchema(standard_tools=standard))])


RAILS = [
    search_patient_schema(),
    register_patient_schema(),
    flag_emergency,
    decline_out_of_scope,
    pin_language,
    record_final_intent,
]
