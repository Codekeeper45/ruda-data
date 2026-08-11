from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audio import SUPPORTED_INPUT_SUFFIXES, file_signature, probe_duration
from .models import JobKind, JobStatus, SourceFolder, TranscriptionJob, Video, utcnow
from .secrets import get_setting


def scan_source_folder(session: Session, source: SourceFolder) -> tuple[int, int]:
    root = Path(source.path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Folder does not exist or is not a directory: {root}")

    pattern = "**/*" if source.recursive else "*"
    discovered = sorted(
        path for path in root.glob(pattern)
        if path.is_file() and path.suffix.lower() in SUPPORTED_INPUT_SUFFIXES
    )

    language = get_setting(session, "language", "ru") or "ru"
    model = get_setting(session, "model", "enhanced") or "enhanced"
    added = 0
    skipped = 0

    for path in discovered:
        signature = file_signature(path)
        exists = session.scalar(select(Video.id).where(Video.source_signature == signature))
        if exists is not None:
            skipped += 1
            continue

        stat = path.stat()
        video = Video(
            source_folder_id=source.id,
            title=path.stem,
            original_filename=path.name,
            source_path=str(path.resolve()),
            source_signature=signature,
            source_size_bytes=stat.st_size,
            source_modified_ns=stat.st_mtime_ns,
            duration_seconds=probe_duration(path),
            status=JobStatus.QUEUED,
            language=language,
            model=model,
        )
        session.add(video)
        session.flush()
        session.add(
            TranscriptionJob(
                kind=JobKind.VIDEO,
                status=JobStatus.QUEUED,
                video_id=video.id,
            )
        )
        added += 1

    source.last_scanned_at = utcnow()
    source.last_error = None
    return added, skipped
