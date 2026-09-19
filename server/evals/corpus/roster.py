"""The organisers' published case roster: the only published ground truth.

Every published case carries the persona, the exact script the organisers feed
their caller, and — the part that matters — ``expected.acceptable``: the literal
action lists the scorer accepts for it. That turns "did we pass this case?" from
a slow, blind leaderboard round trip into a local diff we can run in
milliseconds, as often as we like, before spending a scored run on finding out.

Two properties of the roster shape how it is used:

- it is **fixed by a seed** for every process, so the published cases are stable;
- the answers are anchored to 09:00 Europe/Madrid **on the day the case is
  dialled**, so "the earliest appointment" moves overnight. Re-fetch it each
  morning of the event; a stale roster judges yesterday's call correctly and
  today's wrongly.

A copy is committed beside this module so the judge, the tests and CI need no
network at all. ``--fetch`` refreshes it and prints the digest.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from pathlib import Path

BASE_URL = "https://hackspain.getprosperapp.com/leaderboard"
#: The asset name carries a hash that changes when the organisers republish it,
#: so the known one is a fallback: the index page is scanned for the current name.
KNOWN_ASSET_URL = f"{BASE_URL}/assets/public-cases-BH3bsRyz.json"
INDEX_URL = f"{BASE_URL}/"
CACHE = Path(__file__).with_name("public-cases.json")


class RosterUnavailable(RuntimeError):
    """No roster on disk and no way to fetch one."""


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def discover_url(timeout: float = 20.0) -> str:
    """The current asset URL, read off the leaderboard index page."""
    with urllib.request.urlopen(INDEX_URL, timeout=timeout) as response:  # noqa: S310
        html = response.read().decode("utf-8", "replace")
    matches = re.findall(r"assets/(public-cases-[A-Za-z0-9]+\.json)", html)
    if not matches:
        return KNOWN_ASSET_URL
    return f"{BASE_URL}/assets/{matches[0]}"


def download(url: str, timeout: float = 30.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read()


def fetch(path: Path = CACHE, *, url: str | None = None, discover: bool = True) -> dict:
    """Refresh the cached roster. Prints nothing; returns the parsed document."""
    target = url or (discover_url() if discover else KNOWN_ASSET_URL)
    blob = download(target)
    document = json.loads(blob)
    path.write_bytes(blob)
    return document


def load(path: Path = CACHE) -> list[dict]:
    """The published cases, from the committed copy."""
    if not path.exists():
        raise RosterUnavailable(
            f"no roster at {path}: run `uv run python -m evals.corpus --fetch` once"
        )
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def sha256(path: Path = CACHE) -> str:
    return digest(path.read_bytes())


def by_id(cases: list[dict]) -> dict[str, dict]:
    return {case["id"]: case for case in cases}


def by_problem(cases: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for case in cases:
        grouped.setdefault(case["problem_id"], []).append(case)
    return grouped
