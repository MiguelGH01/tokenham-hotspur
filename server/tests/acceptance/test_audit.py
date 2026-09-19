"""The per-call audit log: what a scored run cannot show us must be reconstructable.

The log is private bookkeeping, so the contract here is narrow: one JSON object
per line, decisions only, and never an exception into the caller — a broken
audit must not cost a scored case.
"""

import json

import audit as audit_module
from audit import audit


def test_writes_one_json_object_per_line(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    audit("call-1", "offer_prepared", provider_id="PR01", slot="2026-09-20T09:00:00+02:00")
    audit("call-1", "plan_set", verb="BOOK", provisional=False)
    path = tmp_path / "audit-call-1.ndjson"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "offer_prepared"
    assert first["provider_id"] == "PR01"
    assert first["call_id"] == "call-1"
    assert "ts" in first


def test_call_id_is_sanitized_for_the_filesystem(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    audit("../../etc/passwd", "plan_set", verb="BOOK")
    assert (tmp_path / "audit-.._.._etc_passwd.ndjson").exists()


def test_empty_audit_dir_disables_the_log(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", "")
    audit("call-1", "plan_set", verb="BOOK")
    assert list(tmp_path.iterdir()) == []


def test_truncates_oversized_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    audit("call-1", "plan_set", payload={"x": "y" * 5000})
    lines = (tmp_path / "audit-call-1.ndjson").read_text(encoding="utf-8").strip().splitlines()
    assert max(len(line) for line in lines) < audit_module._MAX_LINE_BYTES


def test_never_raises_into_the_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    monkeypatch.setattr(audit_module.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    audit("call-1", "plan_set", verb="BOOK")  # must not raise
