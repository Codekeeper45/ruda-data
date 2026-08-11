from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .config import settings
from .models import Utterance, Video
from .transcript_parser import format_timestamp


def export_video_json(session: Session, video: Video, destination: Path) -> Path:
    utterances = session.scalars(
        select(Utterance)
        .where(Utterance.video_id == video.id)
        .options(joinedload(Utterance.speaker))
        .order_by(Utterance.sequence)
    ).all()
    payload = {
        "video": {
            "id": video.id,
            "title": video.title,
            "source_path": video.source_path,
            "duration_seconds": video.duration_seconds,
            "language": video.language,
            "model": video.model,
            "remote_job_id": video.remote_job_id,
        },
        "utterances": [
            {
                "sequence": row.sequence,
                "speaker_label": row.speaker.label,
                "speaker_name": row.speaker.display_name,
                "speaker_profile_id": row.speaker.profile_id,
                "start_time": row.start_time,
                "end_time": row.end_time,
                "text": row.text,
                "average_confidence": row.average_confidence,
                "word_count": row.word_count,
            }
            for row in utterances
        ],
    }
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def export_video_csv(session: Session, video: Video, destination: Path) -> Path:
    utterances = session.scalars(
        select(Utterance)
        .where(Utterance.video_id == video.id)
        .options(joinedload(Utterance.speaker))
        .order_by(Utterance.sequence)
    ).all()
    with destination.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "video_id",
                "video_title",
                "sequence",
                "speaker_label",
                "speaker_name",
                "speaker_profile_id",
                "start_time",
                "end_time",
                "start_timestamp",
                "end_timestamp",
                "confidence",
                "word_count",
                "text",
            ]
        )
        for row in utterances:
            writer.writerow(
                [
                    video.id,
                    video.title,
                    row.sequence,
                    row.speaker.label,
                    row.speaker.display_name,
                    row.speaker.profile_id or "",
                    row.start_time,
                    row.end_time,
                    format_timestamp(row.start_time),
                    format_timestamp(row.end_time),
                    row.average_confidence if row.average_confidence is not None else "",
                    row.word_count,
                    row.text,
                ]
            )
    return destination


def export_video_srt(session: Session, video: Video, destination: Path) -> Path:
    utterances = session.scalars(
        select(Utterance)
        .where(Utterance.video_id == video.id)
        .options(joinedload(Utterance.speaker))
        .order_by(Utterance.sequence)
    ).all()
    chunks: list[str] = []
    for index, row in enumerate(utterances, start=1):
        chunks.append(str(index))
        chunks.append(
            f"{format_timestamp(row.start_time, srt=True)} --> {format_timestamp(row.end_time, srt=True)}"
        )
        chunks.append(f"{row.speaker.display_name}: {row.text}")
        chunks.append("")
    destination.write_text("\n".join(chunks), encoding="utf-8")
    return destination


def export_video_vtt(session: Session, video: Video, destination: Path) -> Path:
    utterances = session.scalars(
        select(Utterance)
        .where(Utterance.video_id == video.id)
        .options(joinedload(Utterance.speaker))
        .order_by(Utterance.sequence)
    ).all()
    chunks = ["WEBVTT", ""]
    for row in utterances:
        chunks.append(f"{format_timestamp(row.start_time)} --> {format_timestamp(row.end_time)}")
        chunks.append(f"{row.speaker.display_name}: {row.text}")
        chunks.append("")
    destination.write_text("\n".join(chunks), encoding="utf-8")
    return destination


def export_video_txt(session: Session, video: Video, destination: Path) -> Path:
    utterances = session.scalars(
        select(Utterance)
        .where(Utterance.video_id == video.id)
        .options(joinedload(Utterance.speaker))
        .order_by(Utterance.sequence)
    ).all()
    lines = [
        (
            f"[{format_timestamp(row.start_time)} --> {format_timestamp(row.end_time)}] "
            f"{row.speaker.display_name}: {row.text}"
        )
        for row in utterances
    ]
    destination.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return destination


def backup_sqlite_database(destination: Path) -> Path:
    prefix = "sqlite:///"
    if not settings.database_url.startswith(prefix):
        raise RuntimeError("Database download is currently implemented for SQLite only")
    source_path = Path(settings.database_url[len(prefix):]).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    return destination
