"""Reception's notices: what the front desk knows today and the clinic's API does not.

The catalogue and ``/availability`` are the clinic's standing truth. A doctor who
rings in sick, a day the clinic shuts, a lift that is out of order — none of that
reaches the API, so the agent would book straight through it. Reception writes
those facts to one JSON file and the next call honours them.

Three properties hold the design together:

- **No file, no change.** Every reader treats a missing file as "no notices", so
  the agent that answers a scored call is byte-for-byte the agent without this
  module.
- **Every notice ends.** ``until`` is mandatory. The failure this guards against
  is not a wrong notice but a forgotten one.
- **A bad entry never breaks a call.** The file is written by hand or by a form;
  it crosses a trust boundary. Invalid entries are dropped and logged, valid ones
  still apply. Only the write route is strict, because there a person is waiting
  to be told what they got wrong.

Dated notices (absence, closure, spoken) are compared against the *appointment's*
day, so they expire on their own. ``insurer_dropped`` and the tone are compared
against the day of the call.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from loguru import logger

import audit
from booking import MADRID
from clinic.clinic_catalog import load_base_catalog

DEFAULT_PATH = Path(__file__).resolve().parent / "reception_notices.json"
EMPTY: dict = {"tone": None, "notices": []}

#: Free text ends up in front of the model, so it is short and it is data.
MAX_TEXT = 200
#: A closure is expanded day by day into the calendar; a typo in the year should
#: be an error, not four thousand closed days.
MAX_SPAN_DAYS = 366
#: Per-notice caps bound one entry; this bounds the file. Without it the text cap
#: is defeated by arithmetic — 500 notices of 200 characters is 100 KB of staff
#: text in front of the model on every offer.
MAX_NOTICES = 50
#: The whole document, read into memory before anything validates it, by the very
#: process that is carrying live audio. A few dozen notices is kilobytes.
MAX_BODY_BYTES = 64 * 1024
#: An id is a handle, not prose: it reaches the model in the justification and
#: the audit trail, so it gets no free text at all.
ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: Which keys each kind is allowed to carry. Only these are persisted, so a body
#: cannot smuggle a field past validation into whatever reads the file next.
KIND_FIELDS = {
    "provider_absent": ("provider_id",),
    "clinic_closed": (),
    "insurer_dropped": ("provider_id", "insurer_id"),
    "spoken": ("text", "location_id"),
}
KINDS = tuple(KIND_FIELDS)


@dataclass(frozen=True)
class Notice:
    id: str
    kind: str
    start: date
    until: date
    provider_id: str | None = None
    insurer_id: str | None = None
    location_id: str | None = None
    text: str | None = None

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.until


@dataclass(frozen=True)
class Notices:
    entries: tuple[Notice, ...] = ()
    tone_text: str | None = None
    tone_until: date | None = None

    def of_kind(self, kind: str) -> tuple[Notice, ...]:
        return tuple(n for n in self.entries if n.kind == kind)


def notices_path() -> Path:
    return Path(os.getenv("RECEPTION_NOTICES_PATH") or DEFAULT_PATH)


def _text(value) -> str:
    """One line of staff prose, with nothing in it that can forge structure.

    Control characters are the whole attack: a newline lets free text open what
    looks like a new section of the system instruction ("\\n\\nSYSTEM: ..."), and
    the length cap does nothing to stop it. Stripping them leaves a sentence,
    which is all a tone or a spoken notice was ever meant to be.
    """
    if not isinstance(value, str):
        raise ValueError("text is required")
    # Replaced with a space, not removed: a staff note written over two lines
    # would otherwise come back with the two words glued together.
    cleaned = " ".join(
        "".join(" " if unicodedata.category(c) == "Cc" else c for c in value).split()
    )
    if not cleaned:
        raise ValueError("text is required")
    if len(cleaned) > MAX_TEXT:
        raise ValueError(f"text is longer than {MAX_TEXT} characters")
    return cleaned


def _day(entry: dict, key: str) -> date:
    try:
        return date.fromisoformat(str(entry[key])[:10])
    except KeyError:
        raise ValueError(f"'{key}' is required") from None
    except ValueError:
        raise ValueError(f"'{key}' is not a YYYY-MM-DD date") from None


def _parse(entry: dict, catalogue: dict) -> Notice:
    if not isinstance(entry.get("id"), str) or not ID_PATTERN.match(entry["id"]):
        raise ValueError("'id' must be 1-64 characters of letters, digits, dot, dash or underscore")
    kind = entry.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    start, until = _day(entry, "from"), _day(entry, "until")
    if start > until:
        raise ValueError("'from' is after 'until'")
    if (until - start).days > MAX_SPAN_DAYS:
        raise ValueError(f"spans more than {MAX_SPAN_DAYS} days")

    # The value that failed is never echoed back. Reporting which entry was
    # rejected is the contract; repeating what was sent turns the error list into
    # a directory oracle — absence from it says "that id exists", which is the
    # one thing the agent is told never to disclose.
    provider_id = insurer_id = location_id = text = None
    if kind in ("provider_absent", "insurer_dropped"):
        provider_id = entry.get("provider_id")
        if provider_id not in catalogue["_provider_ids"]:
            raise ValueError("'provider_id' is not a provider at this clinic")
    if kind == "insurer_dropped":
        insurer_id = entry.get("insurer_id")
        if insurer_id not in catalogue["_plan_ids"]:
            raise ValueError("'insurer_id' is not a plan this clinic bills")
    if kind == "spoken":
        text = _text(entry.get("text"))
        location_id = entry.get("location_id")
        if location_id is not None and location_id not in catalogue["_location_ids"]:
            raise ValueError("'location_id' is not a site of this clinic")
    return Notice(entry["id"], kind, start, until, provider_id, insurer_id, location_id, text)


@lru_cache(maxsize=1)
def _known_ids() -> dict:
    """The published ids a notice may name, hoisted out of the per-entry loop."""
    catalogue = load_base_catalog()
    return {
        "_provider_ids": frozenset(p["id"] for p in catalogue["providers"]),
        "_plan_ids": frozenset(p["id"] for p in catalogue["plans"]),
        "_location_ids": frozenset(loc["id"] for loc in catalogue["locations"]),
    }


def validate_notices(raw: dict) -> tuple[Notices, list[dict]]:
    """The notices that can be honoured, and why each of the others cannot."""
    if not isinstance(raw, dict):
        return Notices(), [{"id": None, "error": "the file is not a JSON object"}]
    listed = raw.get("notices") or []
    if not isinstance(listed, list):
        return Notices(), [{"id": None, "error": "'notices' must be a list"}]
    if len(listed) > MAX_NOTICES:
        return Notices(), [{"id": None, "error": f"more than {MAX_NOTICES} notices"}]
    catalogue = _known_ids()
    entries, errors, seen = [], [], set()
    for entry in listed:
        try:
            if not isinstance(entry, dict):
                raise ValueError("a notice must be an object")
            notice = _parse(entry, catalogue)
            # Two entries under one id would render as one row in the console and
            # be deleted together, so the second is an error, not a duplicate.
            if notice.id in seen:
                raise ValueError("duplicate 'id'")
            seen.add(notice.id)
            entries.append(notice)
        except ValueError as exc:
            errors.append(
                {"id": entry.get("id") if isinstance(entry, dict) else None, "error": str(exc)}
            )

    tone_text = tone_until = None
    tone = raw.get("tone")
    if tone is not None:
        try:
            if not isinstance(tone, dict):
                raise ValueError("tone must be an object")
            # The tone is a standing preference, not a dated exception: how the
            # clinic wants to sound does not expire on a Tuesday. An end date is
            # still allowed, for a tone that genuinely is temporary.
            tone_text = _text(tone.get("text"))
            tone_until = _day(tone, "until") if tone.get("until") else None
        except ValueError as exc:
            tone_text = tone_until = None
            errors.append({"id": "tone", "error": str(exc)})
    return Notices(tuple(entries), tone_text, tone_until), errors


def _read(path: Path) -> dict | None:
    """The file as written, or ``None`` when there is nothing usable there."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Reception notices unreadable, ignoring them: {}", exc)
        return None


