"""Parse server/run-logs/<mode>-<timestamp>/bot.log directories (real
webrtc/twilio calls, as opposed to the eval harness) for the local dashboard.

A single `make run-webrtc`/`make run-twilio` process can serve many calls,
including CONCURRENTLY (one asyncio task per websocket) -- their lines then
interleave in one bot.log. bot.py tags every line for a call's lifetime with
`call=<id>` (via loguru's `logger.contextualize`), so this now groups a call's
lines by that tag directly rather than assuming calls never overlap. Logs
captured before that tag existed have no `call=` segment on any line, so as a
fallback for those specifically, each "Starting bot for call <id>" line still
marks a new call and everything up to the next such line (or EOF) belongs to
it -- correct only when calls in that older log truly ran one at a time.
"""
import ast
import json
import re
from datetime import datetime
from pathlib import Path

from transcript_utils import build_transcript, extract_caller_name

LINE_RE = re.compile(
    r"^(?P<ts>[\d\-: .]+) \| (?P<level>\w+)\s*\| (?:call=(?P<call_tag>\S+) \| )?(?P<src>[^-]+) - (?P<msg>.*)$"
)
CTX_RE = re.compile(r"Generating chat from context (\[.*\])\s*$")
CALL_ID_RE = re.compile(r"Starting bot for call (\S+)")
AUDIT_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _read_audit_outcome(audit_dir: Path, call_id: str):
    """Read the final `submission_result` from this call's audit-logs/audit-<id>.ndjson.

    This is the authoritative outcome (see server/audit.py / submission.py) --
    bot.log text is not, since its log message formats have drifted since the
    old regex-based outcome tracking below was written.
    """
    if not call_id:
        return None
    safe_id = AUDIT_ID_RE.sub("_", call_id)[:128] or "unknown"
    path = audit_dir / f"audit-{safe_id}.ndjson"
    if not path.exists():
        return None
    last = None
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == "submission_result":
            last = rec
    if last is None:
        return None
    return last.get("verb"), bool(last.get("ok"))


