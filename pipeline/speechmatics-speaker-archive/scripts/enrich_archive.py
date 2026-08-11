from __future__ import annotations

import argparse
import fcntl
import json
import sys
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from pathlib import Path

from sqlalchemy import func, select, update

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.enrichment import extract_video_enrichment, seed_mafia_roles  # noqa: E402
from app.migrations import ensure_database_features  # noqa: E402
from app.models import (  # noqa: E402
    EmbeddingJob,
    EnrichmentRun,
    MafiaRound,
    MafiaRoundParticipant,
    SemanticDocument,
    Video,
    VideoEnrichment,
    utcnow,
)
from app.openrouter import OpenRouterClient, OpenRouterError  # noqa: E402
from app.secrets import get_openrouter_api_key  # noqa: E402
from app.semantic_search import (  # noqa: E402
    build_all_semantic_documents,
    embed_pending_documents,
)
from app.visual_evidence import fill_missing_roles_from_frames  # noqa: E402


def initialize(*, clear_stale_runs: bool = False) -> None:
    Base.metadata.create_all(bind=engine)
    ensure_database_features(engine)
    with SessionLocal() as session:
        if clear_stale_runs:
            session.execute(
                update(EnrichmentRun)
                .where(EnrichmentRun.status == "running")
                .values(
                    status="failed",
                    error_message="Предыдущий процесс обогащения был прерван",
                    completed_at=utcnow(),
                )
            )
        seed_mafia_roles(session)
        session.commit()


@contextmanager
def pipeline_lock():
    path = settings.storage_dir / "enrichment" / "pipeline.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("Другой процесс обогащения уже запущен") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def client_from_db() -> OpenRouterClient:
    with SessionLocal() as session:
        key = get_openrouter_api_key(session)
    if not key:
        raise SystemExit(
            "OpenRouter API key не настроен. Добавьте OPENROUTER_API_KEY в .env "
            "или сохраните ключ на странице настроек."
        )
    return OpenRouterClient(key)


def selected_videos(args: argparse.Namespace) -> list[Video]:
    with SessionLocal() as session:
        statement = select(Video).where(Video.status == "completed").order_by(Video.id)
        if args.video_id:
            statement = statement.where(Video.id.in_(args.video_id))
        if args.limit:
            statement = statement.limit(args.limit)
        return list(session.scalars(statement).all())


def probe() -> None:
    with client_from_db() as client:
        embedding = client.embeddings(
            ["Проверка семантического поиска по архиву мафии."],
            model=settings.embedding_model,
            input_type="document",
            dimensions=settings.embedding_dimensions,
        )
        if len(embedding.data[0]) != settings.embedding_dimensions:
            raise SystemExit("Voyage вернул неожиданную размерность")
        print(
            f"Voyage OK: model={embedding.model}, dimensions={len(embedding.data[0])}"
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        }
        result = client.structured_chat(
            model=settings.enrichment_model,
            system="Верни запрошенный JSON.",
            user="Проверка. Установи ok=true.",
            schema_name="probe",
            schema=schema,
            reasoning_effort="low",
        )
        print(f"Extractor OK: model={result.model}, parsed={result.data == {'ok': True}}")


def _extract_one(
    video_id: int,
    *,
    api_key: str,
    force: bool,
    verify: bool,
) -> tuple[str, bool, float]:
    with OpenRouterClient(api_key) as client, SessionLocal() as session:
        video = session.get(Video, video_id)
        if video is None:
            raise ValueError(f"Видео {video_id} не найдено")
        enrichment = extract_video_enrichment(
            session,
            client,
            video,
            force=force,
            verify=verify,
        )
        return (
            enrichment.content_type,
            enrichment.has_mafia,
            float(enrichment.confidence or 0.0),
        )


def extract(args: argparse.Namespace) -> tuple[int, int]:
    videos = selected_videos(args)
    completed = 0
    failed = 0
    with SessionLocal() as session:
        api_key = get_openrouter_api_key(session)
    if not api_key:
        raise SystemExit("OpenRouter API key не настроен")
    workers = max(1, min(int(args.workers), 4))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="enrichment")
    futures: dict[Future[tuple[str, bool, float]], Video] = {
        executor.submit(
            _extract_one,
            video.id,
            api_key=api_key,
            force=args.force,
            verify=not args.no_verify,
        ): video
        for video in videos
    }
    stop_queue = False
    try:
        for position, future in enumerate(as_completed(futures), start=1):
            video = futures[future]
            try:
                content_type, has_mafia, confidence = future.result()
                completed += 1
                print(
                    f"[{position}/{len(videos)}] video {video.id}: {video.title}\n"
                    f"  {content_type}; mafia={has_mafia}; confidence={confidence:.2f}",
                    flush=True,
                )
            except OpenRouterError as exc:
                failed += 1
                print(f"video {video.id} ERROR: {exc}", flush=True)
                if exc.is_account_rejection:
                    stop_queue = True
                    for pending in futures:
                        pending.cancel()
                    break
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"video {video.id} ERROR: {exc}", flush=True)
    finally:
        executor.shutdown(wait=True, cancel_futures=stop_queue)
    return completed, failed


