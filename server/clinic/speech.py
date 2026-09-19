"""Recover site / named doctor from what the caller said, not from the LLM's tool args."""

from __future__ import annotations

import re
from datetime import date

from clinic.clinic_catalog import _name_tokens, fold, load_catalog, match_plan, match_providers
from dates import _MONTHS, _ORDINALS
from national_id import is_valid_national_id

_GREETING = re.compile(
    r"\b((good)\s+(morning|afternoon|evening)|(buenos|buenas)\s+(dias|días|tardes|noches))\b",
    re.IGNORECASE,
)
_NID = re.compile(r"\b([XYZ]\s*\d{7}\s*[A-Z]|\d{8}\s*[A-Z])\b", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE = re.compile(r"(?:\+34\s*)?((?:\d[\s.\-]*){9})")
_ISO_DOB = re.compile(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b")
_WANTS_REGISTER = re.compile(r"\b(register|registration|new patient|not on file|first time)\b", re.I)

_SPECIALTY_HINTS = (
    ("gynaecology", ("gynaecology", "gynecology", "gynaecolog", "ginecolog")),
    ("dermatology", ("dermatolog", "eczema", "mole")),
    ("orthopaedics", ("orthopaedic", "orthopedic", "hip")),
    ("paediatrics", ("paediatric", "pediatric")),
    ("physiotherapy", ("physio",)),
    ("general_practice", ("general practice", "general practitioner", "family doctor", " gp", "g.p")),
)


def greeting_stripped(text: str) -> str:
    return _GREETING.sub(" ", text or "")


def infer_site(text: str | None) -> str | None:
    if not text:
        return None
    raw = fold(text)
    hits = []
    for loc in load_catalog()["locations"]:
        if fold(loc["name"]) in raw or f" {loc['id']} " in f" {raw} ":
            hits.append(loc["id"])
    if len(hits) == 1:
        return hits[0]
    return None


def infer_provider_spoken(text: str | None, specialty_id: str | None = None) -> str | None:
    if not text:
        return None
    hits = match_providers(text, specialty_id)
    if len(hits) == 1:
        return hits[0]["name"]
    have = set(_name_tokens(text))
    surnames = []
    for provider in load_catalog()["providers"]:
        family = set(_name_tokens(provider["name"])[1:])
        if have & family:
            surnames.append(provider)
    if specialty_id:
        in_spec = [p for p in surnames if p["specialty_id"] == specialty_id]
        if in_spec:
            surnames = in_spec
    if len(surnames) == 1:
        return surnames[0]["name"]
    return None


def infer_specialty(text: str | None) -> str | None:
    if not text:
        return None
    raw = f" {fold(text)} "
    hits = [sid for sid, keys in _SPECIALTY_HINTS if any(k in raw for k in keys)]
    return hits[0] if len(hits) == 1 else None


def infer_dob(text: str | None) -> str | None:
    if not text:
        return None
    if iso := _ISO_DOB.search(text):
        return iso.group(1)
    raw = re.sub(r"\s+", " ", text.lower())
    year_m = re.search(r"\b((?:19|20)\d{2})\b", raw)
    month = next((n for name, n in _MONTHS.items() if name in raw), None)
    day = None
    for word, n in sorted(_ORDINALS.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{re.escape(word)}\b", raw) or re.search(
            rf"\b{re.escape(word.replace('-', ' '))}\b", raw
        ):
            day = n
            break
    if day is None:
        day_m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", raw)
        if day_m:
            day = int(day_m.group(1))
    if year_m is None or month is None or day is None:
        return None
    try:
        return date(int(year_m.group(1)), month, day).isoformat()
    except ValueError:
        return None


def parse_register(args: dict, spoken: str) -> dict:
    """Fill REGISTER fields from tool args, then from what the caller actually said."""
    nid = (args.get("national_id") or "").strip()
    if not is_valid_national_id(nid):
        found = _NID.search(spoken or "")
        nid = found.group(0) if found else nid
    phone = args.get("phone") or ""
    if len(re.sub(r"\D", "", phone)) < 9:
        found = _PHONE.search(spoken or "")
        phone = found.group(1) if found else phone
    email = (args.get("email") or "").strip()
    if "@" not in email:
        found = _EMAIL.search(spoken or "")
        email = found.group(0) if found else email
    dob = (args.get("date_of_birth") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
        dob = infer_dob(spoken) or dob
    insurer = match_plan(args.get("insurer") or "") or match_plan(spoken or "")
    return {
        "given_name": (args.get("given_name") or "").strip(),
        "first_surname": (args.get("first_surname") or "").strip(),
        "second_surname": (args.get("second_surname") or "").strip(),
        "national_id": nid,
        "date_of_birth": dob,
        "phone": phone,
        "email": email,
        "insurer": insurer or (args.get("insurer") or "").strip(),
    }


def register_complete(fields: dict) -> bool:
    needed = (
        "given_name",
        "first_surname",
        "second_surname",
        "national_id",
        "date_of_birth",
        "phone",
        "email",
        "insurer",
    )
    return all(fields.get(k) for k in needed) and is_valid_national_id(fields["national_id"])


def wants_register(text: str | None) -> bool:
    return bool(text and _WANTS_REGISTER.search(text))


def user_speech(flow_manager) -> str:
    chunks: list[str] = []
    if summary := (getattr(flow_manager, "state", {}) or {}).get("final_intent"):
        chunks.append(summary)
    agg = getattr(flow_manager, "_context_aggregator", None)
    if agg is not None:
        try:
            for message in agg.user()._context.get_messages():
                if message.get("role") == "user" and isinstance(message.get("content"), str):
                    chunks.append(message["content"])
        except Exception:
            pass
    return " ".join(chunks)
