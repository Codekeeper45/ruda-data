from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, joinedload

from .config import settings
from .models import (
    EmbeddingJob,
    EmbeddingVector,
    MafiaPhase,
    MafiaRole,
    MafiaRound,
    MafiaRoundParticipant,
    SemanticDocument,
    SemanticDocumentUtterance,
    SpeakerProfile,
    Utterance,
    Video,
    VideoEnrichment,
    VideoSpeaker,
    utcnow,
)
from .openrouter import OpenRouterClient


PIPELINE_VERSION = settings.enrichment_pipeline_version
TARGET_WORDS = 300
MAX_WORDS = 480
MAX_DURATION_SECONDS = 120.0
OVERLAP_UTTERANCES = 2


@dataclass(slots=True)
class SearchFilters:
    video_id: int | None = None
    round_id: int | None = None
    profile_id: int | None = None
    role_code: str | None = None
    winning_faction: str | None = None
    content_type: str | None = None
    start_time: float | None = None
    end_time: float | None = None


@dataclass(slots=True)
class SearchHit:
    document_id: int
    score: float
    vector_score: float | None
    lexical_rank: int | None
    document_type: str
    video_id: int
    video_title: str
    round_id: int | None
    round_number: int | None
    start_time: float
    end_time: float
    text: str
    speakers: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _content_hash(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def _round_for_time(rounds: list[MafiaRound], start: float, end: float) -> MafiaRound | None:
    midpoint = (start + end) / 2
    return next(
        (row for row in rounds if row.start_time <= midpoint <= row.end_time),
        None,
    )


def _document_text(
    video: Video,
    round_row: MafiaRound | None,
    utterances: list[Utterance],
) -> str:
    header = f"Видео: {video.title}."
    if round_row:
        header += f" Игра мафии, раунд {round_row.round_number}."
    body = "\n".join(f"{row.speaker.display_name}: {row.text}" for row in utterances)
    return f"{header}\n{body}"


def _add_document(
    session: Session,
    *,
    document_type: str,
    video: Video,
    round_row: MafiaRound | None,
    start_time: float,
    end_time: float,
    text_value: str,
    utterances: list[Utterance],
) -> SemanticDocument:
    document = SemanticDocument(
        document_type=document_type,
        video_id=video.id,
        round_id=round_row.id if round_row else None,
        start_time=start_time,
        end_time=end_time,
        text=text_value,
        token_count=max(1, int(sum(row.word_count for row in utterances) * 1.35)),
        content_hash=_content_hash(text_value),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(document)
    session.flush()
    for sequence, utterance in enumerate(utterances):
        session.add(
            SemanticDocumentUtterance(
                document_id=document.id,
                utterance_id=utterance.id,
                sequence=sequence,
            )
        )
    session.add(
        EmbeddingJob(
            document_id=document.id,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            input_hash=document.content_hash,
            status="queued",
        )
    )
    return document


def _round_summary(
    session: Session,
    video: Video,
    round_row: MafiaRound,
) -> str:
    participants = session.execute(
        select(MafiaRoundParticipant, MafiaRole)
        .outerjoin(MafiaRole, MafiaRole.id == MafiaRoundParticipant.role_id)
        .where(MafiaRoundParticipant.round_id == round_row.id)
        .order_by(MafiaRoundParticipant.display_name)
    ).all()
    nights = session.scalar(
        select(func.count())
        .select_from(MafiaPhase)
        .where(MafiaPhase.round_id == round_row.id, MafiaPhase.phase_type == "night")
    ) or 0
    participant_text = []
    for participant, role in participants:
        role_name = role.name if role else "роль неизвестна"
        participant_text.append(
            f"{participant.display_name}: {role_name}, фракция {participant.faction}, "
            f"исход {participant.outcome}"
        )
    return (
        f"Видео: {video.title}. Раунд {round_row.round_number}. "
        f"Победившая сторона: {round_row.winning_faction}. "
        f"Итог: {round_row.winner_summary or 'не установлен'}. "
        f"Количество ночей: {nights}. Участники: {'; '.join(participant_text)}."
    )


def build_semantic_documents_for_video(
    session: Session,
    video: Video,
    *,
    force: bool = False,
) -> int:
    existing = session.scalar(
        select(func.count())
        .select_from(SemanticDocument)
        .where(
            SemanticDocument.video_id == video.id,
            SemanticDocument.pipeline_version == PIPELINE_VERSION,
        )
    ) or 0
    if existing and not force:
        return int(existing)
    session.execute(delete(SemanticDocument).where(SemanticDocument.video_id == video.id))
    session.flush()

    utterances = list(
        session.scalars(
            select(Utterance)
            .where(Utterance.video_id == video.id)
            .options(joinedload(Utterance.speaker))
            .order_by(Utterance.sequence)
        ).all()
    )
    rounds = list(
        session.scalars(
            select(MafiaRound)
            .where(MafiaRound.video_id == video.id)
            .order_by(MafiaRound.start_time)
        ).all()
    )
    if not utterances:
        return 0
    created = 0
    index = 0
    while index < len(utterances):
        first = utterances[index]
        round_row = _round_for_time(rounds, first.start_time, first.end_time)
        chunk: list[Utterance] = []
        words = 0
        cursor = index
        while cursor < len(utterances):
            row = utterances[cursor]
            row_round = _round_for_time(rounds, row.start_time, row.end_time)
            if row_round is not round_row:
                break
            projected = words + max(1, row.word_count)
            duration = row.end_time - first.start_time
            if chunk and (projected > MAX_WORDS or duration > MAX_DURATION_SECONDS):
                break
            chunk.append(row)
            words = projected
            cursor += 1
            if words >= TARGET_WORDS:
                break
        if not chunk:
            chunk = [first]
            cursor = index + 1
        _add_document(
            session,
            document_type="timeline_chunk",
            video=video,
            round_row=round_row,
            start_time=chunk[0].start_time,
            end_time=chunk[-1].end_time,
            text_value=_document_text(video, round_row, chunk),
            utterances=chunk,
        )
        created += 1
        if cursor >= len(utterances):
            index = cursor
        else:
            index = max(index + 1, cursor - min(OVERLAP_UTTERANCES, len(chunk) - 1))

    for round_row in rounds:
        linked = [
            row
            for row in utterances
            if round_row.start_time <= row.start_time <= round_row.end_time
        ]
        _add_document(
            session,
            document_type="round_summary",
            video=video,
            round_row=round_row,
            start_time=round_row.start_time,
            end_time=round_row.end_time,
            text_value=_round_summary(session, video, round_row),
            utterances=linked[:1],
        )
        created += 1

    video_text = (
        f"Видео: {video.title}. Тип: "
        f"{session.get(VideoEnrichment, video.id).content_type if session.get(VideoEnrichment, video.id) else 'unknown'}. "
        f"Раундов мафии: {len(rounds)}. "
        + " ".join(
            f"Раунд {row.round_number}: {row.winner_summary or row.winning_faction}."
            for row in rounds
        )
    )
    _add_document(
        session,
        document_type="video_summary",
        video=video,
        round_row=None,
        start_time=0.0,
        end_time=video.duration_seconds or utterances[-1].end_time,
        text_value=video_text,
        utterances=utterances[:1],
    )
    created += 1
    session.commit()
    return created


def build_all_semantic_documents(session: Session, *, force: bool = False) -> int:
    total = 0
    videos = session.scalars(
        select(Video).where(Video.status == "completed").order_by(Video.id)
    ).all()
    for video in videos:
        total += build_semantic_documents_for_video(session, video, force=force)
    return total


def embed_pending_documents(
    session: Session,
    client: OpenRouterClient,
    *,
    batch_size: int = 48,
    limit: int | None = None,
) -> tuple[int, int]:
    completed = 0
    failed = 0
    while True:
        query = (
            select(EmbeddingJob)
            .where(EmbeddingJob.status.in_(["queued", "failed"]))
            .order_by(EmbeddingJob.id)
            .limit(batch_size)
        )
        jobs = list(session.scalars(query).all())
        if limit is not None:
            jobs = jobs[: max(0, limit - completed - failed)]
        if not jobs:
            break
        documents = [
            session.get(SemanticDocument, job.document_id) for job in jobs
        ]
        pairs = [
            (job, document)
            for job, document in zip(jobs, documents, strict=True)
            if document is not None
        ]
        if not pairs:
            break
        for job, _document in pairs:
            job.status = "running"
            job.attempt_count += 1
            job.error_message = None
        session.commit()
        try:
            result = client.embeddings(
                [document.text for _job, document in pairs],
                model=settings.embedding_model,
                input_type="document",
                dimensions=settings.embedding_dimensions,
            )
            if len(result.data) != len(pairs):
                raise ValueError("OpenRouter вернул неверное количество эмбеддингов")
            for (job, document), values in zip(pairs, result.data, strict=True):
                vector = np.asarray(values, dtype=np.float32)
                if vector.size != settings.embedding_dimensions:
                    raise ValueError(
                        f"Ожидалось {settings.embedding_dimensions} измерений, "
                        f"получено {vector.size}"
                    )
                norm = float(np.linalg.norm(vector))
                if not math.isfinite(norm) or norm <= 0:
                    raise ValueError("Получен пустой или некорректный вектор")
                vector /= norm
                existing = session.scalar(
                    select(EmbeddingVector).where(
                        EmbeddingVector.document_id == document.id,
                        EmbeddingVector.model == settings.embedding_model,
                        EmbeddingVector.dimensions == settings.embedding_dimensions,
                    )
                )
                if existing is None:
                    existing = EmbeddingVector(
                        document_id=document.id,
                        model=settings.embedding_model,
                        dimensions=settings.embedding_dimensions,
                        dtype="float32",
                        vector=b"",
                        content_hash=document.content_hash,
                    )
                    session.add(existing)
                existing.vector = vector.tobytes()
                existing.content_hash = document.content_hash
                job.status = "completed"
                job.completed_at = utcnow()
                completed += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            for job, _document in pairs:
                current = session.get(EmbeddingJob, job.id)
                if current is not None:
                    current.status = "failed"
                    current.error_message = str(exc)[:4000]
                    failed += 1
            session.commit()
            break
        if limit is not None and completed + failed >= limit:
            break
    return completed, failed


def _allowed_document_ids(session: Session, filters: SearchFilters) -> set[int]:
    statement = select(SemanticDocument.id)
    if filters.video_id is not None:
        statement = statement.where(SemanticDocument.video_id == filters.video_id)
    if filters.round_id is not None:
        statement = statement.where(SemanticDocument.round_id == filters.round_id)
    if filters.start_time is not None:
        statement = statement.where(SemanticDocument.end_time >= filters.start_time)
    if filters.end_time is not None:
        statement = statement.where(SemanticDocument.start_time <= filters.end_time)
    if filters.content_type:
        statement = statement.join(
            VideoEnrichment, VideoEnrichment.video_id == SemanticDocument.video_id
        ).where(VideoEnrichment.content_type == filters.content_type)
    if filters.winning_faction:
        statement = statement.join(
            MafiaRound, MafiaRound.id == SemanticDocument.round_id
        ).where(MafiaRound.winning_faction == filters.winning_faction)
    if filters.role_code:
        statement = (
            statement.join(
                MafiaRoundParticipant,
                MafiaRoundParticipant.round_id == SemanticDocument.round_id,
            )
            .join(MafiaRole, MafiaRole.id == MafiaRoundParticipant.role_id)
            .where(MafiaRole.code == filters.role_code)
        )
    if filters.profile_id is not None:
        statement = (
            statement.join(
                SemanticDocumentUtterance,
                SemanticDocumentUtterance.document_id == SemanticDocument.id,
            )
            .join(Utterance, Utterance.id == SemanticDocumentUtterance.utterance_id)
            .join(VideoSpeaker, VideoSpeaker.id == Utterance.speaker_id)
            .where(VideoSpeaker.profile_id == filters.profile_id)
        )
    return set(session.scalars(statement.distinct()).all())


def _fts_query(value: str) -> str:
    terms = re.findall(r"[\wёЁ-]+", value, flags=re.UNICODE)
    return " ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:30])


def hybrid_search(
    session: Session,
    client: OpenRouterClient,
    query: str,
    *,
    filters: SearchFilters | None = None,
    limit: int = 20,
) -> list[SearchHit]:
    filters = filters or SearchFilters()
    allowed = _allowed_document_ids(session, filters)
    if not allowed:
        return []
    query_result = client.embeddings(
        [query],
        model=settings.embedding_model,
        input_type="query",
        dimensions=settings.embedding_dimensions,
    )
    query_vector = np.asarray(query_result.data[0], dtype=np.float32)
    query_vector /= max(float(np.linalg.norm(query_vector)), 1e-12)

    vector_rows = session.execute(
        select(EmbeddingVector.document_id, EmbeddingVector.vector).where(
            EmbeddingVector.model == settings.embedding_model,
            EmbeddingVector.dimensions == settings.embedding_dimensions,
            EmbeddingVector.document_id.in_(allowed),
        )
    ).all()
    vector_scores: dict[int, float] = {}
    if vector_rows:
        matrix = np.vstack(
            [np.frombuffer(row.vector, dtype=np.float32) for row in vector_rows]
        )
        scores = matrix @ query_vector
        order = np.argsort(scores)[::-1][: max(limit * 5, 100)]
        vector_scores = {
            int(vector_rows[int(index)].document_id): float(scores[int(index)])
            for index in order
        }

    lexical_ids: list[int] = []
    match_query = _fts_query(query)
    if match_query:
        try:
            lexical_rows = session.execute(
                text(
                    "SELECT rowid FROM semantic_documents_fts "
                    "WHERE semantic_documents_fts MATCH :query "
                    "ORDER BY bm25(semantic_documents_fts) LIMIT 500"
                ),
                {"query": match_query},
            ).all()
            lexical_ids = [int(row[0]) for row in lexical_rows if int(row[0]) in allowed]
        except Exception:
            lexical_ids = []

    combined: dict[int, float] = {}
    for rank, document_id in enumerate(vector_scores, start=1):
        combined[document_id] = combined.get(document_id, 0.0) + 1.0 / (60 + rank)
    lexical_rank_map: dict[int, int] = {}
    for rank, document_id in enumerate(lexical_ids, start=1):
        lexical_rank_map[document_id] = rank
        combined[document_id] = combined.get(document_id, 0.0) + 1.0 / (60 + rank)
    best_ids = [
        document_id
        for document_id, _score in sorted(
            combined.items(), key=lambda item: item[1], reverse=True
        )[:limit]
    ]
    if not best_ids:
        return []

    documents = {
        row.id: row
        for row in session.scalars(
            select(SemanticDocument).where(SemanticDocument.id.in_(best_ids))
        ).all()
    }
    videos = {
        row.id: row
        for row in session.scalars(
            select(Video).where(Video.id.in_({doc.video_id for doc in documents.values()}))
        ).all()
    }
    round_ids = {doc.round_id for doc in documents.values() if doc.round_id is not None}
    rounds = {
        row.id: row
        for row in session.scalars(select(MafiaRound).where(MafiaRound.id.in_(round_ids))).all()
    }
    speaker_rows = session.execute(
        select(
            SemanticDocumentUtterance.document_id,
            VideoSpeaker.display_name,
        )
        .join(Utterance, Utterance.id == SemanticDocumentUtterance.utterance_id)
        .join(VideoSpeaker, VideoSpeaker.id == Utterance.speaker_id)
        .where(SemanticDocumentUtterance.document_id.in_(best_ids))
        .distinct()
    ).all()
    speakers: dict[int, list[str]] = {}
    for document_id, name in speaker_rows:
        speakers.setdefault(int(document_id), []).append(str(name))

    hits = []
    for document_id in best_ids:
        document = documents[document_id]
        video = videos[document.video_id]
        round_row = rounds.get(document.round_id)
        hits.append(
            SearchHit(
                document_id=document.id,
                score=combined[document.id],
                vector_score=vector_scores.get(document.id),
                lexical_rank=lexical_rank_map.get(document.id),
                document_type=document.document_type,
                video_id=video.id,
                video_title=video.title,
                round_id=round_row.id if round_row else None,
                round_number=round_row.round_number if round_row else None,
                start_time=document.start_time,
                end_time=document.end_time,
                text=document.text,
                speakers=sorted(speakers.get(document.id, [])),
            )
        )
    return hits


RAG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "insufficient_evidence", "citations"],
    "properties": {
        "answer": {"type": "string"},
        "insufficient_evidence": {"type": "boolean"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["document_id", "claim"],
                "properties": {
                    "document_id": {"type": "integer"},
                    "claim": {"type": "string"},
                },
            },
        },
    },
}


