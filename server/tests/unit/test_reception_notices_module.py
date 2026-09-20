"""Scaffolding for reception_notices: the parsing and date edges, without the flow."""

import json
from datetime import date

import reception_notices as rn

ABSENCE = {
    "id": "n1",
    "kind": "provider_absent",
    "provider_id": "PR03",
    "from": "2026-09-22",
    "until": "2026-09-24",
}


def _load(tmp_path, monkeypatch, raw):
    path = tmp_path / "notices.json"
    path.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("RECEPTION_NOTICES_PATH", str(path))
    return rn.load_notices()


def _slot(provider_id, day):
    return {
        "provider_id": provider_id,
        "location_id": "centro",
        "appointment_type_id": "review",
        "start_time": f"{day}T09:00:00+02:00",
    }


def test_absence_edges_are_inclusive(tmp_path, monkeypatch):
    notices = _load(tmp_path, monkeypatch, {"notices": [ABSENCE]})
    availability = {
        "slots": [_slot("PR03", d) for d in ("2026-09-21", "2026-09-22", "2026-09-24", "2026-09-25")]
    }

    shown, applied = rn.hide_absent(availability, notices)

    assert [s["start_time"][:10] for s in shown["slots"]] == ["2026-09-21", "2026-09-25"]
    assert applied == ["n1"]


def test_an_absence_that_removes_nothing_is_not_reported(tmp_path, monkeypatch):
    """Only a notice that actually changed the offer earns a line in the trail."""
    notices = _load(tmp_path, monkeypatch, {"notices": [ABSENCE]})

    shown, applied = rn.hide_absent({"slots": [_slot("PR07", "2026-09-23")]}, notices)

    assert applied == []
    assert len(shown["slots"]) == 1


def test_a_closure_expands_to_every_day_it_covers(tmp_path, monkeypatch):
    closed = {"id": "c", "kind": "clinic_closed", "from": "2026-09-24", "until": "2026-09-26"}
    notices = _load(tmp_path, monkeypatch, {"notices": [closed]})

    assert rn.closed_days(notices) == ["2026-09-24", "2026-09-25", "2026-09-26"]


def test_a_spoken_notice_without_a_site_covers_them_all(tmp_path, monkeypatch):
    everywhere = {
        "id": "s",
        "kind": "spoken",
        "text": "Traiga su tarjeta.",
        "location_id": None,
        "from": "2026-09-19",
        "until": "2026-09-24",
    }
    notices = _load(tmp_path, monkeypatch, {"notices": [everywhere]})

    for site in ("centro", "norte", "sur"):
        assert rn.spoken_for(notices, site, date(2026, 9, 22)) == ["Traiga su tarjeta."]


def test_dropped_insurers_groups_by_provider(tmp_path, monkeypatch):
    def drop(notice_id, insurer, until):
        return {
            "id": notice_id,
            "kind": "insurer_dropped",
            "provider_id": "PR07",
            "insurer_id": insurer,
            "from": "2026-09-01",
            "until": until,
        }

    notices = _load(
        tmp_path,
        monkeypatch,
        {"notices": [drop("a", "dkv", "2026-12-31"), drop("b", "asisa", "2026-09-10")]},
    )

    assert rn.dropped_insurers(notices, date(2026, 9, 20)) == {"PR07": ["dkv"]}


def test_a_reversed_range_and_an_absurd_span_are_refused(tmp_path, monkeypatch):
    raw = {
        "notices": [
            {**ABSENCE, "id": "backwards", "from": "2026-09-24", "until": "2026-09-22"},
            {**ABSENCE, "id": "forever", "from": "2026-09-22", "until": "2126-09-22"},
        ]
    }
    _, errors = rn.validate_notices(raw)

    assert sorted(e["id"] for e in errors) == ["backwards", "forever"]


def test_a_bad_tone_does_not_take_the_notices_with_it(tmp_path, monkeypatch):
    notices = _load(
        tmp_path, monkeypatch, {"tone": {"text": "x" * 201, "until": "2026-09-30"}, "notices": [ABSENCE]}
    )

    assert rn.tone_for(notices, date(2026, 9, 19)) is None
    assert len(notices.entries) == 1
