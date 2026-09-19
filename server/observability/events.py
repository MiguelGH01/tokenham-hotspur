"""Typed observability events published by the bot into the CallHub."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, NotRequired, TypedDict

Transport = Literal["twilio", "webrtc", "daily", "eval", "unknown"]

EventKind = Literal[
    "call.started",
    "call.ended",
    "transcript.user",
    "transcript.bot",
    "transcript.user_interim",
    "node.entered",
    "tool.called",
    "tool.returned",
    "state.patched",
    "action.queued",
    "submit.posted",
    "metrics.first_word",
]

# Nodes the running bot actually uses (flows/*.py). Grow when the graph grows.
PROTOCOL_NODES: list[str] = [
    "reception",
    "identify",
    "find_slot",
    "confirm",
    "registration",
    "registration_confirm",
    "appointments",
    "cancel_confirm",
    "request_complete",
    "no_booking",
    "refused",
    "goodbye",
    "giveup",
]

ActionVerb = Literal[
    "BOOK",
    "REGISTER",
    "RESCHEDULE",
    "CANCEL",
    "NO_ACTION",
    "ESCALATE",
]


class ObsEvent(TypedDict):
    kind: EventKind
    call_id: str
    ts: str
    payload: dict[str, Any]


class CallSnapshot(TypedDict):
    call_id: str
    transport: Transport
    from_number: NotRequired[str | None]
    started_at: str
    ended_at: NotRequired[str | None]
    current_node: NotRequired[str | None]
    patient_name: NotRequired[str | None]
    patient_id: NotRequired[str | None]
    pending_action: NotRequired[str | None]
    primary_action: NotRequired[str | None]
    primary_reason: NotRequired[str | None]
    last_justification: NotRequired[str | None]
    status: Literal["live", "ended"]
    duration_ms: NotRequired[int | None]
    first_word_ms: NotRequired[int | None]
    submitted: NotRequired[bool]
    failed_posts: NotRequired[int]
    events: list[ObsEvent]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(kind: EventKind, call_id: str, **payload: Any) -> ObsEvent:
    return {"kind": kind, "call_id": call_id, "ts": utc_now_iso(), "payload": payload}


def mask_secret(value: str | None, *, keep: int = 2) -> str | None:
    if not value:
        return value
    if len(value) <= keep:
        return "•" * len(value)
    return ("•" * (len(value) - keep)) + value[-keep:]


def safe_tool_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Strip or mask PII from tool arguments before they hit the console."""
    if not args:
        return {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"id_value", "national_id", "phone"} and isinstance(value, str):
            out[key] = mask_secret(value)
        elif key in {"email"} and isinstance(value, str):
            out[key] = mask_secret(value, keep=4)
        else:
            out[key] = value
    return out


def safe_action_payload(action: dict[str, Any] | None) -> dict[str, Any]:
    """Mask PII fields on a queued/posted action for console storage."""
    if not action:
        return {}
    return safe_tool_args(action)
