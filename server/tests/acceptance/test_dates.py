from datetime import date, datetime, timedelta, timezone

from clinic.clinic_catalog import is_closed_day, next_open_day
from dates import parse_when

MADRID = timezone(timedelta(hours=2))
CONNECTED = datetime(2026, 9, 18, 10, 0, tzinfo=MADRID)


def test_tomorrow_is_saturday_after_friday_call():
    when = parse_when("tomorrow, hay fever", CONNECTED)
    assert when.target_date == date(2026, 9, 19)


def test_this_coming_thursday_skips_this_week():
    when = parse_when("this coming Thursday", CONNECTED)
    assert when.target_date == date(2026, 9, 24)


def test_saturday_morning():
    when = parse_when("on Saturday morning", CONNECTED)
    assert when.target_date == date(2026, 9, 19)
    assert when.part_of_day == "morning"


def test_coming_sunday_is_the_20th():
    when = parse_when("this coming Sunday at Arenal Centro", CONNECTED)
    assert when.target_date == date(2026, 9, 20)


def test_first_thing_twelfth_of_october():
    when = parse_when("first thing on Monday the twelfth of October", CONNECTED)
    assert when.target_date == date(2026, 10, 12)
    assert when.first_thing is True
    assert when.part_of_day == "morning"


def test_sunday_rolls_to_monday_at_centro():
    assert next_open_day(date(2026, 9, 20), "centro", date(2026, 10, 2)) == date(2026, 9, 21)


def test_fiesta_rolls_to_tuesday():
    assert is_closed_day(date(2026, 10, 12), "centro")
    assert next_open_day(date(2026, 10, 12), "centro", date(2026, 10, 16)) == date(2026, 10, 13)