def _parse_ts(ts_str):
    try:
        return datetime.strptime(ts_str.strip()[:23], "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def _attach_entry_timestamps(transcript, ctx_snapshots):
    """Stamp each transcript entry with the call-relative second it first appeared.

    A call logs one "Generating chat from context [...]" snapshot per LLM
    call, each a growing prefix of the final context (messages are appended,
    never edited mid-call). Flattening each snapshot with the same
    `build_transcript` used for the final one and diffing consecutive lengths
    gives the wall-clock offset each entry first showed up at -- close enough
    to line the transcript up with the call recording.
    """
    seen = 0
    for t, ctx in ctx_snapshots:
        try:
            flat = build_transcript(ctx)
        except Exception:
            continue
        n = min(len(flat), len(transcript))
        for i in range(seen, n):
            transcript[i]["t"] = round(t, 2)
        seen = max(seen, n)


def _find_call_boundaries(lines, matched):
    """Return [(line_index, call_id), ...] for each 'Starting bot for call' line."""
    boundaries = []
    for i, (line, m) in enumerate(zip(lines, matched)):
        if not m:
            continue
        cid_m = CALL_ID_RE.search(m.group("msg"))
        if cid_m:
            boundaries.append((i, cid_m.group(1)))
    return boundaries


def _pair_spans(marks):
    """Turn a list of (t, "start"|"stop") marks into closed (start, end) spans.

    An unmatched trailing "start" (call ended mid-turn) is dropped rather than
    guessed at; a "stop" with no open "start" is ignored the same way.
    """
    spans = []
    open_start = None
    for t, kind in marks:
        if kind == "start":
            if open_start is None:
                open_start = t
        elif kind == "stop" and open_start is not None:
            spans.append((open_start, t))
            open_start = None
    return spans


def _overlap_spans(a, b):
    """Intersection intervals between two lists of (start, end) spans -- the
    stretches where both sides were talking at once (barge-ins/talk-over)."""
    out = []
    for a_start, a_end in a:
        for b_start, b_end in b:
            lo, hi = max(a_start, b_start), min(a_end, b_end)
            if hi > lo:
                out.append((lo, hi))
    return out


def _parse_segment(lines, matched, start_i, end_i, call_id):
    """Parse one call's slice of a bot.log ([start_i, end_i)) into events/transcript."""
    seg_lines = lines[start_i:end_i]
    seg_matched = matched[start_i:end_i]

    base_ts = None
    for m in seg_matched:
        if m:
            base_ts = _parse_ts(m.group("ts"))
            break
    call_started_at = base_ts.strftime("%Y%m%d-%H%M%S") if base_ts else None

    events, errors, warnings = [], [], []
    last_ctx = None
    ctx_snapshots = []  # [(t, parsed_context), ...] in chronological order
    # Pipecat/pipecat-flows log these at DEBUG (LOG_LEVEL=DEBUG only) from
    # their own frame processors and flow manager, not bot.py -- absent
    # entirely on INFO-level runs, in which case the *_spans below just come
    # out empty for that call.
    user_marks, bot_marks, tool_marks = [], [], []

    for line, m in zip(seg_lines, seg_matched):
        if not m:
            continue
        level, src, msg = m.group("level"), m.group("src").strip(), m.group("msg")
        ts = _parse_ts(m.group("ts"))
        t = (ts - base_ts).total_seconds() if (ts and base_ts) else 0.0

        ctx_m = CTX_RE.search(line)
        if ctx_m:
            try:
                parsed_ctx = ast.literal_eval(ctx_m.group(1))
            except Exception:
                parsed_ctx = None
            if parsed_ctx is not None:
                last_ctx = parsed_ctx
                ctx_snapshots.append((t, parsed_ctx))

        if CALL_ID_RE.search(msg):
            events.append({"t": t, "kind": "call_start", "detail": msg})
            continue
        if "Client connected" in msg:
            events.append({"t": t, "kind": "connected", "detail": msg}); continue
        if "Client disconnected" in msg:
            events.append({"t": t, "kind": "disconnected", "detail": msg}); continue

        if "User started speaking" in msg:
            user_marks.append((t, "start")); continue
        if "User stopped speaking" in msg:
            user_marks.append((t, "stop")); continue
        if msg == "Bot started speaking":
            bot_marks.append((t, "start")); continue
        if msg == "Bot stopped speaking":
            bot_marks.append((t, "stop")); continue

        # pipecat_flows.manager:transition_func brackets a tool's own handler
        # this way; the function name isn't unique across a call's flow (the
        # same tool can be called more than once), so pairing is by simple
        # chronological order, not by matching names.
        if msg.startswith("Function called: "):
            tool_marks.append((t, "start")); continue
        if msg.startswith("Function handler completed for "):
            tool_marks.append((t, "stop")); continue

        if level in ("ERROR", "CRITICAL"):
            errors.append({"t": t, "src": src, "msg": msg})
            events.append({"t": t, "kind": "error", "detail": msg})
        elif level == "WARNING":
            warnings.append({"t": t, "src": src, "msg": msg})
            events.append({"t": t, "kind": "warning", "detail": msg})

    transcript = build_transcript(last_ctx)
    _attach_entry_timestamps(transcript, ctx_snapshots)

    duration = events[-1]["t"] if events else 0.0
    # The real outcome comes from audit-logs (parse_real_calls, below) or the
    # disconnect-without-audit ABANDONED fallback; nothing here sets it, since
    # bot.log carries no reliable booking-outcome text of its own.
    outcome = "IN_PROGRESS"

    user_spans = _pair_spans(user_marks)
    bot_spans = _pair_spans(bot_marks)
    tool_spans = _pair_spans(tool_marks)
    overlap_spans = _overlap_spans(user_spans, bot_spans)

    def _spans_json(spans):
        return [{"start": round(s, 2), "end": round(e, 2)} for s, e in spans]

    return {
        "call_id": call_id,
        "call_started_at": call_started_at,
        "duration": duration,
        "outcome": outcome,
        "events": events,
        "transcript": transcript,
        "caller_name": extract_caller_name(transcript),
        "user_spans": _spans_json(user_spans),
        "assistant_spans": _spans_json(bot_spans),
        "tool_spans": _spans_json(tool_spans),
        "overlap_spans": _spans_json(overlap_spans),
        "errors": errors,
        "warnings": warnings,
    }


def _parse_by_call_tag(lines, matched):
    """Group lines by their explicit `call=<id>` tag -- correct even when
    several calls' lines interleave, since it doesn't rely on position."""
    order = []
    seen = set()
    for m in matched:
        if not m:
            continue
        tag = m.group("call_tag")
        if tag and tag != "-" and tag not in seen:
            seen.add(tag)
            order.append(tag)

    calls = []
    for call_id in order:
        idxs = [i for i, m in enumerate(matched) if m and m.group("call_tag") == call_id]
        seg_lines = [lines[i] for i in idxs]
        seg_matched = [matched[i] for i in idxs]
        calls.append(_parse_segment(seg_lines, seg_matched, 0, len(seg_lines), call_id))
    return calls


def _parse_by_position(lines, matched):
    """Fallback for logs predating the `call=<id>` tag: assumes calls never
    overlap, splitting purely on "Starting bot for call" line position."""
    boundaries = _find_call_boundaries(lines, matched)
    calls = []
    for idx, (start_i, call_id) in enumerate(boundaries):
        end_i = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        calls.append(_parse_segment(lines, matched, start_i, end_i, call_id))
    return calls


def parse_bot_log(path: Path):
    """Split one bot.log into one dict per call it contains (possibly zero)."""
    lines = path.read_text(errors="replace").splitlines()
    matched = [LINE_RE.match(l) for l in lines]
    has_call_tags = any(m and m.group("call_tag") not in (None, "-") for m in matched)
    if has_call_tags:
        return _parse_by_call_tag(lines, matched)
    return _parse_by_position(lines, matched)


def parse_real_calls(run_logs_dir: Path):
    calls = []
    if not run_logs_dir.exists():
        return {"calls": calls}
    audit_dir = run_logs_dir.parent / "audit-logs"
    for d in sorted(run_logs_dir.iterdir()):
        if not d.is_dir() or d.name == "recordings":
            continue
        bot_log = d / "bot.log"
        if not bot_log.exists():
            continue
        # Run dirs are <transport>-[<person>-]<timestamp>; the person tag
        # (e.g. "carlos") distinguishes whose machine a call was run from --
        # untagged runs are Migui's own, which is the default.
        m = re.match(r"(?:webrtc|twilio)(?:-([A-Za-z0-9]+))?-(\d{8}-\d{6})$", d.name)
        if m:
            person, session_started_at = m.group(1), m.group(2)
            mode = person.lower() if person else "migui"
        else:
            mode, session_started_at = "unknown", d.name

        rec_dir = run_logs_dir / "recordings"
        for call in parse_bot_log(bot_log):
            call["run_dir"] = d.name
            call["mode"] = mode
            call["started_at"] = call["call_started_at"] or session_started_at
            recording = None
            if rec_dir.exists() and call["call_id"]:
                hits = list(rec_dir.glob(f"*{call['call_id']}*"))
                if hits:
                    recording = hits[0].name
            call["recording"] = recording

            audit_result = _read_audit_outcome(audit_dir, call["call_id"])
            if audit_result:
                verb, ok = audit_result
                call["outcome"] = verb if ok else "SUBMIT_FAILED"
            elif call["outcome"] == "IN_PROGRESS" and any(e["kind"] == "disconnected" for e in call["events"]):
                call["outcome"] = "ABANDONED"

            calls.append(call)

    calls.sort(key=lambda c: c["started_at"], reverse=True)
    return {"calls": calls}
