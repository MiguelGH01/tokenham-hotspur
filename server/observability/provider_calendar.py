"""Provider week calendar: free slots from /availability, booked = schedule − free.

The clinic API has no “appointments for this doctor” route — only patient diaries
and availability. For the doctor console we treat every standing schedule slot
that is *not* returned by ``/availability?provider_id=…`` as occupied, and
overlay named BOOKs the bot has posted for that provider.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from clinic_catalog import load_catalog
from rules import provider_by_id

MADRID = ZoneInfo("Europe/Madrid")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_INTERVAL_RE = re.compile(r"(\d{2}:\d{2})\s*[–\-]\s*(\d{2}:\d{2})")


def _parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def _iter_interval_starts(day: date, interval: str, slot_minutes: int) -> list[datetime]:
    match = _INTERVAL_RE.fullmatch(interval.strip())
    if not match:
        return []
    sh, sm = _parse_hhmm(match.group(1))
    eh, em = _parse_hhmm(match.group(2))
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=MADRID)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=MADRID)
    out: list[datetime] = []
    step = timedelta(minutes=slot_minutes)
    cur = start
    while cur + step <= end:
        out.append(cur)
        cur += step
    return out


def standing_slot_starts(
    provider: dict,
    day: date,
    *,
    slot_minutes: int,
    closure_days: frozenset[str],
) -> list[tuple[datetime, str, str]]:
    """(start, location_id, location_name) for every bookable tick that day."""
    if day.isoformat() in closure_days:
        return []
    weekday = WEEKDAYS[day.weekday()]
    leave = provider.get("leave")
    if leave:
        try:
            leave_start = date.fromisoformat(leave["start"])
            leave_end = date.fromisoformat(leave["end"])
            if leave_start <= day <= leave_end:
                return []
        except (KeyError, TypeError, ValueError):
            pass
    out: list[tuple[datetime, str, str]] = []
    for site in provider.get("schedules") or []:
        loc_id = site.get("location_id") or ""
        loc_name = site.get("location_name") or loc_id
        for day_row in site.get("days") or []:
            if day_row.get("weekday") != weekday:
                continue
            for interval in day_row.get("intervals") or []:
                for start in _iter_interval_starts(day, interval, slot_minutes):
                    out.append((start, loc_id, loc_name))
    out.sort(key=lambda row: row[0])
    return out


def _merge_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive same-kind ticks into longer spans."""
    if not blocks:
        return []
    merged: list[dict[str, Any]] = []
    for block in blocks:
        if not merged:
            merged.append(dict(block))
            continue
        prev = merged[-1]
        same = (
            prev["kind"] == block["kind"]
            and prev.get("location_id") == block.get("location_id")
            and prev.get("patient_name") == block.get("patient_name")
            and prev.get("source") == block.get("source")
            and prev["end"] == block["start"]
        )
        if same:
            prev["end"] = block["end"]
            continue
        merged.append(dict(block))
    return merged


def build_day_blocks(
    *,
    day: date,
    provider: dict,
    free_starts: set[str],
    bot_by_start: dict[str, dict[str, Any]],
    slot_minutes: int,
    closure_days: frozenset[str],
    infer_busy: bool = True,
    overlay_by_start: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One day's blocks for the calendar UI."""
    overlays = overlay_by_start or {}
    ticks = standing_slot_starts(
        provider, day, slot_minutes=slot_minutes, closure_days=closure_days
    )
    blocks: list[dict[str, Any]] = []
    for start, loc_id, loc_name in ticks:
        end = (start + timedelta(minutes=slot_minutes)).strftime("%H:%M")
        start_hm = start.strftime("%H:%M")
        norm = _norm_start_key(start)
        overlay = overlays.get(norm)
        if overlay:
            name = overlay.get("patient_name")
            label = f"Urgencia · {name}" if name else "Urgencia"
            blocks.append(
                {
                    "start": start_hm,
                    "end": end,
                    "kind": "emergency",
                    "source": "overlay",
                    "location_id": loc_id,
                    "location_name": loc_name,
                    "patient_name": name,
                    "appointment_type_id": None,
                    "label": label,
                }
            )
            continue
        bot = bot_by_start.get(norm)
        if bot:
            blocks.append(
                {
                    "start": start_hm,
                    "end": end,
                    "kind": "booked",
                    "source": "bot",
                    "location_id": loc_id,
                    "location_name": loc_name,
                    "patient_name": bot.get("patient_name"),
                    "appointment_type_id": bot.get("appointment_type_id"),
                    "label": bot.get("patient_name") or "Cita",
                }
            )
            continue
        if norm in free_starts:
            blocks.append(
                {
                    "start": start_hm,
                    "end": end,
                    "kind": "free",
                    "source": "availability",
                    "location_id": loc_id,
                    "location_name": loc_name,
                    "label": "Libre",
                }
            )
            continue
        if infer_busy:
            blocks.append(
                {
                    "start": start_hm,
                    "end": end,
                    "kind": "booked",
                    "source": "inferred",
                    "location_id": loc_id,
                    "location_name": loc_name,
                    "patient_name": None,
                    "label": "Cita",
                }
            )
        else:
            # No live occupancy: treat standing hours as free rather than a
            # third “Consulta” state that confuses the doctor UI.
            blocks.append(
                {
                    "start": start_hm,
                    "end": end,
                    "kind": "free",
                    "source": "schedule",
                    "location_id": loc_id,
                    "location_name": loc_name,
                    "label": "Libre",
                }
            )
    return _merge_blocks(blocks)


def _norm_start_key(start: datetime | str) -> str:
    """Normalise to Europe/Madrid ``YYYY-MM-DDTHH:MM`` for set membership."""
    if isinstance(start, datetime):
        local = start.astimezone(MADRID)
        return f"{local.date().isoformat()}T{local.strftime('%H:%M')}"
    text = str(start).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MADRID)
        local = dt.astimezone(MADRID)
        return f"{local.date().isoformat()}T{local.strftime('%H:%M')}"
    except ValueError:
        return text[:16]


