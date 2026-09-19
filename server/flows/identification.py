"""Identify the patient independently of the caller and route new patients to registration."""

import re

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NodeConfig

from flows.common import ROLE_MESSAGE, announce, create_giveup_node
from llm_messages import chat_role, chat_text
from national_id import is_valid_national_id, normalize_national_id

MAX_IDENTIFY_ATTEMPTS = 3
_ID_IN_TEXT = re.compile(r"\b(\d{8}\s*-?\s*[A-Za-z]|[XYZxyz]\s*\d{7}\s*-?\s*[A-Za-z])\b")
_NAME_INTRO = re.compile(
    r"(?:my name is|i am|i'm|this is|me llamo|soy)\s+"
    r"([A-Za-záéíóúñüÁÉÍÓÚÑÜ]+(?:\s+[A-Za-záéíóúñüÁÉÍÓÚÑÜ]+){1,3})",
    re.I,
)
_ID_SPLIT = re.compile(
    r"\b(?:dni|nie|n\.?i\.?e\.?|national id|phone|tel[eé]fono)\b", re.I
)
_NAME_FILLER = {
    "a", "an", "and", "appointment", "book", "booking", "for", "gp", "hello",
    "hi", "hola", "i", "need", "please", "see", "the", "to", "want", "with",
    "yes", "yeah",
}


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[-9:]


def _user_blob(flow_manager) -> str:
    getter = getattr(flow_manager, "get_current_context", None)
    if getter is None:
        return ""
    try:
        return " ".join(
            chat_text(message) or "" for message in getter() if chat_role(message) == "user"
        )
    except Exception:
        return ""


def _name_from_blob(blob: str) -> str | None:
    if intro := _NAME_INTRO.search(blob):
        return intro.group(1).strip()
    parts = _ID_SPLIT.split(blob, maxsplit=1)
    if len(parts) != 2:
        return None
    words = [
        token
        for token in re.findall(r"[A-Za-záéíóúñüÁÉÍÓÚÑÜ]+", parts[0])
        if token.lower() not in _NAME_FILLER
    ]
    if 2 <= len(words) <= 5:
        return " ".join(words[-4:])
    return None


def spoken_identity(flow_manager) -> dict:
    """Name and identifier already in the caller's turns, if any.

    Asking again for a DNI they just said is how calls burn the clock and then
    submit nothing. This is only a hint for the prompt and a prefill: the
    directory lookup still has to match.
    """
    blob = _user_blob(flow_manager)
    found: dict[str, str] = {}
    for match in _ID_IN_TEXT.finditer(blob):
        raw = re.sub(r"[\s\-]", "", match.group(1))
        if is_valid_national_id(raw):
            found["id_type"] = "national_id"
            found["id_value"] = normalize_national_id(raw)
            break
    digits = re.sub(r"\D", "", blob)
    if "id_value" not in found and len(digits) >= 9 and digits[-9] in "67":
        found["id_type"] = "phone"
        found["id_value"] = digits[-9:]
    if name := _name_from_blob(blob):
        found["name"] = name
    return found


@announce("search_patient")
async def search_patient(args: FlowArgs, flow_manager: FlowManager):
    from flows.booking import create_slot_node

    state = flow_manager.state
    known = spoken_identity(flow_manager)
    id_type = args.get("id_type") or known.get("id_type")
    id_value = args.get("id_value") or known.get("id_value")
    stated_name = args.get("stated_name") or known.get("name")
    if not id_type or not id_value or not stated_name:
        return {
            "status": "incomplete",
            "instruction": (
                "Use the name and identifier already in the caller's turns. "
                "Do not ask again for a fact they already gave."
            ),
        }, None

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

    if not matches:
        # Zero exact hits: speak a clarification, then register if they confirm.
        # Jumping straight to registration with nothing to say is the silence cut.
        return _clarify_not_on_file(state, flow_manager, stated_name, id_type, wanted)
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
    return {
        "status": "found",
        "patient_summary": summary,
        "instruction": (
            "Do not ask the caller to confirm the name or identifier. "
            "Go straight to finding the appointment."
        ),
    }, create_slot_node(flow_manager)


