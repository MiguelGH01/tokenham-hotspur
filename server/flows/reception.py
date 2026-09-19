"""Open-ended reception: route supported needs without resetting conversation history."""

from pipecat.flows import FlowsFunctionSchema, NodeConfig, flows_tool_options

from flows.common import ROLE_MESSAGE, announce
from flows.identification import create_identify_node
from flows.requests import begin_request


@announce("route_request")
async def route_request(args, flow_manager):
    intent = args["intent"]
    if intent not in ("book", "register", "cancel", "reschedule"):
        return {"status": "unsupported", "supported": ["book", "register", "cancel", "reschedule"]}, None
    begin_request(flow_manager, intent)
    if intent == "register":
        from flows.registration import create_registration_node

        return {"status": "routed"}, create_registration_node(flow_manager)
    return {"status": "routed"}, create_identify_node(flow_manager)


def create_reception_node():
    return NodeConfig(
        name="reception",
        role_message=ROLE_MESSAGE,
        respond_immediately=False,
        task_messages=[
            {
                "role": "developer",
                "content": "If they already said they need an appointment, route book on that turn — do not promise to book first and do not ask how you can help. "
                "Route as soon as the need is clear: a new appointment request is intent 'book' — identification of the patient happens in the next step, so never ask whether the appointment is for the caller or for someone else before routing. "
                "A caller who says they are new, have never been here, or are not in the "
                "system is intent 'register'. A caller who merely lacks their ID document or "
                "number right now is NOT 'register' — route 'book' as usual; they identify by "
                "phone or ID in the next step. Clarify only when the need itself is "
                "genuinely ambiguous, then route immediately. "
                "Preserve all details already spoken; do not ask them again. "
                "For cancellation route cancel; for moving an existing appointment route reschedule. "
                "Cancel two, both, or all upcoming appointments is still intent cancel — route it "
                "this turn. You have no search or cancel tool on this node: do not ask whose "
                "appointments they are, do not read an ID back, and do not claim a cancellation.",
            }
        ],
        functions=[
            FlowsFunctionSchema(
                name="route_request",
                description="Route the expressed need.",
                properties={
                    "intent": {"type": "string", "enum": ["book", "register", "cancel", "reschedule", "unsupported"]}
                },
                required=["intent"],
                handler=route_request,
                cancel_on_interruption=True,
            )
        ],
    )


@flows_tool_options(cancel_on_interruption=True)
@announce("start_registration")
async def start_registration(args, flow_manager):
    """The caller explicitly says they are new and wants registration, not another lookup."""
    from flows.registration import create_registration_node

    flow_manager.state["intent"] = "register"
    return {"status": "routed"}, create_registration_node(flow_manager)


def start_registration_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="start_registration",
        description="The caller says they are new and not in the clinic records.",
        properties={},
        required=[],
        handler=start_registration,
        cancel_on_interruption=True,
    )
