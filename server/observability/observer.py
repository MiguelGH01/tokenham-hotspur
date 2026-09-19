"""Pipeline observer that forwards transcription frames into the CallHub."""

from __future__ import annotations

from datetime import datetime, timezone

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from observability.events import make_event
from observability.hub import CallHub


class TraceObserver(BaseObserver):
    """Watch STT / TTS text frames and publish them without altering the pipeline.

    TTS arrives as word/phrase chunks (``TTSTextFrame``). Those are buffered and
    emitted as one ``transcript.bot`` when the bot stops speaking, so the console
    shows a single receptionist bubble per turn rather than one per word.
    """

    def __init__(self, hub: CallHub, call_id: str, *, started_at: datetime | None = None):
        super().__init__()
        self._hub = hub
        self._call_id = call_id
        self._started_at = started_at or datetime.now(timezone.utc)
        self._first_word_emitted = False
        self._bot_chunks: list[str] = []

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
            text = getattr(frame, "text", None) or ""
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
                self._bot_chunks.append(text)
        elif isinstance(frame, (BotStoppedSpeakingFrame, TTSStoppedFrame, InterruptionFrame)):
            await self._flush_bot()

    async def _flush_bot(self) -> None:
        if not self._bot_chunks:
            return
        chunks = self._bot_chunks
        self._bot_chunks = []
        # Chunks often arrive as bare tokens ("Parece", "que") without spaces.
        if any(c[:1].isspace() or c[-1:].isspace() for c in chunks if c):
            text = "".join(chunks).strip()
        else:
            text = " ".join(c.strip() for c in chunks if c.strip())
        if text:
            await self._hub.emit(
                make_event("transcript.bot", self._call_id, text=text, final=True)
            )
