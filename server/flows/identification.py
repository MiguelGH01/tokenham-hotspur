"""Identify the patient independently of the caller and route new patients to registration."""

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NodeConfig

from flows.common import ROLE_MESSAGE, announce, create_giveup_node
from national_id import is_valid_national_id, normalize_national_id

MAX_IDENTIFY_ATTEMPTS = 3


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[-9:]


@announce("search_patient")
async def search_patient(args: FlowArgs, flow_manager: FlowManager):
    from flows.booking import create_slot_node

    state = flow_manager.state
    id_type, id_value, stated_name = args["id_type"], args["id_value"], args["stated_name"]

    def failed(status: str):
        state["identify_attempts"] += 1
        if state["identify_attempts"] >= MAX_IDENTIFY_ATTEMPTS:
            return {"status": status, "attempts_left": 0}, create_giveup_node()
        return {
            "status": status,
            "attempts_left": MAX_IDENTIFY_ATTEMPTS - state["identify_attempts"],
        }, None

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
        # The clinic's own API failing is not the caller failing to be found. The
        # two must not look alike to the model: told only "could not find them",
        # it reads a correctly-spelled name back as absent from the records and
        # asks the caller to repeat an identifier they already gave, and the call
        # is lost with nobody identified — observed as /v1/directory 502 then
        # ReadTimeout on 19 Sep, twice in fifteen local calls.
        logger.error("directory lookup failed: {}", type(exc).__name__)
        return {
            "status": "lookup_failed",
            "instruction": (
                "The clinic's records could not be reached. This is a fault on our side, "
                "not a missing patient. Do not tell the caller they are not in the "
                "system and do not ask them to repeat an identifier they gave "
                "correctly. Apologise for the delay in one short sentence and call "
                "search_patient again with exactly the same name and identifier."
            ),
        }, None

    if len(matches) != 1:
        return failed("not_found")

    patient = matches[0]
    from flows.requests import revise_request

    revise_request(flow_manager)
    relationship = args.get("relationship") or "self"
    if relationship == "self":
        state["caller"] = patient
    state["patient"] = patient
    visited = "a returning patient" if patient["has_visited_before"] else "a first-time patient"
    summary = f"Found {patient['given_name']} {patient['first_surname']}, {visited}."
    if state.get("intent") in ("cancel", "reschedule"):
        from flows.appointments import create_appointments_node, load_appointments
        result, node = await load_appointments(flow_manager)
        return result, node or create_appointments_node()
    return {"status": "found", "patient_summary": summary}, create_slot_node(flow_manager)


def _search_patient_schema() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="search_patient",
        description="Look the patient up in the clinic records by name plus one exact identifier.",
        properties={
            "stated_name": {
                "type": "string",
                "description": "Patient's full name as the caller said it.",
            },
            "id_type": {"type": "string", "enum": ["national_id", "phone"]},
            "id_value": {
                "type": "string",
                "description": "DNI/NIE including the final letter, or the phone number, digits as heard.",
            },
            "relationship": {
                "type": "string",
                "enum": ["self", "parent", "carer", "other"],
                "description": "self if the caller is the patient; otherwise who they are to the patient.",
            },
        },
        required=["stated_name", "id_type", "id_value"],
        handler=search_patient,
        cancel_on_interruption=True,
    )


def create_identify_node() -> NodeConfig:
    from flows.reception import start_registration

    return NodeConfig(
        name="identify",
        role_message=ROLE_MESSAGE,
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Establish who the appointment is for. Example: 'my son's had a temperature' "
                    "→ the patient is the child, not the caller. If they first gave their own "
                    "details and then said it is for someone else, keep them as the caller and "
                    "search again for the patient with relationship other than self. Establish the patient's full name "
                    "and ONE exact identifier: their DNI or NIE "
                    "including the letter, or their phone number. Ask for whatever is missing, one "
                    "short question at a time. The moment you hold the full name plus one complete "
                    "identifier, call search_patient immediately — do not ask for a second "
                    "identifier or a date of birth, and do not read details back first. Never "
                    "search by name alone. If the caller says they are new, call start_registration. If the result is misheard_id or not_found, say you could not find them "
                    "and ask them to repeat the identifier slowly, digit by digit. A "
                    "lookup_failed result is a fault in the clinic's records, not a missing "
                    "patient: apologise for the delay and call search_patient again with the "
                    "same details, and never tell the caller they are not in the system. When the caller "
                    "repeats or corrects the identifier, always call search_patient again with what "
                    "you heard — never give up on your own; the flow decides when attempts are exhausted."
                ),
            }
        ],
        # The fixed greeting is queued by bot.py after initialize(): as a tts_say pre_action,
        # a caller barging into it drops Flows' ActionFinishedFrame and the node never loads.
        respond_immediately=True,
        functions=[_search_patient_schema(), start_registration],
    )