def free_start_keys(slots: list[dict[str, Any]]) -> set[str]:
    return {_norm_start_key(s["start_time"]) for s in slots if s.get("start_time")}


def bot_booking_index(
    bookings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Map normalised start → booking row (last write wins)."""
    out: dict[str, dict[str, Any]] = {}
    for row in bookings:
        slot = row.get("slot")
        if not slot:
            continue
        out[_norm_start_key(slot)] = row
    return out


def overlay_index(
    overlays: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Map normalised start → emergency overlay (last write wins)."""
    out: dict[str, dict[str, Any]] = {}
    for row in overlays:
        slot = row.get("slot")
        if not slot:
            continue
        out[_norm_start_key(slot)] = row
    return out


def calendar_window(*, anchor: date | None = None, days: int = 6) -> tuple[date, date]:
    """Monday-aligned window of ``days`` calendar days (default Mon–Sat)."""
    today = anchor or datetime.now(MADRID).date()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=max(0, days - 1))


def apply_cancellations(
    bookings: list[dict[str, Any]],
    cancellations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Drop bookings whose slot was later cancelled; return freed slot keys."""
    cancelled_keys = {_norm_start_key(c["slot"]) for c in cancellations if c.get("slot")}
    cancelled_ids = {
        c["appointment_id"] for c in cancellations if c.get("appointment_id")
    }
    kept: list[dict[str, Any]] = []
    for row in bookings:
        if row.get("slot") and _norm_start_key(row["slot"]) in cancelled_keys:
            continue
        if row.get("appointment_id") and row["appointment_id"] in cancelled_ids:
            continue
        kept.append(row)
    return kept, cancelled_keys


def assemble_calendar(
    provider: dict,
    *,
    date_from: date,
    date_to: date,
    free_slots: list[dict[str, Any]],
    bot_bookings: list[dict[str, Any]],
    cancellations: list[dict[str, Any]] | None = None,
    overlays: list[dict[str, Any]] | None = None,
    source: str = "availability",
) -> dict[str, Any]:
    catalogue = load_catalog()
    slot_minutes = int(catalogue["calendar"]["slot_minutes"])
    closure_days = frozenset(catalogue["calendar"]["closure_days"])
    kept, freed = apply_cancellations(bot_bookings, cancellations or [])
    free = free_start_keys(free_slots) | freed
    bots = bot_booking_index(kept)
    overlay_by_start = overlay_index(overlays or [])
    infer_busy = source == "availability"
    days_out: list[dict[str, Any]] = []
    day = date_from
    while day <= date_to:
        ticks = standing_slot_starts(
            provider, day, slot_minutes=slot_minutes, closure_days=closure_days
        )
        location_name = ticks[0][2] if ticks else None
        location_id = ticks[0][1] if ticks else None
        if ticks:
            from collections import Counter

            top = Counter((loc_id, loc_name) for _, loc_id, loc_name in ticks).most_common(1)[0][0]
            location_id, location_name = top
        days_out.append(
            {
                "date": day.isoformat(),
                "weekday": WEEKDAYS[day.weekday()],
                "location_id": location_id,
                "location_name": location_name,
                "blocks": build_day_blocks(
                    day=day,
                    provider=provider,
                    free_starts=free,
                    bot_by_start=bots,
                    slot_minutes=slot_minutes,
                    closure_days=closure_days,
                    infer_busy=infer_busy,
                    overlay_by_start=overlay_by_start,
                ),
            }
        )
        day += timedelta(days=1)
    return {
        "provider_id": provider["id"],
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "source": source,
        "slot_minutes": slot_minutes,
        "days": days_out,
    }


async def fetch_provider_free_slots(
    provider_id: str,
    date_from: date,
    date_to: date,
) -> tuple[list[dict[str, Any]], str]:
    """Call the clinic API; returns (slots, source_tag)."""
    from clients.clinic_client import ClinicClient

    provider = provider_by_id(load_catalog(), provider_id)
    if provider is None:
        return [], "missing_provider"

    client = ClinicClient()
    # Span capped at 14 days by the API.
    slots: list[dict[str, Any]] = []
    cursor = date_from
    try:
        while cursor <= date_to:
            chunk_end = min(cursor + timedelta(days=13), date_to)
            data = await client._request(
                "GET",
                "/v1/availability",
                params={
                    "date_from": cursor.isoformat(),
                    "date_to": chunk_end.isoformat(),
                    "provider_id": provider_id,
                    "specialty_id": provider["specialty_id"],
                },
                read=True,
            )
            for slot in data.get("slots") or []:
                if slot.get("provider_id") == provider_id:
                    slots.append(slot)
            cursor = chunk_end + timedelta(days=1)
        return slots, "availability"
    except Exception as exc:  # noqa: BLE001 — console must still render
        return [], f"availability_error:{type(exc).__name__}"
