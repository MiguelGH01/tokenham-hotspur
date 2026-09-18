"""Local DNI/NIE check-letter validation (no API call needed to reject a misheard id)."""

import re

CHECK_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
NIE_PREFIX = {"X": "0", "Y": "1", "Z": "2"}
DNI_RE = re.compile(r"^(\d{8})([A-Z])$")
NIE_RE = re.compile(r"^([XYZ])(\d{7})([A-Z])$")


def normalize_national_id(value: str) -> str:
    return re.sub(r"[\s\-]", "", value).upper()


def is_valid_national_id(value: str) -> bool:
    value = normalize_national_id(value)
    if m := DNI_RE.match(value):
        digits, letter = m.groups()
    elif m := NIE_RE.match(value):
        prefix, rest, letter = m.groups()
        digits = NIE_PREFIX[prefix] + rest
    else:
        return False
    return CHECK_LETTERS[int(digits) % 23] == letter
