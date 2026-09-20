"""Thin async wrapper around TypeSafe System One (Jev) for insight Choices."""

from __future__ import annotations

import os
from typing import Any


class TypeSafeInsightClient:
    """One Choice question per ``classify_choice`` call — never batch insights."""

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        from typesafe_sdk import AsyncTypeSafeClient

        key = (api_key if api_key is not None else os.getenv("TYPESAFE_API_KEY", "")).strip()
        if not key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        kwargs: dict[str, Any] = {"api_key": key}
        resolved_model = (model or os.getenv("TYPESAFE_DEFAULT_MODEL") or "").strip()
        if resolved_model:
            kwargs["model"] = resolved_model
        self._client = AsyncTypeSafeClient(**kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def classify_choice(
        self,
        *,
        state: Any,
        question_id: str,
        instructions: str,
        values: list[str],
    ) -> dict[str, Any]:
        from typesafe_sdk import Choice

        criteria = {v: None for v in values}
        response = await self._client.system_one(
            state=state,
            questions={
                question_id: Choice(instructions=instructions, criteria=criteria),
            },
        )
        answer = response.choices[question_id]
        probabilities = dict(answer.probabilities or {})
        return {
            "choice": answer.choice,
            "probabilities": probabilities,
            "confidence": float(answer.confidence)
            if answer.confidence is not None
            else 0.0,
        }
