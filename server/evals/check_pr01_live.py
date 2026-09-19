"""PR-01 published callers against the LIVE clinic API, on the real clock. No LLM, never submits.

Runs the same handlers the bot runs, then checks the offer against an oracle computed
independently from the raw /availability response, so no expected date is written down:
the slot must be the earliest one after the call day that fits what the caller asked.
"""

import asyncio
import sys
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

from dotenv import load_dotenv

load_dotenv()

from booking import MADRID, WEEKDAYS  # noqa: E402
from clinic.clinic_catalog import closure_days  # noqa: E402
from clinic.clinic_client import ClinicClient  # noqa: E402
from flow.tools import get_earliest_slot, search_patient  # noqa: E402
from submission import CallSubmission  # noqa: E402

CALLERS = [  # name, id_type, id_value, what they ask for
    ("Josefa Domínguez Navarro", "national_id", "48064716Y", {"specialty": "general_practice"}),
    ("Amelia Hughes White", "national_id", "13309713G", {"specialty": "general_practice", "site": "centro"}),
    ("Ignacio Vázquez Moreno", "phone", "731 169 716", {"specialty": "orthopaedics"}),
    ("Chloe Roberts Smith", "national_id", "Z4237244M",
     {"specialty": "general_practice", "site": "sur", "weekday": "monday", "part_of_day": "morning"}),
]  # fmt: skip


class RecordingClient:
    """Keeps the raw availability so the oracle reads the API's answer, not pick_offer's."""

    def __init__(self, client):
        self._client, self.availability_response = client, None

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def availability(self, *args, **kwargs):
        self.availability_response = await self._client.availability(*args, **kwargs)
        return self.availability_response


def _problems(offer: dict, raw: dict, patient: dict, ask: dict, now: datetime) -> list[str]:
    def fits(start: datetime) -> bool:
        return (
            start.date() > now.date()
            and start.date().isoformat() not in closure_days()
            and ask.get("weekday") in (None, WEEKDAYS[start.weekday()])
            and ask.get("part_of_day") in (None, "morning" if start.hour < 14 else "afternoon")
        )

    slots = [(datetime.fromisoformat(s["start_time"]).astimezone(MADRID), s) for s in raw["slots"]]
    fitting = [(start, s) for start, s in slots if fits(start)]
    earliest = min(start for start, _ in fitting)
    load = Counter(s["provider_id"] for _, s in slots)
    tied = [s for start, s in fitting if start == earliest]
    least_loaded = max(load[s["provider_id"]] for s in tied)
    chosen = next((s for s in tied if s["provider_id"] == offer["provider_id"]), None)

    problems = []
    if offer["slot"] != earliest.isoformat():
        problems.append(f"slot {offer['slot']} is not the earliest fitting one, {earliest.isoformat()}")
    elif chosen is None or load[chosen["provider_id"]] != least_loaded:
        problems.append(f"provider {offer['provider_id']} is not the least loaded at {earliest.isoformat()}")
    elif (offer["location_id"], offer["appointment_type_id"]) != (chosen["location_id"], chosen["appointment_type_id"]):
        problems.append("site or appointment type differs from the API's slot")
    if ask.get("site") and offer["location_id"] != ask["site"]:
        problems.append(f"site {offer['location_id']} is not the one asked for")
    if (offer["patient_id"], offer["policy_id"]) != (patient["patient_id"], patient["insurer"]):
        problems.append("patient or policy does not match the directory")
    return problems


async def main() -> int:
    async def queue_frames(frames):
        pass

    now, failures = datetime.now(MADRID), 0
    client = RecordingClient(ClinicClient())
    for name, id_type, id_value, ask in CALLERS:
        state = {"client": client, "submission": CallSubmission("dry-run", client), "patient": None,
                 "offers": {}, "identify_attempts": 0, "connected_at": now}  # fmt: skip
        flow = SimpleNamespace(state=state, worker=SimpleNamespace(queue_frames=queue_frames))
        found, _ = await search_patient({"stated_name": name, "id_type": id_type, "id_value": id_value}, flow)
        if found["status"] != "found":
            problems = [f"identify -> {found}"]
        else:
            result, _ = await get_earliest_slot(ask, flow)
            offer = state["offers"].get("offer-1")
            problems = (
                _problems(offer, client.availability_response, state["patient"], ask, now)
                if offer
                else [f"no offer -> {result}"]
            )
        failures += bool(problems)
        print(f"{'FAIL' if problems else 'OK  '} {name}: {state['offers'].get('offer-1') or ''}")
        for problem in problems:
            print(f"       {problem}")
    await client.aclose()
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
