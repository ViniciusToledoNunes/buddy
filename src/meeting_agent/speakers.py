"""Who is talking on the other side of a meeting.

System audio arrives as one stream, so every line from it was "REMOTE". Two signals
put a name on it, and each covers the other's blind spot:

- **The screen.** Meeting apps outline the tile of whoever is talking. A capture taken
  while the other side speaks shows which tile is outlined, and the name on it
  (`active_speaker.py`).
- **The voice.** A small local model turns each utterance into a voice embedding. Lines
  whose embeddings are close belong to the same voice, whether or not the screen shows
  anyone (shared screen, camera off, two people at once).

A voice gets a name once the screen has named it consistently, or when it matches a
voice remembered from an earlier meeting. Only embeddings are kept, never audio, and
only when `listen.remember_voices` is on; `buddy voices forget` deletes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import string
import tempfile
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

# WeSpeaker ResNet34-LM trained on VoxCeleb (CC BY 4.0, https://github.com/wenet-e2e/wespeaker),
# as converted to ONNX by sherpa-onnx. 26 MB, 256-dimensional embeddings, 16 kHz input.
VOICE_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "wespeaker_en_voxceleb_resnet34_LM.onnx"
)
VOICE_MODEL_SHA256 = "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"
VOICE_MODEL_FILE = "wespeaker_en_voxceleb_resnet34_LM.onnx"

SAMPLE_RATE = 16_000
_EPS = float(np.finfo(np.float32).eps)


# ------------------------------------------------------------------ features


def _mel_banks(n_mels: int = 80, n_fft: int = 512, low: float = 20.0, high: float = 8000.0) -> np.ndarray:
    def mel(freq: np.ndarray | float) -> np.ndarray | float:
        return 1127.0 * np.log(1.0 + np.asarray(freq) / 700.0)

    points = np.linspace(mel(low), mel(high), n_mels + 2)
    bins = mel(np.arange(n_fft // 2) * SAMPLE_RATE / n_fft)
    banks = np.zeros((n_mels, n_fft // 2 + 1))
    for index in range(n_mels):
        left, centre, right = points[index : index + 3]
        rising = (bins - left) / (centre - left)
        falling = (right - bins) / (right - centre)
        banks[index, : n_fft // 2] = np.maximum(0.0, np.minimum(rising, falling))
    return banks


_BANKS = _mel_banks()
_POVEY = np.power(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399), 0.85)


def fbank(samples: np.ndarray) -> np.ndarray | None:
    """80 log-mel filterbank frames, Kaldi style, mean-normalised: what WeSpeaker expects.

    25 ms windows every 10 ms, DC removed, 0.97 pre-emphasis, Povey window, no dither.
    The model was trained on 16-bit sample values, hence the scale. None for audio
    shorter than one window.
    """
    signal = np.asarray(samples, dtype=np.float64) * 32768.0
    window, hop = 400, 160
    if signal.size < window:
        return None
    count = 1 + (signal.size - window) // hop
    frames = signal[np.arange(window)[None, :] + hop * np.arange(count)[:, None]]
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames = np.concatenate([frames[:, :1], frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1) * _POVEY
    power = np.abs(np.fft.rfft(frames, n=512)) ** 2
    features = np.log(np.maximum(power @ _BANKS.T, _EPS))
    return (features - features.mean(axis=0, keepdims=True)).astype(np.float32)


class VoiceModel:
    """The embedding model, loaded on first use. Unit-length 256-float vectors."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._session: Any = None

    def available(self) -> bool:
        return self.path.exists()

    def embed(self, samples: np.ndarray) -> np.ndarray | None:
        features = fbank(samples)
        if features is None:
            return None
        if self._session is None:
            import onnxruntime  # faster-whisper already depends on it

            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = 2  # leave the cores to transcription
            self._session = onnxruntime.InferenceSession(
                str(self.path), options, providers=["CPUExecutionProvider"]
            )
        vector = self._session.run(None, {"feats": features[None]})[0][0]
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else None


