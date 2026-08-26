"""Backward-compatible public audio interface."""

from .capture import AudioCapture, AudioFrame, capture_capabilities, list_audio_devices, probe_audio

__all__ = ["AudioCapture", "AudioFrame", "capture_capabilities", "list_audio_devices", "probe_audio"]
