"""Shared voice guidance and terminal nodes, independent of individual flows."""

from pipecat.flows import NodeConfig

import audit
import confirmation

GREETING = "Clínica Arenal, how can I help you?"
ROLE_MESSAGE = (
    "You are the receptionist for Clínica Arenal. Answer in the caller's language. "
    "Your responses will be spoken aloud, so avoid emojis, bullet points, or other formatting that cannot be spoken. "
    "Ask one short question at a time. Never invent records, slots or rules. Never disclose directory identifiers. "
    "Read back caller-supplied registration data only for confirmation. Preserve already supplied details during transitions."
)


async def flush_submission(action, flow_manager):
    """Terminal nodes: the plan is final now, so promote it and deliver it.

    Every terminal node in this module ends the conversation in the same breath,
    which is exactly the point at which a provisional refusal stops being
    revisable. Promoting here means no flow has to remember to.
    """
    submission = flow_manager.state["submission"]
    submission.decide()
    await submission.flush()


#: Plain words for the rules a caller can hit, so the refusal is explained
#: rather than recited as a machine token.
RULE_WORDS = {
    "not_eligible_age": "this patient is seen by a different team because of their age",
    "referral_required": "this specialty needs a referral that we do not have on file",
    "provider_not_in_network": "that doctor does not work with this patient's insurance",
    "specialty_not_covered": "this patient's insurance does not cover that specialty",
    "location_not_covered": "this patient's insurance does not cover that site",
    "provider_on_leave": "that doctor is away",
    "provider_not_found": "there is no doctor by that name at this clinic",
    "no_availability": "there is nothing free in that window",
}


def gated_confirmation(node: str, flow_manager, *, decided: bool = False, instruction: str | None = None):
    """Block a confirmed write the caller has not unqualifiedly agreed to.

    A "yes, but..." is not consent: a price question, a correction, a request
    to check an alternative or a negation all mean the conversation is still
    deciding. The check is deterministic code (see ``confirmation.py``), not
    model judgement, because whether the record should be delivered is scored.

    Returns ``None`` when the confirmation may proceed, or a ``(result, None)``
    tuple for the tool to return so it stays in its node and clarifies instead.
    Conservative in both directions that matter: a plain "yes" never blocks,
    and a missing transcript never blocks either.
    """
    if decided:
        return None
    utterance = ""
    try:
        for message in reversed(flow_manager.get_current_context()):
            if message.get("role") == "user":
                utterance = str(message.get("content") or "")
                break
    except Exception:  # no context, no gate: never block on missing data
        return None
    reason = confirmation.gate_result(utterance)
    if reason is None:
        return None
    audit.audit(
        flow_manager.state.get("call_id", "unknown"),
        "gate_blocked",
        node=node,
        reason_code=reason,
    )
    return {
        "status": "qualified_confirmation",
        "reason_code": reason,
        "instruction": instruction,
    }, None


def create_refusal_node(reason: str) -> NodeConfig:
    """A rule that bit ends the attempt.

    Deliberately terminal and tool-free. A refusal the model is free to keep
    working around becomes a substitution — offering general practice to someone
    who asked for dermatology — and the contract's expected answer for a refusal
    is a single NO_ACTION carrying the rule that bit, not a different booking.
    Wording a prompt cannot be trusted to hold; the node graph can.
    """
    words = RULE_WORDS.get(reason, "that cannot be done")
    return NodeConfig(
        name="refused",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"Tell the caller plainly that {words}. One or two short sentences, then "
                    "stop. Offer nothing else: no other specialty, no other doctor, no other "
                    "site and no other day. Do not ask a question. Do not apologise more than "
                    "once. Then say goodbye."
                ),
            }
        ],
        post_actions=[
            {"type": "function", "handler": flush_submission},
            {"type": "end_conversation"},
        ],
    )


def create_goodbye_node(accepted=True, kind="appointment"):
    return NodeConfig(
        name="goodbye",
        task_messages=[
            {
                "role": "developer",
                "content": f"Confirm the {kind} request was received and say goodbye."
                if accepted
                else "Explain that delivery could not be confirmed. Do not claim success. Say goodbye.",
            }
        ],
        post_actions=[
            {"type": "function", "handler": flush_submission},
            {"type": "end_conversation"},
        ],
    )


def create_giveup_node():
    return NodeConfig(
        name="giveup",
        task_messages=[
            {
                "role": "developer",
                "content": "Explain that the patient could not be identified. No appointment was booked. Say goodbye.",
            }
        ],
        post_actions=[
            {"type": "function", "handler": flush_submission},
            {"type": "end_conversation"},
        ],
    )
