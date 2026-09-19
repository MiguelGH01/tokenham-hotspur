"""PR-05's calendar law, tested as arithmetic rather than as conversation.

The public cases assume the call connects on Friday 18 September 2026. These
tests pin that instant so the assertions are about the rule, not about today:
"tomorrow" resolving to a Saturday stays true whatever day the suite runs.
"""

from datetime import date, datetime

import pytest

import dates
from clinic_catalog import load_catalog

#: The instant the published PR-05 cases assume: Friday 18 September 2026.
CALL = datetime.fromisoformat("2026-09-18T10:00:00+02:00")
CALL_DAY = date(2026, 9, 18)
#: Fiesta Nacional, the calendar's one closure day.
FIESTA = date(2026, 10, 12)


def test_tomorrow_can_be_saturday():
    """PR-05-S1: the weekend is not skipped."""
    assert dates.resolve_relative("tomorrow", CALL_DAY) == date(2026, 9, 19)
    assert dates.resolve_relative("tomorrow", CALL_DAY).weekday() == 5


def test_every_named_relative_phrase_has_the_offset_it_says():
    assert dates.resolve_relative("day_after_tomorrow", CALL_DAY) == date(2026, 9, 20)
    assert dates.resolve_relative("in_a_week", CALL_DAY) == date(2026, 9, 25)
    assert dates.resolve_relative("in_a_fortnight", CALL_DAY) == date(2026, 10, 2)


def test_this_coming_weekday_is_strictly_after_the_call_day():
    """PR-05-S2: Friday's "this coming Thursday" is the 24th, not yesterday's 17th."""
    assert dates.next_weekday(CALL_DAY, "thursday") == date(2026, 9, 24)
    assert dates.next_weekday(CALL_DAY, "friday") == date(2026, 9, 25)


def test_a_named_date_resolves_to_its_calendar_year():
    """PR-05-S5: "Monday the twelfth of October" is 2026-10-12."""
    assert dates.resolve_named("monday", 12, "october", None, CALL_DAY) == FIESTA
    assert dates.resolve_named("monday", 12, 10, None, CALL_DAY) == FIESTA


def test_a_named_date_already_past_means_next_year():
    assert dates.resolve_named("monday", 5, "january", None, CALL_DAY) == date(2027, 1, 5)


def test_rolling_off_a_closure_keeps_the_rest_of_the_ask():
    """PR-05-S5: the Fiesta rolls to Tuesday 13 October, not to the next Monday."""
    rolled = dates.next_open_day(FIESTA)
    assert rolled == date(2026, 10, 13)
    assert rolled.weekday() == 1


def test_sunday_rolls_forward_because_no_site_opens():
    """PR-05-S4: nothing opens Sunday, and the roll must not jump a whole week."""
    assert dates.is_closed(date(2026, 9, 20))
    assert dates.next_open_day(date(2026, 9, 20)) == date(2026, 9, 21)


def test_a_site_shut_day_rolls_for_that_site_only():
    """Sur closes Friday afternoons and closes at weekends; Centro opens Saturday."""
    saturday = date(2026, 9, 19)
    assert dates._site_opens(saturday, "centro") is True
    assert dates._site_opens(saturday, "sur") is False
    assert dates.next_open_day(saturday, "sur") == date(2026, 9, 21)


def test_window_is_capped_by_span_but_clamped_to_the_calendar():
    """A 422 mid-call reads as "no appointments", so the window is always legal."""
    catalogue = load_catalog()["calendar"]
    first_allowed = date.fromisoformat(catalogue["starts"])
    last_allowed = date.fromisoformat(catalogue["ends"])
    for start in (date(2026, 9, 1), date(2026, 9, 18), FIESTA, date(2026, 10, 30)):
        from_date, to_date = dates.window_around(start)
        start_day, end_day = date.fromisoformat(from_date), date.fromisoformat(to_date)
        assert end_day >= start_day
        assert (end_day - start_day).days + 1 <= dates.max_span_days()
        assert end_day <= last_allowed
        assert start_day >= first_allowed or start > last_allowed
    # A date inside the window keeps its own day as the first day searched.
    from_date, _ = dates.window_around(FIESTA)
    assert date.fromisoformat(from_date) == FIESTA


def test_unknown_phrase_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        dates.resolve_relative("next tuesday-ish", CALL_DAY)
