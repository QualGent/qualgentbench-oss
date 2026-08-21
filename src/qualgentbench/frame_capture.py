"""Independent evidence frames, one per tool call, triggered by watching the budget
hook's counter file — no agent cooperation, and the frame never reaches the agent.
Capture can overlap the call, so a frame is "around" it, not a precise pre-state."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_POLL_SEC = 0.15
_MAX_WIDTH = 540        # half a typical screen width: still readable, far smaller
_JPEG_QUALITY = 70
_CAPTURE_TIMEOUT_SEC = 20.0

# Where each adapter's budget hook keeps its counter (claude-code writes the first,
# codex nests it under its isolated CODEX_HOME).
_COUNT_FILES = ("hooks/count", "codex_home/hooks/count")


def _adb_bin() -> str:
    return os.environ.get("QGB_ADB_PATH") or "adb"


def _downscale(png: bytes) -> bytes:
    """Full-res screencap PNG → small JPEG. Returns b"" if it cannot be decoded."""
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(png))
        image.load()
    except Exception:  # noqa: BLE001 - a truncated capture is not worth a traceback
        return b""
    if image.mode != "RGB":
        image = image.convert("RGB")
    if image.width > _MAX_WIDTH:
        height = round(image.height * _MAX_WIDTH / image.width)
        image = image.resize((_MAX_WIDTH, height), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


class FrameCapture:
    """Captures one frame per tool call into <run_dir>/evidence/frames. Async
    context manager around the agent run; every failure is swallowed — the worst
    case is fewer frames, never a disturbed episode."""

    def __init__(self, run_dir: Path, device_serial: str) -> None:
        self.run_dir = run_dir
        self.device = device_serial
        self.dir = run_dir / "evidence" / "frames"
        self.index = self.dir / "index.jsonl"
        self._task: asyncio.Task | None = None
        self._last_sha: str | None = None
        self._last_file: str | None = None
        self.captured = 0
        self.failures = 0

    async def __aenter__(self) -> "FrameCapture":
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("frame capture disabled (%s)", exc)
            return self
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await self._task
        logger.info("frame capture: %d frame(s), %d failure(s) → %s",
                    self.captured, self.failures, self.dir)

    def _count_file(self) -> Path | None:
        for relative in _COUNT_FILES:
            candidate = self.run_dir / relative
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _read_count(path: Path | None) -> int | None:
        if path is None:
            return None
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return None

    async def _loop(self) -> None:
        # The screen as the episode starts, before the agent has done anything.
        await self._capture(0)
        seen: int | None = None
        while True:
            count = self._read_count(self._count_file())
            if count is not None and count != seen:
                seen = count
                await self._capture(count)
            await asyncio.sleep(_POLL_SEC)

    async def _screencap(self) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            _adb_bin(), "-s", self.device, "exec-out", "screencap", "-p",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_CAPTURE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                proc.kill()
            return b""
        return out or b""

    async def _capture(self, count: int) -> None:
        try:
            png = await self._screencap()
            if not png:
                self.failures += 1
                return

            digest = hashlib.sha256(png).hexdigest()
            record: dict[str, object] = {
                "hook_count": count,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "sha256": digest,
            }

            # Consecutive frames are often byte-identical (shell calls don't touch
            # the screen); point at the existing file instead of rewriting it.
            if digest == self._last_sha and self._last_file:
                record["file"] = self._last_file
                record["duplicate"] = True
            else:
                jpeg = await asyncio.to_thread(_downscale, png)
                if not jpeg:
                    self.failures += 1
                    return
                name = f"frames/{count:05d}.jpg"
                (self.dir / f"{count:05d}.jpg").write_bytes(jpeg)
                self._last_sha, self._last_file = digest, name
                record["file"] = name
                self.captured += 1

            with self.index.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - evidence must not break an episode
            # Counted and skipped, never re-raised — one failed write must not end
            # capture for the rest of the episode.
            self.failures += 1
            logger.debug("frame capture at count %s failed: %s", count, exc)


def load_frames(run_dir: Path) -> dict[int, dict]:
    """``hook_count -> frame record`` for an episode, or empty if none were captured."""
    index = run_dir / "evidence" / "frames" / "index.jsonl"
    frames: dict[int, dict] = {}
    try:
        for line in index.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            count = record.get("hook_count")
            if isinstance(count, int) and record.get("file"):
                frames[count] = record
    except OSError:
        return {}
    return frames
