import hashlib
import json
import stat

import numpy as np
import pytest

from meeting_agent import speakers
from meeting_agent.speakers import (
    Attribution,
    SpeakerTracker,
    VoiceBook,
    VoiceModel,
    display_name,
    matches,
    parse_participant,
    parse_participants,
    download_model,
    fbank,
    name_voice,
    same_person,
)


def _voice(seed):
    vector = np.random.default_rng(seed).normal(size=256)
    return vector / np.linalg.norm(vector)


def _line(base, seed, noise=0.35):
    """Another utterance of the same voice: the base embedding plus some noise."""
    jitter = np.random.default_rng(1000 + seed).normal(size=256)
    vector = base + noise * jitter / np.linalg.norm(jitter)
    return vector / np.linalg.norm(vector)


# ------------------------------------------------------------------- features


def test_fbank_gives_80_mean_normalised_bands_per_10ms():
    tone = np.sin(2 * np.pi * 440 * np.arange(16_000) / 16_000).astype(np.float32) * 0.1
    features = fbank(tone)

    assert features.shape == (98, 80)
    assert features.dtype == np.float32
    assert np.allclose(features.mean(axis=0), 0, atol=1e-4)


def test_fbank_of_audio_shorter_than_a_window_is_none():
    assert fbank(np.zeros(100, dtype=np.float32)) is None


def test_the_voice_model_returns_unit_vectors_and_loads_once(tmp_path):
    class Session:
        calls = 0

        def run(self, outputs, inputs):
            Session.calls += 1
            assert inputs["feats"].shape[0] == 1 and inputs["feats"].shape[2] == 80
            return [np.array([[3.0, 4.0]])]

    model = VoiceModel(tmp_path / "model.onnx")
    model._session = Session()
    vector = model.embed(np.random.default_rng(0).normal(size=16_000).astype(np.float32) * 0.05)

    assert np.allclose(vector, [0.6, 0.8])
    assert model.embed(np.zeros(10, dtype=np.float32)) is None  # too short to look at
    assert Session.calls == 1
    assert not model.available()


def test_the_model_download_is_checked_against_its_digest(tmp_path):
    source = tmp_path / "source.onnx"
    source.write_bytes(b"model bytes")
    digest = hashlib.sha256(b"model bytes").hexdigest()

    path = download_model(tmp_path / "models", url=source.as_uri(), sha256=digest)
    assert path.read_bytes() == b"model bytes"
    assert download_model(tmp_path / "models", url="file:///nowhere", sha256=digest) == path  # already there

    with pytest.raises(RuntimeError, match="digest"):
        download_model(tmp_path / "other", url=source.as_uri(), sha256="0" * 64)
    assert list((tmp_path / "other").iterdir()) == []  # nothing half-written is left


# ---------------------------------------------------------------------- names


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("Reyes, Dana M", "Dana Reyes"),
        ("Il Okafor, Chidi", "Chidi Okafor"),
        ("Lindqvist, Mae *", "Mae Lindqvist"),
        ("Lindqvist,", "Lindqvist"),
        ("Dana Reyes", "Dana Reyes"),
        ("Thornton, Bra...", "Thornton"),  # cut short by the app: only what is certain
        ("Reyes, Da…", "Reyes"),
        ("Jeyarama...", None),
        ("Doe, Jane (Guest)", "Jane Doe"),
        ("DJ O'Rourke", "DJ O'Rourke"),
        ("PAGTEDI", None),  # slide or button text read in capitals is not a name
        ("FAGATRI ROA", None),
        ("02:28", None),
        ("", None),
    ],
)
def test_tile_labels_become_names(label, name):
    assert display_name(label) == name


def test_a_truncated_label_is_the_same_person():
    assert same_person("Lindqvist", "Mae Lindqvist")
    assert same_person("Dana Reyes", "Dana Reyes")
    assert not same_person("Dana Reyes", "Mae Lindqvist")
    assert not same_person("Dana Cole", "Dana Reyes")


def test_attribution_tags():
    assert Attribution("Dana Reyes", "A", True).tag() == "Dana Reyes"
    assert Attribution("Dana Reyes", "A", False).tag() == "Dana Reyes?"
    assert Attribution(None, "C", False).tag() == "voice C"
    assert Attribution(None, None, False).tag() == ""


# -------------------------------------------------------------------- tracker


def test_lines_of_one_voice_share_a_label_and_another_voice_gets_its_own():
    dana, chidi = _voice(1), _voice(2)
    tracker = SpeakerTracker()

    labels = [tracker.observe(_line(base, n), []).voice for n, base in enumerate([dana, dana, chidi, dana, chidi])]

    assert labels == ["A", "A", "B", "A", "B"]


