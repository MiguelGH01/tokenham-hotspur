"""Select existing appointments only from the verified patient's upcoming diary.

``PR-08`` is a question about *which* appointment, and the diary is the only
source of a valid ``appointment_id``: an id the model invented, or one belonging
to another patient, is a refusal answered as if it were a booking. So the
appointment is chosen from a table this module filled from the API, by exact id,
and the choice is then read back and confirmed in a **later** turn before
anything is cancelled or moved.
"""

from pipecat.flows import FlowsFunctionSchema, NodeConfig, flows_tool_options

from flows.common import WAIT_FOR_ANSWER, announce, gated_confirmation, record_already_settled, speak_tool
from flows.requests import prepare_proposal, proposal_status, revise_request
from observability.emit import trace_tool
from submission import cancel_action


async def load_appointments(manager):
    """The patient's upcoming diary, keyed by id.

    Only rows the asked-for patient owns are kept: the lookup is one call away
    from a wrong appointment, and ``appointment_id`` is not something the caller
    can correct after the fact.
    """
    state = manager.state
    try:
        rows = await state["client"].appointments(state["patient"]["patient_id"])
    except Exception:
        return {"status": "lookup_failed"}, None
    state["appointments"] = {
        row["appointment_id"]: row
        for row in rows
        if row["patient_id"] == state["patient"]["patient_id"]
    }
    return {
        "status": "appointments",
        "appointments": list(state["appointments"].values()),
    }, create_appointments_node()


def _selected_ids(args) -> list[str]:
    ids = []
    for value in args.get("appointment_ids") or []:
        if value:
            ids.append(value)
    if args.get("appointment_id"):
        ids.append(args["appointment_id"])
    seen: set[str] = set()
    ordered: list[str] = []
    for appointment_id in ids:
        if appointment_id not in seen:
            seen.add(appointment_id)
            ordered.append(appointment_id)
    return ordered


def _proposal_key(ids: list[str]) -> str:
    return ids[0] if len(ids) == 1 else ",".join(ids)


@trace_tool()
@announce("select_appointment")
async def select_appointment(args, flow_manager):
    """Choose diary rows by id, and read them back.

    Several ids in one call is how PR-08 cancels two upcoming appointments:
    both ids come off this patient's diary, one readback, one later yes, two
    CANCEL posts. An unknown id is not a booking attempt to be salvaged.
    """
    state = flow_manager.state
    diary = state.get("appointments", {})
    patient_id = state["patient"]["patient_id"]
    ids = _selected_ids(args)
    chosen = [diary.get(appointment_id) for appointment_id in ids]
    if not ids or any(
        appointment is None or appointment["patient_id"] != patient_id for appointment in chosen
    ):
        return {"status": "invalid_appointment"}, None
    revise_request(flow_manager)
    state["appointment"] = chosen[0].copy()
    state["cancel_batch"] = [appointment.copy() for appointment in chosen]
    if state["intent"] == "reschedule":
        if len(chosen) != 1:
            return {
                "status": "invalid_appointment",
                "instruction": "A move is one appointment. Select only the booking they want to change.",
            }, None
        from flows.booking import create_slot_node

        return {"status": "selected", "appointment": chosen[0]}, create_slot_node(flow_manager)
    prepare_proposal(flow_manager, _proposal_key(ids))
    return {
        "status": "needs_confirmation",
        "readback": chosen if len(chosen) > 1 else chosen[0],
    }, create_cancel_node()


@trace_tool()
async def confirm_cancellation(args, flow_manager):
    state = flow_manager.state
    batch = state.get("cancel_batch") or (
        [state["appointment"]] if state.get("appointment") else []
    )
    if not batch:
        return {"status": "invalid_appointment"}, None
    ids = [row["appointment_id"] for row in batch]
    submission = state["submission"]
    announce_cancel = False
    if submission.delivery_attempted:
        # A retry of what the frozen plan already carries is safe; a different
        # action cannot become this call's record any more.
        if not all(submission.carries(cancel_action(appointment_id)) for appointment_id in ids):
            return record_already_settled("cancellation")
    else:
        status = proposal_status(flow_manager, _proposal_key(ids))
        if status == "stale":
            # The appointment changed after the readback: read it back again
            # from the diary rather than cancelling the one that was named.
            return {
                "status": "expired",
                "instruction": "That appointment is no longer the one on offer. Select it again.",
            }, None
        if status != "ok":
            return {
                "status": "needs_confirmation",
                "instruction": WAIT_FOR_ANSWER,
            }, None
        blocked = gated_confirmation(
            "confirm_cancellation",
            flow_manager,
            instruction=(
                "The caller's answer carries a condition, correction or question. Do not "
                "treat it as consent to cancel. Clarify only the unfinished part; do not "
                "re-read the appointment."
            ),
        )
        if blocked is not None:
            return blocked
        submission.set_cancel(ids[0], appointment=batch[0])
        for appointment_id in ids[1:]:
            submission.add_action(cancel_action(appointment_id))
        announce_cancel = True
    if announce_cancel:
        await speak_tool(flow_manager, "confirm_cancellation")
    accepted = await submission.flush()
    diary = {
        appointment_id: row
        for appointment_id, row in (state.get("appointments") or {}).items()
        if appointment_id not in set(ids)
    }
    state["appointments"] = diary
    state.pop("appointment", None)
    state.pop("cancel_batch", None)
    from flows.common import create_completion_node

    if accepted and diary:
        # A second CANCEL cannot rewrite the first request's frozen plan.
        root = state.setdefault("call_submission", state["submission"])
        state["submission"] = root.new_request()
        return {
            "status": "accepted",
            "remaining": list(diary.values()),
            "instruction": (
                "Those cancellations were received. If they asked to cancel more of the "
                "upcoming list, select the next remaining appointment now. If they said "
                "to keep the rest, or they are finished, call finish_call. Do not invent "
                "an id and do not claim a cancellation you have not selected."
            ),
        }, create_appointments_node()
    return {"status": "accepted" if accepted else "delivery_failed"}, create_completion_node(
        accepted, "cancellation"
    )


