"""Read off the screen who is talking: the outlined tile and the name on it.

Teams outlines the speaking participant's tile with a two-pixel violet border and
writes the name in its bottom-left corner. A capture is decoded to pixels with `sips`
(macOS ships it), the outline is found as a rectangle of border-coloured edges, and
the text inside it is read by the Vision framework through a small Swift helper,
`buddy-screen-text`. Nothing here leaves the machine, and the capture is deleted as
soon as it has been read.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .speakers import display_name

Rect = tuple[int, int, int, int]  # x0, y0, x1, y1, in pixels


@dataclass(frozen=True)
class TextBox:
    text: str
    x: float
    y: float
    w: float
    h: float


def helper_path() -> str | None:
    bundled = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "buddy-screen-text"
    for candidate in (os.getenv("BUDDY_SCREEN_TEXT_HELPER"), shutil.which("buddy-screen-text"), str(bundled)):
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    return None


def read_bmp(path: Path) -> np.ndarray:
    """An uncompressed 24/32-bit BMP as an RGB array (what `sips -s format bmp` writes)."""
    data = path.read_bytes()
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    depth = struct.unpack_from("<H", data, 28)[0] // 8
    if depth not in (3, 4):
        raise ValueError(f"unsupported BMP depth {depth * 8}")
    stride = (width * depth + 3) // 4 * 4
    rows = np.frombuffer(data, np.uint8, offset=offset, count=stride * abs(height)).reshape(abs(height), stride)
    pixels = rows[:, : width * depth].reshape(abs(height), width, depth)
    if height > 0:
        pixels = pixels[::-1]  # bottom-up rows
    return pixels[..., 2::-1].astype(np.int16)  # BGR(A) -> RGB


def border_mask(pixels: np.ndarray) -> np.ndarray:
    """Pixels of the speaking outline: Teams' violet, blue well above red and green."""
    red, green, blue = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    return (blue > 140) & (blue - red > 45) & (blue - green > 45)


def _runs(row: np.ndarray, minimum: int) -> list[tuple[int, int]]:
    padded = np.concatenate([[False], row, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    starts, ends = edges[0::2], edges[1::2]
    return [(int(a), int(b)) for a, b in zip(starts, ends) if b - a >= minimum]


def outlined_tiles(mask: np.ndarray, min_width: int = 160, min_height: int = 100) -> list[Rect]:
    """Rectangles whose four edges are drawn in the border colour."""
    height = mask.shape[0]
    candidates = [y for y in range(height) if mask[y].sum() >= min_width]
    lines = [(y, a, b) for y in candidates for a, b in _runs(mask[y], min_width)]
    found: list[Rect] = []
    for index, (top, a0, a1) in enumerate(lines):
        for bottom, b0, b1 in lines[index + 1 :]:
            if bottom - top < min_height or abs(b0 - a0) > 6 or abs(b1 - a1) > 6:
                continue
            left = mask[top:bottom, max(0, a0 - 6) : a0 + 4].any(axis=1).mean()
            right = mask[top:bottom, max(0, a1 - 4) : a1 + 6].any(axis=1).mean()
            if left > 0.8 and right > 0.8:
                rect = (a0, top, a1, bottom)
                if not any(abs(rect[0] - r[0]) < 8 and abs(rect[1] - r[1]) < 8 for r in found):
                    found.append(rect)
    # An outline drawn two pixels thick is found twice; nested ones are the same tile.
    return [r for r in found if not any(o != r and o[0] <= r[0] and o[1] <= r[1] and o[2] >= r[2] and o[3] >= r[3] for o in found)]


def name_in_tile(tile: Rect, boxes: list[TextBox]) -> str | None:
    """The label in the tile's bottom-left corner, as a name."""
    x0, y0, x1, y1 = tile
    best: tuple[float, str] | None = None
    for box in boxes:
        middle = box.y + box.h / 2
        if x0 - 5 <= box.x <= x0 + 0.6 * (x1 - x0) and y1 - 60 <= middle <= y1 + 5:
            name = display_name(box.text)
            if name and (best is None or box.y > best[0]):
                best = (box.y, name)
    return best[1] if best else None


def recognise_text(image: Path, helper: str | None = None) -> list[TextBox]:
    helper = helper or helper_path()
    if helper is None:
        return []
    try:
        result = subprocess.run([helper, str(image)], capture_output=True, text=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    boxes = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
            boxes.append(TextBox(str(item["t"]), float(item["x"]), float(item["y"]), float(item["w"]), float(item["h"])))
        except (ValueError, KeyError, TypeError):
            continue
    return boxes


def decode(image: Path) -> np.ndarray | None:
    if shutil.which("sips") is None:
        return None
    with tempfile.TemporaryDirectory() as folder:
        bmp = Path(folder) / "frame.bmp"
        try:
            subprocess.run(["sips", "-s", "format", "bmp", str(image), "--out", str(bmp)],
                           capture_output=True, timeout=20, check=True)
            return read_bmp(bmp)
        except (OSError, subprocess.SubprocessError, ValueError, struct.error):
            return None


def speaker_on_screen(
    image: Path,
    pixels: Callable[[Path], np.ndarray | None] = decode,
    text: Callable[[Path], list[TextBox]] = recognise_text,
) -> str | None:
    """The name on the outlined tile of a capture, or None (no outline, no readable name)."""
    frame = pixels(image)
    if frame is None:
        return None
    tiles = outlined_tiles(border_mask(frame))
    if not tiles:
        return None  # nobody outlined: shared screen, or a pause; skip the OCR
    boxes = text(image)
    for tile in tiles:
        name = name_in_tile(tile, boxes)
        if name:
            return name
    return None


def available() -> bool:
    return sys.platform == "darwin" and helper_path() is not None and shutil.which("sips") is not None
