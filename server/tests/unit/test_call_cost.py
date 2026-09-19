"""A call's cost in euros and seconds, read back from its audit trail."""

import json

import pytest

import call_cost

START = "2026-09-19T10:00:00+00:00"


def _write(directory, call_id, events):
    lines = [json.dumps({"call_id": call_id, **e}) for e in events]
    (directory / f"audit-{call_id}.ndjson").write_text("\n".join(lines) + "\n")


def _call(llm_model="gemini-3.6-flash", ended="2026-09-19T10:01:30+00:00"):
    return [
        {"ts": START, "event": "call_started"},
        {"ts": START, "event": "service_usage", "kind": "stt", "model": "stt-rt-v5", "audio_seconds": 1800.0},
        {"ts": START, "event": "service_usage", "kind": "stt", "model": "stt-rt-v5", "audio_seconds": 1800.0},
        {"ts": START, "event": "service_usage", "kind": "llm", "model": llm_model,
         "prompt_tokens": 1_000_000, "cache_read_input_tokens": 400_000, "completion_tokens": 100_000},
        {"ts": START, "event": "service_usage", "kind": "tts", "model": "eleven_v3", "characters": 10_000},
        {"ts": START, "event": "service_latency", "kind": "ttfat", "seconds": 0.8},
        {"ts": START, "event": "service_latency", "kind": "ttfat", "seconds": 0.4},
        {"ts": START, "event": "service_latency", "kind": "ttfat", "seconds": 0.6},
        {"ts": START, "event": "plan_set", "verb": "BOOK"},  # decision events are not usage
        {"ts": ended, "event": "call_ended", "reason": "client_disconnected"},
    ]


def test_a_call_costs_what_its_services_list_prices_add_up_to(tmp_path):
    _write(tmp_path, "call-a", _call())
    (call,) = call_cost.load(tmp_path)

    # Gemini reports prompt tokens gross of cache: 600k uncached + 400k cached + 100k out.
    llm_usd = 0.6 * 0.75 + 0.4 * 0.075 + 0.1 * 3.75
    stt_usd = 0.12  # one hour of audio, summed across two reports
    tts_usd = 1.00  # 10k characters of eleven_v3
    assert call.eur == pytest.approx((llm_usd + stt_usd + tts_usd) / call_cost.USD_PER_EUR)
    assert call.duration_s == 90.0
    assert call.llm_ttfat_p50_s == 0.6
    assert call.unpriced == ()


def test_net_and_gross_cache_reporting_cost_the_same():
    price = {"input": 3.0, "cache_read": 0.3, "cache_write": 3.75, "output": 15.0}
    tokens = {"cache_read_input_tokens": 400_000, "cache_creation_input_tokens": 100_000,
              "completion_tokens": 100_000}
    gross = call_cost.llm_usd({**tokens, "prompt_tokens": 1_100_000}, {**price, "prompt_includes_cache": True})
    net = call_cost.llm_usd({**tokens, "prompt_tokens": 600_000}, {**price, "prompt_includes_cache": False})
    assert gross == pytest.approx(net) == pytest.approx(0.6 * 3.0 + 0.4 * 0.3 + 0.1 * 3.75 + 0.1 * 15.0)


def test_cache_counts_outside_the_prompt_never_become_a_discount():
    price = {"input": 3.0, "cache_read": 0.3, "output": 15.0, "prompt_includes_cache": True}
    usage = {"prompt_tokens": 100, "cache_read_input_tokens": 1_000_000, "completion_tokens": 0}
    assert call_cost.llm_usd(usage, price) == pytest.approx(0.3)  # the cache read, no negative input


def test_a_model_without_a_price_is_named_not_counted_as_free(tmp_path):
    _write(tmp_path, "call-a", _call())
    _write(tmp_path, "call-b", _call(llm_model="mystery-1", ended="2026-09-19T10:10:00+00:00"))
    calls = {c.call_id: c for c in call_cost.load(tmp_path)}
    assert calls["call-b"].eur is None
    assert calls["call-b"].unpriced == ("llm:mystery-1",)

    report = call_cost.report(tmp_path)
    assert "UNPRICED" in report and "llm:mystery-1" in report
    # The unpriced call stays out of the euro aggregates, but its duration is still known.
    assert "over 1 priced calls" in report and "over 2 timed calls" in report
    assert f"{calls['call-a'].eur:.4f}" in report.split("p50")[1]


def test_deepseek_is_priced_at_its_own_api_peak_rate(tmp_path):
    usage = {"ts": START, "event": "service_usage", "kind": "llm", "model": "deepseek-v4-flash",
             "prompt_tokens": 900_000, "cache_read_input_tokens": 300_000, "completion_tokens": 100_000}
    _write(tmp_path, "call-a", [{"ts": START, "event": "call_started"}, usage])
    (call,) = call_cost.load(tmp_path)
    # OpenAI-compatible, so prompt tokens are gross: 600k cache miss + 300k cache hit + 100k out.
    usd = 0.6 * 0.30 + 0.3 * 0.006 + 0.1 * 1.20
    assert call.eur == pytest.approx(usd / call_cost.USD_PER_EUR)
    assert call.unpriced == ()


def test_a_call_that_never_ended_has_a_cost_but_no_duration(tmp_path):
    _write(tmp_path, "call-a", [e for e in _call() if e["event"] != "call_ended"])
    (call,) = call_cost.load(tmp_path)
    assert call.duration_s is None and call.eur is not None


def test_percentile_is_nearest_rank():
    assert call_cost.percentile([60.0, 120.0], 50) == 60.0
    assert call_cost.percentile([60.0, 120.0], 95) == 120.0
    assert call_cost.percentile([5.0], 95) == 5.0


def test_an_empty_directory_reports_no_calls(tmp_path):
    assert "0 calls" in call_cost.report(tmp_path)
