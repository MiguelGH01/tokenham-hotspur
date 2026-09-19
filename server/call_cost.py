"""What a call cost, in euros and seconds, read back from its audit trail.

``call_metrics`` appends what each service consumed to the call's audit file;
this prices it. The numbers are PUBLIC LIST PRICES on the date below, not an
invoice: a plan, a promotion or a negotiated rate would move them. Every price
carries the page it was read from so it can be checked by eye.

A model with no price here is never counted as free: the call is reported
``UNPRICED``, names what is missing, and stays out of the aggregates.

    uv run python -m call_cost [audit-dir]
"""

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PRICES_AS_OF = "2026-09-19"

#: ECB euro reference rate for 2026-09-18: 1 EUR buys this many USD.
#: https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml
USD_PER_EUR = 1.1460

_M = 1_000_000

#: Helmcode Starter: 399 EUR / 5,000 M tokens, in USD per 1M tokens like the rest.
_HELMCODE_USD_PER_M = 399 / 5_000 * USD_PER_EUR

#: LLM prices in USD per 1M tokens. ``prompt_includes_cache`` records how pipecat's
#: service reports ``prompt_tokens``: Google and OpenAI-compatible services report
#: it gross (cached tokens inside it), Anthropic reports it net. Pricing both the
#: same way bills the cache twice. ``cache_write`` defaults to the input rate.
LLM_PRICES = {
    # https://ai.google.dev/gemini-api/docs/pricing — paid tier, Standard.
    # Doubles on 2027-01-01 (1.50 / 0.15 / 7.50).
    "gemini-3.6-flash": {
        "input": 0.75, "cache_read": 0.075, "output": 3.75, "prompt_includes_cache": True,
    },
    # https://platform.claude.com/docs/en/about-claude/pricing — Cloudflare's gateway
    # passes provider rates through with no markup (5% fee on credit purchases, not here).
    "anthropic/claude-sonnet-4.6": {
        "input": 3.0, "cache_read": 0.30, "cache_write": 3.75, "output": 15.0,
        "prompt_includes_cache": False,
    },
    # https://helmcode.com/pricing — Helmcode bills a flat fee per key, not per token:
    # Starter is 399 EUR/month for 5B tokens, input and output alike. Every plan lists the
    # same models, so Starter is the cheapest one that carries this model. The team itself
    # runs on free hackathon tokens: this is what a clinic would pay, not our bill. It is that fee
    # spread over the full quota, so it is a FLOOR: a key that uses 1% of its quota pays
    # 100x this per token. Reported as amortized, never as a list price (see AMORTIZED).
    "deepseek-v4-flash": {
        "input": _HELMCODE_USD_PER_M, "cache_read": _HELMCODE_USD_PER_M,
        "output": _HELMCODE_USD_PER_M, "prompt_includes_cache": True,
    },
    # Deliberately absent: gpt-4.1 (bot.py runs it on the priority tier, whose price two
    # readings of the page disagreed on).
}

#: Models priced by spreading a flat fee over its quota rather than from a per-unit price.
AMORTIZED = {"deepseek-v4-flash"}

#: USD per second of audio submitted. https://soniox.com/pricing — $0.12/hour, real-time.
#: Deliberately absent: Deepgram nova-3-general (its streaming rate is a promotion with
#: no published end date), so STT_PROVIDER=deepgram reports every call UNPRICED.
STT_PRICES = {"stt-rt-v5": 0.12 / 3600}

#: USD per character. https://elevenlabs.io/pricing/api — billed in dollars, not credits.
TTS_PRICES = {
    "eleven_v3": 0.10 / 1000,
    "eleven_v3_conversational": 0.05 / 1000,
    "eleven_flash_v2_5": 0.05 / 1000,
}


@dataclass(frozen=True)
class CallCost:
    call_id: str
    duration_s: float | None  # None when the trail has no call_started / call_ended pair
    eur: float | None  # None when any usage could not be priced
    llm_ttfat_p50_s: float | None
    unpriced: tuple[str, ...]
    amortized: bool = False  # some of the cost is a flat fee spread over its quota: a floor


def llm_usd(usage: dict, price: dict) -> float:
    """Price one LLM usage report, whichever way its service counts cached tokens."""
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    uncached = usage.get("prompt_tokens") or 0
    if price["prompt_includes_cache"]:
        # Floored: a gateway that reports cache counts outside prompt_tokens must not
        # turn the input leg into a discount.
        uncached = max(0, uncached - (cache_read + cache_write))
    # ponytail: reasoning tokens are priced as output only where the service already
    # folds them into completion_tokens; the bot runs with reasoning off. Revisit if not.
    return (
        uncached * price["input"]
        + cache_read * price["cache_read"]
        + cache_write * price.get("cache_write", price["input"])
        + (usage.get("completion_tokens") or 0) * price["output"]
    ) / _M


