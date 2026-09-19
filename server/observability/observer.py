"""Pipeline observer that forwards transcription frames into the CallHub."""

from __future__ import annotations

from datetime import datetime, timezone

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from observability.events import make_event
from observability.hub import CallHub


class TraceObserver(BaseObserver):
    """Watch STT / TTS text frames and publish them without altering the pipeline."""

    def __init__(self, hub: CallHub, call_id: str, *, started_at: datetime | None = None):
        super().__init__()
        self._hub = hub
        self._call_id = call_id
        self._started_at = started_at or datetime.now(timezone.utc)
        self._first_word_emitted = False

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                await self._hub.emit(
                    make_event("transcript.user", self._call_id, text=text, final=True)
                )
        elif isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                await self._hub.emit(
                    make_event(
                        "transcript.user_interim",
                        self._call_id,
                        text=text,
                        final=False,
                    )
                )
        elif isinstance(frame, TTSTextFrame):
            text = (getattr(frame, "text", None) or "").strip()
            if text:
                if not self._first_word_emitted:
                    self._first_word_emitted = True
                    ms = int(
                        (datetime.now(timezone.utc) - self._started_at.astimezone(timezone.utc))
                        .total_seconds()
                        * 1000
                    )
                    await self._hub.emit(
                        make_event("metrics.first_word", self._call_id, ms=max(ms, 0))
                    )
                await self._hub.emit(
                    make_event("transcript.bot", self._call_id, text=text, final=True)
                )
