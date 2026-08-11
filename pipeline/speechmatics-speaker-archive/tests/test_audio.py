from __future__ import annotations

import asyncio
import math
import struct
import wave
from pathlib import Path

from app.audio import prepare_for_upload, probe_duration


def create_wav(path: Path, seconds: float = 1.0, sample_rate: int = 48000) -> None:
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        for index in range(frames):
            value = int(2000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            output.writeframesraw(struct.pack("<hh", value, value))


def test_probe_and_prepare_audio(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "prepared.flac"
    create_wav(source)
    duration = probe_duration(source)
    assert duration is not None and 0.9 <= duration <= 1.1
    result = asyncio.run(prepare_for_upload(source, destination))
    assert result == destination
    assert destination.exists()
    assert source.exists()
    assert destination.stat().st_size < source.stat().st_size
