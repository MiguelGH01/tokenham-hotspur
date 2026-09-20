"""ConvAI conversation list/detail mapped into CallHub events."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from observability.elevenlabs_history import (
    ElevenLabsConvAI,
    ElevenLabsHistory,
    conversation_to_events,
    conversation_to_summary,
    merge_detail,
    merge_summaries,
    prosper_call_id_from,
    reset_elevenlabs_history,
)
from observability.hub import reset_hub
from observability.store import reset_store
from voice_agent import IngestEventBody, ingest_obs_event


def _detail() -> dict:
    return {
        "conversation_id": "conv_abc",
        "status": "done",
        "metadata": {
            "start_time_unix_secs": 1_700_000_000,
            "call_duration_secs": 42,
            "conversation_initiation_source": "twilio",
        },
        "conversation_initiation_client_data": {
            "dynamic_variables": {"call_id": "CA-real", "caller_phone": "+34600111222"},
        },
        "transcript": [
            {
                "role": "agent",
                "time_in_call_secs": 1,
                "message": "Buenos días",
            },
            {
                "role": "user",
                "time_in_call_secs": 5,
                "message": "Quiero una cita",
            },
            {
                "role": "agent",
                "time_in_call_secs": 8,
                "message": "Un momento",
                "tool_calls": [
                    {
                        "request_id": "req-1",
                        "tool_name": "search-patient",
                        "params_as_json": '{"name": "Ana"}',
                        "tool_has_been_called": True,
                        "type": "webhook",
                    }
                ],
                "tool_results": [
                    {
                        "request_id": "req-1",
                        "tool_name": "search-patient",
                        "result_value": '{"matches": 1}',
                        "is_error": False,
                        "tool_has_been_called": True,
                    }
                ],
            },
            {
                "role": "agent",
                "time_in_call_secs": 20,
                "message": "Queda reservada.",
                "tool_calls": [
                    {
                        "request_id": "req-2",
                        "tool_name": "book",
                        "params_as_json": '{"patient_id": "P1"}',
                        "tool_has_been_called": True,
                        "type": "webhook",
                    }
                ],
                "tool_results": [
                    {
                        "request_id": "req-2",
                        "tool_name": "book",
                        "result_value": "ok",
                        "is_error": False,
                        "tool_has_been_called": True,
                    }
                ],
            },
        ],
    }


def test_prosper_call_id_from_dynamic_variables():
    assert prosper_call_id_from(_detail()) == "CA-real"


def test_conversation_to_events_includes_speech_and_tools():
    events = conversation_to_events(_detail(), "CA-real")
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "call.started"
    assert kinds[-1] == "call.ended"
    assert "transcript.user" in kinds
    assert "transcript.bot" in kinds
    assert kinds.count("tool.called") == 2
    assert kinds.count("tool.returned") == 2
    search = next(e for e in events if e["kind"] == "tool.called" and e["payload"]["name"] == "search-patient")
    assert search["payload"]["args"]["name"] == "Ana"
    assert search["payload"]["request_id"] == "req-1"
    book = next(e for e in events if e["kind"] == "tool.called" and e["payload"]["name"] == "book")
    assert book["payload"]["args"]["patient_id"] == "P1"
    book_back = next(e for e in events if e["kind"] == "tool.returned" and e["payload"]["name"] == "book")
    assert book_back["payload"]["status"] == "ok"
    assert not book_back["payload"].get("justification")
    started = events[0]
    assert started["payload"]["from_number"] == "+34600111222"
    assert started["payload"]["transport"] == "twilio"
    nodes = [e["payload"]["to"] for e in events if e["kind"] == "node.entered"]
    assert "identify" in nodes
    assert "confirm" in nodes


def test_conversation_to_summary_from_list_row():
    summary = conversation_to_summary(
        {
            "conversation_id": "conv_abc",
            "start_time_unix_secs": 1_700_000_000,
            "call_duration_secs": 42,
            "message_count": 4,
            "status": "done",
            "tool_names": ["search-patient", "book"],
            "conversation_initiation_source": "widget",
            "call_summary_title": "Booking for Ana",
        }
    )
    assert summary["call_id"] == "conv_abc"
    assert summary["status"] == "ended"
    assert summary["primary_action"] == "BOOK"
    assert summary["current_node"] == "confirm"
    assert summary["transport"] == "webrtc"
    assert summary["duration_ms"] == 42_000
    assert summary["call_summary_title"] == "Booking for Ana"
    assert summary["tool_names"] == ["search-patient", "book"]
    assert datetime.fromisoformat(summary["started_at"]).tzinfo == UTC


def test_conversation_to_events_split_turns_and_html_error():
    events = conversation_to_events(
        {
            "conversation_id": "conv_split",
            "status": "done",
            "metadata": {"start_time_unix_secs": 1_700_000_000, "call_duration_secs": 12},
            "transcript": [
                {
                    "role": "agent",
                    "time_in_call_secs": 2,
                    "message": None,
                    "tool_calls": [
                        {
                            "request_id": "req-html",
                            "tool_name": "search_patient",
                            "params_as_json": '{"name": "Ana"}',
                            "type": "webhook",
                        }
                    ],
                },
                {
                    "role": "agent",
                    "time_in_call_secs": 4,
                    "message": "",
                    "tool_results": [
                        {
                            "request_id": "req-html",
                            "tool_name": "search_patient",
                            "result_value": "<!DOCTYPE html><html>404 ngrok</html>",
                            "is_error": True,
                            "error_type": "timeout",
                        }
                    ],
                },
            ],
        },
        "conv_split",
    )
    kinds = [e["kind"] for e in events]
    assert kinds.count("tool.called") == 1
    assert kinds.count("tool.returned") == 1
    returned = next(e for e in events if e["kind"] == "tool.returned")
    assert returned["payload"]["status"] == "error"
    assert "failed" in returned["payload"]["justification"]
    assert "<html" not in returned["payload"]["justification"].lower()


def test_conversation_to_summary_extracts_tools_from_get_payload():
    summary = conversation_to_summary(_detail(), call_id="CA-real")
    assert summary["tool_names"] == ["search-patient", "book"]


def test_merge_summaries_prefers_local_call_id_and_submit():
    local = [
        {
            "call_id": "CA-real",
            "eleven_conversation_id": "conv_abc",
            "transport": "twilio",
            "from_number": "+34600111222",
            "started_at": "2023-11-14T22:13:20+00:00",
            "status": "ended",
            "submitted": True,
            "primary_action": "BOOK",
            "is_test": False,
            "patient_name": "Ana",
        }
    ]
    eleven = [
        {
            "conversation_id": "conv_abc",
            "start_time_unix_secs": 1_700_000_000,
            "call_duration_secs": 42,
            "status": "done",
            "tool_names": ["book"],
        },
        {
            "conversation_id": "conv_other",
            "start_time_unix_secs": 1_700_000_100,
            "call_duration_secs": 10,
            "status": "done",
            "tool_names": [],
        },
    ]
    merged = merge_summaries(local, eleven)
    by_id = {row["call_id"]: row for row in merged}
    assert "CA-real" in by_id
    assert by_id["CA-real"]["submitted"] is True
    assert by_id["CA-real"]["patient_name"] == "Ana"
    assert by_id["CA-real"]["from_number"] == "+34600111222"
    assert "conv_other" in by_id
    assert "conv_abc" not in by_id


def test_merge_summaries_collapses_unbound_webrtc_stub():
    local = [
        {
            "call_id": "CA-webrtc-deadbeef",
            "transport": "webrtc",
            "started_at": "2023-11-14T22:13:20+00:00",
            "ended_at": "2023-11-14T22:14:02+00:00",
            "status": "ended",
            "duration_ms": 42_000,
            "submitted": False,
            "is_test": True,
        }
    ]
    eleven = [
        {
            "conversation_id": "conv_abc",
            "start_time_unix_secs": 1_700_000_000,
            "call_duration_secs": 42,
            "status": "done",
            "call_summary_title": "New Patient Registration",
            "tool_names": [],
        }
    ]
    merged = merge_summaries(local, eleven)
    assert [row["call_id"] for row in merged] == ["conv_abc"]
    assert merged[0]["call_summary_title"] == "New Patient Registration"
    assert merged[0]["_bound_call_id"] == "CA-webrtc-deadbeef"
    assert merged[0]["is_test"] is True
    assert merged[0]["transport"] == "webrtc"


def test_merge_summaries_drops_poller_ghost_under_conv_id():
    local = [
        {
            "call_id": "CA-webrtc-deadbeef",
            "eleven_conversation_id": "conv_abc",
            "transport": "webrtc",
            "started_at": "2023-11-14T22:13:20+00:00",
            "ended_at": "2023-11-14T22:13:49+00:00",
            "status": "ended",
            "duration_ms": 29_000,
            "is_test": True,
        },
        {
            "call_id": "conv_abc",
            "transport": "unknown",
            "started_at": "2023-11-14T22:13:21+00:00",
            "status": "live",
            "is_test": False,
        },
    ]
    eleven = [
        {
            "conversation_id": "conv_abc",
            "start_time_unix_secs": 1_700_000_000,
            "call_duration_secs": 29,
            "status": "done",
            "call_summary_title": "New Patient Registration",
        }
    ]
    merged = merge_summaries(local, eleven)
    assert [row["call_id"] for row in merged] == ["CA-webrtc-deadbeef"]
    assert merged[0]["call_summary_title"] == "New Patient Registration"


def test_merge_summaries_collapses_live_stub_despite_clock_drift():
    local = [
        {
            "call_id": "CA-webrtc-stuck",
            "transport": "unknown",
            "started_at": "2023-11-14T22:13:20+00:00",
            "status": "live",
            "duration_ms": 1_209_000,
            "is_test": False,
        }
    ]
    eleven = [
        {
            "conversation_id": "conv_abc",
            "start_time_unix_secs": 1_700_000_000,
            "call_duration_secs": 29,
            "status": "done",
            "call_summary_title": "New Patient Registration",
        }
    ]
    merged = merge_summaries(local, eleven)
    assert [row["call_id"] for row in merged] == ["conv_abc"]
    assert merged[0]["_bound_call_id"] == "CA-webrtc-stuck"


def test_merge_detail_keeps_local_submit_and_uses_eleven_speech():
    el_events = conversation_to_events(_detail(), "CA-real")
    local = {
        "call": {
            "call_id": "CA-real",
            "transport": "twilio",
            "submitted": True,
            "primary_action": "BOOK",
            "status": "ended",
        },
        "events": [
            {
                "kind": "submit.posted",
                "call_id": "CA-real",
                "ts": "2023-11-14T22:13:40+00:00",
                "payload": {"action": "BOOK", "http_status": 200},
            }
        ],
        "actions": [{"verb": "BOOK", "status": "posted", "http_status": 200}],
    }
    summary = conversation_to_summary(_detail(), call_id="CA-real")
    summary["submitted"] = True
    summary["primary_action"] = "BOOK"
    detail = merge_detail(local, el_events, summary=summary)
    kinds = [e["kind"] for e in detail["events"]]
    assert "transcript.user" in kinds
    assert "tool.called" in kinds
    assert "submit.posted" in kinds
    assert detail["call"]["submitted"] is True
    assert detail["actions"][0]["verb"] == "BOOK"
    assert any(t["type"] == "utterance" for t in detail["timeline"])
    assert any(t["type"] == "tool" for t in detail["timeline"])


def test_client_list_and_get_with_mock_transport():
    detail = _detail()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/convai/conversations" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "conversations": [
                        {
                            "conversation_id": "conv_abc",
                            "start_time_unix_secs": 1_700_000_000,
                            "call_duration_secs": 42,
                            "status": "done",
                            "message_count": 4,
                            "tool_names": ["book"],
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.url.path == "/v1/convai/conversations/conv_abc":
            return httpx.Response(200, json=detail)
        return httpx.Response(404, json={"detail": "missing"})

    async def run():
        client = ElevenLabsConvAI(
            api_key="xi-test",
            agent_id="agent_test",
            transport=httpx.MockTransport(handler),
        )
        listed = await client.list_conversations()
        assert listed[0]["conversation_id"] == "conv_abc"
        got = await client.get_conversation("conv_abc")
        assert got["conversation_id"] == "conv_abc"
        await client.aclose()

    asyncio.run(run())


class _FakeConvAI:
    def __init__(self) -> None:
        self.after_unix = "unset"

    async def list_conversations(self, *, after_unix=None, max_pages=15):
        self.after_unix = after_unix
        return [
            {
                "conversation_id": "conv_overnight",
                "start_time_unix_secs": 1_700_000_000,
                "call_duration_secs": 12,
                "status": "done",
                "message_count": 4,
                "tool_names": ["submit_book"],
            }
        ]


def test_list_console_calls_includes_elevenlabs_history_outside_shift(tmp_path):
    async def run():
        store = await reset_store(tmp_path / "console.sqlite")
        fake = _FakeConvAI()
        history = ElevenLabsHistory(client=fake)
        listed = await history.list_console_calls(
            store, since="2026-09-20T06:00:00+00:00", include="all"
        )
        assert fake.after_unix is None
        assert listed[0]["call_id"] == "conv_overnight"
        assert listed[0]["primary_action"] == "BOOK"
        await store.close()

    asyncio.run(run())


def test_ingest_eleven_bound_stores_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_DB", str(tmp_path / "console.sqlite"))
    monkeypatch.setenv("VOICE_AGENT", "carloslabs")

    async def run():
        store = await reset_store(tmp_path / "console.sqlite")
        reset_hub(store)
        await ingest_obs_event(
            IngestEventBody(
                kind="call.started",
                call_id="CA-bound",
                payload={"transport": "twilio"},
            )
        )
        await ingest_obs_event(
            IngestEventBody(
                kind="eleven.bound",
                call_id="CA-bound",
                payload={"conversation_id": "conv_abc"},
            )
        )
        row = await store._get_call_row("CA-bound")
        assert row is not None
        assert row["eleven_conversation_id"] == "conv_abc"
        assert await store.call_id_for_eleven_conversation("conv_abc") == "CA-bound"
        await store.close()
        reset_elevenlabs_history(None)

    asyncio.run(run())


def test_ingest_eleven_bound_requires_conversation_id(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_DB", str(tmp_path / "console.sqlite"))

    async def run():
        store = await reset_store(tmp_path / "console.sqlite")
        reset_hub(store)
        with pytest.raises(Exception):
            await ingest_obs_event(
                IngestEventBody(kind="eleven.bound", call_id="CA-x", payload={})
            )
        await store.close()

    asyncio.run(run())
