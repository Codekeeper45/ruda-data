from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.db import SessionLocal
from app.exports import (
    backup_sqlite_database,
    export_video_csv,
    export_video_json,
    export_video_srt,
    export_video_txt,
    export_video_vtt,
)
from app.models import Utterance, Video, VideoSpeaker, Word
from app.transcript_parser import format_timestamp


def main() -> None:
    root = settings.storage_dir / "exports" / "all_videos"
    exporters = {
        "json": export_video_json,
        "csv": export_video_csv,
        "srt": export_video_srt,
        "vtt": export_video_vtt,
        "txt": export_video_txt,
    }
    for name in exporters:
        (root / name).mkdir(parents=True, exist_ok=True)

    manifest_rows: list[list[object]] = []
    with SessionLocal() as session:
        videos = session.scalars(
            select(Video).where(Video.status == "completed").order_by(Video.id)
        ).all()
        for video in videos:
            for extension, exporter in exporters.items():
                exporter(session, video, root / extension / f"video_{video.id}.{extension}")

            utterance_count = session.scalar(
                select(func.count()).select_from(Utterance).where(Utterance.video_id == video.id)
            ) or 0
            word_count = session.scalar(
                select(func.count()).select_from(Word).where(Word.video_id == video.id)
            ) or 0
            speaker_count = session.scalar(
                select(func.count()).select_from(VideoSpeaker).where(VideoSpeaker.video_id == video.id)
            ) or 0
            unknown_count = session.scalar(
                select(func.count())
                .select_from(VideoSpeaker)
                .where(VideoSpeaker.video_id == video.id, VideoSpeaker.is_known.is_(False))
            ) or 0
            manifest_rows.append(
                [
                    video.id,
                    video.title,
                    video.source_path,
                    video.duration_seconds,
                    utterance_count,
                    word_count,
                    speaker_count,
                    unknown_count,
                    video.raw_transcript_path or "",
                ]
            )

    manifest_path = root / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "video_id",
                "title",
                "source_path",
                "duration_seconds",
                "utterance_count",
                "word_count",
                "speaker_track_count",
                "unknown_track_count",
                "raw_transcript_path",
            ]
        )
        writer.writerows(manifest_rows)

    unknown_path = root / "unknown_review.csv"
    with SessionLocal() as session, unknown_path.open(
        "w", encoding="utf-8-sig", newline=""
    ) as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "video_id",
                "video_title",
                "local_speaker_label",
                "start_time",
                "end_time",
                "start_timestamp",
                "end_timestamp",
                "duration_seconds",
                "average_confidence",
                "text",
            ]
        )
        rows = session.execute(
            select(Utterance, Video, VideoSpeaker)
            .join(Video, Video.id == Utterance.video_id)
            .join(VideoSpeaker, VideoSpeaker.id == Utterance.speaker_id)
            .where(VideoSpeaker.is_known.is_(False))
            .order_by(Video.id, Utterance.start_time)
        ).all()
        for utterance, video, speaker in rows:
            writer.writerow(
                [
                    video.id,
                    video.title,
                    speaker.label,
                    utterance.start_time,
                    utterance.end_time,
                    format_timestamp(utterance.start_time),
                    format_timestamp(utterance.end_time),
                    utterance.end_time - utterance.start_time,
                    utterance.average_confidence or "",
                    utterance.text,
                ]
            )

        total_speech = float(
            session.scalar(select(func.coalesce(func.sum(VideoSpeaker.total_speech_seconds), 0)))
            or 0
        )
        unknown_speech = float(
            session.scalar(
                select(func.coalesce(func.sum(VideoSpeaker.total_speech_seconds), 0)).where(
                    VideoSpeaker.is_known.is_(False)
                )
            )
            or 0
        )
        summary = {
            "completed_videos": len(manifest_rows),
            "utterances": int(session.scalar(select(func.count()).select_from(Utterance)) or 0),
            "words": int(session.scalar(select(func.count()).select_from(Word)) or 0),
            "speaker_tracks": int(
                session.scalar(select(func.count()).select_from(VideoSpeaker)) or 0
            ),
            "unknown_tracks": int(
                session.scalar(
                    select(func.count())
                    .select_from(VideoSpeaker)
                    .where(VideoSpeaker.is_known.is_(False))
                )
                or 0
            ),
            "unknown_utterances": len(rows),
            "unknown_speech_seconds": unknown_speech,
            "known_speech_percent": (
                (total_speech - unknown_speech) / total_speech * 100 if total_speech else 0
            ),
        }
        (root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    backup_sqlite_database(root / "speaker_archive.sqlite3")
    archive_base = settings.storage_dir / "exports" / "speaker_archive_all_videos"
    archive_path = Path(
        shutil.make_archive(str(archive_base), "zip", root_dir=root)
    )
    print(f"videos={len(manifest_rows)}")
    print(f"manifest={manifest_path}")
    print(f"unknown_review={unknown_path}")
    print(f"database={root / 'speaker_archive.sqlite3'}")
    print(f"archive={archive_path}")


if __name__ == "__main__":
    main()
