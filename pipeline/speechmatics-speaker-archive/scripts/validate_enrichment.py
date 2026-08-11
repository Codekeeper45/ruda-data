from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.enrichment import ROLE_SEEDS  # noqa: E402
from app.models import (  # noqa: E402
    EmbeddingVector,
    EnrichmentEvidence,
    MafiaRound,
    MafiaRoundParticipant,
    MafiaRole,
    SemanticDocument,
    Video,
    VideoEnrichment,
)


def validate(*, allow_incomplete: bool) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    allowed_roles = {row[0] for row in ROLE_SEEDS}
    with SessionLocal() as session:
        integrity = str(session.execute(text("PRAGMA integrity_check")).scalar())
        videos = int(session.scalar(select(func.count()).select_from(Video)) or 0)
        completed_source = int(
            session.scalar(
                select(func.count())
                .select_from(Video)
                .where(Video.status == "completed")
            )
            or 0
        )
        enriched = int(
            session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(VideoEnrichment.status == "completed")
            )
            or 0
        )
        mafia_videos = int(
            session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(
                    VideoEnrichment.status == "completed",
                    VideoEnrichment.has_mafia.is_(True),
                )
            )
            or 0
        )
        mafia_without_rounds = list(
            session.scalars(
                select(VideoEnrichment.video_id)
                .outerjoin(
                    MafiaRound, MafiaRound.video_id == VideoEnrichment.video_id
                )
                .where(
                    VideoEnrichment.status == "completed",
                    VideoEnrichment.has_mafia.is_(True),
                    MafiaRound.id.is_(None),
                )
            ).all()
        )
        invalid_boundaries = int(
            session.scalar(
                select(func.count())
                .select_from(MafiaRound)
                .join(Video, Video.id == MafiaRound.video_id)
                .where(
                    (MafiaRound.start_time < 0)
                    | (MafiaRound.end_time <= MafiaRound.start_time)
                    | (
                        (Video.duration_seconds.is_not(None))
                        & (MafiaRound.end_time > Video.duration_seconds + 2)
                    )
                )
            )
            or 0
        )
        empty_rounds = int(
            session.scalar(
                select(func.count())
                .select_from(MafiaRound)
                .outerjoin(
                    MafiaRoundParticipant,
                    MafiaRoundParticipant.round_id == MafiaRound.id,
                )
                .where(MafiaRoundParticipant.id.is_(None))
            )
            or 0
        )
        invalid_roles = int(
            session.scalar(
                select(func.count())
                .select_from(MafiaRole)
                .where(MafiaRole.code.not_in(allowed_roles))
            )
            or 0
        )
        roles_without_evidence = int(
            session.scalar(
                select(func.count())
                .select_from(MafiaRoundParticipant)
                .outerjoin(
                    EnrichmentEvidence,
                    (EnrichmentEvidence.entity_type == "participant")
                    & (
                        EnrichmentEvidence.entity_id
                        == MafiaRoundParticipant.id
                    )
                    & (EnrichmentEvidence.field_name == "actual_role"),
                )
                .where(
                    MafiaRoundParticipant.role_id.is_not(None),
                    MafiaRoundParticipant.review_status != "confirmed",
                    EnrichmentEvidence.id.is_(None),
                )
            )
            or 0
        )
        winners_without_evidence = int(
            session.scalar(
                select(func.count())
                .select_from(MafiaRound)
                .outerjoin(
                    EnrichmentEvidence,
                    (EnrichmentEvidence.entity_type == "round")
                    & (EnrichmentEvidence.entity_id == MafiaRound.id)
                    & (EnrichmentEvidence.field_name == "winner"),
                )
                .where(
                    MafiaRound.winning_faction != "unknown",
                    MafiaRound.review_status != "confirmed",
                    EnrichmentEvidence.id.is_(None),
                )
            )
            or 0
        )
        unknown_roles = int(
            session.scalar(
                select(func.count())
                .select_from(MafiaRoundParticipant)
                .where(MafiaRoundParticipant.role_id.is_(None))
            )
            or 0
        )
        documents = int(
            session.scalar(select(func.count()).select_from(SemanticDocument)) or 0
        )
        embeddings = int(
            session.scalar(select(func.count()).select_from(EmbeddingVector)) or 0
        )
        fts_documents = int(
            session.execute(text("SELECT count(*) FROM semantic_documents_fts")).scalar()
            or 0
        )

    if integrity != "ok":
        errors.append(f"SQLite integrity_check: {integrity}")
    if completed_source != enriched:
        message = (
            f"обогащено {enriched} из {completed_source} завершённых исходных видео"
        )
        (warnings if allow_incomplete else errors).append(message)
    if mafia_without_rounds:
        errors.append(
            "видео с has_mafia без раундов: "
            + ", ".join(map(str, mafia_without_rounds[:30]))
        )
    if invalid_boundaries:
        errors.append(f"раундов с некорректными границами: {invalid_boundaries}")
    if empty_rounds:
        errors.append(f"раундов без участников: {empty_rounds}")
    if invalid_roles:
        errors.append(f"ролей вне контролируемого словаря: {invalid_roles}")
    if roles_without_evidence:
        errors.append(
            f"автоматических назначений роли без доказательства: {roles_without_evidence}"
        )
    if winners_without_evidence:
        errors.append(
            f"автоматических победителей без доказательства: {winners_without_evidence}"
        )
    if documents != fts_documents:
        errors.append(
            f"FTS5 рассинхронизирована: documents={documents}, fts={fts_documents}"
        )
    if embeddings != documents:
        message = f"эмбеддингов {embeddings} из {documents} смысловых документов"
        (warnings if allow_incomplete else errors).append(message)
    if unknown_roles:
        warnings.append(f"ролей оставлено на ручную проверку: {unknown_roles}")

    return {
        "ok": not errors,
        "allow_incomplete": allow_incomplete,
        "counts": {
            "videos": videos,
            "completed_source_videos": completed_source,
            "enriched_videos": enriched,
            "mafia_videos": mafia_videos,
            "semantic_documents": documents,
            "embedding_vectors": embeddings,
            "unknown_roles": unknown_roles,
        },
        "errors": errors,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка обогащённого архива")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.storage_dir / "enrichment" / "validation_report.json",
    )
    args = parser.parse_args()
    report = validate(allow_incomplete=args.allow_incomplete)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
