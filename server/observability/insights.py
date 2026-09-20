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
) -> str:
    """Run one Jev Choice per insight definition; emit pending/extracted/failed.

    Never raises into the call path — extraction is fire-and-forget after hang-up.
    Returns a status token: ``ok``, ``skipped_eval``, ``no_defs``,
    ``unconfigured``, or ``no_transcript``.
    """
    try:
        row = await store._get_call_row(call_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Insight extract: cannot read call {}: {}", call_id, exc)
        row = None

    transport = (row["transport"] if row else None) or "unknown"
    if transport == "eval":
        return "skipped_eval"

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
