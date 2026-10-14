from meeting_agent.echo import EchoGuard, coverage, words


def test_coverage_counts_words_in_order():
    assert coverage(words("we ship on friday"), words("so we ship on friday, right?")) == 1.0
    assert coverage(words("we ship on friday"), words("lunch is at noon")) == 0.0


def test_an_echo_heard_after_the_remote_line_is_dropped_at_once():
    guard = EchoGuard(window_seconds=6)
    guard.remote("Where are we on the rollout for the vendor page?", now=10)

    assert guard.mine("me-1", "where are we on the roll out for the vendor page", now=12) is False
    assert guard.release(now=100) == []
    assert guard.dropped == 1


def test_an_echo_that_finished_first_is_dropped_when_the_remote_line_arrives():
    guard = EchoGuard(window_seconds=6)
    assert guard.mine("me-1", "Can everyone see my screen right now?", now=10) is True

    assert guard.remote("Can everyone see my screen right now?", now=12) == ["me-1"]
    assert guard.release(now=100) == []


def test_my_own_speech_is_kept_once_the_window_passes():
    guard = EchoGuard(window_seconds=6)
    guard.remote("Where are we on the rollout?", now=10)
    guard.mine("me-1", "Almost done, I'm testing the last screen today.", now=12)

    assert guard.release(now=15) == []  # still inside the window
    assert guard.release(now=18) == [("me-1", 12)]


def test_a_short_reply_is_not_mistaken_for_an_echo():
    guard = EchoGuard(window_seconds=6)
    guard.remote("Does that sound good to everyone?", now=10)

    assert guard.mine("me-1", "Sounds good.", now=11) is True  # 2 words, not all of them just played
    assert guard.release(now=20) == [("me-1", 11)]


def test_remote_lines_outside_the_window_do_not_count():
    guard = EchoGuard(window_seconds=6)
    guard.remote("We should move the release to Monday.", now=10)

    assert guard.mine("me-1", "we should move the release to monday", now=30) is True


def test_a_zero_window_turns_the_guard_off():
    guard = EchoGuard(window_seconds=0)
    guard.remote("Can everyone see my screen?", now=10)

    assert not guard.enabled
    assert guard.remote("Can everyone see my screen?", now=11) == []
