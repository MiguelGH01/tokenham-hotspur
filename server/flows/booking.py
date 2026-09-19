"""Booking conversation and catalogue-backed provider selection."""

import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher

from loguru import logger
from pipecat.flows import (
    FlowArgs,
    FlowManager,
    FlowsFunctionSchema,
    NodeConfig,
    flows_tool_options,
)

import audit
import dates
from booking import MADRID, WEEKDAYS, pick_offer, search_window
from clinic_catalog import load_catalog, location_ids, location_name, specialty_ids
from flows.common import (
    RULE_WORDS,
    TRIAGE_EXAMPLES,
    create_goodbye_node,
    create_refusal_node,
    gated_confirmation,
)
from rules import (
    check_patient_rules,
    check_provider_rules,
    location_from_spoken_place,
    provider_speaks,
    resolve_plan,
)
from submission import book_action, reschedule_action

#: The titles a caller and the roster actually use, and the gender each one
#: implies. The spelled-out "doctor" is consumed as a title but leaves the
#: gender open, because in speech it is used for both — only "Dr.", "Dra." and
#: "D." say which.
TITLE_ALIASES = {
    "dr": "m",
    "dra": "f",
    "d": "m",
    "doctor": None,
    "doctora": "f",
}


def _fold(value):
    return "".join(
        c for c in unicodedata.normalize("NFD", value.lower()) if not unicodedata.combining(c)
    ).replace(".", "")


def _title_and_tokens(value):
    """Split a spoken or published name into its title's gender and its words.

    ``_fold`` has already removed periods, so "D. Álvaro Cid" arrives as
    "d alvaro cid" and its title is recognised. A title is only consumed at the
    front: a surname that happens to look like one stays a surname.
    """
    gender = None
    seen_title = False
    tokens = []
    for token in _fold(value).split():
        if not seen_title and token in TITLE_ALIASES:
            seen_title = True
            gender = TITLE_ALIASES[token]
            continue
        tokens.append(token)
    return gender, tokens


def _specialty_can_serve(specialty_id, patient, plan, today):
    """Whether this specialty could take this patient at all.

    Used only to choose between two doctors a spoken name matches, never to
    refuse anything: ``/availability`` stays the authority on who can serve
    what. An adult cannot be a paediatric patient, and that alone settles the
    Sáez/Sáenz pair without asking the caller.

    ``today`` is the call's connect date, so the age boundary is measured
    against the clinic's clock. A caller that has no connect moment gets the
    current Madrid date, which is the same thing in practice.
    """
    verdict = check_patient_rules(
        specialty_id=specialty_id,
        patient=patient,
        plan=plan,
        today=today or datetime.now(MADRID).date(),
    )
    return verdict is None


def resolve_provider(name, specialty=None, *, patient=None, plan=None, today=None):
    """The roster entries a spoken name could mean.

    Two discriminators, in order of what they are worth:

    - **The title the caller said.** Every provider is published as "Dr." or
      "Dra.", so a spoken title halves the candidates for free. Applied only
      when the caller said one: a neutral "Doctor Iglesias" still matches both.
    - **Whether the specialty could take this patient** — age, the referral the
      record holds, the plan's coverage. This settles Sáez/Sáenz for an adult
      without a clarifying question. It deliberately does **not** settle
      Iglesias/Iglesia, where neither specialty is ruled out for a dermatology
      patient, so that question stays and the caller answers it.

    Returns every remaining candidate; the caller decides whether one is an
    answer or two are a question.
    """
    spoken_gender, tokens = _title_and_tokens(name)
    matches = []
    for provider in load_catalog()["providers"]:
        if specialty and provider["specialty_id"] != specialty:
            continue
        if spoken_gender and _title_and_tokens(provider["name"])[0] not in (None, spoken_gender):
            continue
        words = _fold(provider["name"]).split()
        if tokens and all(
            any(SequenceMatcher(None, t, w).ratio() >= 0.85 for w in words) for t in tokens
        ):
            matches.append(provider)
    if len(matches) > 1 and patient is not None:
        compatible = [
            p for p in matches if _specialty_can_serve(p["specialty_id"], patient, plan, today)
        ]
        if compatible:
            matches = compatible
    return matches


