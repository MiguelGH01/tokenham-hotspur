"""Normalization parity with the scorer, where a human voice was in the loop.

The scorer folds demographics with NFKD accent-stripping, whitespace removal
and case folding; the API re-derives the DNI/NIE check letter and answers 422
on a mismatch. Submitting an unforgiven form wastes a scored call, so these
regressions pin the tolerance the scorer grants.
"""

import pytest

from flows.registration import validate_registration
from national_id import is_valid_national_id, normalize_national_id
from datetime import datetime

BASE = {
    "given_name": "Ana",
    "first_surname": "García",
    "second_surname": "López",
    "national_id": "12345678Z",
    "date_of_birth": "1988-03-14",
    "phone": "+34612345678",
    "email": "ana.garcia@gmail.com",
    "insurer": "mapfre",
}
NOW = datetime(2026, 9, 19, 10, 0)


def _validated(**overrides):
    values = {**BASE, **overrides}
    patient, errors = validate_registration(values, NOW)
    return patient, errors


def test_email_strips_internal_whitespace_like_the_scorer():
    patient, errors = _validated(email="ana.garcia @ gmail.com")
    assert errors == []
    assert patient["email"] == "ana.garcia@gmail.com"


def test_email_is_case_folded():
    patient, errors = _validated(email="Ana.Garcia@Gmail.com")
    assert errors == []
    assert patient["email"] == "ana.garcia@gmail.com"


@pytest.mark.parametrize(
    "submitted,normalized",
    [
        ("12345678-Z", "12345678Z"),
        ("12345678z", "12345678Z"),
        ("1234 5678 Z", "12345678Z"),
        ("x-1234567-l", "X1234567L"),
        (" 12345678-z ", "12345678Z"),
    ],
)
def test_national_id_normalization(submitted, normalized):
    assert normalize_national_id(submitted) == normalized


@pytest.mark.parametrize(
    "submitted,valid",
    [
        ("12345678Z", True),
        ("12345678A", False),  # right shape, wrong letter
        ("X1234567L", True),   # a NIE validates like a DNI
        ("1234567L", False),   # too short
    ],
)
def test_national_id_check_letter(submitted, valid):
    assert is_valid_national_id(submitted) is valid


@pytest.mark.parametrize(
    "phone",
    ["+34 612 345 678", "0034612345678", "612-345-678", "612345678"],
)
def test_phone_forms_are_accepted(phone):
    _, errors = _validated(phone=phone)
    assert "phone" not in errors


def test_bad_check_letter_is_rejected_before_any_submit():
    _, errors = _validated(national_id="12345678A")
    assert "national_id" in errors
