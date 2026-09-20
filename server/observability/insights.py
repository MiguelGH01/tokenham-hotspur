"""Post-call insight extraction via TypeSafe Jev (Choice per definition).

Clinic staff define classifications (name + natural-language question + discrete
values). When a call ends, each definition becomes one Choice question over the
transcript — one System One request per insight, never batched.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Protocol

from loguru import logger

from observability.events import make_event

NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_INSIGHTS = 20
MAX_VALUES = 32
MAX_DESCRIPTION = 500


class InsightEmitter(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class InsightStore(Protocol):
    async def list_insight_defs(self) -> list[dict[str, Any]]: ...

    async def transcript_turns(self, call_id: str) -> list[dict[str, str]]: ...

    async def _get_call_row(self, call_id: str) -> Any: ...


def validate_insight_body(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return (normalized, None) or (None, error message naming the field)."""
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "name: required"
    name = name.strip()
    if not NAME_PATTERN.match(name):
        return None, "name: must match [A-Za-z0-9._-]{1,64}"

    description = body.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, "description: required"
    description = description.strip()
    if len(description) > MAX_DESCRIPTION:
        return None, f"description: at most {MAX_DESCRIPTION} characters"

    values = body.get("values")
    if not isinstance(values, list):
        return None, "values: must be a list"
    cleaned: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(values):
        if not isinstance(raw, str) or not raw.strip():
            return None, f"values[{i}]: empty"
        v = raw.strip()
        if not VALUE_PATTERN.match(v):
            return None, f"values[{i}]: must match [A-Za-z0-9._-]{{1,64}}"
        if v in seen:
            return None, f"values[{i}]: duplicate '{v}'"
        seen.add(v)
        cleaned.append(v)
    if len(cleaned) < 2:
        return None, "values: need at least 2 options"
    if len(cleaned) > MAX_VALUES:
        return None, f"values: at most {MAX_VALUES} options"

    return {"name": name, "description": description, "values": cleaned}, None


def typesafe_configured() -> bool:
    return bool(os.getenv("TYPESAFE_API_KEY", "").strip())


