"""ElevenLabs ConvAI conversation history as the source of truth for elevenagent.

When ``VOICE_AGENT=elevenagent``, the console's call list and transcript come from
``GET /v1/convai/conversations`` (and the per-conversation GET), including tool
calls and tool results. A short poll loop also hydrates the CallHub so the live
WebSocket stays in sync while a call is still open.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from observability.events import (
    ObsEvent,
    make_event,
    safe_tool_args,
    utc_now_iso,
)
from observability.store import ObservabilityStore, project_decision_trail, project_timeline

ELEVENLABS_API_BASE = "https://api.elevenlabs.io"
LIST_PAGE_SIZE = 100
LIST_PAGE_CAP = 15  # 1_500 conversations per period at most
POLL_SECS = 4.0
FINAL_FETCH_DELAY_SECS = 2.0

TOOL_TO_NODE: dict[str, str] = {
    "search-patient": "identify",
    "search_patient": "identify",
    "nearest-site": "find_slot",
    "nearest_site": "find_slot",
    "find_nearest_site": "find_slot",
    "book": "confirm",
    "reschedule": "confirm",
    "cancel": "cancel_confirm",
    "register": "registration",
    "no-action": "no_booking",
    "no_action": "no_booking",
    "escalate": "refused",
}

SUBMIT_VERBS: dict[str, str] = {
    "book": "BOOK",
    "submit_book": "BOOK",
    "register": "REGISTER",
    "submit_register": "REGISTER",
    "reschedule": "RESCHEDULE",
    "submit_reschedule": "RESCHEDULE",
    "cancel": "CANCEL",
    "submit_cancel": "CANCEL",
    "no-action": "NO_ACTION",
    "no_action": "NO_ACTION",
    "submit_no_action": "NO_ACTION",
    "escalate": "ESCALATE",
    "submit_escalate": "ESCALATE",
}

_SOURCE_TRANSPORT: dict[str, str] = {
    "twilio": "twilio",
    "exotel": "twilio",
    "sip_trunk": "twilio",
    "widget": "webrtc",
    "js_sdk": "webrtc",
    "python_sdk": "webrtc",
    "react_sdk": "webrtc",
    "node_js_sdk": "webrtc",
}

_SPEECH_KINDS = frozenset({"transcript.user", "transcript.bot"})
_TOOL_KINDS = frozenset({"tool.called", "tool.returned"})
_LOCAL_KEEP_KINDS = frozenset(
    {
        "node.entered",
        "state.patched",
        "action.queued",
        "submit.posted",
        "metrics.first_word",
        "insight.pending",
        "insight.extracted",
        "insight.failed",
    }
)


def elevenlabs_history_configured() -> bool:
    agent = (os.getenv("VOICE_AGENT") or "").strip().lower()
    return agent == "elevenagent" and bool(_api_key() and _agent_id())


def _api_key() -> str:
    return (os.getenv("ELEVENLABS_API_KEY") or "").strip()


def _agent_id() -> str:
    return (os.getenv("ELEVEN_AGENT_ID") or os.getenv("AGENT_ID") or "").strip()


def _iso_from_unix(seconds: int | float | None) -> str:
    if seconds is None:
        return utc_now_iso()
    return datetime.fromtimestamp(float(seconds), tz=UTC).isoformat()


def iso_to_unix(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _parse_jsonish(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": value}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {"value": value}


def _tool_node(name: str) -> str | None:
    return TOOL_TO_NODE.get(name) or TOOL_TO_NODE.get(name.replace("_", "-"))


def _submit_verb(name: str) -> str | None:
    return SUBMIT_VERBS.get(name) or SUBMIT_VERBS.get(name.replace("_", "-"))


def _transport_for(source: str | None) -> str:
    if not source:
        return "unknown"
    return _SOURCE_TRANSPORT.get(source, "unknown")


def _console_status(el_status: str | None) -> str:
    if el_status in {"done", "failed", "processing"}:
        return "ended"
    return "live"


def prosper_call_id_from(detail: dict[str, Any]) -> str | None:
    """Prosper / Twilio call id injected as the ConvAI ``call_id`` dynamic variable."""
    initiation = detail.get("conversation_initiation_client_data") or {}
    variables = initiation.get("dynamic_variables") or {}
    raw = variables.get("call_id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def caller_phone_from(detail: dict[str, Any]) -> str | None:
    initiation = detail.get("conversation_initiation_client_data") or {}
    variables = initiation.get("dynamic_variables") or {}
    phone = variables.get("caller_phone")
    if phone:
        return str(phone)
    metadata = detail.get("metadata") or {}
    phone_call = metadata.get("phone_call") or {}
    external = phone_call.get("external_number")
    return str(external) if external else None


def _fingerprint(event: ObsEvent) -> str:
    kind = event["kind"]
    payload = event.get("payload") or {}
    if kind in _SPEECH_KINDS:
        return f"{kind}:{(payload.get('text') or '').strip()}"
    if kind == "tool.called":
        request_id = payload.get("request_id")
        if request_id:
            return f"tool.called:{request_id}"
        name = payload.get("name") or ""
        args = json.dumps(payload.get("args") or {}, sort_keys=True, ensure_ascii=False)
        return f"tool.called:{name}:{args}"
    if kind == "tool.returned":
        request_id = payload.get("request_id")
        if request_id:
            return f"tool.returned:{request_id}"
        return f"tool.returned:{payload.get('name')}:{payload.get('status')}"
    if kind in {"call.started", "call.ended", "node.entered"}:
        extra = payload.get("to") or payload.get("conversation_id") or ""
        return f"{kind}:{extra}"
    return f"{kind}:{json.dumps(payload, sort_keys=True, default=str)}"


class ElevenLabsConvAI:
    """Thin REST client for ConvAI conversation list + detail."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        agent_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = ELEVENLABS_API_BASE,
    ) -> None:
        self.api_key = api_key if api_key is not None else _api_key()
        self.agent_id = agent_id if agent_id is not None else _agent_id()
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"xi-api-key": self.api_key, "accept": "application/json"},
            timeout=20.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_page(
        self,
        *,
        cursor: str | None = None,
        after_unix: int | None = None,
        page_size: int = LIST_PAGE_SIZE,
        summary_mode: str = "include",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page_size": min(max(page_size, 1), LIST_PAGE_SIZE),
            "summary_mode": summary_mode,
            "sort_direction": "desc",
        }
        if self.agent_id:
            params["agent_id"] = self.agent_id
        if cursor:
            params["cursor"] = cursor
        if after_unix is not None:
            params["call_start_after_unix"] = int(after_unix)
        response = await self._http.get("/v1/convai/conversations", params=params)
        response.raise_for_status()
        return response.json()

    async def list_conversations(
        self,
        *,
        after_unix: int | None = None,
        max_pages: int = LIST_PAGE_CAP,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            data = await self.list_page(cursor=cursor, after_unix=after_unix)
            batch = data.get("conversations") or []
            items.extend(batch)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return items

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        response = await self._http.get(f"/v1/convai/conversations/{conversation_id}")
        response.raise_for_status()
        return response.json()


def _tool_names_from_detail(detail: dict[str, Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for turn in detail.get("transcript") or []:
        for bucket in (turn.get("tool_calls"), turn.get("tool_results")):
            for call in bucket or []:
                name = call.get("tool_name")
                if name and str(name) not in seen:
                    seen.add(str(name))
                    names.append(str(name))
    return names


def as_list_item(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a list row or a conversation GET into the list-item shape."""
    if payload.get("start_time_unix_secs") is not None:
        item = dict(payload)
        if not item.get("tool_names"):
            item["tool_names"] = _tool_names_from_detail(payload)
        return item
    metadata = payload.get("metadata") or {}
    initiation = payload.get("conversation_initiation_client_data") or {}
    source = metadata.get("conversation_initiation_source") or (
        (initiation.get("source_info") or {}).get("source")
    )
    analysis = payload.get("analysis") or {}
    return {
        "conversation_id": payload.get("conversation_id"),
        "start_time_unix_secs": metadata.get("start_time_unix_secs"),
        "call_duration_secs": metadata.get("call_duration_secs"),
        "status": payload.get("status"),
        "tool_names": _tool_names_from_detail(payload),
        "conversation_initiation_source": source,
        "call_summary_title": payload.get("call_summary_title")
        or analysis.get("call_summary_title"),
        "termination_reason": metadata.get("termination_reason"),
        "message_count": len(payload.get("transcript") or []),
    }


def conversation_to_summary(
    item: dict[str, Any],
    *,
    call_id: str | None = None,
) -> dict[str, Any]:
    item = as_list_item(item)
    conv_id = str(item.get("conversation_id") or "")
    start = int(item.get("start_time_unix_secs") or 0)
    duration_secs = int(item.get("call_duration_secs") or 0)
    started_at = _iso_from_unix(start)
    ended_at = None
    status = _console_status(item.get("status"))
    if status == "ended" and start:
        ended_at = _iso_from_unix(start + duration_secs)
    tool_names = [str(n) for n in (item.get("tool_names") or []) if n]
    primary = None
    node = None
    for name in tool_names:
        verb = _submit_verb(name)
        if verb:
            primary = verb
        node = _tool_node(name) or node
    source = item.get("conversation_initiation_source")
    return {
        "call_id": call_id or conv_id,
        "eleven_conversation_id": conv_id,
        "transport": _transport_for(source),
        "from_number": None,
        "started_at": started_at,
        "ended_at": ended_at,
        "current_node": node,
        "patient_name": None,
        "patient_id": None,
        "pending_action": primary,
        "primary_action": primary,
        "primary_reason": None,
        "last_justification": item.get("termination_reason") or None,
        "status": status,
        "duration_ms": duration_secs * 1000 if duration_secs else None,
        "first_word_ms": None,
        "submitted": False,
        "failed_posts": 0,
        "is_test": False,
        "event_count": item.get("message_count"),
        "call_summary_title": item.get("call_summary_title") or None,
        "tool_names": tool_names,
        "source": "elevenlabs",
    }


def _looks_like_markup(text: str) -> bool:
    head = text.lstrip()[:80].lower()
    return head.startswith("<") or "<!doctype" in head or "<html" in head


def _tool_justification(result: dict[str, Any], name: str, status: str) -> str | None:
    if not (result.get("is_error") or status == "error"):
        return None
    err = str(result.get("error_type") or "error")
    raw = str(result.get("result_value") or result.get("raw_error_message") or "").strip()
    if not raw or _looks_like_markup(raw) or raw.startswith("{") or raw.startswith("["):
        return f"{name} failed ({err})"
    return raw.splitlines()[0][:200]


def conversation_to_events(detail: dict[str, Any], call_id: str) -> list[ObsEvent]:
    """Turn a ConvAI conversation GET payload into CallHub events."""
    metadata = detail.get("metadata") or {}
    start = int(metadata.get("start_time_unix_secs") or 0)
    duration_secs = int(metadata.get("call_duration_secs") or 0)
    source = metadata.get("conversation_initiation_source") or (
        (detail.get("conversation_initiation_client_data") or {})
        .get("source_info")
        or {}
    ).get("source")
    started_at = _iso_from_unix(start)
    events: list[ObsEvent] = []

    started = make_event(
        "call.started",
        call_id,
        transport=_transport_for(source),
        from_number=caller_phone_from(detail),
        is_test=False,
        conversation_id=detail.get("conversation_id"),
    )
    started["ts"] = started_at
    events.append(started)

    last_node: str | None = None
    pending_calls: set[str] = set()
    for turn in detail.get("transcript") or []:
        offset = turn.get("time_in_call_secs") or 0
        ts = _iso_from_unix(start + int(offset))
        role = turn.get("role")
        message = (turn.get("message") or "").strip()
        if role == "user" and message:
            event = make_event("transcript.user", call_id, text=message, final=True)
            event["ts"] = ts
            events.append(event)
        elif role == "agent" and message:
            event = make_event("transcript.bot", call_id, text=message, final=True)
            event["ts"] = ts
            events.append(event)

        for call in turn.get("tool_calls") or []:
            name = str(call.get("tool_name") or "")
            if not name:
                continue
            args = safe_tool_args(_parse_jsonish(call.get("params_as_json")))
            request_id = call.get("request_id")
            called = make_event(
                "tool.called",
                call_id,
                name=name,
                args=args,
                request_id=request_id,
                tool_type=call.get("type"),
            )
            called["ts"] = ts
            events.append(called)
            pending_calls.add(str(request_id or name))
            node = _tool_node(name)
            if node and node != last_node:
                entered = make_event("node.entered", call_id, to=node)
                entered["payload"]["from"] = last_node
                entered["ts"] = ts
                events.append(entered)
                last_node = node

        for result in turn.get("tool_results") or []:
            name = str(result.get("tool_name") or "")
            if not name:
                continue
            is_error = bool(result.get("is_error"))
            status = "error" if is_error else str(result.get("status") or "ok")
            if status in {"success"}:
                status = "ok"
            justification = _tool_justification(result, name, status)
            rid = str(result.get("request_id") or name)
            if rid not in pending_calls:
                called = make_event(
                    "tool.called",
                    call_id,
                    name=name,
                    args={},
                    request_id=result.get("request_id"),
                )
                called["ts"] = ts
                events.append(called)
            pending_calls.discard(rid)
            returned = make_event(
                "tool.returned",
                call_id,
                name=name,
                status=status,
                justification=justification,
                request_id=result.get("request_id"),
                next_node=_tool_node(name),
            )
            returned["ts"] = ts
            events.append(returned)

    if _console_status(detail.get("status")) == "ended":
        ended = make_event("call.ended", call_id)
        ended["ts"] = _iso_from_unix(start + duration_secs) if start else utc_now_iso()
        events.append(ended)
    return events


def _ts_epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_MATCH_START_SECS = 120.0
_MATCH_DURATION_MS = 8_000


def _looks_like_unbound_local(row: dict[str, Any]) -> bool:
    cid = str(row.get("call_id") or "")
    if not cid or cid.startswith("conv_"):
        return False
    if row.get("eleven_conversation_id"):
        return False
    if cid.startswith("CA-"):
        return True
    if row.get("is_test"):
        return True
    return (row.get("transport") or "") in {"webrtc", "unknown"}


def _pair_score(local_row: dict[str, Any], el_summary: dict[str, Any]) -> float | None:
    local_start = _ts_epoch(local_row.get("started_at"))
    el_start = _ts_epoch(el_summary.get("started_at"))
    if local_start is None or el_start is None:
        return None
    gap = abs(local_start - el_start)
    if gap > _MATCH_START_SECS:
        return None
    # A stub that never got hang-up stays "live" and its clock keeps running
    # (20 min vs the real 29 s). Start time is the signal; duration is not.
    if local_row.get("status") == "live" or el_summary.get("status") == "live":
        return gap
    local_dur = local_row.get("duration_ms")
    el_dur = el_summary.get("duration_ms")
    if local_dur and el_dur and abs(int(local_dur) - int(el_dur)) > _MATCH_DURATION_MS:
        return None
    return gap


def _overlay_local(summary: dict[str, Any], local_row: dict[str, Any]) -> dict[str, Any]:
    """Copy submit/patient fields; keep the ElevenLabs title and conversation id."""
    summary["_bound_call_id"] = local_row["call_id"]
    summary["submitted"] = bool(local_row.get("submitted"))
    summary["failed_posts"] = local_row.get("failed_posts") or 0
    summary["patient_name"] = local_row.get("patient_name") or summary.get("patient_name")
    summary["patient_id"] = local_row.get("patient_id")
    summary["primary_action"] = local_row.get("primary_action") or summary.get(
        "primary_action"
    )
    summary["pending_action"] = summary["primary_action"]
    summary["primary_reason"] = local_row.get("primary_reason")
    summary["is_test"] = bool(local_row.get("is_test") or summary.get("is_test"))
    summary["from_number"] = local_row.get("from_number") or summary.get("from_number")
    transport = local_row.get("transport")
    if transport and transport != "unknown":
        summary["transport"] = transport
    if local_row.get("first_word_ms") is not None:
        summary["first_word_ms"] = local_row["first_word_ms"]
    if local_row.get("current_node"):
        summary["current_node"] = local_row["current_node"]
    return summary


def merge_summaries(
    local: list[dict[str, Any]],
    eleven: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay local submit/patient fields onto ElevenLabs conversation rows.

    A Place-test-call creates a local ``CA-webrtc-…`` row and ElevenLabs a
    ``conv_…`` row. If ``eleven.bound`` missed, they still collapse when start
    time and duration line up — the ElevenLabs title is the one we keep.
    """
    local_by_el: dict[str, dict[str, Any]] = {}
    local_by_id: dict[str, dict[str, Any]] = {}
    for row in local:
        local_by_id[row["call_id"]] = row
        el_id = row.get("eleven_conversation_id")
        if el_id:
            local_by_el[str(el_id)] = row

    used_local: set[str] = set()
    merged: list[dict[str, Any]] = []
    for item in eleven:
        conv_id = str(item.get("conversation_id") or "")
        local_row = local_by_el.get(conv_id) or local_by_id.get(conv_id)
        summary = conversation_to_summary(
            item, call_id=local_row["call_id"] if local_row else conv_id
        )
        if local_row:
            used_local.add(local_row["call_id"])
            _overlay_local(summary, local_row)
        merged.append(summary)

    unbound = [
        row for row in local if row["call_id"] not in used_local and _looks_like_unbound_local(row)
    ]
    for local_row in unbound:
        best_i: int | None = None
        best_score: float | None = None
        for i, summary in enumerate(merged):
            if summary.get("_bound_call_id"):
                continue
            conv_id = summary.get("eleven_conversation_id")
            if not conv_id or summary["call_id"] != conv_id:
                continue
            score = _pair_score(local_row, summary)
            if score is None:
                continue
            if best_score is None or score < best_score:
                best_score = score
                best_i = i
        if best_i is None:
            continue
        used_local.add(local_row["call_id"])
        _overlay_local(merged[best_i], local_row)

    merged_el_ids = {
        str(s.get("eleven_conversation_id"))
        for s in merged
        if s.get("eleven_conversation_id")
    }
    merged_ids = {s["call_id"] for s in merged}

    for row in local:
        cid = row["call_id"]
        if cid in used_local or cid in merged_ids:
            continue
        # Poller opened a second row under the ConvAI id; the titled line
        # already represents that conversation.
        if cid in merged_el_ids:
            continue
        if row.get("status") == "live":
            ghost = False
            for summary in merged:
                if _pair_score(row, summary) is not None:
                    ghost = True
                    break
            if ghost:
                continue
        merged.append(row)

    merged.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return merged


async def match_local_call(store: ObservabilityStore, detail: dict[str, Any]) -> str | None:
    """Find the Place-test-call / sidecar row that belongs to this ConvAI conversation."""
    conv_id = str(detail.get("conversation_id") or "")
    if conv_id:
        mapped = await store.call_id_for_eleven_conversation(conv_id)
        if mapped:
            return mapped
    prosper = prosper_call_id_from(detail)
    if prosper:
        row = await store._get_call_row(prosper)
        if row:
            return prosper
    metadata = detail.get("metadata") or {}
    start = int(metadata.get("start_time_unix_secs") or 0)
    duration_secs = int(metadata.get("call_duration_secs") or 0)
    if not start:
        return None
    el_summary = {
        "started_at": _iso_from_unix(start),
        "duration_ms": duration_secs * 1000 if duration_secs else None,
    }
    since = _iso_from_unix(max(0, start - int(_MATCH_START_SECS)))
    locals_ = await store.list_calls(since=since, include="all")
    best_id: str | None = None
    best_score: float | None = None
    for row in locals_:
        if not _looks_like_unbound_local(row):
            continue
        score = _pair_score(row, el_summary)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best_score = score
            best_id = row["call_id"]
    return best_id


def _events_from_detail_payload(detail: dict[str, Any]) -> list[ObsEvent]:
    return list(detail.get("events") or [])


def merge_detail(
    local: dict[str, Any] | None,
    el_events: list[ObsEvent],
    *,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Prefer ElevenLabs speech + tools; keep local submits, nodes, insights."""
    local_events = _events_from_detail_payload(local) if local else []
    el_speech_tools = [
        e for e in el_events if e["kind"] in _SPEECH_KINDS or e["kind"] in _TOOL_KINDS
    ]
    el_nodes = [e for e in el_events if e["kind"] == "node.entered"]
    local_keep = [e for e in local_events if e["kind"] in _LOCAL_KEEP_KINDS]
    local_nodes = [e for e in local_events if e["kind"] == "node.entered"]
    bookends = [e for e in el_events if e["kind"] in {"call.started", "call.ended"}]
    if not bookends and local_events:
        bookends = [e for e in local_events if e["kind"] in {"call.started", "call.ended"}]

    nodes = local_nodes if local_nodes else el_nodes
    events = bookends + el_speech_tools + nodes + local_keep
    events.sort(key=lambda e: e.get("ts") or "")

    call = dict((local or {}).get("call") or {})
    call.update(
        {
            "call_id": summary["call_id"],
            "transport": summary.get("transport") or call.get("transport") or "unknown",
            "from_number": summary.get("from_number") or call.get("from_number"),
            "opened_at": summary.get("started_at") or call.get("opened_at"),
            "ended_at": summary.get("ended_at") or call.get("ended_at"),
            "elapsed_ms": summary.get("duration_ms") or call.get("elapsed_ms"),
            "node": summary.get("current_node") or call.get("node"),
            "patient_name": summary.get("patient_name") or call.get("patient_name"),
            "patient_id": summary.get("patient_id") or call.get("patient_id"),
            "status": summary.get("status") or call.get("status") or "ended",
            "primary_action": summary.get("primary_action") or call.get("primary_action"),
            "primary_reason": summary.get("primary_reason") or call.get("primary_reason"),
            "last_justification": summary.get("last_justification")
            or call.get("last_justification"),
            "first_word_ms": summary.get("first_word_ms")
            if summary.get("first_word_ms") is not None
            else call.get("first_word_ms"),
            "submitted": bool(summary.get("submitted") or call.get("submitted")),
            "failed_posts": summary.get("failed_posts")
            if summary.get("failed_posts") is not None
            else call.get("failed_posts") or 0,
        }
    )
    actions = list((local or {}).get("actions") or [])
    return {
        "call": call,
        "timeline": project_timeline(events),
        "decision_trail": project_decision_trail(events),
        "actions": actions,
        "events": events,
    }


def detail_from_events(events: list[ObsEvent], summary: dict[str, Any]) -> dict[str, Any]:
    return merge_detail(None, events, summary=summary)


class ElevenLabsHistory:
    """List/detail for the console plus a live poller that feeds the CallHub."""

    def __init__(self, client: ElevenLabsConvAI | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._task: asyncio.Task[None] | None = None
        self._final_tasks: dict[str, asyncio.Task[None]] = {}
        self._seen_counts: dict[str, int] = {}
        self._bindings: dict[str, str] = {}  # call_id -> conversation_id
        self._lock = asyncio.Lock()

    def _ensure_client(self) -> ElevenLabsConvAI:
        if self._client is None:
            self._client = ElevenLabsConvAI()
        return self._client

    def bind(self, call_id: str, conversation_id: str) -> None:
        if call_id and conversation_id:
            self._bindings[call_id] = conversation_id

    def conversation_id_for(self, call_id: str) -> str | None:
        return self._bindings.get(call_id)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop(), name="elevenlabs-history-poll")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for task in list(self._final_tasks.values()):
            task.cancel()
        self._final_tasks.clear()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def list_console_calls(
        self,
        store: ObservabilityStore,
        *,
        since: str | None,
        include: str = "all",
    ) -> list[dict[str, Any]]:
        local = await store.list_calls(since=since, include="all")
        try:
            # ConvAI history is the switchboard source. Do not clip it to the
            # 08:00 shift bound — overnight and pre-shift calls are exactly
            # what the ElevenLabs dashboard is showing.
            eleven = await self._ensure_client().list_conversations()
        except Exception as exc:
            logger.warning("ElevenLabs conversation list failed; using local store: {}", exc)
            return _filter_include(local, include)
        logger.info("ElevenLabs listed {} conversations for the console", len(eleven))
        merged = merge_summaries(local, eleven)
        merged_el_ids = {
            str(s.get("eleven_conversation_id"))
            for s in merged
            if s.get("eleven_conversation_id")
        }
        merged_ids = {s["call_id"] for s in merged}
        for row in local:
            cid = str(row.get("call_id") or "")
            if row.get("status") != "live":
                continue
            if cid in merged_ids:
                continue
            ghost = cid in merged_el_ids
            if not ghost:
                ghost = any(_pair_score(row, summary) is not None for summary in merged)
            if not ghost:
                continue
            try:
                from observability.hub import get_hub

                await get_hub().end_call(cid)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("elevenlabs ghost hangup skipped: {}", exc)
        for row in merged:
            bound = row.pop("_bound_call_id", None)
            conv_id = row.get("eleven_conversation_id")
            if bound and conv_id and bound != conv_id:
                try:
                    await store.set_eleven_conversation_id(str(bound), str(conv_id))
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("elevenlabs bind heal skipped: {}", exc)
        return _filter_include(merged, include)

    async def get_console_call(
        self, store: ObservabilityStore, call_id: str
    ) -> dict[str, Any] | None:
        local = await store.get_call(call_id)
        if local is None:
            mapped = await store.call_id_for_eleven_conversation(call_id)
            if mapped:
                call_id = mapped
                local = await store.get_call(mapped)
        conv_id = None
        if local:
            conv_id = (local.get("call") or {}).get("eleven_conversation_id")
        conv_id = (
            conv_id
            or self._bindings.get(call_id)
            or (call_id if _looks_like_conversation_id(call_id) else None)
        )
        if not conv_id and local is None:
            conv_id = call_id
        if not conv_id:
            return local
        try:
            raw = await self._ensure_client().get_conversation(str(conv_id))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return local
            logger.warning("ElevenLabs conversation GET failed for {}: {}", conv_id, exc)
            return local
        except Exception as exc:
            logger.warning("ElevenLabs conversation GET failed for {}: {}", conv_id, exc)
            return local

        resolved = (
            prosper_call_id_from(raw)
            or (local["call"]["call_id"] if local else None)
            or str(raw.get("conversation_id") or call_id)
        )
        events = conversation_to_events(raw, resolved)
        summary = conversation_to_summary(raw, call_id=resolved)
        if local:
            local_call = local.get("call") or {}
            summary["submitted"] = bool(local_call.get("submitted"))
            summary["failed_posts"] = local_call.get("failed_posts") or 0
            summary["patient_name"] = local_call.get("patient_name")
            summary["patient_id"] = local_call.get("patient_id")
            summary["primary_action"] = local_call.get("primary_action") or summary.get(
                "primary_action"
            )
            summary["primary_reason"] = local_call.get("primary_reason")
            summary["from_number"] = local_call.get("from_number") or summary.get("from_number")
            summary["first_word_ms"] = local_call.get("first_word_ms")
            if local_call.get("transport") and local_call["transport"] != "unknown":
                summary["transport"] = local_call["transport"]
        return merge_detail(local, events, summary=summary)

    def schedule_final_fetch(self, call_id: str, conversation_id: str | None = None) -> None:
        conv_id = conversation_id or self._bindings.get(call_id)
        if not conv_id:
            return
        existing = self._final_tasks.get(call_id)
        if existing and not existing.done():
            return

        async def _run() -> None:
            try:
                await asyncio.sleep(FINAL_FETCH_DELAY_SECS)
                await self._ingest_conversation(conv_id, preferred_call_id=call_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ElevenLabs final fetch failed for {}: {}", call_id, exc)

        self._final_tasks[call_id] = asyncio.create_task(_run(), name=f"eleven-final-{call_id}")

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.sync_recent()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ElevenLabs history poll failed: {}", exc)
            await asyncio.sleep(POLL_SECS)

    async def sync_recent(self) -> None:
        client = self._ensure_client()
        items = await client.list_conversations(max_pages=1)
        for item in items:
            conv_id = str(item.get("conversation_id") or "")
            if not conv_id:
                continue
            count = int(item.get("message_count") or 0)
            status = item.get("status") or ""
            live = status in {"initiated", "in-progress"}
            bound = conv_id in self._bindings.values()
            finishing = bound and status in {"processing", "done", "failed"}
            if not live and not finishing:
                continue
            prev = self._seen_counts.get(conv_id)
            if prev == count and not live:
                continue
            await self._ingest_conversation(conv_id)
            self._seen_counts[conv_id] = count

    async def _ingest_conversation(
        self, conversation_id: str, *, preferred_call_id: str | None = None
    ) -> None:
        from observability.hub import get_hub

        raw = await self._ensure_client().get_conversation(conversation_id)
        hub = get_hub()
        store = await hub.ensure_ready()
        call_id = (
            preferred_call_id
            or prosper_call_id_from(raw)
            or await store.call_id_for_eleven_conversation(conversation_id)
            or await match_local_call(store, raw)
            or str(raw.get("conversation_id") or conversation_id)
        )
        self._bindings[call_id] = conversation_id
        events = conversation_to_events(raw, call_id)
        await store.set_eleven_conversation_id(call_id, conversation_id)
        existing = await store.list_events(call_id)
        seen = {_fingerprint(e) for e in existing}
        has_started = any(e["kind"] == "call.started" for e in existing)
        for event in events:
            kind = event["kind"]
            if kind == "call.started" and has_started:
                continue
            if kind == "call.ended" and any(e["kind"] == "call.ended" for e in existing):
                continue
            if _fingerprint(event) in seen:
                continue
            if kind == "call.started":
                payload = event.get("payload") or {}
                await hub.start_call(
                    call_id,
                    transport=payload.get("transport") or "unknown",  # type: ignore[arg-type]
                    from_number=payload.get("from_number"),
                    is_test=bool(payload.get("is_test")),
                )
                has_started = True
                seen.add(_fingerprint(event))
                continue
            if kind == "call.ended":
                await hub.end_call(call_id)
                seen.add(_fingerprint(event))
                continue
            await hub.emit(event)
            seen.add(_fingerprint(event))
        if call_id != conversation_id:
            ghost = await store._get_call_row(conversation_id)
            if ghost and ghost["status"] == "live":
                await hub.end_call(conversation_id)


def _filter_include(rows: list[dict[str, Any]], include: str) -> list[dict[str, Any]]:
    if include == "real":
        return [r for r in rows if not r.get("is_test")]
    if include == "test":
        return [r for r in rows if r.get("is_test")]
    return rows


def _looks_like_conversation_id(value: str) -> bool:
    return str(value).startswith("conv_")


_history: ElevenLabsHistory | None = None


def get_elevenlabs_history() -> ElevenLabsHistory:
    global _history
    if _history is None:
        _history = ElevenLabsHistory()
    return _history


def reset_elevenlabs_history(history: ElevenLabsHistory | None = None) -> ElevenLabsHistory | None:
    global _history
    _history = history
    return _history
