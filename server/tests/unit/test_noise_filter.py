"""Inbound RNNoise must not crash on the first telephony frame."""

import asyncio

import pytest
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter

from bot import _audio_in_filter


def test_inbound_filter_is_pipecat_rnnoise():
    filt = _audio_in_filter()
    if filt is None:
        pytest.skip("pipecat RNNoise extra is not installed")
    assert isinstance(filt, RNNoiseFilter)


def test_rnnoise_survives_the_first_8khz_frame():
    filt = _audio_in_filter()
    if filt is None:
        pytest.skip("pipecat RNNoise extra is not installed")

    async def run():
        await filt.start(8000)
        chunk = b"\x00\x00" * 160
        got = b""
        for _ in range(20):
            got += await filt.filter(chunk)
        await filt.stop()
        return got

    out = asyncio.run(run())
    assert isinstance(out, bytes)
    assert len(out) > 0
