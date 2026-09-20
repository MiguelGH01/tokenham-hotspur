"""Normalize LLMContext entries (dicts or LLMSpecificMessage) to role/content."""


def chat_fields(message) -> dict:
    if isinstance(message, dict):
        return message
    inner = getattr(message, "message", message)
    if isinstance(inner, dict):
        return inner
    return {
        "role": getattr(inner, "role", None),
        "content": getattr(inner, "content", None),
    }


def chat_text(message) -> str | None:
    fields = chat_fields(message)
    content = fields.get("content")
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            else:
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        joined = "".join(parts).strip()
        return joined or None
    return None


def chat_role(message) -> str | None:
    role = chat_fields(message).get("role")
    if role == "model":
        return "assistant"
    return role
