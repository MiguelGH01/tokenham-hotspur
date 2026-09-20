"""Process-wide CallHub: live registry, SQLite persistence, WebSocket fan-out."""

from __future__ import annotations

import asyncio

from loguru import logger
from typing import Any

from observability.events import (
    PROTOCOL_NODES,
    CallSnapshot,
    ObsEvent,
    Transport,
    make_event,
    utc_now_iso,
)
from observability.store import ObservabilityStore, get_store, shift_start_iso


class CallHub:
    def __init__(self, store: ObservabilityStore | None = None) -> None:
        self._calls: dict[str, CallSnapshot] = {}
        self._order: list[str] = []
        self._subscribers: set[asyncio.Queue[ObsEvent | dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._store = store
        self._ready = asyncio.Event()
        self._shift_dirty = False
        self._shift_task: asyncio.Task | None = None

    @property
    def protocol_nodes(self) -> list[str]:
        return list(PROTOCOL_NODES)

    async def ensure_ready(self) -> ObservabilityStore:
        # A store closed underneath us (tests swap the singleton) must not be
        # handed back: every write on it would raise into the call path.
        if self._store is None or not self._store.is_open:
            self._store = await get_store()
            await self._hydrate_live()
            self._ready.set()
        elif not self._ready.is_set():
            await self._hydrate_live()
            self._ready.set()
        return self._store

    async def aclose(self) -> None:
        """Stop the background shift ticker and close the store.

        aiosqlite runs its connection on a non-daemon thread, so a script that
        leaves the store open never exits.
        """
        task = self._shift_task
        self._shift_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
        if self._store is not None and self._store.is_open:
            await self._store.close()

    async def _persist(self, write) -> None:
        """Persist, but never let observation break the call it observes."""
        try:
            await write()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Observability write dropped: {}", exc)

    async def _hydrate_live(self) -> None:
        assert self._store is not None
        snaps = await self._store.load_live_snapshots()
        async with self._lock:
            for snap in snaps:
                cid = snap["call_id"]
                self._calls[cid] = snap
                if cid not in self._order:
                    self._order.append(cid)

    async def start_call(
        self,
        call_id: str,
        *,
        transport: Transport = "unknown",
        from_number: str | None = None,
        is_test: bool = False,
    ) -> ObsEvent:
        store = await self.ensure_ready()
        event = make_event(
            "call.started",
            call_id,
            transport=transport,
            from_number=from_number,
            is_test=is_test,
        )
        async with self._lock:
            if call_id in self._calls:
                snap = self._calls[call_id]
                snap["status"] = "live"
                snap["ended_at"] = None
            else:
                self._order.append(call_id)
                self._calls[call_id] = {
                    "call_id": call_id,
                    "transport": transport,
                    "from_number": from_number,
                    "is_test": is_test,
                    "started_at": event["ts"],
                    "status": "live",
                    "current_node": None,
                    "patient_name": None,
                    "pending_action": None,
                    "last_justification": None,
                    "events": [],
                }
            self._append_locked(call_id, event)
        await self._persist(
            lambda: store.upsert_call_started(
                call_id,
                transport=transport,
                from_number=from_number,
                started_at=event["ts"],
                is_test=is_test,
            )
        )
        await self._persist(lambda: store.append_event(event))
        await self._broadcast(event)
        self._mark_shift_dirty()
        return event

    async def end_call(self, call_id: str) -> ObsEvent | None:
        store = await self.ensure_ready()
        async with self._lock:
            snap = self._calls.get(call_id)
            if snap and snap["status"] == "ended":
                return None
            # Also skip if DB already has it ended and we have no live snap.
            event = make_event("call.ended", call_id)
            if snap:
                snap["status"] = "ended"
                snap["ended_at"] = event["ts"]
                self._append_locked(call_id, event)
            elif call_id not in self._calls:
                # Ended without a live snapshot (restart mid-call).
                pass
        # Idempotent at DB: check status before writing a second end.
        try:
            row = await store._get_call_row(call_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Observability read dropped: {}", exc)
            row = None
        if row and row["status"] == "ended":
            return None
        await self._persist(lambda: store.append_event(event))
        await self._broadcast(event)
        self._mark_shift_dirty()
        # Fire-and-forget: hang-up must not wait on TypeSafe.
        try:
            from observability.insights import schedule_extract

            schedule_extract(call_id, store=store, emitter=self)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Insight schedule dropped for {}: {}", call_id, exc)
        return event

    async def emit(self, event: ObsEvent) -> None:
        store = await self.ensure_ready()
        async with self._lock:
            call_id = event["call_id"]
            if call_id not in self._calls:
                self._order.append(call_id)
                self._calls[call_id] = {
                    "call_id": call_id,
                    "transport": "unknown",
                    "started_at": event["ts"],
                    "status": "live",
                    "events": [],
                }
            self._apply_side_effects(event)
            self._append_locked(call_id, event)
        await self._persist(lambda: store.append_event(event))
        await self._broadcast(event)
        if event["kind"] in {
            "call.started",
            "call.ended",
            "action.queued",
            "submit.posted",
            "metrics.first_word",
        }:
            self._mark_shift_dirty()

    def list_calls(self) -> list[CallSnapshot]:
        return [self._public_snapshot(self._calls[cid]) for cid in reversed(self._order) if cid in self._calls]

    def get_call(self, call_id: str) -> CallSnapshot | None:
        snap = self._calls.get(call_id)
        return self._public_snapshot(snap) if snap else None

    def subscribe(self) -> asyncio.Queue[ObsEvent | dict[str, Any]]:
        queue: asyncio.Queue[ObsEvent | dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ObsEvent | dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def load_fixture_events(self, events: list[ObsEvent], *, clear: bool = True) -> int:
        """Load a fixture JSONL into the hub (for UI work without dialing)."""
        store = await self.ensure_ready()
        async with self._lock:
            if clear:
                self._calls.clear()
                self._order.clear()
        if clear:
            await store.clear()
        count = 0
        for event in events:
            kind = event["kind"]
            call_id = event["call_id"]
            payload = event.get("payload") or {}
            if kind == "call.started":
                # Preserve fixture timestamp.
                stamped = make_event(
                    "call.started",
                    call_id,
                    transport=payload.get("transport", "eval"),
                    from_number=payload.get("from_number"),
                )
                stamped["ts"] = event["ts"]
                async with self._lock:
                    if call_id not in self._calls:
                        self._order.append(call_id)
                    self._calls[call_id] = {
                        "call_id": call_id,
                        "transport": payload.get("transport", "eval"),
                        "from_number": payload.get("from_number"),
                        "started_at": event["ts"],
                        "status": "live",
                        "current_node": None,
                        "patient_name": None,
                        "pending_action": None,
                        "last_justification": None,
                        "events": [],
                    }
                    self._append_locked(call_id, stamped)
                await store.upsert_call_started(
                    call_id,
                    transport=payload.get("transport", "eval"),
                    from_number=payload.get("from_number"),
                    started_at=event["ts"],
                    is_test=bool(payload.get("is_test")),
                )
                await store.append_event(stamped)
                await self._broadcast(stamped)
            elif kind == "call.ended":
                stamped = make_event("call.ended", call_id)
                stamped["ts"] = event["ts"]
                async with self._lock:
                    snap = self._calls.get(call_id)
                    if snap:
                        snap["status"] = "ended"
                        snap["ended_at"] = event["ts"]
                        self._append_locked(call_id, stamped)
                await store.append_event(stamped)
                await self._broadcast(stamped)
                # Fixtures skip live Jev extraction; events may already include insights.
            else:
                stamped = dict(event)
                stamped["ts"] = event.get("ts") or utc_now_iso()
                await self.emit(stamped)  # type: ignore[arg-type]
            count += 1
        self._mark_shift_dirty()
        return count

    def _append_locked(self, call_id: str, event: ObsEvent) -> None:
        snap = self._calls[call_id]
        events = snap["events"]
        events.append(event)

    def _apply_side_effects(self, event: ObsEvent) -> None:
        snap = self._calls.get(event["call_id"])
        if not snap:
            return
        payload = event["payload"]
        kind = event["kind"]
        if kind == "node.entered":
            snap["current_node"] = payload.get("to") or payload.get("node")
        elif kind == "tool.returned":
            if payload.get("justification"):
                snap["last_justification"] = payload["justification"]
            if payload.get("next_node"):
                snap["current_node"] = payload["next_node"]
        elif kind == "state.patched":
            if "patient_name" in payload:
                snap["patient_name"] = payload["patient_name"]
            if "patient_id" in payload:
                snap["patient_id"] = payload["patient_id"]
            if "pending_action" in payload:
                snap["pending_action"] = payload["pending_action"]
                if not snap.get("primary_action"):
                    snap["primary_action"] = payload["pending_action"]
            if "current_node" in payload:
                snap["current_node"] = payload["current_node"]
        elif kind == "action.queued":
            verb = payload.get("action") or payload.get("verb")
            if verb:
                snap["pending_action"] = verb
                if not snap.get("primary_action"):
                    snap["primary_action"] = verb
                    snap["primary_reason"] = payload.get("reason")
        elif kind == "submit.posted":
            snap["pending_action"] = payload.get("action") or snap.get("pending_action")
            snap["submitted"] = True
        elif kind == "metrics.first_word":
            ms = payload.get("ms") or payload.get("first_word_ms")
            if ms is not None and snap.get("first_word_ms") is None:
                snap["first_word_ms"] = int(ms)

    def _public_snapshot(self, snap: CallSnapshot) -> CallSnapshot:
        return {
            **snap,
            "events": list(snap["events"]),
        }

    async def _broadcast(self, event: ObsEvent) -> None:
        dead: list[asyncio.Queue] = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:
                    dead.append(queue)
        for queue in dead:
            self._subscribers.discard(queue)

    def _mark_shift_dirty(self) -> None:
        self._shift_dirty = True
        if self._shift_task is None or self._shift_task.done():
            self._shift_task = asyncio.create_task(self._shift_tick_loop())

    async def _shift_tick_loop(self) -> None:
        """Debounce shift KPI broadcasts (~1s) for live WS clients."""
        await asyncio.sleep(1.0)
        if not self._shift_dirty:
            return
        self._shift_dirty = False
        try:
            store = await self.ensure_ready()
            summary = await store.shift_summary(since=shift_start_iso())
            tick = {"kind": "shift.tick", "payload": summary}
            dead: list[asyncio.Queue] = []
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(tick)  # type: ignore[arg-type]
                except asyncio.QueueFull:
                    dead.append(queue)
            for queue in dead:
                self._subscribers.discard(queue)
        except Exception:
            pass


_hub: CallHub | None = None


def get_hub() -> CallHub:
    global _hub
    if _hub is None:
        _hub = CallHub()
    return _hub


def reset_hub(store: ObservabilityStore | None = None) -> CallHub:
    """Replace the singleton (tests)."""
    global _hub
    _hub = CallHub(store=store)
    return _hub
