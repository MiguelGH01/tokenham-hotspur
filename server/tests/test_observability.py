"""Observability store + hub: shift KPIs and conversation detail."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from booking import MADRID
from observability.hub import reset_hub
from observability.seed_shift import build_shift_events
from observability.store import (
    ObservabilityStore,
    period_start_iso,
    project_decision_trail,
    project_timeline,
    reset_store,
)

FIXTURES = Path(__file__).resolve().parents[1] / "observability" / "fixtures"


def _load_jsonl(name: str) -> list[dict]:
    path = FIXTURES / f"{name}.jsonl"
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        events.append(json.loads(line))
    return events


async def _hub(tmp_path):
    db = tmp_path / "centralita.sqlite"
    store = await reset_store(db)
    h = reset_hub(store)
    await h.ensure_ready()
    return h, store


def test_nuria_fixture_detail_and_actions(tmp_path):
    async def run():
        hub, store = await _hub(tmp_path)
        events = _load_jsonl("nuria_cancel_book")
        count = await hub.load_fixture_events(events, clear=True)
        assert count == len(events)

        detail = await store.get_call("CA9723d3")
        assert detail is not None
        call = detail["call"]
        assert call["patient_name"] == "Nuria Bel Aparici"
        assert call["status"] == "ended"
        assert call["primary_action"] == "CANCEL"
        assert call["submitted"] is True
        assert call["first_word_ms"] == 780

        actions = detail["actions"]
        assert len(actions) == 2
        assert [a["verb"] for a in actions] == ["CANCEL", "BOOK"]
        assert all(a["status"] == "posted" for a in actions)
        assert all(a["http_status"] == 200 for a in actions)

        timeline = detail["timeline"]
        types = [t["type"] for t in timeline]
        assert "utterance" in types
        assert "tool" in types
        assert "action" in types
        assert "submit" in types

        trail = detail["decision_trail"]
        assert trail
        assert trail[0]["node"] == "reception"
        nodes = [s["node"] for s in trail]
        assert "identify" in nodes
        await store.close()

    asyncio.run(run())


def test_simple_booking_primary_book(tmp_path):
    async def run():
        hub, store = await _hub(tmp_path)
        events = _load_jsonl("simple_booking")
        await hub.load_fixture_events(events, clear=True)
        detail = await store.get_call("CA-fixture-amelia")
        assert detail is not None
        assert detail["call"]["primary_action"] == "BOOK"
        assert detail["call"]["submitted"] is True
        await store.close()

    asyncio.run(run())


def test_shift_summary_mix_and_percentiles(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_START_HOUR", "0")

    async def run():
        hub, store = await _hub(tmp_path)
        events = build_shift_events(n_calls=80, seed=7)
        await hub.load_fixture_events(events, clear=True)
        since = min(e["ts"] for e in events if e["kind"] == "call.started")
        summary = await store.shift_summary(since=since)

        assert summary["calls"] == 80
        assert summary["live_calls"] == 0
        assert summary["actions"]["posted"] + summary["actions"]["failed"] == 80
        assert summary["submit"]["posted_calls"] >= 70
        assert summary["duration"]["median_ms"] is not None
        assert summary["duration"]["p95_ms"] is not None
        assert summary["first_word"]["p50_ms"] is not None
        assert summary["hourly"]
        assert summary["busiest_hour"] is not None

        mix_actions = {m["action"] for m in summary["mix"]}
        assert "BOOK" in mix_actions
        assert sum(m["count"] for m in summary["mix"]) == 80
        await store.close()

    asyncio.run(run())


def test_live_call_counted(tmp_path):
    async def run():
        hub, store = await _hub(tmp_path)
        await hub.start_call("CA-live-1", transport="webrtc")
        summary = await store.shift_summary(period="all")
        assert summary["live_calls"] >= 1
        calls = await store.list_calls(since=None)
        assert any(c["call_id"] == "CA-live-1" and c["status"] == "live" for c in calls)

        await hub.end_call("CA-live-1")
        detail = await store.get_call("CA-live-1")
        assert detail is not None
        assert detail["call"]["status"] == "ended"
        await store.close()

    asyncio.run(run())


def test_timeline_and_trail_projection():
    events = _load_jsonl("nuria_cancel_book")
    timeline = project_timeline(events)  # type: ignore[arg-type]
    assert any(t["type"] == "submit" and t["verb"] == "BOOK" for t in timeline)
    trail = project_decision_trail(events)  # type: ignore[arg-type]
    assert any("FR-identify" in (s.get("reason_codes") or []) for s in trail)


def test_period_start_windows(monkeypatch):
    monkeypatch.setenv("SHIFT_START_HOUR", "8")
    now = datetime(2026, 9, 20, 15, 30, tzinfo=MADRID)
    today = datetime.fromisoformat(period_start_iso("today", now=now)).astimezone(MADRID)
    assert (today.year, today.month, today.day, today.hour) == (2026, 9, 20, 8)
    week = datetime.fromisoformat(period_start_iso("week", now=now)).astimezone(MADRID)
    assert week.date() == today.date() - timedelta(days=7)
    assert week.hour == 8
    month = datetime.fromisoformat(period_start_iso("month", now=now)).astimezone(MADRID)
    assert (month.year, month.month, month.day, month.hour) == (2026, 8, 20, 8)
    year = datetime.fromisoformat(period_start_iso("year", now=now)).astimezone(MADRID)
    assert (year.year, year.month, year.day, year.hour) == (2025, 9, 20, 8)
    assert period_start_iso("all", now=now) is None
    assert period_start_iso("nope", now=now) == period_start_iso("today", now=now)


def test_shift_summary_period_filters_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_START_HOUR", "0")

    async def run():
        hub, store = await _hub(tmp_path)
        await store.upsert_call_started(
            "CA-old",
            transport="eval",
            from_number=None,
            started_at=period_start_iso("month"),
        )
        await store.upsert_call_started(
            "CA-new",
            transport="eval",
            from_number=None,
            started_at=datetime.now(MADRID).isoformat(),
        )

        today = await store.shift_summary(period="today")
        assert today["calls"] == 1
        assert today["period"] == "today"
        assert today["volume"]["grain"] == "hour"

        month = await store.shift_summary(period="month")
        assert month["calls"] == 2
        assert month["volume"]["grain"] == "day"

        week = await store.shift_summary(period="week")
        assert week["calls"] == 1

        all_time = await store.shift_summary(period="all")
        assert all_time["calls"] == 2
        assert all_time["shift_start"] is None
        await store.close()

    asyncio.run(run())


def test_migrates_eleven_conversation_id_on_existing_db(tmp_path):
    """An older centralita.sqlite has no eleven_conversation_id; open() must ALTER it.

    CREATE TABLE IF NOT EXISTS cannot add columns, and an index on the new
    column in the bootstrap script would fail before _migrate could run.
    """
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE calls (
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
        )
        """
    )
    conn.execute(
        "INSERT INTO calls (call_id, started_at, status) VALUES ('CA-old', '2026-09-20T08:00:00+00:00', 'ended')"
    )
    conn.commit()
    conn.close()

    async def run():
        store = ObservabilityStore(path)
        await store.open()
        row = await store._get_call_row("CA-old")
        assert row is not None
        assert "eleven_conversation_id" in row.keys()
        await store.set_eleven_conversation_id("CA-old", "conv_abc")
        assert await store.call_id_for_eleven_conversation("conv_abc") == "CA-old"
        summary = store._call_summary(await store._get_call_row("CA-old"))
        assert summary["eleven_conversation_id"] == "conv_abc"
        await store.close()

    asyncio.run(run())

