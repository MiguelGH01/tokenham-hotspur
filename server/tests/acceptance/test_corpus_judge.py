"""The offline oracle: score a submission the way the platform does.

The leaderboard says pass or fail and nothing else, so a scored run is otherwise
undiagnosable. These tests pin the judge against the organisers' own published
answers — every accepted answer must pass, and every near miss must fail with the
field named — because a judge that disagrees with the scorer is worse than no
judge at all.
"""

import json
from datetime import datetime

import pytest

from evals.corpus import (
    CASES_PER_RUN,
    PROBLEM_WEIGHTS,
    UNSCORED_PROBLEMS,
    by_id,
    coverage,
    load,
    max_score,
    score,
)
from evals.corpus.normalize import (
    normalize_email,
    normalize_national_id,
    normalize_phone,
    normalize_slot,
)

CASES = load()
BOOK_CASE = by_id(CASES)["simple_booking-14a8720daa02"]
ACCEPTED_BOOK = BOOK_CASE["expected"]["acceptable"][0]["actions"][0]


def _register_case() -> dict:
    return next(case for case in CASES if case["problem_id"] == "the_new_patient")


def test_the_roster_is_committed_and_complete():
    assert len(CASES) == 73
    assert len({case["id"] for case in CASES}) == 73
    for case in CASES:
        assert case["expected"]["acceptable"], case["id"]


def test_every_accepted_answer_passes():
    """The harness tests itself: the judge must agree with every published answer."""
    checked = 0
    for case in CASES:
        for member in case["expected"]["acceptable"]:
            checked += 1
            verdict = score(case, member["actions"])
            assert verdict.passed, verdict.report()
    assert checked == 75


def test_a_wrong_slot_fails_with_the_field_named():
    wrong = dict(ACCEPTED_BOOK, slot="2026-09-19T11:15:00+02:00")
    verdict = score(BOOK_CASE, [wrong])
    assert not verdict.passed
    assert any("actions[0].slot" in problem for problem in verdict.problems)


def test_ids_compare_exactly():
    """`CR-exact-ids`: a provider id is not folded, cased or guessed."""
    wrong = dict(ACCEPTED_BOOK, provider_id=ACCEPTED_BOOK["provider_id"].lower())
    verdict = score(BOOK_CASE, [wrong])
    assert not verdict.passed
    assert any("provider_id" in problem for problem in verdict.problems)


def test_the_plan_is_submitted_as_its_id_not_its_label():
    """`CL-plan-display-names`: the label is not the answer."""
    wrong = dict(ACCEPTED_BOOK, policy_id="Mapfre Salud")
    assert not score(BOOK_CASE, [wrong]).passed


@pytest.mark.parametrize(
    "submitted,expected",
    [
        ("2026-09-19T11:00:07+02:00", "2026-09-19T11:00:00+02:00"),
        ("2026-09-19T09:00:00+00:00", "2026-09-19T11:00:00+02:00"),
    ],
)
def test_the_slot_is_the_exact_minute_in_madrid(submitted, expected):
    assert normalize_slot(submitted) == expected
    assert score(BOOK_CASE, [dict(ACCEPTED_BOOK, slot=submitted)]).passed


def test_the_submission_may_carry_fields_the_scorer_ignores():
    assert score(BOOK_CASE, [dict(ACCEPTED_BOOK, call_id="CA-123")]).passed


def test_a_register_is_accepted_flat_or_nested_and_folds_the_voice_edits():
    case = _register_case()
    member = case["expected"]["acceptable"][0]["actions"][0]
    patient = dict(member["new_patient"])
    # What a caller's audio produces: accents, a surname the other way round,
    # a spaced DNI, a country-coded phone, whitespace in the email.
    flat = {
        "action": "REGISTER",
        "given_name": patient["given_name"].upper(),
        "first_surname": patient["second_surname"],
        "second_surname": patient["first_surname"],
        "national_id": f"{patient['national_id'][:8]} {patient['national_id'][8]}",
        "date_of_birth": patient["date_of_birth"],
        "phone": f"+34 {patient['phone']}",
        "email": f"  {patient['email'].upper()}  ",
        "insurer": patient["insurer"].upper(),
    }
    assert score(case, [flat]).passed
    assert score(case, [member]).passed


def test_a_register_missing_a_digit_fails_on_that_field():
    case = _register_case()
    member = case["expected"]["acceptable"][0]["actions"][0]
    patient = dict(member["new_patient"])
    patient["phone"] = patient["phone"][:-1]
    verdict = score(case, [{"action": "REGISTER", "new_patient": patient}])
    assert not verdict.passed
    assert any("phone" in problem for problem in verdict.problems)


