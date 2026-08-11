#!/usr/bin/env python3
"""Пересоздать семантические документы для указанных видео и пересоздать эмбеддинги.

Usage:
  python3 scripts/rebuild_semantic.py --video-id 16 17 19 ...
  python3 scripts/rebuild_semantic.py --all
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import EmbeddingJob, Video  # noqa: E402
from app.semantic_search import (  # noqa: E402
    build_semantic_documents_for_video,
    embed_pending_documents,
)
from scripts.enrich_archive import client_from_db  # noqa: E402

# Видео, чьи раунды/роли менялись в ходе ручного аудита (T6 PHASE 3 + этот сегмент)
AUDITED_VIDEO_IDS = [
    16, 17, 19, 20, 21, 23, 24, 27, 32, 39, 41, 43, 44, 58,
    64, 70, 73, 74, 76, 79, 85, 103, 106, 110, 114, 116, 119,
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-id", type=int, action="append")
    parser.add_argument("--all", action="store_true", help="пересоздать для всех видео")
    parser.add_argument("--embeddings-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=48)
    args = parser.parse_args()

    video_ids = list(args.video_id) if args.video_id else (
        None if args.all else AUDITED_VIDEO_IDS
    )

    with SessionLocal() as session:
        statement = select(Video).where(Video.status == "completed").order_by(Video.id)
        if video_ids:
            statement = statement.where(Video.id.in_(video_ids))
        videos = list(session.scalars(statement).all())

    if not args.embeddings_only:
        print(f"Пересоздание документов для {len(videos)} видео...", flush=True)
        total = 0
        for position, video in enumerate(videos, start=1):
            with SessionLocal() as session:
                count = build_semantic_documents_for_video(
                    session, video, force=True
                )
            total += count
            print(
                f"[{position}/{len(videos)}] video {video.id}: {count} документов",
                flush=True,
            )
        print(f"Итого документов: {total}", flush=True)

    with client_from_db() as client, SessionLocal() as session:
        completed, failed = embed_pending_documents(
            session, client, batch_size=args.batch_size
        )
    print(f"Эмбеддинги: completed={completed}, failed={failed}", flush=True)

    with SessionLocal() as session:
        stats = {
            "semantic_documents": session.scalar(
                select(func.count()).select_from(__import__("app.models", fromlist=["SemanticDocument"]).SemanticDocument)
            ) or 0,
            "embeddings_completed": session.scalar(
                select(func.count()).select_from(EmbeddingJob).where(EmbeddingJob.status == "completed")
            ) or 0,
            "embeddings_failed": session.scalar(
                select(func.count()).select_from(EmbeddingJob).where(EmbeddingJob.status == "failed")
            ) or 0,
            "embeddings_queued": session.scalar(
                select(func.count()).select_from(EmbeddingJob).where(EmbeddingJob.status == "queued")
            ) or 0,
        }
    print(json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