def turns_from_events(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Final user/bot utterances, shaped for a Jev ``conversation`` state."""
    turns: list[dict[str, str]] = []
    for event in events:
        kind = event.get("kind")
        text = str((event.get("payload") or {}).get("text") or "").strip()
        if not text:
            continue
        if kind == "transcript.user":
            turns.append({"role": "user", "text": text})
        elif kind == "transcript.bot":
            turns.append({"role": "assistant", "text": text})
    return turns


async def load_insight_turns(call_id: str, store: InsightStore) -> list[dict[str, str]]:
    """Local SQLite transcript, then ElevenLabs ConvAI if the call never landed here."""
    try:
        turns = await store.transcript_turns(call_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Insight extract: local transcript {}: {}", call_id, exc)
        turns = []
    if turns:
        return turns

    from observability.elevenlabs_history import (
        conversation_to_events,
        elevenlabs_history_configured,
        get_elevenlabs_history,
    )

    if not elevenlabs_history_configured():
        return []

    history = get_elevenlabs_history()
    conv_id = None
    try:
        row = await store._get_call_row(call_id)
    except Exception:
        row = None
    if row is not None:
        try:
            conv_id = row["eleven_conversation_id"]
        except (KeyError, IndexError, TypeError):
            conv_id = None
    conv_id = conv_id or history.conversation_id_for(call_id)
    if not conv_id and str(call_id).startswith("conv_"):
        conv_id = call_id
    if not conv_id:
        return []
    try:
        raw = await history._ensure_client().get_conversation(str(conv_id))
    except Exception as exc:
        logger.warning("Insight extract: ElevenLabs transcript {}: {}", call_id, exc)
        return []
    return turns_from_events(conversation_to_events(raw, call_id))


async def extract_call_insights(
    call_id: str,
    *,
    store: InsightStore,
    emitter: InsightEmitter,
    client: Any | None = None,
    turns: list[dict[str, str]] | None = None,
    defs: list[dict[str, Any]] | None = None,
) -> str:
    """Run one Jev Choice per insight definition; emit pending/extracted/failed.

    Never raises into the call path — extraction is fire-and-forget after hang-up.
    Returns a status token: ``ok``, ``skipped_eval``, ``no_defs``,
    ``unconfigured``, or ``no_transcript``.

    Pass ``defs`` to classify only those labels (new-insight backfill);
    otherwise every stored definition runs.
    """
    try:
        row = await store._get_call_row(call_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Insight extract: cannot read call {}: {}", call_id, exc)
        row = None

    transport = (row["transport"] if row else None) or "unknown"
    if transport == "eval":
        return "skipped_eval"

    if defs is None:
        try:
            defs = await store.list_insight_defs()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Insight extract: cannot list defs: {}", exc)
            return "no_defs"
    if not defs:
        return "no_defs"

    if turns is None:
        turns = await load_insight_turns(call_id, store)

    for d in defs:
        await emitter.emit(
            make_event(
                "insight.pending",
                call_id,
                insight_id=d["id"],
                name=d["name"],
                description=d["description"],
            )
        )

    if not typesafe_configured() and client is None:
        for d in defs:
            await emitter.emit(
                make_event(
                    "insight.failed",
                    call_id,
                    insight_id=d["id"],
                    name=d["name"],
                    error="typesafe_unconfigured",
                )
            )
        return "unconfigured"

    if not turns:
        for d in defs:
            await emitter.emit(
                make_event(
                    "insight.failed",
                    call_id,
                    insight_id=d["id"],
                    name=d["name"],
                    error="transcript_unavailable",
                )
            )
        return "no_transcript"

    state = {"conversation": turns}
    own_client = client is None
    if client is None:
        from clients.typesafe_client import TypeSafeInsightClient

        client = TypeSafeInsightClient()

    try:
        await asyncio.gather(
            *[
                _extract_one(call_id, d, state, client, emitter)
                for d in defs
            ]
        )
    finally:
        if own_client and hasattr(client, "aclose"):
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                pass
    return "ok"


async def _extract_one(
    call_id: str,
    definition: dict[str, Any],
    state: dict[str, Any],
    client: Any,
    emitter: InsightEmitter,
) -> None:
    name = definition["name"]
    insight_id = definition["id"]
    try:
        result = await client.classify_choice(
            state=state,
            question_id=name,
            instructions=definition["description"],
            values=list(definition["values"]),
        )
        await emitter.emit(
            make_event(
                "insight.extracted",
                call_id,
                insight_id=insight_id,
                name=name,
                description=definition["description"],
                choice=result["choice"],
                probabilities=result["probabilities"],
                confidence=result["confidence"],
            )
        )
    except Exception as exc:
        logger.warning(
            "Insight {} failed for {}: {}", name, call_id, exc
        )
        await emitter.emit(
            make_event(
                "insight.failed",
                call_id,
                insight_id=insight_id,
                name=name,
                error=str(exc)[:200] or "extraction_failed",
            )
        )


def schedule_extract(
    call_id: str,
    *,
    store: InsightStore,
    emitter: InsightEmitter,
    client: Any | None = None,
) -> asyncio.Task:
    """Fire extraction without blocking hang-up teardown."""
    return asyncio.create_task(
        extract_call_insights(call_id, store=store, emitter=emitter, client=client),
        name=f"insights:{call_id}",
    )


# ------------------------------------------------------------------ backfill (new insight → every past conversation)


_SKIPPED_STATUSES = frozenset(
    {"skipped_eval", "no_defs", "no_transcript", "unconfigured"}
)

_backfill_jobs: list[dict[str, Any]] = []
_backfill_state: dict[str, Any] | None = None
_backfill_task: asyncio.Task | None = None


def eligible_backfill_row(row: dict[str, Any]) -> bool:
    """Ended, non-eval conversations the console would actually show."""
    if (row.get("transport") or "") == "eval":
        return False
    if (row.get("status") or "") == "live":
        return False
    cid = str(row.get("call_id") or "")
    if cid.startswith("conv_"):
        return True
    if row.get("eleven_conversation_id"):
        return True
    if cid.startswith("CA-"):
        return True
    return False


async def collect_backfill_call_ids(store: Any) -> list[str]:
    """Local store plus ElevenLabs history, de-duplicated by conversation."""
    from observability.elevenlabs_history import (
        elevenlabs_history_configured,
        get_elevenlabs_history,
    )

    if elevenlabs_history_configured():
        try:
            rows = await get_elevenlabs_history().list_console_calls(
                store, since=None, include="all"
            )
        except Exception as exc:
            logger.warning("Insight backfill: ElevenLabs list failed: {}", exc)
            rows = await store.list_calls(since=None, include="all")
    else:
        rows = await store.list_calls(since=None, include="all")

    ids: list[str] = []
    seen: set[str] = set()
    seen_el: set[str] = set()
    for row in rows:
        if not eligible_backfill_row(row):
            continue
        cid = str(row.get("call_id") or "")
        el_id = str(row.get("eleven_conversation_id") or "")
        if cid.startswith("conv_"):
            el_id = el_id or cid
        if el_id:
            if el_id in seen_el:
                continue
            seen_el.add(el_id)
        if not cid or cid in seen:
            continue
        seen.add(cid)
        ids.append(cid)
    return ids


def backfill_snapshot() -> dict[str, Any] | None:
    if _backfill_state is None:
        return None
    snap = dict(_backfill_state)
    snap["queued"] = [str(j["definition"]["name"]) for j in _backfill_jobs]
    return snap


def reset_insight_backfill() -> None:
    """Drop queued/running backfills (tests, hub reset)."""
    global _backfill_task, _backfill_state
    task = _backfill_task
    _backfill_task = None
    _backfill_jobs.clear()
    _backfill_state = None
    if task is not None and not task.done():
        task.cancel()


def drop_insight_backfill(insight_id: int) -> None:
    """Forget queued jobs for a deleted definition; the runner notices mid-sweep."""
    _backfill_jobs[:] = [
        job for job in _backfill_jobs if job["definition"].get("id") != insight_id
    ]


def schedule_insight_backfill(
    definition: dict[str, Any],
    *,
    store: Any,
    hub: Any,
    client: Any | None = None,
) -> asyncio.Task:
    """Classify one new insight across existing conversations, without blocking create."""
    global _backfill_task
    _backfill_jobs.append(
        {
            "definition": definition,
            "store": store,
            "hub": hub,
            "client": client,
        }
    )
    if _backfill_task is None or _backfill_task.done():
        _backfill_task = asyncio.create_task(
            _backfill_worker(), name="insights-backfill"
        )
    return _backfill_task


async def _backfill_worker() -> None:
    global _backfill_state
    while _backfill_jobs:
        job = _backfill_jobs.pop(0)
        try:
            await run_insight_backfill(
                job["definition"],
                store=job["store"],
                hub=job["hub"],
                client=job["client"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Insight backfill worker failed: {}", exc)
            if _backfill_state is not None:
                _backfill_state = {
                    **_backfill_state,
                    "status": "failed",
                    "error": str(exc)[:200],
                }
                await _publish_backfill(job["hub"])


async def run_insight_backfill(
    definition: dict[str, Any],
    *,
    store: Any,
    hub: Any,
    client: Any | None = None,
) -> dict[str, Any]:
    """Walk every eligible conversation and run Jev for ``definition`` only."""
    global _backfill_state
    _backfill_state = {
        "insight_id": definition.get("id"),
        "name": definition["name"],
        "status": "running",
        "total": 0,
        "done": 0,
        "ok": 0,
        "skipped": 0,
        "failed": 0,
    }
    await _publish_backfill(hub)

    ids = await collect_backfill_call_ids(store)
    _backfill_state["total"] = len(ids)
    await _publish_backfill(hub)

    own_client = client is None
    if client is None and ids and typesafe_configured():
        from clients.typesafe_client import TypeSafeInsightClient

        client = TypeSafeInsightClient()
    try:
        for cid in ids:
            current = await store.get_insight_def(definition["id"])
            if current is None:
                _backfill_state["status"] = "cancelled"
                await _publish_backfill(hub)
                return _backfill_state
            try:
                status = await extract_call_insights(
                    cid,
                    store=store,
                    emitter=hub,
                    client=client,
                    defs=[definition],
                )
            except Exception as exc:
                logger.warning("Insight backfill {} failed for {}: {}", definition["name"], cid, exc)
                status = "failed"
            _backfill_state["done"] += 1
            if status == "ok":
                _backfill_state["ok"] += 1
            elif status in _SKIPPED_STATUSES:
                _backfill_state["skipped"] += 1
            else:
                _backfill_state["failed"] += 1
            await _publish_backfill(hub)
    finally:
        if own_client and client is not None and hasattr(client, "aclose"):
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                pass

    if _backfill_state.get("status") == "running":
        _backfill_state["status"] = "done"
        await _publish_backfill(hub)
    return _backfill_state


async def _publish_backfill(hub: Any) -> None:
    notice = getattr(hub, "broadcast_notice", None)
    if callable(notice) and _backfill_state is not None:
        try:
            await notice("insight.backfill", **dict(_backfill_state))
        except Exception:  # pragma: no cover - defensive
            pass
