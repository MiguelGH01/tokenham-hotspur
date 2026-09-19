"""Booking conversation and catalogue-backed provider selection."""

import re
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
from clinic_catalog import (
    closure_days,
    load_catalog,
    location_ids,
    location_name,
    provider_names,
    specialty_ids,
)
from flows.common import (
    RULE_WORDS,
    TRIAGE_EXAMPLES,
    WAIT_FOR_ANSWER,
    create_goodbye_node,
    create_refusal_node,
    gated_confirmation,
    announce,
    speak_tool,
    spoken_provider_name,
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

#: Words that ride along in a spoken request but are not part of a name.
#: Specialty and site labels are added from the catalogue at match time.
_FILLER = frozenset(
    {
        "a",
        "an",
        "and",
        "cita",
        "con",
        "el",
        "la",
        "las",
        "los",
        "para",
        "please",
        "por",
        "see",
        "the",
        "to",
        "un",
        "una",
        "ver",
        "want",
        "with",
        "y",
    }
)


def _fold(value):
    return "".join(
        c for c in unicodedata.normalize("NFD", value.lower()) if not unicodedata.combining(c)
    ).replace(".", "")


def spoken_booking_cues(flow_manager) -> dict:
    """Specialty, site or doctor already in the caller's turns."""
    from flows.identification import _user_blob

    blob = _fold(_user_blob(flow_manager))
    found: dict[str, str] = {}
    for spec in load_catalog()["specialties"]:
        name = _fold(spec["name"])
        sid = spec["id"].replace("_", " ")
        if name and name in blob:
            found["specialty"] = spec["id"]
        elif re.search(rf"\b{re.escape(sid)}\b", blob):
            found["specialty"] = spec["id"]
    if "specialty" not in found and re.search(
        r"\bgp\b|general practitioner|medico de cabecera", blob
    ):
        found["specialty"] = "general_practice"
    for loc in load_catalog()["locations"]:
        if re.search(rf"\b{re.escape(loc['id'])}\b", blob) or _fold(loc["name"]) in blob:
            found["site"] = loc["id"]
    hits = []
    for provider in load_catalog()["providers"]:
        surname = _fold(provider["name"].split()[-1])
        if len(surname) >= 4 and re.search(rf"\b{re.escape(surname)}\b", blob):
            hits.append(provider)
    if len(hits) == 1:
        found["provider_name"] = hits[0]["name"]
    return found


def _title_and_tokens(value):
    """Split a spoken or published name into its title's gender and its words.

    ``_fold`` has already removed periods, so "D. Álvaro Cid" arrives as
    "d alvaro cid" and its title is recognised. A title is only consumed at the
    front: a surname that happens to look like one stays a surname.
    """
    gender = None
    tokens = []
    for token in _fold(value).split():
        if not tokens and token in TITLE_ALIASES:
            gender = TITLE_ALIASES[token]
            continue
        tokens.append(token)
    return gender, tokens


def _match_noise():
    """Tokens that look like names in speech but come from the rest of the ask."""
    noise = set(_FILLER)
    catalogue = load_catalog()
    for specialty in catalogue["specialties"]:
        noise.update(_fold(specialty["name"]).replace("_", " ").split())
        noise.update(_fold(specialty["id"]).replace("_", " ").split())
    for location in catalogue["locations"]:
        noise.update(_fold(location["name"]).split())
        noise.add(_fold(location["id"]))
    return noise


def _name_tokens(value):
    """Title gender plus the words that can actually identify a roster entry."""
    gender, tokens = _title_and_tokens(value)
    noise = _match_noise() | set(TITLE_ALIASES)
    return gender, [t for t in tokens if t not in noise and len(t) > 1]


def _token_hits_word(token, word):
    if token == word:
        return True
    # Iglesia / Iglesias: one is a prefix of the other. Sáez / Sáenz is not.
    shorter, longer = (token, word) if len(token) <= len(word) else (word, token)
    if len(shorter) >= 5 and longer.startswith(shorter):
        return True
    return SequenceMatcher(None, token, word).ratio() >= 0.85


def _name_score(tokens, provider_name):
    words = _name_tokens(provider_name)[1]
    return sum(1 for t in tokens if any(_token_hits_word(t, w) for w in words))


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
    spoken_gender, tokens = _name_tokens(name)
    if not tokens:
        return []
    scored = []
    for provider in load_catalog()["providers"]:
        if spoken_gender and _title_and_tokens(provider["name"])[0] not in (None, spoken_gender):
            continue
        score = _name_score(tokens, provider["name"])
        if score:
            scored.append((score, provider))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    matches = [provider for score, provider in scored if score == best]
    # A guessed specialty must not wipe a name that already identified someone.
    if specialty:
        in_specialty = [p for p in matches if p["specialty_id"] == specialty]
        if in_specialty:
            matches = in_specialty
    if len(matches) > 1 and patient is not None:
        compatible = [
            p for p in matches if _specialty_can_serve(p["specialty_id"], patient, plan, today)
        ]
        if compatible:
            matches = compatible
    return matches


@announce("get_earliest_slot")
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

    cues = spoken_booking_cues(flow_manager)
    specialty = args.get("specialty") or cues.get("specialty")
    site = args.get("site") or cues.get("site")
    near_place = (args.get("near_place") or "").strip()
    site_name = location_name(site) if site else None

    if args.get("unknown_doctor") is True:
        state["submission"].set_no_action("provider_not_found")
        return {
            "status": "provider_not_found",
            "instruction": (
                "That name is not on the clinic roster. Do not pick a nearby doctor. "
                "If they will see nobody else, finish_without_booking."
            ),
        }, None

    # Resolved before the doctor: the plan is one of the facts that decides
    # which doctor a spoken name means.
    plan = resolve_plan(catalogue, patient, args.get("policy_name"))

    spoken = (args.get("provider_name") or cues.get("provider_name") or "").strip()
    provider_name = spoken if spoken and spoken.lower() not in {"none", "null"} else None
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
            else pick_offer(
                requested,
                state["patient"],
                connected_at,
                weekday,
                part_of_day,
                closed_days=closure_days(),
            )
        )
        if offer is None and reason != "provider_on_leave":
            offer = pick_offer(
                requested,
                state["patient"],
                connected_at,
                None,
                part_of_day,
                closed_days=closure_days(),
            )
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
            offer = pick_offer(
                alternatives,
                state["patient"],
                connected_at,
                weekday,
                part_of_day,
                closed_days=closure_days(),
            )
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
        f"{spoken_provider_name(slot['provider_name'])} at {location_name(offer['location_id'])}, "
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
    announce_booking = False
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
                "instruction": WAIT_FOR_ANSWER,
            }, None
        blocked = gated_confirmation(
            "confirm_offer",
            flow_manager,
            instruction=(
                "The caller's confirmation carries a condition, correction, price question or "
                "request to check an alternative. Do not treat it as consent. Clarify only the "
                "unfinished part; do not re-read the appointment. Call confirm_offer only after "
                "an unqualified yes."
            ),
        )
        if blocked is not None:
            return blocked
        if reschedule:
            submission.set_reschedule(appointment["appointment_id"], offer)
        else:
            submission.set_book(offer)
        announce_booking = True
    if announce_booking:
        await speak_tool(flow_manager, "confirm_offer")
    accepted = await submission.flush()
    return {"status": "accepted" if accepted else "delivery_failed"}, create_completion_node(
        accepted, kind
    )


