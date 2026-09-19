import httpx

from audio_models import CallSileroVADAnalyzer, CallSmartTurnAnalyzer
from clinic.clinic_client import ClinicClient


def test_silero_analyzers_share_the_onnx_session_not_lstm_state():
    a = CallSileroVADAnalyzer()
    b = CallSileroVADAnalyzer()
    assert a._model.session is b._model.session
    assert a._model is not b._model
    a._model._state[0, 0, 0] = 1.0
    assert b._model._state[0, 0, 0] == 0.0


def test_smart_turn_analyzers_share_the_onnx_session():
    a = CallSmartTurnAnalyzer()
    b = CallSmartTurnAnalyzer()
    assert a._session is b._session
    assert a._audio_buffer is not b._audio_buffer


def test_clinic_clients_reuse_one_http_pool():
    a = ClinicClient(base_url="https://example.invalid", api_key="k")
    b = ClinicClient(base_url="https://example.invalid", api_key="k")
    assert a._http() is b._http()
    assert isinstance(a._http(), httpx.AsyncClient)
