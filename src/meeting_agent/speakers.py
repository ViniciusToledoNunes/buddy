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
# A line with less speech than this joins the closest voice (or none) but never starts a new
# one: a two-word reply's embedding is too noisy to be a person of its own.
NEW_VOICE_SECONDS = 3.0
SHORT_LINE_SLACK = 0.1
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


_TEAMS_NAME = re.compile(r"([A-Z][\w'’.-]+(?:…|\.\.\.)?),\s*([A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*)*(?:…|\.\.\.)?)?")
_TRUNCATED = re.compile(r"(?:…|\.\.\.)$")


def _name_word(word: str) -> bool:
    """A word that can be part of a person's name as an app writes it.

    Apps write names in title case: "Nicholas", "O'Rourke", "McDonald", an initial "D", or
    two capitals ("DJ"). A longer all-caps word ("PAGTEDI") is OCR read off slides or
    buttons, not a name.
    """
    letters = [c for c in word if c.isalpha()]
    if not letters or not word[0].isupper():
        return False
    return len(letters) <= 2 or any(c.islower() for c in letters)


def display_name(label: str) -> str | None:
    """A tile label as a person's name: "Webb, Nicholas D" -> "Nicholas Webb".

    Teams shows "Last, First Middle"; other apps show "First Last". Icons and stray
    characters the OCR reads around the label are dropped. A word cut short by the app
    ("Thornton, Bra...") is left out, keeping what is certain ("Thornton"). None when
    nothing in the label looks like a name.
    """
    text = re.sub(r"\s*\([^)]*\)", "", label)  # "(Guest)", "(External)", "(She/her)"
    text = text.strip().strip(string.punctuation.replace(".", "") + " ")
    text = re.sub(r"^[^A-Za-z]+", "", text)  # a microphone or signal icon read as "Il" or "*"
    text = re.sub(r"^(?:Il|ll|II|lI|aIl|all)\s+", "", text)
    match = _TEAMS_NAME.search(text)
    if match:
        last = match.group(1)
        firsts = (match.group(2) or "").split()
        if _TRUNCATED.search(last) or not _name_word(last):
            return None
        first = firsts[0] if firsts and not _TRUNCATED.search(firsts[0]) and _name_word(firsts[0]) else ""
        return f"{first} {last}" if first else last
    words = [word for word in re.findall(r"[A-Z][\w'’.-]*(?:…)?", text) if not _TRUNCATED.search(word)]
    if not 1 <= len(words) <= 4 or not all(_name_word(word) for word in words):
        return None
    return " ".join(words[:3])


def same_person(a: str, b: str) -> bool:
    """A truncated label ("Pierson") is the same person as the full one ("Tru Pierson")."""
    if a == b:
        return True
    short, full = sorted((a, b), key=len)
    return len(short.split()) == 1 and short in full.split()


def matches(seen: str, full: str) -> bool:
    """Whether a name read off the screen can be this participant's full name.

    Every word read must be one of the full name's words or the start of one: "Thornton"
    and "Bra Thornton" both fit "Brandyn Thornton"; "Brandon Thornton" does not.
    """
    if same_person(seen, full):
        return True
    words = full.lower().split()
    return all(any(w == word or (len(w) >= 3 and word.startswith(w)) for word in words) for w in seen.lower().split())


# --------------------------------------------------------------- participants

# Teams labels each participant's tile in its accessibility tree: the name, then what is
# going on ("video is on", "muted", "Has control of production tools"), then
# "Context menu is available". What is not the name starts in lower case or is one of these.
_PARTICIPANT_WORDS = ("has control", "context menu", "video is", "muted", "unmuted", "speaking", "hand", "organizer",
                      "presenter", "attendee", "external", "guest", "pinned", "spotlight", "in the lobby", "on hold")


@dataclass(frozen=True)
class Participant:
    name: str  # as shown: "Jae Lee"
    muted: bool
    speaking: bool = False


def parse_participant(label: str) -> Participant | None:
    """A participant from the meeting app's accessibility label, or None if it is not one."""
    parts = [part.strip() for part in label.split(",") if part.strip()]
    name_parts: list[str] = []
    for part in parts:
        lowered = part.lower()
        if not part[0].isupper() or lowered.startswith(_PARTICIPANT_WORDS):
            break
        name_parts.append(part)
        if len(name_parts) == 2:
            break
    rest = {part.lower() for part in parts[len(name_parts):]}
    name = display_name(", ".join(name_parts)) if name_parts else None
    if not name:
        return None
    return Participant(name=name, muted="muted" in rest, speaking=any("speaking" in part for part in rest))


def parse_participants(labels: list[str]) -> list[Participant]:
    seen: dict[str, Participant] = {}
    for label in labels:
        participant = parse_participant(label)
        if participant is not None:
            seen.setdefault(participant.name, participant)
    return list(seen.values())


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

    def observe(
        self,
        embedding: np.ndarray | None,
        seen: list[str],
        text: str = "",
        seconds: float | None = None,
        participants: list[Participant] | None = None,
    ) -> Attribution:
        """Name one REMOTE line.

        `seen`: names the screen showed outlined while it was said. `seconds`: how much of
        it was speech. `participants`: the call's participants as the meeting app lists
        them, when it does (Teams): a name read off the screen must be one of them, which
        also turns a cut-short label into the full name; and when exactly one person on
        the other side is unmuted, the line is theirs.
        """
        seen = [name for name in seen if name and not self.exclude(name)]
        remote = [p for p in participants or [] if not self.exclude(p.name)]
        if remote:
            seen = [full for full in (self._in_call(name, remote) for name in seen) if full]
            talking = [p.name for p in remote if p.speaking] or [p.name for p in remote if not p.muted]
            if len(talking) == 1:
                seen.append(talking[0])
        if embedding is not None and seconds is not None and seconds < NEW_VOICE_SECONDS:
            voice = self._nearest(embedding)
            if voice is None:
                embedding = None  # too short to start a voice of its own
            else:
                voice.votes.update(seen)
                return self._attribute(voice)
        if embedding is None:
            # Too short for the voice model, or voice off: the screen alone decides this line.
            if not seen:
                return Attribution(None, None, False)
            name, count = Counter(seen).most_common(1)[0]
            return Attribution(self._canonical(name), None, count == len(seen))
        voice = self._voice_for(embedding, text)
        voice.votes.update(seen)
        return self._attribute(voice)

    @staticmethod
    def _in_call(name: str, participants: list[Participant]) -> str | None:
        found = [p.name for p in participants if matches(name, p.name)]
        return found[0] if len(found) == 1 else None

    def _nearest(self, embedding: np.ndarray) -> Voice | None:
        """The voice a short line most likely belongs to, without changing that voice."""
        scores = [float(voice.centroid @ embedding) for voice in self.voices]
        if scores and max(scores) >= self.threshold - SHORT_LINE_SLACK:
            voice = self.voices[int(np.argmax(scores))]
            voice.lines += 1
            return voice
        return None

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
