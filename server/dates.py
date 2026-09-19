"""Resolve spoken when-phrases against this call's Europe/Madrid connect clock."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from booking import MADRID, WEEKDAYS

_WEEKDAY = {name: i for i, name in enumerate(WEEKDAYS)}
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "twenty-first": 21,
    "twenty-second": 22,
    "twenty-third": 23,
    "twenty-fourth": 24,
    "twenty-fifth": 25,
    "twenty-sixth": 26,
    "twenty-seventh": 27,
    "twenty-eighth": 28,
    "twenty-ninth": 29,
    "thirtieth": 30,
    "thirty-first": 31,
}


@dataclass(frozen=True)
class WhenConstraint:
    target_date: date | None = None
    weekday: str | None = None
    part_of_day: str | None = None
    first_thing: bool = False


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("-", " ")).strip()


def _next_weekday(call_day: date, weekday: str) -> date:
    wanted = _WEEKDAY[weekday]
    delta = (wanted - call_day.weekday()) % 7
    if delta == 0:
        delta = 7
    return call_day + timedelta(days=delta)


def _named_calendar_day(text: str, call_day: date) -> date | None:
    month = next((n for name, n in _MONTHS.items() if name in text), None)
    if month is None:
        return None
    year_m = re.search(r"\b(20\d{2})\b", text)
    year = int(year_m.group(1)) if year_m else call_day.year
    day = None
    day_m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text)
    if day_m:
        day = int(day_m.group(1))
    else:
        for word, n in sorted(_ORDINALS.items(), key=lambda item: -len(item[0])):
            if re.search(rf"\b{word}\b", text):
                day = n
                break
    if day is None:
        return None
    return date(year, month, day)


def parse_when(text: str | None, connected_at: datetime) -> WhenConstraint:
    if not text:
        return WhenConstraint()
    raw = _norm(text)
    call_day = connected_at.astimezone(MADRID).date()
    first_thing = "first thing" in raw
    part = None
    if "afternoon" in raw or "evening" in raw:
        part = "afternoon"
    elif "morning" in raw or first_thing:
        part = "morning"

    if re.search(r"\btomorrow\b", raw) and "day after tomorrow" not in raw:
        return WhenConstraint(target_date=call_day + timedelta(days=1), part_of_day=part, first_thing=first_thing)
    if "day after tomorrow" in raw:
        return WhenConstraint(target_date=call_day + timedelta(days=2), part_of_day=part, first_thing=first_thing)
    if "fortnight" in raw or "two weeks" in raw:
        return WhenConstraint(target_date=call_day + timedelta(days=14), part_of_day=part, first_thing=first_thing)
    if "week from today" in raw or "in a week" in raw or "a week today" in raw:
        return WhenConstraint(target_date=call_day + timedelta(days=7), part_of_day=part, first_thing=first_thing)

    named = _named_calendar_day(raw, call_day)
    weekday = next((name for name in WEEKDAYS if re.search(rf"\b{name}\b", raw)), None)

    if named:
        return WhenConstraint(target_date=named, weekday=None, part_of_day=part, first_thing=first_thing)
    if weekday:
        return WhenConstraint(
            target_date=_next_weekday(call_day, weekday),
            weekday=None,
            part_of_day=part,
            first_thing=first_thing,
        )
    return WhenConstraint(part_of_day=part, first_thing=first_thing)
