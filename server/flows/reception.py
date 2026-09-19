"""Open-ended reception: route supported needs without resetting conversation history."""

from pipecat.flows import FlowsFunctionSchema, NodeConfig, flows_tool_options

from flows.common import ROLE_MESSAGE
from flows.identification import create_identify_node


async def route_request(args, flow_manager):
    intent = args["intent"]
    if intent not in ("book", "register"):
        return {"status": "unsupported", "supported": ["book", "register"]}, None
    flow_manager.state["intent"] = intent
    if intent == "register":
        from flows.registration import create_registration_node

        return {"status": "routed"}, create_registration_node()
    return {"status": "routed"}, create_identify_node()


def create_reception_node():
    return NodeConfig(
        name="reception",
        role_message=ROLE_MESSAGE,
        respond_immediately=False,
        task_messages=[
            {
                "role": "developer",
                "content": "Ask how you can help. Route as soon as the need is clear: any appointment request is intent 'book' — identification of the patient happens in the next step, so never ask whether the appointment is for the caller or for someone else before routing. "
                "A caller who says they are new, have never been here, or are not in the "
                "system is intent 'register'. A caller who merely lacks their ID document or "
                "number right now is NOT 'register' — route 'book' as usual; they identify by "
                "phone or ID in the next step. Clarify only when the need itself is "
                "genuinely ambiguous, then route immediately. "
                "Preserve all details already spoken; do not ask them again. "
                "Changes and cancellations are not supported yet; never claim to complete them.",
            }
        ],
        functions=[
            FlowsFunctionSchema(
                name="route_request",
                description="Route the expressed need.",
                properties={
                    "intent": {"type": "string", "enum": ["book", "register", "unsupported"]}
                },
                required=["intent"],
                handler=route_request,
                cancel_on_interruption=True,
            )
        ],
    )


@flows_tool_options(cancel_on_interruption=True)
async def start_registration(flow_manager):
    """The caller explicitly says they are new and wants registration, not another lookup."""
    from flows.registration import create_registration_node

    flow_manager.state["intent"] = "register"
    return {"status": "routed"}, create_registration_node()
