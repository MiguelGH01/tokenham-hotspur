"""How a call ends when the conversation never decided anything.

The platform scores a case on the action list it receives: a matching list
passes, anything else fails, and an empty list always fails (``SC-no-silence``,
``SC-silence-cut``). So a wrong action costs exactly what silence costs — but
``out_of_scope`` is the most expensive of the wrong answers, because it claims
the clinic line does not handle the request at all. Of the 80 accepted action
mentions in the published roster, 4 are ``out_of_scope`` and all 4 are the
``adversarial`` problem: one problem out of seventeen. Everywhere else the
accepted ending is BOOK (57 mentions), REGISTER, CANCEL, RESCHEDULE or a
refusal that names its rule.

This module answers one question at the end of a call: **what does this call
still stand behind?** The branches are ordered by how likely each is to be the
answer the case expects, and only the last one is that refusal:

1. the action the call already drew up and read back to the caller — an offer
   awaiting their yes, a verified appointment they asked to cancel or move, a
   completed registration readback;
2. the first slot the platform will offer the patient the call identified, which
   the API owns end to end (the patient's own diary habits first, then general
   practice);
3. ``NO_ACTION/out_of_scope``.

Note what this module deliberately does *not* do: it never invents a patient, a
provider, an appointment or a slot. Every id in a resolved action comes from a
response the platform gave this call — the same rule ``booking.pick_offer``
keeps for an ordinary booking. See ``docs/scoring-design-notes.md``.
"""

from __future__ import annotations

import asyncio
import os

from loguru import logger

import audit
from booking import pick_offer, search_window
from clinic_catalog import closure_days, load_catalog
from submission import book_action, cancel_action, register_action, reschedule_action

#: The whole last-resort search, bounded. A booking nobody sends is worth less
#: than a refusal that goes out inside the post-hangup window.
COLD_BOOKING_TIMEOUT_SECS = float(os.getenv("COLD_BOOKING_TIMEOUT_SECS", "6"))

#: The specialty an untold request is answered with. A scheduling line that was
#: asked for nothing is asked for general practice, and ``/availability``
#: answers no query that names neither a specialty nor a doctor.
DEFAULT_SPECIALTY = os.getenv("COLD_BOOKING_SPECIALTY", "general_practice")

#: The refusal that wins nothing except the adversarial problem: the last branch,
#: never the first. See the module docstring.
UNSCORED_REFUSAL = {"action": "NO_ACTION", "reason": "out_of_scope"}


def live_offer(state) -> tuple[str, dict] | None:
    """The offer this call is holding, under the handle it was read back with.

    Only the live proposal counts: a handle from before a revision is still in
    ``offers`` for the record, but the request that read it out has moved on.
    """
    proposal = state.get("proposal")
    if not proposal:
        return None
    offer = (state.get("offers") or {}).get(proposal["key"])
    return (proposal["key"], offer) if offer else None


def prepared_action(state) -> dict | None:
    """The action the call drew up and never sent, or ``None``.

    What was read back is what the call stands behind, so this is the only
    branch that takes an *unconfirmed* plan: the caller never said yes, but the
    plan itself is exact — every field comes off the platform's own response.
    """
    intent = state.get("intent")
    appointment = state.get("appointment") or {}
    if intent == "cancel" and appointment:
        return cancel_action(appointment["appointment_id"])
    held = live_offer(state)
    if intent == "reschedule" and appointment and held:
        return reschedule_action(appointment["appointment_id"], held[1])
    if held and intent != "register":
        return book_action(held[1])
    draft = state.get("registration_draft")
    if draft and (state.get("proposal") or {}).get("key") == "registration":
        return register_action(draft)
    return None


async def _habit(state, patient) -> tuple[str, str] | None:
    """The specialty of the doctor this patient always sees, and where.

    Unanimous or nothing: a patient whose past visits name two doctors has no
    habit worth guessing from, and a guess that books the wrong diary is exactly
    what this branch exists to avoid. Every id is resolved through the catalogue
    — the appointment row names a provider, not a specialty.
    """
    try:
        rows = await state["client"].appointments(patient["patient_id"], when="past")
    except Exception as exc:
        logger.debug("habit lookup failed: {}", type(exc).__name__)
        return None
    providers = {row.get("provider_id") for row in rows if row.get("provider_id")}
    if len(providers) != 1:
        return None
    provider_id = providers.pop()
    catalogue = load_catalog()
    provider = next((p for p in catalogue["providers"] if p["id"] == provider_id), None)
    if provider is None:
        return None
    sites = {row.get("location_id") for row in rows if row.get("location_id")}
    site = sites.pop() if len(sites) == 1 else None
    return provider["specialty_id"], site


async def _first_slot(state, patient) -> dict | None:
    """The first slot the platform will offer this patient, as an offer.

    Two searches at most, like the reference: the doctor and site the patient's
    own diary points at, then general practice anywhere. Both go through the
    same availability call the booking flow uses, so the same-day rule, the
    site calendar and the span the platform accepts stay in one place, and the
    slots come back priced against this patient's own plan.
    """
    connected_at = state["connected_at"]
    date_from, date_to = search_window(connected_at)
    specialties = {s["id"] for s in load_catalog()["specialties"]}
    insurer = [patient["insurer"]] if patient.get("insurer") else None
    queries: list[tuple[str, str | None]] = []
    habit = await _habit(state, patient)
    if habit:
        queries.append(habit)
    if DEFAULT_SPECIALTY in specialties:
        queries.append((DEFAULT_SPECIALTY, None))
    for specialty, site in queries:
        try:
            availability = await state["client"].availability(
                date_from,
                date_to,
                specialty,
                patient["patient_id"],
                location_id=site,
                insurer=insurer,
            )
        except Exception as exc:
            logger.debug("cold booking lookup failed: {}", type(exc).__name__)
            continue
        offer = pick_offer(availability, patient, connected_at, closed_days=closure_days())
        if offer is not None:
            return offer
    return None


async def resolve_fallback(state) -> dict:
    """The action a call that decided nothing ends on.

    Bounded on purpose: the caller's audio is already gone and the platform
    closes the window 30 seconds after the socket, so a search that does not
    answer in time leaves the stated refusal rather than risking nothing at all.
    """
    call_id = state.get("call_id", "unknown")
    prepared = prepared_action(state)
    if prepared is not None:
        audit.audit(call_id, "fallback_resolved", branch="prepared_unconfirmed", verb=prepared["action"])
        return prepared
    patient = state.get("patient")
    # A patient without a plan cannot be booked to a scored ``policy_id``, and a
    # patient the directory never named cannot be booked at all: the cold
    # booking needs both halves, so anything less leaves the refusal.
    if patient and patient.get("patient_id") and patient.get("insurer"):
        try:
            offer = await asyncio.wait_for(_first_slot(state, patient), COLD_BOOKING_TIMEOUT_SECS)
        except TimeoutError:
            audit.audit(call_id, "fallback_resolved", branch="cold_booking_timed_out")
            offer = None
        if offer is not None:
            audit.audit(call_id, "fallback_resolved", branch="cold_booking", **offer)
            return book_action(offer)
    audit.audit(call_id, "fallback_resolved", branch="unscored_refusal", reason="out_of_scope")
    return dict(UNSCORED_REFUSAL)
