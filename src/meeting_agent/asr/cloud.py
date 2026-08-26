from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid

import numpy as np
from websockets.asyncio.client import connect

from ..audio import AudioFrame
from ..config import Settings
from ..events import EventBus, StatusEvent, TranscriptEvent


def _pcm16(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


async def run_cloud_asr(
    queue: asyncio.Queue[AudioFrame],
    settings: Settings,
    bus: EventBus,
    stop: asyncio.Event,
) -> None:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        bus.publish(StatusEvent("asr-cloud", "failed", "OPENAI_API_KEY missing"))
        return
    model = settings.asr.cloud_model
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    speaker = "REMOTE"
    retry = 1.0
    while not stop.is_set():
        try:
            async with connect(
                url,
                additional_headers={"Authorization": f"Bearer {key}", "OpenAI-Beta": "realtime=v1"},
                max_size=4 * 1024 * 1024,
                ping_interval=20,
            ) as ws:
                transcription: dict[str, object] = {"model": model}
                if model == "gpt-live-transcribe":
                    transcription["delay"] = "low"
                if settings.language != "auto":
                    if model == "gpt-live-transcribe":
                        transcription["languages"] = [settings.language]
                    else:
                        transcription["language"] = settings.language
                await ws.send(
                    json.dumps(
                        {
                            "type": "session.update",
                            "session": {
                                "type": "transcription",
                                "audio": {
                                    "input": {
                                        "format": {"type": "audio/pcm", "rate": settings.audio.sample_rate},
                                        "transcription": transcription,
                                        "turn_detection": {
                                            "type": "server_vad",
                                            "threshold": 0.45,
                                            "prefix_padding_ms": 300,
                                            "silence_duration_ms": settings.asr.silence_ms,
                                        },
                                    }
                                },
                            },
                        }
                    )
                )
                bus.publish(StatusEvent(f"asr-cloud-{speaker.lower()}", "connected", model))
                retry = 1.0
                partials: dict[str, str] = {}
                first_audio = time.monotonic()

                async def sender() -> None:
                    nonlocal speaker, first_audio
                    while not stop.is_set():
                        try:
                            frame = await asyncio.wait_for(queue.get(), timeout=0.25)
                        except TimeoutError:
                            continue
                        speaker = frame.speaker
                        first_audio = min(first_audio, frame.captured_monotonic)
                        try:
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "input_audio_buffer.append",
                                        "audio": base64.b64encode(_pcm16(frame.samples)).decode("ascii"),
                                    }
                                )
                            )
                        finally:
                            queue.task_done()

                async def receiver() -> None:
                    async for raw in ws:
                        event = json.loads(raw)
                        kind = event.get("type", "")
                        item_id = event.get("item_id") or uuid.uuid4().hex
                        if kind == "conversation.item.input_audio_transcription.delta":
                            partials[item_id] = partials.get(item_id, "") + event.get("delta", "")
                            bus.publish(TranscriptEvent(speaker, partials[item_id], False, item_id))
                        elif kind == "conversation.item.input_audio_transcription.completed":
                            text = str(event.get("transcript", "")).strip()
                            if text:
                                bus.publish(
                                    TranscriptEvent(
                                        speaker,
                                        text,
                                        True,
                                        item_id,
                                        latency_seconds=max(0.0, time.monotonic() - first_audio),
                                    )
                                )
                            partials.pop(item_id, None)
                            first_audio = time.monotonic()
                        elif kind == "error":
                            detail = event.get("error", {}).get("message", str(event))
                            bus.publish(StatusEvent("asr-cloud", "warning", detail))

                send_task = asyncio.create_task(sender())
                receive_task = asyncio.create_task(receiver())
                done, pending = await asyncio.wait(
                    {send_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            bus.publish(StatusEvent("asr-cloud", "retrying", f"{type(exc).__name__}: {exc}"))
            try:
                await asyncio.wait_for(stop.wait(), timeout=retry)
            except TimeoutError:
                pass
            retry = min(30.0, retry * 2)
    bus.publish(StatusEvent("asr-cloud", "stopped"))
