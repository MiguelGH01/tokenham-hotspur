"""Normalize a captured LLM context into the flat {role, content, tool_calls,
tool_call_id} shape the dashboard UI renders.

The captured context can be OpenAI-style (content is a plain string, tool
calls live in a separate `tool_calls` field), Anthropic-style (content is a
list of blocks: text / tool_use / tool_result), or Gemini-style (messages use
`parts` instead of `content`, role "model" instead of "assistant", and
function_call / function_response instead of tool_use / tool_result). Both
parsers (real calls and evals) hit whichever shape the bot's configured LLM
produces, so this is shared rather than duplicated.
"""
import json

ROLE_ALIASES = {"model": "assistant"}


def _flatten_anthropic_blocks(role, blocks):
    """Split one Anthropic-style content-block list into transcript entries."""
    entries = []
    text_parts = []
    tool_calls = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text") or "")
        elif btype == "tool_use":
            tool_calls.append({
                "name": block.get("name"),
                "arguments": json.dumps(block["input"]) if block.get("input") is not None else None,
            })
        elif btype == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
            entries.append({"role": "tool", "content": content, "tool_call_id": block.get("tool_use_id")})

    if text_parts or tool_calls:
        entry = {"role": role}
        if text_parts:
            entry["content"] = "\n".join(p for p in text_parts if p)
        if tool_calls:
            entry["tool_calls"] = tool_calls
        entries.append(entry)
    return entries


def _flatten_gemini_parts(role, parts):
    """Split one Gemini-style `parts` list into transcript entries."""
    entries = []
    text_parts = []
    tool_calls = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("text"):
            text_parts.append(part["text"])
        elif part.get("function_call"):
            fc = part["function_call"]
            tool_calls.append({
                "name": fc.get("name"),
                "arguments": json.dumps(fc["args"]) if fc.get("args") is not None else None,
            })
        elif part.get("function_response"):
            fr = part["function_response"]
            response = fr.get("response")
            entries.append({
                "role": "tool",
                "content": json.dumps(response) if not isinstance(response, str) else response,
                "tool_call_id": fr.get("id") or fr.get("name"),
            })

    if text_parts or tool_calls:
        entry = {"role": role}
        if text_parts:
            entry["content"] = "\n".join(p for p in text_parts if p)
        if tool_calls:
            entry["tool_calls"] = tool_calls
        entries.append(entry)
    return entries


def build_transcript(messages):
    """Normalize a captured LLM context (list of message dicts) for display."""
    transcript = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = ROLE_ALIASES.get(msg.get("role"), msg.get("role"))

        if "parts" in msg:
            transcript.extend(_flatten_gemini_parts(role, msg["parts"] or []))
            continue

        content = msg.get("content")
        if isinstance(content, list):
            transcript.extend(_flatten_anthropic_blocks(role, content))
            continue

        entry = {"role": role}
        if content:
            entry["content"] = content
        if msg.get("tool_calls"):
            entry["tool_calls"] = [
                {"name": tc.get("function", {}).get("name"), "arguments": tc.get("function", {}).get("arguments")}
                for tc in msg["tool_calls"]
            ]
        if role == "tool":
            entry["tool_call_id"] = msg.get("tool_call_id")
        transcript.append(entry)
    return transcript


# Tool calls that carry the caller's name, used as a human-readable label for
# a call in place of its call_id (a UUID). Keyed by tool name -> the argument
# holding the name, or a tuple of fields to join (for registration, which
# splits the name across given_name/first_surname/second_surname).
_NAME_ARG_BY_TOOL = {"search_patient": "stated_name"}
_NAME_FIELDS_BY_TOOL = {"prepare_registration": ("given_name", "first_surname", "second_surname")}


def extract_caller_name(transcript):
    """Best-effort caller name from a normalized transcript's tool calls."""
    for entry in transcript:
        for tc in entry.get("tool_calls") or []:
            name = tc.get("name")
            try:
                args = json.loads(tc["arguments"]) if tc.get("arguments") else {}
            except (TypeError, ValueError):
                args = {}
            if name in _NAME_ARG_BY_TOOL:
                value = args.get(_NAME_ARG_BY_TOOL[name])
                if value:
                    return value
            if name in _NAME_FIELDS_BY_TOOL:
                parts = [args.get(f) for f in _NAME_FIELDS_BY_TOOL[name]]
                parts = [p for p in parts if p]
                if parts:
                    return " ".join(parts)
    return None