def _clarify_not_on_file(state, flow_manager, stated_name, id_type, wanted):
    state["registration_seed"] = {
        "stated_name": stated_name or "",
        "national_id": wanted if id_type == "national_id" else "",
        "phone": wanted if id_type == "phone" else "",
    }
    return {
        "status": "not_on_file",
        "instruction": (
            "Speak now. There is no matching record. Ask whether they are new, or "
            "whether the name or identifier should be tried again. Then stop. "
            "Do not call start_registration in this turn. Do not book a similar name."
        ),
    }, create_not_on_file_node(flow_manager)


def create_not_on_file_node(flow_manager=None):
    from flows.reception import start_registration_schema

    seed = {}
    if flow_manager is not None:
        seed = flow_manager.state.get("registration_seed") or {}
    bits = ", ".join(f"{k}={v}" for k, v in seed.items() if v)
    looked = bits or "those details"
    return NodeConfig(
        name="not_on_file",
        respond_immediately=True,
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"Speak in this turn. No clinic record matches {looked}. "
                    "In one short sentence say you could not find them and ask if they "
                    "are a new patient, or if the name or identifier should be tried "
                    "again. Then stop talking. Do not stay silent. Do not read the "
                    "identifier back. Do not book anyone with a similar name. "
                    "Do not call start_registration until they have answered. "
                    "When they confirm they are new, call start_registration. "
                    "When they correct the name or identifier, call search_patient "
                    "with the correction."
                ),
            }
        ],
        functions=[_search_patient_schema(), start_registration_schema()],
    )


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


def create_identify_node(flow_manager=None) -> NodeConfig:
    from flows.reception import start_registration_schema

    known = spoken_identity(flow_manager)
    if known.get("name") and known.get("id_value"):
        already = (
            f"The caller already gave the name {known['name']} and "
            f"{known['id_type']} {known['id_value']}. Call search_patient now with those "
            "values. Do not ask for them again, do not thank them and wait, and do not "
            "read the identifier back."
        )
    elif known.get("name"):
        already = (
            f"You already have the name {known['name']}. Ask only for DNI, NIE or phone. "
            "Do not ask the name again."
        )
    elif known.get("id_value"):
        already = (
            f"You already have {known['id_type']} {known['id_value']}. Ask only for the "
            "full name. Do not ask for the identifier again."
        )
    else:
        already = (
            "Ask only for what is missing. Never re-ask a name or identifier they already said."
        )

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
                    "search again for the patient with relationship other than self. "
                    "If they said my appointment, cancel two, or already named themselves, they "
                    "are the patient — do not ask whose appointments they are. "
                    f"{already} "
                    "The moment you hold the full name plus one complete identifier, call "
                    "search_patient immediately. Never search by name alone. You cannot cancel "
                    "or move until search_patient returns the diary. If the caller says "
                    "they are new, call start_registration. If search_patient returns not_on_file, "
                    "the next node asks whether they are new: speak that question, wait, then "
                    "start_registration after they confirm. Do not stay silent and do not book a "
                    "similar name. If the result is "
                    "misheard_id, ask them to repeat the identifier slowly, digit by digit. A "
                    "lookup_failed result is a fault in the clinic's records: apologise for the "
                    "delay and call search_patient again with the same details. When they repeat "
                    "or correct the identifier, call search_patient again. "
                    "Do not read a DNI/NIE back for confirmation before calling search_patient — "
                    "call the tool with what you heard; its result tells you whether it was right. "
                    "If you already read an identifier back once and the caller said it was wrong, "
                    "do not read the same digits back again: ask them to say it one digit at a "
                    "time instead, then call search_patient with that — do not ask for a third "
                    "free-form dictation of the whole number."
                ),
            }
        ],
        respond_immediately=True,
        functions=[_search_patient_schema(), start_registration_schema()],
    )
