from meeting_agent.asr.local import is_hallucination


def test_whisper_outro_lines_are_dropped():
    for text in ("Thanks for watching!", "thank you for watching.", "You", " you. ", "Obrigado por assistir."):
        assert is_hallucination(text), text


def test_real_speech_is_kept():
    for text in (
        "",
        "Thank you, Arthur.",
        "You should check the vendor baseline.",
        "Thanks for watching the deploy, I saw it failed.",
    ):
        assert not is_hallucination(text), text
