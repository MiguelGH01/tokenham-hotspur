#!/usr/bin/env python
"""Concurrency readiness: hold N Twilio-shaped sockets open at the same time.

Run All opens **ten** sockets to the endpoint at once, and PR-02's largest burst
opens **twenty**. Both are one process answering many calls, and the failures
this looks for are the ones that only appear under load: a socket that is never
picked up, a greeting that never arrives because the pipeline never started, or
two calls sharing state they should not share.

It is a readiness check, not a scored call. Nobody speaks, so every case would
fail if it were scored; what is measured is that all N lines are answered and
stay isolated. Run it before the first scored run of the day — that is what the
problem set asks for, and finding it out on Friday afternoon is cheaper than
finding it out with a Run All in flight.

Side effect: when a socket closes, the bot posts its end-of-call fallback
(``NO_ACTION`` / ``out_of_scope``) against the call id it was dialled with. These
ids were never dialled by the platform, so every one of them answers
``404 unknown call`` — the same thing the local eval scenarios produce. Point
``CLINIC_API_BASE_URL`` at a stub if even that is unwanted.

Usage::

    cd server && uv run python ../scripts/concurrency_check.py \
        --connections 20 --url ws://localhost:7862/ws \
        --bot-log /tmp/pipecat-twilio.log
"""

import argparse
import asyncio
import base64
import json
import re
import statistics
import sys
import time
import uuid
from pathlib import Path

import websockets

#: 20 ms of 8 kHz µ-law — the frame size and cadence the call contract states.
FRAME_BYTES = 160
FRAME_SECS = 0.02
#: 8-bit µ-law silence.
SILENCE = base64.b64encode(b"\xff" * FRAME_BYTES).decode()

CALL_LOG_RE = re.compile(r"Starting bot for call ([0-9a-f-]{36})")


async def one_call(index, url, hold_secs, greet_timeout):
    """One dialled line: handshake, stream silence, wait for the greeting."""
    call_id = str(uuid.uuid4())
    stream_id = f"MZ{index:08d}"
    outcome = {
        "call_id": call_id,
        "index": index,
        "greet_secs": None,
        "outbound_bytes": 0,
        "error": None,
    }
    started = time.perf_counter()
    try:
        async with websockets.connect(url, max_size=None, open_timeout=greet_timeout) as ws:
            await ws.send(
                json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
            )
            await ws.send(
                json.dumps(
                    {
                        "event": "start",
                        "streamSid": stream_id,
                        "start": {
                            "streamSid": stream_id,
                            "callSid": call_id,
                            "customParameters": {
                                "call_id": call_id,
                                "from_number": f"+3460000{index:04d}",
                            },
                        },
                    }
                )
            )

            async def reader():
                async for raw in ws:
                    try:
                        message = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if message.get("event") != "media":
                        continue
                    payload = (message.get("media") or {}).get("payload") or ""
                    if not payload:
                        continue
                    if outcome["greet_secs"] is None:
                        outcome["greet_secs"] = time.perf_counter() - started
                    outcome["outbound_bytes"] += len(base64.b64decode(payload))

            read_task = asyncio.create_task(reader())
            try:
                deadline = time.perf_counter() + hold_secs
                step = 0
                while time.perf_counter() < deadline and outcome["greet_secs"] is None:
                    await ws.send(
                        json.dumps(
                            {
                                "event": "media",
                                "streamSid": stream_id,
                                "media": {
                                    "track": "inbound",
                                    "chunk": str(step),
                                    "timestamp": str(step * 20),
                                    "payload": SILENCE,
                                },
                            }
                        )
                    )
                    step += 1
                    await asyncio.sleep(FRAME_SECS)
                # Keep the line open a moment past the greeting so a late
                # pipeline error shows up as an exception rather than as silence.
                if outcome["greet_secs"] is not None:
                    await asyncio.sleep(0.5)
                await ws.send(json.dumps({"event": "stop", "streamSid": stream_id}))
            finally:
                read_task.cancel()
    except Exception as exc:  # noqa: BLE001 - the report is the point
        outcome["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return outcome


def check_isolation(bot_log, expected_ids):
    """Every dialled id must appear once, and no id twice or twice over."""
    if not bot_log:
        return None, "no --bot-log given, isolation not checked"
    path = Path(bot_log)
    if not path.exists():
        return None, f"{path} does not exist"
    seen = CALL_LOG_RE.findall(path.read_text(errors="replace"))
    ours = [call_id for call_id in seen if call_id in expected_ids]
    missing = expected_ids - set(ours)
    duplicated = {call_id for call_id in ours if ours.count(call_id) > 1}
    if missing or duplicated:
        return False, f"missing {len(missing)}, duplicated {len(duplicated)}"
    return True, f"{len(ours)} distinct call ids took their own session"


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://localhost:7862/ws")
    parser.add_argument("--connections", "-n", type=int, default=20)
    parser.add_argument(
        "--hold-secs",
        type=float,
        default=25.0,
        help="How long each line waits for its greeting before giving up.",
    )
    parser.add_argument("--bot-log", default=None, help="Bot stdout log, for the isolation check.")
    args = parser.parse_args()

    print(f"dialling {args.connections} concurrent sockets at {args.url} ...", flush=True)
    results = await asyncio.gather(
        *(
            one_call(index, args.url, args.hold_secs, args.hold_secs + 5)
            for index in range(args.connections)
        )
    )

    greeted = [r for r in results if r["greet_secs"] is not None]
    failed = [r for r in results if r["greet_secs"] is None]
    latencies = sorted(r["greet_secs"] for r in greeted)

    for result in sorted(results, key=lambda r: r["index"]):
        if result["greet_secs"] is None:
            print(
                f"  line {result['index']:>3}  NO GREETING"
                f"  {result['error'] or 'silence for the whole hold'}"
            )
        else:
            print(
                f"  line {result['index']:>3}  greeted in {result['greet_secs']:5.2f} s"
                f"  {result['outbound_bytes'] / 16000:5.1f} s of audio out"
            )

    print()
    print(f"{len(greeted)}/{args.connections} lines greeted")
    if latencies:
        print(
            f"greeting latency: p50 {statistics.median(latencies):.2f} s, "
            f"p95 {latencies[max(0, int(len(latencies) * 0.95) - 1)]:.2f} s, "
            f"max {latencies[-1]:.2f} s"
        )
    if failed:
        print("failures:")
        for result in failed:
            print(f"  {result['call_id']} {result['error'] or 'no greeting'}")

    isolated, detail = check_isolation(args.bot_log, {r["call_id"] for r in results})
    print(f"isolation: {detail}")

    if failed or isolated is False:
        print("\nNOT READY: an endpoint that loses a socket loses the case it carried.")
        return 1
    print("\nREADY for the wave Run All opens.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
