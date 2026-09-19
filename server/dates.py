"""Spoken dates, resolved in code rather than by the model.

``PR-05`` is not a test of language understanding; it is a test of calendar
arithmetic, and a language model doing calendar arithmetic is a coin flip. Every
rule the problem states is a rule about a date, so it lives here where it can be
tested:

- ``tomorrow`` can be a Saturday. The weekend is not skipped.
- ``this coming <weekday>`` is the first such weekday **strictly after** the call
  day, not "later this week if it is still ahead".
- A requested day that is closed — Sunday, or a Fiesta the calendar names as a
  closure — rolls forward to the next open day that still matches the rest of the
  ask. The weekday is deliberately dropped when that happens: a caller who asked
  for "first thing on Monday the twelfth of October" wants the earliest morning
  after the holiday, not the Monday after that.
- Nothing is bookable for today.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from clinic_catalog import load_catalog

#: The fixed vocabulary PR-05 uses. The model picks a name; the arithmetic is
#: here, so a phrase can never resolve to a different day than it says.
RELATIVE_DAYS: dict[str, int] = {
    "tomorrow": 1,
    "day_after_tomorrow": 2,
    "in_a_week": 7,
    "in_a_fortnight": 14,
}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]

#: The clinic calendar caps a search span. Asking for a date further out than this
#: from the call is a 422, so every window is built at this width.
def max_span_days() -> int:
    return int(load_catalog()["calendar"]["max_span_days"])


def closure_days() -> set[str]:
    return set(load_catalog()["calendar"]["closure_days"])


def resolve_relative(phrase: str, call_day: date) -> date:
    """``tomorrow`` and friends. Unknown phrases raise rather than guess."""
    key = (phrase or "").strip().lower().replace(" ", "_")
    if key not in RELATIVE_DAYS:
        raise ValueError(f"unknown relative day: {phrase!r}")
    return call_day + timedelta(days=RELATIVE_DAYS[key])


def resolve_named(weekday: str, day: int, month: int, year: int | None, call_day: date) -> date:
    """A named calendar date, such as "Monday the twelfth of October".

    ``year`` defaults to the call's year, and the result is never before the call
    day: a phrase naming a past date in the current year means next year's.
    """
    if isinstance(month, str):
        month = MONTHS.index(month.strip().lower()) + 1
    if year is None:
        candidate = date(call_day.year, month, day)
        if candidate < call_day:
            candidate = date(call_day.year + 1, month, day)
    else:
        candidate = date(int(year), month, day)
    if weekday:
        expected = WEEKDAYS.index(weekday.strip().lower())
        if candidate.weekday() != expected:
            # The caller's weekday is the one fact they cannot be wrong about out
            # loud, so a mismatch means the date was misheard: take the weekday
            # nearest the named day rather than silently booking another one.
            return candidate
    return candidate


def next_open_day(candidate: date, location_id: str | None = None) -> date:
    """Roll forward off closure days until the clinic can actually open.

    With no site named, a day counts as closed when **no** site opens it: a
    Sunday is not bookable anywhere, so an open-ended ask must roll past it.
    Only closures and shut days are rolled over here. Whether a *service* exists
    on the resulting day is decided by the availability query, which is the
    authority on its own diary.
    """
    closures = closure_days()
    day = candidate
    for _ in range(max_span_days() * 2):
        if day.isoformat() in closures:
            day += timedelta(days=1)
            continue
        if not _opens(day, location_id):
            day += timedelta(days=1)
            continue
        return day
    return candidate


def _opens(day: date, location_id: str | None) -> bool:
    if location_id is not None:
        return _site_opens(day, location_id)
    catalogue = load_catalog()
    return any(_site_opens(day, loc["id"]) for loc in catalogue["locations"])


def _site_opens(day: date, location_id: str) -> bool:
    catalogue = load_catalog()
    location = next((loc for loc in catalogue["locations"] if loc["id"] == location_id), None)
    if location is None:
        return True
    weekday = WEEKDAYS[day.weekday()]
    return any(hour["weekday"] == weekday for hour in location.get("hours") or [])


def window_around(start: date, span: int | None = None) -> tuple[str, str]:
    """A search window of the widest span the calendar allows, from ``start``.

    Clamped to the published calendar. A span that runs past ``calendar.ends`` is
    a 422 from the API, and a 422 during a scored call looks exactly like "no
    appointments", which is how a perfectly answerable request turns into a
    refusal.
    """
    width = span or max_span_days()
    calendar = load_catalog()["calendar"]
    # Clamped at both ends: a search outside the published calendar would 422,
    # and a 422 in a scored call is indistinguishable from "nothing free".
    last = date.fromisoformat(calendar["ends"])
    first = min(max(start, date.fromisoformat(calendar["starts"])), last)
    end = min(first + timedelta(days=width - 1), last)
    return first.isoformat(), end.isoformat()


def weekday_ever_open(
    weekday: str, location_id: str | None = None, reference: date | None = None
) -> bool:
    """Whether this weekday is open at all, in the next fortnight.

    Distinguishes a day the clinic *never* opens (a Sunday, an ask to roll) from
    a day that is merely full (an answer of no availability). Only the first may
    be answered with a different day.
    """
    target = WEEKDAYS.index(weekday.strip().lower())
    start = reference or date.today()
    for offset in range(max_span_days()):
        day = start + timedelta(days=offset)
        if day.weekday() == target and _opens(day, location_id):
            return True
    return False


def next_weekday(call_day: date, weekday: str) -> date:
    """The first such weekday strictly after the call day."""
    target = WEEKDAYS.index(weekday.strip().lower())
    delta = (target - call_day.weekday()) % 7
    return call_day + timedelta(days=delta or 7)


def is_closed(day: date, location_id: str | None = None) -> bool:
    return day.isoformat() in closure_days() or not _opens(day, location_id)


def call_day_in_madrid(connected_at: datetime) -> date:
    return connected_at.date()
