"""Score our calls against the organisers' own published cases.

The leaderboard scores a run every ~30 minutes and reports a verdict, an
attribution and a fixed signal code — never which field was lost. This package
measures here instead: the published roster carries the accepted action lists,
so a submission can be checked in milliseconds, with a field-level diff.

    uv run python -m evals.corpus --summary       # roster digest and shape
    uv run python -m evals.corpus --coverage      # where the points are
    uv run python -m evals.corpus --selfcheck     # the judge against the roster itself
    uv run python -m evals.corpus --case <case-id> --actions '[...]'
    uv run python -m evals.corpus --case <case-id> --audit audit-logs/audit-<call>.ndjson

See `docs/scoring-design-notes.md`.
"""

from evals.corpus.judge import (
    CASES_PER_RUN,
    PROBLEM_WEIGHTS,
    UNSCORED_PROBLEMS,
    Verdict,
    coverage,
    max_score,
    points_at_stake,
)
from evals.corpus.judge import judge as score
from evals.corpus.roster import (
    CACHE,
    RosterUnavailable,
    by_id,
    by_problem,
    fetch,
    load,
    sha256,
)

#: ``score`` is ``judge.judge``: exported under a name that does not shadow the
#: module it lives in.
__all__ = [
    "CACHE",
    "CASES_PER_RUN",
    "PROBLEM_WEIGHTS",
    "RosterUnavailable",
    "UNSCORED_PROBLEMS",
    "Verdict",
    "by_id",
    "by_problem",
    "coverage",
    "fetch",
    "load",
    "max_score",
    "points_at_stake",
    "score",
    "sha256",
]
