"""One Silero + Smart Turn ONNX session for the process; per-call analyzers keep their own state.

A switchboard burst of 10–20 sockets used to construct both models on the event
loop for every call. That blocked greetings and STT on every other line and
looked like agent silence. Inference sessions are shared; LSTM/audio buffers
stay per analyzer so calls cannot mix (FR-concurrency).
"""

from __future__ import annotations

import threading

from pipecat.audio.turn.smart_turn.base_smart_turn import BaseSmartTurn
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroOnnxModel, SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams

_lock = threading.Lock()
_silero_session = None
_turn_session = None


def _shared_silero_session():
    global _silero_session
    with _lock:
        if _silero_session is None:
            _silero_session = SileroVADAnalyzer()._model.session
        return _silero_session


def _shared_turn_session():
    global _turn_session
    with _lock:
        if _turn_session is None:
            _turn_session = LocalSmartTurnAnalyzerV3()._session
        return _turn_session


class CallSileroVADAnalyzer(SileroVADAnalyzer):
    """Silero VAD that reuses the process ONNX session and keeps LSTM state local."""

    def __init__(self, *, sample_rate: int | None = None, params: VADParams | None = None):
        VADAnalyzer.__init__(self, sample_rate=sample_rate, params=params)
        model = SileroOnnxModel.__new__(SileroOnnxModel)
        model.session = _shared_silero_session()
        model.sample_rates = [8000, 16000]
        model.reset_states()
        self._model = model
        self._last_reset_time = 0


class CallSmartTurnAnalyzer(LocalSmartTurnAnalyzerV3):
    """Smart Turn v3 that reuses the process ONNX session and keeps audio buffers local."""

    def __init__(self, *, sample_rate: int | None = None, **kwargs):
        BaseSmartTurn.__init__(self, sample_rate=sample_rate, **kwargs)
        self._log_data = False
        self._session = _shared_turn_session()


def warm_audio_models() -> None:
    """Load both ONNX graphs once so the first burst does not stall the event loop."""
    _shared_silero_session()
    _shared_turn_session()
