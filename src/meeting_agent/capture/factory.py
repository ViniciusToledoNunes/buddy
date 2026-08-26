from __future__ import annotations

import sys


def _backend():
    if sys.platform == "win32":
        from . import windows
        return windows, windows.WindowsAudioCapture
    if sys.platform == "darwin":
        from . import macos
        return macos, macos.MacOSAudioCapture
    if sys.platform.startswith("linux"):
        from . import linux
        return linux, linux.LinuxAudioCapture
    raise RuntimeError(f"Unsupported audio platform: {sys.platform}")


class AudioCapture:
    """Platform-selecting compatibility constructor."""

    def __new__(cls, *args, **kwargs):
        _, implementation = _backend()
        return implementation(*args, **kwargs)


def list_audio_devices() -> dict[str, object]:
    module, _ = _backend()
    return module.list_devices()


def probe_audio(config) -> list[dict[str, str]]:
    module, _ = _backend()
    return module.probe(config)


def capture_capabilities() -> dict[str, object]:
    backend = {"win32": "wasapi", "darwin": "screencapturekit"}.get(
        sys.platform, "pipewire" if sys.platform.startswith("linux") else "unsupported"
    )
    return {
        "platform": sys.platform, "backend": backend, "system_audio": backend != "unsupported",
        "microphone": backend != "unsupported",
        "hotkeys_note": "Wayland may require desktop-specific global shortcut configuration"
        if sys.platform.startswith("linux") else "Accessibility permission may be required"
        if sys.platform == "darwin" else "supported",
    }
