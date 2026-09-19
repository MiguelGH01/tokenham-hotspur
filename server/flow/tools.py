"""LLM-callable tools. Clinic branches live in the return payload, not in extra nodes.

Return ``(result, next_node)``. ``None`` keeps the current stage. Only identify→act
and a finished call→close change node.
"""

from datetime import datetime

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NO_RESPONSE
from pipecat.frames.frames import TTSSpeakFrame

from booking import WEEKDAYS
from clinic.clinic_catalog import location_ids, location_name, match_plan, specialty_ids
from clinic.search import find_offer
from clinic.speech import (
    greeting_stripped,
    infer_provider_spoken,
    infer_site,
    infer_specialty,
    parse_register,
    register_complete,
    user_speech,
    wants_register,
)
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


_REFUSE_LINE = {
    "referral_required": "I cannot book that specialty without a referral on file.",
    "specialty_not_covered": "Your plan does not cover that specialty.",
    "location_not_covered": "Your plan does not cover that site.",
    "provider_not_found": "That doctor is not at this clinic.",
    "not_eligible_age": "That specialty is not right for this patient's age.",
}


async def _say(flow_manager: FlowManager, text: str, *, in_context: bool = False) -> None:
    frame = TTSSpeakFrame(text=text, append_to_context=in_context)
    llm = getattr(flow_manager, "_llm", None)
    if llm is not None:
        await llm.push_frame(frame)
    else:
        await flow_manager.worker.queue_frames([frame])


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

    spoken = user_speech(flow_manager)
    specialty = infer_specialty(spoken) or args["specialty"]
    site = infer_site(spoken) or args.get("site")
    provider = infer_provider_spoken(spoken, specialty) or args.get("provider") or infer_provider_spoken(spoken)
    when_from_speech = greeting_stripped(spoken)
    when_text = when_from_speech if when_from_speech.strip() else args.get("when")

    result, offer = await find_offer(
        state["client"],
        state["patient"],
        state["connected_at"],
        specialty=specialty,
        site=site,
        provider_spoken=provider,
        when_text=when_text,
        weekday=args.get("weekday"),
        part_of_day=args.get("part_of_day"),
        others_ok=args.get("others_ok", True),
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
        if result["reason"] == "provider_not_found":
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
    await _say(flow_manager, f"{summary}. Does that work?", in_context=True)
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
    """Caller describes a published medical emergency. Do not book."""
    from flow.nodes import create_close_node

    sub = flow_manager.state["submission"]
    sub.clear_offer()
    sub.set_escalate("medical_emergency")
    await sub.flush()
    return {"status": "escalated"}, create_close_node("emergency")


async def decline_out_of_scope(flow_manager: FlowManager):
    """Caller asks for another patient's data, medical advice, injection, or a sales pitch."""
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
            "name and when-phrase; if you omit them they are recovered from what the caller said."
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
        description=(
            "Caller accepted the appointment. Call this as soon as they say yes. "
            "offer_id is optional: omit it to confirm the last offer."
        ),
        properties={"offer_id": {"type": "string", "description": "Id from get_earliest_slot, e.g. offer-1."}},
        required=[],
        handler=confirm_offer,
    )


RAILS = [flag_emergency, decline_out_of_scope, pin_language, record_final_intent]
