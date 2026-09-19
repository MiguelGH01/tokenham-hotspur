"""Per-call decision audit: the forensics a scored run cannot give us.

A scored run reports only pass/fail and fixed signal codes, so a failing call is
otherwise unauditable: was the offer never prepared, was the plan frozen as a
refusal, did the platform reject the POST? This module appends one JSON object
per decision to a private NDJSON file per call, so any run can be reconstructed
afterwards from our side.

Privacy: the file stays local (never served, never sent anywhere) and carries
only decision data — action verbs, ids, reasons, delivery outcomes. It never
carries conversation transcripts.

Configuration: ``AUDIT_DIR`` selects the directory (default ``audit-logs`` in
the working directory); setting it to the empty string disables the log
entirely, which is what the test suite wants.
"""

import json
import os
import re
import threading
from datetime import datetime, timezone

from loguru import logger

_LOCK = threading.Lock()

#: A line longer than this is truncated field-by-field. Decisions are tiny;
#: only a pathological payload could approach the bound.
_MAX_LINE_BYTES = 8 * 1024


def _dir() -> str | None:
    return os.getenv("AUDIT_DIR", "audit-logs") or None


def _safe_call_id(call_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", call_id or "unknown")[:128] or "unknown"


def _truncate(value, depth: int = 0):
    """Bound a value for serialization: strings to 500 chars, containers shallowly."""
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:497] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 3:
        return str(value)[:200]
    if isinstance(value, dict):
        return {str(k)[:64]: _truncate(v, depth + 1) for k, v in list(value.items())[:32]}
    if isinstance(value, (list, tuple)):
        return [_truncate(v, depth + 1) for v in value[:32]]
    return str(value)[:200]


def audit(call_id: str, event: str, **fields) -> None:
    """Append one decision to the call's audit file, or fail silently."""
    directory = _dir()
    if directory is None:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "call_id": call_id,
        "event": event,
        **_truncate(fields),
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"audit-{_safe_call_id(call_id)}.ndjson")
        with _LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line[:_MAX_LINE_BYTES] + "\n")
    except Exception as exc:  # never let bookkeeping break a scored call
        logger.debug("audit write failed: {}", type(exc).__name__)