def test_the_closest_member_is_the_one_reported():
    """With several acceptable answers, the report explains the nearest miss."""
    case = next(
        case
        for case in CASES
        if len(case["expected"]["acceptable"]) > 1
        and len(case["expected"]["acceptable"][0]["actions"]) == 1
    )
    members = case["expected"]["acceptable"]
    first, second = members[0]["actions"][0], members[1]["actions"][0]
    differing = [key for key in first if first[key] != second.get(key)]
    assert differing, "two accepted members that differ in nothing are one member"
    key = differing[0]
    near_miss = dict(first, **{key: "nonsense"})
    verdict = score(case, [near_miss])
    assert not verdict.passed
    assert len(verdict.problems) == 1, verdict.report()


def test_every_published_problem_has_a_weight():
    """A problem the organisers add must not be scored at zero by accident."""
    published = {case["problem_id"] for case in CASES}
    assert published <= set(PROBLEM_WEIGHTS) | UNSCORED_PROBLEMS


def test_the_board_maximum_is_the_published_196():
    assert max_score() == 196
    assert sum(PROBLEM_WEIGHTS.values()) * CASES_PER_RUN == 196


def test_the_switchboard_is_published_but_never_dialled():
    """Problem 2 carries no weight and no published case (`SC-problem-2`)."""
    published = {case["problem_id"] for case in CASES}
    assert "switchboard" not in published
    assert all(row["per_run"] == CASES_PER_RUN for row in coverage(CASES))


@pytest.mark.parametrize(
    "submitted,expected",
    [
        ("12345678-Z", "12345678Z"),
        ("1234 5678 z", "12345678Z"),
        ("x-1234567-l", "X1234567L"),
    ],
)
def test_the_published_national_id_table(submitted, expected):
    assert normalize_national_id(submitted) == expected


@pytest.mark.parametrize(
    "submitted,expected",
    [
        ("+34 612 345 678", "612345678"),
        ("0034612345678", "612345678"),
        ("612-345-678", "612345678"),
    ],
)
def test_the_published_phone_table(submitted, expected):
    assert normalize_phone(submitted) == expected


@pytest.mark.parametrize(
    "submitted,expected",
    [
        ("Ana.Garcia@Gmail.com", "ana.garcia@gmail.com"),
        (" ana.garcia @ gmail.com ", "ana.garcia@gmail.com"),
    ],
)
def test_the_published_email_table(submitted, expected):
    assert normalize_email(submitted) == expected


def test_a_slot_without_an_offset_is_rejected_rather_than_guessed():
    """`CR-slot-tz`: the offset is required, so a naive timestamp is not a near miss."""
    with pytest.raises(ValueError):
        normalize_slot("2026-09-19 11:00")


def test_the_judge_reads_the_clock_it_is_given():
    """A slot is compared as an instant, so the same minute in another offset passes."""
    same_minute = datetime.fromisoformat(ACCEPTED_BOOK["slot"]).astimezone().isoformat()
    assert score(BOOK_CASE, [dict(ACCEPTED_BOOK, slot=same_minute)]).passed


def _audit(tmp_path, records: list[dict]) -> str:
    path = tmp_path / "audit-call.ndjson"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(path)


def test_a_call_trail_yields_exactly_what_it_submitted(tmp_path):
    from evals.corpus.__main__ import _actions_from_audit

    trail = _audit(
        tmp_path,
        [
            {"event": "offer_prepared", "offer_id": "offer-1"},
            {"event": "submission_attempt", "verb": "BOOK", "payload": dict(ACCEPTED_BOOK)},
        ],
    )
    assert _actions_from_audit(trail) == [ACCEPTED_BOOK]


def test_a_retried_action_collapses_to_the_one_the_platform_holds(tmp_path):
    from evals.corpus.__main__ import _actions_from_audit

    attempt = {"event": "submission_attempt", "verb": "BOOK", "payload": dict(ACCEPTED_BOOK)}
    trail = _audit(tmp_path, [attempt, attempt, attempt])
    assert _actions_from_audit(trail) == [ACCEPTED_BOOK]


def test_a_trail_can_be_scored_against_a_case(tmp_path):
    from evals.corpus.__main__ import main

    trail = _audit(
        tmp_path,
        [{"event": "submission_attempt", "verb": "BOOK", "payload": dict(ACCEPTED_BOOK)}],
    )
    assert main(["--case", BOOK_CASE["id"], "--audit", trail]) == 0


def test_a_trail_with_nothing_submitted_is_an_error(tmp_path, capsys):
    from evals.corpus.__main__ import main

    trail = _audit(tmp_path, [{"event": "offer_prepared", "offer_id": "offer-1"}])
    assert main(["--audit", trail]) == 2
    assert "no submission attempts" in capsys.readouterr().err
