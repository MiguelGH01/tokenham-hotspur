"""Suite-wide isolation from what a test run must never read or leave behind.

Running the suite used to write into ``server/audit-logs/`` — the directory a
scored run's forensics live in — so a test run could append to the evidence of a
call that was being scored, and did: fixture trails for ``call-1``, ``x`` and
``unknown`` sat next to the real per-call files. Auditing is off for the suite by
default; a test that asserts on it points ``AUDIT_DIR`` at its own ``tmp_path``.

The same hazard runs the other way for reception's notices. Whatever the front
desk has set right now changes which slots the agent offers, so a suite that
reads the real file passes or fails depending on what somebody typed into the
console — a closure set for next week moved an expected date and failed two
tests that had nothing to do with notices. The suite always runs as if there
were none; a test about notices points ``RECEPTION_NOTICES_PATH`` at its own.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", "")


@pytest.fixture(autouse=True)
def _isolate_voice_agent(monkeypatch):
    monkeypatch.setenv("VOICE_AGENT", "carloslabs")


@pytest.fixture(autouse=True)
def _no_live_notices(monkeypatch, tmp_path_factory):
    absent = tmp_path_factory.mktemp("notices") / "none.json"
    monkeypatch.setenv("RECEPTION_NOTICES_PATH", str(absent))