def extract_with_retries(
    args: argparse.Namespace, *, retry_passes: int = 2
) -> tuple[int, int]:
    completed_total = 0
    failed = 0
    for attempt in range(retry_passes + 1):
        completed, failed = extract(args)
        completed_total += completed
        if failed == 0:
            break
        print(
            f"Повторный проход извлечения {attempt + 1}/{retry_passes}: "
            f"ошибок осталось {failed}",
            flush=True,
        )
    return completed_total, failed


def frames(args: argparse.Namespace) -> tuple[int, int]:
    videos = selected_videos(args)
    updated = 0
    failed = 0
    with client_from_db() as client:
        for position, video in enumerate(videos, start=1):
            with SessionLocal() as session:
                current = session.get(Video, video.id)
                enrichment = session.get(VideoEnrichment, video.id)
                if current is None or enrichment is None or not enrichment.has_mafia:
                    continue
                try:
                    count = fill_missing_roles_from_frames(
                        session, client, current, force_frames=args.force
                    )
                    updated += count
                    print(
                        f"[{position}/{len(videos)}] video {video.id}: +{count} ролей",
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print(f"video {video.id} FRAME ERROR: {exc}", flush=True)
    return updated, failed


def documents(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        count = build_all_semantic_documents(session, force=args.force)
        print(f"Смысловых документов: {count}")
        return count


def embeddings(args: argparse.Namespace) -> tuple[int, int]:
    with client_from_db() as client, SessionLocal() as session:
        result = embed_pending_documents(
            session, client, batch_size=args.batch_size, limit=args.embedding_limit
        )
    print(f"Эмбеддинги: completed={result[0]}, failed={result[1]}")
    return result


def status() -> None:
    with SessionLocal() as session:
        data = {
            "videos_total": session.scalar(select(func.count()).select_from(Video)) or 0,
            "videos_enriched": session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(VideoEnrichment.status == "completed")
            )
            or 0,
            "mafia_videos": session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(VideoEnrichment.has_mafia.is_(True))
            )
            or 0,
            "rounds": session.scalar(select(func.count()).select_from(MafiaRound)) or 0,
            "participants": session.scalar(
                select(func.count()).select_from(MafiaRoundParticipant)
            )
            or 0,
            "unknown_roles": session.scalar(
                select(func.count())
                .select_from(MafiaRoundParticipant)
                .where(MafiaRoundParticipant.role_id.is_(None))
            )
            or 0,
            "semantic_documents": session.scalar(
                select(func.count()).select_from(SemanticDocument)
            )
            or 0,
            "embeddings_completed": session.scalar(
                select(func.count())
                .select_from(EmbeddingJob)
                .where(EmbeddingJob.status == "completed")
            )
            or 0,
            "embeddings_failed": session.scalar(
                select(func.count())
                .select_from(EmbeddingJob)
                .where(EmbeddingJob.status == "failed")
            )
            or 0,
        }
    print(json.dumps(data, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Обогащение архива играми и RAG")
    result.add_argument(
        "command",
        choices=["probe", "extract", "frames", "documents", "embeddings", "all", "status"],
    )
    result.add_argument("--video-id", type=int, action="append")
    result.add_argument("--limit", type=int)
    result.add_argument("--force", action="store_true")
    result.add_argument("--no-verify", action="store_true")
    result.add_argument("--no-frames", action="store_true")
    result.add_argument("--batch-size", type=int, default=48)
    result.add_argument("--embedding-limit", type=int)
    result.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Параллельные сетевые извлечения (1-4; кадры и БД остаются последовательными)",
    )
    return result


def main() -> None:
    args = parser().parse_args()
    mutating = args.command not in {"probe", "status"}
    with pipeline_lock() if mutating else nullcontext():
        initialize(clear_stale_runs=mutating)
        if args.command == "probe":
            probe()
        elif args.command == "extract":
            completed, failed = extract_with_retries(args)
            print(f"Итог extract: completed={completed}, failed={failed}")
        elif args.command == "frames":
            print(f"Итог frames: updated={frames(args)[0]}")
        elif args.command == "documents":
            documents(args)
        elif args.command == "embeddings":
            embeddings(args)
        elif args.command == "status":
            status()
        else:
            _completed, failed = extract_with_retries(args)
            if failed:
                print(
                    f"ВНИМАНИЕ: после повторов осталось ошибок извлечения: {failed}",
                    flush=True,
                )
            if not args.no_frames:
                frames(args)
            documents(args)
            embeddings(args)
            status()


if __name__ == "__main__":
    main()
