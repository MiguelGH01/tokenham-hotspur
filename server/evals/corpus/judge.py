"""Score a submission the way the platform does: membership, binary, no partial credit.

``SC-membership``: a case passes when the submitted list of actions matches *any*
member of the case's acceptable set, after normalization. Nothing else scores —
not most of a name, not a slot one minute out, not three fields of four
(``SC-binary``). So this module answers one question and reports the field-level
diff that explains a failure, never a percentage.

The weights come from ``docs/requirements/06-problems.md`` and are asserted
against the roster by the tests, so a problem the organisers add cannot be scored
at weight zero by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from evals.corpus.normalize import normalize_actions

#: `problem_id` → difficulty weight, from the requirements table.
PROBLEM_WEIGHTS: dict[str, int] = {
    "simple_booking": 1,
    "doctor_and_site": 2,
    "the_new_patient": 2,
    "when_exactly": 2,
    "no_slot_free": 2,
    "change_and_cancel": 2,
    "the_rules": 3,
    "third_party": 3,
    "triage": 3,
    "languages": 3,
    "noise": 3,
    "nearest_site": 3,
    "the_questions": 3,
    "difficult_caller": 4,
    "adversarial": 4,
    "second_policy": 4,
    "the_real_call": 5,
}

#: Cases per problem in one scored run (`SC-run-all`): four private cases each.
CASES_PER_RUN = 4

#: No scored run dials problem 2 (`SC-problem-2`).
UNSCORED_PROBLEMS = frozenset({"switchboard"})


@dataclass(frozen=True)
class Verdict:
    """What the scorer would say about one submission for one case."""

    case_id: str
    passed: bool
    member_index: int | None = None
    problems: list[str] = field(default_factory=list)

    def report(self) -> str:
        if self.passed:
            return f"PASS {self.case_id} (accepted member {self.member_index})"
        lines = [f"FAIL {self.case_id}"]
        lines += [f"  - {problem}" for problem in self.problems]
        return "\n".join(lines)


def _diff(got: list[dict], want: list[dict]) -> list[str]:
    """The reasons ``got`` is not ``want``. Empty means it is."""
    if len(got) != len(want):
        return [f"actions: expected {len(want)} action(s), got {len(got)}"]
    problems: list[str] = []
    for index, (submitted, expected) in enumerate(zip(got, want)):
        if submitted.get("action") != expected.get("action"):
            problems.append(
                f"actions[{index}].action: expected {expected.get('action')}, "
                f"got {submitted.get('action')}"
            )
            continue
        for key, value in expected.items():
            if submitted.get(key) != value:
                problems.append(
                    f"actions[{index}].{key}: expected {value!r}, got {submitted.get(key)!r}"
                )
    return problems


def judge(case: dict, actions: list[dict]) -> Verdict:
    """Score one submitted action list against one published case."""
    got = normalize_actions(actions)
    closest: list[str] = []
    for index, member in enumerate(case["expected"]["acceptable"]):
        problems = _diff(got, normalize_actions(member["actions"]))
        if not problems:
            return Verdict(case["id"], True, index)
        if not closest or len(problems) < len(closest):
            closest = problems
    return Verdict(case["id"], False, None, closest)


def points_at_stake(case: dict) -> int:
    """What one case of this problem is worth on the board: its weight."""
    return PROBLEM_WEIGHTS.get(case["problem_id"], 0)


def coverage(cases: list[dict]) -> list[dict]:
    """Where the points are, per problem: weight, published cases, expected endings."""
    rows = []
    for problem_id in sorted({case["problem_id"] for case in cases}):
        problem_cases = [case for case in cases if case["problem_id"] == problem_id]
        verbs = sorted(
            {
                action["action"]
                for case in problem_cases
                for member in case["expected"]["acceptable"]
                for action in member["actions"]
            }
        )
        rows.append(
            {
                "problem_id": problem_id,
                "weight": PROBLEM_WEIGHTS.get(problem_id, 0),
                "cases": len(problem_cases),
                "per_run": 0 if problem_id in UNSCORED_PROBLEMS else CASES_PER_RUN,
                "verbs": verbs,
            }
        )
    return rows


def max_score() -> int:
    """The whole board: every scored problem, four cases each, at its weight."""
    return sum(
        weight * CASES_PER_RUN
        for problem, weight in PROBLEM_WEIGHTS.items()
        if problem not in UNSCORED_PROBLEMS
    )
