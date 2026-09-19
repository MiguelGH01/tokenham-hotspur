"""Validate and confirm new-patient demographics; never create a booking."""

import re
from datetime import date

from pipecat.flows import FlowsFunctionSchema, NodeConfig

from clinic_catalog import load_catalog
from flows.common import gated_confirmation
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
    patient["email"] = patient["email"].lower()
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


async def prepare_registration(args, flow_manager):
    state = flow_manager.state
    state.pop("registration_draft", None)
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
    state["registration_draft"] = patient
    return {"status": "needs_confirmation", "readback": patient}, create_registration_confirm_node()


async def confirm_registration(args, flow_manager):
    state = flow_manager.state
    if args.get("confirmed") is not True or "registration_draft" not in state:
        return {"status": "not_confirmed"}, create_registration_node()
    submission = state["submission"]
    blocked = gated_confirmation(
        "confirm_registration",
        flow_manager,
        decided=submission.delivery_started,
        instruction=(
            "Clarify the caller's correction, condition or unfinished field before "
            "registering; do not register with an unconfirmed detail. Only call "
            "confirm_registration again with an unqualified confirmation."
        ),
    )
    if blocked is not None:
        return blocked
    if not submission.delivery_started:
        submission.set_register(state["registration_draft"])
    accepted = await submission.flush()
    from flows.common import create_goodbye_node

    return {"status": "accepted" if accepted else "delivery_failed"}, create_goodbye_node(
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
                "content": "Read back the supplied demographics for explicit confirmation, spelling the email and national ID character by character. "
                "These are caller-supplied details, not directory data. Ask if everything is correct. Only then confirm_registration. "
                "For corrections call prepare_registration with the whole corrected record. No booking.",
            }
        ],
        functions=[
            FlowsFunctionSchema(
                name="confirm_registration",
                description="Confirm only after the caller accepts the complete readback.",
                properties={"confirmed": {"type": "boolean"}},
                required=["confirmed"],
                handler=confirm_registration,
                cancel_on_interruption=True,
            ),
            create_registration_node()["functions"][0],
        ],
    )