def notices_stamp():
    """A cache key for the current version of the file, for callers that layer on it."""
    return _stamp(notices_path())


def _stamp(path: Path):
    """What makes one version of the file different from the next."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _parsed(stamp) -> tuple[Notices, tuple]:
    """Parse once per version of the file, not once per reader.

    ``load_catalog`` is called many times inside one tool turn, so re-parsing per
    call did two things wrong: two reads within one decision could disagree, and
    a single bad entry logged a warning on every one of them. Keying on the
    file's mtime keeps the guarantee that matters — a new file is a new key, so
    an edit still reaches the very next call.
    """
    if stamp is None:
        return Notices(), ()
    raw = _read(Path(stamp[0]))
    if raw is None:
        return Notices(), ()
    notices, errors = validate_notices(raw)
    for error in errors:
        logger.warning("Reception notice {} ignored: {}", error["id"], error["error"])
    return notices, tuple(errors)


def load_notices(path: Path | None = None) -> Notices:
    target = path or notices_path()
    return _parsed(_stamp(target))[0]


def notice_errors(path: Path | None = None) -> list[dict]:
    """What the file asked for that the agent is not doing."""
    target = path or notices_path()
    return list(_parsed(_stamp(target))[1])


def hide_absent(availability: dict, notices: Notices) -> tuple[dict, list[str]]:
    """Drop the slots of a doctor reception says is away that day.

    By appointment day, not call day: a note left today about tomorrow has to
    bite today, and a doctor back on Thursday is bookable for Thursday while
    still away. Returns a new dict; the API's answer is never edited in place.
    """
    absences = notices.of_kind("provider_absent")
    if not absences:
        return availability, []
    kept, applied = [], []
    for slot in availability["slots"]:
        day = datetime.fromisoformat(slot["start_time"]).astimezone(MADRID).date()
        hit = next(
            (n for n in absences if n.provider_id == slot["provider_id"] and n.covers(day)), None
        )
        if hit is None:
            kept.append(slot)
        elif hit.id not in applied:
            applied.append(hit.id)
    if not applied:
        return availability, []
    return {**availability, "slots": kept}, applied


def spoken_for(notices: Notices, location_id: str, day: date) -> list[str]:
    return [
        n.text
        for n in notices.of_kind("spoken")
        if n.covers(day) and n.location_id in (None, location_id)
    ]


def tone_for(notices: Notices, today: date) -> str | None:
    """The voice in force today. No end date means it stands until changed."""
    if not notices.tone_text:
        return None
    if notices.tone_until and today > notices.tone_until:
        return None
    return notices.tone_text


def absent_days(notices: Notices, provider_id: str) -> dict[str, str]:
    """Day → the notice id that says this doctor is away, for that doctor.

    The doctor's own calendar reads this so it cannot disagree with the agent:
    a day reception marked away must look away to them too, or the console says
    one thing while the phone says another.
    """
    out: dict[str, str] = {}
    for n in notices.of_kind("provider_absent"):
        if n.provider_id != provider_id:
            continue
        for offset in range((n.until - n.start).days + 1):
            out[(n.start + timedelta(days=offset)).isoformat()] = n.id
    return out


def closed_days(notices: Notices) -> list[str]:
    return [
        (n.start + timedelta(days=offset)).isoformat()
        for n in notices.of_kind("clinic_closed")
        for offset in range((n.until - n.start).days + 1)
    ]


def dropped_insurers(notices: Notices, today: date) -> dict[str, list[str]]:
    """Provider id → insurer ids that provider stopped taking, as of ``today``."""
    out: dict[str, list[str]] = {}
    for n in notices.of_kind("insurer_dropped"):
        if n.covers(today):
            out.setdefault(n.provider_id, []).append(n.insurer_id)
    return out


def as_document(notices: Notices) -> dict:
    """The notices as JSON, carrying only the fields their kind is allowed.

    Round-trips: reading this back yields the same ``Notices``. It is what gets
    persisted and what the console is served, so the file, the page and the
    agent cannot drift apart.
    """
    source = {
        "provider_id": lambda n: n.provider_id,
        "insurer_id": lambda n: n.insurer_id,
        "location_id": lambda n: n.location_id,
        "text": lambda n: n.text,
    }
    document: dict = {"tone": None, "notices": []}
    if notices.tone_text:
        document["tone"] = {"text": notices.tone_text}
        if notices.tone_until:
            document["tone"]["until"] = notices.tone_until.isoformat()
    for notice in notices.entries:
        entry = {
            "id": notice.id,
            "kind": notice.kind,
            "from": notice.start.isoformat(),
            "until": notice.until.isoformat(),
        }
        for field in KIND_FIELDS[notice.kind]:
            entry[field] = source[field](notice)
        document["notices"].append(entry)
    return document


def _write_atomically(path: Path, document: dict) -> None:
    """Replace the file in one step, without following a planted symlink.

    ``mkstemp`` creates a fresh file with a name nobody could have pre-placed,
    which ``Path.write_text`` on a fixed ``.tmp`` name could not promise.
    """
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def record_active_notices(call_id: str) -> None:
    """Pin which notices were in force when the call began.

    Silent when there are none, so a call with no notices file leaves exactly
    the trail it left before this module existed.
    """
    notices = load_notices()
    if notices.entries or notices.tone_text:
        audit.audit(
            call_id,
            "notices_active",
            ids=[n.id for n in notices.entries],
            tone=bool(notices.tone_text),
        )


def mount_notices_routes(app: FastAPI, write_guard: Callable | None = None) -> None:
    """GET/PUT ``/notices``: the file, for the console to show and edit.

    ``write_guard`` is the console's ``require_admin``. It is a parameter rather
    than an import so this module stays free of the console's auth — and so a
    test can mount the routes without a session. The caller that mounts this on
    the tunnelled port is the one that must pass it.
    """
    guard = [Depends(write_guard)] if write_guard else []

    @app.get("/notices")
    async def get_notices():
        """What the agent is actually honouring — not what the file happens to say.

        Returning the raw file would let the console paint an absence that the
        agent silently dropped as invalid, which is the exact bug a console
        exists to prevent. ``errors`` carries those, so the page can show them.
        """
        stored = _read(notices_path())
        if stored is None:
            return EMPTY
        return as_document(validate_notices(stored)[0])

    @app.get("/notices/catalogue")
    async def get_notices_catalogue():
        """The names and ids a notice may refer to, for the form's dropdowns.

        Served from the published catalogue rather than typed into the page, so
        a doctor who joins the clinic appears in the form without a deploy, and
        the page can never offer an id the validator would then refuse.
        """
        catalogue = load_base_catalog()
        pick = lambda items: [{"id": i["id"], "name": i["name"]} for i in items]
        return {
            "providers": pick(catalogue["providers"]),
            "plans": pick(catalogue["plans"]),
            "locations": pick(catalogue["locations"]),
        }

    @app.get("/notices/errors")
    async def get_notice_errors():
        """Entries the file asks for that the agent is not doing.

        Only a hand-edited file can hold these — ``PUT`` refuses them — but the
        console must be able to show them, or it would paint an absence the
        agent silently dropped, which is the bug a console exists to prevent.
        """
        stored = _read(notices_path())
        return {"errors": [] if stored is None else validate_notices(stored)[1]}

    @app.put("/notices", dependencies=guard)
    async def put_notices(request: Request):
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="notices document too large")
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail=[{"id": None, "error": "invalid JSON"}])
        notices, errors = validate_notices(body)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        # Only what validation understood is written back, so a field that slipped
        # past unexamined cannot reach whatever reads this file next.
        _write_atomically(notices_path(), as_document(notices))
        return {"saved": True}