async def get_earliest_slot(args: FlowArgs, flow_manager: FlowManager):
    """Find the earliest bookable slot, applying the rules the API cannot.

    ``/availability`` names the restriction in ``blocked`` when it refuses a
    provider, and that name is the reason we submit. But it answers only about
    providers, and only once a request is concrete enough to ask about. Two
    decisions have to be made before that round trip, from the catalogue:

    - **Which specialty the patient belongs in.** A caller who asks for "the GP"
      for a five-year-old must be booked into paediatrics; asking the API about
      general practice would return the age refusal, and submitting that refusal
      would fail a case whose answer is a booking.
    - **Whether a named doctor has anywhere to go.** A doctor who refuses the
      plan, or is on leave, is a redirect when a colleague in the same specialty
      can serve and a refusal only when nobody can.
    """
    state = flow_manager.state
    from flows.requests import prepare_proposal, revise_request

    if state["submission"].delivery_attempted:
        return {
            "status": "delivery_pending",
            "instruction": (
                "This request is already with the clinic and cannot be searched again. "
                "Retry confirm_offer with the same offer_id, or route a new request."
            ),
        }, None
    revise_request(flow_manager)
    revision = state["revision"]
    patient = state["patient"]
    today = state["connected_at"].astimezone(MADRID).date()
    catalogue = load_catalog()

    specialty, site = args.get("specialty"), args.get("site")
    near_place = (args.get("near_place") or "").strip()
    site_name = location_name(site) if site else None

    # Resolved before the doctor: the plan is one of the facts that decides
    # which doctor a spoken name means.
    plan = resolve_plan(catalogue, patient, args.get("policy_name"))

    provider_name = args.get("provider_name")
    provider_id = None
    if provider_name:
        providers = resolve_provider(
            provider_name, specialty, patient=patient, plan=plan, today=today
        )
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

    # 1. Age first: the widest rule, and the only one that changes what we search.
    age_verdict = check_patient_rules(
        specialty_id=specialty, location_name=site_name, patient=patient, plan=None, today=today
    )
    note = None
    if age_verdict and age_verdict.reason == "not_eligible_age":
        if not age_verdict.redirect_specialty:
            state["submission"].set_no_action("not_eligible_age")
            return {"status": "blocked", "reason": "not_eligible_age"}, create_refusal_node(
                "not_eligible_age"
            )
        specialty, note = age_verdict.redirect_specialty, "redirected_by_age"

    if not site and near_place:
        nearest = location_from_spoken_place(near_place, specialty)
        if nearest is not None:
            site = nearest["id"]
            site_name = nearest["name"]

    # 2. A named doctor the plan or the roster rules out is a redirect, not a refusal.
    if provider_id:
        verdict = check_provider_rules(
            provider_id=provider_id,
            specialty_id=specialty,
            location_name=site_name,
            plan=plan,
            today=today,
        )
        if verdict:
            if not verdict.redirect_to:
                state["submission"].set_no_action(verdict.reason)
                return {"status": "blocked", "reason": verdict.reason}, create_refusal_node(
                    verdict.reason
                )
            provider_id, note = None, verdict.reason

    # 3. The rules that bite before a doctor is chosen.
    rules_verdict = check_patient_rules(
        specialty_id=specialty, location_name=site_name, patient=patient, plan=plan, today=today
    )
    if rules_verdict and not rules_verdict.redirect_to and not rules_verdict.redirect_specialty:
        state["submission"].set_no_action(rules_verdict.reason)
        return {"status": "blocked", "reason": rules_verdict.reason}, create_refusal_node(
            rules_verdict.reason
        )

    connected_at: datetime = state["connected_at"]
    target = _target_day(args, connected_at)
    if target is None and weekday and not dates.weekday_ever_open(weekday, site, today):
        # The caller named a day the clinic never opens ("this coming Sunday").
        # That day is impossible rather than full, so the ask rolls to the next
        # open day that still matches the site, the specialty and the part of
        # day. A day that is merely booked out is answered, not rolled.
        target = dates.next_open_day(dates.next_weekday(today, weekday), site)
    if target is not None:
        # A named day is searched from itself, so "first thing on Monday the
        # twelfth of October" can still be answered when that Monday is a
        # closure. Rolling forward drops the weekday on purpose: the caller asked
        # for the earliest morning after the holiday, not the next Monday.
        target = dates.next_open_day(target, site)
        date_from, date_to = dates.window_around(target)
        weekday = None
    else:
        date_from, date_to = search_window(connected_at)
    try:
        availability = await state["client"].availability(
            date_from,
            date_to,
            specialty,
            patient["patient_id"],
            location_id=site,
            insurer=[plan["id"]] if plan else None,
        )
    except Exception as exc:
        # Same distinction as the directory lookup: the clinic's API being down is
        # not the same as "nothing is free". Told "no slots" the agent would name
        # a rule it never read; told nothing it stalls. Name the fault and ask
        # again.
        logger.error("availability lookup failed: {}", type(exc).__name__)
        return {
            "status": "lookup_failed",
            "instruction": (
                "The clinic's diary could not be reached. This is a fault on our side, "
                "not an answer about availability: do not say that nothing is free and "
                "do not offer a rule. Apologise for the delay in one short sentence and "
                "call get_earliest_slot again with exactly the same request."
            ),
        }, None

    restrictions = [
        b.get("restriction")
        for b in availability.get("blocked", [])
        if not provider_id or b.get("provider_id") == provider_id
    ]
    # The API names the rule it applied; the catalogue explains the ones it does
    # not carry. Never default to no_availability while a rule is known to bite.
    reason = next(
        (r for r in restrictions if isinstance(r, str)),
        (rules_verdict.reason if rules_verdict else None) or "no_availability",
    )
    language = state.get("language")
    availability = {
        **availability,
        "slots": [
            s
            for s in availability["slots"]
            if (not site or s["location_id"] == site)
            and s.get("specialty_id", specialty) == specialty
            and provider_speaks(catalogue, s["provider_id"], language)
        ],
    }
    if state.get("intent") == "reschedule":
        # A moved appointment stays the appointment it is: the type already on
        # its diary, never the type the patient's history would pick for a new
        # one. The reschedule route carries no type for the same reason, so a
        # slot of another type would be submitted as this appointment.
        appointment_type = (state.get("appointment") or {}).get("appointment_type_id")
        if appointment_type:
            availability = {
                **availability,
                "slots": [
                    s
                    for s in availability["slots"]
                    if s.get("appointment_type_id") == appointment_type
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
            result = {
                "status": "provider_unavailable",
                "blocked": availability.get("blocked", []),
                "instruction": "Ask consent for another doctor at the SAME site and specialty. Never silently relax constraints.",
            }
            if words := RULE_WORDS.get(reason):
                result["reason_words"] = words
            return result, None
        if offer is None:
            alternatives = {
                **availability,
                "slots": [s for s in availability["slots"] if s["provider_id"] != provider_id],
            }
            offer = pick_offer(alternatives, state["patient"], connected_at, weekday, part_of_day)
    else:
        offer = pick_offer(availability, state["patient"], connected_at, weekday, part_of_day)
        if offer is None and (weekday or part_of_day):
            # PR-07: the asked window is empty. Offer the earliest slot that
            # still respects site and specialty, never a different ask.
            relaxed = pick_offer(availability, state["patient"], connected_at, None, part_of_day)
            if relaxed is None:
                relaxed = pick_offer(
                    availability, state["patient"], connected_at, weekday, None
                )
            if relaxed is None:
                relaxed = pick_offer(availability, state["patient"], connected_at, None, None)
            if relaxed is not None:
                offer, note = relaxed, "negotiated"
    if offer is None:
        state["submission"].set_no_action(reason)
        result = {"status": "no_slots", "blocked": availability.get("blocked", [])}
        if words := RULE_WORDS.get(reason):
            # The rule is already decided; this is only so the agent can say it
            # in plain words instead of admitting it does not know why.
            result["reason_words"] = words
        return result, None
    if state.get("revision") != revision or state.get("patient") != patient:
        return {"status": "expired"}, None
    slot = next(
        s
        for s in availability["slots"]
        if s["provider_id"] == offer["provider_id"]
        and datetime.fromisoformat(s["start_time"]).astimezone(MADRID).isoformat() == offer["slot"]
    )
    start = datetime.fromisoformat(offer["slot"])
    # One offer, one handle, numbered per call: the model refers to what it read
    # out loud, and two searches in one conversation must not answer to the same
    # name. Whether a handle still counts is the proposal's business, not the
    # number's.
    state["offer_seq"] = state.get("offer_seq", 0) + 1
    offer_id = f"offer-{state['offer_seq']}"
    state["offers"][offer_id] = offer
    prepare_proposal(flow_manager, offer_id)
    audit.audit(
        state.get("call_id", "unknown"),
        "offer_prepared",
        offer_id=offer_id,
        provider_id=offer["provider_id"],
        location_id=offer["location_id"],
        appointment_type_id=offer["appointment_type_id"],
        slot=offer["slot"],
        policy_id=offer["policy_id"],
    )
    summary = (
        f"{slot['provider_name']} at {location_name(offer['location_id'])}, "
        f"{start.strftime('%A %d %B')} at {start.strftime('%H:%M')}"
    )
    logger.info("Offer prepared")
    result = {"status": "offer", "offer_id": offer_id, "summary": summary}
    if note:
        # Tells the model why the offer may not be what the caller asked for, so
        # it explains instead of presenting the redirect as the original answer.
        result["note"] = note
        result["specialty"] = specialty
        if note == "negotiated":
            result["instruction"] = (
                "The window they asked for was empty. Offer this as the closest that "
                "fits; do not present it as the original time."
            )
    return result, create_confirm_node(flow_manager)


async def confirm_offer(args: FlowArgs, flow_manager: FlowManager):
    from flows.common import create_completion_node, record_already_settled
    from flows.requests import proposal_status

    state = flow_manager.state
    offer = state["offers"].get(args["offer_id"])
    if offer is None:
        return {"status": "expired"}, None
    submission = state["submission"]
    reschedule = state.get("intent") == "reschedule"
    appointment = state.get("appointment") or {}
    if reschedule and (
        not appointment or appointment["patient_id"] != state["patient"]["patient_id"]
    ):
        return {"status": "invalid_appointment"}, None
    kind = "reschedule" if reschedule else "appointment"
    wanted = (
        reschedule_action(appointment["appointment_id"], offer) if reschedule else book_action(offer)
    )
    if submission.delivery_attempted:
        # A retry of the action the frozen plan already carries is safe — the
        # first POST may have landed. Anything else cannot become this call's
        # record, and saying it did would be a wrong record and a lie at once.
        if not submission.carries(wanted):
            return record_already_settled(kind)
    else:
        status = proposal_status(flow_manager, args["offer_id"])
        if status == "stale":
            # The request moved on after this was read out: it is no longer an
            # offer, so the answer is a fresh search, not a re-readback.
            return {
                "status": "expired",
                "instruction": "That appointment is no longer on offer. Call revise_search.",
            }, None
        if status != "ok":
            return {
                "status": "needs_confirmation",
                "instruction": (
                    "Nothing is booked yet: the caller has to answer the readback out "
                    "loud, in a turn of their own. Ask them and call confirm_offer again."
                ),
            }, None
        blocked = gated_confirmation(
            "confirm_offer",
            flow_manager,
            instruction=(
                "The caller's confirmation carries a condition, correction, price question or "
                "request to check an alternative. Do not treat it as consent. Clarify the "
                "unfinished part first; only call confirm_offer again with an unqualified "
                "confirmation."
            ),
        )
        if blocked is not None:
            return blocked
        if reschedule:
            submission.set_reschedule(appointment["appointment_id"], offer)
        else:
            submission.set_book(offer)
    accepted = await submission.flush()
    return {"status": "accepted" if accepted else "delivery_failed"}, create_completion_node(
        accepted, kind
    )


@flows_tool_options(cancel_on_interruption=True)
async def revise_search(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller wants a different specialty, site, day or time."""
    from flows.requests import revise_request

    revise_request(flow_manager)
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
                "description": "Only if the caller named a clinic site. Never guess from the enum.",
            },
            "near_place": {
                "type": "string",
                "description": (
                    "Street, neighbourhood or town the caller said they are at when they "
                    "want the closest site. Pass their words; never a site id."
                ),
            },
            "policy_name": {
                "type": "string",
                "description": (
                    "Only when the caller names a second plan their record may not hold "
                    "(PR-17). Pass the plan as spoken; never guess one to unlock a booking."
                ),
            },
            "weekday": {
                "type": "string",
                "enum": WEEKDAYS,
                "description": "Only if the caller asked for a weekday without a date.",
            },
            "relative_day": {
                "type": "string",
                "enum": list(dates.RELATIVE_DAYS),
                "description": (
                    "Only when the caller said 'tomorrow', 'the day after tomorrow', "
                    "'a week from today' or 'in a fortnight'. Never compute the date "
                    "yourself and never pass both this and date_from."
                ),
            },
            "date_from": {
                "type": "string",
                "description": (
                    "Only when the caller named a calendar date, as ISO YYYY-MM-DD. "
                    "Never compute it yourself: pass named_date instead and use what "
                    "resolve_date returned."
                ),
            },
            "part_of_day": {
                "type": "string",
                "enum": ["morning", "afternoon"],
                "description": "Only if the caller asked for morning (before 2pm) or afternoon.",
            },
        },
        required=[],
        handler=get_earliest_slot,
        cancel_on_interruption=True,
    )


def _confirm_offer_schema(flow_manager: FlowManager) -> FlowsFunctionSchema:
    from flows.requests import live_keys

    # Only the handle the current request stands behind: an offer from before a
    # revision is still in the table for the record, but it is not a choice.
    live = [key for key in flow_manager.state["offers"] if key in live_keys(flow_manager)]
    return FlowsFunctionSchema(
        name="confirm_offer",
        description="The caller accepted the offered appointment.",
        properties={"offer_id": {"type": "string", "enum": live}},
        required=["offer_id"],
        handler=confirm_offer,
        cancel_on_interruption=True,
    )


async def resolve_date(args: FlowArgs, flow_manager: FlowManager):
    """Turn a named calendar date into an ISO day, in code.

    The model is good at hearing "Monday the twelfth of October" and bad at
    knowing it is 2026-10-12, which is exactly the split this tool exists for.
    """
    call_day = flow_manager.state["connected_at"].astimezone(MADRID).date()
    try:
        day = dates.resolve_named(
            args.get("weekday"),
            int(args["day"]),
            args["month"],
            args.get("year"),
            call_day,
        )
    except (KeyError, ValueError):
        return {"status": "unresolved", "instruction": "Ask the caller to repeat the date."}, None
    return (
        {
            "status": "resolved",
            "date": day.isoformat(),
            "weekday": dates.WEEKDAYS[day.weekday()],
            "instruction": "Pass this as date_from to get_earliest_slot. Do not recompute it.",
        },
        None,
    )


def _resolve_date_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="resolve_date",
        description="Resolve a calendar date the caller named out loud into an ISO day.",
        properties={
            "weekday": {"type": "string", "enum": WEEKDAYS},
            "day": {"type": "integer", "description": "Day of the month the caller said."},
            "month": {"type": "string", "enum": dates.MONTHS},
            "year": {"type": "integer", "description": "Only if the caller named a year."},
        },
        required=["day", "month"],
        handler=resolve_date,
        cancel_on_interruption=True,
    )


def _target_day(args: FlowArgs, connected_at: datetime) -> date | None:
    """The calendar day the caller named, or ``None`` for an open-ended search."""
    if args.get("relative_day"):
        return dates.resolve_relative(args["relative_day"], connected_at.astimezone(MADRID).date())
    if args.get("date_from"):
        return date.fromisoformat(str(args["date_from"])[:10])
    return None


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
                    "specialty. If they only described symptoms, pass the specialty from the "
                    "triage examples. Pass provider_name whenever the caller names a doctor, and pass "
                    "BOTH when they give both: 'Dr. Sáez, the GP' is specialty=general_practice "
                    "AND provider_name='Sáez'. That pair is what identifies a doctor whose "
                    "surname is ambiguous, so never drop one of the two. "
                    "A named GP, site or doctor the caller said wins over an invented enum value. "
                    "Do not guess a site or doctor from the enum. "
                    "Only pass site, weekday or part_of_day if the caller asked for them. If they "
                    "gave a street or neighbourhood instead of a site name, pass near_place as "
                    "they said it and do not guess a site. Never "
                    "pass policy_name unless the caller named a plan out loud. If coverage blocks, "
                    "ask whether they hold another plan and only then retry with policy_name. "
                    "Then call "
                    "get_earliest_slot. If it returns no_slots, say nothing is available for that "
                    "request; when reason_words is set, say that rule in plain words as well, and "
                    "only ask whether they would drop a constraint when no rule applies. If the "
                    "offer note is negotiated, say this is the closest that fits, not the window "
                    "they first asked for. If it "
                    "returns "
                    "redirected_by_age, explain plainly that this patient belongs with the other "
                    "team and offer what came back — never call it an error. If blocked, say the "
                    "rule in plain words and do not offer an appointment. If lookup_failed, "
                    "apologise and ask them to call back shortly. If a tool result is "
                    "CANCELLED or says the call is still running, you have no appointment to "
                    "describe: say you are just checking and call get_earliest_slot again. "
                    "Never describe a day, a doctor or a site from memory. "
                    + TRIAGE_EXAMPLES
                ),
            }
        ],
        functions=[_get_earliest_slot_schema(), _resolve_date_schema(), finish_without_booking],
    )


def create_confirm_node(flow_manager: FlowManager) -> NodeConfig:
    return NodeConfig(
        name="confirm",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Offer the appointment from the summary the tool returned: doctor, site, day "
                    "and time, including the address if they asked which site is closest. "
                    "Ask if that works. Nothing is booked until they say yes. On yes, "
                    "call confirm_offer. If they want something different, call revise_search. If "
                    "confirm_offer returns CANCELLED, or says the call is still running, the "
                    "booking is not recorded yet: say you are finishing it off and call "
                    "confirm_offer again with the same offer_id. If it returns "
                    "needs_confirmation, the caller has not answered you in a turn of their own "
                    "yet: ask them to confirm out loud and call confirm_offer only once they "
                    "have. If it returns qualified_confirmation, clarify the condition, question "
                    "or correction first. If it returns expired, that offer is no longer on the "
                    "table: call revise_search and offer what it returns. If it returns "
                    "delivery_conflict, the call's record is already settled: do not claim the "
                    "appointment, apologise and say goodbye."
                ),
            }
        ],
        functions=[_confirm_offer_schema(flow_manager), revise_search],
    )


@flows_tool_options(cancel_on_interruption=True)
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