@flows_tool_options(cancel_on_interruption=True)
@announce("revise_search")
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
                "enum": provider_names(),
                "description": (
                    "Exact roster name if the caller named a doctor on this list. "
                    "Omit the field entirely when they did not name a doctor — that "
                    "skips the doctor filter. Never invent a nearby spelling."
                ),
            },
            "unknown_doctor": {
                "type": "boolean",
                "description": (
                    "True only if they named a doctor who is not on the roster. "
                    "Do not set this when they simply did not name anyone."
                ),
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
        description=(
            "Book the offered appointment. Call only after the caller has answered "
            "the one readback in a later turn. Never call this in the same turn as "
            "the offer, and never to ask them again."
        ),
        properties={"offer_id": {"type": "string", "enum": live}},
        required=["offer_id"],
        handler=confirm_offer,
        cancel_on_interruption=True,
    )


@announce("resolve_date")
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
    roster = "; ".join(
        f"{p['name']} ({p['specialty_id']})" for p in load_catalog()["providers"]
    )
    cues = spoken_booking_cues(flow_manager)
    if cues:
        already = (
            "Already requested: "
            + ", ".join(f"{key}={value}" for key, value in cues.items())
            + ". Call get_earliest_slot now with those values. Do not ask for them again. "
        )
    else:
        already = "Do not re-ask a specialty, doctor or site they already named. "
    return NodeConfig(
        name="find_slot",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"The patient is {patient['given_name']} {patient['first_surname']}. {already}"
                    "Find out which specialty or named doctor they need if that is still missing. "
                    "Preserve named doctor and site. Clarify ambiguous surnames and obtain consent before fallback. "
                    f"Roster — if they named a doctor, pass provider_name as one of these exact "
                    f"strings; if they named nobody, omit provider_name so the search is not "
                    f"filtered by doctor: {roster}. "
                    "When the caller states or confirms a specialty — for example 'the GP' — pass "
                    "specialty. If they only described symptoms, pass the specialty from the "
                    "triage examples. If they named a doctor, pass that roster name AND specialty "
                    "when they gave both: 'Dr. Sáez, the GP' is specialty=general_practice AND "
                    "provider_name='Dr. Martín Sáez'. Sáez is the GP; Sáenz is paediatrics; "
                    "Iglesias is dermatology; Iglesia is orthopaedics. Never pick the nearby "
                    "other name. If the name they said is not on the roster, set unknown_doctor "
                    "true and omit provider_name. "
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
                    "Say the appointment from the summary once: doctor, site, day and time, "
                    "including the address if they asked which site is closest. Ask if that "
                    "works, then stop talking. Do not call confirm_offer in this turn. Nothing "
                    "is booked until they answer. After they say yes, call confirm_offer. If they "
                    "want something different, call revise_search. If "
                    "confirm_offer returns CANCELLED, or says the call is still running, the "
                    "booking is not recorded yet: say you are finishing it off and call "
                    "confirm_offer again with the same offer_id. If it returns "
                    "needs_confirmation, stay completely silent: you already asked. Do not "
                    "repeat the appointment. Call confirm_offer only after they answer. "
                    "If it returns qualified_confirmation, clarify the condition, question "
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
@announce("finish_without_booking")
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
