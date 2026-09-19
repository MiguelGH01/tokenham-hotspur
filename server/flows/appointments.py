"""Select existing appointments only from the verified patient's upcoming diary.

``PR-08`` is a question about *which* appointment, and the diary is the only
source of a valid ``appointment_id``: an id the model invented, or one belonging
to another patient, is a refusal answered as if it were a booking. So the
appointment is chosen from a table this module filled from the API, by exact id,
and the choice is then read back and confirmed in a **later** turn before
anything is cancelled or moved.
"""

from pipecat.flows import FlowsFunctionSchema, NodeConfig

from flows.common import gated_confirmation, record_already_settled
from flows.requests import prepare_proposal, proposal_status, revise_request
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


async def select_appointment(args, flow_manager):
    """Choose one of the returned appointments, and read it back.

    An unknown id is not a booking attempt to be salvaged: it stays in this node
    so the model asks which appointment the caller means.
    """
    state = flow_manager.state
    appointment = state.get("appointments", {}).get(args.get("appointment_id"))
    if not appointment or appointment["patient_id"] != state["patient"]["patient_id"]:
        return {"status": "invalid_appointment"}, None
    revise_request(flow_manager)
    state["appointment"] = appointment.copy()
    if state["intent"] == "reschedule":
        from flows.booking import create_slot_node

        return {"status": "selected", "appointment": appointment}, create_slot_node(flow_manager)
    prepare_proposal(flow_manager, appointment["appointment_id"])
    return {"status": "needs_confirmation", "readback": appointment}, create_cancel_node()


async def confirm_cancellation(args, flow_manager):
    state = flow_manager.state
    appointment = state.get("appointment")
    if not appointment:
        return {"status": "invalid_appointment"}, None
    submission = state["submission"]
    if submission.delivery_attempted:
        # A retry of what the frozen plan already carries is safe; a different
        # action cannot become this call's record any more.
        if not submission.carries(cancel_action(appointment["appointment_id"])):
            return record_already_settled("cancellation")
    else:
        status = proposal_status(flow_manager, appointment["appointment_id"])
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
                "instruction": (
                    "Nothing is cancelled yet: the caller has to confirm the readback out "
                    "loud, in a turn of their own. Ask them and call confirm_cancellation "
                    "again."
                ),
            }, None
        blocked = gated_confirmation(
            "confirm_cancellation",
            flow_manager,
            instruction=(
                "The caller's answer carries a condition, correction or question. Do not "
                "treat it as consent to cancel. Clarify the unfinished part first; only "
                "call confirm_cancellation again with an unqualified confirmation."
            ),
        )
        if blocked is not None:
            return blocked
        submission.set_cancel(appointment["appointment_id"])
    accepted = await submission.flush()
    from flows.common import create_completion_node

    return {"status": "accepted" if accepted else "delivery_failed"}, create_completion_node(
        accepted, "cancellation"
    )


def _selection_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="select_appointment",
        description="Select an appointment ID returned from the verified patient's diary.",
        properties={"appointment_id": {"type": "string"}},
        required=["appointment_id"],
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
                    "Read back the selected appointment: its date, time, doctor and site. Ask "
                    "the caller to confirm the cancellation. If confirm_cancellation returns "
                    "needs_confirmation, the caller has not answered the readback in a turn of "
                    "their own yet: ask them and call it again only once they have. If it "
                    "returns qualified_confirmation, clarify the condition, question or "
                    "correction first. If it returns expired, that appointment is no longer the "
                    "one on offer: select it again. If it returns delivery_conflict, do not "
                    "claim the cancellation and say goodbye. A correction means selecting the "
                    "appointment again, never cancelling the one that was read out."
                ),
            }
        ],
        functions=[_confirm_cancellation_schema(), _selection_schema()],
    )


def _confirm_cancellation_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="confirm_cancellation",
        description="Cancel the selected appointment after explicit confirmation.",
        properties={},
        required=[],
        handler=confirm_cancellation,
        cancel_on_interruption=True,
    )


def create_appointments_node() -> NodeConfig:
    return NodeConfig(
        name="appointments",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Use only the returned upcoming appointments. Clarify which appointment the "
                    "caller means by date, provider or site, then select_appointment. Never "
                    "invent an ID and never select an ambiguous appointment. If lookup failed, "
                    "retry lookup_appointments; never claim an empty diary."
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
        ],
    )


async def lookup_appointments(args, flow_manager):
    return await load_appointments(flow_manager)
