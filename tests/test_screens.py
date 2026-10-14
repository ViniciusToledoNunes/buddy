from meeting_agent.listener import format_event
from meeting_agent.screens import ScreenShots

from test_listener import Clock, _daemon, _events, _said


def _writer(contents):
    """A fake capture that writes the next item of `contents` to the path."""
    items = iter(contents)

    def capture(path):
        data = next(items)
        if data is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True

    return capture


def test_captures_follow_the_interval_and_the_ceiling(tmp_path):
    shots = ScreenShots(tmp_path, interval=60, max_shots=2, capture=_writer([b"a", b"b", b"c"]))

    assert shots.due(0)
    shots.shoot(0)
    assert not shots.due(30)
    assert shots.due(60)
    shots.shoot(60)
    assert not shots.due(500)  # the ceiling stops it
    assert shots.count == 2


def test_an_unchanged_screen_is_not_kept_twice(tmp_path):
    shots = ScreenShots(tmp_path / "s", interval=1, max_shots=10, capture=_writer([b"same", b"same", b"new"]))

    first = shots.shoot(0)
    assert shots.shoot(1) is None
    third = shots.shoot(2)

    assert shots.take() == [first, third]
    assert shots.take() == []


def test_a_failed_capture_is_skipped(tmp_path):
    shots = ScreenShots(tmp_path, interval=1, max_shots=10, capture=_writer([None]))

    assert shots.shoot(0) is None
    assert shots.count == 0 and shots.take() == []


async def test_meeting_batches_name_the_new_screenshots(tmp_path):
    clock = Clock()
    daemon, _ = _daemon(
        tmp_path, clock, batch_min_seconds=30, batch_pause_seconds=2, screen_capture=True, screen_interval_seconds=10
    )
    frames = iter([b"slide-1", b"slide-2"])

    def capture(path, max_width):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(next(frames))
        return True

    daemon.screen_capture = capture
    await daemon.start_meeting("test")
    daemon.batcher.last_emit = clock.now
    screens_dir = daemon.storage.directory / "screens"

    await daemon.tick()  # first capture right away
    await daemon.on_final(_said("REMOTE", "Let me share my screen."))
    clock.now += 31
    await daemon.tick()  # second capture, then the batch

    batch = _events(daemon)[-1]
    assert batch["type"] == "MEETING_BATCH"
    assert len(batch["screens"]) == 2
    assert all(path.startswith(str(screens_dir)) for path in batch["screens"])
    assert f"latest_screen={batch['screens'][-1]}" in format_event(batch)

    await daemon.stop_meeting("test")
    meeting = sorted((tmp_path / "meetings").iterdir())[-1]
    assert '"screens_captured": 2' in (meeting / "metadata.json").read_text(encoding="utf-8")


async def test_no_screenshots_unless_turned_on(tmp_path):
    daemon, _ = _daemon(tmp_path)
    calls = []
    daemon.screen_capture = lambda path, width: calls.append(path) or True
    await daemon.start_meeting("test")

    await daemon.tick()

    assert daemon.screens is None and calls == []
