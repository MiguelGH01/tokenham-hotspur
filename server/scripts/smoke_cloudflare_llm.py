"""One-shot smoke test: Claude via Cloudflare unified Messages API."""

import asyncio
import json
import os
import sys
from pathlib import Path

from anthropic import APIStatusError, AsyncAnthropic
from dotenv import load_dotenv
from pipecat.processors.aggregators.llm_context import LLMContext

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=True)
os.environ["LLM_PROVIDER"] = "cloudflare"

from bot import _CLOUDFLARE_DEFAULT_MODEL, _cloudflare_llm, _cloudflare_messages_base_url  # noqa: E402


def redact(text: str, token: str, account_id: str) -> str:
    return text.replace(token, "***").replace(account_id, "***")


async def sdk_call(
    *,
    token: str,
    base: str,
    model: str,
    thinking_disabled: bool,
):
    kwargs = dict(
        model=model,
        max_tokens=64,
        messages=[{"role": "user", "content": "Responde exactamente: ok-cloudflare"}],
    )
    if thinking_disabled:
        kwargs["thinking"] = {"type": "disabled"}
    client = AsyncAnthropic(auth_token=token, base_url=base)
    msg = await client.messages.create(**kwargs)
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return {
        "id_prefix": (msg.id or "")[:8],
        "model": msg.model,
        "stop": msg.stop_reason,
        "in": msg.usage.input_tokens,
        "out": msg.usage.output_tokens,
        "text": text.strip()[:200],
    }


async def pipecat_call() -> str:
    llm = _cloudflare_llm()
    context = LLMContext()
    context.add_message({"role": "user", "content": "Di solo: pipecat-ok"})
    text = await llm.run_inference(context, max_tokens=32)
    return (text or "").strip()[:200]


async def main() -> int:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    model = os.getenv("CLOUDFLARE_LLM_MODEL", _CLOUDFLARE_DEFAULT_MODEL)
    if not account_id or not token:
        print("Missing CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN in server/.env")
        return 1
    base = _cloudflare_messages_base_url(account_id)
    print(f"account_id_len={len(account_id)}")
    print(f"token_prefix={token[:4]}… token_len={len(token)}")
    print(f"model={model}")
    print(f"path_suffix=.../accounts/***/ai")

    print("\n== 1) Anthropic SDK via Cloudflare ==")
    try:
        result = await sdk_call(
            token=token, base=base, model=model, thinking_disabled=True
        )
        print("thinking=disabled", json.dumps(result, ensure_ascii=False))
    except APIStatusError as exc:
        body = ""
        try:
            body = exc.response.text[:1200]
        except Exception:
            body = str(exc)[:1200]
        print(f"thinking=disabled FAILED status={exc.status_code}")
        print(redact(body, token, account_id))
        print("retry without thinking…")
        try:
            result = await sdk_call(
                token=token, base=base, model=model, thinking_disabled=False
            )
            print("thinking=omit", json.dumps(result, ensure_ascii=False))
        except APIStatusError as exc2:
            body2 = ""
            try:
                body2 = exc2.response.text[:1200]
            except Exception:
                body2 = str(exc2)[:1200]
            print(f"thinking=omit FAILED status={exc2.status_code}")
            print(redact(body2, token, account_id))
            return 1
    except Exception as exc:
        print("SDK unexpected:", type(exc).__name__, redact(str(exc), token, account_id)[:800])
        return 1

    print("\n== 2) Pipecat AnthropicLLMService ==")
    try:
        text = await pipecat_call()
        print("pipecat_text=", json.dumps(text, ensure_ascii=False))
    except Exception as exc:
        print("PIPECAT FAILED", type(exc).__name__, redact(str(exc), token, account_id)[:800])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
