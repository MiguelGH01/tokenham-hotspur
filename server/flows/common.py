"""Shared voice guidance and terminal nodes, independent of individual flows."""

from pipecat.flows import NodeConfig

GREETING = "Clínica Arenal, how can I help you?"
ROLE_MESSAGE = (
    "You are the receptionist for Clínica Arenal. Answer in the caller's language. "
    "Your responses will be spoken aloud, so avoid emojis, bullet points, or other formatting that cannot be spoken. "
    "Ask one short question at a time. Never invent records, slots or rules. Never disclose directory identifiers. "
    "Read back caller-supplied registration data only for confirmation. Preserve already supplied details during transitions."
)


async def flush_submission(action, flow_manager):
    await flow_manager.state["submission"].flush()


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
