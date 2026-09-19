"""Suite-wide isolation from what a test run must never leave behind.

Running the suite used to write into ``server/audit-logs/`` — the directory a
scored run's forensics live in — so a test run could append to the evidence of a
call that was being scored, and did: fixture trails for ``call-1``, ``x`` and
``unknown`` sat next to the real per-call files. Auditing is off for the suite by
default; a test that asserts on it points ``AUDIT_DIR`` at its own ``tmp_path``.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", "")
