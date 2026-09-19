"""Parse server/run-logs/<mode>-<timestamp>/bot.log directories (real
webrtc/twilio calls, as opposed to the eval harness) for the local dashboard."""
import ast
import re
from datetime import datetime
from pathlib import Path

LINE_RE = re.compile(r"^(?P<ts>[\d\-: .]+) \| (?P<level>\w+)\s*\| (?P<src>[^-]+) - (?P<msg>.*)$")
CTX_RE = re.compile(r"Generating chat from context (\[.*\])\s*$")
CALL_ID_RE = re.compile(r"Starting bot for call (\S+)")
OFFER_RE = re.compile(r"^Offer (\S+): (\{.*\})$")
SUBMIT_OK_RE = re.compile(r"^Submitted (\S+) for call (\S+): (.*)$")
SUBMIT_FAIL_RE = re.compile(r"^Submission failed for call (\S+) \((.*)\): (.*)$")
REPROMPT_RE = re.compile(r"^Call (\S+): (\d+)s of silence, re-prompting: (.*)$")


def _offsets(ts_strs):
    parsed = []
    for s in ts_strs:
        s = s.strip()
        try:
            parsed.append(datetime.strptime(s[:23], "%Y-%m-%d %H:%M:%S.%f"))
        except ValueError:
            parsed.append(None)
    base = next((p for p in parsed if p), None)
    return [(p - base).total_seconds() if (p and base) else 0.0 for p in parsed]


def parse_bot_log(path: Path):
    lines = path.read_text(errors="replace").splitlines()
    matched = [LINE_RE.match(l) for l in lines]
    offsets = _offsets([m.group("ts") if m else "" for m in matched])

    events, errors, warnings = [], [], []
    call_id = None
    last_ctx = None

    for i, (line, m) in enumerate(zip(lines, matched)):
        if not m:
            continue
        level, src, msg = m.group("level"), m.group("src").strip(), m.group("msg")
        t = offsets[i]

        ctx_m = CTX_RE.search(line)
        if ctx_m:
            try:
                last_ctx = ast.literal_eval(ctx_m.group(1))
            except Exception:
                pass

        cid_m = CALL_ID_RE.search(msg)
        if cid_m:
            call_id = cid_m.group(1)
            events.append({"t": t, "kind": "call_start", "detail": msg})
            continue
        if "Client connected" in msg:
            events.append({"t": t, "kind": "connected", "detail": msg}); continue
        if "Client disconnected" in msg:
            events.append({"t": t, "kind": "disconnected", "detail": msg}); continue

        om = OFFER_RE.match(msg)
        if om:
            try:
                offer = ast.literal_eval(om.group(2))
            except Exception:
                offer = {"raw": om.group(2)}
            events.append({"t": t, "kind": "offer", "detail": msg, "offer": offer})
            continue

        sm = SUBMIT_OK_RE.match(msg)
        if sm:
            events.append({"t": t, "kind": "submitted", "detail": msg, "action": sm.group(1), "result": sm.group(3)})
            continue

        fm = SUBMIT_FAIL_RE.match(msg)
        if fm:
            events.append({"t": t, "kind": "submit_failed", "detail": msg}); continue

        rm = REPROMPT_RE.match(msg)
        if rm:
            events.append({"t": t, "kind": "reprompt", "detail": msg}); continue

        if level in ("ERROR", "CRITICAL"):
            errors.append({"t": t, "src": src, "msg": msg})
            events.append({"t": t, "kind": "error", "detail": msg})
        elif level == "WARNING":
            warnings.append({"t": t, "src": src, "msg": msg})
            events.append({"t": t, "kind": "warning", "detail": msg})

    transcript = []
    if last_ctx:
        for msg in last_ctx:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            entry = {"role": role}
            if msg.get("content"):
                entry["content"] = msg["content"]
            if msg.get("tool_calls"):
                entry["tool_calls"] = [
                    {"name": tc.get("function", {}).get("name"), "arguments": tc.get("function", {}).get("arguments")}
                    for tc in msg["tool_calls"]
                ]
            transcript.append(entry)

    duration = offsets[-1] if offsets else 0.0
    outcome = "IN_PROGRESS"
    for e in events:
        if e["kind"] == "submitted":
            outcome = e["action"]
        elif e["kind"] == "submit_failed":
            outcome = "SUBMIT_FAILED"

    return {
        "call_id": call_id, "duration": duration, "outcome": outcome,
        "events": events, "transcript": transcript, "errors": errors, "warnings": warnings,
    }


def parse_real_calls(run_logs_dir: Path):
    calls = []
    if not run_logs_dir.exists():
        return {"calls": calls}
    for d in sorted(run_logs_dir.iterdir()):
        if not d.is_dir() or d.name == "recordings":
            continue
        bot_log = d / "bot.log"
        if not bot_log.exists():
            continue
        m = re.match(r"(webrtc|twilio)-(\d{8}-\d{6})", d.name)
        mode, ts = (m.group(1), m.group(2)) if m else ("unknown", d.name)
        parsed = parse_bot_log(bot_log)
        parsed["run_dir"] = d.name
        parsed["mode"] = mode
        parsed["started_at"] = ts
        recording = None
        rec_dir = run_logs_dir / "recordings"
        if rec_dir.exists() and parsed["call_id"]:
            hits = list(rec_dir.glob(f"*{parsed['call_id']}*"))
            if hits:
                recording = hits[0].name
        parsed["recording"] = recording
        calls.append(parsed)
    calls.sort(key=lambda c: c["started_at"], reverse=True)
    return {"calls": calls}
