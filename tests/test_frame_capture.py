"""Out-of-band evidence frames — downscaling, dedupe, and pairing to steps."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

from PIL import Image

from qualgentbench.episode_evidence import write_episode_evidence
from qualgentbench.frame_capture import FrameCapture, _downscale, load_frames


def _png(color: tuple[int, int, int], size: tuple[int, int] = (1080, 2400)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _capturer(tmp_path: Path, frames: list[bytes]) -> FrameCapture:
    """A capturer whose adb is replaced by a fixed list of screencaps."""
    capture = FrameCapture(tmp_path, "emulator-5554")
    capture.dir.mkdir(parents=True, exist_ok=True)
    queue = list(frames)

    async def _fake_screencap() -> bytes:
        return queue.pop(0) if queue else b""

    capture._screencap = _fake_screencap  # type: ignore[method-assign]
    return capture


def _index(capture: FrameCapture) -> list[dict]:
    return [json.loads(line) for line in capture.index.read_text().splitlines()]


def test_downscale_shrinks_the_screencap_and_converts_to_jpeg() -> None:
    jpeg = _downscale(_png((10, 120, 200)))

    assert jpeg[:2] == b"\xff\xd8"                    # JPEG magic
    image = Image.open(io.BytesIO(jpeg))
    assert (image.width, image.height) == (540, 1200)  # halved, aspect preserved
    # Byte size not asserted: a flat-colour fixture would measure itself, not the resize.


def test_undecodable_capture_is_not_written(tmp_path: Path) -> None:
    capture = _capturer(tmp_path, [b"not a png"])
    asyncio.run(capture._capture(1))

    assert capture.captured == 0
    assert capture.failures == 1
    assert not capture.index.exists()


def test_each_counter_value_gets_a_frame(tmp_path: Path) -> None:
    capture = _capturer(tmp_path, [_png((0, 0, 0)), _png((255, 0, 0))])
    asyncio.run(capture._capture(0))
    asyncio.run(capture._capture(1))

    assert capture.captured == 2
    assert sorted(p.name for p in capture.dir.glob("*.jpg")) == ["00000.jpg", "00001.jpg"]
    assert [r["hook_count"] for r in _index(capture)] == [0, 1]


def test_an_unchanged_screen_is_recorded_but_not_rewritten(tmp_path: Path) -> None:
    # Shell and file calls don't move the screen; the mapping still needs an entry.
    same = _png((7, 7, 7))
    capture = _capturer(tmp_path, [same, same])
    asyncio.run(capture._capture(1))
    asyncio.run(capture._capture(2))

    assert capture.captured == 1
    assert len(list(capture.dir.glob("*.jpg"))) == 1
    records = _index(capture)
    assert records[1]["duplicate"] is True
    assert records[1]["file"] == records[0]["file"]   # points at the frame it repeats


def test_frames_are_paired_to_steps_by_hook_count(tmp_path: Path) -> None:
    capture = _capturer(tmp_path, [_png((1, 1, 1)), _png((2, 2, 2))])
    asyncio.run(capture._capture(1))
    asyncio.run(capture._capture(2))
    assert set(load_frames(tmp_path)) == {1, 2}

    tap = {"type": "mcp_tool_call", "tool": "mobile_tap",
           "arguments": {"device": "emulator-5554", "x": 1, "y": 2},
           "result": {"content": [{"type": "text", "text": "Tapped"}]},
           "status": "completed", "error": None}
    transcript = "\n".join(
        json.dumps({"type": "item.completed", "item": tap}) for _ in range(3))
    write_episode_evidence(tmp_path, transcript)

    steps = [json.loads(line)
             for line in (tmp_path / "evidence" / "steps.jsonl").read_text().splitlines()]
    assert steps[0]["frame"]["path"] == "frames/00001.jpg"
    assert steps[0]["frame"]["hook_count"] == 1
    assert steps[0]["frame"]["when"] == "around this call"
    assert steps[0]["frame"]["captured_at"]   # the only per-step clock in the bundle
    assert steps[1]["frame"]["path"] == "frames/00002.jpg"
    # counter drift leaves a step without a frame rather than borrowing a stale one
    assert "frame" not in steps[2]

    meta = json.loads((tmp_path / "evidence" / "meta.json").read_text())
    assert meta["counts"]["frames"] == 2
