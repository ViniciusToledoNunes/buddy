from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import psutil
from faster_whisper import WhisperModel

from .config import Settings, project_root
from .doctor import hardware_snapshot
from .system import find_ffmpeg


def _read_wav(path: Path, seconds: float | None = None) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.getnframes() if seconds is None else min(handle.getnframes(), int(rate * seconds))
        raw = handle.readframes(frames)
        channels = handle.getnchannels()
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


def _peak_monitor(done: threading.Event, peak: list[int]) -> None:
    process = psutil.Process()
    while not done.wait(0.05):
        try:
            peak[0] = max(peak[0], process.memory_info().rss)
        except psutil.Error:
            return


def _run_process_measured(args: list[str]) -> tuple[float, float, str, str, int]:
    started = time.perf_counter()
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    peak = 0
    while process.poll() is None:
        try:
            peak = max(peak, psutil.Process(process.pid).memory_info().rss)
        except psutil.Error:
            pass
        time.sleep(0.02)
    stdout, stderr = process.communicate()
    return time.perf_counter() - started, peak / 1024**2, stdout, stderr, process.returncode


def _whispercpp_result(wav: Path, short_audio: np.ndarray, rate: int, actual_duration: float) -> dict[str, Any] | None:
    root = project_root()
    executable = root / "tools" / "whispercpp" / "Release" / "whisper-cli.exe"
    model = root / "models" / "whispercpp" / "ggml-base.en-q5_1.bin"
    if not executable.exists() or not model.exists():
        return None
    short_wav = wav.parent / "sample-3s.wav"
    with wave.open(str(short_wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(short_audio, -1, 1) * 32767).astype("<i2").tobytes())
    common = [
        str(executable), "-m", str(model), "-t", "4", "-bo", "1", "-bs", "1", "-nf", "-ng", "-l", "en", "-nt", "-np",
    ]
    latency, short_peak, _, short_error, short_code = _run_process_measured(common + ["-f", str(short_wav)])
    processing, peak, stdout, stderr, code = _run_process_measured(common + ["-f", str(wav)])
    if code or short_code:
        return {
            "backend": "whisper.cpp",
            "model": "base.en-q5_1",
            "device": "cpu",
            "compute_type": "q5_1",
            "audio_seconds": round(actual_duration, 3),
            "status": "failed",
            "error": (stderr or short_error)[-500:],
        }
    return {
        "backend": "whisper.cpp",
        "model": "base.en-q5_1",
        "device": "cpu",
        "compute_type": "q5_1",
        "audio_seconds": round(actual_duration, 3),
        "processing_seconds": round(processing, 3),
        "real_time_factor": round(processing / actual_duration, 4),
        "approximate_3s_latency_seconds": round(latency, 3),
        "load_seconds": None,
        "ram_peak_mb": round(peak, 1),
        "process_ram_peak_mb": round(peak, 1),
        "vram_mb": None,
        "characters": len(stdout.strip()),
        "status": "ok",
        "note": "CLI wall time includes model startup; production faster-whisper keeps its model hot",
    }


def run_benchmark(settings: Settings, models: list[str] | None = None) -> dict[str, Any]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found")
    source = Path(settings.benchmark.source)
    if not source.exists():
        raise FileNotFoundError(source)
    duration = settings.benchmark.seconds
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="meeting-agent-benchmark-") as temporary:
        wav = Path(temporary) / "sample.wav"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-t", str(duration), "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            check=True,
        )
        audio, rate = _read_wav(wav)
        actual_duration = len(audio) / rate
        short_audio = audio[: rate * 3]
        for model_name in models or settings.benchmark.models:
            before = psutil.Process().memory_info().rss
            load_started = time.perf_counter()
            try:
                model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4, num_workers=1)
                load_seconds = time.perf_counter() - load_started
                # Warm feature path, then time a live-sized 3 second utterance.
                list(model.transcribe(short_audio, language="en", beam_size=1, best_of=1, condition_on_previous_text=False)[0])
                latency_started = time.perf_counter()
                list(model.transcribe(short_audio, language="en", beam_size=1, best_of=1, condition_on_previous_text=False)[0])
                approximate_latency = time.perf_counter() - latency_started
                done = threading.Event()
                peak = [psutil.Process().memory_info().rss]
                monitor = threading.Thread(target=_peak_monitor, args=(done, peak), daemon=True)
                monitor.start()
                started = time.perf_counter()
                segments, _ = model.transcribe(
                    audio,
                    language="en" if settings.language == "en" else None,
                    beam_size=1,
                    best_of=1,
                    condition_on_previous_text=False,
                    vad_filter=True,
                )
                transcript = " ".join(segment.text.strip() for segment in segments)
                processing = time.perf_counter() - started
                done.set()
                monitor.join(1)
                result = {
                    "backend": "faster-whisper",
                    "model": model_name,
                    "device": "cpu",
                    "compute_type": "int8",
                    "audio_seconds": round(actual_duration, 3),
                    "processing_seconds": round(processing, 3),
                    "real_time_factor": round(processing / actual_duration, 4),
                    "approximate_3s_latency_seconds": round(approximate_latency, 3),
                    "load_seconds": round(load_seconds, 3),
                    "ram_peak_mb": round((peak[0] - before) / 1024**2, 1),
                    "process_ram_peak_mb": round(peak[0] / 1024**2, 1),
                    "vram_mb": None,
                    "characters": len(transcript),
                    "status": "ok",
                }
            except Exception as exc:
                result = {
                    "backend": "faster-whisper",
                    "model": model_name,
                    "device": "cpu",
                    "compute_type": "int8",
                    "audio_seconds": round(actual_duration, 3),
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)
            try:
                del model
            except UnboundLocalError:
                pass
        cpp = _whispercpp_result(wav, short_audio, rate, actual_duration)
        if cpp is not None:
            results.append(cpp)
    # Only the persistent, in-process backend is eligible for automatic live
    # selection. whisper.cpp CLI is measured as the requested quantized control.
    valid = [x for x in results if x.get("status") == "ok" and x.get("backend") == "faster-whisper"]
    realtime = [x for x in valid if x["real_time_factor"] < 0.7 and x["approximate_3s_latency_seconds"] < 3]
    # Latency is the primary product requirement. Accuracy is a tie-breaker only
    # when segment latency is effectively equal (within 150 ms).
    recommended = None
    if realtime:
        fastest_latency = min(x["approximate_3s_latency_seconds"] for x in realtime)
        near_fastest = [x for x in realtime if x["approximate_3s_latency_seconds"] <= fastest_latency + 0.15]
        rank = {"tiny.en": 1, "base.en": 2, "distil-small.en": 3, "small.en": 4}
        recommended = max(near_fastest, key=lambda x: rank.get(x["model"], 0))
    if recommended is None and valid:
        recommended = min(valid, key=lambda x: x["real_time_factor"])
    report = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": str(source),
        "hardware": hardware_snapshot(),
        "results": results,
        "recommended": None if recommended is None else recommended["model"],
    }
    (project_root() / "benchmark-results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