def test_the_screen_settles_a_voice_name_after_two_sightings():
    dana = _voice(1)
    tracker = SpeakerTracker(votes_needed=2)

    first = tracker.observe(_line(dana, 1), ["Dana Reyes"])
    second = tracker.observe(_line(dana, 2), ["Dana Reyes"])
    unseen = tracker.observe(_line(dana, 3), [])  # shared screen: nobody outlined

    assert (first.name, first.sure) == ("Dana Reyes", False)
    assert (second.name, second.sure) == ("Dana Reyes", True)
    assert (unseen.name, unseen.sure, unseen.voice) == ("Dana Reyes", True, "A")


def test_a_stray_sighting_does_not_rename_a_settled_voice():
    dana = _voice(1)
    tracker = SpeakerTracker()
    for n in range(4):
        tracker.observe(_line(dana, n), ["Dana Reyes"])

    crossing = tracker.observe(_line(dana, 9), ["Chidi Okafor"])  # the outline moved as she finished

    assert crossing.name == "Dana Reyes" and crossing.sure


def test_the_users_own_tile_is_never_a_remote_name():
    tracker = SpeakerTracker(exclude=lambda name: "Sam" in name.split())

    who = tracker.observe(_voice(1), ["Sam Ortiz", "Sam Ortiz"])

    assert who.name is None and who.voice == "A"


def test_a_truncated_name_takes_its_full_form():
    mae = _voice(3)
    tracker = SpeakerTracker()
    tracker.observe(_line(mae, 1), ["Lindqvist"])

    who = tracker.observe(_line(mae, 2), ["Mae Lindqvist", "Lindqvist"])

    assert who.name == "Mae Lindqvist"


def test_without_a_voice_the_screen_alone_names_the_line():
    tracker = SpeakerTracker()

    assert tracker.observe(None, ["Dana Reyes", "Dana Reyes"]) == Attribution("Dana Reyes", None, True)
    assert tracker.observe(None, ["Dana Reyes", "Chidi Okafor"]).sure is False
    assert tracker.observe(None, []) == Attribution(None, None, False)
    assert tracker.voices == []


def test_unnamed_voices_and_the_summary():
    dana, chidi = _voice(1), _voice(2)
    tracker = SpeakerTracker()
    tracker.observe(_line(dana, 1), ["Dana Reyes"], "first from dana")
    tracker.observe(_line(dana, 2), ["Dana Reyes"])
    tracker.observe(_line(chidi, 1), [], "hello from the other voice")
    tracker.observe(_line(chidi, 2), [])

    assert [voice.label for voice in tracker.unnamed()] == ["B"]
    summary = tracker.summary()
    assert summary[0]["name"] == "Dana Reyes" and summary[0]["screen_votes"] == {"Dana Reyes": 2}
    assert summary[1]["name"] is None and summary[1]["first_line"] == "hello from the other voice"
    assert [voice["voice"] for voice in tracker.state()["voices"]] == ["A", "B"]


# ----------------------------------------------------------------- voice book


def test_a_named_voice_is_remembered_and_recognised_without_the_screen(tmp_path):
    dana = _voice(1)
    book = VoiceBook(tmp_path / "voices.json")
    first = SpeakerTracker(book=book)
    for n in range(4):
        first.observe(_line(dana, n), ["Dana Reyes"])

    assert first.remember() == ["Dana Reyes"]
    assert stat.S_IMODE((tmp_path / "voices.json").stat().st_mode) == 0o600
    stored = json.loads((tmp_path / "voices.json").read_text())
    assert set(stored["voices"]) == {"Dana Reyes"} and len(stored["voices"]["Dana Reyes"]["centroid"]) == 256

    later = SpeakerTracker(book=VoiceBook(tmp_path / "voices.json"))
    who = later.observe(_line(dana, 50), [])

    assert (who.name, who.sure) == ("Dana Reyes", True)


def test_a_voice_heard_too_little_or_never_named_is_not_remembered(tmp_path):
    book = VoiceBook(tmp_path / "voices.json")
    tracker = SpeakerTracker(book=book)
    tracker.observe(_line(_voice(1), 1), ["Dana Reyes"])
    tracker.observe(_line(_voice(1), 2), ["Dana Reyes"])
    for n in range(5):
        tracker.observe(_line(_voice(2), n), [])

    assert tracker.remember(min_lines=3) == []
    assert not (tmp_path / "voices.json").exists()
    assert SpeakerTracker().remember() == []  # no book at all


