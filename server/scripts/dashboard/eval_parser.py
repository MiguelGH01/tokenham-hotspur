"""Parse server/eval-runs/<timestamp>/ directories for the local dashboard."""
import ast
import re
from pathlib import Path

EVENT_RE = re.compile(r"^\s*([\d.]+)\s+\[\s*(t\d+|--)\s*\]\s+(.*)$")
TURN_RE = re.compile(r"^--- turn (\d+): (.*)$")
CTX_RE = re.compile(r"Generating chat from context (\[.*\])\s*$")
ERROR_LINE_RE = re.compile(r"^(?P<ts>[\d\-: .]+) \| (?P<level>\w+)\s*\| (?P<src>[^-]+) - (?P<msg>.*)$")


def parse_eval_log(path: Path):
    events = []
    for raw in path.read_text(errors="replace").splitlines():
        line = re.sub(r"^\d+\t", "", raw)
        m = EVENT_RE.match(line)
        if not m:
            continue
        t, turn, rest = m.groups()
        t = float(t)
        kind = "other"
        detail = rest
        payload = None

        tm = TURN_RE.match(rest)
        if tm:
            kind = "turn"
            detail = f"turn {tm.group(1)}"
        elif rest.startswith("event: "):
            kind = "event"
            body = rest[len("event: "):]
            parts = body.split(None, 1)
            detail = parts[0]
            if len(parts) > 1:
                payload = parts[1].strip()
        elif rest.startswith("match: "):
            kind, detail = "match", rest[len("match: "):]
        elif rest.startswith("send: "):
            kind, detail = "send", rest[len("send: "):]
        elif rest.startswith("FAIL: "):
            kind, detail = "fail", rest[len("FAIL: "):]
        elif rest.startswith("done: "):
            kind, detail = "done", rest[len("done: "):]
        elif rest.startswith("discard: "):
            kind, detail = "discard", rest[len("discard: "):]
        elif rest.startswith("run: "):
            kind, detail = "run", rest[len("run: "):]
        elif rest == "connected":
            kind, detail = "connected", rest
        elif rest.startswith("handshake: "):
            kind, detail = "handshake", rest[len("handshake: "):]
        elif rest.startswith("user  -> ") or rest.startswith("judge -> "):
            kind, detail = "config", rest

        events.append({"t": t, "turn": turn, "kind": kind, "detail": detail, "payload": payload, "raw": rest})
    return events


def parse_bot_log(path: Path):
    text = path.read_text(errors="replace")
    last_ctx = None
    errors, warnings = [], []
    for line in text.splitlines():
        m = CTX_RE.search(line)
        if m:
            try:
                last_ctx = ast.literal_eval(m.group(1))
            except Exception:
                pass
        em = ERROR_LINE_RE.match(line)
        if em and em.group("level") in ("ERROR", "CRITICAL"):
            errors.append({"ts": em.group("ts").strip(), "src": em.group("src").strip(), "msg": em.group("msg").strip()})
        elif em and em.group("level") == "WARNING":
            warnings.append({"ts": em.group("ts").strip(), "src": em.group("src").strip(), "msg": em.group("msg").strip()})

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
            if role == "tool":
                entry["tool_call_id"] = msg.get("tool_call_id")
            transcript.append(entry)
    return transcript, errors, warnings


def parse_results(path: Path):
    """results.txt is `<scenario name>\\t<PASS|FAIL>`, one per line, in run order."""
    order = []
    if not path.exists():
        return order
    idx = 0
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        idx += 1
        name, status = parts[0], parts[1]
        order.append({"idx": idx, "name": name, "status": status})
    return order


def parse_eval_run(run_dir: Path):
    results = parse_results(run_dir / "results.txt")
    status_by_name = {r["name"]: r["status"] for r in results}
    order_by_name = {r["name"]: r["idx"] for r in results}

    scenario_names = sorted({p.stem.rsplit(".", 1)[0] for p in run_dir.glob("*.eval.log")})
    scenarios = []
    for name in scenario_names:
        eval_log = run_dir / f"{name}.eval.log"
        bot_log = run_dir / f"{name}.bot.log"
        events = parse_eval_log(eval_log) if eval_log.exists() else []
        transcript, errors, warnings = parse_bot_log(bot_log) if bot_log.exists() else ([], [], [])

        status = status_by_name.get(name) or ("FAIL" if any(e["kind"] == "fail" for e in events) else "PASS")
        fail_count = sum(1 for e in events if e["kind"] == "fail")
        duration = max((e["t"] for e in events), default=0.0)

        scenarios.append({
            "name": name, "status": status, "order": order_by_name.get(name, 0),
            "duration": duration, "fail_count": fail_count,
            "events": events, "transcript": transcript, "errors": errors, "warnings": warnings,
        })
    scenarios.sort(key=lambda s: s["order"])
    return {"run_id": run_dir.name, "scenarios": scenarios}


def list_eval_runs(eval_runs_dir: Path):
    if not eval_runs_dir.exists():
        return []
    runs = []
    for d in sorted(eval_runs_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        results = parse_results(d / "results.txt")
        if not results:
            scenario_names = list(d.glob("*.eval.log"))
            if not scenario_names:
                continue
            passed = failed = 0
        else:
            passed = sum(1 for r in results if r["status"] == "PASS")
            failed = sum(1 for r in results if r["status"] == "FAIL")
        runs.append({"run_id": d.name, "passed": passed, "failed": failed, "total": passed + failed})
    return runs
