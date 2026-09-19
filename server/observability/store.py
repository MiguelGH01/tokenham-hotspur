"""SQLite persistence for the centralita console (WAL, local-only)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from booking import MADRID
from observability.events import CallSnapshot, ObsEvent, PROTOCOL_NODES, utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    transport TEXT NOT NULL DEFAULT 'unknown',
    from_number TEXT,
    status TEXT NOT NULL DEFAULT 'live',
    current_node TEXT,
    patient_name TEXT,
    patient_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER,
    first_word_ms INTEGER,
    primary_action TEXT,
    primary_reason TEXT,
    submitted INTEGER NOT NULL DEFAULT 0,
    failed_posts INTEGER NOT NULL DEFAULT 0,
    last_justification TEXT,
    is_test INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (call_id) REFERENCES calls(call_id)
);

CREATE INDEX IF NOT EXISTS idx_events_call_ts ON events(call_id, ts);
CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    verb TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    http_status INTEGER,
    reason TEXT,
    summary TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (call_id) REFERENCES calls(call_id),
    UNIQUE(call_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_actions_call ON actions(call_id);
"""


def default_db_path() -> Path:
    override = os.getenv("OBSERVABILITY_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data" / "centralita.sqlite"


def shift_start_iso(*, now: datetime | None = None) -> str:
    """Today's shift start in Europe/Madrid, returned as UTC ISO."""
    from datetime import timezone

    local = (now or datetime.now(MADRID)).astimezone(MADRID)
    hour = int(os.getenv("SHIFT_START_HOUR", "8"))
    start = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _ms_between(start: str, end: str) -> int:
    return int((_parse_ts(end) - _parse_ts(start)).total_seconds() * 1000)


def _percentile(sorted_vals: list[int], p: float) -> int | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (len(sorted_vals) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return int(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


class ObservabilityStore:
    """Async SQLite store. One writer (the hub); REST readers share the file."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self._db: aiosqlite.Connection | None = None

    async def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cur = await self.db.execute("PRAGMA table_info(calls)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "is_test" not in cols:
            await self.db.execute(
                "ALTER TABLE calls ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0"
            )
            await self.db.commit()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await self._migrate()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ObservabilityStore is not open")
        return self._db

    async def clear(self) -> None:
        await self.db.execute("DELETE FROM events")
        await self.db.execute("DELETE FROM actions")
        await self.db.execute("DELETE FROM calls")
        await self.db.commit()

    async def upsert_call_started(
        self,
        call_id: str,
        *,
        transport: str,
        from_number: str | None,
        started_at: str,
        is_test: bool = False,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO calls (call_id, transport, from_number, started_at, status, is_test)
            VALUES (?, ?, ?, ?, 'live', ?)
            ON CONFLICT(call_id) DO UPDATE SET
                transport = excluded.transport,
                from_number = COALESCE(excluded.from_number, calls.from_number),
                status = 'live',
                ended_at = NULL,
                duration_ms = NULL,
                is_test = excluded.is_test
            """,
            (call_id, transport, from_number, started_at, 1 if is_test else 0),
        )
        await self.db.commit()

    async def append_event(self, event: ObsEvent) -> None:
        await self.db.execute(
            "INSERT INTO events (call_id, kind, ts, payload_json) VALUES (?, ?, ?, ?)",
            (
                event["call_id"],
                event["kind"],
                event["ts"],
                json.dumps(event.get("payload") or {}, ensure_ascii=False),
            ),
        )
        await self._apply_projections(event)
        await self.db.commit()

    async def _apply_projections(self, event: ObsEvent) -> None:
        call_id = event["call_id"]
        kind = event["kind"]
        payload = event.get("payload") or {}
        ts = event["ts"]

        # Ensure a stub call row exists for late events.
        await self.db.execute(
            """
            INSERT OR IGNORE INTO calls (call_id, transport, started_at, status)
            VALUES (?, 'unknown', ?, 'live')
            """,
            (call_id, ts),
        )

        if kind == "call.ended":
            row = await self._get_call_row(call_id)
            duration = None
            if row and row["started_at"]:
                duration = _ms_between(row["started_at"], ts)
            await self.db.execute(
                """
                UPDATE calls SET status = 'ended', ended_at = ?, duration_ms = ?
                WHERE call_id = ?
                """,
                (ts, duration, call_id),
            )
        elif kind == "node.entered":
            node = payload.get("to") or payload.get("node")
            if node:
                await self.db.execute(
                    "UPDATE calls SET current_node = ? WHERE call_id = ?",
                    (node, call_id),
                )
        elif kind == "tool.returned":
            if payload.get("justification"):
                await self.db.execute(
                    "UPDATE calls SET last_justification = ? WHERE call_id = ?",
                    (payload["justification"], call_id),
                )
            if payload.get("next_node"):
                await self.db.execute(
                    "UPDATE calls SET current_node = ? WHERE call_id = ?",
                    (payload["next_node"], call_id),
                )
        elif kind == "state.patched":
            fields: list[str] = []
            values: list[Any] = []
            for key, col in (
                ("patient_name", "patient_name"),
                ("patient_id", "patient_id"),
                ("current_node", "current_node"),
                ("pending_action", "primary_action"),
            ):
                if key in payload:
                    fields.append(f"{col} = ?")
                    values.append(payload[key])
            if fields:
                values.append(call_id)
                await self.db.execute(
                    f"UPDATE calls SET {', '.join(fields)} WHERE call_id = ?",
                    values,
                )
        elif kind == "action.queued":
            verb = payload.get("action") or payload.get("verb")
            if not verb:
                return
            seq = payload.get("seq")
            if seq is None:
                cur = await self.db.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM actions WHERE call_id = ?",
                    (call_id,),
                )
                row = await cur.fetchone()
                seq = int(row["n"]) if row else 1
            await self.db.execute(
                """
                INSERT INTO actions (call_id, seq, verb, status, reason, summary, payload_json, created_at)
                VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                ON CONFLICT(call_id, seq) DO UPDATE SET
                    verb = excluded.verb,
                    reason = excluded.reason,
                    summary = excluded.summary,
                    payload_json = excluded.payload_json
                """,
                (
                    call_id,
                    seq,
                    verb,
                    payload.get("reason"),
                    payload.get("summary"),
                    json.dumps(payload.get("payload") or payload, ensure_ascii=False),
                    ts,
                ),
            )
            # Primary action = first queued verb for the call.
            row = await self._get_call_row(call_id)
            if row and not row["primary_action"]:
                await self.db.execute(
                    """
                    UPDATE calls SET primary_action = ?, primary_reason = ?
                    WHERE call_id = ? AND primary_action IS NULL
                    """,
                    (verb, payload.get("reason"), call_id),
                )
        elif kind == "submit.posted":
            verb = payload.get("action") or payload.get("verb")
            http_status = payload.get("http_status")
            ok = payload.get("ok")
            if ok is None:
                ok = http_status is not None and 200 <= int(http_status) < 300
            status = "posted" if ok else "failed"
            if verb:
                # Update the oldest matching queued/failed row, or insert.
                cur = await self.db.execute(
                    """
                    SELECT id FROM actions
                    WHERE call_id = ? AND verb = ? AND status IN ('queued', 'failed')
                    ORDER BY seq ASC LIMIT 1
                    """,
                    (call_id, verb),
                )
                existing = await cur.fetchone()
                if existing:
                    await self.db.execute(
                        """
                        UPDATE actions SET status = ?, http_status = ?, reason = COALESCE(?, reason)
                        WHERE id = ?
                        """,
                        (status, http_status, payload.get("reason"), existing["id"]),
                    )
                else:
                    cur = await self.db.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM actions WHERE call_id = ?",
                        (call_id,),
                    )
                    row = await cur.fetchone()
                    seq = int(row["n"]) if row else 1
                    await self.db.execute(
                        """
                        INSERT INTO actions
                        (call_id, seq, verb, status, http_status, reason, summary, payload_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            call_id,
                            seq,
                            verb,
                            status,
                            http_status,
                            payload.get("reason"),
                            payload.get("summary"),
                            json.dumps(payload, ensure_ascii=False),
                            ts,
                        ),
                    )
            if ok:
                await self.db.execute(
                    "UPDATE calls SET submitted = 1 WHERE call_id = ?",
                    (call_id,),
                )
                row = await self._get_call_row(call_id)
                if row is not None and not row["primary_action"] and verb:
                    await self.db.execute(
                        "UPDATE calls SET primary_action = ?, primary_reason = ? WHERE call_id = ?",
                        (verb, payload.get("reason"), call_id),
                    )
            else:
                await self.db.execute(
                    "UPDATE calls SET failed_posts = failed_posts + 1 WHERE call_id = ?",
                    (call_id,),
                )
        elif kind == "metrics.first_word":
            ms = payload.get("ms") or payload.get("first_word_ms")
            if ms is not None:
                await self.db.execute(
                    "UPDATE calls SET first_word_ms = ? WHERE call_id = ? AND first_word_ms IS NULL",
                    (int(ms), call_id),
                )

    async def _get_call_row(self, call_id: str) -> aiosqlite.Row | None:
        cur = await self.db.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,))
        return await cur.fetchone()

    async def list_calls(
        self, *, since: str | None = None, include: str = "all"
    ) -> list[dict[str, Any]]:
        rows = await self._list_call_rows(since=since)
        if include == "real":
            rows = [r for r in rows if not r["is_test"]]
        elif include == "test":
            rows = [r for r in rows if r["is_test"]]
        return [self._call_summary(r) for r in rows]

    async def _list_call_rows(self, *, since: str | None = None):
        if since:
            cur = await self.db.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM events e WHERE e.call_id = c.call_id) AS event_count
                FROM calls c
                WHERE c.started_at >= ?
                ORDER BY c.started_at DESC
                """,
                (since,),
            )
        else:
            cur = await self.db.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM events e WHERE e.call_id = c.call_id) AS event_count
                FROM calls c
                ORDER BY c.started_at DESC
                """
            )
        return await cur.fetchall()

    async def get_call(self, call_id: str) -> dict[str, Any] | None:
        row = await self._get_call_row(call_id)
        if not row:
            return None
        events = await self.list_events(call_id)
        actions = await self.list_actions(call_id)
        summary = self._call_summary(row)
        return {
            "call": {
                "call_id": summary["call_id"],
                "transport": summary["transport"],
                "from_number": summary.get("from_number"),
                "opened_at": summary["started_at"],
                "elapsed_ms": summary.get("duration_ms")
                or (
                    _ms_between(summary["started_at"], utc_now_iso())
                    if summary["status"] == "live"
                    else None
                ),
                "ended_at": summary.get("ended_at"),
                "node": summary.get("current_node"),
                "patient_name": summary.get("patient_name"),
                "patient_id": summary.get("patient_id"),
                "status": summary["status"],
                "primary_action": summary.get("primary_action"),
                "primary_reason": summary.get("primary_reason"),
                "last_justification": summary.get("last_justification"),
                "first_word_ms": summary.get("first_word_ms"),
                "submitted": summary.get("submitted"),
                "failed_posts": summary.get("failed_posts"),
            },
            "timeline": project_timeline(events),
            "decision_trail": project_decision_trail(events),
            "actions": actions,
            "events": events,
        }

    async def list_events(self, call_id: str) -> list[ObsEvent]:
        cur = await self.db.execute(
            "SELECT kind, call_id, ts, payload_json FROM events WHERE call_id = ? ORDER BY id ASC",
            (call_id,),
        )
        rows = await cur.fetchall()
        return [
            {
                "kind": r["kind"],  # type: ignore[typeddict-item]
                "call_id": r["call_id"],
                "ts": r["ts"],
                "payload": json.loads(r["payload_json"] or "{}"),
            }
            for r in rows
        ]

    async def list_actions(self, call_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            """
            SELECT seq, verb, status, http_status, reason, summary, payload_json, created_at
            FROM actions WHERE call_id = ? ORDER BY seq ASC
            """,
            (call_id,),
        )
        rows = await cur.fetchall()
        return [
            {
                "seq": r["seq"],
                "verb": r["verb"],
                "status": r["status"],
                "http_status": r["http_status"],
                "reason": r["reason"],
                "summary": r["summary"],
                "payload": json.loads(r["payload_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def load_live_snapshots(self) -> list[CallSnapshot]:
        """Hydrate in-memory hub with currently-live calls and their events."""
        cur = await self.db.execute(
            "SELECT * FROM calls WHERE status = 'live' ORDER BY started_at ASC"
        )
        rows = await cur.fetchall()
        snaps: list[CallSnapshot] = []
        for row in rows:
            events = await self.list_events(row["call_id"])
            snaps.append(
                {
                    "call_id": row["call_id"],
                    "transport": row["transport"] or "unknown",  # type: ignore[typeddict-item]
                    "from_number": row["from_number"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "current_node": row["current_node"],
                    "patient_name": row["patient_name"],
                    "patient_id": row["patient_id"],
                    "pending_action": row["primary_action"],
                    "primary_action": row["primary_action"],
                    "primary_reason": row["primary_reason"],
                    "last_justification": row["last_justification"],
                    "status": "live",
                    "duration_ms": row["duration_ms"],
                    "first_word_ms": row["first_word_ms"],
                    "submitted": bool(row["submitted"]),
                    "failed_posts": row["failed_posts"] or 0,
                    "events": events,
                }
            )
        return snaps

    async def shift_summary(self, *, since: str | None = None) -> dict[str, Any]:
        since = since or shift_start_iso()
        now = utc_now_iso()
        cur = await self.db.execute(
            "SELECT * FROM calls WHERE started_at >= ? AND COALESCE(is_test, 0) = 0",
            (since,),
        )
        calls = await cur.fetchall()
        call_ids = [c["call_id"] for c in calls]
        live = sum(1 for c in calls if c["status"] == "live")
        total = len(calls)
        submitted = sum(1 for c in calls if c["submitted"])
        durations = sorted(c["duration_ms"] for c in calls if c["duration_ms"] is not None)
        first_words = sorted(c["first_word_ms"] for c in calls if c["first_word_ms"] is not None)

        # Hourly buckets in Europe/Madrid.
        start_local = _parse_ts(since).astimezone(MADRID)
        now_local = datetime.now(MADRID)
        hourly: list[dict[str, int]] = []
        hour = start_local.replace(minute=0, second=0, microsecond=0)
        busiest_hour: int | None = None
        busiest_count = -1
        while hour <= now_local:
            h = hour.hour
            count = 0
            for c in calls:
                started = _parse_ts(c["started_at"]).astimezone(MADRID)
                if started.year == hour.year and started.month == hour.month and started.day == hour.day and started.hour == h:
                    count += 1
            hourly.append({"hour": h, "count": count})
            if count > busiest_count:
                busiest_count = count
                busiest_hour = h
            hour += timedelta(hours=1)

        # Primary-action mix.
        mix_counts: dict[str, int] = {}
        for c in calls:
            action = c["primary_action"] or "NO_ACTION"
            mix_counts[action] = mix_counts.get(action, 0) + 1
        mix_total = sum(mix_counts.values()) or 1
        order = ["BOOK", "REGISTER", "RESCHEDULE", "CANCEL", "NO_ACTION", "ESCALATE"]
        mix = [
            {
                "action": a,
                "count": mix_counts.get(a, 0),
                "pct": round(mix_counts.get(a, 0) / mix_total, 3),
            }
            for a in order
            if mix_counts.get(a, 0) > 0
        ]
        for a, n in mix_counts.items():
            if a not in order:
                mix.append({"action": a, "count": n, "pct": round(n / mix_total, 3)})

        cur = await self.db.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'posted' THEN 1 ELSE 0 END) AS posted,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM actions
            WHERE call_id IN (SELECT call_id FROM calls WHERE started_at >= ?)
            """,
            (since,),
        )
        act = await cur.fetchone()
        posted = int(act["posted"] or 0) if act else 0
        failed = int(act["failed"] or 0) if act else 0

        cur = await self.db.execute(
            """
            SELECT verb, COUNT(*) AS n FROM actions
            WHERE call_id IN (SELECT call_id FROM calls WHERE started_at >= ?)
            GROUP BY verb ORDER BY n DESC, verb
            """,
            (since,),
        )
        by_verb = [{"verb": r["verb"], "count": int(r["n"])} for r in await cur.fetchall()]

        return {
            "clinic": os.getenv("CLINIC_NAME", "Clínica Arenal"),
            "timezone": "Europe/Madrid",
            "shift_start": since,
            "now": now,
            "live_calls": live,
            "calls": total,
            "busiest_hour": busiest_hour,
            "hourly": hourly,
            "submit": {
                "posted_calls": submitted,
                "total_calls": total,
                "rate": (submitted / total) if total else 1.0,
            },
            "duration": {
                "median_ms": _percentile(durations, 0.5),
                "p95_ms": _percentile(durations, 0.95),
            },
            "first_word": {
                "p50_ms": _percentile(first_words, 0.5),
                "p95_ms": _percentile(first_words, 0.95),
            },
            "length_buckets": _length_buckets(durations),
            "funnel": await self._funnel(call_ids, calls),
            "reasons": _reason_counts(calls),
            "tools": await self._tool_counts(call_ids),
            "actions": {"posted": posted, "failed": failed, "by_verb": by_verb},
            "mix": mix,
            "protocol_nodes": list(PROTOCOL_NODES),
        }

    async def _tool_counts(self, call_ids: list[str]) -> list[dict[str, Any]]:
        """How often each tool was called across the shift."""
        if not call_ids:
            return []
        marks = ",".join("?" for _ in call_ids)
        cur = await self.db.execute(
            f"SELECT payload_json FROM events "
            f"WHERE kind = 'tool.called' AND call_id IN ({marks})",
            call_ids,
        )
        counts: dict[str, int] = {}
        for row in await cur.fetchall():
            name = (json.loads(row["payload_json"] or "{}")).get("name")
            if name:
                counts[name] = counts.get(name, 0) + 1
        return [
            {"tool": t, "count": n}
            for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    async def _funnel(self, call_ids: list[str], calls: list[Any]) -> list[dict[str, Any]]:
        """Stage counts, with the coded reasons of the calls that stopped there.

        Stages are read off node.entered and the compiled action, so a
        drop-off always reconciles against a reason the code actually set.
        """
        visited = await _visited_nodes(self.db, call_ids)
        by_id = {c["call_id"]: c for c in calls}

        def reached_identify(cid: str) -> bool:
            return bool(visited.get(cid, set()) - {"reception"})

        def identified(cid: str) -> bool:
            return bool(by_id[cid]["patient_id"])

        def offered(cid: str) -> bool:
            return bool(visited.get(cid, set()) & _OFFER_NODES)

        def committed(cid: str) -> bool:
            return (by_id[cid]["primary_action"] or "") in _COMMITTED_ACTIONS

        stages = [
            ("Call connected", lambda cid: True),
            ("Intent captured", reached_identify),
            ("Patient identified", identified),
            ("Offer read back", offered),
            ("Action committed", committed),
        ]
        out: list[dict[str, Any]] = []
        prev: list[str] | None = None
        for label, pred in stages:
            # A funnel stage is reached only by calls that reached the one
            # before it, so the counts are nested and can only decrease.
            pool = call_ids if prev is None else prev
            here = [cid for cid in pool if pred(cid)]
            entry: dict[str, Any] = {"label": label, "count": len(here)}
            if prev is not None:
                lost = [cid for cid in prev if cid not in set(here)]
                entry["dropped"] = len(lost)
                reasons: dict[str, int] = {}
                for cid in lost:
                    reason = by_id[cid]["primary_reason"] or "no coded reason"
                    reasons[reason] = reasons.get(reason, 0) + 1
                entry["drop_reasons"] = [
                    {"reason": r, "count": n}
                    for r, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
                ]
            out.append(entry)
            prev = here
        return out

    def _call_summary(self, row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "call_id": row["call_id"],
            "transport": row["transport"],
            "from_number": row["from_number"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "current_node": row["current_node"],
            "patient_name": row["patient_name"],
            "patient_id": row["patient_id"],
            "pending_action": row["primary_action"],
            "primary_action": row["primary_action"],
            "primary_reason": row["primary_reason"],
            "last_justification": row["last_justification"],
            "status": row["status"],
            "duration_ms": row["duration_ms"],
            "first_word_ms": row["first_word_ms"],
            "submitted": bool(row["submitted"]),
            "failed_posts": row["failed_posts"] or 0,
            "is_test": bool(row["is_test"]) if "is_test" in row.keys() else False,
            "event_count": row["event_count"] if "event_count" in row.keys() else None,
        }


def project_timeline(events: list[ObsEvent]) -> list[dict[str, Any]]:
    """Project a chronological UI timeline from raw events."""
    timeline: list[dict[str, Any]] = []
    pending_tool: dict[str, Any] | None = None
    for event in events:
        kind = event["kind"]
        payload = event.get("payload") or {}
        ts = event["ts"]
        if kind == "transcript.bot":
            timeline.append(
                {"type": "utterance", "role": "bot", "text": payload.get("text", ""), "ts": ts}
            )
        elif kind == "transcript.user":
            timeline.append(
                {"type": "utterance", "role": "user", "text": payload.get("text", ""), "ts": ts}
            )
        elif kind == "tool.called":
            pending_tool = {
                "type": "tool",
                "name": payload.get("name"),
                "args": payload.get("args") or {},
                "ts": ts,
            }
        elif kind == "tool.returned":
            item = pending_tool or {
                "type": "tool",
                "name": payload.get("name"),
                "args": {},
                "ts": ts,
            }
            item["status"] = payload.get("status")
            item["justification"] = payload.get("justification")
            item["reason_codes"] = payload.get("reason_codes") or []
            item["returned_at"] = ts
            timeline.append(item)
            pending_tool = None
        elif kind == "action.queued":
            timeline.append(
                {
                    "type": "action",
                    "verb": payload.get("action") or payload.get("verb"),
                    "status": "queued",
                    "summary": payload.get("summary"),
                    "reason": payload.get("reason"),
                    "ts": ts,
                }
            )
        elif kind == "submit.posted":
            timeline.append(
                {
                    "type": "submit",
                    "verb": payload.get("action") or payload.get("verb"),
                    "http_status": payload.get("http_status"),
                    "ok": payload.get("ok"),
                    "error": payload.get("error"),
                    "ts": ts,
                }
            )
        elif kind == "node.entered":
            timeline.append(
                {
                    "type": "node",
                    "from": payload.get("from"),
                    "to": payload.get("to") or payload.get("node"),
                    "ts": ts,
                }
            )
    if pending_tool:
        timeline.append(pending_tool)
    return timeline


def project_decision_trail(events: list[ObsEvent]) -> list[dict[str, Any]]:
    """Project decision-trail steps from node/tool events."""
    steps: list[dict[str, Any]] = []
    step_n = 0
    # Map node -> latest tool result for that transition.
    for event in events:
        kind = event["kind"]
        payload = event.get("payload") or {}
        if kind == "node.entered":
            step_n += 1
            steps.append(
                {
                    "step": step_n,
                    "node": payload.get("to") or payload.get("node"),
                    "from": payload.get("from"),
                    "status": "entry",
                    "reason_codes": [],
                    "justification": None,
                    "ts": event["ts"],
                }
            )
        elif kind == "tool.returned" and steps:
            # Annotate the latest step (or add a tool step).
            codes = payload.get("reason_codes") or []
            status = payload.get("status") or "ok"
            just = payload.get("justification")
            last = steps[-1]
            if last.get("status") == "entry" and not last.get("justification"):
                last["status"] = status
                last["reason_codes"] = codes
                last["justification"] = just
                last["tool"] = payload.get("name")
            else:
                step_n += 1
                steps.append(
                    {
                        "step": step_n,
                        "node": payload.get("from_node") or last.get("node"),
                        "status": status,
                        "reason_codes": codes,
                        "justification": just,
                        "tool": payload.get("name"),
                        "ts": event["ts"],
                    }
                )
    return steps


# Module-level singleton used by the hub / routes.
_store: ObservabilityStore | None = None


async def get_store(path: Path | str | None = None) -> ObservabilityStore:
    global _store
    if _store is None:
        _store = ObservabilityStore(path)
        await _store.open()
    return _store


async def reset_store(path: Path | str | None = None) -> ObservabilityStore:
    """Replace the singleton (tests). Closes the previous connection."""
    global _store
    if _store is not None:
        await _store.close()
    _store = ObservabilityStore(path)
    await _store.open()
    return _store


# ---------------------------------------------------------------------------
# Shift panels — every figure below is a projection of the call event log.
# ---------------------------------------------------------------------------

_LENGTH_EDGES_MS = [60_000, 120_000, 180_000, 240_000, 300_000]
_LENGTH_LABELS = ["<1m", "1–2m", "2–3m", "3–4m", "4–5m", "5m+"]

# A call reaching one of these nodes has read an offer back to the caller.
_OFFER_NODES = {"confirm", "cancel_confirm", "registration_confirm"}
_COMMITTED_ACTIONS = {"BOOK", "REGISTER", "RESCHEDULE", "CANCEL"}


def _length_buckets(durations_ms: list[int]) -> list[dict[str, Any]]:
    """Histogram of call length, marking the bucket that holds the median."""
    counts = [0] * len(_LENGTH_LABELS)
    for d in durations_ms:
        idx = len(_LENGTH_EDGES_MS)
        for i, edge in enumerate(_LENGTH_EDGES_MS):
            if d < edge:
                idx = i
                break
        counts[idx] += 1
    median = _percentile(sorted(durations_ms), 0.5)
    median_idx = None
    if median is not None:
        median_idx = len(_LENGTH_EDGES_MS)
        for i, edge in enumerate(_LENGTH_EDGES_MS):
            if median < edge:
                median_idx = i
                break
    return [
        {"label": label, "count": counts[i], "median": i == median_idx}
        for i, label in enumerate(_LENGTH_LABELS)
    ]


def _reason_counts(calls: list[Any]) -> list[dict[str, Any]]:
    """Coded reasons carried by the calls that did not commit an action."""
    counts: dict[str, int] = {}
    for c in calls:
        reason = c["primary_reason"]
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": r, "count": n}
        for r, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


async def _visited_nodes(db, call_ids: list[str]) -> dict[str, set[str]]:
    """Which nodes each call actually entered, straight from node.entered."""
    if not call_ids:
        return {}
    marks = ",".join("?" for _ in call_ids)
    cur = await db.execute(
        f"SELECT call_id, payload_json FROM events "
        f"WHERE kind = 'node.entered' AND call_id IN ({marks})",
        call_ids,
    )
    out: dict[str, set[str]] = {}
    for row in await cur.fetchall():
        payload = json.loads(row["payload_json"] or "{}")
        node = payload.get("to") or payload.get("node")
        if node:
            out.setdefault(row["call_id"], set()).add(node)
    return out
