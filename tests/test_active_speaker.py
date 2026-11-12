import struct
import sys

import numpy as np
import pytest

from meeting_agent import active_speaker
from meeting_agent.active_speaker import (
    TextBox,
    border_mask,
    name_in_tile,
    outlined_tiles,
    read_bmp,
    recognise_text,
    speaker_on_screen,
)

VIOLET = (110, 108, 190)


def _frame(outlined=((40, 30, 240, 170),), plain=((260, 30, 460, 170),)):
    """A dark call window: tiles with a grey edge, and the speaking ones outlined in violet."""
    frame = np.full((220, 500, 3), 30, dtype=np.int16)
    for x0, y0, x1, y1 in plain:
        frame[y0:y1, x0:x1] = 60
    for x0, y0, x1, y1 in outlined:
        frame[y0:y1, x0:x1] = 60
        for thickness in range(2):
            frame[y0 + thickness, x0:x1] = VIOLET
            frame[y1 - 1 - thickness, x0:x1] = VIOLET
            frame[y0:y1, x0 + thickness] = VIOLET
            frame[y0:y1, x1 - 1 - thickness] = VIOLET
    return frame


def _write_bmp(path, rgb):
    height, width, _ = rgb.shape
    stride = (width * 3 + 3) // 4 * 4
    rows = bytearray()
    for row in rgb[::-1]:  # BMP rows run bottom-up
        line = bytes(int(v) for pixel in row for v in pixel[::-1])  # RGB -> BGR
        rows += line + b"\0" * (stride - len(line))
    header = struct.pack("<2sIHHI", b"BM", 54 + len(rows), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(rows), 2835, 2835, 0, 0)
    path.write_bytes(header + info + bytes(rows))


def test_a_bmp_reads_back_as_rgb(tmp_path):
    rgb = np.zeros((3, 5, 3), dtype=np.int16)
    rgb[0, 0] = (255, 0, 0)
    rgb[2, 4] = (0, 0, 255)
    _write_bmp(tmp_path / "f.bmp", rgb)

    assert np.array_equal(read_bmp(tmp_path / "f.bmp"), rgb)


def test_only_the_outlined_tile_is_found():
    tiles = outlined_tiles(border_mask(_frame()))

    assert len(tiles) == 1
    x0, y0, x1, y1 = tiles[0]
    assert abs(x0 - 40) <= 2 and abs(y0 - 30) <= 2 and abs(x1 - 240) <= 2 and abs(y1 - 169) <= 2


def test_no_outline_means_no_tile():
    assert outlined_tiles(border_mask(_frame(outlined=()))) == []


def test_the_name_is_read_from_the_tiles_bottom_left_corner():
    tile = (40, 30, 240, 170)
    boxes = [
        TextBox("Reyes, Dana M", 48, 150, 80, 12),  # the label
        TextBox("Okafor, Chidi", 268, 150, 80, 12),  # the next tile's label
        TextBox("02:28", 60, 40, 30, 12),  # the call timer, top of the tile
    ]

    assert name_in_tile(tile, boxes) == "Dana Reyes"
    assert name_in_tile(tile, boxes[1:]) is None


def test_speaker_on_screen_reads_the_outlined_tile(tmp_path):
    calls = []

    def text(path):
        calls.append(path)
        return [TextBox("Il Okafor, Chidi", 46, 152, 90, 12)]

    assert speaker_on_screen(tmp_path / "x.jpg", pixels=lambda p: _frame(), text=text) == "Chidi Okafor"
    # Nobody outlined (shared screen): the text is not even read.
    assert speaker_on_screen(tmp_path / "x.jpg", pixels=lambda p: _frame(outlined=()), text=text) is None
    assert len(calls) == 1
    assert speaker_on_screen(tmp_path / "x.jpg", pixels=lambda p: None, text=text) is None
    assert speaker_on_screen(tmp_path / "x.jpg", pixels=lambda p: _frame(), text=lambda p: []) is None


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in helper is a POSIX shell script; the real one is macOS only")
def test_the_text_helper_output_is_parsed(tmp_path):
    helper = tmp_path / "buddy-screen-text"
    helper.write_text(
        "#!/bin/sh\n"
        "echo '{\"t\": \"Reyes, Dana\", \"x\": 1, \"y\": 2, \"w\": 3, \"h\": 4}'\n"
        "echo 'not json'\n"
        "echo '{\"t\": \"missing fields\"}'\n"
    )
    helper.chmod(0o755)

    assert recognise_text(tmp_path / "x.jpg", helper=str(helper)) == [TextBox("Reyes, Dana", 1, 2, 3, 4)]


def test_without_the_helper_no_text_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(active_speaker, "helper_path", lambda: None)
    assert recognise_text(tmp_path / "x.jpg") == []
    failing = tmp_path / "fail"
    failing.write_text("#!/bin/sh\nexit 3\n")
    failing.chmod(0o755)
    assert recognise_text(tmp_path / "x.jpg", helper=str(failing)) == []


def test_without_sips_nothing_is_decoded(tmp_path, monkeypatch):
    monkeypatch.setattr(active_speaker.shutil, "which", lambda name: None)

    assert active_speaker.decode(tmp_path / "x.jpg") is None
    assert not active_speaker.available()


@pytest.mark.skipif(sys.platform != "darwin", reason="sips is macOS only")
def test_a_capture_is_decoded_through_sips(tmp_path):
    _write_bmp(tmp_path / "frame.bmp", _frame())

    pixels = active_speaker.decode(tmp_path / "frame.bmp")

    assert pixels is not None and pixels.shape == (220, 500, 3)
    assert len(outlined_tiles(border_mask(pixels))) == 1


def test_while_a_screen_is_shared_the_small_outlined_tile_is_read(tmp_path):
    strip = _frame(outlined=((40, 30, 140, 130),), plain=((150, 30, 250, 130),))

    def text(path):
        return [TextBox("Thornton, Bra...", 44, 112, 90, 12)]

    assert speaker_on_screen(tmp_path / "x.jpg", pixels=lambda p: strip, text=text) == "Thornton"