def download_model(directory: Path, url: str = VOICE_MODEL_URL, sha256: str = VOICE_MODEL_SHA256) -> Path:
    """Fetch the voice model once, checking its digest before it is used."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / VOICE_MODEL_FILE
    if target.exists() and _digest(target) == sha256:
        return target
    with tempfile.NamedTemporaryFile(dir=directory, delete=False, suffix=".part") as handle:
        partial = Path(handle.name)
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
            while chunk := response.read(1 << 20):
                handle.write(chunk)
    if _digest(partial) != sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"the voice model from {url} does not match its expected digest")
    partial.replace(target)
    return target


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


# ---------------------------------------------------------------------- names


_TEAMS_NAME = re.compile(r"([A-Z][\w'’.-]+),\s*([A-Z][\w'’.-]+)?")


def display_name(label: str) -> str | None:
    """A tile label as a person's name: "Webb, Nicholas D" -> "Nicholas Webb".

    Teams shows "Last, First Middle"; other apps show "First Last". Icons and stray
    characters the OCR reads around the label are dropped. None when nothing in the
    label looks like a name.
    """
    text = label.strip().strip(string.punctuation + " ")
    text = re.sub(r"^[^A-Za-z]+", "", text)  # a microphone or signal icon read as "Il" or "*"
    text = re.sub(r"^(?:Il|ll|II|lI)\s+", "", text)
    match = _TEAMS_NAME.search(text)
    if match:
        last, first = match.group(1), match.group(2)
        return f"{first} {last}" if first else last
    words = [word for word in re.findall(r"[A-Z][\w'’.-]+", text)]
    if 1 <= len(words) <= 4:
        return " ".join(words[:3])
    return None


def same_person(a: str, b: str) -> bool:
    """A truncated label ("Pierson") is the same person as the full one ("Tru Pierson")."""
    if a == b:
        return True
    short, full = sorted((a, b), key=len)
    return len(short.split()) == 1 and short in full.split()


# ----------------------------------------------------------------- voice book


class VoiceBook:
    """Voices remembered across meetings: one mean embedding per name, on this machine only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.voices: dict[str, dict[str, Any]] = {}
        try:
            self.voices = json.loads(path.read_text(encoding="utf-8")).get("voices", {})
        except (OSError, ValueError, AttributeError):
            self.voices = {}

    def names(self) -> list[str]:
        return sorted(self.voices)

    def match(self, embedding: np.ndarray, threshold: float) -> tuple[str, float] | None:
        best: tuple[str, float] | None = None
        for name, entry in self.voices.items():
            centroid = np.asarray(entry["centroid"], dtype=np.float32)
            score = float(centroid @ embedding / max(np.linalg.norm(centroid), _EPS))
            if score >= threshold and (best is None or score > best[1]):
                best = (name, score)
        return best

    def learn(self, name: str, centroid: np.ndarray, count: int) -> None:
        """Fold a meeting's voice into the remembered one, weighted by how much each heard."""
        entry = self.voices.get(name)
        if entry:
            old = np.asarray(entry["centroid"], dtype=np.float32)
            total = entry["count"] + count
            merged = (old * entry["count"] + centroid * count) / total
        else:
            merged, total = centroid, count
        merged = merged / max(float(np.linalg.norm(merged)), _EPS)
        self.voices[name] = {
            "centroid": [round(float(value), 6) for value in merged],
            "count": int(min(total, 500)),  # an old voice still adapts to a new microphone
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def forget(self, name: str | None = None) -> list[str]:
        removed = list(self.voices) if name is None else [key for key in self.voices if key == name]
        for key in removed:
            del self.voices[key]
        return removed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"voices": self.voices}, indent=1), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)


# -------------------------------------------------------------------- tracker


@dataclass
class Voice:
    """One voice heard in this meeting."""

    label: str
    total: np.ndarray
    lines: int = 1
    votes: Counter[str] = field(default_factory=Counter)
    remembered: str | None = None  # matched in the voice book
    first_text: str = ""

    @property
    def centroid(self) -> np.ndarray:
        return self.total / max(float(np.linalg.norm(self.total)), _EPS)


@dataclass(frozen=True)
class Attribution:
    name: str | None  # None: nobody named yet
    voice: str | None  # this meeting's label for the voice, "A", "B", ...
    sure: bool

    def tag(self) -> str:
        """How the line is written: "Nick Webb", "Nick Webb?", "voice B", or ""."""
        if self.name:
            return self.name if self.sure else f"{self.name}?"
        return f"voice {self.voice}" if self.voice else ""


