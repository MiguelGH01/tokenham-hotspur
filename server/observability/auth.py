"""Demo auth for the centralita console: admin key or provider id."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from clinic_catalog import load_catalog
from rules import provider_by_id

COOKIE_NAME = "arenal_session"
_DEFAULT_SECRET = "arenal-console-demo"


def _secret() -> bytes:
    return (os.getenv("CONSOLE_AUTH_SECRET") or _DEFAULT_SECRET).encode("utf-8")


def provider_public(provider: dict) -> dict[str, Any]:
    """Fields the schedule UI needs — nothing sensitive beyond the catalogue."""
    return {
        "id": provider["id"],
        "name": provider["name"],
        "specialty_id": provider["specialty_id"],
        "specialty_name": provider["specialty_name"],
        "languages": list(provider.get("languages") or []),
        "appointment_type_names": list(provider.get("appointment_type_names") or []),
        "location_names": list(provider.get("location_names") or []),
        "schedules": provider.get("schedules") or [],
        "accepted_insurers": provider.get("accepted_insurers") or [],
        "refused_insurers": provider.get("refused_insurers") or [],
        "leave": provider.get("leave"),
    }


def resolve_key(key: str) -> dict[str, Any] | None:
    """Map a login key to a session payload, or None if the key is unknown."""
    raw = (key or "").strip()
    if not raw:
        return None
    if raw.lower() == "admin":
        return {"role": "admin"}
    provider_id = raw.upper()
    provider = provider_by_id(load_catalog(), provider_id)
    if provider is None:
        return None
    return {"role": "provider", "id": provider["id"]}


def session_to_cookie(session: dict[str, Any]) -> str:
    """HMAC-signed cookie value (stdlib only)."""
    payload = base64.urlsafe_b64encode(
        json.dumps(session, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def cookie_to_session(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    role = data.get("role")
    if role == "admin":
        return {"role": "admin"}
    if role == "provider" and isinstance(data.get("id"), str):
        provider = provider_by_id(load_catalog(), data["id"])
        if provider is None:
            return None
        return {"role": "provider", "id": provider["id"]}
    return None


def session_response(session: dict[str, Any]) -> dict[str, Any]:
    """JSON body for /auth/login and /auth/me."""
    if session["role"] == "admin":
        return {"role": "admin"}
    provider = provider_by_id(load_catalog(), session["id"])
    if provider is None:
        raise KeyError(session["id"])
    return {"role": "provider", "provider": provider_public(provider)}


def is_admin(session: dict[str, Any] | None) -> bool:
    return session is not None and session.get("role") == "admin"
