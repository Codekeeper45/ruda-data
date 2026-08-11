from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from .config import settings


SUPPORTED_INPUT_SUFFIXES = {".wav"}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def file_signature(path: Path) -> str:
    stat = path.stat()
    payload = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def probe_duration(path: Path) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=60
        )
        data = json.loads(completed.stdout)
        return float(data["format"]["duration"])
    except (subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError):
        return None


def build_listening_clip(
    source: Path,
    destination: Path,
    *,
    start_time: float,
    end_time: float,
) -> Path:
    """Create a small cached MP3 preview without touching the source WAV."""
    if end_time <= start_time:
        raise ValueError("Конец аудиофрагмента должен быть позже начала")
    if end_time - start_time > 600:
        raise ValueError("Аудиофрагмент не может быть длиннее 10 минут")
    if not source.exists():
        raise FileNotFoundError(source)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg не найден")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    temporary = destination.with_suffix(".part.mp3")
    temporary.unlink(missing_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_time:.3f}",
        "-i",
        str(source),
        "-t",
        f"{end_time - start_time:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "24000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        "-y",
        str(temporary),
    ]
    try:
        subprocess.run(command, capture_output=True, check=True, timeout=120)
    except subprocess.CalledProcessError as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            exc.stderr.decode("utf-8", errors="replace").strip() or "ffmpeg завершился ошибкой"
        ) from exc
    temporary.replace(destination)
    return destination


async def prepare_for_upload(source: Path, destination: Path) -> Path:
    """Convert large archival WAV audio to compact 16 kHz mono FLAC.

    The source is never modified. Atomic rename prevents half-written files from
    being picked up after a restart.
    """
    if not settings.prepare_audio:
        return source
    if shutil.which("ffmpeg") is None:
        if source.stat().st_size >= settings.max_upload_bytes:
            raise RuntimeError("FFmpeg is required because the source exceeds the 1 GB API upload limit")
        return source

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_destination = destination.with_name(f"{destination.stem}.part{destination.suffix}")
    temp_destination.unlink(missing_ok=True)

    codec_args = (
        ["-c:a", "libmp3lame", "-b:a", "128k"]
        if destination.suffix.casefold() == ".mp3"
        else ["-c:a", "flac", "-compression_level", "8"]
    )
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        str(settings.prepared_channels),
        "-ar",
        str(settings.prepared_sample_rate),
        *codec_args,
        str(temp_destination),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        temp_destination.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg failed: {stderr.decode('utf-8', errors='replace').strip()}")

    if temp_destination.stat().st_size >= settings.max_upload_bytes:
        temp_destination.unlink(missing_ok=True)
        raise RuntimeError(
            "Prepared audio is still larger than the direct Speechmatics upload limit. "
            "Use cloud storage with fetch_data URL or reduce the source size."
        )

    temp_destination.replace(destination)
    return destination
