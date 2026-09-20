"""The clinic catalogue: what the clinic publishes, plus what reception says today.

``clinic.json`` is a snapshot of ``GET /api/v1/clinic``, which the API guarantees
will not change during the event (``API-clinic-cache``) — so it is read once and
cached. Reception's notices are the part the API cannot know: a day the clinic
shuts, a doctor who stopped taking an insurer. They are layered *on top* of that
snapshot, never merged into it, because eight modules hold the catalogue dict and
a mutation would change it under them mid-call.

With no notices file the layer is the identity function and this module behaves
exactly as it did before it existed.
"""

import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

CATALOG_PATH = Path(__file__).resolve().parent.parent.parent / "clinic.json"
MADRID = ZoneInfo("Europe/Madrid")


@lru_cache(maxsize=1)
def load_base_catalog() -> dict:
    """The catalogue as the clinic publishes it, with nothing of ours in it."""
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_catalog() -> dict:
    """The catalogue the agent decides on: published, plus today's notices.

    Cached on the notices file's mtime rather than re-derived per reader. One
    tool turn calls this many times — the rules, the date maths, the offer — and
    without a cache two of those reads could land either side of a save and
    decide against different catalogues. A new file is a new key, so reception's
    edit still reaches the very next call.
    """
    # Imported here, not at module scope: reception_notices validates against the
    # published catalogue, so importing it at the top would be a cycle. Nothing
    # in that validation path may call *this* function — it would recurse.
    import reception_notices

    return _layered(reception_notices.notices_stamp(), datetime.now(MADRID).date())


@lru_cache(maxsize=4)
def _layered(stamp, today: date) -> dict:
    import reception_notices

    base = load_base_catalog()
    notices = reception_notices.load_notices()
    closed = reception_notices.closed_days(notices)
    dropped = reception_notices.dropped_insurers(notices, today)
    if not closed and not dropped:
        return base

    catalogue = dict(base)
    if closed:
        calendar = dict(base["calendar"])
        calendar["closure_days"] = [*base["calendar"]["closure_days"], *closed]
        catalogue["calendar"] = calendar
    if dropped:
        catalogue["providers"] = [
            _with_refusals(provider, dropped.get(provider["id"])) for provider in base["providers"]
        ]
    return catalogue


def _with_refusals(provider: dict, insurer_ids: list[str] | None) -> dict:
    """A copy of the provider that also refuses these insurers.

    Shaped exactly like the API's own ``refused_insurers`` so ``accepts_plan``
    needs no notion of a notice: a doctor reception says has dropped DKV is
    indistinguishable from one the clinic publishes as refusing it.
    """
    if not insurer_ids:
        return provider
    known = {ref["id"] for ref in provider.get("refused_insurers") or []}
    added = [{"id": i, "name": i} for i in insurer_ids if i not in known]
    return {**provider, "refused_insurers": [*(provider.get("refused_insurers") or []), *added]}


def specialty_ids() -> list[str]:
    return [s["id"] for s in load_catalog()["specialties"]]


def location_ids() -> list[str]:
    return [loc["id"] for loc in load_catalog()["locations"]]


def provider_names() -> list[str]:
    return [p["name"] for p in load_catalog()["providers"]]


def closure_days() -> frozenset[str]:
    return frozenset(load_catalog()["calendar"]["closure_days"])


def location_name(location_id: str) -> str:
    return next(loc["name"] for loc in load_catalog()["locations"] if loc["id"] == location_id)
