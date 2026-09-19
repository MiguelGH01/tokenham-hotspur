"""Seed a synthetic shift of ~100 calls for Overview demos without live traffic.

Usage (from server/)::

    uv run python -m observability.seed_shift
    # or POST /observability/fixtures/shift_seed/load after generating the JSONL

Writes ``fixtures/shift_seed.jsonl`` and optionally loads it into the local DB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observability.events import ObsEvent
from observability.hub import reset_hub
from observability.store import reset_store, shift_start_iso

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACTIONS = [
    ("BOOK", 0.50),
    ("REGISTER", 0.06),
    ("CANCEL", 0.05),
    ("NO_ACTION", 0.32),
    ("ESCALATE", 0.04),
    ("RESCHEDULE", 0.03),
]


def _pick_action(rng: random.Random) -> str:
    roll = rng.random()
    acc = 0.0
    for name, weight in ACTIONS:
        acc += weight
        if roll <= acc:
            return name
    return "NO_ACTION"


# How a call of each outcome walks the graph, and which tool moved it on.
# Stages match the funnel the console draws, so a seeded shift exercises the
# same projections a real one does.
_PATHS: dict[str, list[tuple[str, str]]] = {
    "BOOK":       [("identify", "search_patient"), ("find_slot", "get_earliest_slot"),
                   ("confirm", "confirm_offer"), ("goodbye", "finish_call")],
    "REGISTER":   [("identify", "search_patient"), ("registration", "prepare_registration"),
                   ("registration_confirm", "confirm_registration"), ("goodbye", "finish_call")],
    "CANCEL":     [("identify", "search_patient"), ("appointments", "lookup_appointments"),
                   ("cancel_confirm", "confirm_cancellation"), ("goodbye", "finish_call")],
    "RESCHEDULE": [("identify", "search_patient"), ("appointments", "lookup_appointments"),
                   ("find_slot", "get_earliest_slot"), ("confirm", "confirm_offer"),
                   ("goodbye", "finish_call")],
}
# A refusal stops at a stage, carrying the reason the code set there.
_REFUSALS: list[tuple[int, str]] = [
    (1, "patient_not_found"), (1, "out_of_scope"),
    (2, "no_availability"), (2, "provider_not_in_network"), (2, "referral_required"),
    (2, "not_eligible_age"), (2, "location_not_covered"), (2, "specialty_not_covered"),
    (2, "provider_not_found"),
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def build_shift_events(
    *,
    n_calls: int = 100,
    seed: int = 42,
    day: datetime | None = None,
) -> list[ObsEvent]:
    rng = random.Random(seed)
    day = day or datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # Shift window 08:00–16:52 Europe/Madrid ≈ 06:00–14:52 UTC in September.
    start = day.replace(hour=6, minute=0)
    events: list[ObsEvent] = []
    rich_ids = {0, n_calls // 3, n_calls // 2}  # a few with full traces

    for i in range(n_calls):
        hour_offset = rng.uniform(0, 8.8)  # ~08:00–16:52 local
        call_start = start + timedelta(hours=hour_offset)
        duration_s = rng.randint(45, 320)
        call_end = call_start + timedelta(seconds=duration_s)
        first_word_ms = rng.randint(400, 1800)
        action = _pick_action(rng)
        call_id = f"CA-seed-{i:04d}"
        transport = rng.choice(["twilio", "twilio", "twilio", "webrtc", "eval"])

        events.append(
            {
                "kind": "call.started",
                "call_id": call_id,
                "ts": _iso(call_start),
                "payload": {"transport": transport, "from_number": None},
            }
        )
        events.append(
            {
                "kind": "node.entered",
                "call_id": call_id,
                "ts": _iso(call_start + timedelta(milliseconds=50)),
                "payload": {"from": None, "to": "reception"},
            }
        )

        # Walk the graph the way this outcome would. A refused call stops
        # partway and carries the reason the tool set when it did.
        reason: str | None = None
        if action in _PATHS:
            walk = _PATHS[action]
        elif action == "ESCALATE":
            walk, reason = [("identify", "search_patient"), ("refused", "finish_call")], "medical_emergency"
        else:  # NO_ACTION
            stop, reason = _REFUSALS[rng.randrange(len(_REFUSALS))]
            walk = _PATHS["BOOK"][:stop] + [("no_booking", "finish_call")]

        node_from = "reception"
        identified = False
        for step, (node_to, tool) in enumerate(walk, start=1):
            at = call_start + timedelta(seconds=4 * step)
            events.append({
                "kind": "tool.called", "call_id": call_id, "ts": _iso(at),
                "payload": {"name": tool, "args": {"node": node_from}},
            })
            events.append({
                "kind": "tool.returned", "call_id": call_id,
                "ts": _iso(at + timedelta(milliseconds=rng.randint(80, 900))),
                "payload": {
                    "name": tool, "status": "ok", "next_node": node_to,
                    "from_node": node_from,
                    "justification": f"{tool} resolved, so the call moves to {node_to}.",
                    "reason_codes": [],
                },
            })
            events.append({
                "kind": "node.entered", "call_id": call_id,
                "ts": _iso(at + timedelta(seconds=1)),
                "payload": {"from": node_from, "to": node_to},
            })
            if node_to == "identify" and not (reason == "patient_not_found" and step == 1):
                identified = True
                events.append({
                    "kind": "state.patched", "call_id": call_id,
                    "ts": _iso(at + timedelta(seconds=1, milliseconds=200)),
                    "payload": {"patient_id": f"PA-{i:04d}"},
                })
            node_from = node_to
        events.append(
            {
                "kind": "metrics.first_word",
                "call_id": call_id,
                "ts": _iso(call_start + timedelta(milliseconds=first_word_ms)),
                "payload": {"ms": first_word_ms},
            }
        )

        if i in rich_ids:
            events.append(
                {
                    "kind": "transcript.bot",
                    "call_id": call_id,
                    "ts": _iso(call_start + timedelta(seconds=1)),
                    "payload": {"text": "Clínica Arenal, ¿en qué puedo ayudarte?", "final": True},
                }
            )
            events.append(
                {
                    "kind": "transcript.user",
                    "call_id": call_id,
                    "ts": _iso(call_start + timedelta(seconds=8)),
                    "payload": {"text": "Necesito una cita, por favor.", "final": True},
                }
            )
            events.append(
                {
                    "kind": "tool.called",
                    "call_id": call_id,
                    "ts": _iso(call_start + timedelta(seconds=9)),
                    "payload": {"name": "route_request", "args": {"intent": "book"}},
                }
            )
            events.append(
                {
                    "kind": "tool.returned",
                    "call_id": call_id,
                    "ts": _iso(call_start + timedelta(seconds=9, milliseconds=100)),
                    "payload": {
                        "name": "route_request",
                        "status": "routed",
                        "next_node": "identify",
                        "justification": "Caller asked to book.",
                        "reason_codes": ["capture_intent"],
                    },
                }
            )
            events.append(
                {
                    "kind": "node.entered",
                    "call_id": call_id,
                    "ts": _iso(call_start + timedelta(seconds=9, milliseconds=150)),
                    "payload": {"from": "reception", "to": "identify"},
                }
            )

        events.append(
            {
                "kind": "state.patched",
                "call_id": call_id,
                "ts": _iso(call_start + timedelta(seconds=20)),
                "payload": {"patient_name": f"Paciente {i}", "pending_action": action},
            }
        )
        events.append(
            {
                "kind": "action.queued",
                "call_id": call_id,
                "ts": _iso(call_end - timedelta(seconds=5)),
                "payload": {
                    "action": action,
                    "seq": 1,
                    "reason": reason,
                    "summary": f"Paciente {i} · {action}",
                },
            }
        )
        # ~98% successful posts
        ok = rng.random() < 0.98
        events.append(
            {
                "kind": "submit.posted",
                "call_id": call_id,
                "ts": _iso(call_end - timedelta(seconds=2)),
                "payload": {
                    "action": action,
                    "http_status": 200 if ok else 503,
                    "ok": ok,
                    "error": None if ok else "ClinicApiError",
                },
            }
        )
        events.append(
            {
                "kind": "call.ended",
                "call_id": call_id,
                "ts": _iso(call_end),
                "payload": {},
            }
        )

    return events


def write_jsonl(events: list[ObsEvent], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("# Generated shift seed — do not hand-edit; regenerate with seed_shift.py\n")
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


async def load_into_db(events: list[ObsEvent], db_path: Path) -> dict:
    store = await reset_store(db_path)
    hub = reset_hub(store)
    try:
        await hub.ensure_ready()
        count = await hub.load_fixture_events(events, clear=True)
        summary = await store.shift_summary(since=shift_start_iso())
        return {"loaded": count, "shift": summary}
    finally:
        # Without this the script prints its summary and then hangs: the
        # aiosqlite connection owns a non-daemon thread.
        await hub.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", type=int, default=100, help="Number of calls")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--load", action="store_true", help="Also load into local SQLite")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite path when --load (default: OBSERVABILITY_DB / server/data/...)",
    )
    args = parser.parse_args()
    events = build_shift_events(n_calls=args.n, seed=args.seed)
    out = FIXTURES / "shift_seed.jsonl"
    write_jsonl(events, out)
    print(f"Wrote {len(events)} events ({args.n} calls) → {out}")
    if args.load:
        result = asyncio.run(load_into_db(events, args.db) if args.db else load_into_db(events, Path(
            __import__("os").getenv("OBSERVABILITY_DB")
            or Path(__file__).resolve().parent.parent / "data" / "centralita.sqlite"
        )))
        print(json.dumps(result["shift"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
