"""CLI for the offline oracle: `uv run python -m evals.corpus`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evals.corpus import (
    CASES_PER_RUN,
    PROBLEM_WEIGHTS,
    UNSCORED_PROBLEMS,
    coverage,
    max_score,
    roster,
    score,
    sha256,
)


def _actions(value: str) -> list[dict]:
    """Actions from inline JSON, or from a file when the argument starts with @."""
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8")
    payload = json.loads(value)
    if isinstance(payload, dict):
        payload = payload.get("actions") or [payload]
    return payload


def _actions_from_audit(path: str) -> list[dict]:
    """What one call submitted, read from our own audit trail.

    ``submission_attempt`` is written for every POST, retries included, so the
    same action can appear several times: identical payloads collapse to the one
    action the platform ends up holding, in the order the call decided them.
    """
    actions: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") != "submission_attempt":
            continue
        action = dict(record.get("payload") or {})
        action.pop("call_id", None)
        if not action.get("action"):
            action["action"] = record.get("verb")
        if action not in actions:
            actions.append(action)
    return actions


def _summary(cases: list[dict]) -> int:
    grouped = roster.by_problem(cases)
    print(f"{len(cases)} published cases · sha256 {sha256()[:12]}")
    print(f"board max {max_score()} points ({CASES_PER_RUN} cases per problem)")
    for problem_id in sorted(grouped):
        weight = PROBLEM_WEIGHTS.get(problem_id, 0)
        marker = " (not scored)" if problem_id in UNSCORED_PROBLEMS else ""
        print(f"  {problem_id:<20} {len(grouped[problem_id]):>2} cases · weight {weight}{marker}")
    return 0


def _coverage(cases: list[dict]) -> int:
    print(f"{'problem':<20} {'weight':>6} {'cases':>5} {'per run':>7}  expected endings")
    for row in coverage(cases):
        print(
            f"  {row['problem_id']:<18} {row['weight']:>6} {row['cases']:>5} "
            f"{row['per_run']:>7}  {', '.join(row['verbs'])}"
        )
    return 0


def _selfcheck(cases: list[dict]) -> int:
    """Every accepted answer must pass. A judge that fails its own roster is a lie."""
    failures = 0
    checked = 0
    for case in cases:
        for member in case["expected"]["acceptable"]:
            checked += 1
            verdict = score(case, member["actions"])
            if not verdict.passed:
                failures += 1
                print(verdict.report())
    print(f"{checked} accepted answers checked, {failures} rejected")
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score submissions against the published roster.")
    parser.add_argument("--fetch", action="store_true", help="refresh the cached roster")
    parser.add_argument("--summary", action="store_true", help="roster digest and shape")
    parser.add_argument("--coverage", action="store_true", help="where the points are")
    parser.add_argument("--selfcheck", action="store_true", help="judge the roster's own answers")
    parser.add_argument("--case", metavar="CASE_ID", help="score one submission for one case")
    parser.add_argument("--actions", metavar="JSON", help='the action list, or @file.json')
    parser.add_argument(
        "--audit",
        metavar="NDJSON",
        help="read what one call submitted from its audit trail (audit-logs/audit-<call>.ndjson)",
    )
    parser.add_argument("--print", action="store_true", help="print the actions instead of scoring")
    parser.add_argument("--list", metavar="PROBLEM", help="list the cases of one problem")
    args = parser.parse_args(argv)

    if args.fetch:
        document = roster.fetch()
        print(f"{len(document['cases'])} cases · sha256 {sha256()[:12]} → {roster.CACHE}")
        return 0

    try:
        cases = roster.load()
    except roster.RosterUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.list:
        for case in roster.by_problem(cases).get(args.list, []):
            print(f"  {case['id']}  {case['summary'][:80]}")
        return 0
    if args.coverage:
        return _coverage(cases)
    if args.selfcheck:
        return _selfcheck(cases)
    if args.actions is not None and args.audit is not None:
        print("give --actions or --audit, not both", file=sys.stderr)
        return 2

    if args.audit is not None:
        actions = _actions_from_audit(args.audit)
        if not actions:
            print(f"no submission attempts in {args.audit}", file=sys.stderr)
            return 2
        if args.case is None or args.print:
            print(json.dumps(actions, indent=2, ensure_ascii=False))
            return 0
    elif args.actions is not None:
        actions = _actions(args.actions)
        if args.print:
            print(json.dumps(actions, indent=2, ensure_ascii=False))
            return 0
    else:
        actions = None

    if args.case is None:
        if actions is None:
            return _summary(cases)
        print("--case needs the case to score against", file=sys.stderr)
        return 2
    case = roster.by_id(cases).get(args.case)
    if case is None:
        print(f"no such case: {args.case}", file=sys.stderr)
        return 2
    verdict = score(case, actions)
    print(verdict.report())
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main())
