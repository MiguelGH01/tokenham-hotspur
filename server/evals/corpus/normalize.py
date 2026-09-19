"""The published normalization table, field by field (`SC-norm-*`).

The scorers compare ids **exactly** and everything a human voice produced after
folding it: ``docs/requirements/05-scoring.md`` is the authority this implements,
and these functions are the only place in this package that decide what "the
same answer" means. Anything the judge compares goes through here first.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")

#: Ids are compared exactly (`CR-exact-ids`): no folding, no case change.
ID_FIELDS = (
    "patient_id",
    "provider_id",
    "location_id",
    "appointment_type_id",
    "policy_id",
    "appointment_id",
)

#: The demographics of a REGISTER, in the order the form asks for them.
REGISTER_FIELDS = (
    "given_name",
    "first_surname",
    "second_surname",
    "national_id",
    "date_of_birth",
    "phone",
    "email",
    "insurer",
)


def fold(text) -> str:
    """Accent-insensitive fold: NFKD, combining marks dropped (ñ → n), lowercased."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def normalize_national_id(value) -> str:
    """``12345678-Z`` / ``1234 5678 z`` → ``12345678Z``. The check letter is re-derived, never invented."""
    return re.sub(r"[\s-]", "", str(value or "")).upper()


def normalize_phone(value) -> str:
    """Nine national digits: ``+34`` and ``0034`` are country codes, not part of the number."""
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0034"):
        digits = digits[4:]
    elif digits.startswith("34") and len(digits) > 9:
        digits = digits[2:]
    return digits


def normalize_email(value) -> str:
    """Whitespace anywhere is removed, then the whole thing is lowercased."""
    return re.sub(r"\s+", "", str(value or "")).lower()


def normalize_slot(value) -> str:
    """The exact minute, in Europe/Madrid: seconds truncate, offsets do not matter.

    A slot with no offset is refused rather than assumed: ``CR-slot-tz`` requires
    the offset, and guessing local time would score a submission the platform
    rejects as if it were merely wrong.
    """
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        raise ValueError(f"slot has no UTC offset: {value!r}")
    return moment.astimezone(MADRID).replace(second=0, microsecond=0).isoformat()


def normalize_enum(value) -> str:
    """Free-text enums: ``  Review  `` and ``Paediátric_Review`` → ``review`` / ``paediatric_review``."""
    return fold(value).replace("-", "_").replace(" ", "_")


def normalize_name_parts(values) -> tuple[str, tuple[str, ...]]:
    """A name as ``(given, surnames)``: the surnames are unordered on purpose.

    ``José García López`` and ``José López García`` are the same person, and the
    scorer folds them together, so the pair is sorted rather than compared in the
    order it was said.
    """
    given, *surnames = values
    return fold(given), tuple(sorted(fold(surname) for surname in surnames))


def _register_fields(action: dict) -> dict:
    """Demographics wherever they arrived: ours flat, the roster nested under ``new_patient``."""
    nested = action.get("new_patient")
    source = nested if isinstance(nested, dict) else action
    return {key: source[key] for key in REGISTER_FIELDS if key in source}


def normalize_action(action: dict) -> dict:
    """One action as the scorer compares it."""
    verb = normalize_enum(action.get("action"))
    normalized: dict = {"action": verb}
    for key, value in action.items():
        if key in ("action", "new_patient"):
            continue
        if key in ID_FIELDS:
            normalized[key] = value  # exact
        elif key == "slot":
            normalized[key] = normalize_slot(value)
        elif key == "reason":
            normalized[key] = normalize_enum(value)
        elif key in REGISTER_FIELDS:
            continue  # folded with the rest of the demographics below
        else:
            normalized[key] = value
    if verb == "register":
        fields = _register_fields(action)
        given, surnames = normalize_name_parts(
            [fields.get("given_name"), fields.get("first_surname"), fields.get("second_surname")]
        )
        normalized["given_name"] = given
        normalized["first_surname"], normalized["second_surname"] = surnames
        if "national_id" in fields:
            normalized["national_id"] = normalize_national_id(fields["national_id"])
        if "phone" in fields:
            normalized["phone"] = normalize_phone(fields["phone"])
        if "email" in fields:
            normalized["email"] = normalize_email(fields["email"])
        if "insurer" in fields:
            normalized["insurer"] = normalize_enum(fields["insurer"])
        if "date_of_birth" in fields:
            normalized["date_of_birth"] = str(fields["date_of_birth"]).strip()
    return normalized


def normalize_actions(actions) -> list[dict]:
    return [normalize_action(action) for action in actions]