def _usage_usd(usage: dict) -> float | None:
    kind, model = usage.get("kind"), usage.get("model")
    if kind == "llm" and model in LLM_PRICES:
        return llm_usd(usage, LLM_PRICES[model])
    if kind == "stt" and model in STT_PRICES:
        return (usage.get("audio_seconds") or 0) * STT_PRICES[model]
    if kind == "tts" and model in TTS_PRICES:
        return (usage.get("characters") or 0) * TTS_PRICES[model]
    return None


def _ts(events: list[dict], event: str) -> datetime | None:
    stamps = [e["ts"] for e in events if e.get("event") == event and e.get("ts")]
    return datetime.fromisoformat(stamps[0]) if stamps else None


def summarize(call_id: str, events: list[dict]) -> CallCost:
    usd, unpriced = 0.0, []
    for usage in (e for e in events if e.get("event") == "service_usage"):
        cost = _usage_usd(usage)
        if cost is None:
            unpriced.append(f"{usage.get('kind')}:{usage.get('model')}")
        else:
            usd += cost
    started, ended = _ts(events, "call_started"), _ts(events, "call_ended")
    ttfat = [
        e["seconds"] for e in events
        if e.get("event") == "service_latency" and e.get("kind") == "ttfat"
    ]
    return CallCost(
        call_id=call_id,
        duration_s=(ended - started).total_seconds() if started and ended else None,
        eur=None if unpriced else usd / USD_PER_EUR,
        llm_ttfat_p50_s=percentile(ttfat, 50) if ttfat else None,
        unpriced=tuple(sorted(set(unpriced))),
        amortized=any(
            e.get("model") in AMORTIZED for e in events if e.get("event") == "service_usage"
        ),
    )


def load(directory) -> list[CallCost]:
    """One CallCost per audit file that recorded any usage or call boundary."""
    calls = []
    for path in sorted(Path(directory).glob("audit-*.ndjson")):
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # audit truncates over-long lines; a cut line is not a usage record
        # Trails written before call_metrics existed carry decisions only: nothing to price.
        if any(e.get("event") in {"service_usage", "call_started"} for e in events):
            calls.append(summarize(path.stem.removeprefix("audit-"), events))
    return calls


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile: always a value that was actually observed."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def _fmt(value: float | None, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def report(directory) -> str:
    calls = load(directory)
    lines = [f"{len(calls)} calls in {directory}", ""]
    lines.append(f"{'call_id':<38} {'seconds':>8} {'eur':>9} {'llm ttfat med':>14}")
    for c in calls:
        eur = "UNPRICED" if c.eur is None else f"{c.eur:.4f}" + ("*" if c.amortized else "")
        lines.append(
            f"{c.call_id:<38} {_fmt(c.duration_s, '.1f'):>8} {eur:>9} {_fmt(c.llm_ttfat_p50_s, '.3f'):>14}"
        )
    eurs = [c.eur for c in calls if c.eur is not None]
    # A duration is known whether or not the models could be priced.
    secs = [c.duration_s for c in calls if c.duration_s is not None]
    lines.append("")
    for pct in (50, 95):
        lines.append(
            f"p{pct}  eur {_fmt(percentile(eurs, pct) if eurs else None, '.4f')}"
            f" (over {len(eurs)} priced calls)"
            f"  seconds {_fmt(percentile(secs, pct) if secs else None, '.1f')}"
            f" (over {len(secs)} timed calls)"
        )
    missing = sorted({m for c in calls for m in c.unpriced})
    if missing:
        lines.append(f"UNPRICED, left out of the aggregates — no list price for: {', '.join(missing)}")
    if any(c.amortized for c in calls):
        lines.append(
            "* LLM priced as Helmcode's flat fee spread over its full token quota: a floor, "
            "real cost per call is higher the less of the quota is used."
        )
    lines.append(
        f"Public list prices as of {PRICES_AS_OF}, not an invoice. 1 EUR = {USD_PER_EUR} USD (ECB, 2026-09-18)."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(sys.argv[1] if len(sys.argv) > 1 else os.getenv("AUDIT_DIR") or "audit-logs"))