def test_learning_again_blends_the_voice_and_forget_deletes_it(tmp_path):
    book = VoiceBook(tmp_path / "voices.json")
    book.learn("Dana Reyes", _voice(1), 3)
    book.learn("Dana Reyes", _voice(1), 3)
    book.learn("Chidi Okafor", _voice(2), 1)
    book.save()

    assert book.voices["Dana Reyes"]["count"] == 6
    assert book.match(_voice(1), 0.6)[0] == "Dana Reyes"
    assert book.match(_voice(9), 0.6) is None
    assert book.forget("Dana Reyes") == ["Dana Reyes"]
    assert book.forget() == ["Chidi Okafor"]
    book.save()
    assert VoiceBook(tmp_path / "voices.json").names() == []


def test_a_broken_voice_book_reads_as_empty(tmp_path):
    (tmp_path / "voices.json").write_text("{not json")

    assert VoiceBook(tmp_path / "voices.json").voices == {}


def test_a_meeting_voice_can_be_named_afterwards(tmp_path):
    meeting = tmp_path / "2026-01-05_100000"
    meeting.mkdir()
    tracker = SpeakerTracker()
    for n in range(3):
        tracker.observe(_line(_voice(4), n), [])
    (meeting / "voices.json").write_text(json.dumps(tracker.state()))
    book = VoiceBook(tmp_path / "voices.json")

    assert name_voice(meeting, "A", "Mae Lindqvist", book)
    assert not name_voice(meeting, "Z", "Nobody", book)
    assert not name_voice(tmp_path / "missing", "A", "Nobody", book)
    assert VoiceBook(tmp_path / "voices.json").match(_voice(4), 0.6)[0] == "Mae Lindqvist"


def test_labels_run_past_z():
    assert speakers._label(0) == "A"
    assert speakers._label(25) == "Z"
    assert speakers._label(26) == "AA"


def test_a_bare_yaml_off_turns_speaker_names_off():
    import yaml

    from meeting_agent.config import Settings

    assert Settings.model_validate(yaml.safe_load("listen:\n  speaker_names: off\n")).listen.speaker_names == "off"
    assert Settings.model_validate({"listen": {"speaker_names": "both"}}).listen.speaker_names == "both"


# --------------------------------------------------------------- participants


def test_participants_are_read_from_the_apps_labels():
    lee = parse_participant("Lee, Jae, Has control of production tools, muted, Context menu is available")
    tim = parse_participant("Denison, Tim, video is on, Has control of production tools, Context menu is available")
    guest = parse_participant("Jane Doe (Guest), muted, Context menu is available")

    assert (lee.name, lee.muted) == ("Jae Lee", True)
    assert (tim.name, tim.muted) == ("Tim Denison", False)
    assert guest.name == "Jane Doe"
    assert parse_participant("Context menu is available") is None
    assert [p.name for p in parse_participants(["Lee, Jae, muted", "Lee, Jae, muted", "Mesa, Steve"])] == ["Jae Lee", "Steve Mesa"]


def test_a_name_read_off_the_screen_fits_a_full_name_word_by_word():
    assert matches("Bra Thornton", "Brandyn Thornton")
    assert matches("Thornton", "Brandyn Thornton")
    assert not matches("Brandon Thornton", "Brandyn Thornton")
    assert not matches("Br Thornton", "Dana Thornton")


def _call(*people):
    return parse_participants([f"{last}, {first}{', muted' if muted else ''}, Context menu is available"
                               for first, last, muted in people])


def test_in_a_call_a_screen_name_becomes_the_participants_full_name_or_is_dropped():
    call = _call(("Brandyn", "Thornton", True), ("Tim", "Denison", True))
    tracker = SpeakerTracker()

    assert tracker.observe(None, ["Bra Thornton"], participants=call).name == "Brandyn Thornton"
    assert tracker.observe(None, ["Slide Title"], participants=call).name is None


def test_the_only_unmuted_person_on_the_other_side_names_the_line():
    tracker = SpeakerTracker(exclude=lambda name: "Vinicius" in name)
    call = _call(("Vinicius", "Nunes", False), ("Brandyn", "Thornton", False), ("Tim", "Denison", True))
    two = _call(("Brandyn", "Thornton", False), ("Tim", "Denison", False))

    alone = tracker.observe(None, [], participants=call)  # shared screen: no tile outlined
    assert (alone.name, alone.sure) == ("Brandyn Thornton", True)
    assert tracker.observe(None, [], participants=two).name is None  # two unmuted: the call alone cannot tell


def test_a_short_line_joins_the_closest_voice_but_never_starts_one():
    dana, chidi = _voice(1), _voice(2)
    tracker = SpeakerTracker()
    tracker.observe(_line(dana, 1), ["Dana Reyes"], seconds=6)

    joined = tracker.observe(_line(dana, 2), [], seconds=1.5)
    stranger = tracker.observe(_line(chidi, 3), [], seconds=1.5)

    assert joined.voice == "A"
    assert stranger.voice is None and len(tracker.voices) == 1
    assert tracker.observe(_line(chidi, 4), [], seconds=5).voice == "B"
