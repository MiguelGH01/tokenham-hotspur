#!/usr/bin/env python3
"""Run Jev on every existing conversation (local store + ElevenLabs history).

Usage (from server/):

    uv run python scripts/backfill_insights.py
    uv run python scripts/backfill_insights.py --limit 10 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from clients.typesafe_client import TypeSafeInsightClient  # noqa: E402
from observability.elevenlabs_history import (  # noqa: E402
    elevenlabs_history_configured,
    get_elevenlabs_history,
)
from observability.hub import reset_hub  # noqa: E402
from observability.insights import extract_call_insights, typesafe_configured  # noqa: E402
from observability.store import default_db_path, reset_store  # noqa: E402


def _keep_row(row: dict) -> bool:
    if (row.get("transport") or "") == "eval":
        return False
    cid = str(row.get("call_id") or "")
    if cid.startswith("conv_"):
        return True
    if row.get("eleven_conversation_id"):
        return True
    if cid.startswith("CA-"):
        return True
    return False


async def collect_call_ids(store, history) -> list[str]:
    if elevenlabs_history_configured() and history is not None:
        rows = await history.list_console_calls(store, since=None, include="all")
    else:
        rows = await store.list_calls(since=None, include="all")
    ids: list[str] = []
    seen: set[str] = set()
    seen_el: set[str] = set()
    for row in rows:
        if not _keep_row(row):
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


async def latest_choices(store, call_id: str) -> str:
    events = await store.list_events(call_id)
    choices: dict[str, str] = {}
    for event in events:
        payload = event.get("payload") or {}
        name = payload.get("name")
        if not name:
            continue
        if event["kind"] == "insight.extracted":
            choices[str(name)] = str(payload.get("choice") or "")
        elif event["kind"] == "insight.failed":
            choices[str(name)] = "failed"
    if not choices:
        return ""
    return ", ".join(f"{k}={v}" for k, v in choices.items())


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="Max conversations (0 = all)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--db",
        default=os.getenv("OBSERVABILITY_DB") or str(default_db_path()),
        help="SQLite path (default: OBSERVABILITY_DB or server/data/centralita.sqlite)",
    )
    args = parser.parse_args()

    if not typesafe_configured() and not args.dry_run:
        print("TYPESAFE_API_KEY is not set; cannot run Jev.", file=sys.stderr)
        return 1

    store = await reset_store(args.db)
    hub = reset_hub(store)
    await hub.ensure_ready()
    history = get_elevenlabs_history() if elevenlabs_history_configured() else None

    defs = await store.list_insight_defs()
    if not defs:
        print("No insight definitions. Add them on the Insights page first.")
        await store.close()
        return 1
    print("Insights: " + ", ".join(d["name"] for d in defs))

    ids = await collect_call_ids(store, history)
    if args.limit:
        ids = ids[: args.limit]
    print(f"Conversations: {len(ids)}")

    if args.dry_run:
        for cid in ids:
            print(f"  {cid}")
        await store.close()
        return 0

    client = TypeSafeInsightClient()
    ok = skipped = failed = 0
    try:
        for i, cid in enumerate(ids, 1):
            status = await extract_call_insights(
                cid, store=store, emitter=hub, client=client
            )
            extra = await latest_choices(store, cid)
            suffix = f"  {extra}" if extra else ""
            print(f"[{i}/{len(ids)}] {cid}  {status}{suffix}")
            if status == "ok":
                ok += 1
            elif status in {"skipped_eval", "no_defs", "no_transcript"}:
                skipped += 1
            else:
                failed += 1
    finally:
        await client.aclose()
        if history is not None:
            await history.aclose()
        await hub.aclose()

    print(f"Done. ok={ok} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
