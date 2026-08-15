from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reels_scheduler.config import Config  # noqa: E402
from reels_scheduler.db import Store  # noqa: E402
from reels_scheduler.scheduler import get_zone  # noqa: E402

IST = get_zone("Asia/Kolkata")


def make_video(path: str, payload: bytes = b"", seconds: float = 0.4) -> str:
    """Create a small on-disk file to stand in for a reel.

    Uses ffmpeg for a genuine (tiny) mp4 when it is installed; otherwise falls
    back to deterministic bytes. Nothing in this project decodes video -- the
    dedup layer only hashes bytes -- so both paths exercise the same code.
    """

    if shutil.which("ffmpeg"):
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=%s" % seconds,
            "-pix_fmt", "yuv420p", path,
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0 and os.path.getsize(path) > 0:
            # ffmpeg output is content-identical across calls (same color/size/
            # duration), so append the caller's payload as a trailing "atom" to
            # keep each generated clip's hash unique while the file is still a
            # genuine, playable mp4.
            with open(path, "ab") as handle:
                handle.write(b"\x00\x00\x00\x00free" + (payload or b"fake-video-bytes"))
            return path
    with open(path, "wb") as handle:
        handle.write(payload or b"fake-video-bytes")
    return path


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def video_factory(tmp_path):
    counter = {"n": 0}

    def _make(name=None, content=None):
        counter["n"] += 1
        name = name or ("clip%d.mp4" % counter["n"])
        content = content if content is not None else ("unique-%d" % counter["n"]).encode()
        return make_video(str(tmp_path / name), content)

    return _make


@pytest.fixture
def now_ist():
    """A fixed, DST-free reference instant: 2026-03-02 08:00 IST (a Monday)."""

    return dt.datetime(2026, 3, 2, 8, 0, tzinfo=IST)
