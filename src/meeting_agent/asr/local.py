from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
import re

import numpy as np
from faster_whisper import WhisperModel

from ..audio import AudioFrame
from ..config import Settings
from ..events import EventBus, StatusEvent, TranscriptEvent
from .selection import ASRSelection


# A calibration window is only a noise-floor estimate, so it must never gate the
# stream above speech level: anyone already talking one second into the session
# would otherwise stay silent for the whole meeting.
MAX_ADAPTIVE_THRESHOLD_FACTOR = 8.0


def calibrate_threshold(calibration: list[float], speaker: str, configured_floor: float) -> float:
    """Noise floor for a speaker, taken from the quiet end of the calibration window."""
    quiet = float(np.percentile(calibration, 20))
    margin = 2.5 if speaker == "ME" else 1.5
    return min(
        max(configured_floor, quiet * margin),
        configured_floor * MAX_ADAPTIVE_THRESHOLD_FACTOR,
    )


def resample_linear(samples: np.ndarray, source_rate: int, target_rate: int = 16_000) -> np.ndarray:
    if source_rate == target_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    output_size = max(1, round(samples.size * target_rate / source_rate))
    old_x = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    new_x = np.linspace(0.0, 1.0, output_size, endpoint=False)
    return np.interp(new_x, old_x, samples).astype(np.float32)


class LocalModelPool:
    """One hot model shared by ME and REMOTE to avoid duplicating RAM."""

    def __init__(self, selection: ASRSelection, settings: Settings) -> None:
        self.selection = selection
        self.settings = settings
        self.model: WhisperModel | None = None
        self.lock = asyncio.Lock()

    async def load(self) -> None:
        if self.model is not None:
            return
        self.model = await asyncio.to_thread(
            WhisperModel,
            self.selection.model,
            device=self.selection.device,
            compute_type=self.selection.compute_type,
            # Reserve CPU for the two real-time WASAPI capture threads and UI.
            cpu_threads=2,
            num_workers=1,
        )

    async def transcribe(self, audio: np.ndarray) -> tuple[str, float]:
        await self.load()
        started = time.monotonic()
        async with self.lock:
            assert self.model is not None

            def infer() -> str:
                language = None if self.settings.language == "auto" else self.settings.language
                segments, _ = self.model.transcribe(
                    audio,
                    language=language,
                    beam_size=self.settings.asr.beam_size,
                    best_of=1,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    vad_filter=False,
                    word_timestamps=False,
                    repetition_penalty=1.1,
                    no_repeat_ngram_size=3,
                    no_speech_threshold=0.5,
                )
                return " ".join(part.text.strip() for part in segments if part.text.strip()).strip()

            text = await asyncio.to_thread(infer)
        return text, time.monotonic() - started


async def run_local_asr(
    queue: asyncio.Queue[AudioFrame],
    pool: LocalModelPool,
    settings: Settings,
    bus: EventBus,
    stop: asyncio.Event,
) -> None:
    frame_ms = settings.audio.chunk_ms
    silence_frames_needed = max(1, settings.asr.silence_ms // frame_ms)
    min_frames = max(1, settings.asr.min_speech_ms // frame_ms)
    max_frames = max(1, int(settings.asr.max_segment_seconds * 1000 / frame_ms))
    preroll: deque[np.ndarray] = deque(maxlen=max(1, 300 // frame_ms))
    segment: list[np.ndarray] = []
    silence_frames = 0
    speech_frames = 0
    start_consecutive = 0
    segment_started = 0.0
    speaker = "REMOTE"
    calibration: list[float] = []
    adaptive_threshold = settings.asr.vad_threshold
    bus.publish(StatusEvent("asr-local", "loading", pool.selection.model))
    try:
        await pool.load()
    except Exception as exc:
        bus.publish(StatusEvent("asr-local", "failed", f"{type(exc).__name__}: {exc}"))
        return
    bus.publish(StatusEvent("asr-local", "connected", f"{pool.selection.model}/{pool.selection.compute_type}"))

    async def flush() -> None:
        nonlocal segment, silence_frames, speech_frames, start_consecutive, segment_started
        if speech_frames < min_frames:
            segment = []
            silence_frames = 0
            speech_frames = 0
            start_consecutive = 0
            return
        audio = np.concatenate(segment)
        captured_end = time.monotonic()
        utterance_id = uuid.uuid4().hex
        segment = []
        silence_frames = 0
        speech_frames = 0
        start_consecutive = 0
        audio16 = resample_linear(audio, settings.audio.sample_rate)
        text, processing = await pool.transcribe(audio16)
        tokens = re.findall(r"[a-z0-9']+", text.lower())
        repeated = False
        if len(tokens) >= 12:
            trigrams = list(zip(tokens, tokens[1:], tokens[2:]))
            repeated = len(set(trigrams)) / max(1, len(trigrams)) < 0.35
            repeated = repeated or max(tokens.count(token) for token in set(tokens)) / len(tokens) > 0.32
        if text and not repeated:
            # User-visible latency includes the silence required to decide that a
            # turn ended, not only model inference time.
            latency = max(processing, time.monotonic() - captured_end) + settings.asr.silence_ms / 1000
            bus.publish(
                TranscriptEvent(
                    speaker=speaker,
                    text=text,
                    final=True,
                    utterance_id=utterance_id,
                    latency_seconds=latency,
                )
            )
        bus.publish(StatusEvent("asr-lag", "ok" if processing < len(audio16) / 16_000 else "warning", f"{processing:.2f}s"))

    while not stop.is_set():
        try:
            frame = await asyncio.wait_for(queue.get(), timeout=0.25)
        except TimeoutError:
            continue
        try:
            speaker = frame.speaker
            rms = float(np.sqrt(np.mean(np.square(frame.samples), dtype=np.float64)))
            if len(calibration) < max(5, 1000 // frame_ms):
                calibration.append(rms)
                if len(calibration) == max(5, 1000 // frame_ms):
                    adaptive_threshold = calibrate_threshold(
                        calibration, frame.speaker, settings.asr.vad_threshold
                    )
                    bus.publish(StatusEvent(f"vad-{frame.speaker.lower()}", "calibrated", f"threshold={adaptive_threshold:.4f}"))
                continue
            speech = rms >= adaptive_threshold
            if not segment:
                preroll.append(frame.samples)
                if speech:
                    start_consecutive += 1
                    if start_consecutive >= 2:
                        segment_started = frame.captured_monotonic
                        segment.extend(preroll)
                        speech_frames = start_consecutive
                        preroll.clear()
                else:
                    start_consecutive = 0
                continue
            segment.append(frame.samples)
            if speech:
                speech_frames += 1
            silence_frames = 0 if speech else silence_frames + 1
            if silence_frames >= silence_frames_needed or len(segment) >= max_frames:
                await flush()
        finally:
            queue.task_done()
    if segment:
        await flush()
    bus.publish(StatusEvent("asr-local", "stopped"))