class SpeakerTracker:
    """Puts a name on each REMOTE line from its voice and from what the screen showed."""

    def __init__(
        self,
        threshold: float = 0.6,
        votes_needed: int = 2,
        book: VoiceBook | None = None,
        exclude: Callable[[str], bool] = lambda name: False,
    ) -> None:
        self.threshold = threshold
        self.votes_needed = votes_needed
        self.book = book
        self.exclude = exclude  # the user's own tile, outlined when the user talks
        self.voices: list[Voice] = []

    def observe(self, embedding: np.ndarray | None, seen: list[str], text: str = "") -> Attribution:
        seen = [name for name in seen if name and not self.exclude(name)]
        if embedding is None:
            # Too short for the voice model, or voice off: the screen alone decides this line.
            if not seen:
                return Attribution(None, None, False)
            name, count = Counter(seen).most_common(1)[0]
            return Attribution(self._canonical(name), None, count == len(seen))
        voice = self._voice_for(embedding, text)
        voice.votes.update(seen)
        return self._attribute(voice)

    def _voice_for(self, embedding: np.ndarray, text: str) -> Voice:
        scores = [float(voice.centroid @ embedding) for voice in self.voices]
        if scores and max(scores) >= self.threshold:
            voice = self.voices[int(np.argmax(scores))]
            voice.total = voice.total + embedding
            voice.lines += 1
            return voice
        voice = Voice(label=_label(len(self.voices)), total=embedding.copy(), first_text=text[:80])
        if self.book is not None:
            found = self.book.match(embedding, self.threshold + 0.05)
            voice.remembered = found[0] if found else None
        self.voices.append(voice)
        return voice

    def _attribute(self, voice: Voice) -> Attribution:
        if voice.votes:
            name, count = voice.votes.most_common(1)[0]
            share = count / sum(voice.votes.values())
            if count >= self.votes_needed and share >= 0.6:
                return Attribution(self._canonical(name), voice.label, True)
            if voice.remembered is None:
                return Attribution(self._canonical(name), voice.label, False)
        if voice.remembered:
            return Attribution(voice.remembered, voice.label, True)
        return Attribution(None, voice.label, False)

    def _canonical(self, name: str) -> str:
        """The longest form of a name seen this meeting: "Pierson" becomes "Tru Pierson"."""
        forms = {seen for voice in self.voices for seen in voice.votes} | {name}
        return max((form for form in forms if same_person(form, name)), key=len)

    def named(self) -> dict[str, Voice]:
        """Voices with a settled name, for the voice book at the end of the meeting."""
        result: dict[str, Voice] = {}
        for voice in self.voices:
            attribution = self._attribute(voice)
            if attribution.name and attribution.sure and voice.votes:
                result.setdefault(attribution.name, voice)
        return result

    def unnamed(self) -> list[Voice]:
        return [voice for voice in self.voices if not self._attribute(voice).name and voice.lines >= 2]

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for voice in self.voices:
            attribution = self._attribute(voice)
            out.append(
                {
                    "voice": voice.label,
                    "name": attribution.name,
                    "sure": attribution.sure,
                    "lines": voice.lines,
                    "screen_votes": dict(voice.votes),
                    "remembered": voice.remembered,
                    "first_line": voice.first_text,
                }
            )
        return out

    def remember(self, min_lines: int = 3) -> list[str]:
        """Teach the voice book the voices the screen named this meeting."""
        if self.book is None:
            return []
        learned = []
        for name, voice in self.named().items():
            if voice.lines >= min_lines:
                self.book.learn(name, voice.centroid, voice.lines)
                learned.append(name)
        if learned:
            self.book.save()
        return learned

    def state(self) -> dict[str, Any]:
        """Every voice's centroid, so a voice nobody named can be named after the meeting."""
        return {
            "voices": [
                {"voice": voice.label, "lines": voice.lines, "centroid": [round(float(x), 6) for x in voice.centroid]}
                for voice in self.voices
            ]
        }


def _label(index: int) -> str:
    letters = string.ascii_uppercase
    return letters[index] if index < 26 else f"{letters[index // 26 - 1]}{letters[index % 26]}"


def name_voice(meeting_dir: Path, label: str, name: str, book: VoiceBook) -> bool:
    """`buddy voices name`: remember a voice the meeting could not name by itself."""
    try:
        state = json.loads((meeting_dir / "voices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    for voice in state.get("voices", []):
        if voice.get("voice") == label:
            book.learn(name, np.asarray(voice["centroid"], dtype=np.float32), int(voice.get("lines", 1)))
            book.save()
            return True
    return False