def _selection_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="select_appointment",
        description=(
            "Select one or more appointment IDs returned from the verified patient's "
            "diary. To cancel two (or all remaining) upcoming bookings they asked to "
            "drop, pass every matching id in appointment_ids in this one call."
        ),
        properties={
            "appointment_id": {"type": "string"},
            "appointment_ids": {"type": "array", "items": {"type": "string"}},
        },
        required=[],
        handler=select_appointment,
        cancel_on_interruption=True,
    )


def create_cancel_node() -> NodeConfig:
    """Read the chosen appointment back and wait for a later, explicit yes."""
    return NodeConfig(
        name="cancel_confirm",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Read back the selected appointment once: its date, time, doctor and site. "
                    "Ask the caller to confirm the cancellation, then stop. If more than "
                    "one appointment was selected, read each once. Do not call "
                    "confirm_cancellation in the same turn. Do not read it again. "
                    "If confirm_cancellation returns needs_confirmation, stay silent. If it "
                    "returns qualified_confirmation, clarify the condition, question or "
                    "correction first. If it returns expired, that appointment is no longer the "
                    "one on offer: select it again. If it returns delivery_conflict, do not "
                    "claim the cancellation and say goodbye. A correction means selecting the "
                    "appointment again, never cancelling the one that was read out. If they "
                    "decide to keep the appointment and do not want to select a different one, "
                    "call keep_appointment — do not just say goodbye in text."
                ),
            }
        ],
        functions=[_confirm_cancellation_schema(), _selection_schema(), keep_appointment],
    )


@flows_tool_options(cancel_on_interruption=True)
@announce("keep_appointment")
async def keep_appointment(flow_manager):
    """The caller decides not to cancel the appointment that was read back."""
    state = flow_manager.state
    submission = state["submission"]
    if submission.pending["action"] != "NO_ACTION":
        return {"status": "already_confirmed"}, None
    if not submission.actions:
        # Nothing was decided yet: state a NO_ACTION now, or a hang-up right
        # after this reads state["appointment"] (still set from selection) and
        # submits CANCEL for a booking the caller just asked to keep.
        submission.set_no_action("no_availability", provisional=False)
    state.pop("appointment", None)
    state.pop("cancel_batch", None)
    # The cancellation this call read back is no longer live either way: a
    # hang-up right after this must not let resolve_fallback rediscover it.
    revise_request(flow_manager)
    await submission.flush()
    return {"status": "kept"}, NodeConfig(
        name="kept",
        task_messages=[
            {
                "role": "developer",
                "content": "The appointment was not cancelled and stays as it was. Confirm that and say goodbye.",
            }
        ],
        post_actions=[{"type": "end_conversation"}],
    )


def _confirm_cancellation_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="confirm_cancellation",
        description="Cancel after the caller has accepted the one readback in a later turn.",
        properties={},
        required=[],
        handler=confirm_cancellation,
        cancel_on_interruption=True,
    )


def create_appointments_node() -> NodeConfig:
    from flows.common import finish_call
    from flows.reception import create_reception_node

    return NodeConfig(
        name="appointments",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Use only the returned upcoming appointments. You cannot cancel by talking: "
                    "call select_appointment with the diary ids, then wait for confirm_cancellation "
                    "after they say yes. If they asked to cancel two, both, or all of the listed "
                    "upcoming bookings, pass every matching id in appointment_ids in one "
                    "select_appointment call — never invent an ID and never skip the lookup. "
                    "If they named one by date, doctor or site, select only that id. If they "
                    "said to keep the rest, or they are finished after a cancellation, call "
                    "finish_call. If lookup failed, retry lookup_appointments; never claim an "
                    "empty diary and never say you have no cancel tool. Use route_request for "
                    "a new independent request."
                ),
            }
        ],
        functions=[
            _selection_schema(),
            FlowsFunctionSchema(
                name="lookup_appointments",
                description="Retry the verified patient's diary lookup.",
                properties={},
                required=[],
                handler=lookup_appointments,
            ),
            FlowsFunctionSchema(
                name="finish_call",
                description="The caller has no more appointments to cancel.",
                properties={},
                required=[],
                handler=finish_call,
            ),
            *create_reception_node()["functions"],
        ],
    )


@announce("lookup_appointments")
async def lookup_appointments(args, flow_manager):
    return await load_appointments(flow_manager)
