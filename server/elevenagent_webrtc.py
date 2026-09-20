"""Bridge Place-test-call WebRTC audio to the elevenagent Twilio Media Streams sidecar."""

from __future__ import annotations

import audioop
import base64
import json
import uuid
from typing import Any

import websockets
from loguru import logger
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import SmallWebRTCRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner
from websockets.exceptions import ConnectionClosed

from voice_agent import _sidecar_port_from_env, is_elevenagent

# WebRTC path uses 16 kHz PCM; the sidecar speaks 8 kHz µ-law (Twilio shape).
_WEBRTC_RATE = 16000
_SIDECAR_RATE = 8000


def _pcm16_to_mulaw_b64(pcm: bytes, sample_rate: int, state: list[Any]) -> str:
    if sample_rate != _SIDECAR_RATE:
        pcm, state[0] = audioop.ratecv(pcm, 2, 1, sample_rate, _SIDECAR_RATE, state[0])
    return base64.b64encode(audioop.lin2ulaw(pcm, 2)).decode("ascii")


def _mulaw_b64_to_pcm16(payload_b64: str, out_rate: int, state: list[Any]) -> bytes:
    mulaw = base64.b64decode(payload_b64)
    pcm8 = audioop.ulaw2lin(mulaw, 2)
    if out_rate == _SIDECAR_RATE:
        return pcm8
    pcm, state[0] = audioop.ratecv(pcm8, 2, 1, _SIDECAR_RATE, out_rate, state[0])
    return pcm


class ElevenAgentWebRTCBridge(FrameProcessor):
    """Minimal pipeline processor: WebRTC PCM ↔ localhost elevenagent /ws."""

    def __init__(self, *, sidecar_port: int, call_id: str, stream_sid: str):
        super().__init__()
        self._port = sidecar_port
        self._call_id = call_id
        self._stream_sid = stream_sid
        self._ws = None
        self._reader_task = None
        self._down_state: list[Any] = [None]
        self._up_state: list[Any] = [None]
        self._seq = 1
        self._started = False

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame) and not self._started:
            self._started = True
            await self._connect_sidecar()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InputAudioRawFrame) and self._ws is not None:
            try:
                rate = getattr(frame, "sample_rate", None) or _WEBRTC_RATE
                payload = _pcm16_to_mulaw_b64(frame.audio, rate, self._down_state)
                self._seq += 1
                await self._ws.send(
                    json.dumps(
                        {
                            "event": "media",
                            "sequenceNumber": str(self._seq),
                            "streamSid": self._stream_sid,
                            "media": {
                                "track": "inbound",
                                "chunk": str(self._seq),
                                "timestamp": str(self._seq * 20),
                                "payload": payload,
                            },
                        }
                    )
                )
            except Exception as exc:
                logger.debug("elevenagent webrtc uplink dropped: {}", exc)
            return

        await self.push_frame(frame, direction)

    async def _connect_sidecar(self) -> None:
        url = f"ws://127.0.0.1:{self._port}/ws"
        logger.info(
            "VOICE_AGENT=elevenagent — Place test call bridging WebRTC → sidecar :{} ({})",
            self._port,
            self._call_id,
        )
        self._ws = await websockets.connect(url, open_timeout=10, max_size=8 * 1024 * 1024)
        await self._ws.send(
            json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        )
        await self._ws.send(
            json.dumps(
                {
                    "event": "start",
                    "sequenceNumber": "1",
                    "streamSid": self._stream_sid,
                    "start": {
                        "streamSid": self._stream_sid,
                        "callSid": self._call_id,
                        "tracks": ["inbound"],
                        "mediaFormat": {
                            "encoding": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "channels": 1,
                        },
                        "customParameters": {
                            "from_number": "webrtc-test",
                            "is_test": "true",
                        },
                    },
                }
            )
        )
        self._reader_task = self.create_task(self._read_sidecar(), name="elevenagent-webrtc-reader")

    async def _read_sidecar(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="ignore")
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    continue
                kind = event.get("event")
                if kind == "media":
                    payload = (event.get("media") or {}).get("payload")
                    if not payload:
                        continue
                    pcm = _mulaw_b64_to_pcm16(payload, _WEBRTC_RATE, self._up_state)
                    await self.push_frame(
                        OutputAudioRawFrame(
                            audio=pcm,
                            sample_rate=_WEBRTC_RATE,
                            num_channels=1,
                        )
                    )
                elif kind == "clear":
                    await self.push_frame(InterruptionFrame())
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.debug("elevenagent webrtc downlink closed: {}", exc)

    async def cleanup(self):
        if self._ws is not None:
            try:
                await self._ws.send(
                    json.dumps(
                        {
                            "event": "stop",
                            "sequenceNumber": str(self._seq + 1),
                            "streamSid": self._stream_sid,
                            "stop": {"callSid": self._call_id},
                        }
                    )
                )
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._reader_task is not None:
            await self.cancel_task(self._reader_task)
            self._reader_task = None
        await super().cleanup()


async def run_webrtc_elevenagent(
    runner_args: SmallWebRTCRunnerArguments,
    *,
    audio_kwargs: dict | None = None,
) -> None:
    """Answer a console Place-test-call by bridging WebRTC to the Node sidecar."""
    if not is_elevenagent():
        raise RuntimeError("run_webrtc_elevenagent requires VOICE_AGENT=elevenagent")

    call_id = f"CA-webrtc-{uuid.uuid4().hex[:12]}"
    stream_sid = f"MZ{uuid.uuid4().hex[:16]}"
    port = _sidecar_port_from_env()

    params = TransportParams(
        **(audio_kwargs or {"audio_in_enabled": True, "audio_out_enabled": True}),
        audio_in_sample_rate=_WEBRTC_RATE,
        audio_out_sample_rate=_WEBRTC_RATE,
    )
    transport: BaseTransport = await create_transport(
        runner_args,
        {"webrtc": lambda: params},
    )

    bridge = ElevenAgentWebRTCBridge(sidecar_port=port, call_id=call_id, stream_sid=stream_sid)
    pipeline = Pipeline([transport.input(), bridge, transport.output()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=_WEBRTC_RATE,
            audio_out_sample_rate=_WEBRTC_RATE,
            enable_metrics=False,
            enable_usage_metrics=False,
        ),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("WebRTC test call disconnected ({})", call_id)
        await runner.cancel()

    try:
        await runner.run()
    finally:
        await bridge.cleanup()