def answer_with_rag(
    session: Session,
    client: OpenRouterClient,
    question: str,
    *,
    filters: SearchFilters | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    hits = hybrid_search(session, client, question, filters=filters, limit=limit)
    if not hits:
        return {
            "answer": "В архиве не найдено достаточно данных для ответа.",
            "insufficient_evidence": True,
            "citations": [],
            "hits": [],
        }
    context = "\n\n".join(
        f"[DOC {hit.document_id}] {hit.video_title}; "
        f"{hit.start_time:.2f}-{hit.end_time:.2f}; {hit.text}"
        for hit in hits
    )
    result = client.structured_chat(
        model=settings.rag_answer_model,
        system=(
            "Отвечай по-русски только на основании переданных DOC. "
            "Каждое фактическое утверждение привязывай к document_id. "
            "Если доказательств не хватает, прямо сообщи об этом."
        ),
        user=f"Вопрос: {question}\n\nИсточники:\n{context}",
        schema_name="speaker_archive_rag_answer",
        schema=RAG_SCHEMA,
        reasoning_effort="medium",
    )
    allowed = {hit.document_id for hit in hits}
    citations = [
        item
        for item in result.data.get("citations", [])
        if int(item.get("document_id", -1)) in allowed
    ]
    return {
        "answer": result.data.get("answer", ""),
        "insufficient_evidence": bool(result.data.get("insufficient_evidence")),
        "citations": citations,
        "hits": [hit.to_dict() for hit in hits],
    }
