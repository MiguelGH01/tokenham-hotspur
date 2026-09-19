"""Validate and confirm new-patient demographics; never create a booking."""

import re
from datetime import date

from pipecat.flows import FlowsFunctionSchema, NodeConfig

from clinic_catalog import load_catalog
from flows.common import WAIT_FOR_ANSWER, announce, gated_confirmation, speak_tool
from national_id import is_valid_national_id, normalize_national_id

FIELDS = (
    "given_name",
    "first_surname",
    "second_surname",
    "national_id",
    "date_of_birth",
    "phone",
    "email",
    "insurer",
)


def validate_registration(values, now):
    patient = {key: str(values.get(key, "")).strip() for key in FIELDS}
    errors = [key for key, value in patient.items() if not value]
    patient["national_id"] = normalize_national_id(patient["national_id"])
    if not is_valid_national_id(patient["national_id"]):
        errors.append("national_id")
    try:
        birthday = date.fromisoformat(patient["date_of_birth"])
        if birthday > now.date() or now.year - birthday.year > 120:
            errors.append("date_of_birth")
    except ValueError:
        errors.append("date_of_birth")
    patient["phone"] = re.sub(r"[\s()-]", "", patient["phone"])
    if not re.fullmatch(r"(?:\+34|0034)?[6789]\d{8}", patient["phone"]):
        errors.append("phone")
    patient["email"] = re.sub(r"\s+", "", patient["email"]).lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", patient["email"]):
        errors.append("email")
    plan_aliases = {
        alias.casefold(): plan["id"]
        for plan in load_catalog()["plans"]
        for alias in (plan["id"], plan["name"])
    }
    patient["insurer"] = plan_aliases.get(patient["insurer"].casefold(), patient["insurer"])
    if patient["insurer"] not in [p["id"] for p in load_catalog()["plans"]]:
        errors.append("insurer")
    return patient, sorted(set(errors))


@announce("prepare_registration")
async def prepare_registration(args, flow_manager):
    state = flow_manager.state
    from flows.requests import prepare_proposal, revise_request

    if state["submission"].delivery_attempted:
        return {
            "status": "delivery_pending",
            "instruction": (
                "These details are already with the clinic and cannot be changed. Retry "
                "confirm_registration, or route a new request."
            ),
        }, None
    revise_request(flow_manager)
    revision = state["revision"]
    patient, errors = validate_registration(args, state["connected_at"])
    if errors:
        return {
            "status": "invalid",
            "fields": errors,
            "instruction": "Ask the caller to correct these fields; never invent a check letter.",
        }, None
    try:
        matches = await state["client"].search_directory(
            national_id=patient["national_id"], name=" ".join(patient[k] for k in FIELDS[:3])
        )
    except Exception:
        return {"status": "lookup_failed"}, None
    if any(normalize_national_id(p["national_id"]) == patient["national_id"] for p in matches):
        return {
            "status": "already_registered",
            "instruction": "Do not register again or book automatically.",
        }, None
    if state.get("revision") != revision:
        return {"status": "expired"}, None
    state["registration_draft"] = patient
    prepare_proposal(flow_manager, "registration")
    return {"status": "needs_confirmation", "readback": patient}, create_registration_confirm_node()


async def confirm_registration(args, flow_manager):
    state = flow_manager.state
    if args.get("confirmed") is not True or "registration_draft" not in state:
        return {"status": "not_confirmed"}, create_registration_node()
    submission = state["submission"]
    from flows.common import create_completion_node, record_already_settled
    from flows.requests import proposal_status
    from submission import register_action

    announce_save = False
    if submission.delivery_attempted:
        # A retry of the record the frozen plan already carries is safe; a
        # different one cannot become this call's record any more.
        if not submission.carries(register_action(state["registration_draft"])):
            return record_already_settled("registration")
    else:
        status = proposal_status(flow_manager, "registration")
        if status == "stale":
            # The record was revised after the readback: read it back again.
            return {"status": "not_confirmed"}, create_registration_node()
        if status != "ok":
            return {
                "status": "needs_confirmation",
                "instruction": WAIT_FOR_ANSWER,
            }, None
        blocked = gated_confirmation(
            "confirm_registration",
            flow_manager,
            instruction=(
                "Clarify only the unfinished field; do not re-read the whole record. "
                "Call confirm_registration only after an unqualified yes."
            ),
        )
        if blocked is not None:
            return blocked
        submission.set_register(state["registration_draft"])
        announce_save = True
    if announce_save:
        await speak_tool(flow_manager, "confirm_registration")
    accepted = await submission.flush()
    return {"status": "accepted" if accepted else "delivery_failed"}, create_completion_node(
        accepted, "registration"
    )


def create_registration_node():
    return NodeConfig(
        name="registration",
        task_messages=[
            {
                "role": "developer",
                "content": "Register a new patient only. Collect given name, both surnames, DNI/NIE, full birth date with four-digit year, phone, email and insurer. "
                "Reuse what was already said. Ask one missing field at a time. Transcribe dictated digits/letters, at and dot carefully; ask for spelling when unsure. "
                "Never invent or fix the national ID check letter. Call prepare_registration when complete. Do not book an appointment.",
            }
        ],
        functions=[
            FlowsFunctionSchema(
                name="prepare_registration",
                description="Validate the complete demographics before readback.",
                properties={
                    **{k: {"type": "string"} for k in FIELDS},
                    "insurer": {
                        "type": "string",
                        "enum": [p["id"] for p in load_catalog()["plans"]],
                        "description": "Use the catalogue plan ID: "
                        + "; ".join(f"{p['name']} = {p['id']}" for p in load_catalog()["plans"]),
                    },
                },
                required=list(FIELDS),
                handler=prepare_registration,
                cancel_on_interruption=True,
            )
        ],
    )


def create_registration_confirm_node():
    return NodeConfig(
        name="registration_confirm",
        task_messages=[
            {
                "role": "developer",
                "content": "Read the supplied demographics back once, in one or two spoken sentences. "
                "Do not spell email or national ID character by character unless they ask. "
                "These are caller-supplied details, not directory data. Ask if everything is correct, then stop. "
                "Do not call confirm_registration in the same turn as the readback. "
                "If it returns needs_confirmation, stay silent; do not read the details again. "
                "If it returns qualified_confirmation, clarify the correction, condition or unfinished field first. "
                "If it returns delivery_conflict, the call's record is already settled: do not claim the registration, apologise and say goodbye. "
                "For corrections call prepare_registration with the whole corrected record. No booking.",
            }
        ],
        functions=[
            FlowsFunctionSchema(
                name="confirm_registration",
                description="Call only after the caller has accepted the one readback in a later turn.",
                properties={"confirmed": {"type": "boolean"}},
                required=["confirmed"],
                handler=confirm_registration,
                cancel_on_interruption=True,
            ),
            create_registration_node()["functions"][0],
        ],
    )
